# ret2shell 分支已知待修问题报告

> 依据 2026-08-29 MoeCTF 2026 全自动解题实战（平台 27/125 解出）压测得出。
> 分优先级与层次列出；"已修"项列入回归清单。

## P0 — 成员答题纪律（agent 核心缺陷：上报不自证）

实战中全部出现过，直接导致错误提交、浪费共享提交配额（10 次/5 分钟）：

| # | 现象 | 案例 |
|---|------|------|
| 1 | 诱饵 flag 当答案，且重跑再犯 | 3-4 命令提示符 `moectf{keep_trying_maybe_next_time}` 判错两次 |
| 2 | OCR/噪声"过度美化"再上报 | 半部电台把 `ma1n-way-r4dio-ama/eur5` 改写成 `main-way-radio-amateurs` |
| 3 | 结构非法的 flag 直接上报 | Polyglot 拼出内含 `}{` 的环形窗口 |
| 4 | 裸字符串当 flag 上报 | Ultra Potato 提交无 `{}` 的随机串 `Fnrr68qU...` |
| 5 | 漏字符/漏碎片 | 奇怪的APP 漏 AndroidManifest.xml 的 base64 段；让我们说中文漏"诶"（且音译映射错误：役=e 非 y、克诶=K 两字连读、外=y） |

**待修方案（未实施）**：
- 提交闸门：提交前结构校验（必须是 `moectf{...}`、内部无 `{}`、非空）；非法候选不消耗配额，直接记 rejected-flag 反馈打回。
- runtime_notes 上报纪律：逐字符保真（禁止 OCR 美化）、碎片拼接必须闭合且 leet 可读自检（4→a、@→a、$→s、0→o、7→t，结果须是通顺英文短语）。
- 去重键改为 (challenge, flag)，避免"重跑找到新 flag 却被旧记录挡住不提交"（代码已改一半未部署）。

## P1 — 反馈回流的服从度与记忆污染

- **黑名单服从度未验证**：proj_018 重跑时黑名单已在上下文，成员仍收敛到同一个被拒 flag。hint 通过 hints 表才真正生效——rejected-flag 通道只注入 flag 字符串清单，hint 正文不进上下文。需要验证/加强注入效果。
- **记忆污染无清理策略**：旧 incarnation 的错误结论（如 3-4 记忆库里"exploit: Flag: moectf{keep_trying...}"）会背书重蹈覆辙，本次靠手工删除。待修：reopen 时自动失效该项目的 solution/exploit 类记忆条目。
- hint 类信息缺正式通道（本次是 operator 手工 POST /hints 绕过）。

## P1 — 提交链路与状态一致性

- `flags` API 的 `submitted` 字段语义漂移：Ultra Potato 显示 False 实际提交过且判错；completed ≠ 平台通过，UI 无"平台判定"列，造成"显示做出来了实际没过"的误读（proj_018 假完成、proj_103 记录过期两例）。
- 手动补交成功后本地 flag 记录不更新（proj_103 仍挂旧残缺 flag）。
- 提交循环遇 429 直接 `break`，同轮剩余 flag 漏提交（下一轮才补）。

## P1 — 编排与基础设施

- **导入重复项目根因未修**：客户端超时重试 + 服务端滞后建项，每次全量导入稳定多出 ~15 个 dup，需手工去重（已发生 3 次）。
- **僵尸槽位不自愈**：强杀循环/异常退出后 TaskSlotLimiter 槽位不释放，新题全部排队（本次靠 stop-all 解锁）。
- 日志在任务结束才批量落盘，运行中无法观测成员行为（误判"没进度"多次）。
- `wp_failed`（写 WP 失败）状态与 completed 的关系未定义，观感像任务失败。
- SQLite WAL × Windows bind mount 跨 OS 锁死（已修 journal 兜底；纪律：宿主机一律走 API，不碰 .db 文件）。
- config.yaml 与 limits.yaml 的 `setdefault` 优先级陷阱（并发曾悄悄从 8 退化为 5）。

## P2 — LLM 网关

- DeepSeek 决策输出在 4096 token 处截断 JSON，多项目反复 degrade（重试可自愈但浪费）；可对 deepseek 提决策预算或做截断检测重试。
- **余额耗尽无预警**：402 Insufficient Balance 时只是默默 degrade 重试，缺一个"网关不可用，全批停止"的熔断信号（本次 8 任务空转 10 分钟才发现）。
- key 轮换仍全手工（GLM 余额 → Kimi 频限 → DeepSeek 余额，一天换三次）。
- Kimi 强制推理吃光决策预算导致空输出（已修 16384）；各家怪癖由 adapters 兼容层覆盖。

## P2 — 题目池遗留（恢复运行时的作业清单）

- 105 个 stopped 项目待跑（按 platform_targets 排序），**64 道实例题从未实战**（实例串行策略 0 次触发）。
- proj_018 让我们说中文：解码 hint 已写入 hints 表（音译映射修正 + leet 自检法），重跑即可验证。
- 半部电台（SSTV）/Polyglot（环形拼接）/Ultra Potato：正确 flag 均未由 agent 得出；黑名单已就位。
- 平台累计提交配额消耗需注意：31 次提交中 6 次浪费在被拒 flag 上。

## 已修复、建议回归验证

1. `IPC_DB_PERSIST=1` 持久化（graph.db/memory.db 落盘）——重启/重建不再丢状态。
2. 启动时对照平台已解名单跳过 + completed-but-unsolved 自动 reopen + 旧提交记录清理。
3. rejected-flag 记忆注入决策上下文（黑名单）。
4. Kimi 决策预算 16384；网关 4xx 响应体透出。
5. SQLite journal 模式降级兜底。
6. 排序数据 platform_targets 挂载、并发 5→8 修正。
