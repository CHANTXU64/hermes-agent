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
- `turn_index`
- `turn_json`
- `created_at`

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
- `auto_retain=true` 时，自动提交逻辑仍按原机制运行。
- `auto_retain=false` 时，只写本地 SQLite，不自动提交到 Hindsight。
- `/retain` 从当前 active `session_id` 出发，按 `parent_session_id` 往上找 lineage。
- 提交顺序是 root parent → ... → current child。
- 提交内容来自 Hindsight provider 自己持久化的 turn payload，不包含 tool output、tool-call stub、内部推理或压缩 summary。
- 不创建 `manual-session:*` 文档。

## 压缩和 parent session

如果一次用户视角会话因为压缩形成：

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

## document_id / bank / metadata

默认使用当前 leaf session 作为 document：

```text
document_id = current session_id
```

提交到哪个 Hindsight Bank 只看 `/retain` 执行时的当前配置：

```text
bank_id = current configured bank
```

本地 `retain_turns.sqlite3` 里的历史 `bank_id` 不参与读取过滤；它只是当时写入时的记录，避免因 `hermes` / `Hermes` 这类大小写或配置变化丢 turn。

手动 `/retain` 的 item 保持干净，只提交：

```json
{"content": "..."}
```

不额外塞 `metadata`、`tags`、`context`。

## 重复提交

当前实现不维护 retained cursor。也就是说，同一个 session lineage 反复执行 `/retain`，append 模式下可能重复提交完整 lineage。

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
- Gateway `/retain` 调用 provider 的 persisted lineage retain，而不是读取原始 SessionDB transcript。
- `sync_turn()` 会持久化和自动 retain 同源的 turn payload。
- local `bank_id` differences in `retain_turns.sqlite3` do not exclude persisted turns; `/retain` submits all matching session lineage turns to the currently configured Hindsight bank.
- Manual `/retain` submits a clean item containing `content` only, with no extra metadata/tags/context.
- 没有 persisted turns 时不提交。
- `get_tool_schemas()` 不暴露 `hindsight_retain_session`。
- legacy buffer flush 的 append/pending/失败回滚/generation guard 回归测试仍保留，避免自动 retain 路径退化。

验证命令：

```bash
python -m pytest tests/plugins/memory/test_hindsight_provider.py tests/hermes_cli/test_commands.py tests/gateway/test_retain_command.py -q -o 'addopts='
python -m py_compile plugins/memory/hindsight/__init__.py cli.py gateway/run.py hermes_cli/commands.py tests/gateway/test_retain_command.py
git diff --check
```
