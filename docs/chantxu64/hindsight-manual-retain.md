# Hindsight 手动保存会话：`/retain`

状态：fork-only，本地功能。

## 作用

`/retain` 用于把当前 Hermes 会话 lineage 中，Hindsight provider 每轮已经整理好的 retain turn，手动提交到 Hindsight。

关键点：手动保存不再从 Hermes 原始 SessionDB transcript 反推内容；Hindsight provider 在 `sync_turn()` 生成自动 retain payload 时，会把同一份 turn JSON 持久化到独立 SQLite 文件。若 MemoryManager 提供完成后的 `messages` transcript，`sync_turn(..., messages=...)` 会先从 transcript 重建干净 retain turns，以保留 gateway interrupt / 多用户消息同一完成 turn 中更早的真实用户消息。

## 本地持久化文件

独立文件：

```text
$HERMES_HOME/hindsight/retain_turns.sqlite3
```

不写入 Hermes 主 `state.db`。

表：

```text
hindsight_retain_turns
hindsight_retain_submissions
```

每行保存：

- `bank_id`
- `session_id`
- `parent_session_id`
- `retain_document_id`
- `turn_index`
- `turn_json`
- `created_at`
- `active`
- `rewound_at`

`turn_json` 是自动 retain 同源格式：

```json
[
  {"role": "user", "content": "User: ...", "timestamp": "..."},
  {"role": "assistant", "content": "Assistant: ...", "timestamp": "..."}
]
```

Gateway 可提供平台消息号时，user message 还会带内部
`_hermes_source_occurrence_id`。这个标识持久化同一平台事件的身份；它不按
文本去重，因此两个消息号不同、文字相同的真实消息仍是两个 occurrence。

`hindsight_retain_submissions` 在请求排队前记录 exact outbound
`content_json`，并在请求返回或失败后记录 `succeeded` / `failed`、完成时间
和错误。进程在完成前中断时，行保留为 `queued`，不会伪装成成功。

通常一行对应一个 `user → assistant` turn。内部 async completion 触发后如果产生用户实际看见的正式 assistant 结果，该结果会单独保存为 assistant-only event：

```json
[
  {"role": "assistant", "content": "Assistant: ...", "timestamp": "..."}
]
```

这避免把内部 runtime payload 写入 Document，也不虚构一条用户原话。

## 命令

```text
/retain
```

只保留这一个用户手打命令。没有其它别名。

## `/new` / `/reset` 前自动保存（可选）

在当前 profile 的 `$HERMES_HOME/hindsight/config.json` 中可启用：

```json
{
  "retain_on_new": true,
  "retain_on_new_timeout_seconds": 30
}
```

- `retain_on_new` 默认 `false`；不会因为 `auto_retain=false` 而自动开启，也不会把每轮自动 Retain 打开。
- `retain_on_new_timeout_seconds` 默认 `30` 秒、最小 `0.1` 秒，是 pending memory drain、persisted payload 重建、Hindsight API capability probe 与 Retain API 请求合计使用的一个总超时预算；capability probe 自身的网络 timeout 也不会超过当时的剩余预算。
- 启用后，CLI、Gateway 聊天平台和 TUI 的显式 `/new` 及其 `/reset` 别名，都会先等待当前 persisted session lineage 的 Retain API 请求成功返回，再创建新会话。
- TUI 在 Retain 与新会话建立期间关闭 session boundary；此时输入的普通 prompt 只进入现有本地队列，不会提交到旧 session。Retain 失败时队列回到旧 session 继续处理，成功时由新 session 接续处理。
- Retain 失败、超时、Provider 不可用或缺少同步门禁能力时，`/new` / `/reset` 失败关闭：旧会话不结束、不旋转、不清空，并向用户显示错误。
- API 请求成功返回表示 Hindsight 已确认接收请求；当 Hindsight 配置为异步处理时，不把后续服务端提取完成冒充为同步完成。
- 没有 persisted turns 时属于正常成功边界，可以继续创建新会话。
- 手动 `/retain` 保持原来的非阻塞提交行为；该开关只改变显式 `/new` / `/reset` 的 session boundary。

## 行为说明

