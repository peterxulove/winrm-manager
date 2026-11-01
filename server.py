#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WinRM Manager — 远程 Windows 进程管理服务

同时适用于 macOS 本地运行与飞牛 fnOS / 任意 Linux 部署。
除 pywinrm 外不依赖任何第三方库（HTTP 服务用标准库实现），
因此在 NAS 上无需 pip 安装即可运行。

设计要点：
  - 所有 PowerShell 脚本的输出统一走 Base64(UTF-8)，彻底规避中文乱码
  - 用户输入统一走 Base64(UTF-8) JSON 注入，规避 PowerShell 转义地狱
  - CPU 占用率由相邻两次采样差值计算（与任务管理器同原理）
"""

from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import os
import re
import secrets
import signal
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

# 内网 WinRM 必须直连。shell 里的代理变量会让 requests 把 192.168.x.x 送去代理，
# 表现是"连接超时"但 ping 正常，因此启动时强制清除。
for _k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy",
           "ALL_PROXY", "all_proxy"):
    os.environ.pop(_k, None)
os.environ["NO_PROXY"] = "*"

import requests  # noqa: E402
import winrm  # noqa: E402

APP_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(APP_DIR, "static")

VERSION = "1.0.0"

# 结束这些进程会直接导致蓝屏 / 注销 / 网络中断，需要显式确认
PROTECTED_NAMES = {
    "system", "registry", "memory compression", "smss", "csrss", "wininit",
    "winlogon", "services", "lsass", "lsaiso", "svchost", "fontdrvhost",
    "dwm", "spoolsv", "audiodg", "wudfhost",
}

# Web 访问密码（NAS 部署时由启动脚本注入，防止局域网内任何人操作远程主机）
WEB_PASSWORD = os.environ.get("WINRM_WEB_PASSWORD", "").strip()

# Job Object 的 P/Invoke 声明。用于给单个进程设内存硬上限。
# Windows 没有原生命令行接口，只能内嵌 C# 调 kernel32。
JOB_CS = r'''
using System;
using System.Runtime.InteropServices;
public class WbJob {
  [StructLayout(LayoutKind.Sequential)]
  public struct BASIC {
    public long A; public long B; public uint LimitFlags;
    public UIntPtr MinWS; public UIntPtr MaxWS; public uint ActiveLimit;
    public UIntPtr Affinity; public uint Priority; public uint Scheduling;
  }
  [StructLayout(LayoutKind.Sequential)]
  public struct IOC { public ulong a,b,c,d,e,f; }
  [StructLayout(LayoutKind.Sequential)]
  public struct EXT {
    public BASIC Basic; public IOC Io;
    public UIntPtr ProcessMemoryLimit; public UIntPtr JobMemoryLimit;
    public UIntPtr PeakProcess; public UIntPtr PeakJob;
  }
  [DllImport("kernel32.dll", CharSet=CharSet.Unicode)]
  public static extern IntPtr CreateJobObject(IntPtr a, string n);
  [DllImport("kernel32.dll")]
  public static extern bool SetInformationJobObject(IntPtr j, int c, IntPtr i, uint l);
  [DllImport("kernel32.dll")]
  public static extern bool AssignProcessToJobObject(IntPtr j, IntPtr p);
  [DllImport("kernel32.dll")]
  public static extern IntPtr OpenProcess(uint a, bool i, int pid);
  [DllImport("kernel32.dll")]
  public static extern bool CloseHandle(IntPtr h);
  [DllImport("kernel32.dll")]
  public static extern bool IsProcessInJob(IntPtr p, IntPtr j, out bool result);
}
'''


# ---------------------------------------------------------------- 异常

class RemoteError(Exception):
    """带用户可读中文信息的远程调用错误"""

    def __init__(self, message: str, detail: str = ""):
        super().__init__(message)
        self.message = message
        self.detail = detail


class HttpError(Exception):
    def __init__(self, status: int, message: str, **extra):
        super().__init__(message)
        self.status = status
        self.message = message
        self.extra = extra


# ---------------------------------------------------------------- WinRM 封装

class Remote:
    def __init__(self, host, username, password, port=5985,
                 transport="ntlm", use_ssl=False):
        self.host = host
        self.username = username
        self.port = int(port)
        self.transport = transport
        self.use_ssl = bool(use_ssl)
        scheme = "https" if self.use_ssl else "http"
        url = f"{scheme}://{host}:{self.port}/wsman"

        self._lock = threading.RLock()
        try:
            self.session = winrm.Session(
                url, auth=(username, password), transport=transport,
                operation_timeout_sec=45, read_timeout_sec=60,
                server_cert_validation="ignore",
            )
        except Exception as exc:
            raise RemoteError(f"初始化 WinRM 会话失败：{exc}") from exc

        # CPU 采样缓存：{(pid, startTime): 累计CPU秒}
        self._prev_cpu: dict = {}
        self._prev_ts: float = 0.0
        self._core_count: int = 1

    # -- 底层执行 ---------------------------------------------------

    def _run(self, script: str) -> str:
        """执行 PowerShell，返回纯文本 stdout"""
        with self._lock:
            try:
                res = self.session.run_ps(script)
            except winrm.exceptions.InvalidCredentialsError as exc:
                raise RemoteError("认证失败：用户名或密码错误", str(exc)) from exc
            except winrm.exceptions.BasicAuthDisabledError as exc:
                raise RemoteError(
                    "服务端未启用 Basic 认证，请改用 NTLM 或 Kerberos", str(exc)) from exc
            except winrm.exceptions.AuthenticationError as exc:
                raise RemoteError("认证失败：请检查账号、密码与认证方式", str(exc)) from exc
            except winrm.exceptions.WinRMTransportError as exc:
                raise RemoteError(
                    f"WinRM 传输错误（HTTP {getattr(exc, 'code', '?')}）",
                    str(exc)) from exc
            except requests.exceptions.ConnectTimeout as exc:
                raise RemoteError(f"连接 {self.host}:{self.port} 超时", str(exc)) from exc
            except requests.exceptions.ConnectionError as exc:
                raise RemoteError(
                    f"无法连接到 {self.host}:{self.port}，请确认 WinRM 服务已启动、防火墙已放行",
                    str(exc)) from exc
            except requests.exceptions.ReadTimeout as exc:
                raise RemoteError("远程执行超时（脚本耗时过长）", str(exc)) from exc
            except Exception as exc:
                raise RemoteError(
                    f"远程调用失败：{type(exc).__name__}", str(exc)) from exc

        out = res.std_out.decode("utf-8", "replace")
        err = res.std_err.decode("utf-8", "replace").strip()
        if res.status_code != 0:
            raise RemoteError("PowerShell 执行失败", err or out[:500])
        return out

    def run_json(self, script: str):
        """执行 PowerShell，把结果以 JSON 取回（Base64 保护中文）"""
        wrapper = (
            "$ErrorActionPreference='SilentlyContinue'\n"
            "$ProgressPreference='SilentlyContinue'\n"
            "$__wb = & {\n" + script + "\n}\n"
            "$__wbJson = if ($null -eq $__wb) { 'null' } "
            "else { ConvertTo-Json -InputObject @($__wb) -Depth 6 -Compress }\n"
            "[Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($__wbJson))\n"
        )
        out = self._run(wrapper)
        lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
        if not lines:
            return []
        try:
            payload = base64.b64decode(lines[-1]).decode("utf-8")
            data = json.loads(payload)
        except Exception as exc:
            raise RemoteError("解析远程返回数据失败",
                              f"{exc} | {lines[-1][:200]}") from exc
        if data is None:
            return []
        if isinstance(data, dict):
            return [data]
        return data

    @staticmethod
    def _payload(obj) -> str:
        """把 Python 对象编码成 Base64(UTF-8 JSON)，供脚本内安全解码"""
        raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        return base64.b64encode(raw).decode("ascii")

    def _decode_cfg_prologue(self, obj, var="__cfg") -> str:
        return (
            f"${var} = [Text.Encoding]::UTF8.GetString("
            f"[Convert]::FromBase64String('{self._payload(obj)}')) | ConvertFrom-Json\n"
        )

    # -- 业务能力 ---------------------------------------------------

    def ping(self) -> float:
        t0 = time.time()
        self._run("[Console]::Write('pong')")
        return round((time.time() - t0) * 1000, 1)

    def system_info(self) -> dict:
        data = self.run_json(r"""
