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
import tempfile
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

VERSION = "1.1.0"

# 结束这些进程会直接导致蓝屏 / 注销 / 网络中断，需要显式确认
PROTECTED_NAMES = {
    "system", "registry", "memory compression", "smss", "csrss", "wininit",
    "winlogon", "services", "lsass", "lsaiso", "svchost", "fontdrvhost",
    "dwm", "spoolsv", "audiodg", "wudfhost",
}

# 反作弊 / 安全组件。这些进程即使名字像游戏也绝不能限流：
# 降优先级或改亲和性可能被判定为篡改运行环境，导致掉线甚至封号。
NEVER_LIMIT = {
    "easyanticheat", "easyanticheat_eos", "easyanticheat_launcher",
    "beservice", "battleye", "beservice_x64",
    "vgc", "vgk", "vgtray",
    "tenprotect", "tenfight", "tenio", "tp3helper",
    "anticheatexpert", "ace-guard", "acegame",
    "nprotect", "npggnt", "gameguard",
    "xhunter1", "mhyprot2", "mhyprot3",
}

# 常见 Windows 游戏进程名（Get-Process 的 ProcessName，不含 .exe，小写）
# 分组只用于界面展示，匹配时一律用 proc 字段
DEFAULT_GAMES = [
    # ---- 竞技 / 网游 ----
    ("league of legends", "英雄联盟", "竞技"),
    ("leagueclient", "英雄联盟客户端", "竞技"),
    ("leagueclientux", "英雄联盟客户端", "竞技"),
    ("leagueclientuxrender", "英雄联盟客户端", "竞技"),
    ("valorant", "无畏契约 / VALORANT", "竞技"),
    ("valorant-win64-shipping", "无畏契约 / VALORANT", "竞技"),
    ("cs2", "反恐精英 2", "竞技"),
    ("csgo", "反恐精英 CS:GO", "竞技"),
    ("dota2", "Dota 2", "竞技"),
    ("tslgame", "绝地求生 PUBG", "竞技"),
    ("r5apex", "Apex 英雄", "竞技"),
    ("overwatch", "守望先锋", "竞技"),
    ("narakabladepoint", "永劫无间", "竞技"),
    ("deltaforceclient", "三角洲行动", "竞技"),
    ("deltaforce", "三角洲行动", "竞技"),
    ("crossfire", "穿越火线", "竞技"),
    ("dnf", "地下城与勇士", "竞技"),
    ("nsh", "逆水寒", "竞技"),
    ("jx3client", "剑网 3", "竞技"),
    ("wow", "魔兽世界", "竞技"),
    ("wowclassic", "魔兽世界怀旧服", "竞技"),
    ("diablo iii", "暗黑破坏神 3", "竞技"),
    ("diablo iv", "暗黑破坏神 4", "竞技"),
    ("sc2", "星际争霸 2", "竞技"),
    ("war3", "魔兽争霸 3", "竞技"),
    ("ffxiv", "最终幻想 14", "竞技"),
    ("ffxiv_dx11", "最终幻想 14", "竞技"),
    ("gw2-64", "激战 2", "竞技"),
    ("blackdesert64", "黑色沙漠", "竞技"),
    ("lostark", "命运方舟", "竞技"),
    ("newworld", "新世界", "竞技"),
    ("eso64", "上古卷轴 OL", "竞技"),
    ("pathofexile", "流放之路", "竞技"),
    ("pathofexilesteam", "流放之路", "竞技"),
    ("worldoftanks", "坦克世界", "竞技"),
    ("worldofwarships", "战舰世界", "竞技"),
    ("aces", "战争雷霆", "竞技"),
    ("rainbowsix", "彩虹六号", "竞技"),
    ("rainbowsix_dx11", "彩虹六号", "竞技"),
    ("rainbowsix_vulkan", "彩虹六号", "竞技"),
    ("thedivision2", "全境封锁 2", "竞技"),
    ("destiny2", "命运 2", "竞技"),
    ("rocketleague", "火箭联盟", "竞技"),
    ("fallguys_client", "糖豆人", "竞技"),
    ("maplestory", "冒险岛", "竞技"),
    ("kartrider", "跑跑卡丁车", "竞技"),
    ("escapefromtarkov", "逃离塔科夫", "竞技"),
    ("rustclient", "Rust", "竞技"),

    # ---- 单机 / 3A ----
    ("gta5", "侠盗猎车手 5", "单机"),
    ("gtav", "侠盗猎车手 5", "单机"),
    ("rdr2", "荒野大镖客 2", "单机"),
    ("cyberpunk2077", "赛博朋克 2077", "单机"),
    ("eldenring", "艾尔登法环", "单机"),
    ("sekiro", "只狼", "单机"),
    ("b1-win64-shipping", "黑神话：悟空", "单机"),
    ("bg3", "博德之门 3", "单机"),
    ("bg3_dx11", "博德之门 3", "单机"),
    ("witcher3", "巫师 3", "单机"),
    ("starfield", "星空", "单机"),
    ("hogwartslegacy", "霍格沃茨之遗", "单机"),
    ("itakestwo", "双人成行", "单机"),
    ("nms", "无人深空", "单机"),
    ("acorigins", "刺客信条：起源", "单机"),
    ("acodyssey", "刺客信条：奥德赛", "单机"),
    ("acvalhalla", "刺客信条：英灵殿", "单机"),
    ("acmirage", "刺客信条：幻景", "单机"),
    ("farcry5", "孤岛惊魂 5", "单机"),
    ("farcry6", "孤岛惊魂 6", "单机"),
    ("grb", "幽灵行动：断点", "单机"),
    ("forzahorizon5", "极限竞速：地平线 5", "单机"),
    ("forzahorizon4", "极限竞速：地平线 4", "单机"),
    ("f1_23", "F1 23", "单机"),
    ("f1_24", "F1 24", "单机"),
    ("f1_25", "F1 25", "单机"),
    ("nba2k24", "NBA 2K24", "单机"),
    ("nba2k25", "NBA 2K25", "单机"),
    ("fc24", "EA SPORTS FC 24", "单机"),
    ("fc25", "EA SPORTS FC 25", "单机"),
    ("ts4_x64", "模拟人生 4", "单机"),
    ("cities", "城市：天际线", "单机"),
    ("citiesskylines", "城市：天际线", "单机"),
    ("civilizationvi", "文明 6", "单机"),
    ("warhammer3", "全面战争：战锤 3", "单机"),
    ("aoe2de", "帝国时代 2 决定版", "单机"),
    ("aoe3de", "帝国时代 3 决定版", "单机"),
    ("aoe4", "帝国时代 4", "单机"),
    ("reliccardinal", "帝国时代 4", "单机"),
    ("anno1800", "纪元 1800", "单机"),
    ("frostpunk2", "冰汽时代 2", "单机"),
    ("stronghold", "要塞", "单机"),
    ("terraria", "泰拉瑞亚", "单机"),
    ("stardew valley", "星露谷物语", "单机"),
    ("dontstarve", "饥荒", "单机"),
    ("dontstarve_steam", "饥荒联机版", "单机"),
    ("oxygennotincluded", "缺氧", "单机"),
    ("rimworldwin64", "环世界", "单机"),
    ("factorio", "异星工厂", "单机"),
    ("subnautica", "深海迷航", "单机"),
    ("subnauticazero", "深海迷航：零度之下", "单机"),
    ("valheim", "英灵神殿", "单机"),
    ("palworld-win64-shipping", "幻兽帕鲁", "单机"),
    ("lethalcompany", "致命公司", "单机"),
    ("oncehuman", "七日世界", "单机"),
    ("helldivers2", "绝地潜兵 2", "单机"),
    ("remnant2", "遗迹 2", "单机"),
    ("armoredcore6", "装甲核心 6", "单机"),
    ("liesofp", "匹诺曹的谎言", "单机"),
    ("atomicheart", "原子之心", "单机"),
    ("re4", "生化危机 4 重制版", "单机"),
    ("re2", "生化危机 2 重制版", "单机"),
    ("re3", "生化危机 3 重制版", "单机"),
    ("monsterhunterworld", "怪物猎人：世界", "单机"),
    ("monsterhunterrise", "怪物猎人：崛起", "单机"),
    ("dmc5", "鬼泣 5", "单机"),
    ("nierautomatagame", "尼尔：机械纪元", "单机"),
    ("ghostoftsushima", "对马岛之魂", "单机"),
    ("spiderman", "漫威蜘蛛侠", "单机"),
    ("spiderman2", "漫威蜘蛛侠 2", "单机"),
    ("horizonzerodawn", "地平线：零之曙光", "单机"),
    ("daysgone", "往日不再", "单机"),
    ("deathstranding", "死亡搁浅", "单机"),
    ("tesv", "上古卷轴 5", "单机"),
    ("skyrimse", "上古卷轴 5 特别版", "单机"),
    ("fallout4", "辐射 4", "单机"),
    ("fallout76", "辐射 76", "单机"),
    ("doometernal", "毁灭战士：永恒", "单机"),
    ("borderlands3", "无主之地 3", "单机"),
    ("shootergame", "方舟：生存进化", "单机"),
    ("7daystodie", "七日杀", "单机"),
    ("conansandbox", "流放者柯南", "单机"),
    ("factorygame", "幸福工厂", "单机"),
    ("astroneer", "异星探险家", "单机"),
    ("elitedangerous64", "精英：危险", "单机"),
    ("starcitizen", "星际公民", "单机"),
    ("minecraft.windows", "我的世界（基岩版）", "单机"),
    ("robloxplayerbeta", "Roblox", "单机"),
    ("robloxplayer", "Roblox", "单机"),
    ("robloxstudio", "Roblox Studio", "单机"),
    ("among us", "Among Us", "单机"),
    ("hollow_knight", "空洞骑士", "单机"),
    ("hades", "哈迪斯", "单机"),
    ("hades2", "哈迪斯 2", "单机"),
    ("deadcells", "死亡细胞", "单机"),
    ("slaythespire", "杀戮尖塔", "单机"),
    ("balatro", "小丑牌", "单机"),
    ("vampire survivors", "吸血鬼幸存者", "单机"),
    ("cuphead", "茶杯头", "单机"),

    # ---- 平台 / 启动器 ----
    ("steam", "Steam", "平台"),
    ("steamwebhelper", "Steam 内嵌浏览器", "平台"),
    ("epicgameslauncher", "Epic Games 启动器", "平台"),
    ("wegame", "WeGame", "平台"),
    ("battle.net", "战网", "平台"),
    ("eadesktop", "EA App", "平台"),
    ("origin", "Origin", "平台"),
    ("ubisoftconnect", "Ubisoft Connect", "平台"),
    ("uplay", "Uplay", "平台"),
    ("goggalaxy", "GOG Galaxy", "平台"),
    ("galaxyclient", "GOG Galaxy", "平台"),
    ("riotclientservices", "Riot 客户端", "平台"),
    ("playnite", "Playnite", "平台"),

    # ---- 手游模拟器 ----
    ("ldplayer", "雷电模拟器", "模拟器"),
    ("dnplayer", "雷电模拟器", "模拟器"),
    ("bluestacks", "BlueStacks", "模拟器"),
    ("bluestacks_nxt", "BlueStacks", "模拟器"),
    ("hd-player", "逍遥模拟器", "模拟器"),
    ("mumuplayer", "MuMu 模拟器", "模拟器"),
    ("nemuplayer", "MuMu 模拟器", "模拟器"),
    ("mumuvmmheadless", "MuMu 模拟器", "模拟器"),
    ("nox", "夜神模拟器", "模拟器"),
    ("noxplayer", "夜神模拟器", "模拟器"),
    ("memu", "MEmu 模拟器", "模拟器"),
    ("memuconsole", "MEmu 模拟器", "模拟器"),
]


