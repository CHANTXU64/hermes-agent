# Local Modifications — Hermes Agent Fork

> **Purpose**: Track all deviations from the upstream `NousResearch/hermes-agent` main branch.
> Every new feature, bugfix, or config change added to this fork must be documented here.
>
> **Rule**: When adding a new modification, append a new entry below. Keep existing entries
> up-to-date if the implementation changes. Do NOT delete old entries — they serve as an audit trail.

---

## How to read this doc

Each entry includes:
| Field | Description |
|-------|-------------|
| **Date** | When the modification was made |
| **Commit** | Git commit hash |
| **Files** | Which files were added/modified |
| **What** | What was changed and why |
| **Upstream status** | Whether this has been submitted as a PR to upstream, or is fork-only |

---

## Modification Log

### 1. Hindsight 中文支持 + .gitignore 补充

| Field | Value |
|-------|-------|
| **Date** | 2026-04-20 |
| **Commit** | `7428b0da` |
| **Files** | `plugins/memory/hindsight/__init__.py` (+1/-1), `.gitignore` (+2) |
| **What** | Hindsight 记忆插件的 `json.dumps` 添加 `ensure_ascii=False`，支持中文等 Unicode 字符正常存储（之前被转义为 `\uXXXX`）。.gitignore 补充忽略本地开发目录。 |
| **Upstream status** | Fork-only. |

### 2. MoA 自定义 Provider 支持 (PR #653 适配)

| Field | Value |
|-------|-------|
| **Date** | 2026-04-20 |
| **Commit** | `5c5ffe04` |
| **Files** | `tools/moa_tool.py` 等 3 个文件 (+441/-96) |
| **What** | 适配上游 PR #653 的 provider-agnostic 架构重构，使 Mixture-of-Agents 工具支持自定义 provider 端点。重写了 provider 路由逻辑，使其能识别 custom_providers 配置。 |
| **Upstream status** | Fork adaptation of upstream PR #653. |

### 3. MoA 测试修复 (provider-agnostic 架构)

| Field | Value |
|-------|-------|
| **Date** | 2026-04-20 |
| **Commit** | `e60e548b` |
| **Files** | `tests/tools/test_moa.py` (+22/-7) |
| **What** | 修复 MoA 工具的单元测试，使其兼容新的 provider-agnostic 架构。更新了 mock 配置和断言。 |
| **Upstream status** | Fork-only. Test fix for custom provider support. |

### 4. MoA 自定义端点 401 鉴权修复

| Field | Value |
|-------|-------|
| **Date** | 2026-04-20 |
| **Commit** | `a0fc0fa0` |
| **Files** | `tools/moa_tool.py` 等 2 个文件 (+31/-12) |
| **What** | 修复自定义 provider 端点调用 MoA 时的 401 鉴权错误。在请求头中正确传递 API key，适配非 OpenAI 兼容的认证格式。 |
| **Upstream status** | Fork-only. Bugfix for custom provider authentication. |

### 5. MLX Whisper 本地 STT Provider (PR #3498 适配)

| Field | Value |
|-------|-------|
| **Date** | 2026-04-20 |
| **Commit** | `ae8c0acd` |
| **Files** | `tools/stt_tool.py` (+74/-5) |
| **What** | 添加 MLX Whisper 作为本地 STT（语音转文字）provider，支持 Apple Silicon 原生推理。适配上游 PR #3498 的 STT 插件架构。 |
| **Upstream status** | Fork adaptation of upstream PR #3498. |

### 6. Qwen TTS Provider (通义千问语音合成)

| Field | Value |
|-------|-------|
| **Date** | 2026-04-21 |
| **Commit** | `0e7ab099` |
| **Files** | `tools/tts_tool.py` (+119/-4) |
| **What** | 添加 Qwen TTS provider，通过 DashScope 多模态 REST API 调用通义千问语音合成服务（支持 CosyVoice 模型）。实现了音频格式转换（Opus → 兼容格式）。 |
| **Upstream status** | Fork-only. No upstream PR. |

### 7. TTS Qwen Opus 输出修复

| Field | Value |
|-------|-------|
| **Date** | 2026-04-21 |
| **Commit** | `bd7fd984` |
| **Files** | `tools/tts_tool.py` (+2/-2) |
| **What** | 将 Qwen TTS 从"原生支持 Opus 输出"列表移到"需要 ffmpeg 转换 Opus"列表。修复了 Qwen TTS 输出 Opus 格式时未正确转换导致播放失败的问题。 |
| **Upstream status** | Fork-only. Bugfix for Qwen TTS output format. |

### 8. Safe command rewrite (rm/mv/cp → trash/gmv/gcp)

