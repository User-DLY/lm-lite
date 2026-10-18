# lm-lite 重建指南（重装系统后照这个做就能恢复）

> **给接手这件事的智能体看**：这个项目是"用 ROCm 加速的 llama.cpp 跑本地模型 + 自建 Web 前端 + 接进 DSH"。
> 整套东西都在 `<project>\`，只要这个目录还在（或从备份恢复），照下面做就能重建。
> 如果连目录都没了，本文档最后有"从零重建"的完整步骤和下载地址。

---

## 一、这套东西是什么

```
DSH 智能体 ──直连──> llama-server (8081, ROCm)   ← 推理，零中转
管理前端 ────────────────────> 网关 (9000) ──只管启停/加载/监控
```

**为什么用 ROCm 而不是 Vulkan**：同一张 RX 6600 上实测，ROCm 的 prefill（读上下文）比 Vulkan 快 **4.6 倍**（8K 上下文 3094 vs 666 t/s），生成速度略慢 19%（83 vs 97 t/s）。DSH 场景是"长提示 + 短回答"，所以 ROCm 净赚。

---

## 二、机器要求

| 项目 | 本机实际 |
|---|---|
| 系统 | Windows 11 |
| 显卡 | AMD Radeon RX 6600（8 GB，gfx1032 / RDNA2） |
| CPU | i5-13600KF |
| 内存 | 16 GB DDR4 2400 |
| Python | 3.12（`%USERPROFILE%\AppData\Local\Programs\Python\Python312\python.exe`） |

**Python 只用标准库，不需要 pip 装任何东西。**

---

## 三、目录结构（必须完整）

```
<project>\
├── index.html          # 前端（单文件，约 1500 行）
├── gateway.py          # 网关（管理 llama-server + 转 API + 统计）
├── launcher.py         # 智能启动器（环境检查 + 端口清理 + 崩溃自动重启）
├── lm-lite.bat         # 一键启动（桌面也有同名文件）
├── last.json           # 上次加载的模型（自动生成，删了也不影响）
├── backend.log         # llama-server 日志（自动生成）
├── gateway_warn.log    # 网关警告/自动切换记录（自动生成）
└── backend\            # ★ 这是 llama.cpp ROCm 后端，见第四节
```

---

## 四、llama.cpp ROCm 后端（最关键，必须单独下载）

**当前实际路径**：`D:\AI_Agent_Project\llama-rocm-win\`

**来源**：GitHub `lemonade-sdk/llamacpp-rocm`（⭐782，专做 AMD ROCm 的 llama.cpp 构建）

**下载**（选 `gfx103X` 那个，对应 RX 6600 的 gfx1032）：
```
https://github.com/lemonade-sdk/llamacpp-rocm/releases
  找到最新的 bXXXX 版本
  下载 llama-bXXXX-windows-rocm-gfx103X-x64.zip   （约 491 MB）
```

**国内加速**（GitHub 直连约 15 KB/s，用镜像 4~8 MB/s）：
```
https://gh-proxy.com/https://github.com/lemonade-sdk/llamacpp-rocm/releases/download/bXXXX/llama-bXXXX-windows-rocm-gfx103X-x64.zip
```

**解压后关键文件**（缺一不可）：
```
llama-server.exe      （9 KB 空壳，真逻辑在 dll）
ggml-hip.dll          （177 MB ★ ROCm 后端，就是它替代 Vulkan）
amdhip64_7.dll        （16.6 MB  ROCm 7 运行时）
amd_comgr.dll         （121 MB）
rocblas\   libhipblaslt.dll   hipblas.dll
```

**验证后端能用**：
```powershell
cd D:\AI_Agent_Project\llama-rocm-win
.\llama-cli.exe --list-devices
# 应该输出：ROCm0: AMD Radeon RX 6600 (8176 MiB, ...)
```

---

## 五、模型（放 LM Studio 目录，网关会自动扫）

**目录**：`%USERPROFILE%\.lmstudio\models\`

**实际在用的**：
```
XHToken\Spark-X2.5-1.7B-GGUF\Spark-X2.5-1.7B-Q4_K_M.gguf     1.03 GB  ← 主力
XHToken\Spark-X2.5-1.7B-GGUF\Spark-X2.5-1.7B-Q8_0.gguf       1.70 GB
XHToken\Spark-X2.5-4B-GGUF\Spark-X2.5-4B-Q4_K_M.gguf         2.42 GB
HauhauCS\Gemma-4-E4B-Uncensored-HauhauCS-Aggressive\...Q4_K_M.gguf  4.97 GB
```

**下载源**（ModelScope，实测 9~10 MB/s，比 HF 快 44 倍）：
```
列文件：https://modelscope.cn/api/v1/models/<owner>/<repo>/repo/files?Revision=master
下载：  https://modelscope.cn/api/v1/models/<owner>/<repo>/repo?Revision=master&FilePath=<文件名>
```
**Spark 系列**：`XHToken/Spark-X2.5-1.7B-GGUF`（有 Q4_K_M 1056MB / Q8_0 1736MB / f16 3262MB）

---

## 六、DSH 配置（决定 DSH 用哪个模型）

**文件**：`%USERPROFILE%\.dsh\profiles\web\cordis.patch.yml`

**必须存在的两段**：

```yaml
# 1) provider：指向网关的 8081
      local-rocm:
        displayName: 本地 ROCm 加速
        api: openai-completions
        baseURL: http://127.0.0.1:8081/v1
        apiKeyEnv: LM_STUDIO_LFM_KEY
        reasoning: high
        models:
          - id: spark-x2.5-1.7b-q4
            name: 星火X2.5-1.7B-Q4 (ROCm 加速)
            contextWindow: 100096
            maxTokens: 8192
            input:
              - text
            reasoningEfforts:        # ★ 每个模型都必须声明，否则 DSH 报 UNSUPPORTED_REASONING_EFFORT
              off: none
              high: high
          # ... 其它模型同格式

