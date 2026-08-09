"""Fork-owned tests for the Hindsight P5 recall preprocessor integration."""

import hashlib
import importlib
import json
import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import agent.turn_context as turn_context
from agent.memory_manager import MemoryManager
from agent.memory_provider import MemoryProvider
from tests.plugins.memory.test_hindsight_provider import provider, provider_with_config
from tests.agent.test_turn_context import _build, _make_agent_with_cooldown


class _LegacyPrefetchProvider(MemoryProvider):
    def __init__(self, name: str = "builtin") -> None:
        self._name = name
        self.prefetch_calls: list[tuple[str, str]] = []
        self.queue_prefetch_calls: list[tuple[str, str]] = []

    @property
    def name(self) -> str:
        return self._name

    def is_available(self) -> bool:
        return True

    def initialize(self, session_id: str, **kwargs) -> None:
        return None

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        self.prefetch_calls.append((query, session_id))
        return f"{self.name}-memory"

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        self.queue_prefetch_calls.append((query, session_id))

    def get_tool_schemas(self):
        return []


class _AssistantAwarePrefetchProvider(_LegacyPrefetchProvider):
    def __init__(self) -> None:
        super().__init__("hindsight")
        self.previous_assistant_messages: list[str] = []
        self.turn_ids: list[str] = []
        self.queue_turn_ids: list[str] = []

    def prefetch(
        self,
        query: str,
        *,
        session_id: str = "",
        turn_id: str = "",
        previous_assistant_message: str = "",
    ) -> str:
        self.prefetch_calls.append((query, session_id))
        self.turn_ids.append(turn_id)
        self.previous_assistant_messages.append(previous_assistant_message)
        return "hindsight-memory"

    def queue_prefetch(
        self,
        query: str,
        *,
        session_id: str = "",
        turn_id: str = "",
    ) -> None:
        self.queue_prefetch_calls.append((query, session_id))
        self.queue_turn_ids.append(turn_id)


def test_prefetch_all_forwards_previous_assistant_only_to_opted_in_provider():
    manager = MemoryManager()
    legacy = _LegacyPrefetchProvider()
    hindsight = _AssistantAwarePrefetchProvider()
    manager.add_provider(legacy)
    manager.add_provider(hindsight)

    result = manager.prefetch_all(
        "修吧。",
        session_id="session-1",
        turn_id="turn-1",
        previous_assistant_message="上一轮已经定位到具体配置错误。",
    )

    assert result == "builtin-memory\n\nhindsight-memory"
    assert legacy.prefetch_calls == [("修吧。", "session-1")]
    assert hindsight.prefetch_calls == [("修吧。", "session-1")]
    assert hindsight.turn_ids == ["turn-1"]
    assert hindsight.previous_assistant_messages == ["上一轮已经定位到具体配置错误。"]


def test_queue_prefetch_all_forwards_turn_id_only_to_opted_in_provider():
    manager = MemoryManager()
    legacy = _LegacyPrefetchProvider()
    hindsight = _AssistantAwarePrefetchProvider()
    manager.add_provider(legacy)
    manager.add_provider(hindsight)

    manager.queue_prefetch_all(
        "继续。",
        session_id="session-1",
        turn_id="turn-1",
    )
    assert manager.flush_pending(timeout=5.0)

    assert legacy.queue_prefetch_calls == [("继续。", "session-1")]
    assert hindsight.queue_prefetch_calls == [("继续。", "session-1")]
    assert hindsight.queue_turn_ids == ["turn-1"]


def test_latest_completed_assistant_message_skips_tool_calls_and_flattens_text_parts():
    extractor = getattr(turn_context, "_latest_completed_assistant_message", None)
    assert callable(extractor)

    messages = [
        {"role": "assistant", "content": "更早的答复"},
        {"role": "user", "content": "请检查"},
        {
            "role": "assistant",
            "content": "准备调用工具",
            "tool_calls": [{"id": "call-1", "type": "function"}],
        },
        {"role": "tool", "tool_call_id": "call-1", "content": "结果"},
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "上一轮最终答复"},
                {"type": "image_url", "image_url": {"url": "https://example.invalid/a.png"}},
            ],
        },
        {"role": "user", "content": "修吧。"},
    ]

    assert extractor(messages, current_turn_user_idx=5) == "上一轮最终答复"


def test_turn_prefetch_keeps_previous_assistant_across_preflight_compression(tmp_path):
    agent = _make_agent_with_cooldown(tmp_path / "state.db", "sess-1")
    manager = SimpleNamespace(
        on_turn_start=MagicMock(),
        prefetch_all=MagicMock(return_value=""),
    )
    setattr(agent, "_memory_manager", manager)
    setattr(agent, "_compress_context", MagicMock(
        return_value=(
            [
                {"role": "user", "content": "[summary of earlier turns]"},
                {"role": "user", "content": "继续。"},
            ],
            "SYSTEM",
        )
    ))

    with (
        patch("agent.auxiliary_client.set_runtime_main", lambda *a, **k: None),
        patch("agent.turn_context._should_run_preflight_estimate", return_value=True),
        patch(
            "agent.turn_context.estimate_request_tokens_rough",
            side_effect=[999_999, 1],
        ),
    ):
        _build(
            agent,
            user_message="继续。",
            conversation_history=[
                {"role": "user", "content": "请检查启动错误"},
                {
                    "role": "assistant",
                    "content": "上一轮已定位到具体启动参数校验错误。",
                },
            ],
        )

    manager.prefetch_all.assert_called_once_with(
        "继续。",
        turn_id=agent._current_turn_id,
        previous_assistant_message="上一轮已定位到具体启动参数校验错误。",
    )
    assert agent._current_turn_id


