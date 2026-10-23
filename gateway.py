#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
lm-lite 网关：管理 llama-server 启停 + 转发 API

  GET  /api/models          列出可用模型
  POST /api/load            切换模型 {model, ctx, kv}  -> 重启 llama-server
  GET  /api/status          当前后端状态
  POST /v1/chat/completions 转发给 llama-server（支持流式 SSE）
                              未指定的采样参数按 api_cfg.json 的「API 默认值」注入
  GET  /api/api-cfg         读取 API 调用默认值
  POST /api/api-cfg         保存 API 调用默认值（持久化到 api_cfg.json）
  GET  /                    返回前端页面
"""
import hmac
import json
import os
import re
import struct
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
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
# 模型目录：默认 LM Studio 的目录，可在 config.json 里加 model_dirs 数组扩展
MODELS_DIR = os.path.expanduser(r"~\.lmstudio\models")
MODEL_DIRS = [MODELS_DIR]


def _load_model_dirs():
    """从 config.json 读 model_dirs，合并进 MODEL_DIRS（去重、只留有在的）"""
    global MODEL_DIRS
    dirs = [MODELS_DIR]
    try:
        cp = os.path.join(HERE, "config.json")
        if os.path.isfile(cp):
            import json as _j
            with open(cp, encoding="utf-8-sig") as fp:
                c = _j.load(fp) or {}
            for d in (c.get("model_dirs") or []):
                if isinstance(d, str) and d.strip():
                    dirs.append(os.path.expanduser(d.strip()))
    except Exception:
        pass
    seen, out = set(), []
    for d in dirs:
        k = os.path.normcase(os.path.abspath(d))
        if k in seen or not os.path.isdir(d):
            continue
        seen.add(k); out.append(d)
    MODEL_DIRS = out
LLAMA_PORT = 8081
GATEWAY_PORT = 9000

# 当前转发中的上游响应（用于 /api/stop 主动切断）
# ★ 活动请求表：{rid: {"resp": 响应对象, "is_web": 是否页面请求}}
#   原来是单槽 ACTIVE{"resp":...} —— 并发（-np 2~4）时后一个请求会覆盖前一个的句柄，
#   导致 /api/stop 杀错人、finally 无条件清空别人的状态、ensure_model 等不到真正空闲。
ACTIVE = {"reqs": {}, "lock": threading.Lock(), "seq": 0}


def _active_register(resp, is_web):
    """登记一条正在流式的请求，返回 id"""
    with ACTIVE["lock"]:
        ACTIVE["seq"] += 1
        rid = ACTIVE["seq"]
        ACTIVE["reqs"][rid] = {"resp": resp, "is_web": bool(is_web)}
        return rid


def _active_unregister(rid):
    """只清自己那条 —— 绝不碰别人的"""
    with ACTIVE["lock"]:
        ACTIVE["reqs"].pop(rid, None)


def _active_snapshot():
    """快照 [(rid, info)]，遍历时不用持锁"""
    with ACTIVE["lock"]:
        return list(ACTIVE["reqs"].items())


def _active_busy():
    """有没有任何请求正在进行"""
    with ACTIVE["lock"]:
        return bool(ACTIVE["reqs"])
BACKEND_LOCK = threading.Lock()   # 串行化 start/stop，防互杀

STATE = {
    "model": None,
    "ctx": 8192,
    "kv": "q8_0",
    "proc": None,
    "loading": False,
    "last_error": "",
    "parallel": 1,          # llama-server 的 -np：同时处理几个请求
    "mismatch": "",         # 最近一次「请求的模型与实际加载的不一致」的记录
}

# ═══ 登录保护（可选）═══
# 密码用 PBKDF2-HMAC-SHA256 + 随机 salt 存，不存明文
AUTH_FILE = os.path.join(HERE, "auth.json")
# ★ 双角色：users = {用户名: {"salt","hash","iter","role"}}，role ∈ admin|user
#   admin 可以改模型/参数/用户；user 只能聊天（聊天记录也在前端按用户名隔离）。
#   旧版 auth.json 是单用户结构（顶层 user/salt/hash），_load_auth 会自动迁移成 users.admin。
AUTH = {"enabled": False, "users": {}, "iter": 120000}


def _load_auth():
    global AUTH
    try:
        with open(AUTH_FILE, encoding="utf-8-sig") as f:
            d = json.load(f) or {}
    except Exception:
        return
    us = d.get("users")
    if isinstance(us, dict) and us:
        AUTH["enabled"] = bool(d.get("enabled"))
        AUTH["users"] = {str(k): v for k, v in us.items() if isinstance(v, dict)}
        if "iter" in d:
            AUTH["iter"] = int(d.get("iter") or 120000)
        return
    # —— 旧版单用户结构 → 迁移成 users["admin"] ——
    if d.get("hash") and d.get("salt"):
        nm = str(d.get("user") or "admin")
        AUTH["users"] = {nm: {"salt": d["salt"], "hash": d["hash"],
                              "iter": int(d.get("iter") or 120000),
                              "role": "admin"}}
        AUTH["enabled"] = bool(d.get("enabled"))
        AUTH["iter"] = int(d.get("iter") or 120000)
        try:
            _save_auth()        # 立刻落盘成新结构
        except Exception:
            pass


def _hash_pw(password, salt_hex, iterations=120000):
    import hashlib
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                               bytes.fromhex(salt_hex), iterations).hex()


def _save_auth():
    try:
        with open(AUTH_FILE, "w", encoding="utf-8") as f:
            json.dump(AUTH, f, ensure_ascii=False, indent=1)
        return True
    except Exception:
        return False


def auth_required():
    """需要鉴权吗？只有 enabled 且至少有一个设过密码的用户才需要"""
    us = AUTH.get("users") or {}
    return bool(AUTH.get("enabled") and any(u.get("hash") and u.get("salt")
                                           for u in us.values() if isinstance(u, dict)))


# ═══ 审计日志 + Windows 桌面通知 ═══
# admin 的关键操作（改模型/改配置/改用户/拉黑）要留痕，并弹系统通知，
# 这样即使远程有人动了这台机器，坐在屏幕前的人也能立刻看到。
AUDIT_FILE = os.path.join(HERE, "audit.log")
_AUDIT = []                 # 内存里保留最近 N 条，供前端轮询
_AUDIT_LOCK = threading.Lock()
AUDIT_MAX = 300


def audit(what, who=""):
    """记一条审计 + 异步弹 Windows 通知（不阻塞请求）"""
    try:
        import datetime as _dt
        ts = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        rec = {"ts": ts, "who": who or "未知", "what": what}
        with _AUDIT_LOCK:
            _AUDIT.append(rec)
            if len(_AUDIT) > AUDIT_MAX:
                del _AUDIT[:len(_AUDIT) - AUDIT_MAX]
        # 落盘（追加）
        try:
            with open(AUDIT_FILE, "a", encoding="utf-8") as fp:
                fp.write("%s  [%s]  %s\n" % (ts, rec["who"], what))
        except Exception:
            pass
        # ★ 本机操作不弹 Toast：人就在屏幕前，弹窗纯属噪音。
        #   只有来自非本机（局域网设备）的操作才需要桌面提醒。
        _is_local_op = (who or "") in ("本机", "local", "")
        if not _is_local_op:
            threading.Thread(target=_notify, args=(who or "未知", what),
                             daemon=True).start()
    except Exception:
        pass


def _notify(who, what):
    """弹 Windows 右下角通知（Toast）。失败静默 —— 通知不是核心功能。"""
    try:
        if os.name != "nt":
            return
        t = (what or "").replace("'", "'").replace('"', '"')[:120]
        w = (who or "").replace("'", "'").replace('"', '"')[:40]
        ps = (
            "$ErrorActionPreference='SilentlyContinue';"
            "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, "
            "ContentType=WindowsRuntime] | Out-Null;"
            "[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom, "
            "ContentType=WindowsRuntime] | Out-Null;"
            "$tpl=[Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent("
            "[Windows.UI.Notifications.ToastTemplateType]::ToastText02);"
            "$x=$tpl.GetElementsByTagName('text');"
            "$x.Item(0).AppendChild($tpl.CreateTextNode('lm-lite · 来自 %s 的操作')) | Out-Null;"
            "$x.Item(1).AppendChild($tpl.CreateTextNode('%s')) | Out-Null;"
            "$n=[Windows.UI.Notifications.ToastNotification]::new($tpl);"
            "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("
            "'lm-lite').Show($n);"
        ) % (w, t)
        subprocess.run(["powershell", "-NoProfile", "-WindowStyle", "Hidden",
                        "-Command", ps],
                       timeout=8, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


# 已登录的会话令牌（内存态，重启失效）
# ★ 从 set 改成 dict：token -> {"user","role","ip","created"}，权限判断要读 role。
_SESSIONS = {}
_SESS_LOCK = threading.Lock()
SESS_MAX = 128              # 上限，超过就淘汰最早的


def _new_session(user, role, ip=""):
    import secrets, time as _t
    tok = secrets.token_hex(24)          # 48 hex，足够抗暴力猜测
    with _SESS_LOCK:
        _SESSIONS[tok] = {"user": user, "role": role,
                          "ip": ip or "", "created": _t.time()}
        if len(_SESSIONS) > SESS_MAX:
            # 淘汰最早创建的（不随机 pop，避免踢掉刚登录的）
            old = sorted(_SESSIONS.items(), key=lambda kv: kv[1].get("created") or 0)
            for k, _ in old[:len(_SESSIONS) - SESS_MAX]:
                _SESSIONS.pop(k, None)
    return tok


def _session_info(tok):
    """返回会话信息 dict，无效返回 None"""
    if not tok:
        return None
    with _SESS_LOCK:
        return _SESSIONS.get(tok)


def _session_ok(tok):
    return _session_info(tok) is not None


def _drop_session(tok):
    with _SESS_LOCK:
        _SESSIONS.pop(tok, None)


# ═══ 聊天限速：防局域网用户持续占用显存 ═══
# 登录有锁定，但登录后的聊天请求没有限制：一个普通用户狂发请求就能把
# 单槽后端和显存长期占满，本机用户反而抢不到。
_CHAT_HITS = {}                      # ip -> [时间戳]
_CHAT_LOCK = threading.Lock()
CHAT_WINDOW = 60                     # 统计窗口（秒）
CHAT_MAX_PER_WIN = 30                # 每窗口最多多少次


def _chat_allow(ip):
    """返回 True 表示放行；False 表示超限"""
    now = time.time()
    with _CHAT_LOCK:
        arr = [t for t in (_CHAT_HITS.get(ip) or []) if now - t < CHAT_WINDOW]
        if len(arr) >= CHAT_MAX_PER_WIN:
            _CHAT_HITS[ip] = arr
            return False
        arr.append(now)
        _CHAT_HITS[ip] = arr
        # 清理长期不活跃的 key，防内存增长
        if len(_CHAT_HITS) > 256:
            for k in list(_CHAT_HITS.keys()):
                if not _CHAT_HITS[k]:
                    _CHAT_HITS.pop(k, None)
    return True


# ═══ 登录限速：防暴力破解 ═══
# 同一 IP 连续失败 N 次就锁一段时间；成功一次即清零。
# 没有这个，局域网里任何人都能拿字典刷密码 —— 这是"普通攻击者获取电脑权限"
# 最直接的一条路。
_LOGIN_FAILS = {}                       # ip -> [失败时间戳]
_LOGIN_LOCK = threading.Lock()
LOGIN_MAX_FAIL = 10                     # 连续失败次数上限（5 次太容易误伤自己）
LOGIN_LOCK_SECS = 120                   # 锁定秒数


def _login_blocked(ip):
    """返回剩余锁定秒数，0 表示未锁定"""
    now = time.time()
    with _LOGIN_LOCK:
        arr = [t for t in (_LOGIN_FAILS.get(ip) or []) if now - t < LOGIN_LOCK_SECS]
        _LOGIN_FAILS[ip] = arr
        if len(arr) >= LOGIN_MAX_FAIL:
            return int(LOGIN_LOCK_SECS - (now - arr[0])) + 1
    return 0


def _login_fail(ip):
    now = time.time()
    with _LOGIN_LOCK:
        arr = [t for t in (_LOGIN_FAILS.get(ip) or []) if now - t < LOGIN_LOCK_SECS]
        arr.append(now)
        _LOGIN_FAILS[ip] = arr


def _login_ok(ip):
    with _LOGIN_LOCK:
        _LOGIN_FAILS.pop(ip, None)


def find_user(name):
    """按用户名精确查找（大小写敏感），返回 (name, info) 或 (None, None)"""
    us = AUTH.get("users") or {}
    if name in us and isinstance(us[name], dict):
        return name, us[name]
    return None, None


# ═══ 局域网访问：黑名单 + 来源溯源 ═══
BLOCK_FILE = os.path.join(HERE, "blocklist.json")
# 局域网模式：设环境变量 LM_LITE_LAN=1 才监听 0.0.0.0
LAN_MODE = os.environ.get("LM_LITE_LAN", "").strip().lower() in ("1", "true", "yes", "on")
# ★ 运行时开关：服务器永远绑 0.0.0.0，靠这个标志决定是否放行非本机请求 →
#   切换时不需要重启（原来改 bind 地址必须重启进程，用户要"热加载"）。
LAN_ENABLED = [bool(LAN_MODE)]


def _local_ip():
    """取本机在局域网里的地址（优先非 127 的那个）"""
    try:
        import socket as _sk
        s = _sk.socket(_sk.AF_INET, _sk.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
        finally:
            s.close()
    except Exception:
        try:
            import socket as _sk
            return _sk.gethostbyname(_sk.gethostname())
        except Exception:
            return "127.0.0.1"


def _is_local_ip(ip):
    # ⚠ 临时强制外部模式（安全测试用，测完必须还原）
    if os.environ.get("LM_LITE_TEST_AS_REMOTE", "").strip() == "1":
        return False
    return (not ip) or ip in ("127.0.0.1", "::1", "localhost") or ip.startswith("127.")

BLOCKLIST = set()          # 被拉黑的 IP，所有请求一律忽略
_BLOCK_LOCK = threading.Lock()   # add/discard/sorted 要加锁，读判断靠 GIL 原子性
PEERS = {}                 # ip -> {"first":ts, "last":ts, "count":n, "ua":str}
_PEER_LOCK = threading.Lock()


def load_blocklist():
    global BLOCKLIST
    try:
        with open(BLOCK_FILE, encoding="utf-8") as f:
            BLOCKLIST = set(json.load(f) or [])
    except Exception:
        BLOCKLIST = set()


def save_blocklist():
    try:
        with open(BLOCK_FILE, "w", encoding="utf-8") as f:
            json.dump(sorted(BLOCKLIST), f, ensure_ascii=False, indent=1)
    except Exception:
        pass


def note_peer(ip, ua):
    """记录一次访问（用于溯源面板）"""
    if not ip:
        return
    now = time.time()
    with _PEER_LOCK:
        p = PEERS.get(ip)
        if p is None:
            PEERS[ip] = {"first": now, "last": now, "count": 1, "ua": (ua or "")[:120]}
        else:
            p["last"] = now
            p["count"] += 1
            if ua:
                p["ua"] = ua[:120]


load_blocklist()


# ═══ 请求队列记录（性能页「任务队列」用）═══
REQLOG = []          # [{id, t0, t1, src, status, promptN, outN}]
_REQ_LOCK = threading.Lock()
_REQ_SEQ = [0]
REQLOG_MAX = 40


def req_start(src):
    with _REQ_LOCK:
        _REQ_SEQ[0] += 1
        r = {"id": _REQ_SEQ[0], "t0": time.time(), "t1": None,
             "src": src or "?", "status": "running",
             "promptN": 0, "outN": 0,
             "model": os.path.basename(STATE.get("model") or "") or "—",
             "ttft": None, "tps": None, "hit": None, "err": "",
             "parallel": int(STATE.get("parallel", 1))}
        REQLOG.append(r)
        if len(REQLOG) > REQLOG_MAX:
            del REQLOG[:len(REQLOG) - REQLOG_MAX]
        return r


def req_end(r, status="done", info=None):
    """info: dict(inTok, outTok, ttft, tps, hit, err)"""
    with _REQ_LOCK:
        try:
            r["t1"] = time.time()
            r["status"] = status
            if isinstance(info, dict):
                if info.get("inTok"):  r["promptN"] = int(info["inTok"])
                if info.get("outTok"): r["outN"] = int(info["outTok"])
                r["ttft"] = info.get("ttft")
                r["tps"]  = info.get("tps")
                r["hit"]  = info.get("hit")
                if info.get("err"): r["err"] = str(info["err"])[:200]
        except Exception:
            pass


# 累计统计（前端性能页读这个）
STATS = {
    "requests": 0,          # 全部（含 DSH）
    "webRequests": 0,       # 只算 lm-lite 页面自己发的
    "inflight": 0,          # 当前处理中的推理请求数（并发，性能页读它）
    "totalIn": 0, "totalOut": 0, "totalGenMs": 0, "totalPromptMs": 0,
    "totalCacheN": 0, "totalPromptN": 0,
    "ttftList": [], "tpsList": [], "ppList": [], "hitList": [],
    "last": {},       # 最近一次请求的完整 timings
}

# prefill 速度的测量门槛：高缓存命中时 prompt_n 可能只有几个 token，
# 耗时基本是固定开销，除出来的"速度"（如 5 tok / 36ms = 139 tok/s）是噪声，
# 会污染曲线和大卡显示。不足门槛的不进曲线、不更新 last.pp（保留上次有效值）。
# 32 是折中：低于它固定开销占比过半、数值不可信；高于它（普通问答的
# 新鲜提示就有 40+ token）测量可用，曲线保持活跃。
MIN_PP_TOKENS = 32

# ═══════════════════════════════════════════════════
# API 调用默认值（DSH 等外部 /v1 客户端）
# 页面对话有自己的采样参数（存浏览器 localStorage）；这里是网关侧的默认值，
# 只填「调用方没指定」的参数，绝不覆盖调用方显式传的值。
# ═══════════════════════════════════════════════════
API_CFG_PATH = os.path.join(HERE, "api_cfg.json")
_API_CFG_LOCK = threading.Lock()
API_DEFAULTS = {
    "enabled": True,            # 总开关：关则完全按调用方原样转发
    "temp": "0.7", "maxTok": "2048", "topP": "0.95",
    "topK": "40", "repPen": "1.1", "seed": "",     # 留空 = 不注入该项
}
# 前端字段名 → llama-server 请求体字段名
API_FIELD_MAP = {
    "temp": "temperature", "maxTok": "max_tokens", "topP": "top_p",
    "topK": "top_k", "repPen": "repeat_penalty", "seed": "seed",
}
API_RANGES = {"temp": (0, 2), "maxTok": (16, 32768), "topP": (0.01, 1),
              "topK": (0, 200), "repPen": (0, 2)}


def load_api_cfg():
    cfg = dict(API_DEFAULTS)
    try:
        if os.path.isfile(API_CFG_PATH):
            with open(API_CFG_PATH, encoding="utf-8") as f:
                d = json.load(f)
            if isinstance(d, dict):
                cfg.update({k: d[k] for k in API_DEFAULTS if k in d})
    except Exception as e:
        log_warn("读取 api_cfg.json 失败: %r" % (e,))
    return cfg


API_CFG = load_api_cfg()


def save_api_cfg(cfg):
    """落盘 + 更新内存（先写临时文件再替换，避免写一半损坏）"""
    with _API_CFG_LOCK:
        API_CFG.clear()
        API_CFG.update(cfg)
        tmp = API_CFG_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=1)
        os.replace(tmp, API_CFG_PATH)


def api_cfg_validate(d):
    """校验设置页提交的 API 默认值；返回 (cfg, err)"""
    cfg = dict(API_DEFAULTS)
    if isinstance(d.get("enabled"), bool):
        cfg["enabled"] = d["enabled"]
    for k in ("temp", "maxTok", "topP", "topK", "repPen", "seed"):
        v = d.get(k)
        if v is None or v == "":
            cfg[k] = ""                      # 留空 = 不注入
            continue
        try:
            if k == "seed":
                iv = int(v)
                if iv < 0:
                    return None, "随机种子不能为负数"
                cfg[k] = str(iv)
            else:
                fv = float(v)
                lo, hi = API_RANGES[k]
                if not (lo <= fv <= hi):
                    return None, "%s 超出范围 %s ~ %s" % (k, lo, hi)
                cfg[k] = str(int(fv)) if k in ("maxTok", "topK") else ("%g" % fv)
        except Exception:
            return None, "%s 不是有效数字" % k
    return cfg, None


def apply_api_defaults(req):
    """把 API 默认值填进未指定参数的请求体；返回 (req, 注入的字段名列表)"""
    if not API_CFG.get("enabled") or not isinstance(req, dict):
        return req, []
    injected = []
    for fk, bk in API_FIELD_MAP.items():
        v = API_CFG.get(fk)
        if v in (None, ""):
            continue
        cur = req.get(bk)
        if cur is not None and cur != "":
            continue                        # 调用方指定过，不覆盖
        req[bk] = int(v) if bk in ("max_tokens", "top_k", "seed") else float(v)
        injected.append(bk)
    return req, injected


_MODELS_CACHE = {"t": 0, "data": None}
_PROMPTS_CACHE = {"v": None}   # (mtime, lines) — prompts.txt 的缓存


def find_models(force=False):
    """扫模型目录（10 秒缓存，避免每个对话请求都全盘 walk）"""
    now = time.time()
    if not force and _MODELS_CACHE["data"] is not None and now - _MODELS_CACHE["t"] < 10:
        return _MODELS_CACHE["data"]
    data = _find_models_uncached()
    _MODELS_CACHE["data"] = data
    _MODELS_CACHE["t"] = now
    return data


def _find_models_uncached():
    """扫所有配置的模型目录里的 gguf"""
    out = []
    seen = set()
    for base in MODEL_DIRS:
        if not os.path.isdir(base):
            continue
        for root, _dirs, files in os.walk(base):
            for fn in files:
                if not fn.lower().endswith(".gguf") or "mmproj" in fn.lower():
                    continue
                p = os.path.join(root, fn)
                if p in seen:
                    continue
                seen.add(p)
                try:
                    sz = os.path.getsize(p)
                except OSError:
                    continue
                # 顺手读 GGUF 头拿真实上下文（失败不影响列表）
                ctx = None
                try:
                    meta = read_gguf_meta(p)
                    if meta and meta.get("ok"):
                        ctx = meta.get("contextLength")
                except Exception:
                    pass
                out.append({
                    "path": p,
                    "name": fn,
                    "id": os.path.splitext(fn)[0],
                    "sizeGB": round(sz / (1024 ** 3), 2),
                    "ctx": ctx,
                    "dir": base,
                })
    out.sort(key=lambda x: x["name"])
    return out


_ALIVE_CACHE = {"t": 0.0, "v": False}
_ALIVE_LOCK = threading.Lock()


def backend_alive():
    """带 1.5 秒 TTL 的存活探测。

    proxy() 与 _proxy_stream() 各调一次，一次聊天会多两条到 8081 的 HTTP
    往返（实测 0.8~20ms，叠加 15~40ms）。TTL 缓存把它压成一次。
    成功探测缓存久一点、失败探测短一点，避免后端刚起来时被判死。"""
    import time as _t
    now = _t.time()
    with _ALIVE_LOCK:
        c = _ALIVE_CACHE
        if c["t"] and (now - c["t"]) < (1.5 if c["v"] else 0.4):
            return c["v"]
    v = _backend_alive_raw()
    with _ALIVE_LOCK:
        _ALIVE_CACHE["t"] = _t.time()
        _ALIVE_CACHE["v"] = v
    return v


def _backend_alive_raw():
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d/health" % LLAMA_PORT, timeout=2) as r:
            return r.status == 200
    except Exception:
        return False


_WARN_LOCK = threading.Lock()   # 轮转 + 写入必须原子，否则多线程会互写坏
_INFLIGHT_LOCK = threading.Lock()   # inflight 计数：多线程增减不能丢更新
_STATS_LOCK = threading.Lock()      # ★ STATS 的所有计数：并发完成时裸 += 会丢更新

def log_warn(msg):
    """网关事件日志。超过 1MB 自动轮转（只留最后 2000 行）——
    原先不轮转，长期运行会一直变大（已知限制第 2 条，已修）。"""
    fp = os.path.join(HERE, "gateway_warn.log")
    try:
        with _WARN_LOCK:
            try:
                if os.path.getsize(fp) > 1024 * 1024:
                    with open(fp, encoding="utf-8", errors="ignore") as f:
                        alll = f.readlines()
                    with open(fp, "w", encoding="utf-8", errors="ignore") as f:
                        f.writelines(alll[-2000:])
            except Exception:
                pass
            with open(fp, "a", encoding="utf-8") as f:
                f.write("[%s] %s\n" % (time.strftime("%H:%M:%S"), msg))
    except Exception:
        pass


# 整卡显存容量：优先环境变量覆盖（非 8GB 机器 / 共享显存时不至于算错百分比）
try:
    # 支持两个环境变量名，避免"文档写一个、代码读另一个"
    _vram_env = (os.environ.get("LM_LITE_GPU_TOTAL_MB")
                 or os.environ.get("LM_LITE_VRAM_MB") or "8192")
    GPU_CACHE = {"t": 0, "used": None, "total": int(_vram_env)}
except Exception:
    GPU_CACHE = {"t": 0, "used": None, "total": 8192}


def _gpu_sampler():
    """后台线程：不管有没有加载模型都要采样。

    原来写成 `if STATE.get("model"):` 才采样 —— 结果没加载模型时
    整卡显存一直是 null，前端只能显示 `—`。而「整卡占用」跟模型无关，
    是系统实时状态，必须一直采。"""
    while True:
        try:
            _sample_gpu_once()
        except Exception:
            pass
        # 有模型 6 秒一次（性能页要实时）；没模型 15 秒一次（省开销）
        # ★ 3 秒：用户反馈"显存数据更新太慢"。虽然每次采样要起一个 PowerShell
        #   （Get-Counter 约 0.5~1s），但显存是判断能不能再加载模型的关键信息，
        #   3 秒的精度比省那点 CPU 重要。没加载模型时还是 10 秒一次（没意义）。
        time.sleep(3 if STATE.get("model") else 10)


def _sample_gpu_once():
    ps = (
      # ★ 只取占用最大的单个适配器（通常就是独显），不能把所有适配器求和：
      #   核显 / 虚拟显示适配器 / 微软基本显示适配器都会被 Get-Counter 列出来，
      #   求和会让"整卡占用"虚高甚至超过 100%，而 total 又是写死的 8192。
      "$tot = ((Get-Counter '\\GPU Adapter Memory(*)\\Dedicated Usage' -EA 0 | "
      "ForEach-Object { $_.CounterSamples } | "
      "Where-Object { $_.CookedValue -gt 0 } | "
      "Measure-Object CookedValue -Maximum).Maximum); "
      "$p = Get-Process llama-server -EA 0 | Select-Object -First 1; "
      "$m = 0; "
      "if($p){ "
      "  $s = Get-Counter '\\GPU Process Memory(*)\\Dedicated Usage' -EA 0; "
      "  if($s){ $m = ($s.CounterSamples | "
      "    Where-Object { $_.InstanceName -like ('*pid_' + $p.Id + '_*') } | "
      "    Measure-Object CookedValue -Sum).Sum } "
      "}; "
      "Write-Output ($tot.ToString() + '|' + $m.ToString())"
    )
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                           capture_output=True, text=True, timeout=20,
                           encoding="utf-8", errors="ignore",
                           creationflags=0x08000000)   # CREATE_NO_WINDOW
        out = (r.stdout or "").strip().replace("\r", "")
        line = out.split("\n")[-1].strip() if out else ""
        if "|" in line:
            a, b = line.split("|", 1)
            GPU_CACHE["used"] = round(float(a) / (1024 ** 2)) if a.strip() else None
            GPU_CACHE["model"] = round(float(b) / (1024 ** 2)) if b.strip() else None
            GPU_CACHE["t"] = time.time()
    except Exception:
        pass


def read_gpu_memory():
    """只读缓存，立即返回（由后台线程更新）"""
    return GPU_CACHE["used"], GPU_CACHE["total"], GPU_CACHE.get("model")





def load_progress():
    """从 backend.log 尾部解析加载进度（llama.cpp 会打 load_tensors 进度）

    机制：先取日志尾部窗口，再从【后往前】找最后一条 "listening on" / "model loaded"，
    找到就只算它后面那几行的进度。没有这个截断时，换模型会立刻读到上一次的
    "listening on" 从而直接报 100%，然后挂几十秒等新行把它挤出窗口 ——
    用户看到的就是「先 100% 再掉回个位数」。

    已知局限（诚实标注）：如果上一次加载【失败】（那次日志里没有 listening on），
    窗口里的旧百分比不会被截断；日志超过窗口大小时也可能只看到片段。
    （曾经这里靠一个 LOAD_STARTED_AT 时间戳来区分，但 llama.cpp 打的是相对时间
     `0.01.288`，无法与墙钟对齐，所以那个变量从未生效，已删除。）
    """
    try:
        fp = os.path.join(HERE, "backend.log")
        if not os.path.isfile(fp):
            return None
        with open(fp, "rb") as fb:
            size = fb.seek(0, 2)
            fb.seek(max(0, size - 65536))     # 扩大窗口，够覆盖一次加载
            tail = fb.read().decode("utf-8", "ignore")
        lines = tail.splitlines()[-200:]
        # 从后往前扫，遇到「上一次加载完成」的标记就停 —— 那个标记之前的进度属于旧模型
        start = 0
        for i in range(len(lines) - 1, -1, -1):
            if 'listening on' in lines[i] or 'model loaded' in lines[i]:
                # 最后一条如果是它自己刚完成的，说明这次真的完了
                if i == len(lines) - 1:
                    return 100
                # 否则这个标记属于上一次加载，只扫它之后的行
                start = i + 1
                break
        pct = None
        for l in lines[start:]:
            m = re.search(r'\]\s*(\d{1,3})\s*%', l)          # load_tensors: ... [ 47%]
            if m:
                pct = int(m.group(1))
            m2 = re.search(r'load_tensors:.*?(\d{1,3})\s*%', l)
            if m2:
                pct = int(m2.group(1))
            if 'model loaded' in l or 'listening on' in l:
                return 100
        return pct
    except Exception:
        return None


# ── GGUF 头部元数据（真实上下文长度 / KV 每 token 占用） ──
# 别再按文件名猜（"4b" 就按 4B 算）：直接读模型头部的 KV 元数据。
# 与 gguf_meta2.js 同款逻辑的 Python 版；按 (路径,大小,mtime) 缓存，只读前 16MB。
_GGUF_CACHE = {}


def read_gguf_meta(path, force=False):
    if not path or not os.path.isfile(path):
        return None
    try:
        st = os.stat(path)
        key = (os.path.abspath(path), st.st_size, st.st_mtime_ns)
    except OSError:
        return None
    c = _GGUF_CACHE.get(key)
    if c is not None and not force:
        return c
    out = {"ok": False, "error": ""}
    try:
        with open(path, "rb") as f:
            b = f.read(16 * 1024 * 1024)
        if len(b) < 24:
            out["error"] = "文件太小，不是有效的 GGUF 头部"
            _GGUF_CACHE[key] = out
            return out
        if b[:4] != b"GGUF":
            out["error"] = "magic 不匹配，不是 GGUF 文件"
            _GGUF_CACHE[key] = out
            return out
        p = 4
        version = int.from_bytes(b[p:p+4], "little"); p += 4
        p += 8                                    # tensor_count
        n_kv = int.from_bytes(b[p:p+8], "little"); p += 8
        total = len(b)

        def need(k, what):
            # 每一步都校验边界：错位/截断的头部必须显式报错，
            # 不能静默读出半个字典还装成 ok=True（那会把错值当真值用）
            if p + k > total:
                raise ValueError("GGUF 头部数据不足（%s：偏移 %d + %d > %d）"
                                 % (what, p, k, total))

        def rstr():
            nonlocal p
            need(8, "key 长度")
            n = int.from_bytes(b[p:p+8], "little"); p += 8
            if n > total - p:
                raise ValueError("字符串长度越界: %d（剩余 %d）" % (n, total - p))
            s = b[p:p+n].decode("utf-8", "ignore"); p += n
            return s

        def rval(t):
            nonlocal p
            if   t == 0: need(1, "u8");    v = b[p]; p += 1; return v
            elif t == 1: need(1, "i8");    v = int.from_bytes(b[p:p+1], "little", signed=True); p += 1; return v
            elif t == 2: need(2, "u16");   v = int.from_bytes(b[p:p+2], "little"); p += 2; return v
            elif t == 3: need(2, "i16");   v = int.from_bytes(b[p:p+2], "little", signed=True); p += 2; return v
            elif t == 4: need(4, "u32");   v = int.from_bytes(b[p:p+4], "little"); p += 4; return v
            elif t == 5: need(4, "i32");   v = int.from_bytes(b[p:p+4], "little", signed=True); p += 4; return v
            elif t == 6: need(4, "f32");   v = struct.unpack_from("<f", b, p)[0]; p += 4; return v
            elif t == 7: need(1, "bool");  v = b[p]; p += 1; return bool(v)
            elif t == 8: return rstr()
            elif t == 9:
                need(12, "array 头")
                et = int.from_bytes(b[p:p+4], "little"); p += 4
                n = int.from_bytes(b[p:p+8], "little"); p += 8
                # 每个元素至少 1 字节：count 超过剩余字节数的一律是坏数据，
                # 不拦的话一个伪造 count 就能把内存吃光
                if n > total - p:
                    raise ValueError("数组长度越界: %d（剩余 %d）" % (n, total - p))
                return [rval(et) for _ in range(n)]
            elif t == 10: need(8, "u64");  v = int.from_bytes(b[p:p+8], "little"); p += 8; return v
            elif t == 11: need(8, "i64");  v = int.from_bytes(b[p:p+8], "little", signed=True); p += 8; return v
            elif t == 12: need(8, "f64");  v = struct.unpack_from("<d", b, p)[0]; p += 8; return v
            raise ValueError("未知 GGUF 值类型: %d" % t)

        meta = {}
        truncated = False               # 头部没读完（KV 段超过 16MB 或数据损坏）
        for _ in range(n_kv):
            try:
                k = rstr()
                need(4, "value 类型")
                t = int.from_bytes(b[p:p+4], "little"); p += 4
                meta[k] = rval(t)
            except Exception:
                truncated = True
                break                   # 拿到多少算多少，但必须标记
        arch = str(meta.get("general.architecture") or "")
        pre = (arch + ".") if arch else ""
        ctx = meta.get(pre + "context_length")
        L  = meta.get(pre + "block_count")
        HK = meta.get(pre + "attention.head_count_kv")
        E  = meta.get(pre + "embedding_length")
        H  = meta.get(pre + "attention.head_count")
        KL = meta.get(pre + "attention.key_length")
        VL = meta.get(pre + "attention.value_length")
        hd = int(KL) if KL else (round(int(E) / int(H)) if (E and H) else None)
        vd = int(VL) if VL else hd
        per_tok_kb = None
        swa = None
        pat = meta.get(pre + "attention.sliding_window_pattern")
        sw = meta.get(pre + "attention.sliding_window")
        if L and HK and hd:
            if isinstance(pat, list) and pat and sw:
                # 混合注意力（SWA）：只有全注意力层需要随上下文存 KV，
                # 滑动窗口层是固定开销 —— 朴素估算会严重高估
                full = sum(1 for x in pat if not x)
                swaL = sum(1 for x in pat if x)
                per_tok = full * int(HK) * (int(hd) + int(vd or hd)) * 2
                swa = {"window": int(sw), "fullLayers": full,
                       "swaLayers": swaL, "totalLayers": len(pat)}
            else:
                per_tok = int(L) * int(HK) * (int(hd) + int(vd or hd)) * 2
            per_tok_kb = round(per_tok / 1024, 1)
        out = {
            "ok": True,
            "version": version,           # GGUF 版本号（v1/v2/v3 头部布局相同，见注释）
            "truncated": truncated,       # 头部没读完：KV 段 >16MB 或数据损坏
            "kvRead": len(meta),
            "name": meta.get("general.name"),
            "arch": arch,
            "contextLength": int(ctx) if ctx else None,
            "layers": int(L) if L else None,
            "kvHeads": int(HK) if HK else None,
            "headDim": int(hd) if hd else None,
            "perTokKB": per_tok_kb,          # f16 下每 token 的 KV（KiB）
            "swa": swa,
            "fileMB": round(st.st_size / (1024 ** 2)),
        }
        # ★ MoE 判定：--n-cpu-moe 只对混合专家模型有效，dense 模型传了要么被忽略
        #   要么直接报错。前端要据此禁用那个输入框，不能让用户以为对任何模型都生效。
        try:
            _ec = meta.get(pre + "expert_count")
            _eu = meta.get(pre + "expert_used_count")
            out["expertCount"] = int(_ec) if _ec is not None else 0
            out["expertUsed"] = int(_eu) if _eu is not None else 0
            out["isMoE"] = bool(_ec and int(_ec) > 1)
        except Exception:
            out["expertCount"] = 0
            out["expertUsed"] = 0
            out["isMoE"] = False
    except Exception as e:
        out = {"ok": False, "error": str(e)[:200]}
    _GGUF_CACHE[key] = out
    return out


MODEL_CTX = {}   # 模型 id -> 建议上下文（加载后可覆盖）


def match_model(name):
    """把请求里的 model 名匹配到实际 gguf 路径（模糊匹配）"""
    if not name:
        return None
    found = find_models()
    # 1. 精确 basename（不含 .gguf）
    for m in found:
        if m["id"].lower() == name.lower():
            return m
    # 2. 包含匹配（DSH 里写 spark-x2.5-1.7b-q4 之类）
    key = name.lower().replace("_", "-")
    for m in found:
        mid = m["id"].lower().replace("_", "-")
        if key in mid or mid in key:
            return m
    # 3. 按 token 重合度打分
    toks = [t for t in key.replace(".", "-").split("-") if len(t) > 1]
    best, best_score = None, 0
    for m in found:
        mid = m["id"].lower().replace("_", "-")
        score = sum(1 for t in toks if t in mid)
        if score > best_score:
            best, best_score = m, score
    return best if best_score >= 2 else None


def ctx_for(model_id):
    """按模型决定上下文（别再一律 65536）"""
    n = (model_id or "").lower()
    if "spark" in n:            # Spark-X2.5 全系 1M 训练上下文，实际给 100K
        return 100096
    if "qwen3.5" in n:
        return 32768
    if "gemma" in n:
        return 32768
    if "qwen3" in n:
        return 40960
    return 65536


def ensure_model(name):
    """确保请求的模型已加载；不是就自动切换。返回 (ok, 说明)"""
    m = match_model(name)
    if not m:
        # 通配名（客户端不指定具体模型）→ 沿用已加载，正常
        nm = (name or "").strip().lower()
        generic = nm in ("", "local", "auto", "default", "model", "unspecified")
        if STATE.get("model") and backend_alive():
            if not generic:
                # ★ 具体模型名却匹配不上：这是最容易骗人的路径 —— 用户以为在用 A，
                #   实际 B 在答。不再静默，标记到 STATE 供响应头/队列透出。
                STATE["mismatch"] = "%s → %s" % (name, os.path.basename(STATE["model"]))
                log_warn("!! 模型不匹配：请求 %r，实际用 %s（不是同一个模型，能力不可比）"
                         % (name, os.path.basename(STATE["model"])))
            return True, "沿用已加载"
        return False, "找不到模型: %s" % name
    cur = STATE.get("model")
    if cur and os.path.basename(cur) == m["name"] and backend_alive():
        STATE["mismatch"] = ""        # 匹配上了就清掉上次的不一致标记
        return True, "已加载"
    # 正在生成时不要硬切（会把别人的流掐断），等它完
    # ★ 正在生成时不要硬切。原来"等 30 秒"之后照样 start_backend()，
    #   而 start_backend 第一句就是 stop_backend_unlocked()（terminate 后端），
    #   正在流式的客户端会收到半截 + 断流 —— 注释承诺的和实现根本不是一回事。
    #   现在：等不到空闲就【放弃并如实报错】，让调用方决定，绝不静默砍别人的流。
    if _active_busy():
        waited = 0.0
        while _active_busy() and waited < 20:
            time.sleep(0.2); waited += 0.2
        if _active_busy():
            n = len(_active_snapshot())
            return False, ("当前有 %d 个请求正在生成，切换模型会中断它们。"
                           "请等生成结束或先停止，再切换。" % n)
    # 需要切换
    ctx = MODEL_CTX.get(m["id"]) or ctx_for(m["id"])
    # ★ 必须把当前并发数带上：漏传会让 llama-server 回到 -np 1，
    #   而 STATE 里还写着 3 → 队列页显示"并发上限 3"实际串行
    ok, err = start_backend(m["path"], ctx, STATE.get("kv") or "q8_0",
                            STATE.get("parallel") or 1)
    return ok, ("已自动切换" if ok else err)


def stop_backend():
    with BACKEND_LOCK:
        stop_backend_unlocked()


def _abort_upstream():
    """让 llama-server 放弃当前生成（释放唯一的 slot）"""
    try:
        req = urllib.request.Request(
            "http://127.0.0.1:%d/slots/0?action=erase" % LLAMA_PORT, method="POST")
        urllib.request.urlopen(req, timeout=3).read()
    except Exception:
        pass


def stop_backend_unlocked():
    _MODELS_CACHE["data"] = None      # 目录可能变了，下次重新扫
    p = STATE.get("proc")
    if p and p.poll() is None:
        try:
            p.terminate()
            p.wait(timeout=12)
        except Exception:
            try:
                p.kill()
            except Exception:
                pass
    STATE["proc"] = None
    # 保险：按端口找残留
    try:
        r = subprocess.run(["netstat", "-ano"], capture_output=True, text=True,
                           encoding="utf-8", errors="ignore", timeout=10)
        for line in r.stdout.splitlines():
            if "LISTENING" not in line:
                continue
            parts = line.split()
            if len(parts) < 2:
                continue
            local = parts[1]
            if not (local.endswith(":%d" % LLAMA_PORT) or local.endswith("]%d" % LLAMA_PORT)):
                continue
            pid = parts[-1]
            if not pid or pid == "0":
                continue
            # ★ 只杀自己起的那个：校验进程名必须是 llama-server
            try:
                chk = subprocess.run(
                    ["tasklist", "/FI", "PID eq %s" % pid, "/FO", "CSV", "/NH"],
                    capture_output=True, text=True,
                    encoding="utf-8", errors="ignore", timeout=10)
                out = (chk.stdout or "").strip()
                if "llama-server" not in out.lower():
                    log_warn("端口 %d 被 %s 占用，但不是 llama-server，跳过" % (LLAMA_PORT, out[:60]))
                    continue
            except Exception:
                continue
            subprocess.run(["taskkill", "/F", "/PID", pid], capture_output=True, timeout=10)
    except Exception:
        pass
    time.sleep(1.5)


def start_backend(model_path, ctx, kv, parallel=1, gpu_layers=None, kv_offload=True,
                  cpu_moe=0, batch=2048, ubatch=1024, threads=10,
                  flash_attn=True, ctx_shift=False, use_mmap=True,
                  ctk=None, ctv=None, mlock=False, rope_scaling=None,
                  no_reasoning_preserve=False):
    # ★ 加载一个模型可能占用 BACKEND_LOCK 240 秒（等 llama-server 出 listening on）。
    #   原来第二次调用会一直阻塞在锁上，线程越堆越多（配合浏览器只有 6 个连接，
    #   页面就彻底卡死）。现在：已有加载在进行时立即返回，让调用方重试。
    if BACKEND_LOCK.locked():
        return False, "已有模型正在加载，请等它完成后再试"
    with BACKEND_LOCK:
        return _start_backend_locked(model_path, ctx, kv, parallel,
                                     gpu_layers, kv_offload, cpu_moe,
                                     batch, ubatch, threads, flash_attn,
                                     ctx_shift, use_mmap,
                                     ctk, ctv, mlock, rope_scaling,
                                     no_reasoning_preserve)


def _start_backend_locked(model_path, ctx, kv, parallel=1,
                          gpu_layers=None, kv_offload=True, cpu_moe=0,
                          batch=2048, ubatch=1024, threads=10,
                          flash_attn=True, ctx_shift=False, use_mmap=True,
                          ctk=None, ctv=None, mlock=False, rope_scaling=None,
                          no_reasoning_preserve=False):
    stop_backend_unlocked()
    STATE["loading"] = True
    _ALIVE_CACHE["t"] = 0.0        # 清掉存活缓存，让调用方立刻看到"加载中"
    STATE["last_error"] = ""
    # ★ GPU 层数：None/99 = 全卸载；0 = 纯 CPU；其他值 = 部分卸载
    _ngl = 99 if gpu_layers is None else max(0, min(999, int(gpu_layers)))
    # ★ llama.cpp 的 -c 是【共享 KV 池总大小】，-np N 时每个槽只分到 c/N。
    #   而 UI 里"上下文"一直按【每个会话】显示 —— 用户设「100K + 并发 3」
    #   实际每会话只有 33K，超了报 "exceeds the available context size"，
    #   而界面上每个数字都是错的。这里乘上并发数，让 -c 变成总池，
    #   使"上下文"真正等于每个会话可用量（显存也按总池估算，见 /api/vram）。
    _par = max(1, min(4, int(parallel or 1)))
    _ctx_total = int(ctx) * _par
    args = [LLAMA_SERVER, "-m", model_path, "-c", str(_ctx_total), "-ngl", str(_ngl),
            "--port", str(LLAMA_PORT), "--host", "127.0.0.1", "--no-webui",
            "-np", str(_par),
            # ★ 性能参数
            "-fa", ("on" if flash_attn else "off"),
            "-b", str(max(32, min(8192, int(batch or 2048)))),
            "-ub", str(max(32, min(4096, int(ubatch or 1024)))),
            "-tb", str(max(1, min(32, int(threads or 10)))),
            ]
    # ★ llama.cpp 默认就用 mmap，只有 --no-mmap；没有 --mmap 这个参数
    #   （之前写成 if use_mmap: args += ["--mmap"] 导致启动直接失败）
    if not use_mmap:
        args += ["--no-mmap"]
    if ctx_shift:
        args += ["--context-shift"]
    # K/V 缓存类型：可以分别指定（-ctk / -ctv），默认用统一的 kv
    _ctk = ctk or kv
    _ctv = ctv or kv
    if _ctk and _ctk != "f16":
        args += ["--cache-type-k", _ctk]
    if _ctv and _ctv != "f16":
        args += ["--cache-type-v", _ctv]
    # 锁定在物理内存，避免模型/缓存被系统换出到页面文件（慢几十倍）
    if mlock:
        args += ["--mlock"]
    # RoPE 缩放：长上下文外推（linear 适合 2~4 倍，yarn 适合 8 倍以上）
    if rope_scaling and rope_scaling not in ("", "none"):
        args += ["--rope-scaling", str(rope_scaling)]
    # 不保留历史思考内容（每次重传会白白吃掉大量 token）
    if no_reasoning_preserve:
        args += ["--no-reasoning-preserve"]
    # KV 不放进显存（省显存但明显变慢，适合显存吃紧时）
    if not kv_offload:
        args += ["--no-kv-offload"]
    # MoE 模型：把前 N 层专家留在 CPU（dense 模型无效）
    try:
        _moe = int(cpu_moe or 0)
        if _moe > 0:
            args += ["--n-cpu-moe", str(min(999, _moe))]
    except Exception:
        pass
    logf = open(os.path.join(HERE, "backend.log"), "a", encoding="utf-8", errors="ignore")
    # 追加前先裁剪：超过 2MB 就只留最后 2000 行（保持行号语义连续）
    try:
        lp = os.path.join(HERE, "backend.log")
        if os.path.isfile(lp) and os.path.getsize(lp) > 2 * 1024 * 1024:
            logf.close()
            with open(lp, encoding="utf-8", errors="ignore") as f:
                alll = f.readlines()
            with open(lp, "w", encoding="utf-8", errors="ignore") as f:
                f.writelines(alll[-2000:])
            logf = open(lp, "a", encoding="utf-8", errors="ignore")
    except Exception:
        # ★ 轮转失败必须重开一个可用句柄，否则后面 Popen 会拿到已关闭的文件
        try:
            logf.close()
        except Exception:
            pass
        try:
            logf = open(os.path.join(HERE, "backend.log"), "a", encoding="utf-8", errors="ignore")
        except Exception:
            logf = subprocess.DEVNULL
    try:
        p = subprocess.Popen(args, cwd=BACKEND_DIR, stdout=logf, stderr=subprocess.STDOUT,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception as e:
        STATE["loading"] = False
        STATE["last_error"] = "启动失败: %s" % e
        return False, STATE["last_error"]

    STATE["proc"] = p
    t0 = time.time()
    while time.time() - t0 < 240:
        if p.poll() is not None:
            STATE["loading"] = False
            try:
                tail = open(os.path.join(HERE, "backend.log"), encoding="utf-8", errors="ignore").read()[-600:]
            except Exception:
                tail = ""
            STATE["last_error"] = "后端退出:\n" + tail
            return False, STATE["last_error"]
        if backend_alive():
            STATE["model"] = model_path
            STATE["ctx"] = ctx
            STATE["kv"] = kv
            STATE["loading"] = False
            # 记住本次配置，供下次启动自动加载（原先是死代码）
            try:
                with open(os.path.join(HERE, "last.json"), "w", encoding="utf-8") as f:
                    json.dump({"path": model_path, "ctx": ctx, "kv": kv,
                               "parallel": parallel,
                               "gpu_layers": gpu_layers if gpu_layers is not None else 99,
                               "kv_offload": bool(kv_offload),
                               "cpu_moe": int(cpu_moe or 0)}, f, ensure_ascii=False)
            except Exception:
                pass
            return True, ""
        time.sleep(1.2)
    STATE["loading"] = False
    STATE["last_error"] = "加载超时（240秒）"
    return False, STATE["last_error"]




def _friendly_err(msg):
    """把底层异常翻译成用户看得懂的一句话。

    原来直接把原始文本写进队列详情，用户看到
    `'NoneType' object has no attribute 'read1'`、`[WinError 10053] 你的主机中的软件中止了...`
    这种内部错误，观感像程序崩了。实际它们分别对应「用户点了停止」「客户端断开」。
    """
    low = (msg or "").lower()
    if "read1" in low or "read of closed" in low or "nonetype" in low:
        return "已停止（用户中断）"
    if "10053" in low or "10054" in low or "中止" in low or "aborted" in low:
        return "客户端已断开"
    if "timed out" in low or "timeout" in low:
        return "上游超时"
    if "connection" in low or "refused" in low or "reset" in low or "断开" in low:
        return "上游连接中断"
    return (msg or "未知错误")[:160]


def _record_from_probe(blob, is_web=False, t_req=None, t_think=None, t_body=None, t_end=None):
    """从转发的 SSE/JSON 里提取 timings 与 usage，并入全局统计。
    返回 (输入 token, 输出 token) —— 推理队列面板要用。"""
    if not blob:
        return {}
    txt = blob.decode("utf-8", "ignore")
    tm = None
    us = None
    for line in txt.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("data:"):
            line = line[5:].strip()
        if line == "[DONE]" or not line.startswith("{"):
            continue
        try:
            o = json.loads(line)
        except Exception:
            continue
        if isinstance(o.get("timings"), dict):
            tm = o["timings"]
        if isinstance(o.get("usage"), dict):
            us = o["usage"]
    if not tm:
        # ★ 请求数要在 return 之前计：否则报错/无 timings 的请求不计入「总请求数」，
        #   出现「队列 3 条、统计 4 条」这种对不上的情况
        with _STATS_LOCK:
            STATS["requests"] += 1
            if is_web:
                STATS["webRequests"] += 1
        if us:
            return {"inTok": int(us.get("prompt_tokens") or 0),
                    "outTok": int(us.get("completion_tokens") or 0)}
        return {}
    cache_n = int(tm.get("cache_n") or 0)
    prompt_n = int(tm.get("prompt_n") or 0)
    pred_n = int(tm.get("predicted_n") or 0)
    pp = tm.get("prompt_per_second")
    tp = tm.get("predicted_per_second")
    in_tok = (us or {}).get("prompt_tokens") or (cache_n + prompt_n)
    # ★ 整块在一个锁里：原来 requests 加了锁、后面 6 个裸 += 没加，
    #   -np 2~4 并发完成时必然丢计数（"队列 N 条 / 统计 M 条"对不上就出在这）。
    with _STATS_LOCK:
        STATS["requests"] += 1
        if is_web:
            STATS["webRequests"] += 1
        STATS["totalIn"] += int(in_tok or 0)
        STATS["totalOut"] += int(pred_n)
        STATS["totalGenMs"] += int(tm.get("predicted_ms") or 0)
        STATS["totalPromptMs"] += int(tm.get("prompt_ms") or 0)
        STATS["totalCacheN"] += cache_n
        STATS["totalPromptN"] += prompt_n
    hit = round(cache_n / (cache_n + prompt_n) * 100, 1) if (cache_n + prompt_n) else 0
    # prompt_ms 是 prefill 总时长，不是"正文首字"。只在没有真实 ttft 时才用它兜底
    # ★ prefill 速度门槛：实算 token 太少时该速度是固定开销噪声，不采用
    pp_valid = bool(pp) and prompt_n >= MIN_PP_TOKENS
    if pp_valid:
        STATS["ppList"].append(float(pp)); STATS["ppList"] = STATS["ppList"][-200:]
    if tp: STATS["tpsList"].append(float(tp)); STATS["tpsList"] = STATS["tpsList"][-200:]
    # 0% 也是有效数据（完全没有命中），不能因为假值就丢
    if (cache_n + prompt_n) > 0:
        STATS["hitList"].append(hit); STATS["hitList"] = STATS["hitList"][-200:]
    # ★ 真实首字：网关自己测的（不再拿 prefill 时长冒充）
    ttft_ms = None
    think_ttft_ms = None
    think_ms = None
    if t_req is not None:
        if t_think is not None:
            think_ttft_ms = int((t_think - t_req) * 1000)
        if t_body is not None:
            ttft_ms = int((t_body - t_req) * 1000)
            if t_think is not None:
                think_ms = int((t_body - t_think) * 1000)
        # ★ 没有正文就不报 TTFT（诚实显示 —），绝不能拿结束时间冒充

    if ttft_ms is not None and ttft_ms > 0:
        STATS["ttftList"].append(float(ttft_ms))
        STATS["ttftList"] = STATS["ttftList"][-200:]

    # ★ last（最近一次请求详情）：页面请求和 API 调用（DSH 等）一视同仁都更新，
    # 否则会出现「卡片有数据、详情面板却写还没有请求」的割裂状态。
    # 新请求测不到的指标（非流式没有首字概念）保留上次有效值，不用 null 顶掉；
    # pp 在实算 token 不足门槛时同样保留（否则固定开销噪声会顶掉 1500+）。
    # src 标记来源，前端详情面板会标注，API 流量不再伪装成页面流量。
    old = STATS["last"] if isinstance(STATS["last"], dict) else {}
    STATS["last"] = {
        "pp": round(float(pp), 1) if pp_valid else old.get("pp"),
        "tps": round(float(tp), 1) if tp else old.get("tps"),
        "hit": hit if (cache_n + prompt_n) else old.get("hit"),
        "cacheN": cache_n, "promptN": prompt_n,
        "inTok": int(in_tok or 0), "outTok": pred_n,
        "ppMs": int(tm.get("prompt_ms") or 0),
        "genMs": int(tm.get("predicted_ms") or 0),
        "ttft": ttft_ms if ttft_ms is not None else old.get("ttft"),
        "thinkTtft": think_ttft_ms if think_ttft_ms is not None else old.get("thinkTtft"),
        "thinkMs": think_ms if think_ms is not None else old.get("thinkMs"),
        "src": "page" if is_web else "api",
        "streamed": bool(t_think is not None or t_body is not None),
    }
    return {"inTok": int(in_tok or 0), "outTok": int(pred_n),
            "ttft": ttft_ms, "tps": tp, "hit": hit}


class Handler(BaseHTTPRequestHandler):
    # 不暴露 Python 版本号
    server_version = "lm-lite"
    sys_version = ""
    protocol_version = "HTTP/1.1"      # ★ 启用 keep-alive，省掉每个请求的 TCP 建连
    server_version = "lm-lite/1.0"

    def log_message(self, *a):
        # 不打印访问日志（避免刷屏），但异常要留痕
        pass

    # ---------- helpers ----------
    # 只允许本机来源（精确匹配，不用前缀，防 localhost.evil.com）
    ALLOWED_ORIGINS = ("http://127.0.0.1:%d" % GATEWAY_PORT,
                       "http://localhost:%d" % GATEWAY_PORT,
                       "http://127.0.0.1", "http://localhost")
    def _origin(self):
        return self.headers.get("Origin") or ""

    def _reject_if_lan_off(self):
        """局域网开关关着时，挡掉所有非本机来源（热切换靠这个标志）"""
        try:
            ip = self.client_address[0] if self.client_address else ""
        except Exception:
            ip = ""
        if LAN_ENABLED[0] or _is_local_ip(ip):
            return False
        self.send_json({"error": "局域网访问未开启（请在设置页打开）"}, 403)
        return True

    def _is_blocked(self):
        """来源 IP 在黑名单里 → 直接断连，不返回任何东西"""
        try:
            ip = self.client_address[0] if self.client_address else ""
        except Exception:
            ip = ""
        return ip in BLOCKLIST

    def _send_login_page(self):
        """极简登录页（不依赖 index.html，避免未登录时加载整个前端）"""
        html = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>登录 · lm-lite</title>
<style>
:root{--bg:#0d1117;--s1:#161b22;--ln:#242c38;--tx:#e9ebef;--dim:#8b94a1;
--ac:#818cf8;--er:#f87171}
*{box-sizing:border-box}
body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
background:var(--bg);color:var(--tx);font:14px/1.6 system-ui,"Segoe UI",sans-serif}
.box{width:min(340px,90vw);background:var(--s1);border:1px solid var(--ln);
border-radius:14px;padding:28px 24px;box-shadow:0 20px 50px rgba(0,0,0,.4)}
h1{margin:0 0 4px;font-size:17px;font-weight:700}
p.sub{margin:0 0 20px;font-size:11.5px;color:var(--dim)}
label{display:block;font-size:11.5px;color:var(--dim);margin:12px 0 5px}
input{width:100%;padding:9px 12px;border-radius:8px;background:var(--bg);
border:1px solid var(--ln);color:var(--tx);font:inherit;outline:none;
transition:border-color .18s,box-shadow .18s}
input:focus{border-color:var(--ac);box-shadow:0 0 0 3px rgba(129,140,248,.18)}
button{width:100%;margin-top:20px;padding:10px;border:none;border-radius:8px;
background:var(--ac);color:#fff;font:inherit;font-weight:600;cursor:pointer;
transition:filter .15s,transform .1s}
button:hover{filter:brightness(1.08)}
button:active{transform:scale(.985)}
button:disabled{opacity:.55;cursor:not-allowed}
#err{margin-top:12px;font-size:11.5px;color:var(--er);min-height:16px}
</style></head><body>
<form class="box" id="f">
  <h1>lm-lite</h1>
  <p class="sub">这个实例开启了登录保护</p>
  <label>用户名</label>
  <input id="u" autocomplete="off" data-lpignore="true" required>
  <label>密码</label>
  <input id="p" type="password" autocomplete="off" data-lpignore="true" required>
  <button id="b" type="submit">登录</button>
  <div id="err"></div>
</form>
<script>
document.getElementById('f').onsubmit = async (e)=>{
  e.preventDefault();
  const b = document.getElementById('b'), err = document.getElementById('err');
  b.disabled = true; err.textContent = '';
  try{
    const r = await fetch('/api/login', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({
        user: document.getElementById('u').value,
        password: document.getElementById('p').value
      })
    });
    const j = await r.json();
    if(j.ok){ location.href = '/'; return; }
    err.textContent = j.error || '登录失败';
  }catch(ex){ err.textContent = '无法连接网关'; }
  b.disabled = false;
};
</script></body></html>"""
        data = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    # 只有 admin 能调用的接口（改模型/改配置/改用户/拉黑设备）
    ADMIN_ONLY = (
        "/api/load", "/api/unload", "/api/stop", "/api/config",
        "/api/auth_config", "/api/block", "/api/queue/clear",
        "/api/model-dirs", "/api/browse", "/api/gguf-meta",
        "/api/stats", "/api/load-profile",
        # 审计日志（谁在什么时候做了什么、来源 IP）与后端运行日志
        # （可能含提示词片段）都属于敏感信息，普通用户不可读。
        "/api/audit", "/api/log",
    )

    def _current_user(self):
        """返回 (user, role) ；未登录返回 (None, None)"""
        tok = ""
        ck = self.headers.get("Cookie") or ""
        for part in ck.split(";"):
            part = part.strip()
            if part.startswith("lml_tok="):
                tok = part.split("=", 1)[1]
                break
        if not tok:
            tok = self.headers.get("X-LML-Token") or ""
        info = _session_info(tok)
        if not info:
            return None, None, ""
        return info.get("user"), info.get("role"), tok

    def _reject_if_forbidden(self, path):
        """非本机来源调管理类接口 → 必须是 admin。必须在 _reject_if_unauthed 之后调用。

        ⚠ 这里【不能】因为 auth_required() 为假就整体放行：
        登录保护关闭时，局域网任何人都能 POST /api/auth_config 自建 admin 账号，
        然后再让你打开登录保护，就永久拿到了管理权 —— 而所有者只看到
        "登录保护已开启"，不会知道多了一个管理员。

        正确语义：本机 = 无条件 admin（物理接触即信任）；
        非本机 = 无论登录保护开没开，管理类接口一律要求 role 为 admin
        （没开启登录时就没有 admin 会话，等于全部拒绝 —— 这是刻意的）。
        """
        _u, role, _t = self._current_user()
        # 本机（物理操控这台电脑的人）视为 admin
        try:
            _ip = self.client_address[0] if self.client_address else ""
        except Exception:
            _ip = ""
        # ★ 测试开关：LM_LITE_FORCE_REMOTE=1 时把本机也当外部设备，用于验证权限
        _force_remote = os.environ.get("LM_LITE_FORCE_REMOTE", "").strip() == "1"
        if _is_local_ip(_ip) and not _force_remote:
            return False
        for p in self.ADMIN_ONLY:
            if path == p or path.startswith(p + "/") or path.startswith(p + "?"):
                if role != "admin":
                    self.send_json({"error": "需要管理员权限", "needAdmin": True}, 403)
                    return True
                return False
        return False

    def _reject_if_lan_manage(self, path):
        """开启局域网但一个密码都没设时，禁止非本机调管理类接口。

        给用户一个明确的提示，而不是含糊的 403：
        "先在本机设置一个管理员密码，再从局域网使用管理功能。"
        """
        try:
            _ip = self.client_address[0] if self.client_address else ""
        except Exception:
            _ip = ""
        _force_remote = os.environ.get("LM_LITE_FORCE_REMOTE", "").strip() == "1"
        if _is_local_ip(_ip) and not _force_remote:
            return False
        us = AUTH.get("users") or {}
        has_pw = any((v.get("hash") and v.get("salt")) for v in us.values())
        if has_pw:
            return False
        for p in self.ADMIN_ONLY:
            if path == p or path.startswith(p + "/") or path.startswith(p + "?"):
                self.send_json({
                    "error": "尚未设置管理员密码，请先在本机设置后再从局域网使用管理功能",
                    "needSetup": True}, 403)
                return True
        return False

    def _reject_if_unauthed(self, path):
        """没登录就挡掉（登录页和登录接口本身除外）"""
        if not auth_required():
            return False
        # ★ 本机访问免登录：127.0.0.1 说明使用者已经在物理操控这台电脑，
        #   密码的用途是"拦住局域网里的别人"，不是给本机添堵。
        #   本机的 CSRF 风险由 _origin_ok() 兜（写操作校验 Origin）。
        try:
            _ip = self.client_address[0] if self.client_address else ""
        except Exception:
            _ip = ""
        if _is_local_ip(_ip):
            return False
        # 白名单：登录页、登录接口、静态资源
        if path in ("/login", "/api/login", "/api/auth_status") or path.startswith("/favicon"):
            return False
        # 带有效 token 的 cookie 或 header
        tok = ""
        ck = self.headers.get("Cookie") or ""
        for part in ck.split(";"):
            part = part.strip()
            if part.startswith("lml_tok="):
                tok = part.split("=", 1)[1]
                break
        if not tok:
            tok = self.headers.get("X-LML-Token") or ""
        if tok and _session_ok(tok):
            return False
        # 未登录
        if path.startswith("/api/"):
            self.send_json({"error": "需要登录", "needLogin": True}, 401)
        else:
            self.send_response(302)
            self.send_header("Location", "/login")
            self.send_header("Content-Length", "0")
            self.end_headers()
        return True

    def _reject_if_blocked(self):
        """返回 True 表示已处理（调用方应直接 return）"""
        if not self._is_blocked():
            return False
        # 用户要求：黑名单设备的「所有请求」都要忽略 —— 所以直接关连接，
        # 连错误信息都不给（避免被探测到服务存在）
        try:
            self.close_connection = True
        except Exception:
            pass
        return True

    def _origin_ok(self):
        """校验 Origin。之前是一张死白名单（只含 127.0.0.1 / localhost），
        局域网设备访问时 Origin 是 http://<LAN_IP>:9000，永远匹配不上 →
        页面能打开但所有 /api/* 返回 403 {"error":"origin not allowed"}，
        手机登录后也是一片空白、什么都加载不出来。

        正确的 CSRF 判据不是"白名单里的主机"，而是"Origin 的 host 必须等于
        本次请求来源 IP（或本机地址）"—— 跨站请求的 Origin 一定来自别的 host，
        同源请求的 Origin 必然与 Host 一致。"""
        o = self._origin()
        if not o:
            return True                      # curl / 同源导航 / 老浏览器
        if o in self.ALLOWED_ORIGINS:
            return True
        try:
            from urllib.parse import urlparse as _up
            oh = (_up(o).hostname or "").lower()
            if not oh:
                return False
            # 1) Origin 的 host == 本次请求的来源 IP（局域网设备走这条）
            try:
                cip = (self.client_address[0] if self.client_address else "").lower()
            except Exception:
                cip = ""
            if cip and oh == cip:
                return True
            # 2) Origin 的 host == 本机 LAN 地址（有些浏览器/代理会这样填）
            if oh == (_local_ip() or "").lower():
                return True
            # 3) Origin 的 host == 请求头 Host 的 host（最标准的同源判断）
            try:
                hh = (_up("http://" + (self.headers.get("Host") or "")).hostname or "").lower()
            except Exception:
                hh = ""
            if hh and oh == hh:
                return True
        except Exception:
            pass
        return False

    def _security_headers(self):
        """统一安全响应头（防点击劫持、MIME 嗅探、referrer 泄露）"""
        try:
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "SAMEORIGIN")
            self.send_header("Referrer-Policy", "same-origin")
            self.send_header("X-XSS-Protection", "1; mode=block")
        except Exception:
            pass

    def send_json(self, obj, code=200):
        b = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store")
        self._security_headers()
        o = self._origin()
        if o and self._origin_ok():          # 只有明确带 Origin 且通过，才回显
            self.send_header("Access-Control-Allow-Origin", o)
            self.send_header("Vary", "Origin")
        self.end_headers()
        self.wfile.write(b)

    def cors(self):
        o = self._origin()
        if o and self._origin_ok():
            self.send_header("Access-Control-Allow-Origin", o)
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")

    # ---------- routes ----------
    def do_OPTIONS(self):
        if self._reject_if_blocked():
            return
        try:
            note_peer(self.client_address[0], self.headers.get("User-Agent"))
        except Exception:
            pass
        self.send_response(204)
        self.cors()
        self.end_headers()

    def do_HEAD(self):
        """HEAD 是 HTTP 标准方法，浏览器预取 / 扩展探测 / 某些客户端会发。
        原来没实现 → BaseHTTPRequestHandler 回 501 Unsupported method，
        用户在网络面板里就看到"明明成功却有个 501"。"""
        try:
            if self._reject_if_blocked():
                return
            path = self.path.split("?")[0]
            if self._reject_if_lan_off():
                return
            if self._reject_if_unauthed(path):
                return
            if self._reject_if_lan_manage(path):
                return
            if self._reject_if_forbidden(path):
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", "0")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
        except Exception:
            pass

    def _method_not_allowed(self):
        """PUT/DELETE/PATCH 等：明确回 405（比 501 语义正确）"""
        try:
            self.send_response(405)
            self.send_header("Allow", "GET, HEAD, POST, OPTIONS")
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", "0")
            self.end_headers()
        except Exception:
            pass

    def do_PUT(self):     self._method_not_allowed()
    def do_DELETE(self):  self._method_not_allowed()
    def do_PATCH(self):   self._method_not_allowed()
    def do_TRACE(self):   self._method_not_allowed()

    def do_GET(self):
        if self._reject_if_blocked():
            return
        try:
            note_peer(self.client_address[0], self.headers.get("User-Agent"))
        except Exception:
            pass
        path = self.path.split("?")[0]
        # 鉴权 + 局域网白名单：GET/POST 两个入口都必须有
        if self._reject_if_lan_off():
            return
        if self._reject_if_unauthed(path):
            return
        if self._reject_if_forbidden(path):
            return
        # 读接口也要校验来源：/api/* 可能泄露模型路径与日志
        if path.startswith("/api/") and not self._origin_ok():
            self.send_json({"error": "origin not allowed"}, 403)
            return
        if path == "/login":
            self._send_login_page()
            return

        if path in ("/", "/index.html"):
            fp = os.path.join(HERE, "index.html")
            if os.path.isfile(fp):
                data = open(fp, "rb").read()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                # ★ 必须显式 no-store：不发的话浏览器会启发式缓存页面，
                #   改完 index.html 刷新看到的还是旧版（本人踩过：以为改动没生效）
                self.send_header("Cache-Control", "no-store, must-revalidate")
                self.cors()
                self.end_headers()
                self.wfile.write(data)
            else:
                self.send_json({"error": "index.html 不存在"}, 404)
            return
        if path == "/v1/models":
            # 给 DSH 之类的 OpenAI 客户端做模型发现（返回真实可用的 gguf）
            data = []
            for m in find_models():
                data.append({
                    "id": m["id"],
                    "object": "model",
                    "owned_by": "lm-lite",
                    "created": 0,
                })
            self.send_json({"object": "list", "data": data})
            return

        if path == "/api/model-caps":
            # 探测当前模型的能力（是 chat_template 说话，不是我们猜）
            caps = {"loaded": False, "supportsThinking": False,
                    "supportsReasoningEffort": False, "model": None, "why": ""}
            if backend_alive():
                try:
                    with urllib.request.urlopen(
                            "http://127.0.0.1:%d/props" % LLAMA_PORT, timeout=8) as r:
                        pr = json.loads(r.read().decode("utf-8", "ignore"))
                    tpl = pr.get("chat_template") or ""
                    tpl_caps = pr.get("chat_template_caps") or {}
                    caps["loaded"] = True
                    caps["model"] = os.path.basename(pr.get("model_alias") or STATE.get("model") or "")
                    # 1) 模板里真有 enable_thinking 变量
                    has_var = "enable_thinking" in tpl
                    # 2) 或者有显式的思考标记
                    has_mark = ("<|think" in tpl) or ("thought" in tpl and "reasoning" in tpl)
                    caps["supportsThinking"] = bool(has_var or has_mark)
                    caps["supportsReasoningEffort"] = bool(tpl_caps.get("supports_reasoning_effort"))
                    if caps["supportsThinking"]:
                        caps["why"] = "模板含 enable_thinking" if has_var else "模板含思考标记"
                    else:
                        caps["why"] = "该模型的 chat template 没有思考开关，无法切换"
                except Exception as e:
                    caps["why"] = "读取 /props 失败: %s" % e
            else:
                caps["why"] = "没有加载模型"
            self.send_json(caps)
            return

        if path == "/api/gguf-meta":
            # GGUF 头部真实元数据：训练上下文长度 + KV 每 token 占用（前端选 ctx 用）
            qs = self.path.split("?", 1)
            mp = ""
            if len(qs) > 1:
                for kv in qs[1].split("&"):
                    if kv.startswith("path="):
                        try:
                            mp = urllib.parse.unquote(kv[5:])
                        except Exception:
                            mp = kv[5:]
            if not mp or not os.path.isfile(mp):
                self.send_json({"ok": False, "error": "模型路径无效"}, 400)
                return
            self.send_json(read_gguf_meta(mp) or {"ok": False, "error": "无法读取"})
            return

        if path == "/api/prompts":
            # 欢迎页快捷问题：读项目根目录 prompts.txt（一行一条，# 注释/空行跳过）。
            # 按 mtime 缓存——改文件即时生效，不用重启
            fp = os.path.join(HERE, "prompts.txt")
            lines = []
            try:
                mt = os.path.getmtime(fp)
                c = _PROMPTS_CACHE["v"]
                if c and c[0] == mt:
                    lines = c[1]
                else:
                    with open(fp, encoding="utf-8-sig") as f:   # 去 BOM（DSH 提醒的坑）
                        for ln in f:
                            ln = ln.strip()
                            if not ln or ln.startswith("#"):
                                continue
                            lines.append(ln[:200])
                    lines = lines[:30]
                    _PROMPTS_CACHE["v"] = (mt, lines)
            except Exception:
                lines = []
            self.send_json({"lines": lines})
            return

        if path == "/api/api-cfg":
            with _API_CFG_LOCK:
                cfg = dict(API_CFG)
            self.send_json({"ok": True, "cfg": cfg})
            return

        if path == "/api/models":
            self.send_json({"models": find_models(), "current": STATE["model"],
                            "dirs": MODEL_DIRS})
            return

        if path == "/api/browse":
            # 简易目录浏览：给「从资源管理器选 GGUF」用（浏览器拿不到本地绝对路径，
            # 只能由网关列目录、前端呈现）
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            target = (qs.get("path") or [""])[0].strip()

            # 磁盘浏览只对 admin 开放：本机天然 admin；局域网必须已登录 admin 角色
            # （_reject_if_forbidden 已把 /api/browse 列进 ADMIN_ONLY，这里只需
            #   确认调用者身份，不再写死"非本机一律拒绝"——那样局域网管理员
            #   就没法用文件选择器挑模型了）。
            try:
                _ip = self.client_address[0]
            except Exception:
                _ip = ""
            _bu, _br, _ = self._current_user()
            _force = os.environ.get("LM_LITE_TEST_AS_REMOTE", "").strip() == "1"
            if not (_is_local_ip(_ip) and not _force) and _br != "admin":
                self.send_json({"error": "浏览本地磁盘需要管理员权限"}, 403)
                return

            if not target:
                drives = []
                for letter in "CDEFGHIJKLMNOPQRSTUVWXYZ":
                    dp = letter + ":\\"
                    if os.path.isdir(dp):
                        drives.append({"name": dp, "path": dp})
                self.send_json({"path": "", "parent": None,
                                "dirs": drives, "files": []})
                return

            target = os.path.abspath(target)
            # 浏览本地磁盘只允许本机（上面已挡），这里再留一条审计：
            # 万一真的有人在物理接触这台机器时乱翻，事后能从 audit.log 看到。
            try:
                _cu2, _cr2, _ = self._current_user()
                audit("浏览目录 %s" % target, _cu2 or "本机")
            except Exception:
                pass
            if not os.path.isdir(target):
                self.send_json({"error": "目录不存在: " + target}, 400)
                return

            dirs, files = [], []
            try:
                for name in sorted(os.listdir(target), key=lambda x: x.lower()):
                    fp = os.path.join(target, name)
                    if os.path.isdir(fp):
                        dirs.append({"name": name, "path": fp})
                    elif name.lower().endswith(".gguf"):
                        try:
                            sz = os.path.getsize(fp)
                        except OSError:
                            sz = 0
                        files.append({"name": name, "path": fp,
                                      "sizeGB": round(sz / (1024 ** 3), 2)})
            except PermissionError:
                self.send_json({"error": "没有权限访问这个目录"}, 403)
                return
            except Exception as ex:
                self.send_json({"error": str(ex)[:200]}, 400)
                return

            parent = os.path.dirname(target.rstrip("\\"))
            if parent == target.rstrip("\\"):
                parent = ""      # 已经在根
            self.send_json({"path": target, "parent": parent,
                            "dirs": dirs, "files": files})
            return
        if path == "/api/slots":
            # 代理 llama-server 的 /slots，拿缓存命中统计
            try:
                with urllib.request.urlopen(
                        "http://127.0.0.1:%d/slots" % LLAMA_PORT, timeout=6) as r:
                    data = json.loads(r.read().decode("utf-8", "ignore"))
                out = []
                for s in (data if isinstance(data, list) else [data]):
                    if not isinstance(s, dict):
                        continue
                    out.append({
                        "id": s.get("id"),
                        "n_ctx": s.get("n_ctx"),
                        "promptTokens": s.get("n_prompt_tokens"),
                        "cacheTokens": s.get("n_prompt_tokens_cache"),
                        "processedTokens": s.get("n_prompt_tokens_processed"),
                        "processing": s.get("is_processing"),
                    })
                self.send_json({"slots": out})
            except Exception as e:
                self.send_json({"slots": [], "error": str(e)})
            return

        if path == "/api/vram":
            # 显存估算：模型文件 + KV（按实际 kv 精度估，别再忽略 kv）
            KV_BYTES = {"f16": 2.0, "q8_0": 1.0625, "q5_1": 0.75, "q4_0": 0.5625}
            info = {"loaded": backend_alive(), "model": STATE["model"],
                    "fileMB": 0, "vramMB": 0, "source": "estimate", "kv": STATE.get("kv")}
            if STATE["model"]:
                try:
                    info["fileMB"] = round(os.path.getsize(STATE["model"]) / (1024 ** 2))
                except Exception:
                    pass
            # KV 按模型架构估：1.7B=14 KiB/token(f16)，4B=36 KiB/token(f16)
            # 每 token KV：优先 GGUF 头部真实元数据（含混合注意力修正），
            # 读不到才按文件名猜（旧行为，保留兜底）
            meta = read_gguf_meta(STATE["model"]) if STATE["model"] else None
            if meta and meta.get("ok") and meta.get("perTokKB"):
                base_kb, ctx_src = meta["perTokKB"], "gguf-meta"
            else:
                name = (STATE["model"] or "").lower()
                base_kb = 36.0 if "4b" in name or "4-b" in name else 14.0
                ctx_src = "guess"
            per_tok_kb = base_kb * KV_BYTES.get(STATE.get("kv") or "f16", 2.0) / 2.0
            info["perTokKB"] = base_kb
            info["ctxSource"] = ctx_src
            # KV 按【总池】估：ctx 是每会话上下文，并发 N 时总池 = ctx × N
            _p = max(1, int(STATE.get("parallel") or 1))
            kv_mb = (STATE.get("ctx") or 0) * _p * per_tok_kb / 1024
            info["parallel"] = _p
            info["ctxTotal"] = (STATE.get("ctx") or 0) * _p
            info["kvMB"] = round(kv_mb)
            info["baseKbPerTok"] = base_kb
            info["modelVramMB"] = round(info["fileMB"] + kv_mb * 1.15 + 200)
            # 整卡占用（所有进程，含桌面/浏览器），和"模型占用"分开报
            gpu_used, gpu_total, gpu_model = read_gpu_memory()
            info["gpuUsedMB"] = gpu_used
            info["gpuTotalMB"] = gpu_total
            info["modelRealMB"] = gpu_model          # llama-server 的真实进程显存
            info["otherMB"] = (gpu_used - gpu_model) if (gpu_used is not None and gpu_model is not None) else None
            info["vramMB"] = info["modelVramMB"]     # 估算值，兼容旧字段
            self.send_json(info)
            return

        if path == "/api/queue":
            with _REQ_LOCK:
                items = [dict(x) for x in REQLOG]
            now = time.time()
            for x in items:
                x["elapsed"] = round((x["t1"] or now) - x["t0"], 2)
            running = sum(1 for x in items if x["status"] == "running")
            done_n = sum(1 for x in items if x["status"] == "done")
            err_n  = sum(1 for x in items if x["status"] == "error")
            # 近 40 条的平均值（给右栏「健康概览」用）
            ttfts = [x["ttft"] for x in items if x.get("ttft")]
            tpss  = [x["tps"]  for x in items if x.get("tps")]
            ins   = [x["promptN"] for x in items if x.get("promptN")]
            outs  = [x["outN"]    for x in items if x.get("outN")]
            avg = (lambda a: round(sum(a) / len(a), 1) if a else None)
            self.send_json({
                "items": items,
                "running": running,
                "parallel": int(STATE.get("parallel", 1)),
                "done": done_n,
                "error": err_n,
                "total": len(items),
                "avgTtft": avg(ttfts),
                "avgTps":  avg(tpss),
                "avgIn":   avg(ins),
                "avgOut":  avg(outs),
                "slowest": max((x["elapsed"] for x in items), default=None),
            })
            return

        if path == "/api/peers":
            with _PEER_LOCK:
                peers = [{"ip": k, "count": v["count"],
                          "first": v["first"], "last": v["last"],
                          "ua": v.get("ua", ""), "blocked": (k in BLOCKLIST)}
                         for k, v in sorted(PEERS.items(), key=lambda x: -x[1]["last"])]
            me = ""
            try:
                me = self.client_address[0]
            except Exception:
                pass
            with _BLOCK_LOCK:
                _blk = sorted(BLOCKLIST)
            # ★ 黑名单是落盘的、PEERS 是内存的：网关重启后已拉黑的 IP 不在 peers 里，
            #   界面上就没法解黑。这里把「黑名单里但没有访问记录」的补进去。
            known = set(x["ip"] for x in peers)
            for b_ip in _blk:
                if b_ip not in known:
                    peers.append({"ip": b_ip, "count": 0, "first": None, "last": None,
                                  "ua": "", "blocked": True})
            self.send_json({"peers": peers, "blocked": _blk,
                            "lan": LAN_ENABLED[0], "self": me,
                            "localIp": _local_ip(), "port": GATEWAY_PORT})
            return

        if path == "/api/audit":
            _cu0, _cr0, _ = self._current_user()
            if not _cr0:
                try:
                    _ip1 = self.client_address[0] if self.client_address else ""
                except Exception:
                    _ip1 = ""
                _cr0 = "admin" if _is_local_ip(_ip1) else "user"
            since = 0
            try:
                since = int(urllib.parse.parse_qs(
                    urllib.parse.urlparse(self.path).query).get("since", ["0"])[0])
            except Exception:
                since = 0
            with _AUDIT_LOCK:
                items = list(_AUDIT)
            # admin 看全部；普通用户只看条数（不泄露别人的操作细节）
            if _cr0 != "admin":
                items = [{"ts": x["ts"], "who": "", "what": "管理员操作"} for x in items[-20:]]
            self.send_json({"items": items[since:], "total": len(items)})
            return

        if path == "/api/logout":
            _u5, _r5, _t5 = self._current_user()
            if _t5:
                _drop_session(_t5)
            payload = json.dumps({"ok": True}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            # 立刻过期 cookie（HttpOnly，必须由服务端清）
            self.send_header("Set-Cookie",
                             "lml_tok=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0")
            self.end_headers()
            self.wfile.write(payload)
            return

        if path == "/api/me":
            _u, _r, _t = self._current_user()
            try:
                _ip0 = self.client_address[0] if self.client_address else ""
            except Exception:
                _ip0 = ""
            _local = _is_local_ip(_ip0)
            # ★ 本机 = 超级管理员，与登录了哪个账号无关。
            #   原来只有"未登录"才给 admin；一旦在本机用 guest 登录，
            #   /api/me 就返回 user，前端把模型管理等入口全藏了，
            #   用户感觉"原本的超管权限没了"。物理接触这台机器的人
            #   本来就该有全部权限（密码只用于拦局域网设备）。
            if _local:
                _u = "admin"
                _r = "admin"
            elif not _u:
                _u = "guest"
                _r = "user"
            self.send_json({"user": _u, "role": _r,
                            "authEnabled": auth_required(),
                            "local": _local})
            return

        if path == "/api/auth_status":
            us = AUTH.get("users") or {}
            self.send_json({
                "required": auth_required(),
                "enabled": bool(AUTH.get("enabled")),
                # 兼容旧字段：user/hasPassword 取第一个用户
                "user": next(iter(us.keys()), ""),
                "hasPassword": any(v.get("hash") for v in us.values() if isinstance(v, dict)),
                # 新结构：用户清单（不含 hash/salt）
                "users": [{"user": k, "role": v.get("role") or "user",
                           "hasPassword": bool(v.get("hash"))}
                          for k, v in us.items() if isinstance(v, dict)]
            })
            return

        if path == "/api/status":
            # ★ 外部把 llama-server 杀了（崩溃 / 任务管理器结束 / LM Studio 抢端口），
            #   STATE 里还留着 model/ctx → 顶栏继续显示模型名、模型页继续显示卡片和
            #   "卸载"按钮，用户看到的是"明明没模型了却还说有"。这里自愈：检测到
            #   后端不在、且当前并没有在加载中，就把残留状态清掉。
            _alive_now = backend_alive()
            if not _alive_now and not STATE["loading"] and STATE["model"]:
                STATE["model"] = None
                STATE["ctx"] = 0
                STATE["mismatch"] = ""
                STATE["last_error"] = STATE.get("last_error") or "推理后端已不在运行（可能被外部关闭或崩溃）"
                log_warn("检测到后端已退出，已清理残留的模型状态")
            self.send_json({
                "alive": _alive_now,
                "model": STATE["model"],
                "ctx": STATE["ctx"],

                "ctxTotal": (STATE["ctx"] or 0) * max(1, int(STATE.get("parallel") or 1)),
                "kv": STATE.get("kv"),
                "loading": STATE["loading"],
                "parallel": STATE.get("parallel", 1),
                "mismatch": STATE.get("mismatch") or "",   # 请求的模型与实际加载的不一致时非空
                "loadProgress": load_progress() if STATE["loading"] else (100 if backend_alive() else None),
                "error": STATE["last_error"],
                "port": LLAMA_PORT,
            })
            return
        if path == "/api/stats":
            tt = STATS["ttftList"][-60:]
            tp = STATS["tpsList"][-60:]
            pp = STATS["ppList"][-60:]
            hl = STATS["hitList"][-60:]
            avg = lambda a: round(sum(a)/len(a), 1) if a else 0
            totCache = STATS["totalCacheN"]; totPrompt = STATS["totalPromptN"]
            self.send_json({
                "requests": STATS["requests"],
                "webRequests": STATS["webRequests"],
                "inflight": STATS["inflight"],
                "totalIn": STATS["totalIn"], "totalOut": STATS["totalOut"],
                "totalGenMs": STATS["totalGenMs"], "totalPromptMs": STATS["totalPromptMs"],
                "avgTtft": avg(tt), "avgTps": avg(tp),
                "avgPp": avg(pp),
                # 命中率 = 命中 / (命中 + 计算)，不是 命中 / 计算
                "hitRate": round(totCache/(totCache+totPrompt)*100, 1) if (totCache+totPrompt) else 0,
                "totalCacheN": totCache, "totalPromptN": totPrompt,
                "ttftSeries": tt, "tpsSeries": tp, "ppSeries": pp, "hitSeries": hl,
                "last": STATS["last"],
            })
            return
        if path == "/api/log":
            # 用【字节偏移】做游标（行号在追加日志时会漂移，天然不可靠）
            n = 200
            after = None
            fp = os.path.join(HERE, "backend.log")
            try:
                qs = self.path.split("?", 1)
                if len(qs) > 1:
                    for kv in qs[1].split("&"):
                        if kv.startswith("n="):
                            n = max(1, min(2000, int(kv[2:])))
                        elif kv.startswith("after="):
                            after = int(kv[6:])
            except Exception:
                pass
            lines, cursor, size = [], 0, 0
            if os.path.isfile(fp):
                try:
                    with open(fp, "rb") as fb:
                        size = fb.seek(0, 2)
                        if after is not None and 0 <= after <= size:
                            fb.seek(after)                       # 只读新增部分
                            data = fb.read(1024 * 1024).decode("utf-8", "ignore")
                            lines = data.splitlines()
                        else:
                            start = max(0, size - 256 * 1024)    # 首次：读尾部
                            fb.seek(start)
                            data = fb.read().decode("utf-8", "ignore")
                            lines = data.splitlines()[-n:]
                        cursor = size
                except Exception:
                    pass
            self.send_json({"lines": [x.rstrip() for x in lines],
                            "cursor": cursor, "size": size})
            return
        self.send_json({"error": "not found"}, 404)

    def _read_body(self):
        """读请求体。★ 必须有长度上限：没有上限时 Content-Length: 4e9
        会一路 read 直到 MemoryError / 被 OOM 干掉；chunked 同理要累计检查。"""
        MAX_BODY = 32 * 1024 * 1024          # 32MB，足够任何对话请求
        try:
            te = (self.headers.get("Transfer-Encoding") or "").lower()
            if "chunked" in te:
                chunks = []
                total = 0
                while True:
                    line = self.rfile.readline(65536).strip()
                    if not line:
                        break
                    try:
                        size = int(line.split(b";")[0], 16)
                    except Exception:
                        break
                    if size == 0:
                        self.rfile.readline(65536)
                        break
                    total += size
                    if total > MAX_BODY:
                        raise ValueError("chunked 请求体超过上限 %d" % MAX_BODY)
                    chunks.append(self.rfile.read(size))
                    self.rfile.readline(65536)
                return b"".join(chunks)

            try:
                n = int(self.headers.get("Content-Length") or 0)
            except (TypeError, ValueError):
                n = 0
            if n < 0:
                n = 0
            if n > MAX_BODY:
                raise ValueError("请求体过大（%d 字节，上限 %d）" % (n, MAX_BODY))
            return self.rfile.read(n) if n > 0 else b""
        except ValueError:
            raise
        except Exception:
            return b""


    def do_POST(self):
        if self._reject_if_blocked():
            return
        try:
            note_peer(self.client_address[0], self.headers.get("User-Agent"))
        except Exception:
            pass
        path = self.path.split("?")[0]
        # 鉴权 + 局域网白名单：GET/POST 两个入口都必须有
        if self._reject_if_lan_off():
            return
        if self._reject_if_unauthed(path):
            return
        if self._reject_if_forbidden(path):
            return
        # 写操作必须校验来源（/api/unload 是简单请求，浏览器不发预检）
        if not self._origin_ok():
            self.send_json({"ok": False, "error": "origin not allowed"}, 403)
            return
        body = self._read_body()

        if path == "/api/stop":
            # 只停【页面自己发起】的请求：按钮是页面上的，不该误杀 DSH 等外部流。
            # 原来只有一个全局槽位，并发时按下停止会把别人的生成掐死。
            stopped = 0
            for rid, info in _active_snapshot():
                if not info.get("is_web"):
                    continue
                r = info.get("resp")
                if r is not None:
                    try:
                        r.close()
                        stopped += 1
                    except Exception:
                        pass
                _active_unregister(rid)
            if stopped:
                try:
                    _abort_upstream()
                except Exception:
                    pass
                self.send_json({"ok": True, "stopped": True, "count": stopped})
            else:
                self.send_json({"ok": True, "stopped": False,
                                "note": "没有正在进行的页面生成"})
            return

        if path == "/api/unload":
            try:
                _cu4, _cr4, _ = self._current_user()
                audit("卸载模型 %s" % (os.path.basename(STATE.get("model") or "?"),),
                      _cu4 or "本机")
            except Exception:
                pass
            stop_backend()
            STATE["model"] = None
            STATE["ctx"] = 0
            STATE["kv"] = "q8_0"
            STATE["loading"] = False
            _ALIVE_CACHE["t"] = 0.0
            STATE["last_error"] = ""
            # 保留 last.json：卸载是临时的，重启网关应当恢复到上次配置
            # （想彻底不加载就删掉这个文件或改内容）
            self.send_json({"ok": True})
            return

        if path == "/api/login":
            try:
                req = json.loads(body or b"{}")
            except Exception:
                req = {}
            user = str(req.get("user") or "").strip()
            pw = str(req.get("password") or "")
            try:
                _cip = self.client_address[0] if self.client_address else ""
            except Exception:
                _cip = ""
            # ★ 本机登录：无论输入什么账号，一律按超级管理员处理
            if _is_local_ip(_cip):
                _login_ok(_cip)
                _tok_local = _new_session("admin", "admin", _cip)
                self.send_json({"ok": True, "token": _tok_local,
                                "user": "admin", "role": "admin",
                                "note": "本机访问自动获得管理员权限"})
                return
            if not auth_required():
                # 未开启登录保护：局域网访问按 guest 处理（只读+聊天）
                _role = "admin" if _is_local_ip(_cip) else "user"
                self.send_json({"ok": True,
                                "token": _new_session("admin" if _role == "admin" else "guest",
                                                      _role, _cip),
                                "user": "admin" if _role == "admin" else "guest",
                                "role": _role,
                                "note": "未开启登录保护"})
                return
            # 先查是否被锁（防字典暴破）
            _wait = _login_blocked(_cip)
            if _wait > 0:
                self.send_json({"ok": False,
                                "error": "尝试过于频繁，请 %d 秒后再试" % _wait}, 429)
                return
            # 统一错误信息（不泄露"用户名是否存在"）
            _bad = {"ok": False, "error": "用户名或密码错误"}
            nm, uinfo = find_user(user)
            if not uinfo or not uinfo.get("hash") or not uinfo.get("salt"):
                _login_fail(_cip)
                time.sleep(0.25)          # 抹平时间差，防用户名枚举
                self.send_json(_bad, 401)
                return
            try:
                calc = _hash_pw(pw, uinfo["salt"],
                                int(uinfo.get("iter") or AUTH.get("iter") or 120000))
            except Exception:
                calc = ""
            if not calc or not hmac.compare_digest(calc, uinfo["hash"]):
                _login_fail(_cip)
                time.sleep(0.25)
                self.send_json(_bad, 401)
                return
            role = str(uinfo.get("role") or "user")
            if role not in ("admin", "user"):
                role = "user"
            _login_ok(_cip)
            tok = _new_session(nm, role, _cip)
            # 同时下发 cookie（浏览器自动带）和 token（前端可存）
            payload = json.dumps({"ok": True, "token": tok,
                                  "user": nm, "role": role}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Set-Cookie",
                             "lml_tok=%s; Path=/; HttpOnly; SameSite=Lax; Max-Age=%d"
                             % (tok, 7 * 24 * 3600))
            self.end_headers()
            self.wfile.write(payload)
            log_warn("登录成功: user=%s ip=%s" % (user, self.client_address[0]))
            return

        if path == "/api/auth_config":
            # ★ 双角色用户管理：只有 admin 能进来（由 _reject_if_forbidden 保证）
            try:
                req = json.loads(body or b"{}")
            except Exception:
                req = {}
            act = str(req.get("action") or "set")
            _cu, _cr, _ct = self._current_user()
            try:
                _cip2 = self.client_address[0] if self.client_address else ""
            except Exception:
                _cip2 = ""
            _is_local = _is_local_ip(_cip2)
            if _is_local and not _cu:
                _cu, _cr = "admin", "admin"

            if act == "disable":
                AUTH["enabled"] = False
                _save_auth()
                audit("关闭登录保护", _cu)
                log_warn("登录保护已关闭")
                self.send_json({"ok": True, "enabled": False})
                return

            if act == "enable":
                us = AUTH.get("users") or {}
                if not any((v.get("hash") and v.get("salt")) for v in us.values()):
                    self.send_json({"ok": False,
                                    "error": "还没有任何用户设置过密码，无法开启"}, 400)
                    return
                AUTH["enabled"] = True
                _save_auth()
                audit("开启登录保护", _cu)
                self.send_json({"ok": True, "enabled": True})
                return

            if act == "list":
                us = AUTH.get("users") or {}
                self.send_json({"ok": True, "enabled": bool(AUTH.get("enabled")),
                                "users": [{"user": k,
                                           "role": v.get("role") or "user",
                                           "hasPassword": bool(v.get("hash"))}
                                          for k, v in us.items()]})
                return

            if act == "adduser":
                user = str(req.get("user") or "").strip()
                pw = str(req.get("password") or "")
                role = str(req.get("role") or "user").strip()
                if role not in ("admin", "user"):
                    role = "user"
                if not user or len(pw) < 6:
                    self.send_json({"ok": False,
                                    "error": "用户名不能为空，密码至少 6 位"}, 400)
                    return
                if not re.match(r'^[A-Za-z0-9_.-]{1,32}$', user):
                    self.send_json({"ok": False,
                                    "error": "用户名只能含字母数字下划线点横线，最长 32"}, 400)
                    return
                us = AUTH.setdefault("users", {})
                if user in us:
                    self.send_json({"ok": False, "error": "该用户已存在"}, 409)
                    return
                import secrets
                salt = secrets.token_hex(16)
                us[user] = {"salt": salt, "hash": _hash_pw(pw, salt, 120000),
                            "iter": 120000, "role": role}
                _save_auth()
                audit("新增用户 %s（%s）" % (user, role), _cu)
                self.send_json({"ok": True, "user": user, "role": role})
                return

            if act == "deluser":
                user = str(req.get("user") or "").strip()
                us = AUTH.get("users") or {}
                if user not in us:
                    self.send_json({"ok": False, "error": "该用户不存在"}, 404)
                    return
                if user == _cu:
                    self.send_json({"ok": False, "error": "不能删除当前登录的自己"}, 400)
                    return
                if len(us) <= 1:
                    self.send_json({"ok": False, "error": "至少保留一个用户"}, 400)
                    return
                # 不能把最后一个 admin 删掉，否则再也没人能管理
                if (us[user].get("role") == "admin"
                        and sum(1 for v in us.values() if v.get("role") == "admin") <= 1):
                    self.send_json({"ok": False,
                                    "error": "至少要保留一个 admin 用户"}, 400)
                    return
                us.pop(user, None)
                _save_auth()
                audit("删除用户 %s" % user, _cu)
                self.send_json({"ok": True})
                return

            # 默认动作 set：给自己或（admin）给指定用户设密码
            user = str(req.get("user") or _cu or "").strip()
            pw = str(req.get("password") or "")
            role = str(req.get("role") or "").strip()
            if not user or len(pw) < 6:
                self.send_json({"ok": False,
                                "error": "用户名不能为空，密码至少 6 位"}, 400)
                return
            # 非 admin 只能改自己的密码
            if _cr != "admin" and not _is_local and user != _cu:
                self.send_json({"ok": False, "error": "只能修改自己的密码"}, 403)
                return
            us = AUTH.setdefault("users", {})
            exist = us.get(user)
            import secrets
            salt = secrets.token_hex(16)
            new_role = role if (role in ("admin", "user") and (_cr == "admin" or _is_local)) else \
                ((exist or {}).get("role") or "user")
            # 首个用户自动给 admin
            if not exist and not us:
                new_role = "admin"
            us[user] = {"salt": salt, "hash": _hash_pw(pw, salt, 120000),
                        "iter": 120000, "role": new_role}
            AUTH["enabled"] = True
            _save_auth()
            audit("%s 用户 %s 的密码" % ("设置" if not exist else "修改", user), _cu)
            self.send_json({"ok": True, "user": user, "role": new_role,
                            "enabled": True})
            return

        if path == "/api/lan_config":
            # 局域网开关：写进 config.json，提示需要重启
            try:
                req = json.loads(body or b"{}")
            except Exception:
                req = {}
            want = bool(req.get("enabled"))
            cp = os.path.join(HERE, "config.json")
            cfg = {}
            try:
                if os.path.isfile(cp):
                    with open(cp, encoding="utf-8-sig") as fp:
                        cfg = json.load(fp) or {}
            except Exception:
                cfg = {}
            cfg["lan"] = want
            try:
                with open(cp, "w", encoding="utf-8") as fp:
                    json.dump(cfg, fp, ensure_ascii=False, indent=1)
                ok = True
            except Exception:
                ok = False
            # ★ 热切换：立即改运行时开关，不用重启
            LAN_ENABLED[0] = want
            log_warn("局域网访问 %s（立即生效）" % ("已开启" if want else "已关闭"))
            self.send_json({"ok": ok, "lan": want, "needRestart": False,
                            "current": want, "localIp": _local_ip()})
            return

        if path == "/api/queue/clear":
            with _REQ_LOCK:
                cnt = len(REQLOG)
                REQLOG.clear()
            log_warn("清空推理队列记录 %d 条" % cnt)
            self.send_json({"ok": True, "cleared": cnt})
            return

        if path == "/api/block":
            try:
                req = json.loads(body or b"{}")
            except Exception:
                req = {}
            ip = str(req.get("ip") or "").strip()
            act = str(req.get("action") or "block").strip()
            if not ip:
                self.send_json({"ok": False, "error": "要给出 ip"}, 400)
                return
            # ★ 必须是合法 IPv4/IPv6：原来 {"ip":"not-an-ip;rm"} 也能写进黑名单文件
            try:
                import ipaddress as _ipmod
                ip = str(_ipmod.ip_address(ip))
            except Exception:
                self.send_json({"ok": False, "error": "不是合法的 IP 地址: %s" % ip[:40]}, 400)
                return
            # 防止把自己锁在外面
            if ip in ("127.0.0.1", "::1", "localhost"):
                self.send_json({"ok": False, "error": "不能拉黑本机地址"}, 400)
                return
            with _BLOCK_LOCK:
                if act == "unblock":
                    BLOCKLIST.discard(ip)
                else:
                    BLOCKLIST.add(ip)
                cur = sorted(BLOCKLIST)
                save_blocklist()      # ★ 放进锁内：save 里也要 sorted(BLOCKLIST)，
                                      #   在锁外会和另一个 add 撞成 "Set changed size"
            
            log_warn("黑名单 %s: %s（当前共 %d 个）" % (act, ip, len(cur)))
            self.send_json({"ok": True, "blocked": cur})
            return

        if path == "/api/load":
            try:
                req = json.loads(body or b"{}")
            except Exception:
                req = {}
            mp = req.get("path")
            # ★ ctx 必须夹取：原来直接 int() 就用，传 999999 也能过，
            #   超训练上下文不会立刻报错，要等满 240 秒超时才失败。
            try:
                ctx = int(req.get("ctx") or 8192)
            except Exception:
                ctx = 8192
            # ★ 上界要用这个模型真实的训练上下文（读 GGUF 头），不是写死的 1M ——
            #   否则传 999999 照样放行，超长上下文不会立刻报错，要等 240 秒超时。
            _cap = 1048576
            try:
                _meta = read_gguf_meta(mp) if mp else None
                if _meta and _meta.get("contextLength"):
                    _cap = min(_cap, int(_meta["contextLength"]))
            except Exception:
                pass
            ctx = max(512, min(ctx, _cap))
            kv = req.get("kv") or "f16"
            if kv not in ("f16", "q8_0", "q5_1"):
                kv = "f16"                        # 只接受这三个档位
            try:
                parallel = int(req.get("parallel") or STATE.get("parallel") or 1)
            except Exception:
                parallel = 1
            parallel = max(1, min(4, parallel))
            if not mp or not os.path.isfile(mp):
                self.send_json({"ok": False, "error": "模型路径无效"}, 400)
                return
            # ★ 这里才是真正的「加载模型」——必须在这记审计
            try:
                _cuL, _crL, _ = self._current_user()
                audit("加载模型 %s" % os.path.basename(mp), _cuL or "本机")
            except Exception:
                pass
            try:
                gpu_layers = req.get("gpu_layers")
                gpu_layers = None if gpu_layers in (None, "", 99, "99") else int(gpu_layers)
            except Exception:
                gpu_layers = None
            kv_offload = req.get("kv_offload")
            kv_offload = True if kv_offload is None else bool(kv_offload)
            try:
                cpu_moe = int(req.get("cpu_moe") or 0)
            except Exception:
                cpu_moe = 0
            def _num(key, dflt):
                try:
                    v = req.get(key)
                    return dflt if v in (None, "") else int(v)
                except Exception:
                    return dflt
            def _bool(key, dflt):
                v = req.get(key)
                return dflt if v is None else bool(v)
            # ★ 并发数会把 KV 缓存放大 N 倍（-c 现在是 ctx × parallel）。
            #   加载前先粗估一下，显存明显不够就直接拒绝，别等 llama-server OOM。
            try:
                _meta = read_gguf_meta(mp)
                _kvb = {"f16": 2.0, "q8_0": 1.0625, "q5_1": 0.75}.get(kv or "f16", 2.0)
                _base = (_meta.get("perTokKB") if (_meta and _meta.get("ok")) else None) or 14.0
                _per_tok_kb = _base * _kvb / 2.0
                _kv_mb = ctx * max(1, parallel) * _per_tok_kb / 1024
                _file_mb = os.path.getsize(mp) / (1024 ** 2)
                _need = _file_mb + _kv_mb * 1.15 + 200
                _total = 8192
                try:
                    _u, _t, _m = read_gpu_memory()
                    _total = _t or 8192
                except Exception:
                    pass
                if _need > _total * 0.95:
                    self.send_json({
                        "ok": False,
                        "error": "显存可能不够：估算需 %.0f MB（模型 %.0f + KV %.0f × 并发 %d），"
                                 "整卡只有 %d MB。请降低上下文、改用 q8_0，或把并发设为 1。"
                                 % (_need, _file_mb, _kv_mb, max(1, parallel), _total),
                        "needMB": int(_need), "totalMB": _total
                    }, 400)
                    return
            except Exception:
                pass

            # ★ 生成中切换会 terminate 后端、掐断正在流式的回复。
            #   /api/load 原来完全不经过 ensure_model 的等待逻辑，所以这条必须单独加。
            #   想强制切换就传 force=true。
            if _active_busy() and not bool(req.get("force")):
                _n = len(_active_snapshot())
                self.send_json({
                    "ok": False,
                    "error": "当前有 %d 个请求正在生成，切换模型会中断它们。"
                             "请先停止生成，或传 force=true 强制切换。" % _n,
                    "busy": True, "count": _n
                }, 409)
                return
            ok, err = start_backend(
                mp, ctx, kv, parallel, gpu_layers, kv_offload, cpu_moe,
                batch=_num("batch", 2048), ubatch=_num("ubatch", 1024),
                threads=_num("threads", 10),
                flash_attn=_bool("flash_attn", True),
                ctx_shift=_bool("ctx_shift", False),
                use_mmap=_bool("use_mmap", True),
                ctk=(req.get("ctk") or None),
                ctv=(req.get("ctv") or None),
                mlock=_bool("mlock", False),
                rope_scaling=(req.get("rope_scaling") or None),
                no_reasoning_preserve=_bool("no_reasoning_preserve", False))
            if ok:
                STATE["parallel"] = parallel
            self.send_json({"ok": ok, "error": err, "ctx": ctx, "kv": kv,
                            "parallel": parallel})
            return

        if path.startswith("/v1/"):
            # ★ 聊天限速：普通用户狂发请求会长期占满单槽后端和显存
            if path.startswith("/v1/chat") and auth_required():
                try:
                    _cip0 = self.client_address[0] if self.client_address else ""
                except Exception:
                    _cip0 = ""
                if not _is_local_ip(_cip0) and not _chat_allow(_cip0):
                    self.send_json({"error": {
                        "message": "请求过于频繁，请稍后再试（每分钟最多 %d 次）" % CHAT_MAX_PER_WIN,
                        "type": "rate_limited"}}, 429)
                    return
            # 自动匹配 + 拉起请求的模型（DSH 切模型时无需手动操作）
            if path.endswith("/chat/completions") or path.endswith("/completions"):
                # ★ 页面请求优先：只要 lm-lite 自己要模型，就把别的请求（DSH 等）掐掉
                is_web = bool(self.headers.get("X-LML-Web"))
                if is_web:
                    # ★ 只掐断【外部请求】（is_web=False），页面请求之间互不干扰。
                    #   现在按 rid 逐条判断，并发时不会误伤，也不会清错句柄。
                    killed = 0
                    for rid, info in _active_snapshot():
                        if info.get("is_web"):
                            continue
                        r = info.get("resp")
                        if r is not None:
                            try:
                                r.close()
                                killed += 1
                            except Exception:
                                pass
                        _active_unregister(rid)
                    if killed:
                        log_warn("页面请求优先：已掐断 %d 个外部请求" % killed)
                        try:
                            _abort_upstream()
                        except Exception:
                            pass
                try:
                    req = json.loads(body or b"{}")
                    want = req.get("model")

                    # ★ 思考开关统一由 enable_thinking 控制。
                    #   llama-server 的 chat_template_caps.supports_reasoning_effort = false，
                    #   所以调用方传的 reasoning_effort 它根本不认 —— 必须在网关翻译。
                    if "chat_template_kwargs" not in req:
                        re_ = req.get("reasoning_effort") or req.get("reasoning")
                        if re_ is None:
                            think = False                      # 默认关（1.7B 思考=烧 token）
                        elif str(re_).lower() in ("none", "off", "low", "minimal", ""):
                            think = False
                        elif str(re_).lower() in ("high", "medium", "default"):
                            think = True
                        else:
                            think = False
                        req["chat_template_kwargs"] = {"enable_thinking": think}
                        body = json.dumps(req, ensure_ascii=False).encode("utf-8")
                        log_warn("思考开关 → enable_thinking=%s（来自 reasoning_effort=%r）"
                                 % (think, re_))
                    # ★ API 默认值：调用方没指定的采样参数，用设置页「API 调用默认值」补上
                    req, injected = apply_api_defaults(req)
                    if injected:
                        body = json.dumps(req, ensure_ascii=False).encode("utf-8")
                        log_warn("API 默认值注入: %s（client=%r）"
                                 % (",".join(injected),
                                    (self.headers.get("User-Agent") or "")[:40]))
                    log_warn("v1 model=%r stream=%r web=%s client=%r 当前=%s"
                             % (want, req.get("stream"), is_web,
                                (self.headers.get("User-Agent") or "")[:60],
                                os.path.basename(STATE.get("model") or "")))
                    if want:
                        ok, note = ensure_model(want)
                        if not ok:
                            log_warn("切换失败 model=%r: %s" % (want, note))
                            # note 里可能有上层传下来的细节，但模型路径之类
                            # 不该回给调用方；保留简短原因即可
                            self.send_json({"error": {
                                "message": "模型切换失败，请检查模型是否可用",
                                "type": "model_load_failed"}}, 503)
                            return
                        if note == "已自动切换":
                            log_warn("自动切换模型: %s" % want)
                except Exception as e:
                    # ★ 绝不静默：不能让旧模型糊弄客户端
                    # 但也不能把内部异常原文回给调用方（可能含路径、字段名、
                    # 解析器提示如 "Expecting property name..."）——细节只进日志。
                    log_warn("v1 预处理异常: %r" % (e,))
                    self.send_json({"error": {
                        "message": "网关预处理失败（详情见网关日志）",
                        "type": "gateway_error"}}, 500)
                    return
            self.proxy(path, body)
            return

        if path == "/api/api-cfg":
            try:
                d = json.loads(body or b"{}")
            except Exception:
                d = {}
            cfg, err = api_cfg_validate(d)
            if err:
                self.send_json({"ok": False, "error": err}, 400)
                return
            try:
                save_api_cfg(cfg)
            except Exception as e:
                log_warn("保存 api 默认值失败: %r" % (e,))
                self.send_json({"ok": False,
                                "error": "保存失败（详情见网关日志）"}, 500)
                return
            self.send_json({"ok": True, "cfg": cfg})
            return

        if path == "/api/stats":
            # 前端上报一次请求的统计
            try:
                d = json.loads(body or b"{}")
            except Exception:
                d = {}
            # ★ 只有前端独有的指标才累加。
            # requests / totalIn / totalOut / totalGenMs / totalPromptMs / cacheN / promptN
            # 都已由网关转发时按服务端口径统计，这里再加会翻倍。
            # ★★ 这里【不再】把 ttft/tps/pp 追加进 Series。
            #   网关转发时 _record_from_probe 已经按服务端口径记过一遍，
            #   页面这次 POST 再记一次 → 曲线每个请求出两个点、均值被双倍加权，
            #   而 requests/totalIn 只算一次，两边口径互相矛盾（审查发现的 bug）。
            #   前端 POST 现在只用来补网关看不到的两项：思考首字、思考时长。
            old = STATS["last"] if isinstance(STATS["last"], dict) else {}
            if old:
                for k in ("thinkTtft", "thinkMs"):
                    if d.get(k) is not None:
                        old[k] = d[k]
                STATS["last"] = old
            self.send_json({"ok": True})
            return

        self.send_json({"error": "not found"}, 404)

    def proxy(self, path, body):
        """转发入口：计数「当前并发」后交给 _proxy_stream，异常路径也保证减回去"""
        if not backend_alive():
            self.send_json({"error": "后端未就绪，请先加载模型"}, 503)
            return
        with _INFLIGHT_LOCK:
            STATS["inflight"] += 1
        # 队列记录：页面来源标 page，其余（DSH 等）标 api
        _src = "page" if self.headers.get("X-LML-Web") else "api"
        _rec = req_start(_src)
        _ok = False
        _err_msg = ""
        self._stream_err = ""
        self._last_info = {}          # ★ 每次请求先清空，否则 keep-alive 复用同一 handler 时，
        self._stream_chars = 0        # 已转发的正文/思考字符数（停止时估算 token 用）
                                      #   _proxy_stream 提前 return（未就绪/HTTPError/连不上）
                                      #   会让这一笔沿用上一笔的 token/ttft 指标并被记成"已完成"
        try:
            self._proxy_stream(path, body)
            # ★ 流里补过错误帧（上游断流/用户停止）也算失败，不能记成"已完成"
            _ok = not self._stream_err
        except Exception as e:
            # ★ 原来 _err_msg 声明了却从没赋值 → 队列里错误原因永远显示"请求失败"
            _err_msg = repr(e)[:180]
        finally:
            with _INFLIGHT_LOCK:
                STATS["inflight"] = max(0, STATS["inflight"] - 1)
            try:
                _info = getattr(self, "_last_info", {}) or {}
                if not _ok:
                    _info = dict(_info)
                    _info["err"] = _err_msg or getattr(self, "_stream_err", "") or "请求失败"
                # ★ 停止/断流时后端不会返回 usage，outTok 是 0 —— 队列里会出现
                #   "已停止"却显示 0 token，与"保留已流出正文"的设计不一致。
                #   这里用已转发字符数按经验比（中文≈1.5 字/token）估算补上。
                if not _info.get("outTok"):
                    _ch = getattr(self, "_stream_chars", 0) or 0
                    if _ch > 0:
                        _info = dict(_info)
                        _info["outTok"] = max(1, int(_ch / 1.5))
                        _info["_estimated"] = True
                req_end(_rec, "done" if _ok else "error", _info)
            except Exception:
                pass

    def _proxy_stream(self, path, body):
        """把请求转发给 llama-server，流式原样回吐"""
        if not backend_alive():
            self.send_json({"error": "后端未就绪，请先加载模型"}, 503)
            return
        url = "http://127.0.0.1:%d%s" % (LLAMA_PORT, path)
        req = urllib.request.Request(url, data=body, headers={
            "Content-Type": "application/json",
            "Accept": self.headers.get("Accept", "*/*"),
        })
        try:
            resp = urllib.request.urlopen(req, timeout=1800)
        except urllib.error.HTTPError as e:
            self.send_json({"error": e.read().decode("utf-8", "ignore")[:500]}, e.code)
            self._stream_err = "上游返回 HTTP %s" % getattr(e, "code", "?")
            return
        except Exception as e:
            self.send_json({"error": str(e)}, 502)
            return

        self.send_response(resp.status)
        ct = resp.headers.get("Content-Type", "application/json")
        self.send_header("Content-Type", ct)
        self.cors()
        self.send_header("Cache-Control", "no-cache")
        # ★ 流式响应无 Content-Length，HTTP/1.1 下必须显式关闭连接
        self.send_header("Connection", "close")
        self.close_connection = True
        self.end_headers()
        # 登记到活动表：让 /api/stop 和"页面请求优先"都能找到它。
        # is_web 用 X-LML-Web 判断（和 proxy() 里记录队列来源的口径一致；
        # 原来这里错用 Origin 头，导致同一请求在两处被判成不同来源）。
        _req_is_web = bool(self.headers.get("X-LML-Web"))
        _my_rid = _active_register(resp, _req_is_web)
        # 边转发边扫描：timings/usage + 首字时刻（DSH 的请求也走这里）
        probe = b""
        t_req = time.time()
        t_think = None      # 第一个思考 token 到达
        t_body = None       # 第一个正文 token 到达
        linebuf = b""
        first_ids = 0       # 已做的详细解析次数上限，避免长流每 chunk 都 parse
        try:
            while True:
                chunk = resp.read1(16384) if hasattr(resp, "read1") else resp.read(16384)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
                # 累计已转发的【正文字符数】：只解析 SSE 里的 content /
                # reasoning_content，不能直接 len(chunk) —— 那里面还有
                # "data: {...}" 包装和 JSON 结构，实测会把 34672 字节
                # 误算成 34672 token。
                try:
                    for _ln in chunk.split(b"\n"):
                        if not _ln.startswith(b"data: "):
                            continue
                        _pl = _ln[6:].strip()
                        if not _pl or _pl == b"[DONE]":
                            continue
                        try:
                            _o = json.loads(_pl)
                        except Exception:
                            continue
                        _d = (_o.get("choices") or [{}])[0].get("delta") or {}
                        _txt = (_d.get("content") or "") + (_d.get("reasoning_content") or "")
                        if _txt:
                            self._stream_chars = (getattr(self, "_stream_chars", 0)
                                                  + len(_txt))
                except Exception:
                    pass
                probe = (probe + chunk)[-8192:]

                # 只在还没拿到两个首字前做逐行解析（拿到后就不再解析，省 CPU）
                if t_think is None or t_body is None:
                    linebuf += chunk
                    while b"\n" in linebuf:
                        ln, linebuf = linebuf.split(b"\n", 1)
                        ln = ln.strip()
                        if not ln.startswith(b"data:"):
                            continue
                        pl = ln[5:].strip()
                        if not pl or pl == b"[DONE]":
                            continue
                        try:
                            obj = json.loads(pl)
                        except Exception:
                            continue
                        chs = obj.get("choices") or []
                        if not chs:
                            continue
                        dl = chs[0].get("delta") or {}
                        now = time.time()
                        if t_think is None and dl.get("reasoning_content"):
                            t_think = now
                        if dl.get("content") is not None and dl.get("content") != "":
                            if t_body is None:
                                t_body = now
                        first_ids += 1
        except Exception as _e:
            # ★ 上游断流（后端崩了 / 被 kill / 显存抖动）：不能静默 pass。
            #   以前这里吞掉异常，客户端收到的是"干净结束"的流，
            #   前端会把半截回复当成正常完成存进历史 —— 用户完全看不出来。
            #   现在补一个错误帧 + [DONE]，前端据此标记失败。
            try:
                _em = (str(_e)[:200] or "上游连接中断")
                # ★ 标记这次是「中断/失败」：原来这个异常被吞掉，proxy() 里
                #   _ok 仍然是 True → 队列记成"已完成"、异常数不增、成功率虚高。
                self._stream_err = _friendly_err(_em)
                _frame = json.dumps({"error": {"message": _em,
                                               "type": "upstream_disconnected"}},
                                    ensure_ascii=False)
                self.wfile.write(("data: " + _frame + "\n\n").encode("utf-8"))
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
            except Exception:
                pass          # 连写都失败 = 客户端已断开，无所谓了
        finally:
            _active_unregister(_my_rid)     # 只清自己那条，不碰别人的
            t_end = time.time()
            try:
                resp.close()
            except Exception:
                pass
            try:
                _info = _record_from_probe(probe, is_web=_req_is_web,
                                           t_req=t_req, t_think=t_think, t_body=t_body, t_end=t_end)
                self._last_info = _info if isinstance(_info, dict) else {}
            except Exception:
                self._last_info = {}


def main():
    # 启动时自动加载上次的模型（如果有配置）
    cfg = os.path.join(HERE, "last.json")
    if os.path.isfile(cfg):
        try:
            c = json.load(open(cfg, encoding="utf-8"))
            p = c.get("path")
            if p and os.path.isfile(p):
                print("自动加载上次模型:", os.path.basename(p), flush=True)
                log_warn("启动自动加载: %s ctx=%s kv=%s" % (p, c.get("ctx"), c.get("kv")))

                def _bg():
                    try:
                        _par = int(c.get("parallel") or 1)
                        STATE["parallel"] = _par
                        ok, err = start_backend(p, int(c.get("ctx") or 8192),
                                                c.get("kv") or "f16", _par)
                        log_warn("自动加载结果: ok=%s err=%s" % (ok, (err or "")[:200]))
                    except Exception as e:
                        log_warn("自动加载异常: %r" % (e,))

                threading.Thread(target=_bg, daemon=True).start()
            else:
                log_warn("last.json 里的路径不存在: %r" % (p,))
        except Exception as e:
            log_warn("解析 last.json 失败: %r" % (e,))

    # 模型目录（config.json 的 model_dirs）——原来这个函数只定义从没被调用，
    # config.json 里的 model_dirs 写了也无效，前端还提示"可以加 model_dirs"，纯属撒谎
    _load_model_dirs()

    # 登录保护 + 局域网配置
    _load_auth()
    try:
        cp = os.path.join(HERE, "config.json")
        if os.path.isfile(cp):
            with open(cp, encoding="utf-8-sig") as fp:
                _c = json.load(fp) or {}
            if _c.get("lan"):
                LAN_ENABLED[0] = True
                print("  局域网访问：已开启（http://%s:%d）" % (_local_ip(), GATEWAY_PORT))
    except Exception:
        pass
    if auth_required():
        print("  登录保护已开启：用户 %s（局域网访问需先登录）" % AUTH.get("user"))

    # 后台 GPU 采样（接口不再阻塞等待 PowerShell）
    threading.Thread(target=_gpu_sampler, daemon=True).start()
    print("GPU 采样线程已启动", flush=True)

    # ★ 永远监听 0.0.0.0：靠 LAN_ENABLED 白名单挡非本机来源，
    #   这样设置页一开一关立即生效，不用重启（用户要的"热加载"）。
    bind_host = "0.0.0.0"
    srv = ThreadingHTTPServer((bind_host, GATEWAY_PORT), Handler)
    print("=" * 56)
    print("  lm-lite 已启动")
    print("  打开: http://127.0.0.1:%d" % GATEWAY_PORT)
    if LAN_ENABLED[0]:
        print("  局域网: http://%s:%d（已开启）" % (_local_ip(), GATEWAY_PORT))
    else:
        print("  局域网: 未开启（可在设置页随时打开，无需重启）")
    print("  后端: %s" % LLAMA_SERVER)
    print("=" * 56)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n退出")
        stop_backend()


if __name__ == "__main__":
    main()
