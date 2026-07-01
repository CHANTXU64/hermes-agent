# Hindsight 手动保存会话：`/retain`

状态：fork-only，本地功能。

## 作用

`/retain` 用于把当前 Hermes 会话 lineage 中，Hindsight provider 每轮已经整理好的 retain turn，手动提交到 Hindsight。

关键点：手动保存不再从 Hermes 原始 SessionDB transcript 反推内容；Hindsight provider 在 `sync_turn()` 生成自动 retain payload 时，会把同一份 turn JSON 持久化到独立 SQLite 文件。

## 本地持久化文件

独立文件：

```text
$HERMES_HOME/hindsight/retain_turns.sqlite3
```

不写入 Hermes 主 `state.db`。

表：

```text
hindsight_retain_turns
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

## 命令

```text
/retain
```

只保留这一个用户手打命令。没有其它别名。

## 行为说明

- 每个完成 turn 的 `sync_turn()` 都会先把同源 retain payload 写入 `hindsight/retain_turns.sqlite3`。
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

- 在 `hindsight_retain_turns` 中把当前 session 最后 N 个 `active=1` rows 标为 `active=0`，并写入 `rewound_at`。
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

当前实现不维护 retained cursor，也不会在 retain 成功后清理 `retain_turns.sqlite3`。也就是说，同一个 `retain_document_id` 反复执行 `/retain`，append 模式下可能重复提交完整 logical document。

这是有意保持简单：当前使用目标是“一个会话结束后手动保存一次”。

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
- Manual `/retain` 会按 `retain_document_id` 聚合同一压缩 logical document；即使 B/C 在 SessionDB 中表现为 siblings，也能从 C retain 到 A+B+C。
- 旧 row 后续写入空 `parent_session_id` 时，lineage fallback 会使用早先非空 parent，而不是被空 parent 截断。
- local `bank_id` differences in `retain_turns.sqlite3` do not exclude persisted turns; `/retain` submits all matching session lineage turns to the currently configured Hindsight bank.
- `/undo N` marks the current session's last N active persisted retain rows inactive; manual `/retain` skips inactive rows while still preserving compression siblings that share the same `retain_document_id`.
- Hindsight rewind handling does not run flush-on-switch, so `/undo` does not enqueue stale buffered turns as a side effect.
- Manual `/retain` submits a clean item containing `content` and configured `context`, with no extra metadata/tags.
- 没有 persisted turns 时不提交。
- `get_tool_schemas()` 不暴露 `hindsight_retain_session`。
- Slack native slash slots under the 50-command cap preserve canonical `/debug` despite fork-only `/retain`; low-frequency `/blueprint`, `/credits`, `/disk_cleanup`, and `/lcm` remain reachable through `/hermes <command>` on Slack.
- legacy buffer flush 的 append/pending/失败回滚/generation guard 回归测试仍保留，避免自动 retain 路径退化。

验证命令：

```bash
python -m pytest tests/fork/test_gateway_retain_command.py tests/fork/test_cli_retain_command.py tests/fork/test_hindsight_provider_regressions.py -q -o 'addopts='
python -m pytest tests/plugins/memory/test_hindsight_provider.py tests/agent/test_memory_session_switch.py tests/agent/test_memory_async_sync.py tests/run_agent/test_memory_sync_interrupted.py tests/gateway/test_undo_rewind_session.py tests/tui_gateway/test_undo_command.py -q -o 'addopts='
python -m pytest tests/plugins/memory/test_hindsight_provider.py -q
python -m py_compile plugins/memory/hindsight/__init__.py cli.py gateway/run.py gateway/slash_commands.py tui_gateway/server.py hermes_cli/commands.py tests/fork/test_gateway_retain_command.py tests/fork/test_cli_retain_command.py
git diff --check
```
