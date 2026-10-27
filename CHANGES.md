# 变更记录

## 0.1.3（2026-10-27）

**数据库 schema 2 → 3**，启动时自动迁移，现有账号、池、Key、日志无需手工处理。0.1.3 生成的加密备份无法被 0.1.2 及更早版本恢复；旧版本备份仍可在 0.1.3 恢复。

### 1. 密钥可在管理台查看与复制

此前 Key 只保存 SHA-256 摘要，明文仅在创建时出现一次。现在：

- 新增 `api_keys.secret` 列，明文用现有 Fernet 保险库加密（主密钥由 Windows DPAPI 保护）
- `GET /api/keys/{kid}/secret` 返回明文，仅管理员会话可用，每次调用写 `key_secret_viewed` 审计
- `POST /api/keys/{kid}/rotate` 重新生成：换掉 digest 与 secret，旧密钥立即失效，Key 的模型范围、预算、并发等配置全部保留
- 管理台每行新增「查看密钥」，弹窗内可直接复制或重新生成
- **认证仍只比对摘要**，解密仅用于展示；`/api/keys` 列表始终不返回明文

权衡：能登录管理台、又能拿到当前 Windows 用户权限的人，现在可以解出所有 Key。这与上游 OAuth 令牌的保护级别一致，但确实弱于"只存摘要"，README 的安全说明已同步改写。

### 2. 用量面板（「用量与日志」页）

新增 `GET /api/usage?hours=N`（1–720，默认 24），返回总量、按模型/账号/端点的分布，以及按小时（≤48h）或按天的时间序列。

- 4 张 KPI：总请求数、总 Token、消耗积分、平均耗时（含首字）
- 3 个环形图：模型分布、账号分布、端点分布（纯 SVG，未引入图表依赖）
- Token 趋势面积图：输入与输出双序列
- `requests` 新增 `endpoint` 列，记录 `chat`/`responses`/`messages`；旧数据为 NULL，按 `chat` 归类
- `requests` 新增 `cache_tokens` 列，从 `prompt_tokens_details.cached_tokens`、`prompt_cache_hit_tokens` 或 `cached_tokens` 提取；当没有任何上游缓存上报时，`cache_hit_rate` 返回 `null` 而非伪装成 0%

### 3. 界面配色统一为蓝白

原来整套界面是绿系（深绿主按钮、绿调文字与边框、绿色徽章）。现在按色相统一改到蓝色系，保留原有明度/饱和度以保证对比度不变：

- 115 个颜色值中 103 个做了绿→蓝映射（色相统一到 218°），红/橙/琥珀等语义色一律不动
- 品牌色手工提亮：主按钮 `#1e40af`、悬停 `#1d4ed8`、品牌图标 `#1e3a8a`、强调色 `#2f6bff`
- 图表跟着改：输入序列 `#2f6bff`、输出序列 `#0ea5e9`；环形图调色板换成蓝/青阶
- `index.html` 的 `theme-color` 与托盘图标同步改为蓝色
- 修订后复查：样式与组件中已无绿系色相（饱和度 >10% 的绿全部清零）

顺带修掉一个图表缺陷：**Token 趋势在只有一个时间桶时是空白的**——单点会让折线路径宽度为 0。现在单桶改用并列柱状图渲染，并保留峰值与请求数标注。

### 4. 兼容性修复：`store` / `metadata`

OpenAI 兼容客户端（如 DSH）默认会在请求体里带 `store`，此前被白名单拒绝为 `400 不支持的请求字段：store`，导致真实客户端完全无法调用。现在：

- `store=false` 与 `metadata` 被接受，并在转发上游前剥离——本服务不保存服务端对话，下发这两个字段没有意义
- `store=true` 仍返回 400，并给出可操作提示：没有任何服务端会话可以被取回，静默忽略等于说谎
- 类型不合法（`store` 非布尔、`metadata` 非对象）同样 400
- 其余未知字段的严格拒绝策略不变

### 5. 国内版上游兼容：历史结构修复

