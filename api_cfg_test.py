# -*- coding: utf-8 -*-
"""API 默认值注入语义测试：未指定→注入；显式指定→不覆盖；开关→全关"""
import json, time, urllib.error, urllib.request

BASE = "http://127.0.0.1:9000"


def api_cfg(**kw):
    body = json.dumps(kw, ensure_ascii=False).encode()
    r = urllib.request.Request(BASE + "/api/api-cfg", data=body,
                               headers={"Content-Type": "application/json"})
    try:
        return json.load(urllib.request.urlopen(r, timeout=10))
    except urllib.error.HTTPError as e:
        return json.loads(e.read().decode("utf-8", "ignore"))   # 400 的错误体也是 JSON


def chat(payload):
    body = json.dumps(payload, ensure_ascii=False).encode()
    r = urllib.request.Request(BASE + "/v1/chat/completions", data=body,
                               headers={"Content-Type": "application/json"})
    t0 = time.time()
    resp = urllib.request.urlopen(r, timeout=180)
    raw = resp.read().decode("utf-8", "ignore")
    return json.loads(raw), time.time() - t0


def count_out(j):
    u = j.get("usage") or {}
    return u.get("completion_tokens") or 0


fails = []
def T(cond, msg):
    print(("  ✓ " if cond else "  ✗ ") + msg)
    if not cond:
        fails.append(msg)

# ── 1. 校验：坏值必须被拒 ─────────────────────────────────
print("== 1. 服务端校验 ==")
r = api_cfg(enabled=True, temp="9")          # 温度超范围
T(r.get("ok") is False and "temp" in (r.get("error") or ""), "温度 9 被拒（%s）" % r.get("error"))
r = api_cfg(enabled=True, maxTok="abc")      # 非数字
T(r.get("ok") is False, "maxTok=abc 被拒（%s）" % r.get("error"))
r = api_cfg(enabled=True, topK="-5")         # 负数
T(r.get("ok") is False, "topK=-5 被拒（%s）" % r.get("error"))

# ── 2. 未指定 → 注入（把上限压到 60，数到 80 应被截断）───
print("== 2. 未指定参数注入默认值 ==")
r = api_cfg(enabled=True, temp="0.7", maxTok="60", topP="0.95",
            topK="40", repPen="1.1", seed="")
T(r.get("ok") is True, "保存 maxTok=60 成功")
j, _ = chat({"model": "local", "stream": False,
             "messages": [{"role": "user", "content": "从1数到80，每行一个数字，只输出数字"}]})
n = count_out(j)
T(0 < n <= 62, "未指定 max_tokens → 被注入的 60 截断（实际输出 %d token）" % n)

# ── 3. 显式指定 → 不覆盖 ─────────────────────────────────
print("== 3. 调用方显式值优先 ==")
j, _ = chat({"model": "local", "stream": False, "max_tokens": 500,
             "messages": [{"role": "user", "content": "从1数到80，每行一个数字，只输出数字"}]})
n2 = count_out(j)
T(n2 > 62, "显式 max_tokens=500 不被 60 覆盖（实际输出 %d token）" % n2)

# ── 4. 开关关闭 → 不注入 ─────────────────────────────────
print("== 4. 总开关 ==")
r = api_cfg(enabled=False, maxTok="60")
T(r.get("ok") is True, "关闭注入成功")
j, _ = chat({"model": "local", "stream": False,
             "messages": [{"role": "user", "content": "从1数到80，每行一个数字，只输出数字"}]})
n3 = count_out(j)
T(n3 > 62, "关闭后不注入，数到 80 完整输出（%d token）" % n3)

# ── 5. 恢复默认 + last 来源标记 ──────────────────────────
print("== 5. 恢复默认 + 性能页 last 来源 ==")
r = api_cfg(enabled=True, temp="0.7", maxTok="2048", topP="0.95",
            topK="40", repPen="1.1", seed="")
T(r.get("ok") is True, "恢复默认值")
st = json.load(urllib.request.urlopen(BASE + "/api/stats", timeout=10))
L = st.get("last") or {}
T(bool(L), "API 请求后 last 非空（之前只有页面请求才更新，已修）")
T(L.get("src") == "api", "last.src = %r（API 流量被正确标注）" % L.get("src"))
T(L.get("outTok") == n3, "last.outTok=%s 与最后一次请求一致" % L.get("outTok"))
T("streamed" in L, "last.streamed 字段存在（=%r，非流式应为 False）" % L.get("streamed"))

print("\n" + ("ALL_API_CFG_TESTS_PASSED" if not fails else "FAILED: %s" % fails))