def normalize_proc(name) -> str:
    """进程名归一化：去路径、去 .exe、转小写"""
    s = (name or "").strip().lower().replace("\\", "/")
    s = s.rsplit("/", 1)[-1]
    if s.endswith(".exe"):
        s = s[:-4]
    return s.strip()


def is_system_account(user) -> bool:
    """判断进程是否以内置系统账户运行 —— 这类进程一律不自动限流"""
    u = (user or "").strip().lower()
    if not u:
        return False
    tail = u.rsplit("\\", 1)[-1].rsplit("/", 1)[-1]
    return tail in {
        "system", "local service", "localservice", "network service",
        "networkservice", "local system", "trustedinstaller",
    } or "nt authority" in u


def _default_data_dir() -> str:
    """挑一个可写目录存放配置（安装包目录通常只读）"""
    env = os.environ.get("WINRM_DATA_DIR", "").strip()
    if env:
        try:
            os.makedirs(env, exist_ok=True)
            return env
        except OSError:
            pass
    for cand in (os.path.join(os.path.expanduser("~"), ".winrm-manager"),
                 "/var/lib/winrm-manager"):
        try:
            os.makedirs(cand, exist_ok=True)
            if os.access(cand, os.W_OK):
                return cand
        except OSError:
            continue
    return tempfile.gettempdir()