$os = Get-CimInstance Win32_OperatingSystem
$cs = Get-CimInstance Win32_ComputerSystem
$cp = Get-CimInstance Win32_Processor | Select-Object -First 1
$uptime = (Get-Date) - $os.LastBootUpTime
[PSCustomObject]@{
  host      = $env:COMPUTERNAME
  os        = $os.Caption
  version   = $os.Version
  build     = $os.BuildNumber
  arch      = $os.OSArchitecture
  totalMem  = [int64]$cs.TotalPhysicalMemory
  freeMem   = [int64]$os.FreePhysicalMemory * 1024
  lastBoot  = $os.LastBootUpTime.ToString('o')
  uptimeSec = [int64]$uptime.TotalSeconds
  cpuName   = $cp.Name
  cores     = [int]$cs.NumberOfLogicalProcessors
  domain    = $cs.Domain
  logonUser = $env:USERNAME
}
""")
        info = data[0] if data else {}
        self._core_count = max(int(info.get("cores") or 1), 1)
        return info

    def processes(self) -> dict:
        rows = self.run_json(r"""
$procs = Get-Process -IncludeUserName -ErrorAction SilentlyContinue
if (-not $procs) { $procs = Get-Process -ErrorAction SilentlyContinue }
$procs | ForEach-Object {
  $p = $_
  $path = $null; try { $path = $p.Path } catch {}
  $st   = $null; try { $st = $p.StartTime.ToString('o') } catch {}
  $cpu  = $null; try { $cpu = $p.CPU } catch {}
  $un   = $null; try { $un = $p.UserName } catch {}
  $th   = 0;     try { $th = $p.Threads.Count } catch {}
  [PSCustomObject]@{
    pid     = $p.Id
    name    = $p.ProcessName
    cpu     = $cpu
    ws      = [int64]$p.WorkingSet64
    pm      = [int64]$p.PrivateMemorySize64
    vm      = [int64]$p.VirtualMemorySize64
    threads = [int]$th
    handles = [int]$p.HandleCount
    start   = $st
    path    = $path
    user    = $un
  }
}
""")

        now = time.time()
        dt = now - self._prev_ts if self._prev_ts else 0.0
        cores = max(self._core_count, 1)
        new_cache = {}
        for p in rows:
            key = (p.get("pid"), p.get("start"))
            cpu = p.get("cpu")
            pct = None
            if cpu is not None:
                prev = self._prev_cpu.get(key)
                if prev is not None and dt > 0.25:
                    delta = cpu - prev
                    if delta < 0:
                        delta = 0.0
                    pct = round(delta / dt / cores * 100.0, 1)
                new_cache[key] = cpu
            p["cpuPct"] = pct
            p["protected"] = (p.get("name") or "").lower() in PROTECTED_NAMES

        self._prev_cpu = new_cache
        self._prev_ts = now

        total_cpu = sum(p["cpuPct"] for p in rows if p["cpuPct"] is not None)
        stats = {
            "count": len(rows),
            "threads": sum(int(p.get("threads") or 0) for p in rows),
            "handles": sum(int(p.get("handles") or 0) for p in rows),
            "memUsed": sum(int(p.get("ws") or 0) for p in rows),
            "cpuPct": round(min(total_cpu, 100.0), 1),
        }
        return {"ts": now, "processes": rows, "stats": stats}

    def process_detail(self, pid: int) -> dict:
        script = self._decode_cfg_prologue({"pid": int(pid)}) + r"""