真实故障：客户端（DeepSeek Harness）请求 `kimi-k3.1-pro` 返回 `400 上游拒绝请求`。用账号凭据直连上游做探针后定位到三类拒绝（均已在实机验证）：

| 上游 code | 含义 | 触发条件 |
|---|---|---|
| `11148` | tool calls 与 tool results 不匹配 | assistant 的 tool_call 没有对应结果，或 tool 消息找不到对应调用 |
| `11151` | 某条消息 content 为空 | `content: null` 或 `content: ""` |
| `11-128` | Illegal API invocation from an unapproved channel | 出现 `developer` 角色 |

这类坏历史会被客户端**每轮原样重放**，导致整个会话持续 400。现在网关在转发前修复：

- `developer` → `system`（本就会做，现移入统一的 CN 归一函数）
- 孤儿 tool_call：assistant 消息整条「全有或全无」判断——只要有一个 id 配不上就摘掉整组 `tool_calls` 并保留正文；随后**重算存活 id**，再删除配不上的 `role: tool` 消息
- 空 content：统一补成单个空格（已验证上游接受）；assistant 带 `tool_calls` 时置为 `null`（同样已验证）

同时把国内版转发改为**字段白名单**（对齐参考实现的 passthrough 集合），不再把客户端 body 原样下发；`parallel_tool_calls` 等不在白名单内。

诊断改进：上游 4xx 现在会把上游自身的 `code`/`msg` 写入 `attempts.error`，并在返回给客户端的错误消息里附带一段（如 `（上游 11148 tool calls and tool results do not match）`），不再只留一个状态码。

**上游 400 一律重试一次**：实机对照显示同一份 payload 时而 400、多数 200（`11-128` 风控为间歇性），而按 code 精确匹配过于脆弱——运行时出现过未命中、重试未触发的情况。400 发生在生成之前且不扣费，因此改为：先尝试 `11150` 降档，其余任何 400 都在 0.8s 后用**完全相同的请求**重试一次，仍未通过则连同上游原文返回。每次重试会在 `jobs` 里留一条 `upstream_400` 记录（含上游 code），便于复盘。不修改任何请求内容，不做提示词改写。

### 6. 修复

- 加密备份恢复的版本校验写死为 `(1,2)`，schema 3 的备份会被误判"版本不兼容"；已放宽到 `(1,2,3)`，旧备份仍可恢复

### 7. 渠道拦截自动换号

上游 `11128`（`Illegal API invocation from an unapproved channel`）连续拒绝同一账号时，网关会在同一请求预算内切换到账号池中的另一个可用账号再试一次。Key 仍只结算一次；若没有备用账号，仍返回原始上游错误，便于诊断。

### 测试

后端 **104 项全部通过**（0.1.2 为 95 项），证据 `test-results/pytest-0.1.3.xml`。新增覆盖：密钥查看/轮换/旧 Key 无明文提示、用量聚合与端点归类、缓存 Token 提取与命中率、schema 2→3 迁移、备份恢复跨版本、`store`/`metadata` 接受但不下发、国内版字段白名单、孤儿 tool_call 与空 content 归一、上游错误详情入库、`11-128` 瞬时拒绝重试与持续拒绝时的原文透出。

---

## 0.1.2 变更记录

日期：2026-10-27（Asia/Hong_Kong）。基线为 0.1.1（80 项后端回归）。本次为功能增强，**数据库结构不变，仍为 schema 2**，现有账号、账号池、Key、日志无需迁移或重建。

## 兼容性影响