DATA_DIR = _default_data_dir()
AUTO_FILE = os.path.join(DATA_DIR, "auto.json")

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
        # 最近一次进程快照，供自动限流线程复用（避免重复往返打乱采样窗口）
        self._last_snapshot = None

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
        # 采样间隔过长（例如界面闲置了几分钟）时，差值摊到整段时间上会
        # 把瞬时占用稀释成接近 0，反而误判"很闲"。这种情况丢弃基线重新开始。
        usable = 0.25 < dt <= 15.0
        new_cache = {}
        for p in rows:
            key = (p.get("pid"), p.get("start"))
            cpu = p.get("cpu")
            pct = None
            if cpu is not None:
                prev = self._prev_cpu.get(key)
                if prev is not None and usable:
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
        result = {"ts": now, "processes": rows, "stats": stats}
        self._last_snapshot = result
        return result

    def latest(self, max_age: float = 8.0) -> dict:
        """
        取一份"带 CPU 百分比"的进程快照。

        自动限流线程和前端轮询共用同一份数据：既省掉一次 WinRM 往返
        （单次 0.5–1.5 秒），也避免两个调用方互相截断 CPU 采样窗口
        导致百分比乱跳。

        CPU% 必须靠两次采样差值算。若当前快照里全是 None（服务刚启动、
        刚连上、或闲置太久丢弃了基线），补采一次再返回，否则自动限流
        在第一轮会"看不见"任何占用。
        """
        snap = self._last_snapshot
        if snap and (time.time() - float(snap.get("ts") or 0)) <= max_age \
                and self._has_cpu(snap):
            return snap

        first = self.processes()
        if self._has_cpu(first):
            return first
        time.sleep(1.2)
        return self.processes()

    @staticmethod
    def _has_cpu(snap: dict) -> bool:
        return any(p.get("cpuPct") is not None
                   for p in (snap.get("processes") or []))

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


