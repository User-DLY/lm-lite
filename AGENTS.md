# lm-lite —— 本地推理前端 + 网关

给 AI 助手的项目须知。读完这个你就知道这个项目是什么、怎么改、别踩什么坑。

## 架构

```
浏览器页面 (index.html)
      ↓ HTTP
网关 gateway.py (:9000) —— 管后端进程 + 自动切模型 + 统计
      ↓ 转发
llama-server (:8081) —— 由网关拉起，跑 GGUF 模型
      ↓
AMD RX 6600 / ROCm
```

另外 DSH 也能直接用这个后端：baseURL 指到 `http://127.0.0.1:9000/v1`。

**单独的服务**：`D:\AI_Agent_Project\ai-chatroom\` 有个多模型聊天室（:8010）。

## 文件

| 文件 | 作用 |
|---|---|
| `index.html` | 前端全部（HTML+CSS+JS 单文件，约 2600 行，无依赖） |
| `gateway.py` | 网关：进度管理、模型切换、路由转发、统计 |
| `launcher.py` | 启动器：环境自检 + 端口清理 + 崩溃自动重启 |
| `lm-lite.bat` | 双击启动（已在运行则只开浏览器） |
| `交接文档.txt` | 完整交接（10 章，含排障命令和 DSH 配置） |
| `last.json` | 上次加载的模型配置（自动生成） |

后端二进制：`D:\AI_Agent_Project\llama-rocm-win\llama-server.exe`
模型目录：`%USERPROFILE%\.lmstudio\models\`

## 关键 API

```
GET  /api/status        进程/模型/上下文/加载进度
GET  /api/vram          显存（后台线程采样，不阻塞）
GET  /api/models        可用 gguf 列表
GET  /api/model-caps    当前模型支不支持思考（读 /props 的 chat_template）
POST /api/load          {path, ctx, kv} 加载模型
POST /api/unload        卸载
POST /api/stop          中断生成
GET  /api/stats         统计（网关自己测的真实首字）
POST /v1/chat/completions   OpenAI 兼容，带自动切模型
```

## 三个必须知道的事实

**1. llama-server 不认 `reasoning_effort`**
它的 `chat_template_caps.supports_reasoning_effort = false`。唯一有效的开关是
`chat_template_kwargs: {enable_thinking: bool}`。网关会翻译，但如果你直接打 8081，要自己传。

**2. Spark 1.7B 开思考会把 token 全烧光**
实测 max_tokens=300 时，reasoning 300 段、正文 0 段。默认关思考（网关已注入）。

**3. 上下文估算要用真实 GGUF 元数据**
不要靠文件名猜（"4b" 不代表 4B）。`gguf_meta2.js` 和 gateway 里都有解析器。
1.7B = 14 KiB/token(f16)，4B = 36 KiB/token。

## 实测速度（RX 6600 / ROCm）

| 模型 | 2K | 6K | 12K | 20K 提示 |
|---|---|---|---|---|
| **1.7B** | 2372 | 2491 | 2371 | **2198 tok/s** |
| **4B** | 915 | 967 | 930 | ~880 tok/s |

1.7B 几乎不随上下文衰减（混合注意力：28 层里只有 7 层全注意力）。
4B 慢 2.5 倍。日常用 1.7B。

## 改代码的铁律（血泪教训）

**① 别用正则批量改 CSS**
已经踩过 5 次：
- 加 `100dvh` 时把整条 `html,body{height:100%;overflow:hidden}` 替换没了 → 布局塌陷到 432px
- 删 `--r` 变量时留下 `n;overscroll-behavior:none` 垃圾在 `:root` 里
- 删「（点开看）」时留下悬空 `+` 号 → SyntaxError

**改完必须做三件事：**
1. `node --check` 检查 JS 语法
2. 打开页面看布局（用 ego 或截图）
3. 确认目标字符串在整个文件里唯一

**② 主题过渡不能一刀切**
```css
/* 错：会覆盖这些元素原有的 transition */
body,#side,.conv,.card{transition:background-color .34s}

/* 对：逐元素合并 */
#side{transition:width .28s, opacity .22s, background-color .34s}
```
踩过一次：`#side` 的折叠动画直接失效。

**③ 默认隐藏靠状态类显示的元素，必须核实 JS 真的在加那个类**
`#cstats` 只有 CSS 的 `.has-data{display:flex}` 定义，没有一处 `classList.add('has-data')`，
结果底部统计栏**从上线至今一次都没显示过**。