- 每个完成 turn 的 `sync_turn()` 都会先把同源 retain payload 写入 `hindsight/retain_turns.sqlite3`。
- 上下文压缩前，Hindsight `on_pre_compress(messages)` 会用同一套 transcript 重建规则把当前可保留 turns 快照进本地 ledger。快照本身不单独抬高远端 flush 水位；`on_session_switch` 也不会把尾部仅 user 的 orphan 远程提交。压缩后的窗口即使只剩 `继续` + 最终答复，也会合并到该快照上，Document 从原始首句开始。orphan user 补全优先匹配 `_hermes_source_occurrence_id`（两侧都有且不同则不合并）；无 id 时允许同文 + 补全 assistant（可带 rehydrate 后的不同时间戳）。
- 当 `sync_turn()` 收到 `messages` transcript 时，优先从完整 transcript 构建 retain turns，而不是只使用最终 `user_content` / `assistant_content` 标量对。这样 gateway 中一个长任务先收到用户 A、后被用户 B 打断并最终完成时，persisted document 仍从 A 开始。
- Gateway 初始消息与排队 follow-up 都把各自的干净 user text、平台时间和平台消息号传到当前 Agent turn；StateDB 的 `platform_message_id` 与 persisted retain user occurrence 使用同一来源标识。重启、压缩或 replay 改变本地时间戳时，同一非空 occurrence id 仍只保留一次；不同 occurrence id 的相同文本不会合并。
- 多模态同一消息的原生 image part 与 Gateway 简化文本只生成一个 retain occurrence；payload 不保存 `data:image/...;base64`、本地图片路径或像素数据，保留规范化的 `[Image attached]` 占位；原消息已有的安全 `http(s)` 图片 URL 作为回源引用保留。
- transcript replay 写入前会先镜像 `retain_turns.sqlite3` 中当前逻辑 document 的 `active=1` rows 到内存 buffer；provider 重启或压缩切换后再次收到 full transcript 或只含尾部的压缩窗口时，通过 role/content 锚点识别已持久化 turns，只把新出现的 async assistant-only events 按窗口顺序合并进完整历史。合并会软停用旧 rows、保留已匹配 root/child rows 的 session 归属，并写入一份顺序正确的新 active sequence；下一次自动 retain（包括未到阈值就发生 session switch 的旧 buffer flush）强制使用 `replace`，避免把窗口以 `append` 再重复一次。重放中的历史消息若没有来源 timestamp，会保持时间未知，不能通过新生成的 `now()` 冒充新事件；仅本次 `sync_turn()` 真正完成的 final Assistant 可补当前时间。两个稳定锚点之间可按结构恢复缺时间的 assistant-only event，但无锚点窗口不能把缺时间消息当成新尾部。带完整稳定时间戳、且明确晚于旧 canonical candidates 的同文本事件仍视为真实重复并保留。旧 rows 的 `retain_document_id` 为空时，通过已解析 lineage 软停用。
- transcript 构建会过滤 tool output、assistant tool-call stub、`[Recent Summary ...]`、`Operation interrupted:` 通知、空 assistant 消息和同一 user segment 中的中间 assistant 草稿。真实 user segment 只保留最后用户可见 assistant；async completion 后的最后一个 eligible 用户可见正式 assistant 结果按原顺序保留为 assistant-only event。async marker 前若存在 orphan real user，会先单独保存该 user，避免把 async result 错配给它。
- 2026-07-10 起额外过滤运行时合成注入：`[Session Arc Summary ...]`、`[Durable Summary ...]`、`[Depth-N Summary ...]`、`[Current user objective preserved from compacted history]`、`[Your active task list was preserved across context compression]`、`[Externalized payload: ...]`、`[ASYNC DELEGATION BATCH COMPLETE ...]`、`[ASYNC DELEGATION COMPLETE ...]`、`[OUT-OF-BAND USER MESSAGE ...]`，以及位于用户内容开头的 `[Note: model was just switched ... Adjust your self-identification accordingly.]`。模型切换提示只从纯文本开头或多模态消息的首个 text part 删除完整固定 marker，后面的真实用户原话和全部非文本 part 继续保留。2026-07-16 起 async completion 只丢内部 payload，保留其后的用户可见 assistant 结果；其它纯噪声消息整段丢弃，夹带真实用户原话时只保留残留真实文本。assistant 侧同类摘要/interrupt 也不入档。`/retain` 提交前会对历史 `turn_json` 再清洗（clean-on-retain），并用相同规则保留历史 async visible assistant。标量 `sync_turn(user, assistant)` 走同一规则。过滤按 marker，不按业务词。
- retained turn rows 带 `active` 标记；正常写入为 `active=1`。
- `auto_retain=true` 时，自动提交逻辑仍按原机制运行。
- `auto_retain=false` 时，只写本地 SQLite，不自动提交到 Hindsight。
- `/retain` 从当前 active `session_id` 出发，优先解析该 session 的 `retain_document_id`，并读取同一个 logical document 下的 persisted turns。
- 旧数据没有 `retain_document_id` 时，仍可回退到 `parent_session_id` lineage；回退查找会忽略后续空 parent row，避免空 parent 覆盖早先非空 parent。
- Gateway `/retain` 使用和普通消息相同的 `SessionStore.get_or_create_session(source)` 解析当前 `session_id`；因此 `/resume` 或 gateway 重启后，即使还没有 cached agent，也能识别当前会话。
- 提交顺序是 root parent → ... → current child。
- 提交内容来自 Hindsight provider 自己持久化的 turn payload，不包含 tool output、tool-call stub、内部推理或压缩 summary。
- 不创建 `manual-session:*` 文档。