# ---------------------------------------------------------------- 自动限流

class AutoConfig:
    """自动限流配置：开关 / 阈值 / 游戏名单，持久化到 auto.json"""

    def __init__(self, path: str):
        self.path = path
        self.lock = threading.RLock()
        self.enabled = False
        self.threshold = 15.0
        self.interval = 10.0
        self.games: dict = {}
        self._load()

    @staticmethod
    def _default_games() -> dict:
        return {p: {"proc": p, "label": lab, "cat": cat, "custom": False}
                for p, lab, cat in DEFAULT_GAMES}

    def _load(self):
        data = None
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            data = None

        # 首次运行（或文件损坏）→ 用内置默认名单播种
        if not isinstance(data, dict) or not isinstance(data.get("games"), list):
            with self.lock:
                self.games = self._default_games()
            self.save()
            return

        with self.lock:
            self.enabled = bool(data.get("enabled"))
            try:
                self.threshold = float(data.get("threshold") or 15)
            except (TypeError, ValueError):
                self.threshold = 15.0
            try:
                self.interval = float(data.get("intervalSec") or 10)
            except (TypeError, ValueError):
                self.interval = 10.0

            self.games = {}
            for g in data["games"]:
                if isinstance(g, str):
                    proc = normalize_proc(g)
                    if not proc:
                        continue
                    self.games[proc] = {"proc": proc, "label": proc,
                                        "cat": "自定义", "custom": True}
                    continue
                if not isinstance(g, dict):
                    continue
                proc = normalize_proc(g.get("proc"))
                if not proc:
                    continue
                self.games[proc] = {
                    "proc": proc,
                    "label": (g.get("label") or proc).strip() or proc,
                    "cat": (g.get("cat") or "自定义").strip() or "自定义",
                    "custom": bool(g.get("custom")),
                }
        self._clamp()

    def _clamp(self):
        with self.lock:
            self.threshold = max(1.0, min(100.0, float(self.threshold)))
            self.interval = max(5.0, min(120.0, float(self.interval)))

    def save(self):
        with self.lock:
            payload = {
                "enabled": self.enabled,
                "threshold": self.threshold,
                "intervalSec": self.interval,
                "games": list(self.games.values()),
            }
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, indent=1)
            os.replace(tmp, self.path)
        except OSError as exc:
            try:
                STATE.log("error", "保存自动限流配置失败", str(exc))
            except Exception:
                pass

    # -- 名单操作

    def games_map(self) -> dict:
        with self.lock:
            return dict(self.games)

    def add_game(self, name: str, label: str = "", cat: str = "自定义") -> dict:
        proc = normalize_proc(name)
        if not proc:
            raise HttpError(400, "进程名不能为空")
        if len(proc) > 80:
            raise HttpError(400, "进程名过长")
        if proc in NEVER_LIMIT:
            raise HttpError(400, f"{proc} 是反作弊/安全组件，不能加入限流名单")
        entry = {"proc": proc, "label": (label or proc).strip() or proc,
                 "cat": (cat or "自定义").strip() or "自定义", "custom": True}
        with self.lock:
            existed = proc in self.games
            self.games[proc] = entry
        if not existed:
            self.save()
        return {"proc": proc, "added": not existed, **entry}

    def remove_game(self, name: str) -> bool:
        proc = normalize_proc(name)
        with self.lock:
            if proc not in self.games:
                return False
            del self.games[proc]
        self.save()
        return True

    def reset_games(self) -> int:
        with self.lock:
            self.games = self._default_games()
            n = len(self.games)
        self.save()
        return n

    def update(self, enabled=None, threshold=None, interval=None):
        with self.lock:
            if enabled is not None:
                self.enabled = bool(enabled)
            if threshold is not None:
                try:
                    self.threshold = float(threshold)
                except (TypeError, ValueError):
                    raise HttpError(400, "阈值必须是数字")
            if interval is not None:
                try:
                    self.interval = float(interval)
                except (TypeError, ValueError):
                    raise HttpError(400, "间隔必须是数字")
        self._clamp()
        self.save()