**④ 防重入锁必须有超时释放**
`refresh()` 的 `_refreshing` 锁卡住过一次，界面永不更新（显存一直显示 0）。
现在有 `_refreshAt` 时间戳，超 6 秒强制放锁。

**⑤ 数字动画不要用 translateY**
会像"飞"。用 `opacity + 极轻微 scale`，并加 `transform-origin: left center` 保证左对齐。

## 常见故障

| 现象 | 原因 | 处理 |
|---|---|---|
| `Connection error` | 8081 没服务 | 模型没加载或崩了，看 `backend.log` |
| 界面显示"网关断开" | 9000 挂了 | 看 `gw.err` |
| `UNSUPPORTED_REASONING_EFFORT` | DSH 里某模型缺 `reasoningEfforts` | 补上并重启 DSH |
| 上下文显示 16K 但实际 98K | 设置页没同步 | 已修：refresh 会同步实际值 |

## 跑之前必须提醒用户

**GPU 满载前把显卡风扇拉到 100%。**
用户的系统盘（NVMe，C 盘）物理位置就在显卡正下方，
该机 2026-07 至今 40 次异常关机，蓝屏代码 340/122 全部指向硬盘读写失败。

操作：`C:\Program Files\AMD\CNext\CNext\RadeonSoftware.exe` → 性能 → 调整 → 风扇

## 当前状态（2026-10-17）

- 前端已重做 UI（SVG 图标、靛蓝紫配色、49 个 CSS 变量、16 个动画）
- 网关已有真实 TTFT 测量、GGUF 元数据解析、思考开关翻译
- 已知未修：`gateway_warn.log` 无轮转、会话历史静默截断、
  流式渲染 O(n²)、移动端体验弱

## 2026-10-18 三轮审查修复记录（第 1 轮）

### 架构审查发现并已修
1. 统计双计：页面请求的 ttft/tps/pp 被网关和前端各记一次 → 删掉 /api/stats 里的重复追加
2. 队列错误原因恒为"请求失败"：_err_msg 声明未赋值 → 加 except 捕获 + _stream_err
3. BLOCKLIST 无锁：add/discard/sorted 加 _BLOCK_LOCK
4. launcher 退出只杀网关 → 加 _stop_backend_first()，先 /api/unload 再按命令行精确清 llama-server
5. 显存总量硬编码 8192 → 可用 LM_LITE_VRAM_MB 环境变量覆盖
6. /api/load 的 ctx/kv/parallel 无校验 → 夹取 512~1048576、kv 限三档、parallel 1~4
7. 模型匹配不到时静默沿用 → 写入 STATE["mismatch"] 并明确告警（通配名仍正常沿用）
8. 加载进度先跳 100%：换模型时旧日志的 "listening on" 被当成本次完成 → 从后往前扫，遇到上一个完成标记就截断

### 功能审查发现并已修
9. 生成中刷新页面，刚发的用户消息永久丢失 → push 后立即 saveConvs()
10. 被停止/中断的请求记成"已完成" → 错误帧设置 _stream_err，proxy 据此判失败
11. 双击发送按钮会停掉自己刚发的请求 → 500ms 内第二次点击忽略
12. 导出 CSV 不遵守当前筛选 → 按 qFilter 过滤后再导出
13. 卸载后底栏统计残留 → 清 has-data 类 + 各项置 —
14. 未加载模型时刷新，启动遮罩永不消失 → 网关就绪但无模型时直接 finish()
15. 生成中删除别的会话，当前回复消失 → 只在删的是当前会话时才 renderConv()
16. 删会话不清理草稿键 → delConv 里 removeItem
17. 复用空会话新建时不清草稿 → 加 saveDraft()
18. saveDraft('') 传参无效 → 改 saveDraft()
19. 队列详情模型名未转义 → escHtml
20. loadQueue 全吞异常 → 失败时明确提示"数据获取失败"
21. 队列时间跨天看起来像倒退 → 非当天显示完整日期
22. 运行中任务列表耗时冻结 → 详情刷新时同步更新该行文本
23. /api/queue/clear 加到了 do_GET，前端发 POST → 移到 do_POST

### 未修（已知，优先级低）
- STATS 全局无锁（多线程 += 非原子，-np 1 下影响小）
- ACTIVE 单槽与 -np>1 的矛盾（当前 -np 1，未触发）
- send() 单函数 430 行、模块级可变状态约 40 个
- 死代码：_read_gpu_memory_old、_safe()、MODEL_CTX={}、KV_BYTES 重复定义、--cols、gwDown
- 日志游标超 1MB 会跳行
- 5 个常驻定时器，其中 400ms 那个读 scrollHeight 会强制重排