## 与 upstream append retain 的差异

2026-06-09 合并 upstream `09d66037f` 时，官方用 `_last_retained_turn_count` 作为 append retain 的简单 watermark，目标是避免 append 模式重复发送整个 session。

本 fork 不采用该独立 watermark。当前实现保留 `flush_retained_turns()` 的 queued/flushed/pending/generation 状态机：

- append-capable API：只发送 `_last_queued_flush_count` 之后的新 turns。
- legacy / overwrite API：仍发送完整 session，避免覆盖式写入丢历史。
- pending append job 失败时可回滚 queued watermark。
- session switch 用 generation guard 防止旧 pending job 污染新 session。
- `sync_turn()` 必须先持久化 turn；`auto_retain=false` 时仍只写本地 SQLite，供 `/retain` 使用。

## `/undo` 与 manual `/retain`

`/undo N` 的语义是撤销当前 active conversation 的最后 N 个用户 turns。为了让 manual `/retain` 不再保存这些已撤销内容，CLI、Gateway、TUI `/undo` 都会通知 memory provider 的 rewind hook。

Hindsight 收到 rewind 后：

- 在 `hindsight_retain_turns` 中按真实 user turn 计数回退，把对应后缀 rows 标为 `active=0` 并写入 `rewound_at`；若后缀含 assistant-only async result，也随所属回退后缀一起排除。
- `/retain` 读取 persisted turns 时只读取 `active=1` rows。
- 不硬删除 rows，保留本地审计能力。
- 截断当前 provider 的内存 retain buffer，并重置 queued/flushed/pending/generation 状态。
- rewind 场景不会执行普通 `on_session_switch()` 的 flush-on-switch，避免 `/undo` 自己把即将撤销的 buffered turns 推到 Hindsight。

边界：如果某些 turns 在 `/undo` 前已经通过 `auto_retain=true` 成功提交到远端 Hindsight，本地 rewind 不能保证远端删除；当前保证的是后续 manual `/retain` 不再从本地 persisted store 提交这些 inactive turns。

## 压缩、parent session 和 retain document

如果一次用户视角会话因为压缩形成干净 parent 链：

```text
A -> B -> C
```

在 C 中执行 `/retain` 会读取并提交：

```text
A 的 persisted retain turns
+ B 的 persisted retain turns
+ C 的 persisted retain turns
```

因此不需要赶在压缩前手动 `/retain`。

如果 SessionDB / gateway 状态把压缩后的 continuation 记录成 sibling：

```text
A
├─ B
└─ C
```

provider 写入 turn 时仍会让 B/C 继承同一个 `retain_document_id=A`。在 C 中执行 `/retain` 时，读取的是同一个 `retain_document_id` 下的 rows，而不是只沿 C → A 的 parent 链，因此不会漏掉 B。

## document_id / bank / metadata

默认使用 logical root retain document：

```text
root session: retain_document_id = session_id
child session: retain_document_id = parent retain_document_id
document_id = retain_document_id
```

提交到哪个 Hindsight Bank 只看 `/retain` 执行时的当前配置：

```text
bank_id = current configured bank
```

本地 `retain_turns.sqlite3` 里的历史 `bank_id` 不参与读取过滤；它只是当时写入时的记录，避免因 `hermes` / `Hermes` 这类大小写或配置变化丢 turn。

手动 `/retain` 的 item 保持干净，只提交：

```json
{"content": "...", "context": "..."}
```

不额外塞 `metadata`、`tags`。

## 重复提交

当前实现不在 retain 成功后清理 `retain_turns.sqlite3`。手动 `/retain` 每次都从
active persisted turns 重建完整 logical document，并在 API 支持显式更新模式时
使用 `replace`；不会把完整文档按 `append` 重复叠加。自动 retain 的正常增量路径
仍按 append watermark 发送新 turns。

## 返回结果

常见返回：

- `Buffered session turns queued for retain.`：当前 session lineage 的 persisted turns 已排队提交。
- `No persisted turns to retain.`：本地 retain turn store 中没有可提交 turn。
- `Hindsight memory provider is not active for this session.`：当前会话没有可用 Hindsight provider。
- `Failed to retain session: ...`：提交过程抛错。

## 验证覆盖

相关测试覆盖：