def test_p5_prompt_is_frozen_and_parser_enforces_strict_contract():
    preprocessor = importlib.import_module(
        "plugins.memory.hindsight.recall_preprocessor"
    )

    assert "按其在当前 Session 之前是否可能存在" in preprocessor.P5_PROMPT
    assert "不按字段类型或示例清单决定" in preprocessor.P5_PROMPT
    assert "本轮刚创建的 commit hash" in preprocessor.P5_PROMPT
    assert "刚生成文件的完整文件名或路径" in preprocessor.P5_PROMPT
    assert "new_query=null 只表示不发起新的 recall" in preprocessor.P5_PROMPT
    assert "未被 drop_old_refs 删除的旧 results 仍会注入本轮" in preprocessor.P5_PROMPT
    assert hashlib.sha256(preprocessor.P5_PROMPT.encode("utf-8")).hexdigest() == (
        "b9b182478b41ab593398bb1649b8a318ab7f59464cd4abe5681a7add6481106f"
    )

    decision = preprocessor.parse_recall_preprocessor_output(
        '{"drop_old_refs":[2],"new_query":"具体配置修复步骤"}',
        max_ref=2,
    )
    assert decision.drop_old_refs == (2,)
    assert decision.new_query == "具体配置修复步骤"

    invalid_outputs = [
        '{"drop_old_refs":[2],"new_query":null,"reason":"extra"}',
        '{"drop_old_refs":[],"drop_old_refs":[1],"new_query":null}',
        '{"drop_old_refs":[true],"new_query":null}',
        '{"drop_old_refs":[1,1],"new_query":null}',
        '{"drop_old_refs":[3],"new_query":null}',
        '{"drop_old_refs":[],"new_query":"line one\\nline two"}',
        '{"drop_old_refs":[],"new_query":""}',
        "```json\n{\"drop_old_refs\":[],\"new_query\":null}\n```",
    ]
    for raw in invalid_outputs:
        with pytest.raises(ValueError):
            preprocessor.parse_recall_preprocessor_output(raw, max_ref=2)


def test_hindsight_registers_recall_preprocessor_as_auxiliary_task():
    hindsight = importlib.import_module("plugins.memory.hindsight")
    registrations = {}

    class _Context:
        def register_memory_provider(self, provider):
            registrations["memory_provider"] = provider

        def register_auxiliary_task(self, key, **kwargs):
            registrations["auxiliary_task"] = (key, kwargs)

    hindsight.register(_Context())

    key, kwargs = registrations["auxiliary_task"]
    assert key == "hindsight_recall_preprocessor"
    assert kwargs["display_name"] == "Hindsight recall preprocessor"
    assert kwargs["defaults"] == {
        "provider": "openai-codex",
        "model": "gpt-5.6-luna",
        "timeout": 30,
        "extra_body": {"service_tier": "priority"},
    }


def test_recall_preprocessor_uses_configured_auxiliary_route_and_timeout(
    monkeypatch,
):
    preprocessor = importlib.import_module(
        "plugins.memory.hindsight.recall_preprocessor"
    )
    from agent import auxiliary_client as aux

    captured = {}
    task_config = {
        "provider": "custom",
        "model": "configured-model",
        "base_url": "https://llm.example.test/v1",
        "api_key": "test-key",
        "api_mode": "chat_completions",
        "timeout": 12.5,
        "extra_body": {"reasoning": {"enabled": False}},
    }
    monkeypatch.setattr(
        aux,
        "_get_auxiliary_task_config",
        lambda task: task_config if task == "hindsight_recall_preprocessor" else {},
    )

    response = SimpleNamespace(
        model="configured-model",
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(
                    content='{"drop_old_refs":[],"new_query":null}'
                )
            )
        ],
    )

    class _Completions:
        def create(self, **kwargs):
            captured["create_kwargs"] = kwargs
            return response

    client = SimpleNamespace(
        chat=SimpleNamespace(completions=_Completions()),
        base_url="https://llm.example.test/v1",
    )

    def _get_cached_client(provider, model, **kwargs):
        captured["route"] = (provider, model, kwargs)
        return client, model

    def _build_call_kwargs(provider, model, messages, **kwargs):
        captured["build"] = (provider, model, messages, kwargs)
        return {"model": model, "messages": messages}

    monkeypatch.setattr(aux, "_get_cached_client", _get_cached_client)
    monkeypatch.setattr(aux, "_build_call_kwargs", _build_call_kwargs)
    monkeypatch.setattr(aux, "_validate_llm_response", lambda response, task: response)
    monkeypatch.setattr(
        aux,
        "call_llm",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("generic call_llm fallback must not be used")
        ),
    )

    decision = preprocessor.run_recall_preprocessor(
        current_user_message="继续",
        previous_assistant_message="上一轮回答",
        previous_recall_query="old query",
        previous_recall_results=[],
    )

    assert captured["route"] == (
        "custom",
        "configured-model",
        {
            "base_url": "https://llm.example.test/v1",
            "api_key": "test-key",
            "api_mode": "chat_completions",
        },
    )
    provider, model, _, build_kwargs = captured["build"]
    assert (provider, model) == ("custom", "configured-model")
    assert build_kwargs["timeout"] == 12.5
    assert build_kwargs["extra_body"] == {"reasoning": {"enabled": False}}
    assert decision.new_query is None


def test_hindsight_declares_full_prefetch_budget_from_stage_timeouts(
    provider_with_config,
    monkeypatch,
):
    from agent import auxiliary_client as aux

    hindsight = provider_with_config(recall_sync_timeout_seconds=10)
    monkeypatch.setattr(aux, "_get_task_timeout", lambda task, default: 30.0)

    assert hindsight.prefetch_timeout_seconds() == 51.0


def test_hindsight_prefetch_budget_includes_configured_fallback_model(
    provider_with_config,
    monkeypatch,
):
    from agent import auxiliary_client as aux

    hindsight = provider_with_config(recall_sync_timeout_seconds=10)
    monkeypatch.setattr(aux, "_get_task_timeout", lambda task, default: 30.0)
    monkeypatch.setattr(
        aux,
        "_get_auxiliary_task_config",
        lambda task: {
            "fallback_chain": [
                {"provider": "deepseek", "model": "deepseek-v4-flash"}
            ]
        },
    )

    assert hindsight.prefetch_timeout_seconds() == 81.0


def test_hindsight_budget_covers_rewrite_recall_and_current_query_fallback(
    provider_with_config,
    monkeypatch,
):
    from agent import auxiliary_client as aux

    hindsight_module = importlib.import_module("plugins.memory.hindsight")
    preprocessor = importlib.import_module(
        "plugins.memory.hindsight.recall_preprocessor"
    )
    hindsight = provider_with_config(recall_sync_timeout_seconds=0.02)
    monkeypatch.setattr(aux, "_get_task_timeout", lambda task, default: 0.02)
    monkeypatch.setattr(
        hindsight_module,
        "_PREFETCH_OUTER_TIMEOUT_GRACE_SECONDS",
        2.0,
        raising=False,
    )

    def _preprocess(**kwargs):
        time.sleep(0.02)
        return preprocessor.RecallPreprocessDecision((), "rewritten query")

    recall_queries = []

    def _recall(query, *, timeout=None):
        recall_queries.append(query)
        time.sleep(0.02)
        if len(recall_queries) == 1:
            raise TimeoutError("rewritten recall timed out")
        return hindsight_module._RecallSnapshot(
            query=query,
            results=("fallback memory",),
        )

    monkeypatch.setattr(hindsight_module, "run_recall_preprocessor", _preprocess)
    monkeypatch.setattr(hindsight, "_recall_snapshot_for_query", _recall)

    manager = MemoryManager()
    manager.add_provider(hindsight)

    result = manager.prefetch_all(
        "current query",
        previous_assistant_message="previous answer",
    )

    assert "fallback memory" in result
    assert recall_queries == ["rewritten query", "current query"]