## ⚠️ 本轮最严重的回归（已修，务必记住）

**LOAD_STARTED_AT 定义被批量替换覆盖 → 自动加载抛 NameError → 模型在跑但 STATE 全空**

现象链：
1. 网关重启 → main() 后台线程读 last.json 调 start_backend
2. _start_backend_locked 第一行 LOAD_STARTED_AT[0] = time.time() → NameError
3. 异常被 _bg() 的 try/except 吞掉，只打一行 自动加载异常
4. **llama-server 已经启动成功**（-c 100096 在跑），但 STATE["ctx"/"kv"/"model"] 全没更新
5. /api/status 返回 ctx=8192 kv=q8_0 model=null，前端顶栏/设置页全显示错

**排查方法**：/api/status 的 ctx 和 llama-server 实际 -c 对不上 → 查 gateway_warn.log 找"自动加载异常"。
**教训**：给函数加"记录起点"这类全局变量时，替换文本必须包含定义本身；替换后必须确认定义还在（st 检查模块级 Assign）。


## 第 2 轮（复审）修复

复审确认第 1 轮 21 条中 14 条真修好，7 条"改了但没真解决"，已补修：

1. **launcher 常规退出仍孤儿化 llama-server** —— _stop_backend_first() 原来只在 main() 正常 return 前调用，关窗口/Ctrl+C 都不走 → 加 texit + SIGINT/SIGTERM/SIGBREAK 三层兜底
2. **_last_info 残留** —— HTTP/1.1 keep-alive 复用 handler，_proxy_stream 提前 return 时沿用上一笔的 token 指标并记成"已完成" → 每次 proxy 开头清空
3. **中断后内容全丢** —— 第 1 轮把"半截当成功"修成了"半截全丢"（failed 分支直接写"本轮失败，未产生回复"）→ 改成保留已流出正文，错误单独一行
4. **看门狗仍会误杀** —— 按"分配上下文"算（4B ctx=40K 只给 90 秒）而实测 21.6K 提示要 185 秒 → 改成 300 秒起、按 ctx 每 K 给 4 秒
5. **save_blocklist() 在锁外** —— 里面对 BLOCKLIST 做 sorted，并发 add 会 RuntimeError → 移进 _BLOCK_LOCK
6. **STATE["mismatch"] 无人读** —— 只写不读，UI 永远看不到 → 暴露到 /api/status，匹配成功时清空
7. **ctx 上界写死 1M** —— 传 999999 照样放行 → 改用 
ead_gguf_meta 读到的真实训练上下文
8. **显存环境变量名不一致** —— 代码读 LM_LITE_VRAM_MB、文档写 LM_LITE_GPU_TOTAL_MB → 两个都支持

### 外部干扰（非 bug，但要知道）
gateway_warn.log 出现 client='...WindowsPowerShell...' 请求 model='qwen3-1.7b'，触发 ensure_model 自动切换模型并覆盖 last.json。
**自动切换是有意设计**，但意味着：任何客户端请求一个没加载的模型名，都会把当前模型换掉。要固定模型就在设置里加载后别让外部客户端发别的模型名。


## 第 2 轮（对抗测试）补修

1. **上游 4xx/5xx 记成「已完成」** —— _proxy_stream 的 HTTPError 分支只发错误就 return，_ok 仍 True → 在该分支设置 _stream_err
2. **自动切换模型漏传 parallel** —— ensure_model 调 start_backend 时没带并发数，llama-server 回到 -np 1 而 STATE 还写着 3 → 补传
3. **last.json 不记 parallel** —— 重启后 -np 回 1 → 写入并在启动时读回
4. **无 timings 的请求不计入请求总数** —— STATS["requests"] += 1 在 if not tm: return {} 之后 → 移到 return 之前
5. **/api/block 不校验 IP 格式** —— {"ip":"not-an-ip;rm"} 也能写进黑名单 → 用 ipaddress 模块校验
6. **黑名单重启后无法解黑** —— 黑名单落盘、PEERS 内存，重启后已拉黑 IP 不在列表里 → /api/peers 把黑名单中无访问记录的补进去
7. **_qSelHint 残留** —— 「回全部→再筛掉」时右侧还挂着上次详情 → 去掉去重逻辑，每次切换都写
8. **拖动上限漏算 margin / 恢复高度不夹取** —— 拖到极限或上次存了 600px 时，柱状图和按钮被挤出可视区 → 算上 margin，恢复时也夹取
9. **mismatch 前端没消费** —— 接口透出了但界面不显示 → 加 #mismatchWarn 元素