class AutoLimiter:
    """
    后台巡检：游戏进程 CPU 或内存超过阈值 → 限制到 1 核 + 最低优先级。

    只动两样可逆的东西（处理器亲和性、PriorityClass），不碰内存上限，
    因为 Job Object 一旦设下就无法远程撤销。
    """

    MAX_PER_TICK = 3          # 每轮最多处理几个，避免一次改太多
    RESTORE_COOLDOWN = 600    # 手动恢复后 10 分钟内不再自动限制

    def __init__(self, cfg: AutoConfig):
        self.cfg = cfg
        self._stop = threading.Event()
        self._thread = None
        self.limited: dict = {}     # pid -> 记录
        self.skip_until: dict = {}  # pid -> 解除冷却到期时间
        self.last_tick = 0.0
        self.last_error = ""
        self.applied_total = 0

    # -- 线程

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="auto-limiter",
                                        daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _loop(self):
        while not self._stop.is_set():
            interval = max(float(self.cfg.interval or 10), 5.0)
            if self._stop.wait(interval):
                return
            if not self.cfg.enabled:
                continue
            try:
                self.tick()
            except Exception as exc:      # 单轮失败不能让线程死掉
                self.last_error = f"{type(exc).__name__}: {exc}"

    # -- 单轮巡检

    def tick(self) -> dict:
        if not self.cfg.enabled:
            return {"ok": False, "error": "自动限制未开启"}
        remote = STATE.remote
        if remote is None:
            return {"ok": False, "error": "尚未连接远程主机"}

        with self.cfg.lock:
            threshold = float(self.cfg.threshold)
            interval = float(self.cfg.interval)
            games = dict(self.cfg.games)

        total_mem = int(STATE.system.get("totalMem") or 0)
        snap = remote.latest(max_age=max(6.0, interval * 0.9))
        procs = snap.get("processes") or []

        # 刚启动的游戏进程在第一次采样里没有可比对的基线（cpuPct=None）。
        # 这时不能直接放过 —— 补采一次，否则"刚打开的游戏吃满 CPU"这一
        # 最该被限制的场景反而检测不到。
        if any(normalize_proc(p.get("name")) in games and p.get("cpuPct") is None
               for p in procs):
            time.sleep(1.2)
            procs = remote.processes().get("processes") or []

        now = time.time()
        alive = {int(p.get("pid") or 0) for p in procs}

        # 清掉已退出的进程记录
        for pid in [k for k in self.limited if k not in alive]:
            self.limited.pop(pid, None)
        for pid in [k for k, ts in self.skip_until.items() if ts <= now]:
            self.skip_until.pop(pid, None)

        candidates = []
        for p in procs:
            proc = normalize_proc(p.get("name"))
            if not proc or proc not in games:
                continue
            if proc in NEVER_LIMIT or p.get("protected"):
                continue
            if is_system_account(p.get("user")):
                continue
            pid = int(p.get("pid") or 0)
            if pid <= 0 or pid in self.limited:
                continue
            if self.skip_until.get(pid, 0) > now:
                continue

            cpu = p.get("cpuPct")
            cpu = float(cpu) if cpu is not None else None
            mem_pct = (float(p.get("ws") or 0) / total_mem * 100.0) if total_mem else 0.0
            cpu_hit = cpu is not None and cpu >= threshold
            mem_hit = mem_pct >= threshold
            if not (cpu_hit or mem_hit):
                continue

            candidates.append({
                "pid": pid,
                "name": p.get("name") or proc,
                "label": games[proc].get("label") or proc,
                "cpu": cpu,
                "memPct": round(mem_pct, 1),
                "ws": int(p.get("ws") or 0),
                "reason": ("CPU %.1f%%" % cpu) if cpu_hit else ("内存 %.1f%%" % mem_pct),
            })

        candidates.sort(key=lambda c: -max(c["cpu"] or 0.0, c["memPct"]))

        applied, failed = [], []
        for c in candidates[: self.MAX_PER_TICK]:
            try:
                r = remote.set_cpu_limit(c["pid"], 1, "Idle")
            except Exception as exc:
                failed.append({**c, "error": str(exc)})
                continue
            if not r.get("ok"):
                failed.append({**c, "error": "亲和性=%s 优先级=%s" % (
                    r.get("affinityOk"), r.get("priorityOk"))})
                continue

            rec = {
                "pid": c["pid"], "name": c["name"], "label": c["label"],
                "cpu": c["cpu"], "memPct": c["memPct"], "ws": c["ws"],
                "reason": c["reason"], "ts": now,
                "affinity": r.get("affinity"), "priority": r.get("priority"),
            }
            self.limited[c["pid"]] = rec
            self.applied_total += 1
            applied.append(rec)
            STATE.log("warn", "自动限制游戏进程",
                      f"{c['name']} (PID {c['pid']}) · {c['reason']} → 1 核 / Idle")

        self.last_tick = now
        self.last_error = "" if not failed else failed[0].get("error", "")
        return {"ok": True, "checked": len(procs), "candidates": len(candidates),
                "applied": applied, "failed": failed, "ts": now}

    # -- 手动恢复

    def restore(self, pids) -> list:
        remote = STATE.remote
        if remote is None:
            raise HttpError(409, "尚未连接远程主机")
        cores = max(int(STATE.system.get("cores") or 1), 1)
        mask = (1 << cores) - 1
        results = []
        for pid in pids:
            try:
                pid = int(pid)
            except (TypeError, ValueError):
                continue
            try:
                r = remote.set_cpu_limit(pid, mask, "Normal")
                ok = bool(r.get("ok"))
                err = "" if ok else "亲和性=%s 优先级=%s" % (
                    r.get("affinityOk"), r.get("priorityOk"))
            except Exception as exc:
                ok, err = False, str(exc)
            if ok:
                rec = self.limited.pop(pid, {})
                self.skip_until[pid] = time.time() + self.RESTORE_COOLDOWN
                STATE.log("info", "恢复进程限制",
                          f"{rec.get('name') or pid} (PID {pid}) → {cores} 核 / Normal")
            results.append({"pid": pid, "ok": ok, "error": err})
        return results

    def status(self) -> dict:
        with self.cfg.lock:
            games = list(self.cfg.games.values())
            enabled = self.cfg.enabled
            threshold = self.cfg.threshold
            interval = self.cfg.interval
        games.sort(key=lambda g: (g["cat"], g["label"]))
        limited = sorted(self.limited.values(), key=lambda r: -r["ts"])
        return {
            "enabled": enabled,
            "threshold": threshold,
            "intervalSec": interval,
            "games": games,
            "gameCount": len(games),
            "defaultCount": len(DEFAULT_GAMES),
            "limited": limited,
            "limitedCount": len(limited),
            "lastTick": self.last_tick,
            "lastError": self.last_error,
            "appliedTotal": self.applied_total,
            "dataFile": self.cfg.path,
        }


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