def test_recall_preprocessor_uses_default_luna_direct_path_without_generic_fallback(
    monkeypatch,
):
    preprocessor = importlib.import_module(
        "plugins.memory.hindsight.recall_preprocessor"
    )
    from agent import auxiliary_client as aux

    captured = {}
    monkeypatch.setattr(aux, "_get_auxiliary_task_config", lambda task: {})

    class _Completions:
        def create(self, **kwargs):
            captured["create_kwargs"] = kwargs
            return SimpleNamespace(
                model="gpt-5.6-luna",
                provider_reported_model="gpt-5.6-luna",
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(
                            content=(
                                '{"drop_old_refs":[2],'
                                '"new_query":"具体配置修复步骤"}'
                            )
                        )
                    )
                ],
            )

    client = SimpleNamespace(
        chat=SimpleNamespace(completions=_Completions()),
        base_url="https://chatgpt.com/backend-api/codex",
    )

    def _get_cached_client(provider, model):
        captured["route"] = (provider, model)
        return client, model

    def _build_call_kwargs(provider, model, messages, **kwargs):
        captured["build"] = (provider, model, messages, kwargs)
        return {"model": model, "messages": messages}

    monkeypatch.setattr(aux, "_get_cached_client", _get_cached_client)
    monkeypatch.setattr(aux, "_build_call_kwargs", _build_call_kwargs)
    monkeypatch.setattr(aux, "_validate_llm_response", lambda response, task: response)
    monkeypatch.setattr(
        aux,
        "call_llm",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("generic call_llm fallback must not be used")
        ),
    )

    decision = preprocessor.run_recall_preprocessor(
        current_user_message="修吧。",
        previous_assistant_message="上一轮定位到了具体配置错误。",
        previous_recall_query="检查启动报错",
        previous_recall_results=["仍相关的旧记忆", "明确旁题"],
    )

    assert captured["route"] == ("openai-codex", "gpt-5.6-luna")
    provider, model, messages, build_kwargs = captured["build"]
    assert (provider, model) == ("openai-codex", "gpt-5.6-luna")
    assert messages[0] == {"role": "system", "content": preprocessor.P5_PROMPT}
    assert json.loads(messages[1]["content"]) == {
        "current_user_message": "修吧。",
        "previous_assistant_message": "上一轮定位到了具体配置错误。",
        "previous_recall": {
            "query": "检查启动报错",
            "results": [
                {"ref": 1, "text": "仍相关的旧记忆"},
                {"ref": 2, "text": "明确旁题"},
            ],
        },
    }
    assert build_kwargs["temperature"] == 0
    assert build_kwargs["max_tokens"] == 256
    assert build_kwargs["timeout"] == 30.0
    assert decision.drop_old_refs == (2,)
    assert decision.new_query == "具体配置修复步骤"


def test_recall_preprocessor_uses_configured_fallback_after_primary_connection_failure(
    monkeypatch,
):
    preprocessor = importlib.import_module(
        "plugins.memory.hindsight.recall_preprocessor"
    )
    from agent import auxiliary_client as aux

    task_config = {
        "provider": "openai-codex",
        "model": "gpt-5.6-luna",
        "timeout": 30,
        "extra_body": {"service_tier": "priority"},
        "fallback_chain": [
            {
                "provider": "deepseek",
                "model": "deepseek-v4-flash",
                "timeout": 7,
            }
        ],
    }
    monkeypatch.setattr(aux, "_get_auxiliary_task_config", lambda task: task_config)

    class _PrimaryCompletions:
        def create(self, **kwargs):
            raise ConnectionError("primary route unavailable")

    primary_client = SimpleNamespace(
        chat=SimpleNamespace(completions=_PrimaryCompletions()),
        base_url="https://chatgpt.com/backend-api/codex",
    )
    fallback_client = SimpleNamespace(base_url="https://api.deepseek.com/v1")
    fallback_response = SimpleNamespace(
        model="deepseek-v4-flash",
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(
                    content=(
                        '{"drop_old_refs":[1],'
                        '"new_query":"fallback generated query"}'
                    )
                )
            )
        ],
    )
    captured = {}

    monkeypatch.setattr(
        aux,
        "_get_cached_client",
        lambda provider, model, **kwargs: (primary_client, model),
    )
    monkeypatch.setattr(
        aux,
        "_build_call_kwargs",
        lambda provider, model, messages, **kwargs: {
            "model": model,
            "messages": messages,
        },
    )
    monkeypatch.setattr(aux, "_validate_llm_response", lambda response, task: response)

    def _try_configured_fallback_chain(
        task,
        failed_provider,
        reason="error",
        failed_model=None,
    ):
        captured["fallback_resolution"] = (
            task,
            failed_provider,
            reason,
            failed_model,
        )
        return fallback_client, "deepseek-v4-flash", "fallback_chain[0](deepseek)"

    def _call_fallback_candidate_sync(client, model, label, **kwargs):
        captured["fallback_call"] = (client, model, label, kwargs)
        return fallback_response

    monkeypatch.setattr(
        aux,
        "_try_configured_fallback_chain",
        _try_configured_fallback_chain,
    )
    monkeypatch.setattr(
        aux,
        "_call_fallback_candidate_sync",
        _call_fallback_candidate_sync,
    )

    decision = preprocessor.run_recall_preprocessor(
        current_user_message="继续",
        previous_assistant_message="上一轮回答",
        previous_recall_query="old query",
        previous_recall_results=["old memory"],
    )

    assert captured["fallback_resolution"] == (
        "hindsight_recall_preprocessor",
        "openai-codex",
        "primary route unavailable",
        "gpt-5.6-luna",
    )
    client, model, label, call_kwargs = captured["fallback_call"]
    assert (client, model, label) == (
        fallback_client,
        "deepseek-v4-flash",
        "fallback_chain[0](deepseek)",
    )
    assert call_kwargs["task"] == "hindsight_recall_preprocessor"
    assert call_kwargs["temperature"] == 0
    assert call_kwargs["max_tokens"] == 256
    assert call_kwargs["effective_timeout"] == 7.0
    assert call_kwargs["effective_extra_body"] == {}
    assert decision.drop_old_refs == (1,)
    assert decision.new_query == "fallback generated query"
    assert preprocessor.get_recall_preprocessor_budget_seconds() == 37.0


