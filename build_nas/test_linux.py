"""在 Linux 容器内验证 fpk 应用的完整业务链路

用法：
    export WINRM_TEST_HOST=192.168.1.100
    export WINRM_TEST_USER=administrator
    export WINRM_TEST_PASSWORD='your-password'
    python3 test_linux.py
"""
import json
import os
import time
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:5178"
WEB_PASSWORD = os.environ.get("WINRM_TEST_WEB_PASSWORD", "TestPwd123")

TARGET = {
    "host": os.environ.get("WINRM_TEST_HOST", ""),
    "username": os.environ.get("WINRM_TEST_USER", ""),
    "password": os.environ.get("WINRM_TEST_PASSWORD", ""),
    "transport": os.environ.get("WINRM_TEST_TRANSPORT", "ntlm"),
}


def call(path, method="GET", body=None, token=None):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Auth-Token"] = token
    req = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


def main():
    if not TARGET["host"] or not TARGET["password"]:
        print("未设置测试目标。请先导出环境变量：")
        print("  WINRM_TEST_HOST / WINRM_TEST_USER / WINRM_TEST_PASSWORD")
        return

    st, d = call("/api/login", "POST", {"password": WEB_PASSWORD})
    if st != 200:
        print("登录失败:", d)
        return
    token = d["token"]
    print("登录: %d, token 长度 %d" % (st, len(token)))

    st, d = call("/api/connect", "POST", TARGET, token)
    if st != 200:
        print("连接失败:", d)
        return
    sysinfo = d.get("system", {})
    print("连接: %d -> %s | %s | %s ms" % (
        st, sysinfo.get("host"), sysinfo.get("os"), d.get("latency")))

    st, p1 = call("/api/processes", token=token)
    print("进程采集: %d, %d 个, %s ms" % (
        st, len(p1.get("processes", [])), p1.get("elapsedMs")))

    time.sleep(1.5)
    st, p2 = call("/api/processes", token=token)
    withcpu = [x for x in p2["processes"] if x["cpuPct"] is not None]
    top = sorted(withcpu, key=lambda x: -x["cpuPct"])[:5]
    print("CPU 占用 Top5 (共 %d 个进程有 CPU 数据):" % len(withcpu))
    for x in top:
        print("   %-24s pid=%-6s %5.1f%%  %5dMB  %s" % (
            x["name"][:22], x["pid"], x["cpuPct"],
            x["ws"] // 1048576, x["user"] or "-"))

    st, s = call("/api/start", "POST",
                 {"exe": "cmd.exe", "args": "/c ping -n 300 127.0.0.1 > nul"}, token)
    pid = s["process"]["pid"]
    print("启动进程: %d, PID %s" % (st, pid))

    time.sleep(3)
    st, dt = call("/api/process/%d" % pid, token=token)
    if st == 200:
        print("详情: 存活 ✓  %s | owner=%s | cmd=%s" % (
            dt["detail"]["name"], dt["detail"]["owner"],
            (dt["detail"]["commandLine"] or "")[:50]))
    else:
        print("详情: 进程已消失 ✗")

    st, k = call("/api/kill", "POST", {"pids": [pid], "force": True}, token)
    print("结束进程: %d, killed=%s" % (st, k.get("killed")))

    st, p3 = call("/api/processes", token=token)
    lsass = next((x["pid"] for x in p3["processes"] if x["name"] == "lsass"), None)
    if lsass:
        st, b = call("/api/kill", "POST", {"pids": [lsass], "force": True}, token)
        print("保护拦截: %d -> %s" % (st, b.get("error")))


if __name__ == "__main__":
    main()