| 变更 | 类型 | 影响 |
|---|---|---|
| `/v1/chat/completions`、`/v1/responses`、`/v1/messages` 请求体上限 2 MiB → 32 MiB | 放宽 | 原有请求不受影响；管理 API 仍为 2 MiB |
| `POST /api/keys` 的 `models` 开始校验目录 | **收紧** | 传入目录外的模型名现在返回 400；之前任意字符串都会被保存 |
| `PATCH /api/keys/{kid}` 的 `enabled` 由必填改为可选 | 放宽 | 现在 `enabled` 与 `models` 至少给一个，空对象返回 400 |
| 上游返回 HTTP 400 + `code=11150` 时，同一账号降档重试一次 | 行为新增 | `attempts` 表会出现 2 行，本地预算仍只结算 1 次 |
| 模型能力 JSON 新增 `efforts` / `default_effort` | 新增字段 | 需要一次账号同步才会写入；在此之前该字段缺失，推理档位原样透传 |
| 新增 CLI 参数 `--allow-host` | 新增 | 可重复，用于让 TLS 反代的主机名通过 Host 校验；**不传时行为与 0.1.1 完全一致** |
| `GET /v1/models`、计费、调度、协议转换、凭据保护 | 不变 | — |

## 新功能

### 1. 删除账号池

`DELETE /api/pools/{pid}`。池内成员关系一并解除，账号本身保留。

- 默认池（id=1）不可删除：新建账号与 Key 的默认目标就是它，删掉会让默认值悬空
- 仍被 API Key 引用时返回 409，并告知有几个 Key 在用
- 被其他池当作备用池时，在同一事务内把引用置为 NULL，并返回 `detached_fallbacks` 数量
- 写 `pool_deleted` 审计

管理台：账号池卡片新增红色「删除」按钮，默认池按钮置灰并显示原因，删除前二次确认。

### 2. 删除 API Key

`DELETE /api/keys/{kid}`。删除即吊销，明文不可恢复。

- Key 不存在返回 404
- `reserved > 0`（有未结算请求）返回 409，避免账本落不了地
- 删除时把 `requests.key_id`、`ledger.key_id` 置 NULL，**历史日志与账本完整保留**，只是不再归属该 Key
- 写 `key_deleted` 审计，目标记录 Key 名称

配套修复：Key 在"鉴权通过"与"预占预算"之间被删除时，原先会在 `commit_reserve` 触发 `TypeError` → 500，现在返回 401「API Key 已被删除」。

管理台：Keys 表格每行新增「删除」，与「停用」并列；两者语义不同——停用可恢复且保留归属，删除不可恢复。

### 3. API Key 的模型范围

能力原本已有（空数组=池内全部），本次把它做成完整的控制台操作：

- 创建 Key 时用**勾选框**选择允许的模型（含别名），替代原来的逗号文本
- 每行新增「可用模型」按钮，可随时修改已创建 Key 的范围
- 表格新增「可用模型」列：`全部` / 具体模型 ID / `N 个模型`
- 服务端校验：名字必须在模型目录或别名表内，否则 400；自动去重；空数组表示不限制

强制点两处：`GET /v1/models` 只返回允许的模型；生成接口对名单外模型返回 403。

### 4. 推理档位（reasoning effort）

- 同步时解析上游 `reasoning.supportedEfforts` 与 `defaultEffort`（兼容数组与单档两种形态），写入模型能力
- 管理台「模型目录」显示 `档位 low / medium / high` 徽章，悬停显示说明

**不做预降档**：上游目录可能少报（实测 `deepseek-v4.1-flash` 声明 `["high"]` 却接受 `max`），预降档会把用户要的最高档砍掉。改为：请求原样发送，只有当上游明确返回 HTTP 400 + `code=11150`（invalid_reasoning_effort）时，才在同一账号上降一档重试一次。此时尚未产生任何内容，不属于重放。

刻意不处理的拼写：`off` / `disabled` 会被上游拒绝，`none` / `minimal` 反而会开启思考——这些都是上游语义，代理不擅自改写。

## 跨机访问（`--allow-host`）

默认仍只监听 `127.0.0.1`，且 Host 白名单只有回环四项，因此另一台电脑无法直接访问。**没有**添加 `--host` 参数——直接把 API 绑到局域网明文 HTTP 会让 Bearer Key 与全部对话可被同网段嗅探。

取而代之的是白名单开关，配合 TLS 反代使用：

```powershell
.\.venv\Scripts\python.exe main.py --data-dir data --allow-host desk.tailnet-xxxx.ts.net
```