@pytest.mark.parametrize(
    ("provider", "model", "base_url", "fallback_timeout"),
    [
        ("auto", "fallback-model", "", None),
        ("main", "fallback-model", "", None),
        ("custom:auto", "fallback-model", "", None),
        ("custom:main", "fallback-model", "", None),
        ("custom", "fallback-model", "", None),
        ("deepseek", "auto", "", None),
        ("deepseek", "", "", None),
        ("deepseek", "fallback-model", "", float("inf")),
        ("deepseek", "fallback-model", "", float("nan")),
        ("deepseek", "fallback-model", "", 0),
        ("deepseek", "fallback-model", "", -1),
        ("deepseek", "fallback-model", "", True),
    ],
)
def test_recall_preprocessor_rejects_non_explicit_fallback_routes(
    monkeypatch,
    provider,
    model,
    base_url,
    fallback_timeout,
):
    preprocessor = importlib.import_module(
        "plugins.memory.hindsight.recall_preprocessor"
    )
    from agent import auxiliary_client as aux

    fallback_entry = {"provider": provider, "model": model}
    if base_url:
        fallback_entry["base_url"] = base_url
    if fallback_timeout is not None:
        fallback_entry["timeout"] = fallback_timeout
    task_config = {
        "provider": "openai-codex",
        "model": "gpt-5.6-luna",
        "timeout": 30,
        "fallback_chain": [fallback_entry],
    }
    monkeypatch.setattr(aux, "_get_auxiliary_task_config", lambda task: task_config)

    primary_client = SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(
                create=lambda **kwargs: (_ for _ in ()).throw(
                    ConnectionError("primary route unavailable")
                )
            )
        ),
        base_url="https://chatgpt.com/backend-api/codex",
    )
    monkeypatch.setattr(
        aux,
        "_get_cached_client",
        lambda provider, model, **kwargs: (primary_client, model),
    )
    monkeypatch.setattr(
        aux,
        "_build_call_kwargs",
        lambda provider, model, messages, **kwargs: {
            "model": model,
            "messages": messages,
        },
    )
    monkeypatch.setattr(
        aux,
        "_try_configured_fallback_chain",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("non-explicit fallback must not reach generic resolver")
        ),
    )

    with pytest.raises(ConnectionError, match="primary route unavailable"):
        preprocessor.run_recall_preprocessor(
            current_user_message="继续",
            previous_assistant_message="上一轮回答",
            previous_recall_query="old query",
            previous_recall_results=["old memory"],
        )

    assert preprocessor.get_recall_preprocessor_budget_seconds() == 30.0


def test_recall_preprocessor_rejects_provider_reported_model_mismatch(monkeypatch):
    preprocessor = importlib.import_module(
        "plugins.memory.hindsight.recall_preprocessor"
    )
    from agent import auxiliary_client as aux

    monkeypatch.setattr(aux, "_get_auxiliary_task_config", lambda task: {})
    response = SimpleNamespace(
        model="gpt-5.6-luna",
        provider_reported_model="substituted-model",
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(
                    content='{"drop_old_refs":[],"new_query":null}'
                )
            )
        ],
    )
    client = SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(create=lambda **kwargs: response)
        ),
        base_url="https://chatgpt.com/backend-api/codex",
    )
    monkeypatch.setattr(
        aux,
        "_get_cached_client",
        lambda provider, model: (client, "gpt-5.6-luna"),
    )
    monkeypatch.setattr(
        aux,
        "_build_call_kwargs",
        lambda provider, model, messages, **kwargs: {
            "model": model,
            "messages": messages,
        },
    )
    monkeypatch.setattr(aux, "_validate_llm_response", lambda response, task: response)

    with pytest.raises(RuntimeError, match="provider-reported model mismatch"):
        preprocessor.run_recall_preprocessor(
            current_user_message="继续",
            previous_assistant_message="上一轮回答",
            previous_recall_query="old query",
            previous_recall_results=[],
        )


def test_recall_preprocessor_rejects_non_codex_response_model_mismatch(monkeypatch):
    preprocessor = importlib.import_module(
        "plugins.memory.hindsight.recall_preprocessor"
    )
    from agent import auxiliary_client as aux

    task_config = {
        "provider": "custom",
        "model": "configured-model",
        "base_url": "https://llm.example.test/v1",
        "api_key": "test-key",
    }
    monkeypatch.setattr(
        aux,
        "_get_auxiliary_task_config",
        lambda task: task_config,
    )
    response = SimpleNamespace(
        model="substituted-model",
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(
                    content='{"drop_old_refs":[],"new_query":null}'
                )
            )
        ],
    )
    client = SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(create=lambda **kwargs: response)
        ),
        base_url="https://llm.example.test/v1",
    )
    monkeypatch.setattr(
        aux,
        "_get_cached_client",
        lambda provider, model, **kwargs: (client, model),
    )
    monkeypatch.setattr(
        aux,
        "_build_call_kwargs",
        lambda provider, model, messages, **kwargs: {
            "model": model,
            "messages": messages,
        },
    )
    monkeypatch.setattr(aux, "_validate_llm_response", lambda response, task: response)

    with pytest.raises(RuntimeError, match="provider-reported model mismatch"):
        preprocessor.run_recall_preprocessor(
            current_user_message="继续",
            previous_assistant_message="上一轮回答",
            previous_recall_query="old query",
            previous_recall_results=[],
        )