$__p = Get-CimInstance Win32_Process -Filter "ProcessId=$($__cfg.pid)"
if ($null -eq $__p) { return }
$__ps = Get-Process -Id $__cfg.pid -ErrorAction SilentlyContinue
$__owner = $null
try { $__owner = (Invoke-CimMethod -InputObject $__p -MethodName GetOwner).User } catch {}
$__parentName = $null
try { $__parentName = (Get-Process -Id $__p.ParentProcessId -ErrorAction Stop).ProcessName } catch {}
$__conns = @()
try {
  $__conns = Get-NetTCPConnection -OwningProcess $__cfg.pid -ErrorAction SilentlyContinue |
    ForEach-Object {
      [PSCustomObject]@{
        local  = "$($_.LocalAddress):$($_.LocalPort)"
        remote = "$($_.RemoteAddress):$($_.RemotePort)"
        state  = [string]$_.State
      }
    }
} catch {}
[PSCustomObject]@{
  pid         = $__cfg.pid
  name        = $__p.Name
  path        = $__p.ExecutablePath
  commandLine = $__p.CommandLine
  parentPid   = $__p.ParentProcessId
  parentName  = $__parentName
  owner       = $__owner
  sessionId   = $__p.SessionId
  createdAt   = if ($__p.CreationDate) { $__p.CreationDate.ToString('o') } else { $null }
  ws          = if ($__ps) { [int64]$__ps.WorkingSet64 } else { $null }
  pm          = if ($__ps) { [int64]$__ps.PrivateMemorySize64 } else { $null }
  vm          = if ($__ps) { [int64]$__ps.VirtualMemorySize64 } else { $null }
  threads     = if ($__ps) { [int]$__ps.Threads.Count } else { $null }
  handles     = if ($__ps) { [int]$__ps.HandleCount } else { $null }
  cpu         = if ($__ps) { $__ps.CPU } else { $null }
  priority    = if ($__ps) { [string]$__ps.PriorityClass } else { $null }
  responding  = if ($__ps) { $__ps.Responding } else { $null }
  connections = @($__conns)
}
"""
        data = self.run_json(script)
        if not data or data[0].get("name") is None:
            raise HttpError(404, f"进程 {pid} 不存在或已退出")
        return data[0]

    def names_of(self, pids) -> dict:
        """查一批 PID 对应的进程名，用于结束前的安全校验"""
        script = self._decode_cfg_prologue({"pids": [int(x) for x in pids]}) + r"""
$__out = @()
foreach ($__id in @($__cfg.pids)) {
  $__n = $null
  try { $__n = (Get-Process -Id $__id -ErrorAction Stop).ProcessName } catch {}
  $__out += [PSCustomObject]@{ pid = $__id; name = $__n }
}
$__out
"""
        rows = self.run_json(script)
        return {int(r["pid"]): r.get("name") for r in rows if r.get("pid") is not None}

    def kill(self, pids, force=True, tree=False) -> list:
        """
        结束进程。

        tree=True 时用 taskkill /T 连同子进程一起终止 —— 对启动器类进程
        （如 cmd → 实际业务程序）很关键，只杀父进程会留下孤儿进程。
        """
        script = self._decode_cfg_prologue(
            {"pids": [int(x) for x in pids], "force": bool(force),
             "tree": bool(tree)}) + r"""