# 2) 默认模型
- id: agent-default-model
  name: "@deepseek-ai/dsh-agent-default-model"
  config:
    provider: local-rocm
    model: spark-x2.5-1.7b-q4
    reasoningEffort: high
```

**⚠️ 改完必须重启 DSH 才生效。**
**⚠️ 每个模型都要有 `reasoningEfforts`，漏一个选中它就报 `UNSUPPORTED_REASONING_EFFORT`。**

**校验配置能解析**：
```powershell
$node = "C:\Program Files\QClaw\<版本>\resources\node\node.exe"
$bin  = "%USERPROFILE%\AppData\Roaming\QClaw\npm-global\node_modules\@deepseek-ai\dsh\lib\bin.js"
& $node $bin --profile web --dump-config
# 没有 ParserError / YAMLException 就是好的
```

---

## 七、启动和验证

**启动**：
```
双击 桌面\lm-lite.bat
（它起 launcher.py，launcher 再起 gateway.py，gateway 按 last.json 自动加载模型）
```

**验证清单**（缺哪步查哪步）：
```powershell
# 1) 两个端口都要监听
Get-NetTCPConnection -LocalPort 9000 -State Listen   # 网关
Get-NetTCPConnection -LocalPort 8081 -State Listen   # llama-server

# 2) 后端健康
Invoke-RestMethod http://127.0.0.1:8081/health         # 应返回 status=ok

# 3) 网关状态
Invoke-RestMethod http://127.0.0.1:9000/api/status     # alive 应为 True

# 4) 模型列表（DSH 靠这个发现模型）
Invoke-RestMethod http://127.0.0.1:9000/v1/models

# 5) 模拟 DSH 请求
Invoke-RestMethod http://127.0.0.1:9000/v1/chat/completions -Method POST `
  -ContentType 'application/json' `
  -Body '{"model":"spark-x2.5-1.7b-q4","messages":[{"role":"user","content":"hi"}],"max_tokens":16,"stream":false}'
```

**排障**：
| 现象 | 原因 | 处理 |
|---|---|---|
| DSH 报 `Connection error` | **8081 没有服务**（最常见） | 双击 `lm-lite.bat`，确认 8081 在监听 |
| DSH 报 `UNSUPPORTED_REASONING_EFFORT` | 该模型缺 `reasoningEfforts` | 补上并重启 DSH |
| 网关起来了但 `alive=False` | 模型没加载（`last.json` 丢了） | 前端点「加载模型」或调 `/api/load` |
| 网关自己死了 | 没用 `launcher.py` 起，没有看护 | 用 `lm-lite.bat`（走 launcher） |

---

## 八、从零重建（连目录都没了的情况）

```powershell
# 1. 建目录
mkdir <project>
mkdir D:\AI_Agent_Project\llama-rocm-win

# 2. 下载 ROCm 后端（见第四节）

# 3. 把 index.html / gateway.py / launcher.py / lm-lite.bat 放回 lm-lite\
#    （从备份恢复，或让智能体按本文档重写）

# 4. 下载模型（见第五节）

# 5. 改 DSH 配置（见第六节），重启 DSH

# 6. 双击 lm-lite.bat，按第七节验证
```

---

## 九、已知限制（别踩）

1. **llama-server 一次只能加载一个模型** —— 换模型要重启后端（约 4~20 秒）
2. **网关无锁** —— 双标签页同时点加载可能打架
3. **`/api/load` 阻塞最长 240 秒** —— 加载大模型时前端会等
4. **日志每 2.5 秒全量读** —— 长跑后变慢，但不是 bug
5. **历史只存 30 个会话 / 每会话 60 条** —— 超了会静默截断
6. **状态散在 5 个全局变量**（busy/curAbort/sendSeq/activeConv/__ctxUsed）—— 加新功能前先重构

---

## 十、关键实测数据（换硬件时参考）

| 指标 | Vulkan | ROCm |
|---|---|---|
| prefill pp512 | 2262 t/s | **3524 t/s** |
| prefill pp8192 | 666 t/s | **3094 t/s** |
| 生成 tg128 | **97.5 t/s** | 83.4 t/s |
| 8K 提示 + 512 生成总耗时 | 17.6 s | **9.2 s** |

**Q4 vs Q8（Spark 1.7B）**：Q4 快约 40%（100 token：1.09s vs 1.54s），省 0.67 GB 显存。

**加载耗时**：Q4 1 GB ≈ 1~2 秒；切模型 4~9 秒。