def test_recall_preprocessor_codex_alias_still_requires_terminal_model(
    monkeypatch,
):
    preprocessor = importlib.import_module(
        "plugins.memory.hindsight.recall_preprocessor"
    )
    from agent import auxiliary_client as aux

    monkeypatch.setattr(
        aux,
        "_get_auxiliary_task_config",
        lambda task: {"provider": "codex", "model": "gpt-5.6-luna"},
    )
    response = SimpleNamespace(
        model="gpt-5.6-luna",
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(
                    content='{"drop_old_refs":[],"new_query":null}'
                )
            )
        ],
    )
    client = SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(create=lambda **kwargs: response)
        ),
        base_url="https://chatgpt.com/backend-api/codex",
    )
    monkeypatch.setattr(
        aux,
        "_get_cached_client",
        lambda provider, model: (client, model),
    )
    monkeypatch.setattr(
        aux,
        "_build_call_kwargs",
        lambda provider, model, messages, **kwargs: {
            "model": model,
            "messages": messages,
        },
    )
    monkeypatch.setattr(aux, "_validate_llm_response", lambda response, task: response)

    with pytest.raises(RuntimeError, match="provider-reported model mismatch"):
        preprocessor.run_recall_preprocessor(
            current_user_message="继续",
            previous_assistant_message="上一轮回答",
            previous_recall_query="old query",
            previous_recall_results=[],
        )


def test_recall_preprocessor_rejects_resolved_model_substitution(monkeypatch):
    preprocessor = importlib.import_module(
        "plugins.memory.hindsight.recall_preprocessor"
    )
    from agent import auxiliary_client as aux

    monkeypatch.setattr(aux, "_get_auxiliary_task_config", lambda task: {})
    monkeypatch.setattr(
        aux,
        "_get_cached_client",
        lambda provider, model: (object(), "gpt-5.4-mini"),
    )

    with pytest.raises(RuntimeError, match="configured recall-preprocessor route unavailable"):
        preprocessor.run_recall_preprocessor(
            current_user_message="继续",
            previous_assistant_message="上一轮回答",
            previous_recall_query="old query",
            previous_recall_results=["old result"],
        )


def test_recall_preprocessor_rejects_auto_route_without_main_model_fallback(
    monkeypatch,
):
    preprocessor = importlib.import_module(
        "plugins.memory.hindsight.recall_preprocessor"
    )
    from agent import auxiliary_client as aux

    monkeypatch.setattr(
        aux,
        "_get_auxiliary_task_config",
        lambda task: {"provider": "auto", "model": ""},
    )
    monkeypatch.setattr(
        aux,
        "_get_cached_client",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("auto route must not resolve to the main model")
        ),
    )

    with pytest.raises(RuntimeError, match="explicit auxiliary provider and model"):
        preprocessor.run_recall_preprocessor(
            current_user_message="继续",
            previous_assistant_message="上一轮回答",
            previous_recall_query="old query",
            previous_recall_results=["old result"],
        )


@pytest.mark.parametrize(
    "task_config",
    [
        {"provider": "main", "model": "configured-model"},
        {"provider": "custom", "model": "configured-model"},
        {"provider": "custom:", "model": "configured-model"},
        {"provider": "custom:auto", "model": "configured-model"},
        {"provider": "custom:main", "model": "configured-model"},
        {"provider": "custom:custom", "model": "configured-model"},
    ],
)
def test_recall_preprocessor_rejects_dynamic_or_bare_generic_route_before_resolution(
    monkeypatch,
    task_config,
):
    preprocessor = importlib.import_module(
        "plugins.memory.hindsight.recall_preprocessor"
    )
    from agent import auxiliary_client as aux

    monkeypatch.setattr(
        aux,
        "_get_auxiliary_task_config",
        lambda task: task_config,
    )
    monkeypatch.setattr(
        aux,
        "_resolve_task_provider_model",
        lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("unsafe dynamic route must be rejected before resolution")
        ),
    )

    with pytest.raises(RuntimeError, match="explicit auxiliary route"):
        preprocessor.run_recall_preprocessor(
            current_user_message="继续",
            previous_assistant_message="上一轮回答",
            previous_recall_query="old query",
            previous_recall_results=["old result"],
        )


def test_hindsight_prefetch_filters_cached_snapshot_and_appends_new_recall(
    provider,
    monkeypatch,
):
    provider._prefetch_result = "- 仍相关的旧记忆\n- 明确旁题"
    provider._prefetch_snapshot = SimpleNamespace(
        query="检查启动报错",
        results=("仍相关的旧记忆", "明确旁题"),
    )
    preprocessor_calls = []

    def _run_preprocessor(**kwargs):
        preprocessor_calls.append(kwargs)
        return SimpleNamespace(
            drop_old_refs=(2,),
            new_query="具体配置修复步骤",
        )

    recall_calls = []

    def _recall_snapshot(query, *, timeout=None):
        recall_calls.append((query, timeout))
        return SimpleNamespace(
            query=query,
            results=("新召回的配置修改位置和验证步骤",),
        )

    monkeypatch.setattr(
        "plugins.memory.hindsight.run_recall_preprocessor",
        _run_preprocessor,
        raising=False,
    )
    monkeypatch.setattr(
        provider,
        "_recall_snapshot_for_query",
        _recall_snapshot,
        raising=False,
    )

    result = provider.prefetch(
        "修吧。",
        previous_assistant_message="上一轮已经定位到具体配置错误。",
    )

    assert preprocessor_calls == [
        {
            "current_user_message": "修吧。",
            "previous_assistant_message": "上一轮已经定位到具体配置错误。",
            "previous_recall_query": "检查启动报错",
            "previous_recall_results": ("仍相关的旧记忆", "明确旁题"),
        }
    ]
    assert recall_calls == [("具体配置修复步骤", 5.0)]
    assert "仍相关的旧记忆" in result
    assert "新召回的配置修改位置和验证步骤" in result
    assert "明确旁题" not in result


def test_hindsight_prefetch_uses_assistant_derived_query_when_cache_is_empty(
    provider,
    monkeypatch,
):
    preprocessor_calls = []

    def _run_preprocessor(**kwargs):
        preprocessor_calls.append(kwargs)
        return SimpleNamespace(
            drop_old_refs=(),
            new_query="Hindsight 启动配置错误的修改位置和验证步骤",
        )

    recall_calls = []

    def _recall_snapshot(query, *, timeout=None):
        recall_calls.append((query, timeout))
        return SimpleNamespace(
            query=query,
            results=("配置文件位置与启动校验步骤",),
        )

    monkeypatch.setattr(
        "plugins.memory.hindsight.run_recall_preprocessor",
        _run_preprocessor,
    )
    monkeypatch.setattr(provider, "_recall_snapshot_for_query", _recall_snapshot)

    result = provider.prefetch(
        "继续。",
        previous_assistant_message=(
            "上一轮已定位到 retain_max_completion_tokens 小于 "
            "retain_chunk_size 的启动校验错误。"
        ),
    )

    assert preprocessor_calls == [
        {
            "current_user_message": "继续。",
            "previous_assistant_message": (
                "上一轮已定位到 retain_max_completion_tokens 小于 "
                "retain_chunk_size 的启动校验错误。"
            ),
            "previous_recall_query": "",
            "previous_recall_results": (),
        }
    ]
    assert recall_calls == [
        ("Hindsight 启动配置错误的修改位置和验证步骤", 5.0)
    ]
    assert "配置文件位置与启动校验步骤" in result