$__out = @()
foreach ($__id in @($__cfg.pids)) {
  if ($__cfg.tree) {
    & taskkill /PID $__id /T /F 2>&1 | Out-Null
    $__code = $LASTEXITCODE
    $__out += [PSCustomObject]@{
      pid   = $__id
      ok    = ($__code -eq 0)
      error = if ($__code -ne 0) { "taskkill 退出码 $__code" } else { $null }
    }
  } else {
    try {
      Stop-Process -Id $__id -Force:$($__cfg.force) -ErrorAction Stop
      $__out += [PSCustomObject]@{ pid = $__id; ok = $true; error = $null }
    } catch {
      $__out += [PSCustomObject]@{
        pid = $__id; ok = $false; error = $_.Exception.Message
      }
    }
  }
}
$__out
"""
        return self.run_json(script)

    def start(self, exe: str, arguments: str = "", workdir: str = "") -> dict:
        """
        远程启动进程。

        WinRM 的会话是一个 Job Object，Start-Process 直接拉起的子进程会在会话
        结束时被一并回收（表现为"启动成功但立刻消失"）。因此这里走
        「计划任务」通道：任务由 Task Scheduler 服务拉起，完全脱离 WinRM 会话。
        计划任务 /tr 有 261 字符上限，故把真正的启动脚本落盘为 .ps1 再调用。
        """
        token = uuid.uuid4().hex[:10]
        ps1_path = f"C:\\Windows\\Temp\\wb_{token}.ps1"
        pid_path = f"C:\\Windows\\Temp\\wb_{token}.pid"
        task_name = f"WBLaunch_{token}"

        cfg = {"exe": exe, "args": arguments or "", "wd": workdir or "",
               "pidfile": pid_path}
        payload = self._payload(cfg)

        inner = "\n".join([
            "$cfg = [Text.Encoding]::UTF8.GetString("
            f"[Convert]::FromBase64String('{payload}')) | ConvertFrom-Json",
            "$sp = @{ FilePath = $cfg.exe; PassThru = $true }",
            "if (-not [string]::IsNullOrWhiteSpace($cfg.args)) "
            "{ $sp['ArgumentList'] = $cfg.args }",
            "if (-not [string]::IsNullOrWhiteSpace($cfg.wd)) "
            "{ $sp['WorkingDirectory'] = $cfg.wd }",
            "$p = Start-Process @sp",
            "$p.Id | Out-File -FilePath $cfg.pidfile -Encoding ascii -Force",
        ])

        script = f"""
$__ps1 = '{ps1_path}'
$__pidf = '{pid_path}'
$__tn = '{task_name}'
$__c = [Text.Encoding]::UTF8.GetString(
    [Convert]::FromBase64String('{payload}')) | ConvertFrom-Json

Set-Content -Path $__ps1 -Value @'
{inner}
'@ -Encoding UTF8