- `/retain` Gateway handler 可从 cached agent 找到 Hindsight provider。
- `/retain` Gateway handler 在没有 cached agent 时，会按普通消息路径从 `SessionStore` 解析当前 resumed/restarted session，并按当前 `memory.provider=hindsight` 加载 provider。
- Gateway `/retain` 调用 provider 的 persisted lineage retain，而不是读取原始 SessionDB transcript。
- CLI `/retain` 调用 provider 的 persisted lineage retain，而不是读取原始 SessionDB transcript。
- 即使 SessionDB 中存在 LCM/压缩生成的 `[Recent Summary ...]` 消息，`/retain` 也不会把它当作 Hindsight Document 内容源。
- `sync_turn()` 会持久化和自动 retain 同源的 turn payload。
- 平台消息号会贯通 Gateway 当前消息、排队 follow-up、Agent current user turn、StateDB `platform_message_id` 和 Hindsight persisted occurrence；provider restart 后同一 occurrence 即使时间戳变化也不会重复，而不同平台 occurrence 的相同文本仍分别保留。
- 多模态 native/simplified 双表示只保留一个 turn，并验证提交 payload 不含 base64 或本地图片路径。
- manual persisted-lineage、transcript compatibility、正常自动 flush 与 session-switch flush 四条 document-bearing 提交路径都会写 exact payload submission ledger，并在成功/失败后结束 `queued` 状态；进程在结果前中断时保留 `queued`，供监控明确判为证据未决。
- 监控按成功账本顺序重建提交代次：`replace` 重置完整代次，`append` 只把该 delta 接到上一成功代次，空模式按旧 API 的完整覆盖处理；未知模式或畸形 payload 使后续 append 保持未决，直到明确 reset。远端 `original_text` 必须与某一可证明代次语义一致，否则保持 unresolved，不用 active rows 或时间邻近猜测。
- `sync_turn(..., messages=...)` 会通过完整流程测试验证：gateway interrupt / 多用户消息同一完成 turn 时，Hindsight Document `original_text` 从真实第一条用户消息开始，而不是从后来的纠偏消息开始。
- async completion 回归会验证内部 payload 不进入 persisted/manual retain，最后一个用户可见 final assistant 以 assistant-only event 保留，prior orphan user 不会被误配，并且后续真实 user/assistant 顺序不变；clean-on-retain 对历史 dirty async turn 使用相同规则。Restart/partial replay 回归还验证 pre-fix 中间缺口会合并进完整 persisted history，软替换本地 active rows，自动 retain 使用 `replace`，不会产生 `old + replay window` 重复乱序；缺来源时间的历史 Assistant 不会被刷新为当前时间或在连续重放中再次插入，稳定时间证明为后来发生的相同序列仍保留，两个稳定锚点之间缺时间的 assistant-only event 仍能恢复。
- Manual `/retain` 会按 `retain_document_id` 聚合同一压缩 logical document；即使 B/C 在 SessionDB 中表现为 siblings，也能从 C retain 到 A+B+C。
- 旧 row 后续写入空 `parent_session_id` 时，lineage fallback 会使用早先非空 parent，而不是被空 parent 截断。
- local `bank_id` differences in `retain_turns.sqlite3` do not exclude persisted turns; `/retain` submits all matching session lineage turns to the currently configured Hindsight bank.
- `/undo N` counts the current session's last N real user turns, marks their persisted suffix inactive (including trailing assistant-only async results), and manual `/retain` skips inactive rows while still preserving compression siblings that share the same `retain_document_id`.
- Hindsight rewind handling does not run flush-on-switch, so `/undo` does not enqueue stale buffered turns as a side effect.
- Manual `/retain` submits a clean item containing `content` and configured `context`, with no extra metadata/tags.
- 没有 persisted turns 时不提交。
- `get_tool_schemas()` 不暴露 `hindsight_retain_session`。
- Slack native slash slots under the 50-command cap preserve canonical `/debug` despite fork-only `/retain`; low-frequency `/blueprint`, `/topup`, `/disk_cleanup`, and `/lcm` remain reachable through `/hermes <command>` on Slack.
- legacy buffer flush 的 append/pending/失败回滚/generation guard 回归测试仍保留，避免自动 retain 路径退化。

验证命令：

```bash
python -m pytest tests/fork/test_gateway_retain_command.py tests/fork/test_cli_retain_command.py tests/fork/test_hindsight_provider_regressions.py -q -o 'addopts='
python -m pytest tests/plugins/memory/test_hindsight_provider.py tests/agent/test_memory_session_switch.py tests/agent/test_memory_async_sync.py tests/run_agent/test_memory_sync_interrupted.py tests/gateway/test_undo_rewind_session.py tests/tui_gateway/test_undo_command.py -q -o 'addopts='
python -m pytest tests/plugins/memory/test_hindsight_provider.py -q
python -m py_compile plugins/memory/hindsight/__init__.py cli.py gateway/run.py gateway/slash_commands.py tui_gateway/server.py hermes_cli/commands.py tests/fork/test_gateway_retain_command.py tests/fork/test_cli_retain_command.py
git diff --check
```
