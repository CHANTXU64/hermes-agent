# Hindsight 手动保存会话：`/retain`

状态：fork-only，本地功能。

## 作用

`/retain` 用于把当前会话中已经缓存在 Hindsight provider 里的对话 turn 手动提交到 Hindsight。

它不是重新读取 SQLite 会话，也不是重新拼接整段历史；它复用 Hindsight 自动 retain 已有的同一套缓冲与提交路径。

## 命令

```text
/retain
```

只保留这一个用户手打命令。没有其它别名。

## 适用场景

- 关闭自动 retain 后，需要由用户决定什么时候保存当前会话。
- `memory_mode="context"` 时，Hindsight 工具不暴露给模型，但用户仍希望通过 slash 命令保存会话。
- 自动 retain 间隔较大时，希望立即把当前已完成的 turn 提交出去。

## 行为说明

- `/retain` 会调用当前会话 agent 的 Hindsight provider。
- 提交内容来自 provider 内部的 `_session_turns` 缓冲区。
- 提交流程复用自动 retain 的 `document_id`、序列化和 `_resolve_retain_target()` 路径。
- 不创建 `manual-session:*` 之类的新 document。
- 不通过 SessionDB lineage 重建会话。
- `hindsight_retain_session` 不出现在模型可见的 tool schema 里；它只作为 provider 内部 handler/测试入口保留。

## 和自动 retain 的关系

### `auto_retain=true`

- 自动 retain 仍按原机制运行。
- `/retain` 只提交尚未 queued/flushed 的新 turn。
- 自动 retain 后马上执行 `/retain`，不会重复写同一批 turn。

### `auto_retain=false`

- `sync_turn()` 仍会把已完成 turn 放入本地 buffer。
- 不会自动提交到 Hindsight。
- `/retain` 会手动提交 buffer 中的新 turn。
- session switch 时不会自动 flush；旧 session 的 manual-only buffer 会被清空。

## 防重复提交

实现里维护 queued/flushed 的 turn 计数；在 `update_mode="append"` 下额外维护 pending 标记，避免多个 append job 交错导致前一个失败、后一个成功时误判已保存范围。

结果：

- `update_mode="append"` 下，同一时间只允许一个 retain flush job 排队；已有 job pending 时，新的 `/retain` 不会再排第二个 append job。
- 连续执行 `/retain`，第二次没有新 turn 时不会重复提交。
- 自动 retain 已提交的 turn，不会被后续 `/retain` 再提交一次。
- `update_mode="append"` 时，只 append 新增 turn，不重复 append 旧 turn。
- 如果后台提交失败，pending 标记会清除，queued 计数会回滚到最后成功 flushed 的位置，后续可重新 `/retain`。
- `/retain` 后立刻切换 session 时，旧 session 的后台 job 通过 generation guard 隔离，完成或失败都不会回写新 session 的计数状态。

## 返回结果

常见返回：

- `Buffered session turns queued for retain.`：已有新 buffer，已排队提交。
- `No new buffered turns to retain.` 或类似提示：当前没有新的可提交 turn。
- `A retain flush is already queued.`：`update_mode="append"` 下已有 retain flush job 等待或正在执行，本次不会重复排队。
- `Hindsight memory provider is not active for this session.`：当前会话没有可用 Hindsight provider。
- `Failed to retain session: ...`：提交过程抛错。

## 验证覆盖

相关测试覆盖：

- `/retain` Gateway handler 可从 cached agent 找到 Hindsight provider 并 flush。
- 没有 provider 时返回明确提示。
- `get_tool_schemas()` 不暴露 `hindsight_retain_session`。
- `memory_mode="context"` 下仍可直接通过 provider flush。
- 连续手动 flush 不重复提交。
- 自动 retain 后手动 flush 不重复提交。
- `update_mode="append"` 只提交新 turn。
- 已有 retain flush pending 时拒绝排第二个 job。
- 后台提交失败后会清除 pending 并允许重试。
- `/retain` 后切换 session 时，旧 session 后台 job 不污染新 session 计数。
- `auto_retain=false` 时 session switch 不自动提交。

验证命令：

```bash
python -m pytest tests/plugins/memory/test_hindsight_provider.py tests/hermes_cli/test_commands.py tests/gateway/test_retain_command.py -q -o 'addopts='
python -m py_compile plugins/memory/hindsight/__init__.py cli.py gateway/run.py hermes_cli/commands.py tests/gateway/test_retain_command.py
git diff --check
```