def test_hindsight_post_turn_queue_does_not_recall_raw_user_query_or_replace_actual_snapshot(
    provider,
    monkeypatch,
):
    provider._prefetch_result = "- previous memory"
    provider._prefetch_snapshot = SimpleNamespace(
        query="previous query",
        results=("previous memory",),
    )
    preprocessor_calls = []
    decisions = iter(
        [
            SimpleNamespace(drop_old_refs=(1,), new_query="specific current target"),
            SimpleNamespace(drop_old_refs=(), new_query="specific next target"),
        ]
    )

    def _preprocess(**kwargs):
        preprocessor_calls.append(kwargs)
        return next(decisions)

    recall_calls = []

    def _recall(query, *, timeout=None):
        recall_calls.append((query, timeout))
        return SimpleNamespace(query=query, results=(f"memory for {query}",))

    monkeypatch.setattr(
        "plugins.memory.hindsight.run_recall_preprocessor",
        _preprocess,
    )
    monkeypatch.setattr(provider, "_recall_snapshot_for_query", _recall)

    current_context = provider.prefetch(
        "继续。",
        session_id="test-session",
        turn_id="turn-2",
        previous_assistant_message="上一轮已把目标具体化。",
    )
    provider.queue_prefetch(
        "继续。",
        session_id="test-session",
        turn_id="turn-2",
    )
    next_context = provider.prefetch(
        "再看看。",
        session_id="test-session",
        turn_id="turn-3",
        previous_assistant_message="第二轮完成了具体检查。",
    )

    assert "memory for specific current target" in current_context
    assert "memory for specific next target" in next_context
    assert recall_calls == [
        ("specific current target", 5.0),
        ("specific next target", 5.0),
    ]
    assert preprocessor_calls[1]["previous_recall_query"] == "specific current target"
    assert preprocessor_calls[1]["previous_recall_results"] == (
        "memory for specific current target",
    )


def test_hindsight_first_turn_sync_recall_becomes_next_turn_previous_recall(
    provider,
    monkeypatch,
):
    recall_calls = []

    def _recall(query, *, timeout=None):
        recall_calls.append((query, timeout))
        return SimpleNamespace(query=query, results=(f"memory for {query}",))

    preprocessor_calls = []

    def _preprocess(**kwargs):
        preprocessor_calls.append(kwargs)
        return SimpleNamespace(drop_old_refs=(), new_query="second-turn target")

    monkeypatch.setattr(provider, "_recall_snapshot_for_query", _recall)
    monkeypatch.setattr(
        "plugins.memory.hindsight.run_recall_preprocessor",
        _preprocess,
    )

    first_context = provider.prefetch("first-turn target", session_id="test-session")
    second_context = provider.prefetch(
        "继续。",
        session_id="test-session",
        previous_assistant_message="第一轮已经明确了处理对象。",
    )

    assert "memory for first-turn target" in first_context
    assert "memory for second-turn target" in second_context
    assert preprocessor_calls == [
        {
            "current_user_message": "继续。",
            "previous_assistant_message": "第一轮已经明确了处理对象。",
            "previous_recall_query": "first-turn target",
            "previous_recall_results": ("memory for first-turn target",),
        }
    ]
    assert recall_calls == [
        ("first-turn target", 5.0),
        ("second-turn target", 5.0),
    ]


def test_hindsight_public_prefetch_late_turn_cannot_replace_newer_snapshot(
    provider,
    monkeypatch,
):
    old_started = threading.Event()
    release_old = threading.Event()

    def _recall(query, *, timeout=None):
        if query == "old turn target":
            old_started.set()
            assert release_old.wait(timeout=5.0)
        return SimpleNamespace(query=query, results=(f"memory for {query}",))

    monkeypatch.setattr(provider, "_recall_snapshot_for_query", _recall)
    old_result = {}
    old_thread = threading.Thread(
        target=lambda: old_result.setdefault(
            "context",
            provider.prefetch("old turn target", session_id="test-session"),
        )
    )
    old_thread.start()
    assert old_started.wait(timeout=5.0)

    new_context = provider.prefetch("new turn target", session_id="test-session")
    release_old.set()
    old_thread.join(timeout=5.0)

    assert not old_thread.is_alive()
    assert "memory for old turn target" in old_result["context"]
    assert "memory for new turn target" in new_context
    assert provider._prefetch_result == "- memory for new turn target"
    assert provider._prefetch_snapshot.query == "new turn target"
    assert provider._prefetch_snapshot.results == ("memory for new turn target",)


def test_hindsight_session_switch_clears_structured_prefetch_snapshot(provider):
    provider._prefetch_result = "- old-session recall"
    provider._prefetch_snapshot = SimpleNamespace(
        query="old-session query",
        results=("old-session recall",),
    )

    provider.on_session_switch("new-session")

    assert provider._prefetch_result == ""
    assert provider._prefetch_snapshot is None


def test_memory_manager_timeout_invalidates_late_hindsight_snapshot(
    provider,
    monkeypatch,
):
    recall_started = threading.Event()
    release_recall = threading.Event()

    def _recall(query, *, timeout=None):
        recall_started.set()
        assert release_recall.wait(timeout=5.0)
        return SimpleNamespace(query=query, results=("late timed-out memory",))

    monkeypatch.setattr(provider, "_recall_snapshot_for_query", _recall)
    manager = MemoryManager(external_prefetch_timeout=0.01)
    manager.add_provider(provider)

    result = manager.prefetch_all(
        "timed-out target",
        session_id="test-session",
        turn_id="turn-timeout",
    )
    assert result == ""
    assert recall_started.wait(timeout=1.0)

    result = manager.prefetch_all(
        "newer target while old call is stuck",
        session_id="test-session",
        turn_id="turn-after-timeout",
    )
    assert result == ""

    thread = manager._external_prefetch_threads[provider.name]
    release_recall.set()
    thread.join(timeout=5.0)

    assert not thread.is_alive()
    assert provider._prefetch_result == ""
    assert provider._prefetch_snapshot is None


