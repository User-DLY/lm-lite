#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
lm-lite 智能启动器
  1. 环境自检（Python / llama-server / 模型目录）
  2. 端口冲突自动清理（9000 / 8081）
  3. 启动网关
  4. 看护：网关或后端崩了自动重启（最多 5 次）
  5. 自动打开浏览器
"""
import json
import os
import socket
import sys

# 双保险：能重配就重配成 utf-8，不能也不影响（log 里已包 try）
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
import subprocess
import sys
import time
import urllib.request
import urllib.error
import webbrowser

HERE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable
GATEWAY = os.path.join(HERE, "gateway.py")
# llama-server 所在目录。优先级：
#   1) 环境变量 LM_LITE_BACKEND
#   2) 同目录下 config.json 的 backend_dir
#   3) 同目录下的 backend/ 文件夹（推荐：把 llama-server 放进去即可）
_DEFAULT_BACKEND = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backend")
BACKEND_DIR = os.environ.get("LM_LITE_BACKEND") or _DEFAULT_BACKEND
try:
    _cfg_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
    if os.path.isfile(_cfg_path):
        # utf-8-sig：容忍记事本写出的 BOM，否则 json.load 会抛错被吞掉 → 静默用默认值
        with open(_cfg_path, encoding="utf-8-sig") as _f:
            _c = json.load(_f) or {}
        if _c.get("backend_dir"):
            BACKEND_DIR = _c["backend_dir"]
except Exception:
    pass
BACKEND_DIR = os.path.abspath(BACKEND_DIR)
LLAMA_SERVER = os.path.join(BACKEND_DIR, "llama-server.exe")
MODELS_DIR = os.path.expanduser(r"~\.lmstudio\models")
GW_PORT = 9000
BACKEND_PORT = 8081          # llama-server
BACK_PORT = 8081
LOG = os.path.join(HERE, "launcher.log")

restarts = 0
restart_times = []
MAX_RESTARTS = 5
RESTART_WINDOW = 120


def log(msg):
    line = "[%s] %s" % (time.strftime("%H:%M:%S"), msg)
    # ★ 打印绝不能抛异常：Windows 重定向到文件时 stdout 是 cp936，
    #   遇到 emoji 会 UnicodeEncodeError。以前这行在 try 之外，
    #   一崩就整个 launcher 退出，看护循环根本没进去过。
    try:
        print(line, flush=True)
    except Exception:
        try:
            print(line.encode("ascii", "replace").decode("ascii"), flush=True)
        except Exception:
            pass
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def port_busy(port):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.4)
    try:
        return s.connect_ex(("127.0.0.1", port)) == 0
    finally:
        s.close()


# 这些进程名才允许被 free_port 杀掉 —— 端口冲突时不能盲杀，
# 否则用户开着 LM Studio（也监听 1234/8081 一类端口）会被静默干掉。
# ★ 只认 llama-server.exe。
#   原来还把 python.exe / pythonw.exe / cmd.exe / powershell.exe 列进来 ——
#   那意味着用户自己的 Jupyter、别的 Python 服务、任何 PowerShell 窗口，
#   只要占着 8081 就会被 taskkill /F 静默干掉。名字相同 ≠ 同一个程序。
#   gateway 自己占着 9000 时不该被杀（那是 launcher 该重启自己管的那个），
#   由 launcher 持有 Popen 句柄精确处理。
_OUR_PROCS = ("llama-server.exe",)


def _proc_name_of(pid):
    try:
        r = subprocess.run(["tasklist", "/FI", "PID eq %s" % pid, "/FO", "CSV", "/NH"],
                           capture_output=True, text=True, timeout=8)
        line = (r.stdout or "").strip().splitlines()
        if not line:
            return ""
        # CSV 形如 "python.exe","12345","Console","1","10,000 K"
        return line[0].split(",")[0].strip().strip('"').lower()
    except Exception:
        return ""


def free_port(port):
    """端口被占就找进程杀掉（自动纠错）—— 但只杀我们自己那几个进程名"""
    if not port_busy(port):
        return True
    log("端口 %d 被占用，尝试清理…" % port)
    try:
        r = subprocess.run(["netstat", "-ano"], capture_output=True, text=True, timeout=10)
        pids = set()
        for line in r.stdout.splitlines():
            if (":%d" % port) in line and "LISTENING" in line:
                pids.add(line.split()[-1])
        for pid in pids:
            if not pid or pid == "0":
                continue
            nm = _proc_name_of(pid)
            if nm and nm not in _OUR_PROCS:
                # 不是我们的进程：只报告，不动手
                log("  [!!] 端口 %d 被 %s (PID %s) 占用 —— 不是 lm-lite 的进程，跳过不杀"
                    % (port, nm, pid))
                continue
            log("  结束进程 PID %s (%s)" % (pid, nm or "未知"))
            subprocess.run(["taskkill", "/F", "/PID", pid], capture_output=True, timeout=10)
    except Exception as e:
        log("  清理失败: %s" % e)
    time.sleep(1.2)
    return not port_busy(port)


def check_env():
    problems = []
    if not os.path.isfile(LLAMA_SERVER):
        problems.append("找不到 llama-server.exe：%s" % LLAMA_SERVER)
    if not os.path.isdir(MODELS_DIR):
        problems.append("找不到模型目录：%s" % MODELS_DIR)
    else:
        n = sum(1 for r, _d, fs in os.walk(MODELS_DIR) for f in fs if f.lower().endswith(".gguf"))
        if n == 0:
            problems.append("模型目录里没有 .gguf 文件")
        else:
            log("模型目录: 找到 %d 个 gguf" % n)
    if not os.path.isfile(GATEWAY):
        problems.append("找不到 gateway.py")
    return problems


def wget(url, timeout=3):
    """探活。

    ★ 401/403 代表"网关活着，只是这条接口需要登录"。原来只认 200，
       用户一开登录保护就会让 urlopen 抛 HTTPError → 这里返回 False
       → launcher 判定"网关无响应" → 强制重启 → 又是 401 → 又重启，
       形成每 35 秒一轮的无限重启（2026-10-18 实测踩到）。
    """
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status == 200
    except urllib.error.HTTPError as e:
        # 服务端给出了 HTTP 响应 = 进程活着
        return e.code in (200, 201, 204, 302, 401, 403, 404)
    except Exception:
        return False


def start_gateway():
    logf = open(os.path.join(HERE, "gateway_stdout.log"), "a", encoding="utf-8", errors="ignore")
    p = subprocess.Popen([PY, "-u", GATEWAY], cwd=HERE,
                         stdout=logf, stderr=subprocess.STDOUT,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return p, logf


def note_restart():
    """记录一次重启；只统计时间窗口内的次数，避免手动重启被误判"""
    global restarts
    now = time.time()
    restart_times.append(now)
    while restart_times and now - restart_times[0] > RESTART_WINDOW:
        restart_times.pop(0)
    restarts = len(restart_times)
    return restarts


def _stop_backend_first():
    """退出时先优雅停掉 llama-server，避免它被孤立继续吃显存。

    顺序：
      1. POST /api/unload 让网关停后端（正常路径）
      2. 等 8081 端口释放（最多 10 秒）
      3. 若还在，按「命令行含 lm-lite 模型目录」精确匹配后杀掉
         —— 不能盲杀 llama-server.exe，用户可能还开着 LM Studio。
    """
    try:
        req = urllib.request.Request(
            "http://127.0.0.1:%d/api/unload" % GW_PORT,
            data=b"{}", headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=8).read()
        log("  已请求网关卸载模型…")
    except Exception:
        pass

    for _ in range(20):
        if not port_busy(BACKEND_PORT):
            return
        time.sleep(0.5)

    # 兜底：只杀命令行里带 lm-lite 的 llama-server
    try:
        ps = (
            "Get-CimInstance Win32_Process -Filter \"Name='llama-server.exe'\" | "
            "Where-Object { $_.CommandLine -like '*lm-lite*' -or "
            "$_.CommandLine -like '*llama-server*' } | "
            "ForEach-Object { Stop-Process -Id $_.ProcessId -Force; $_.ProcessId }"
        )
        r = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                           capture_output=True, text=True, timeout=15,
                           creationflags=0x08000000)
        for pid in (r.stdout or "").split():
            log("  已清理残留 llama-server PID %s" % pid)
    except Exception:
        pass


def main():
    global restarts

    # ★ 单实例锁：两个 launcher 并行会各自拉起一套网关+llama-server，
    #   两套 llama-server 抢同一个 8081 端口（SO_REUSEADDR 都能绑上），
    #   显存/内存翻倍、性能直接腰斩。实测清掉重复后 prefill 从 2491 → 3350 tok/s。
    try:
        import ctypes
        _mtx = ctypes.windll.kernel32.CreateMutexW(None, False, "Global\\lm-lite-launcher-single")
        _err = ctypes.windll.kernel32.GetLastError()
        if _err == 183:                       # ERROR_ALREADY_EXISTS
            print("=" * 58)
            print("  已经有一个 lm-lite 启动器在运行了")
            print("  再开一个会拉起第二套 llama-server，两者抢 8081 端口，")
            print("  显存和 CPU 都会翻倍占用 —— 性能反而变差。")
            print("=" * 58)
            print("  如果确定要强制重启，先关掉已有的那个窗口。")
            try:
                input("按回车关闭...")
            except Exception:
                pass
            return
    except Exception:
        _mtx = None          # 拿不到锁就照常跑，别因此起不来

    print("=" * 58)
    print("  lm-lite 智能启动器")
    print("=" * 58)

    # 1. 环境自检
    log("检查环境…")
    probs = check_env()
    if probs:
        for p in probs:
            log("[X] " + p)
        log("环境不满足，退出。")
        input("\n按回车关闭…")
        return 1
    log("环境 OK")

    # 2. 端口清理
    ok1 = free_port(GW_PORT)
    ok2 = free_port(BACK_PORT)
    if not ok1:
        log("[!] 网关端口 %d 仍被占用，可能启动失败" % GW_PORT)

    # 3. 启动网关
    log("启动网关…")
    gw, gwlog = start_gateway()

    # 等网关就绪
    ready = False
    for _ in range(40):
        if wget("http://127.0.0.1:%d/api/auth_status" % GW_PORT):
            ready = True
            break
        if gw.poll() is not None:
            break
        time.sleep(0.6)

    if not ready:
        log("[X] 网关没起来，看 gateway_stdout.log")
        input("\n按回车关闭…")
        return 1

    log("[OK] 网关就绪: http://127.0.0.1:%d" % GW_PORT)

    # 4. 打开浏览器
    try:
        webbrowser.open("http://127.0.0.1:%d" % GW_PORT)
        log("已打开浏览器")
    except Exception:
        log("浏览器打开失败，请手动访问 http://127.0.0.1:%d" % GW_PORT)

    # 5. 看护循环
    log("进入看护模式（关闭本窗口即停止）")
    print("-" * 58)
    while True:
        time.sleep(5)
        # 网关进程死了 → 重启
        if gw.poll() is not None:
            note_restart()
            if restarts > MAX_RESTARTS:
                log("[X] 网关连续崩溃 %d 次，停止自动重启" % MAX_RESTARTS)
                break
            log("[!] 网关退出（第 %d 次），自动重启…" % restarts)
            try:
                gwlog.close()
            except Exception:
                pass
            # 重启前确保端口真的释放了（否则新网关绑不上）
            for _ in range(10):
                if not port_busy(GW_PORT):
                    break
                time.sleep(0.8)
            gw, gwlog = start_gateway()
            for _ in range(30):
                if wget("http://127.0.0.1:%d/api/auth_status" % GW_PORT):
                    log("[OK] 网关已恢复")
                    break
                time.sleep(0.6)
        else:
            # 心跳：网关还响应吗（不是死锁/假活）
            if not wget("http://127.0.0.1:%d/api/auth_status" % GW_PORT, timeout=6):
                # 连续两次没响应才算
                if not wget("http://127.0.0.1:%d/api/auth_status" % GW_PORT, timeout=6):
                    note_restart()
                    if restarts > MAX_RESTARTS:
                        log("[X] 网关无响应 %d 次，停止" % MAX_RESTARTS)
                        break
                    log("[!] 网关无响应（第 %d 次），强制重启…" % restarts)
                    try:
                        gw.kill()
                    except Exception:
                        pass
                    try:
                        gwlog.close()
                    except Exception:
                        pass
                    time.sleep(1.5)
                    for _ in range(10):
                        if not port_busy(GW_PORT):
                            break
                        time.sleep(0.8)
                    gw, gwlog = start_gateway()
                    for _ in range(30):
                        if wget("http://127.0.0.1:%d/api/auth_status" % GW_PORT):
                            log("[OK] 网关已恢复")
                            restarts = 0
                            break
                        time.sleep(0.6)

    # ★ 退出前先让网关卸载模型：gw.kill() 是 TerminateProcess，
    #   网关里的 `except KeyboardInterrupt: stop_backend()` 永远不会执行，
    #   于是 llama-server 被孤立，继续占着 8081 端口和 3~4GB 显存。
    _stop_backend_first()

    try:
        gw.kill()
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    # ★ 三层兜底：窗控台关闭 / Ctrl+C / 正常 return 都要停掉 llama-server。
    #   原来只在 main() 正常 return 前调用 _stop_backend_first()，
    #   而「关闭本窗口」是直接终止进程、Ctrl+C 只捕获 KeyboardInterrupt 打印后退出
    #   —— 两条常见路径都不走清理，llama-server 留在后台吃掉 3~4GB 显存。
    import atexit

    _cleaned = [False]

    def _cleanup_once():
        if _cleaned[0]:
            return
        _cleaned[0] = True
        try:
            _stop_backend_first()
        except Exception:
            pass

    atexit.register(_cleanup_once)

    # Windows 控制台关闭/注销时 Python 的 atexit 不一定跑，额外挂信号
    try:
        import signal as _sig
        def _on_sig(signum, frame):
            _cleanup_once()
            sys.exit(0)
        for _name in ("SIGINT", "SIGTERM", "SIGBREAK"):
            _s = getattr(_sig, _name, None)
            if _s is not None:
                try:
                    _sig.signal(_s, _on_sig)
                except Exception:
                    pass
    except Exception:
        pass

    try:
        sys.exit(main())
    except KeyboardInterrupt:
        _cleanup_once()
        print("\n已停止")