AUTO = AutoConfig(AUTO_FILE)
LIMITER = AutoLimiter(AUTO)


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

    # 标注哪些进程在游戏名单里，前端据此显示角标
    games = AUTO.games_map()
    limited_pids = set(LIMITER.limited)
    for p in data.get("processes") or []:
        meta = games.get(normalize_proc(p.get("name")))
        p["isGame"] = bool(meta)
        p["gameLabel"] = meta["label"] if meta else None
        p["autoLimited"] = p.get("pid") in limited_pids

    data["auto"] = {"enabled": AUTO.enabled,
                    "threshold": AUTO.threshold,
                    "limited": len(LIMITER.limited)}
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


# -- 自动限流（游戏进程）

@route("GET", r"/api/auto")
def api_auto_get(body):
    return {"ok": True, **LIMITER.status()}


@route("POST", r"/api/auto/settings")
def api_auto_settings(body):
    before = AUTO.enabled
    AUTO.update(enabled=body.get("enabled"),
                threshold=body.get("threshold"),
                interval=body.get("intervalSec"))
    if body.get("enabled") is not None and bool(body.get("enabled")) != before:
        STATE.log("info", "自动限流已开启" if AUTO.enabled else "自动限流已关闭")
    return {"ok": True, **LIMITER.status()}