def test_hindsight_public_prefetch_late_old_session_result_is_not_carried(
    provider,
    monkeypatch,
):
    old_session_id = provider._session_id
    old_started = threading.Event()
    release_old = threading.Event()

    def _recall(query, *, timeout=None):
        old_started.set()
        assert release_old.wait(timeout=5.0)
        return SimpleNamespace(query=query, results=("old-session memory",))

    monkeypatch.setattr(provider, "_recall_snapshot_for_query", _recall)
    old_result = {}
    old_thread = threading.Thread(
        target=lambda: old_result.setdefault(
            "context",
            provider.prefetch("old-session target", session_id=old_session_id),
        )
    )
    old_thread.start()
    assert old_started.wait(timeout=5.0)

    provider.on_session_switch("new-session")
    release_old.set()
    old_thread.join(timeout=5.0)

    assert not old_thread.is_alive()
    assert "old-session memory" in old_result["context"]
    assert provider._prefetch_result == ""
    assert provider._prefetch_snapshot is None


def test_hindsight_stale_session_prefetch_cannot_consume_current_snapshot(
    provider,
):
    old_session_id = provider._session_id
    provider.on_session_switch("new-session")
    provider._prefetch_result = "- new-session memory"
    provider._prefetch_snapshot = SimpleNamespace(
        query="new-session target",
        results=("new-session memory",),
    )

    result = provider.prefetch("late old request", session_id=old_session_id)

    assert result == ""
    assert provider._prefetch_result == "- new-session memory"
    assert provider._prefetch_snapshot.query == "new-session target"
    assert provider._prefetch_snapshot.results == ("new-session memory",)


def test_hindsight_session_rewind_clears_carried_recall_snapshot(provider):
    provider._prefetch_result = "- rewound recall"
    provider._prefetch_snapshot = SimpleNamespace(
        query="rewound query",
        results=("rewound recall",),
    )

    provider.on_session_rewind(provider._session_id, turns_undone=1)

    assert provider._prefetch_result == ""
    assert provider._prefetch_snapshot is None


def test_hindsight_public_prefetch_late_rewound_result_is_not_carried(
    provider,
    monkeypatch,
):
    recall_started = threading.Event()
    release_recall = threading.Event()

    def _recall(query, *, timeout=None):
        recall_started.set()
        assert release_recall.wait(timeout=5.0)
        return SimpleNamespace(query=query, results=("rewound late memory",))

    monkeypatch.setattr(provider, "_recall_snapshot_for_query", _recall)
    old_result = {}
    old_thread = threading.Thread(
        target=lambda: old_result.setdefault(
            "context",
            provider.prefetch(
                "soon-rewound target",
                session_id=provider._session_id,
                turn_id="turn-before-rewind",
            ),
        )
    )
    old_thread.start()
    assert recall_started.wait(timeout=5.0)

    provider.on_session_rewind(provider._session_id, turns_undone=1)
    release_recall.set()
    old_thread.join(timeout=5.0)

    assert not old_thread.is_alive()
    assert "rewound late memory" in old_result["context"]
    assert provider._prefetch_result == ""
    assert provider._prefetch_snapshot is None


def test_hindsight_empty_generated_recall_is_carried_as_real_snapshot(
    provider,
    monkeypatch,
):
    provider._prefetch_result = "- old memory"
    provider._prefetch_snapshot = SimpleNamespace(
        query="old query",
        results=("old memory",),
    )
    preprocessor_calls = []
    decisions = iter(
        [
            SimpleNamespace(drop_old_refs=(1,), new_query="empty target"),
            SimpleNamespace(drop_old_refs=(), new_query=None),
        ]
    )

    def _preprocess(**kwargs):
        preprocessor_calls.append(kwargs)
        return next(decisions)

    monkeypatch.setattr(
        "plugins.memory.hindsight.run_recall_preprocessor",
        _preprocess,
    )
    monkeypatch.setattr(
        provider,
        "_recall_snapshot_for_query",
        lambda query, *, timeout=None: SimpleNamespace(query=query, results=()),
    )

    first_result = provider.prefetch(
        "first continuation",
        session_id="test-session",
        previous_assistant_message="The first target is concrete.",
    )
    second_result = provider.prefetch(
        "second continuation",
        session_id="test-session",
        previous_assistant_message="The empty recall completed.",
    )

    assert first_result == ""
    assert second_result == ""
    assert preprocessor_calls[1]["previous_recall_query"] == "empty target"
    assert preprocessor_calls[1]["previous_recall_results"] == ()
    assert provider._prefetch_result == ""
    assert provider._prefetch_snapshot.query == "empty target"
    assert provider._prefetch_snapshot.results == ()


def test_hindsight_prefetch_null_query_reuses_selected_old_results_without_new_recall(
    provider,
    monkeypatch,
):
    provider._prefetch_result = "- keep\n- drop"
    provider._prefetch_snapshot = SimpleNamespace(
        query="old query",
        results=("keep", "drop"),
    )
    monkeypatch.setattr(
        "plugins.memory.hindsight.run_recall_preprocessor",
        lambda **kwargs: SimpleNamespace(drop_old_refs=(2,), new_query=None),
    )
    monkeypatch.setattr(
        provider,
        "_recall_snapshot_for_query",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("no recall may run when new_query is null")
        ),
    )

    result = provider.prefetch(
        "继续。",
        previous_assistant_message="没有提出新的具体检索目标。",
    )

    assert "keep" in result
    assert "drop" not in result
    assert provider._prefetch_result == "- keep"
    assert provider._prefetch_snapshot.query == "old query"
    assert provider._prefetch_snapshot.results == ("keep",)


def test_hindsight_prefetch_null_query_all_dropped_clears_old_recall(
    provider,
    monkeypatch,
):
    provider._prefetch_result = "- first old\n- second old"
    provider._prefetch_snapshot = SimpleNamespace(
        query="old query",
        results=("first old", "second old"),
    )
    monkeypatch.setattr(
        "plugins.memory.hindsight.run_recall_preprocessor",
        lambda **kwargs: SimpleNamespace(drop_old_refs=(1, 2), new_query=None),
    )
    monkeypatch.setattr(
        provider,
        "_recall_snapshot_for_query",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("no recall may run when new_query is null")
        ),
    )

    result = provider.prefetch(
        "这个话题结束。",
        previous_assistant_message="上一轮任务已经完成。",
    )

    assert result == ""
    assert provider._prefetch_result == ""
    assert provider._prefetch_snapshot.query == ""
    assert provider._prefetch_snapshot.results == ()


