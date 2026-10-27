# WorkBuddy Hub 0.1.1 验收报告

验证日期：2026-10-26，Asia/Hong_Kong。运行环境：Windows 11 x64（26100）、Python 3.12.14、系统 Microsoft Edge、Node.js。代码与 EXE 均在当前 Windows 电脑构建并运行。

本次 0.1.1 审查修复重新运行后端、浏览器与 EXE 检查；此前真实上游调用保留为 **0.1.0 基线证据**，本次没有新增付费生成请求。审查逐项结论见 [REVIEW-RESPONSE.md](REVIEW-RESPONSE.md)。

## 结果

| 验证层 | 结果 | 证据 |
|---|---|---|
| 后端单元、集成与真实 TCP 断连测试 | **80 / 80 通过**，48.44 秒 | `test-results/pytest.xml` |
| Edge 浏览器端到端测试 | **5 / 5 通过**，28.8 秒 | `test-results/browser.json`、`overview.png`、`mobile.png` |
| 最终 Windows EXE 检查 | **5 / 5 通过** | `test-results/exe.json` |
| 前端生产构建 | TypeScript 类型检查与 Vite 构建通过 | `web/dist/` |

这些证据使用隔离临时数据和协议夹具；真实账号验收记录、请求 ID、余额和运行机器信息不随公开仓库发布。

## 0.1.1 新增回归

原 45 项回归继续通过，新增 **35 项**覆盖：真实 SQLite 写锁下已有 SSE 与事件循环继续推进、预留/结算期间两次取消、并发 Key 名额与备用池环检测、坏密码摘要、异常 expiresIn、会话容量与标识边界、请求体路径/分块限制、迁移失败回滚、2,000 条请求的重启恢复、reasoning 变体与别名目录、多轮/并行工具参数与缺少 ID 的错误路径。

`test-results/review-regressions.xml` 是从最终 80 项 JUnit 报告中提取的 35 项审查回归子集，不是额外重复运行的独立测试次数。EXE 证据记录 0.1.1 版本及其 SHA-256；当前数据继续沿用 schema 2。

## 四阶段对应关系

| 阶段 | 已实现能力 | 参考与新增设计 | 验证方式 |
|---|---|---|---|
| 1：国内版连接器 | 官方扫码授权与轮询、凭据导入/刷新、模型能力同步、Chat 普通与 SSE、当前积分 summary/paid/free、新旧账单兼容、精确积分与到期时间 | Switch2api 的 OAuth/edition/credits；wb2api 的请求、SSE 和账单。新增 Python 连接器与严格协议边界 | 真实扫码入库、刷新、余额和模型同步、Chat、Responses、Messages、指定函数调用均通过；模拟刷新单飞与失效、积分精度、快照保留、协议异常 |
| 2：核心后端 | SQLite 迁移、账号池及显式备用池、均衡/临期调度、会话绑定、账号/Key 并发、冷却、Key 模型范围/有效期/RPM/请求预算、逐请求与尝试账本、密码和凭据保护 | 参考 wb2api 租约/会话调度与 New API 的连接器/预算分层；数据库与事务实现独立编写 | 池隔离、备用池与循环拒绝、临期策略、并发释放、预算预留/结算、刷新竞争、401 一次刷新、429/403/5xx 禁止换号重放、未知用量为 NULL |
| 3：管理台 | 总览、账号、账号池、模型、Keys、日志、系统设置七页；二维码、账号详情、积分包/趋势、别名/调用测试、日志筛选/CSV、后台任务与审计、配置/密码/备份 | 参考 workbuddy-manager 信息架构；React 中文交互与布局独立设计 | Edge 执行七页导航、账号导入/同步、池编辑、别名、Key、HTTP 调用、日志、CSV、备份恢复、扫码模拟；1440 px 与 390 px 视觉检查 |
| 4：稳定与 Windows 交付 | 单实例、断流/取消清理、崩溃恢复结算、请求体限制、Cookie/CSRF/Origin/Host 保护、日志脱敏、DPAPI 凭据、密码加密备份/恢复、托盘、EXE、启动/构建/打包脚本 | 新增实现；MIT 适配器保留原许可证；PyInstaller 打包 | 真实 TCP 客户端断连释放上游、提前取消幂等、崩溃重启、错密码备份失败、恢复到新目录、EXE 自检/资源/登录保护/单实例、ZIP 完整性与排除账号数据 |

