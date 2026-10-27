# 开源参考与源码归属

研究及实现日期：2026-10-26（Asia/Hong_Kong）。参考仓库源码固定到以下提交；不会在运行时下载上游 Python 模块，也不会启动外部项目的服务。

| 项目 | 固定提交 / 来源 | 吸收与复用 |
|---|---|---|
| [jzh-1228/wb2api](https://github.com/jzh-1228/wb2api) | `2f23d96cf9a7e961691ee026902fdb3125f586a4` | 参考 `converter.py` 的国内版请求、刷新和 SSE，`admin/pool.py` 的并发租约/会话绑定，`admin/billing.py` 与 `admin/credits.py` 的积分口径。复用两个纯协议适配模块，见下文。 |
| [cv-superding/WorkBuddy-Switch2api](https://github.com/cv-superding/WorkBuddy-Switch2api) | `4ea798a38886bb0172e180121d142fa44bbcb109` | 参考 `crates/wb-switch-core/src/modules/oauth.rs`、`edition.rs` 的国内版 OAuth 状态/轮询/域名，`credits.rs` 的账单接口及归一化。用 Python 独立实现。 |
| [ithtelab/workbuddy-manager](https://github.com/ithtelab/workbuddy-manager/blob/main/docs/development.md) | 2026-10-26 阅读其架构及 README | 参考管理端与协议接入分层、账号分组、Key 权限、日志/用量信息架构。没有复制该项目源码。 |
| [QuantumNous/new-api](https://github.com/QuantumNous/new-api/blob/main/relay/channel/adapter.go) | 2026-10-26 阅读其适配器接口与 README | 参考连接器边界、模型映射、路由与预算分离。没有复制或链接 New API 源码。 |

## 实际复用的 MIT 源码

`hub/vendor/responses_adapter.py` 和 `hub/vendor/anthropic_adapter.py` 是 wb2api 上述固定提交中的同名文件，原样保留；对应许可证完整保存在 `hub/vendor/LICENSE-wb2api`。

Copyright (c) 2026 HanHan666666；Copyright (c) 2026 Chris。

`hub/providers.py` 中产品码列表、OAuth 请求结构与账单解析口径来源于上述两份 MIT 项目；保留 `hub/vendor/LICENSE-switch`（Copyright (c) 2026 wb-switch / cv-superding）。其余管理台、存储、凭据保护、调度、HTTP 接口、严格协议边界、备份、桌面启动器与测试是本项目新增实现。

原适配器有较宽松的输入处理。`hub/protocols.py` 在外层先校验支持范围：拒绝不能可靠映射的字段/文件/内置工具，避免静默丢失用户输入；没有引入参考项目的提示词改写、历史压缩、指纹修改、设备身份生成、成长奖励或自动注册逻辑。

## 真实联调发现与回归

- 实际模型目录使用 `supportsToolCall`；兼容该字段，并保留 `supportsTools` 的兼容分支。
- 上游目录包含图像/视频模型。依据 `text-to-image` / `text-to-video` / `image-to-video` 标签排除这些聊天端点不支持的模型。
- 当前积分 summary 返回 `Packages`，paid/free 明细返回 `Accounts`。明细请求要求 `PackageCodes`，free 还要求日切片范围；模拟协议夹具按真实结构修正，防止测试固化错误假设。
- 上游实际用量字段为 `usage.credit`；系统同时支持单复数形式。
- 实测指定工具对象被国内版上游拒绝（HTTP 400）；参考 Switch2api 的 `proxy.rs`，把已校验的指定函数转换为函数名字符串，保留强制调用语义。Chat / Responses / Messages 均有回归测试，最终 EXE 的真实函数调用已通过。国内版将 `developer` 角色映射到其支持的 `system`，保留文本内容与顺序。
- 到期时间统一换算为 UTC 秒；上游没有时区的账单时间按 UTC+8 解析。`CycleCapacityRemainPrecise` 优先于整数与累计余额；周期结束早于明显占位到期时间时使用周期结束。

## 许可证

本项目新增代码采用 MIT。发布源码与二进制时保留本文件及 `hub/vendor/` 中的许可证；第三方依赖各自适用其许可证。二进制构建命令已包含上述两份许可证。