def test_hindsight_null_query_reuses_old_results_and_post_turn_queue_is_noop(
    provider,
    monkeypatch,
):
    provider._prefetch_result = "- old memory"
    provider._prefetch_snapshot = SimpleNamespace(
        query="old query",
        results=("old memory",),
    )
    monkeypatch.setattr(
        "plugins.memory.hindsight.run_recall_preprocessor",
        lambda **kwargs: SimpleNamespace(drop_old_refs=(), new_query=None),
    )
    recall_calls = []

    def _recall(query, *, timeout=None):
        recall_calls.append((query, timeout))
        return SimpleNamespace(query=query, results=("unexpected",))

    monkeypatch.setattr(provider, "_recall_snapshot_for_query", _recall)

    result = provider.prefetch(
        "继续。",
        session_id=provider._session_id,
        turn_id="turn-3",
        previous_assistant_message="上一轮回答。",
    )
    provider.queue_prefetch(
        "继续。",
        session_id=provider._session_id,
        turn_id="turn-3",
    )

    assert "old memory" in result
    assert recall_calls == []
    assert provider._prefetch_result == "- old memory"
    assert provider._prefetch_snapshot.query == "old query"
    assert provider._prefetch_snapshot.results == ("old memory",)


def test_hindsight_null_query_delayed_old_session_queue_cannot_repopulate_after_switch(
    provider,
    monkeypatch,
):
    old_session_id = provider._session_id
    provider._prefetch_result = "- old memory"
    provider._prefetch_snapshot = SimpleNamespace(
        query="old query",
        results=("old memory",),
    )
    monkeypatch.setattr(
        "plugins.memory.hindsight.run_recall_preprocessor",
        lambda **kwargs: SimpleNamespace(drop_old_refs=(), new_query=None),
    )
    recall_calls = []

    def _recall(query, *, timeout=None):
        recall_calls.append((query, timeout))
        return SimpleNamespace(query=query, results=("stale old-turn recall",))

    monkeypatch.setattr(provider, "_recall_snapshot_for_query", _recall)

    result = provider.prefetch(
        "继续。",
        session_id=old_session_id,
        turn_id="turn-3",
        previous_assistant_message="上一轮回答。",
    )
    provider.on_session_switch("new-session")
    provider.queue_prefetch(
        "继续。",
        session_id=old_session_id,
        turn_id="turn-3",
    )

    assert "old memory" in result
    assert recall_calls == []
    assert provider._session_id == "new-session"
    assert provider._prefetch_result == ""
    assert provider._prefetch_snapshot is None


def test_hindsight_recall_after_null_query_merges_reused_old_and_new_results(
    provider,
    monkeypatch,
):
    provider._prefetch_result = "- second-turn memory"
    provider._prefetch_snapshot = SimpleNamespace(
        query="second-turn query",
        results=("second-turn memory",),
    )
    preprocessor_calls = []
    decisions = iter(
        [
            SimpleNamespace(drop_old_refs=(), new_query=None),
            SimpleNamespace(drop_old_refs=(), new_query="fourth-turn query"),
        ]
    )

    def _preprocess(**kwargs):
        preprocessor_calls.append(kwargs)
        return next(decisions)

    recall_calls = []

    def _recall(query, *, timeout=None):
        recall_calls.append((query, timeout))
        return SimpleNamespace(query=query, results=("fourth-turn memory",))

    monkeypatch.setattr(
        "plugins.memory.hindsight.run_recall_preprocessor",
        _preprocess,
    )
    monkeypatch.setattr(provider, "_recall_snapshot_for_query", _recall)

    third_context = provider.prefetch(
        "好。",
        session_id=provider._session_id,
        turn_id="turn-3",
        previous_assistant_message="第二轮回答。",
    )
    provider.queue_prefetch(
        "好。",
        session_id=provider._session_id,
        turn_id="turn-3",
    )
    fourth_context = provider.prefetch(
        "继续。",
        session_id=provider._session_id,
        turn_id="turn-4",
        previous_assistant_message="第三轮回答包含了新的具体内容。",
    )

    assert "second-turn memory" in third_context
    assert preprocessor_calls[1]["previous_recall_query"] == "second-turn query"
    assert preprocessor_calls[1]["previous_recall_results"] == (
        "second-turn memory",
    )
    assert recall_calls == [("fourth-turn query", 5.0)]
    assert "fourth-turn memory" in fourth_context
    assert "second-turn memory" in fourth_context


def test_hindsight_prefetch_preprocessor_failure_preserves_full_old_cache(
    provider,
    monkeypatch,
):
    provider._prefetch_result = "- first old\n- second old"
    provider._prefetch_snapshot = SimpleNamespace(
        query="old query",
        results=("first old", "second old"),
    )
    monkeypatch.setattr(
        "plugins.memory.hindsight.run_recall_preprocessor",
        lambda **kwargs: (_ for _ in ()).throw(ValueError("invalid JSON")),
    )

    result = provider.prefetch("new target", previous_assistant_message="prior answer")

    assert "first old" in result
    assert "second old" in result


def test_hindsight_prefetch_new_recall_failure_restores_full_old_cache(
    provider,
    monkeypatch,
):
    provider._prefetch_result = "- first old\n- second old"
    provider._prefetch_snapshot = SimpleNamespace(
        query="old query",
        results=("first old", "second old"),
    )
    monkeypatch.setattr(
        "plugins.memory.hindsight.run_recall_preprocessor",
        lambda **kwargs: SimpleNamespace(
            drop_old_refs=(2,),
            new_query="new target query",
        ),
    )
    monkeypatch.setattr(
        provider,
        "_recall_snapshot_for_query",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("recall down")),
    )

    result = provider.prefetch("new target", previous_assistant_message="prior answer")

    assert "first old" in result
    assert "second old" in result


@pytest.mark.parametrize(
    "config",
    [
        {"memory_mode": "tools"},
        {"auto_recall": False},
    ],
)
def test_hindsight_prefetch_does_not_run_preprocessor_when_auto_recall_is_inactive(
    provider_with_config,
    monkeypatch,
    config,
):
    inactive_provider = provider_with_config(**config)
    preprocessor_calls = []
    monkeypatch.setattr(
        "plugins.memory.hindsight.run_recall_preprocessor",
        lambda **kwargs: preprocessor_calls.append(kwargs),
    )

    result = inactive_provider.prefetch(
        "继续。",
        previous_assistant_message="上一轮有具体诊断。",
    )

    assert result == ""
    assert preprocessor_calls == []