schtasks /create /tn $__tn /tr "powershell -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File $__ps1" `
  /sc once /st 00:00 /f /rl HIGHEST /ru SYSTEM 2>&1 | Out-Null
schtasks /run /tn $__tn 2>&1 | Out-Null

$__pid = 0
for ($i = 0; $i -lt 20; $i++) {{
  Start-Sleep -Milliseconds 200
  if (Test-Path $__pidf) {{
    $__txt = Get-Content $__pidf -Raw -ErrorAction SilentlyContinue
    if ($__txt) {{
      $__txt = $__txt.Trim()
      if ($__txt) {{ $__pid = [int]$__txt; break }}
    }}
  }}
}}

Start-Sleep -Milliseconds 350
schtasks /delete /tn $__tn /f 2>&1 | Out-Null
Remove-Item $__pidf -Force -ErrorAction SilentlyContinue
Remove-Item $__ps1 -Force -ErrorAction SilentlyContinue

$__name = $null
if ($__pid -gt 0) {{
  try {{ $__name = (Get-Process -Id $__pid -ErrorAction Stop).ProcessName }} catch {{}}
}}
if (-not $__name) {{ $__name = [System.IO.Path]::GetFileNameWithoutExtension($__c.exe) }}

[PSCustomObject]@{{
  pid  = [int]$__pid
  name = $__name
  mode = 'scheduled-task'
}}
"""
        data = self.run_json(script)
        if not data:
            raise RemoteError("启动失败：远程未返回结果")
        row = data[0]
        if not row.get("pid"):
            raise RemoteError(
                "启动失败：计划任务未能拉起进程，请检查可执行文件路径是否正确")
        return row

    def run_script(self, script: str) -> dict:
        """执行任意 PowerShell 片段，返回 stdout/stderr"""
        with self._lock:
            try:
                res = self.session.run_ps(script)
            except winrm.exceptions.AuthenticationError as exc:
                raise RemoteError("认证失败", str(exc)) from exc
            except requests.exceptions.RequestException as exc:
                raise RemoteError("网络错误", str(exc)) from exc
            except Exception as exc:
                raise RemoteError(
                    f"远程调用失败：{type(exc).__name__}", str(exc)) from exc
        return {
            "status": res.status_code,
            "stdout": res.std_out.decode("utf-8", "replace"),
            "stderr": res.std_err.decode("utf-8", "replace"),
        }

    # -- 资源限制 ---------------------------------------------------

    def _run_long(self, script: str):
        """
        执行较长的 PowerShell 脚本。

        WinRM 走 -EncodedCommand，命令行上限约 8K 字符（UTF-16LE base64 会翻倍），
        Add-Type 这类内嵌 C# 的脚本很容易超限。超长时分块写入远程临时文件，
        再用 Invoke-Expression 执行（目标机默认禁用脚本执行策略，不能用 & 文件）。
        """
        if len(script) < 2800:
            return self.run_json(script)

        payload = base64.b64encode(script.encode("utf-8")).decode("ascii")
        token = uuid.uuid4().hex[:10]
        tmp = f"C:\\Windows\\Temp\\wb_{token}.b64"
        chunk = 1200

        for i in range(0, len(payload), chunk):
            piece = payload[i:i + chunk]
            verb = "Set-Content" if i == 0 else "Add-Content"
            self._run(f"{verb} -Path '{tmp}' -Value '{piece}' -NoNewline -Encoding ascii")

        wrapper = (
            f"$__b = Get-Content '{tmp}' -Raw\n"
            f"$__code = [Text.Encoding]::UTF8.GetString("
            f"[Convert]::FromBase64String($__b))\n"
            f"Remove-Item '{tmp}' -Force -ErrorAction SilentlyContinue\n"
            "$__r = Invoke-Expression $__code\n"
            "$__j = if ($null -eq $__r) { 'null' } "
            "else { ConvertTo-Json -InputObject @($__r) -Depth 6 -Compress }\n"
            "[Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($__j))\n"
        )
        out = self._run(wrapper)
        lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
        if not lines:
            return []
        try:
            data = json.loads(base64.b64decode(lines[-1]).decode("utf-8"))
        except Exception as exc:
            raise RemoteError("解析远程返回数据失败", f"{exc} | {lines[-1][:200]}") from exc
        if data is None:
            return []
        return [data] if isinstance(data, dict) else data

    def limits_of(self, pid: int) -> dict:
        """读取进程当前的 CPU 亲和性、优先级（核数由调用方补齐，避免慢查询）"""
        script = self._decode_cfg_prologue({"pid": int(pid)}) + r"""
$__p = Get-Process -Id $__cfg.pid -ErrorAction SilentlyContinue
if ($null -eq $__p) { return }
$__aff = 0
try { $__aff = [int64]$__p.ProcessorAffinity } catch {}
[PSCustomObject]@{
  pid      = $__cfg.pid
  affinity = $__aff
  priority = [string]$__p.PriorityClass
  threads  = [int]$__p.Threads.Count
  wsMb     = [math]::Round($__p.WorkingSet64 / 1MB, 1)
}
"""
        data = self.run_json(script)
        if not data or data[0].get("pid") is None:
            raise HttpError(404, f"进程 {pid} 不存在或已退出")
        return data[0]

    def set_cpu_limit(self, pid: int, affinity_mask=None, priority=None) -> dict:
        """设置 CPU 亲和性掩码和/或优先级"""
        cfg = {"pid": int(pid), "aff": affinity_mask, "pri": priority}
        script = self._decode_cfg_prologue(cfg) + r"""
$__p = Get-Process -Id $__cfg.pid -ErrorAction SilentlyContinue
if ($null -eq $__p) { return [PSCustomObject]@{ ok = $false; error = '进程不存在' } }

$__affOk = $null
if ($null -ne $__cfg.aff) {
  try {
    $__p.ProcessorAffinity = [IntPtr][int64]$__cfg.aff
    $__now = [int64](Get-Process -Id $__cfg.pid).ProcessorAffinity
    $__affOk = ($__now -eq [int64]$__cfg.aff)
  } catch { $__affOk = $false }
}

$__priOk = $null
if (-not [string]::IsNullOrWhiteSpace($__cfg.pri)) {
  try {
    $__p.PriorityClass = [System.Diagnostics.ProcessPriorityClass]$__cfg.pri
    $__priOk = ([string](Get-Process -Id $__cfg.pid).PriorityClass -eq $__cfg.pri)
  } catch { $__priOk = $false }
}

[PSCustomObject]@{
  ok         = (($__affOk -ne $false) -and ($__priOk -ne $false))
  affinityOk = $__affOk
  priorityOk = $__priOk
  affinity   = [int64](Get-Process -Id $__cfg.pid).ProcessorAffinity
  priority   = [string](Get-Process -Id $__cfg.pid).PriorityClass
}
"""
        data = self.run_json(script)
        if not data:
            raise RemoteError("设置失败：远程未返回结果")
        return data[0]

    def set_memory_limit(self, pid: int, limit_mb: int) -> dict:
        """
        通过 Job Object 给进程设内存硬上限。

        注意：这是硬限制 —— 超过上限时内存分配直接失败，进程通常崩溃退出。
        且 Job 对象在设置会话结束后无法再按名打开，因此**无法远程解除**，
        只能重启该进程。
        """
        script = f"""
$ErrorActionPreference = 'SilentlyContinue'
Add-Type -TypeDefinition @'
{JOB_CS}
'@
$__procId = {int(pid)}
$__bytes  = [uint64]{int(limit_mb)} * 1048576

if (-not (Get-Process -Id $__procId -ErrorAction SilentlyContinue)) {{
  [PSCustomObject]@{{ ok = $false; error = '进程不存在' }}
  return
}}

$__job = [WbJob]::CreateJobObject([IntPtr]::Zero, $null)
if ($__job -eq [IntPtr]::Zero) {{
  [PSCustomObject]@{{ ok = $false; error = 'CreateJobObject 失败' }}
  return
}}

$__info = New-Object WbJob+EXT
$__info.Basic.LimitFlags = 0x200          # JOB_OBJECT_LIMIT_PROCESS_MEMORY
$__info.ProcessMemoryLimit = [UIntPtr]::new($__bytes)
$__sz  = [System.Runtime.InteropServices.Marshal]::SizeOf($__info)
$__ptr = [System.Runtime.InteropServices.Marshal]::AllocHGlobal($__sz)
[System.Runtime.InteropServices.Marshal]::StructureToPtr($__info, $__ptr, $false)
$__set = [WbJob]::SetInformationJobObject($__job, 9, $__ptr, $__sz)
[System.Runtime.InteropServices.Marshal]::FreeHGlobal($__ptr)

$__h = [WbJob]::OpenProcess(0x1F0FFF, $false, $__procId)
if ($__h -eq [IntPtr]::Zero) {{
  [PSCustomObject]@{{ ok = $false; error = 'OpenProcess 失败（权限不足）' }}
  return
}}
$__asg = [WbJob]::AssignProcessToJobObject($__job, $__h)
$__inJob = $false
[WbJob]::IsProcessInJob($__h, [IntPtr]::Zero, [ref]$__inJob) | Out-Null
[WbJob]::CloseHandle($__h) | Out-Null

[PSCustomObject]@{{
  ok        = ($__set -and $__asg)
  setOk     = $__set
  assignOk  = $__asg
  inJob     = $__inJob
  limitMb   = {int(limit_mb)}
}}
"""
        data = self._run_long(script)
        if not data:
            raise RemoteError("设置失败：远程未返回结果")
        return data[0]

    # -- 网速（QoS，按程序名）----------------------------------------

    def qos_list(self) -> list:
        return self.run_json(r"""
Get-NetQosPolicy -ErrorAction SilentlyContinue |
  Where-Object { $_.Name -like 'WB_*' } |
  ForEach-Object {
    [PSCustomObject]@{
      name     = $_.Name
      appPath  = $_.AppPathName
      throttle = $_.ThrottleRateAction
    }
  }
""")

    def qos_set(self, exe: str, kbps: int) -> dict:
        """按可执行文件名限速（Windows QoS 只能匹配应用路径，无法按 PID）"""
        name = "WB_" + re.sub(r"[^A-Za-z0-9_.-]", "_", exe)[:60]
        bps = int(kbps) * 1024
        script = self._decode_cfg_prologue(
            {"name": name, "exe": exe, "bps": bps}) + r"""
Remove-NetQosPolicy -Name $__cfg.name -Confirm:$false -ErrorAction SilentlyContinue
try {
  New-NetQosPolicy -Name $__cfg.name -AppPathNameMatchCondition $__cfg.exe `
    -ThrottleRateActionBitsPerSecond ([uint64]$__cfg.bps) -ErrorAction Stop | Out-Null
  [PSCustomObject]@{ ok = $true; name = $__cfg.name }
} catch {
  [PSCustomObject]@{ ok = $false; error = $_.Exception.Message }
}
"""
        data = self.run_json(script)
        if not data:
            raise RemoteError("设置失败：远程未返回结果")
        return data[0]

    def qos_remove(self, name: str) -> dict:
        script = self._decode_cfg_prologue({"name": name}) + r"""