- 参数可重复，支持 `*.example.com` 通配
- 自动归一：大小写、尾点、`:端口`、`[::1]:port`
- 服务仍只监听 `127.0.0.1`，TLS 与对外监听由反代（如 `tailscale serve`）负责
- 不传该参数时，白名单与 0.1.1 完全相同

详见 [README.md](README.md) 的「跨机访问」一节。

## 长上下文支持

- 请求体上限按路径分档：生成接口 32 MiB、`/api/restore` 64 MiB、其余 2 MiB。**精确匹配**，尾斜杠和 query 不会借用更大额度（`/v1/chat/completions/` 仍走 2 MiB）
- 生成超时改为 `connect 15s / read 600s / write 300s`：约 1M token 的 prefill 首字节可能等几分钟，原 120s 会被误判为网络错误
- 元数据调用（模型目录、余额、刷新、OAuth 轮询）单独固定 60s，避免卡住的余额查询占住同步锁

1M token 的 JSON 约 4–5 MiB，超出旧上限约一倍，这是本次放宽的直接原因。上游是否真的授予 1M 上下文取决于账号权益，代理不做保证。

## 健壮性

- 静态资源挂载改为同时要求 `assets/` 目录与 `index.html` 存在。之前只判断 `web/dist` 目录存在，构建被清空或中断时会在 `StaticFiles` 内抛 `RuntimeError` 导致启动失败
- `reserve` 增加 Key 已被删除的空行保护（见上文）

## 启动脚本

`start.ps1` 重写，解决"双击没反应"：

- 启动前先探测 `http://127.0.0.1:8792/healthz`，已在运行则只打开管理台并退出，不再启动第二个实例去抢数据目录锁
- **源码优先**：存在 `.venv` 时以 `main.py` 启动，源码目录不再被旧的 `dist\WorkBuddyHub.exe` 抢占；没有虚拟环境时才回退到 EXE
- 控制台保持打开并显示管理台地址；失败时以非零退出码返回，`start.bat` 的 `pause` 生效，不会闪退
- `start.ps1` 新增 `-NoBrowser`，`start.bat` 现在会转发参数（`start.bat --port 8800`）

注意：`dist\WorkBuddyHub.exe` 仍是 0.1.1，需要重新打包才会包含 0.1.2 的改动。

## 测试

后端 **95 项全部通过**（0.1.1 为 80 项）。当时生成的 `test-results/pytest-0.1.2.xml` 已被 0.1.3 的运行覆盖，现行证据见 `pytest-0.1.3.xml`。新增 15 项：

- 池删除：默认池拒绝、不存在 404、被 Key 引用 409、备用链自动解除、成员清理、账号保留、删除后仍可正常调用
- Key 删除：历史分离且行数不变、吊销后 401、同名可重建、进行中请求 409、结算后可删
- 模型范围：未知模型 400、限制后目录收缩、名单外 403、清空恢复全部、`enabled`/`models` 独立、空 PATCH 400
- 长上下文：3 MiB 在三个生成路径放行、尾斜杠仍 413、管理路径仍 413、33 MiB 拒绝
- 推理档位：六种拼写原样透传不被改写、`11150` 场景降档一次且只结算一次
- 跨机访问：`--allow-host` 生效、大小写与尾点归一、未列入的 Host 仍 400、非法参数拒绝
- 兼容性：原有 0.1.1 的全部回归继续通过；`test_body_limits_with_query_trailing_slash_and_chunking` 中"`/v1/chat/completions` 3 MiB → 413"的旧断言按新策略改为 200，其"query 不影响分档、分块体累计、管理接口仍 2 MiB"的验证意图保留

本轮**没有**重跑浏览器端到端与 EXE 检查，`TEST-REPORT.md` 中的 80 项与 5/5 浏览器、5/5 EXE 证据属于 0.1.1 基线；0.1.2 的 EXE 尚未构建。

## 如何验证

```powershell
..\.venv\Scripts\python.exe -m pytest -q --junitxml=test-results/pytest-0.1.2.xml
npm.cmd run build --prefix web
..\.venv\Scripts\python.exe main.py --check
..\.venv\Scripts\python.exe main.py --data-dir data
```