@route("POST", r"/api/auto/games")
def api_auto_games(body):
    action = (body.get("action") or "").strip().lower()
    names = body.get("names")
    if isinstance(names, str):
        names = [names]
    if names is None:
        names = []

    if action == "add":
        added, skipped = [], []
        for nm in names:
            try:
                r = AUTO.add_game(nm, body.get("label") or "")
            except HttpError as exc:
                skipped.append({"name": nm, "error": exc.message})
                continue
            added.append(r)
        if added:
            STATE.log("info", "添加游戏进程名",
                      "、".join(a["proc"] for a in added))
        return {"ok": True, "added": added, "skipped": skipped,
                **LIMITER.status()}

    if action == "remove":
        removed = [normalize_proc(n) for n in names if AUTO.remove_game(n)]
        if removed:
            STATE.log("info", "移除游戏进程名", "、".join(removed))
        return {"ok": True, "removed": removed, **LIMITER.status()}

    if action == "reset":
        n = AUTO.reset_games()
        STATE.log("info", "恢复默认游戏名单", f"{n} 项")
        return {"ok": True, "count": n, **LIMITER.status()}

    raise HttpError(400, "未知操作，应为 add / remove / reset")


@route("POST", r"/api/auto/scan")
def api_auto_scan(body):
    """立即巡检一轮（不等待后台定时器）"""
    result = LIMITER.tick()
    if not result.get("ok"):
        raise HttpError(409, result.get("error") or "巡检失败")
    return {"ok": True, "scan": result, **LIMITER.status()}


@route("POST", r"/api/auto/restore")
def api_auto_restore(body):
    pids = body.get("pids") or []
    if not pids:
        raise HttpError(400, "未指定进程")
    results = LIMITER.restore(pids)
    return {"ok": True, "results": results, **LIMITER.status()}


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

    # 自动限流巡检线程（常驻，配置开启时才真正干活）
    LIMITER.start()

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