try {
  Remove-NetQosPolicy -Name $__cfg.name -Confirm:$false -ErrorAction Stop
  [PSCustomObject]@{ ok = $true }
} catch {
  [PSCustomObject]@{ ok = $false; error = $_.Exception.Message }
}
"""
        data = self.run_json(script)
        return data[0] if data else {"ok": False, "error": "无返回"}


# ---------------------------------------------------------------- 应用状态

class AppState:
    def __init__(self):
        self.lock = threading.RLock()
        self.remote = None
        self.profile: dict = {}
        self.system: dict = {}
        self.logs: deque = deque(maxlen=400)
        self.tokens: set = set()
        # 内存上限无法从远程回读（Job 句柄已关闭），只能服务端自己记账
        self.mem_limits: dict = {}

    def log(self, level: str, action: str, detail: str = ""):
        with self.lock:
            self.logs.append({"ts": time.time(), "level": level,
                              "action": action, "detail": detail})


STATE = AppState()


# ---------------------------------------------------------------- 路由

ROUTES = []


def route(method: str, pattern: str):
    """注册路由；pattern 用正则，命名分组会作为关键字参数传给处理函数"""
    compiled = re.compile(f"^{pattern}$")

    def deco(fn):
        ROUTES.append((method, compiled, fn))
        return fn
    return deco


def need_remote() -> Remote:
    if STATE.remote is None:
        raise HttpError(409, "尚未连接到远程主机")
    return STATE.remote


# -- 元信息

@route("GET", r"/api/health")
def api_health(body):
    return {"ok": True, "app": "winrm-manager", "version": VERSION,
            "authRequired": bool(WEB_PASSWORD)}


@route("POST", r"/api/login")
def api_login(body):
    if not WEB_PASSWORD:
        return {"ok": True, "token": ""}
    if (body.get("password") or "") != WEB_PASSWORD:
        raise HttpError(401, "访问密码错误")
    token = secrets.token_urlsafe(24)
    with STATE.lock:
        STATE.tokens.add(token)
    return {"ok": True, "token": token}


# -- 连接

@route("POST", r"/api/connect")
def api_connect(body):
    host = (body.get("host") or "").strip()
    username = (body.get("username") or "").strip()
    password = body.get("password") or ""
    port = int(body.get("port") or 5985)
    transport = body.get("transport") or "ntlm"
    use_ssl = bool(body.get("use_ssl"))

    if not host or not username:
        raise HttpError(400, "请填写主机地址与用户名")
    if not password:
        raise HttpError(400, "请填写密码")

    remote = Remote(host, username, password, port, transport, use_ssl)
    latency = remote.ping()
    info = remote.system_info()

    with STATE.lock:
        STATE.remote = remote
        STATE.system = info
        STATE.profile = {"host": host, "username": username, "port": port,
                         "transport": transport, "use_ssl": use_ssl}

    STATE.log("success", "连接成功", f"{host} · {info.get('host', '')} · {latency}ms")
    return {"ok": True, "latency": latency, "system": info,
            "profile": STATE.profile}


@route("POST", r"/api/disconnect")
def api_disconnect(body):
    with STATE.lock:
        STATE.remote = None
        STATE.system = {}
    STATE.log("info", "已断开连接")
    return {"ok": True}


@route("GET", r"/api/session")
def api_session(body):
    if STATE.remote is None:
        return {"ok": True, "connected": False}
    return {"ok": True, "connected": True,
            "profile": STATE.profile, "system": STATE.system}


# -- 数据

@route("GET", r"/api/system")
def api_system(body):
    remote = need_remote()
    info = remote.system_info()
    with STATE.lock:
        STATE.system = info
    return {"ok": True, "system": info}


@route("GET", r"/api/ping")
def api_ping(body):
    return {"ok": True, "latency": need_remote().ping()}


@route("GET", r"/api/processes")
def api_processes(body):
    remote = need_remote()
    t0 = time.time()
    data = remote.processes()
    data["elapsedMs"] = round((time.time() - t0) * 1000)
    return {"ok": True, **data}


@route("GET", r"/api/process/(?P<pid>\d+)")
def api_process_detail(body, pid):
    return {"ok": True, "detail": need_remote().process_detail(int(pid))}


@route("POST", r"/api/kill")
def api_kill(body):
    remote = need_remote()
    pids = body.get("pids") or []
    force = bool(body.get("force", True))
    tree = bool(body.get("tree", False))
    allow_protected = bool(body.get("allowProtected"))

    if not pids:
        raise HttpError(400, "未指定进程")

    if not allow_protected:
        names = remote.names_of(pids)
        blocked = [pid for pid, nm in names.items()
                   if (nm or "").lower() in PROTECTED_NAMES]
        if blocked:
            detail = "、".join(f"{nm}({pid})" for pid, nm in names.items()
                               if pid in blocked)
            raise HttpError(403, f"包含系统关键进程：{detail}，已拦截",
                            blocked=blocked)

    result = remote.kill(pids, force, tree)
    ok_count = sum(1 for r in result if r.get("ok"))
    for r in result:
        if r.get("ok"):
            STATE.log("success", "结束进程树" if tree else "结束进程",
                      f"PID {r['pid']}")
        else:
            STATE.log("error", "结束进程失败", f"PID {r['pid']} · {r.get('error')}")
    return {"ok": True, "results": result, "killed": ok_count,
            "total": len(result), "tree": tree}


@route("POST", r"/api/start")
def api_start(body):
    exe = (body.get("exe") or "").strip()
    if not exe:
        raise HttpError(400, "请填写可执行文件路径")
    info = need_remote().start(exe, body.get("args") or "", body.get("workdir") or "")
    STATE.log("success", "启动进程", f"{exe} → PID {info.get('pid')}")
    return {"ok": True, "process": info}


@route("POST", r"/api/script")
def api_script(body):
    script = body.get("script") or ""
    if not script.strip():
        raise HttpError(400, "脚本为空")
    result = need_remote().run_script(script)
    STATE.log("info", "执行脚本", script.strip().splitlines()[0][:80])
    return {"ok": True, **result}


@route("GET", r"/api/logs")
def api_logs(body):
    with STATE.lock:
        return {"ok": True, "logs": list(STATE.logs)}


# -- 资源限制

@route("GET", r"/api/limits/(?P<pid>\d+)")
def api_limits(body, pid):
    info = need_remote().limits_of(int(pid))
    info["cores"] = STATE.system.get("cores") or 1
    with STATE.lock:
        info["memLimitMb"] = STATE.mem_limits.get(int(pid))
    return {"ok": True, "limits": info}


@route("POST", r"/api/limits/(?P<pid>\d+)")
def api_set_limits(body, pid):
    remote = need_remote()
    pid = int(pid)
    affinity = body.get("affinity")
    priority = body.get("priority")
    mem_mb = body.get("memoryLimitMb")

    result = {"ok": True}

    if affinity is not None or priority:
        r = remote.set_cpu_limit(pid, affinity, priority)
        if not r.get("ok"):
            raise RemoteError(
                "CPU 限制设置失败",
                "亲和性=%s 优先级=%s" % (r.get("affinityOk"), r.get("priorityOk")))
        result["cpu"] = r
        STATE.log("success", "调整 CPU 限制",
                  f"PID {pid} · 亲和性={r.get('affinity')} · 优先级={r.get('priority')}")

    if mem_mb:
        r = remote.set_memory_limit(pid, int(mem_mb))
        if not r.get("ok"):
            raise RemoteError(
                "内存限制设置失败",
                f"SetInfo={r.get('setOk')} Assign={r.get('assignOk')} "
                f"{r.get('error', '')}")
        with STATE.lock:
            STATE.mem_limits[pid] = int(mem_mb)
        result["memory"] = r
        STATE.log("warn", "设置内存上限", f"PID {pid} → {mem_mb} MB")

    return result


# -- 网速限制（QoS，按程序名）

@route("GET", r"/api/netlimit")
def api_netlimit_list(body):
    return {"ok": True, "policies": need_remote().qos_list()}


@route("POST", r"/api/netlimit")
def api_netlimit_set(body):
    exe = (body.get("exe") or "").strip()
    try:
        kbps = int(body.get("kbps") or 0)
    except (TypeError, ValueError):
        raise HttpError(400, "限速值必须是数字")
    if not exe:
        raise HttpError(400, "请提供程序名（如 chrome.exe）")
    if kbps <= 0:
        raise HttpError(400, "限速值必须大于 0")

    r = need_remote().qos_set(exe, kbps)
    if not r.get("ok"):
        raise RemoteError("网速限制设置失败", r.get("error", ""))
    STATE.log("success", "设置网速限制", f"{exe} → {kbps} KB/s")
    return {"ok": True, "policy": r}


@route("POST", r"/api/netlimit/remove")
def api_netlimit_remove(body):
    name = (body.get("name") or "").strip()
    if not name:
        raise HttpError(400, "缺少策略名")
    r = need_remote().qos_remove(name)
    if not r.get("ok"):
        raise RemoteError("删除限速策略失败", r.get("error", ""))
    STATE.log("info", "移除网速限制", name)
    return {"ok": True}


# ---------------------------------------------------------------- HTTP 服务

class Handler(BaseHTTPRequestHandler):
    server_version = f"WinRM-Manager/{VERSION}"
    protocol_version = "HTTP/1.1"

    # -- 响应工具 ---------------------------------------------------

    def _send(self, status: int, payload: bytes, ctype: str,
              extra_headers=None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)

    def _json(self, status: int, obj):
        payload = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self._send(status, payload, "application/json; charset=utf-8")

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        try:
            raw = self.rfile.read(length)
            data = json.loads(raw.decode("utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def log_message(self, fmt, *args):
        # 只记录错误，避免每 3 秒的轮询刷屏
        if args and str(args[1] if len(args) > 1 else "").startswith(("4", "5")):
            sys.stderr.write("[%s] %s\n" % (self.log_date_time_string(), fmt % args))

    # -- 鉴权 -------------------------------------------------------

    def _authorized(self, path: str) -> bool:
        if not WEB_PASSWORD:
            return True
        if path in ("/api/health", "/api/login"):
            return True
        token = self.headers.get("X-Auth-Token") or ""
        with STATE.lock:
            return token in STATE.tokens

    # -- 静态文件 ---------------------------------------------------

    def _serve_static(self, path: str) -> bool:
        if path in ("/", "/index.html"):
            rel = "index.html"
        elif path.startswith("/static/"):
            rel = path[len("/static/"):]
        else:
            return False

        # 防目录穿越
        target = os.path.normpath(os.path.join(STATIC_DIR, rel))
        if not target.startswith(STATIC_DIR + os.sep) and target != STATIC_DIR:
            self._json(403, {"ok": False, "error": "forbidden"})
            return True
        if not os.path.isfile(target):
            return False

        ctype = mimetypes.guess_type(target)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in (
                "application/javascript", "application/json"):
            ctype += "; charset=utf-8"
        with open(target, "rb") as fh:
            payload = fh.read()
        self._send(200, payload, ctype)
        return True

    # -- 分发 -------------------------------------------------------

    def _dispatch(self, method: str):
        parsed = urlparse(self.path)
        path = parsed.path

        if method == "GET" and self._serve_static(path):
            return

        if not path.startswith("/api/"):
            self._json(404, {"ok": False, "error": "not found"})
            return

        if not self._authorized(path):
            self._json(401, {"ok": False, "error": "需要访问密码",
                             "authRequired": True})
            return

        body = self._read_json() if method == "POST" else {}

        for m, rx, fn in ROUTES:
            if m != method:
                continue
            match = rx.match(path)
            if not match:
                continue
            try:
                result = fn(body, **match.groupdict())
                self._json(200, result)
            except HttpError as exc:
                payload = {"ok": False, "error": exc.message}
                payload.update(exc.extra)
                self._json(exc.status, payload)
            except RemoteError as exc:
                self._json(502, {"ok": False, "error": exc.message,
                                 "detail": exc.detail})
            except Exception as exc:
                self._json(500, {"ok": False,
                                 "error": f"{type(exc).__name__}: {exc}"})
            return

        self._json(404, {"ok": False, "error": f"未知接口 {path}"})

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_HEAD(self):
        self._dispatch("GET")


# ---------------------------------------------------------------- 入口

def main():
    parser = argparse.ArgumentParser(description="WinRM Manager")
    parser.add_argument("--port", type=int,
                        default=int(os.environ.get("WINRM_PORT", 5178)))
    parser.add_argument("--host", default=os.environ.get("WINRM_HOST", "127.0.0.1"))
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--pidfile", default="")
    args = parser.parse_args()

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.daemon_threads = True

    if args.pidfile:
        try:
            with open(args.pidfile, "w", encoding="utf-8") as fh:
                fh.write(str(os.getpid()))
        except OSError:
            pass

    def _shutdown(signum, frame):
        threading.Thread(target=server.shutdown, daemon=True).start()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _shutdown)
        except (ValueError, OSError):
            pass

    shown_host = "127.0.0.1" if args.host in ("0.0.0.0", "::") else args.host
    url = f"http://{shown_host}:{args.port}"
    print(f"\n  WinRM Manager {VERSION}  →  {url}")
    print(f"  监听 {args.host}:{args.port}"
          f"{'  |  已启用访问密码' if WEB_PASSWORD else ''}\n", flush=True)

    if not args.no_browser and args.host in ("127.0.0.1", "localhost"):
        threading.Timer(0.8, lambda: _open_browser(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def _open_browser(url: str):
    for cmd in (["open", url], ["xdg-open", url]):
        try:
            subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
            return
        except Exception:
            continue


if __name__ == "__main__":
    main()
