# Hindsight 手动保存会话：`/retain`

状态：fork-only，本地功能。

## 作用

`/retain` 用于把当前 Hermes session 已持久化在 SessionDB (`~/.hermes/state.db`) 里的可见对话 turn 手动提交到 Hindsight。

它不是让模型调用 `hindsight_retain`，也不暴露新模型工具；用户只需要发送 slash 命令。

## 命令

```text
/retain
```

只保留这一个用户手打命令。没有其它别名。

## 适用场景

- 关闭自动 retain 后，需要由用户决定什么时候保存当前会话。
- `memory_mode="context"` 时，Hindsight 工具不暴露给模型，但用户仍希望通过 slash 命令保存会话。
- 在同一 Gateway 运行期间或重启后，切换到某个已存在 session，再手动保存该 session 的可见对话。

## 行为说明

- Gateway/CLI `/retain` 会读取当前 active `session_id` 对应的 SessionDB transcript。
- 提交前会过滤掉 SessionDB 里的非可见对话噪音：tool 输出、tool-call assistant stub、内部推理/工具调用消息、压缩/LCM 注入的 recent summary。
- 实际提交内容按原自动 retain 的 turn 形状生成：每个 turn 是一条可见 user 消息 + 随后的最终 assistant 回复。
- 仍复用 Hindsight provider 的配置、metadata、tags、`_resolve_retain_target()`/`update_mode="append"` 判断和 writer queue。
- 不创建 `manual-session:*` 之类的新 document；现代 Hindsight API 下 document_id 仍是当前 `session_id`。
- `hindsight_retain_session` 不出现在模型可见的 tool schema 里；它只作为 provider 内部 handler/旧 buffer flush 测试入口保留。

## 和自动 retain 的关系

### `auto_retain=true`

- 自动 retain 仍按原机制运行。
- `/retain` 现在会从 SessionDB 重建当前 session 的可见 transcript 并提交。
- 当前实现不做“已提交 cursor”去重；重复 `/retain` 可能重复 append。该功能按用户当前使用习惯设计：一个 session 完成后手动保存一次。

### `auto_retain=false`

- `sync_turn()` 不会自动提交到 Hindsight。
- 可见对话仍会由 Hermes 正常写入 SessionDB。
- `/retain` 从 SessionDB 读取当前 session，所以 `/new`、`/resume`、切平台、Gateway 重启后再切回该 session，仍可保存该 session 已持久化的可见对话。
- session switch 不再决定手动 retain 能否成功；关键是目标 session 的 transcript 是否已经在 SessionDB 中。

## 压缩和 session 边界

- Hermes 压缩会创建新的 child `session_id`，旧 session 通过 `parent_session_id` 保留 lineage。
- `/retain` 默认只保存当前 active `session_id` 的可见 turn，不自动拼接 parent chain。
- 这与当前 Hindsight 自动 retain 的 session/document 边界一致：压缩后新 turn 属于新的 session/document。

## 返回结果

常见返回：

- `Buffered session turns queued for retain.`：当前 session 的可见 turn 已排队提交。
- `No conversation turns to retain.`：SessionDB 里没有可提交的可见 user→assistant turn。
- `Hindsight memory provider is not active for this session.`：当前会话没有可用 Hindsight provider。
- `Failed to retain session: ...`：提交过程抛错。

## 验证覆盖

相关测试覆盖：

- `/retain` Gateway handler 可从 cached agent 找到 Hindsight provider。
- Gateway `/retain` 优先使用当前 session 的 SessionDB transcript。
- 没有 provider 时返回明确提示。
- `get_tool_schemas()` 不暴露 `hindsight_retain_session`。
- `memory_mode="context"` 下仍可直接通过 provider flush。
- DB-backed retain 会过滤 recent summary、tool output、tool-call assistant stub，只保留可见 user→assistant turn。
- DB-backed retain 无可见 turn 时不提交。
- legacy buffer flush 的 append/pending/失败回滚/generation guard 回归测试仍保留，避免自动 retain 路径退化。

验证命令：

```bash
python -m pytest tests/plugins/memory/test_hindsight_provider.py tests/hermes_cli/test_commands.py tests/gateway/test_retain_command.py -q -o 'addopts='
python -m py_compile plugins/memory/hindsight/__init__.py cli.py gateway/run.py hermes_cli/commands.py tests/gateway/test_retain_command.py
git diff --check
```