## 当前运行参数（正常态）
- 模型：Spark-X2.5-1.7B-Q4_K_M.gguf
- ctx=100096  kv=f16  parallel=1
- llama-server 实际：-c 100096 -np 1
- 网关 /api/status 返回的 ctx/kv/model 与上面对得上（这是判断"状态有没有崩"的快捷方式）


## 第 3 轮（终审）修复 —— 评分 32/50

终审抽查 13 项全部实测通过（含"制造错误"验证：伪造 /api/stats、
ole:"bogusrole" 逼 400、流式中 /api/stop）。抓到 10 条漏网之鱼，已修：

1. 🔴 **窄屏队列页右栏整块点不到** —— 500px 下右栏只有 328px 高、内容 620px，#pg-queue{overflow:hidden} 裁掉了「导出 CSV/清空记录/柱状图」→ 窄屏改成整页可滚动（#pg-queue.on{overflow-y:auto} + .qright{flex:none;max-height:none}）
2. 🟡 **错误文案未归一** —— 队列里并存 'NoneType' object has no attribute 'read1'、[WinError 10053]... 等内部异常 → 加 _friendly_err() 翻译成「已停止（用户中断）」「客户端已断开」「上游超时」「上游连接中断」
3. 🟡 **launcher.free_port 盲杀端口** —— netstat 拿到 PID 就 	askkill /F，会误杀 LM Studio 等同端口的进程 → 加 _proc_name_of()，只杀 python/llama-server/cmd/powershell，其他只报告不动手

### 未修（已知，明确留作后续）
- STATS 多数 += 无锁（-np 1 下影响小）
- 队列 total 与 stats.requests 口径：4xx 走 HTTPError 分支直接 return，不进 probe
- web/api 归因两套标准（REQLOG 用 X-LML-Web、STATS 用 Origin）
- 柱状图 0 条时无占位；两个页面"平均速度"两套源
- 死代码：.qitem* CSS、MODEL_CTX、--cols、gwDown
- /api/stats POST 仍可被伪造来改 STATS["last"] 的 thinkTtft/thinkMs

### 终审评分
架构 6 / 代码质量 6 / UI 7 / 稳定性 7 / 可维护性 6 = **32/50**
一句话：能天天用，但统计口径、错误文案、窄屏这几处还欠收口。


## 🔴 2026-10-18 聊天 UI 崩溃事故（我造成的）

**现象**：所有会话「全是黑的」打开没内容、新建对话发不出去。

**根因链**（两处，都出现在 addMsg）：
1. 第 3 轮修「历史消息不显示详情」时写了 + (m.stat ? '<span class="ex">详情</span>' : '')，
   但 ddMsg(role, text, thinkTxt, statHtml, fromStore, prepend, thinkSecs) **根本没有 m 这个参数** →
   每次调用都抛 ReferenceError: m is not defined → 一条消息都渲染不出来，聊天区全空。
2. 同一处改动还让 .ex 元素可能不存在，而下一行 d.querySelector('.ex').onclick = ...
   没有判空 → 就算修好第 1 条还会抛 Cannot read properties of null。

**修复**：m.stat → statHtml；.ex 取出来判空。

**教训**：
- 
ode --check 只查语法，**查不出"引用了不存在的变量"**。改完 JS 必须在浏览器里实际调用一次相关函数（
enderConv()）看有没有异常，而不是只看文件能不能解析。
- 这个函数是聊天主渲染路径，改错等于整个聊天界面崩溃。**改核心渲染函数后必须实测"打开旧会话 + 新建 + 发送"三件事。**

## 2026-10-18 布局回退（用户明确要求）

**用户要求：除推理队列页外，性能/日志/设置全部全屏铺满。**
第 3 轮我擅自给性能页加了 max-width:1500px、日志框加了 max-width:1500px、设置页 .wrap 保留 1180px 居中 —— 全部撤销：
- #perf > *{max-width} 删除
- #logbox{max-width} 删除
- .wrap{max-width:1180px} → width:100%（两列断点保留）

**唯一保留限宽的是 #queueWrap{max-width:1600px}（队列页，用户允许）。**

## 输入框高度同步（新修）
--composer-h 原来只在 updateGotoBtn()（滚动相关）里测，输入框从 1 行长到 20 行时
变量停在旧值（实测 265px vs 实际 116px）→「回到底部」按钮位置算错。
改成 ResizeObserver 常驻监听 #composer + input 事件兜底。