详细参考固定提交与源码复用清单见 [REFERENCES.md](REFERENCES.md)。

## 真实账号验收

真实账号验收脚本需要用户在本机完成官方授权后手动运行。为保护账号隐私，真实账号的请求 ID、余额、模型清单、凭据和运行日志不提交到仓库；公开报告只保留脱敏后的测试结论。

## 0.1.0 原始联调记录

1. Windows 备份临时 SQLite 连接需要显式关闭，否则清理文件时出现文件占用。修复后加密备份、错误密码与恢复验证通过。
2. 管理台字段标签未正确关联表单控件；修复可访问名称后，五项浏览器测试全部通过。
3. 真实上游使用 `supportsToolCall`，并把图像/视频模型混入目录；已兼容工具字段并按类型标签排除非聊天模型。
4. 当前积分 summary 返回 `Packages`，明细返回 `Accounts`；明细要求产品码、免费包要求日切片范围。已修复并更新夹具，最终真实同步不再依赖 legacy 回退。
5. 国内版不接受对象形态的指定函数。第一次验收返回 HTTP 400，没有自动重放，未扣本地请求预算；修复为函数名字符串后，三种协议回归通过。
6. 打包环境的 TLS 策略文件未被自动收集导致 EXE 无法初始化 HTTPS。构建脚本现保留该文件，未关闭 TLS 验证；最终真实 HTTPS 调用成功。
7. 系统 Windows PowerShell 对无 BOM 中文脚本的解析失败；脚本保存为 UTF-8 BOM，完整构建已执行通过。实例锁初次读取也移除，第二实例现在清晰退出而非抛出 traceback。

## 重复验证

在项目目录执行（当前工作区 Python 环境在上一层；独立源码环境使用 `.\.venv\Scripts\python.exe`）：

```powershell
..\.venv\Scripts\python.exe -m pytest -q --junitxml=test-results/pytest.xml
npm.cmd run test:e2e --prefix web
..\.venv\Scripts\python.exe scripts/verify_exe.py --data-dir data
```

真实调用脚本会消耗账号权益，只在需要重新验收时执行：

```powershell
..\.venv\Scripts\python.exe scripts/live_acceptance.py --account 1 --model deepseek-v4.1-flash --refresh --password-file data/initial-admin.txt
..\.venv\Scripts\python.exe scripts/live_release_check.py --account 1 --password-file data/initial-admin.txt
```

## 验证范围与限制

- 真实验收只有一个国内版个人账号和一个模型；多账号竞争、故障路由、不同积分结构用隔离夹具覆盖，没有宣称 32 个模型逐一实测。
- Responses/Messages 的 SSE、图片传递和部分工具回合用模拟上游验证；真实 SSE 已验证 Chat。三种接口的支持字段见 README，Responses 是无状态文本/函数子集，不提供完整 OpenAI 托管工具或历史存储。
- Windows EXE 在当前电脑运行成功，包含托盘；未进行其他电脑/Windows 版本或托盘每个菜单的人工点击测试，也没有 Authenticode 签名。
- 加密备份执行了新目录恢复和凭据解密验证；未在另一台物理电脑测试。只有一个后端 worker，尚未做生产容量或长时间压力测试。
- 依赖与上游协议会变化。该交付证明本次实现和上述验证路径可用，不承诺未经测试的全部模型、全部第三方客户端或未来上游版本兼容。