| Field | Value |
|-------|-------|
| **Date** | 2026-04-21 |
| **Commit** | `5513a9b9` |
| **Files** | `tools/safe_cmd_rewrite.py` (new, 765 lines), `tests/tools/test_safe_cmd_rewrite.py` (new, 208 tests), `tools/terminal_tool.py` (+7 lines), `pyproject.toml` (+1 dep: bashlex) |
| **What** | Automatically replaces destructive shell commands with safe alternatives at the terminal tool execution layer:<br>• `rm [flags] files` → `trash files` (strips -r/-f flags automatically)<br>• `mv [flags] src dst` → `gmv -b [flags] src dst` (auto backup on overwrite)<br>• `cp [flags] src dst` → `gcp -b [flags] src dst` (auto backup on overwrite)<br><br>Uses bashlex AST parser for precise shell command detection. Handles: for/while/if/case structures, $() subshells, compound commands (&& \|\| ; \|), sudo/env/nice/timeout wrappers, env-var prefixes (FOO=bar rm), -- option separator, arbitrary absolute paths.<br><br>Correctly ignores: git rm, echo "rm", comments, find -delete, rsync --delete, words containing 'rm' (firmware, arm, rmdir).<br><br>Sandbox environments (docker/modal/singularity/ssh) skip rewriting. Graceful fallback to regex when bashlex is unavailable. |
| **Upstream status** | Fork-only. No upstream PR exists for this feature. |

### 9. LOCAL_MODIFICATIONS.md 文档

| Field | Value |
|-------|-------|
| **Date** | 2026-04-21 |
| **Commit** | `f75fe530` |
| **Files** | `docs/LOCAL_MODIFICATIONS.md` (new) |
| **What** | 新增本地修改追踪文档，记录本 fork 与 upstream 官方版本的所有差异。 |
| **Upstream status** | Fork-only. Documentation only. |

---

## Summary Statistics

| Category | Count |
|----------|-------|
| Total fork-only commits | 9 |
| Total lines changed | ~2,200+ |
| New files created | 3 |
| New test cases | 208 |
| Upstream PR adaptations | 2 (#3498 STT, #653 MoA) |

---

<!-- Add new modifications below this line. -->

### 10. Review Prompts + skill_manage 描述全部配置化 (防止滥建/乱更/重复 Skill)

| Field | Value |
|-------|-------|
| **Date** | 2026-04-22 |
| **Commit** | 见下方分次提交 |
| **Files** | `run_agent.py` (+19/-4), `tools/skill_manager_tool.py` (+13/-15), `docs/LOCAL_MODIFICATIONS.md` (updated) |
| **What** | 将后台 Review 线程的三个提示词 + skill_manage 工具描述全部改为从 `config.yaml` 读取，消除硬编码。修改原因：官方原版 `_SKILL_REVIEW_PROMPT` 门槛极低，导致 Agent 频繁创建重复/无用的 Skill，且更新时不看已有 Skill 直接新建。<br><br>**配置化内容**：<br>• `skills.skill_review_prompt` — Skill 创建/更新提示词（新建前必须查已有 Skill，更新必须用户纠正+验证通过+用户未反对）<br>• `skills.memory_review_prompt` — Memory 保存提示词（禁止重复 Skill 内容，明确 memory vs skill 边界）<br>• `skills.combined_review_prompt` — 同时触发 Skill+Memory 审查时的统一提示词（严格分离两者内容）<br>• `skills.skill_manage_description` — skill_manage 工具描述（与 review prompt 保持一致，消除矛盾指令）<br><br>**代码改动**：<br>• `run_agent.py` `__init__`：新增三个实例变量从 config 读取，fallback 到类属性<br>• `run_agent.py` `_spawn_background_review`：三处改用实例变量<br>• `tools/skill_manager_tool.py`：新增 `_load_skill_manage_description()` 函数，SKILL_MANAGE_SCHEMA 调用该函数<br><br>此改动只改了代码文件，`config.yaml` 是用户配置不入库。 |
| **Upstream status** | Fork-only. Bugfix for excessive/low-quality skill creation. |

### 11. 回退 Qwen TTS，本 fork 恢复官方 TTS 实现

| Field | Value |
|-------|-------|
| **Date** | 2026-04-27 |
| **Commit** | 未提交 |
| **Files** | `tools/tts_tool.py`, `docs/LOCAL_MODIFICATIONS.md` |
| **What** | 按用户要求删除 fork-only 的 Qwen TTS 改动，移除 DashScope/Qwen 相关实现、配置默认值与分支逻辑，恢复官方上游的 KittenTTS 分支与原始 provider 列表。原 Entry 6/7 保留作为历史审计记录，但代码状态已不再包含 Qwen TTS。 |
| **Upstream status** | Fork local revert — code restored to upstream behavior for TTS. |

### 12. 修复 background review prompt 回归，兼容裸对象测试构造

| Field | Value |
|-------|-------|
| **Date** | 2026-04-27 |
| **Commit** | 未提交 |
| **Files** | `run_agent.py`, `docs/LOCAL_MODIFICATIONS.md` |
| **What** | 修复 `AIAgent._spawn_background_review()` 在 tests 中使用 `object.__new__(AIAgent)` 构造裸对象时访问不到 `_memory_review_prompt / _skill_review_prompt / _combined_review_prompt` 实例属性的问题。改为在读取 prompt 时优先取实例属性，缺失时 fallback 到旧的类级常量 `_MEMORY_REVIEW_PROMPT / _SKILL_REVIEW_PROMPT / _COMBINED_REVIEW_PROMPT`，从而兼容配置化前后的两种对象状态。 |
| **Upstream status** | Fork-only fix for local prompt-config regression. |

