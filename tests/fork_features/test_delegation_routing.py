from __future__ import annotations

import ast
import importlib
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


_HOST_POLICY_DEFINITIONS = {
    "DelegationRouteError",
    "_recent_delegation_route_usage",
    "_available_catalog_routes",
    "_model_name_similarity",
    "_route_suggestion_groups",
    "_render_route_markdown_sections",
    "_route_suggestion_markdown",
    "_provider_failure_candidate_details",
    "_infer_delegation_provider_for_model",
    "_validate_explicit_provider_model_catalog",
    "_exact_reasoning_efforts_for_route",
    "_explicit_reasoning_effort_is_exact",
    "_resolve_target_model_reasoning_config",
    "_resolve_delegation_invocation_route",
}


def _parent_agent() -> MagicMock:
    parent = MagicMock()
    parent.base_url = "https://openrouter.ai/api/v1"
    parent.api_key = "***"
    parent.provider = "openrouter"
    parent.api_mode = "chat_completions"
    parent.model = "anthropic/claude-sonnet-4"
    parent.platform = "cli"
    parent.providers_allowed = None
    parent.providers_ignored = None
    parent.providers_order = None
    parent.provider_sort = None
    parent._session_db = None
    parent._delegate_depth = 0
    parent._active_children = []
    parent._print_fn = None
    parent.tool_progress_callback = None
    parent.thinking_callback = None
    return parent


def test_delegate_task_prevalidates_routes_through_fork_policy_before_child_build():
    routing = importlib.import_module("fork_features.delegation_routing")
    import tools.delegate_tool as delegate_module

    failure = routing.DelegationTaskRouteError(
        ValueError("selected route is unavailable"), task_index=1
    )
    tasks = [
        {"goal": "Inspect the first route and return grounded evidence"},
        {"goal": "Inspect the second route and return grounded evidence"},
    ]

    with (
        patch.object(
            routing,
            "resolve_delegation_task_routes",
            side_effect=failure,
        ) as resolve_routes,
        patch.object(delegate_module, "_build_child_preserving_parent_tools") as build_child,
        patch.object(
            delegate_module,
            "_load_config",
            return_value={"max_iterations": 45},
        ),
    ):
        result = json.loads(
            delegate_module.delegate_task(
                tasks=tasks,
                provider="deepseek",
                model="deepseek-v4",
                reasoning_effort="high",
                parent_agent=_parent_agent(),
            )
        )

    assert result["error"].startswith(
        "Task 1 routing invalid: selected route is unavailable"
    )
    build_child.assert_not_called()
    resolve_routes.assert_called_once()
    kwargs = resolve_routes.call_args.kwargs
    assert kwargs["tasks"] == tasks
    assert kwargs["provider"] == "deepseek"
    assert kwargs["model"] == "deepseek-v4"
    assert kwargs["reasoning_effort"] == "high"
    assert kwargs["credential_resolver"] is delegate_module._resolve_delegation_credentials


def test_delegate_tool_host_does_not_define_fork_route_policy():
    host = Path(__file__).parents[2] / "tools" / "delegate_tool.py"
    tree = ast.parse(host.read_text(encoding="utf-8"))
    top_level_names = {
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    }

    assert not (_HOST_POLICY_DEFINITIONS & top_level_names)


@pytest.mark.parametrize(
    "metadata",
    [
        {"source": "configured-current", "authenticated": False},
        {"authenticated": False},
        {"source": "configured-current"},
    ],
)
def test_explicit_route_does_not_reject_model_from_placeholder_catalog(metadata):
    from fork_features import delegation_routing as routing

    payload = {
        "providers": [
            {"slug": "openai-codex", "models": ["saved-model"], **metadata},
        ],
    }
    with (
        patch("hermes_cli.inventory.load_picker_context"),
        patch("hermes_cli.inventory.build_models_payload", return_value=payload),
        patch.object(routing, "_recent_delegation_route_usage", return_value=[]),
    ):
        # A UI-only saved selection says nothing about the requested model.
        routing._validate_explicit_provider_model_catalog("openai-codex", "other-model")


@pytest.mark.parametrize("with_real_provider", [False, True])
def test_model_only_inference_ignores_placeholder_provider(with_real_provider):
    from fork_features import delegation_routing as routing

    rows = [
        {
            "slug": "saved-provider",
            "models": ["requested-model"],
            "source": "configured-current",
            "authenticated": False,
        },
    ]
    if with_real_provider:
        rows.append({"slug": "live-provider", "models": ["requested-model"]})
    with (
        patch("hermes_cli.inventory.load_picker_context"),
        patch("hermes_cli.inventory.build_models_payload", return_value={"providers": rows}),
        patch.object(routing, "_recent_delegation_route_usage", return_value=[]),
    ):
        if with_real_provider:
            assert routing._infer_delegation_provider_for_model("requested-model") == "live-provider"
        else:
            with pytest.raises(routing.DelegationRouteError) as error:
                routing._infer_delegation_provider_for_model("requested-model")
            assert error.value.error_code == "model_not_found"


def test_route_suggestions_exclude_placeholders_even_with_recent_usage():
    from fork_features import delegation_routing as routing

    payload = {
        "providers": [
            None,
            {"slug": "saved-provider", "models": ["model-a"], "source": "configured-current"},
            {"slug": "signed-out", "models": ["model-b"], "authenticated": False},
            {"slug": "live-provider", "models": ["model-c", "model-c"], "authenticated": True},
            {"slug": "legacy-provider", "models": ["model-d"]},
        ],
    }
    usage = [
        {"provider": "saved-provider", "model": "model-a", "api_calls": 100},
        {"provider": "signed-out", "model": "model-b", "api_calls": 99},
        {"provider": "live-provider", "model": "model-c", "api_calls": 1},
    ]
    with patch.object(routing, "_recent_delegation_route_usage", return_value=usage):
        markdown = routing._route_suggestion_markdown(payload, requested_model="model-a")
    assert "saved-provider" not in markdown
    assert "signed-out" not in markdown
    assert "| live-provider | model-c |" in markdown
    assert routing._available_catalog_routes(payload) == [
        {"provider": "live-provider", "model": "model-c"},
        {"provider": "legacy-provider", "model": "model-d"},
    ]


@pytest.mark.parametrize("authenticated", [True, None])
def test_real_catalog_still_rejects_unknown_model_after_placeholder(authenticated):
    from fork_features import delegation_routing as routing

    real_row = {"slug": "live-provider", "models": ["supported-model"]}
    if authenticated is not None:
        real_row["authenticated"] = authenticated
    payload = {
        "providers": [
            {
                "slug": "live-provider",
                "models": ["missing-model"],
                "source": "configured-current",
                "authenticated": False,
            },
            real_row,
        ],
    }
    with (
        patch("hermes_cli.inventory.load_picker_context"),
        patch("hermes_cli.inventory.build_models_payload", return_value=payload),
        patch.object(routing, "_recent_delegation_route_usage", return_value=[]),
        pytest.raises(routing.DelegationRouteError) as error,
    ):
        routing._validate_explicit_provider_model_catalog("live-provider", "missing-model")
    assert error.value.error_code == "model_not_on_provider"


@pytest.mark.parametrize("credentials_available", [True, False])
def test_public_entry_placeholder_defers_to_target_model_credentials(credentials_available):
    import tools.delegate_tool as delegate_module
    from fork_features import delegation_routing as routing

    payload = {
        "providers": [{
            "slug": "openai-codex",
            "models": ["saved-model"],
            "source": "configured-current",
            "authenticated": False,
        }],
    }
    runtime = {
        "provider": "openai-codex",
        "model": "gpt-5.6-luna",
        "base_url": "https://example.invalid/v1",
        "api_key": "test-key",
        "api_mode": "codex_responses",
    }
    child = MagicMock()
    child.run_conversation.return_value = {
        "final_response": "test child", "completed": True, "api_calls": 0, "messages": [],
    }
    child._delegate_saved_tool_names = []
    child._credential_pool = None
    child.session_prompt_tokens = 0
    child.session_completion_tokens = 0
    child.model = "gpt-5.6-luna"
    child.provider = "openai-codex"
    child.reasoning_config = {"enabled": True, "effort": "max"}
    with (
        patch.object(delegate_module, "_load_config", return_value={"max_iterations": 45}),
        patch("hermes_cli.inventory.load_picker_context"),
        patch("hermes_cli.inventory.build_models_payload", return_value=payload),
        patch.object(routing, "_recent_delegation_route_usage", return_value=[]),
        patch(
            "hermes_cli.runtime_provider.resolve_runtime_provider",
            return_value=runtime,
            side_effect=None if credentials_available else ValueError("target model is cooling down"),
        ) as resolve_runtime,
        patch.object(delegate_module, "_build_child_preserving_parent_tools", return_value=child) as build_child,
    ):
        result = json.loads(delegate_module.delegate_task(
            tasks=[{
                "goal": "Verify placeholder route handling without a model call",
                "provider": "openai-codex",
                "model": "gpt-5.6-luna",
                "reasoning_effort": "max",
            }],
            parent_agent=_parent_agent(),
        ))
    resolve_runtime.assert_called_once_with(requested="openai-codex", target_model="gpt-5.6-luna")
    if credentials_available:
        assert "error" not in result
        build_child.assert_called_once()
        assert build_child.call_args.kwargs["model"] == "gpt-5.6-luna"
        assert build_child.call_args.kwargs["override_reasoning_config"] == {"enabled": True, "effort": "max"}
        child.run_conversation.assert_called_once()
    else:
        assert result["error_code"] == "provider_unavailable"
        assert "target model is cooling down" in result["error"]
        build_child.assert_not_called()
        child.run_conversation.assert_not_called()


def test_background_public_entry_dispatches_only_safe_effective_routes():
    routing = importlib.import_module("fork_features.delegation_routing")
    from tools import async_delegation
    import tools.delegate_tool as delegate_module
    import tools.delegate_tool_dispatch as dispatch_module
    import tools.delegation_live_log as live_log

    route = routing.ResolvedDelegationRoute(
        credentials={
            "provider": "deepseek",
            "model": "deepseek-v4-pro",
            "base_url": "https://api.example.invalid/v1",
            "api_key": "must-not-leak",
            "api_mode": "chat_completions",
        },
        reasoning_config={"enabled": True, "effort": "high"},
    )
    child = MagicMock()
    child.provider = "deepseek"
    child.model = "deepseek-v4-pro"
    child.reasoning_config = {"enabled": True, "effort": "high"}
    child.tool_progress_callback = None
    child._delegate_saved_tool_names = []
    child._credential_pool = None
    captured = {}

    def dispatch(**kwargs):
        captured.update(kwargs)
        return {"status": "dispatched", "delegation_id": "deleg-safe-route"}

    parent = _parent_agent()
    parent.session_id = "parent-session"
    with (
        patch.object(routing, "resolve_delegation_task_routes", return_value=[route]),
        patch.object(delegate_module, "_build_child_preserving_parent_tools", return_value=child),
        patch.object(delegate_module, "_load_config", return_value={"max_iterations": 45}),
        patch.object(delegate_module, "_capture_origin", return_value=("wake-session", "ui-session", None, None, False)),
        patch.object(live_log, "create_live_transcripts", return_value=(None, [], [])),
        patch.object(dispatch_module, "_resolve_async_wake_sid", return_value="wake-session"),
        patch.object(dispatch_module, "_resolve_async_session_key", return_value=("owner-session", "ui-session")),
        patch.object(async_delegation, "dispatch_async_delegation_batch", side_effect=dispatch),
    ):
        result = json.loads(
            delegate_module.delegate_task(
                tasks=[{"goal": "Inspect the selected route"}],
                background=True,
                parent_agent=parent,
            )
        )

    assert result["status"] == "dispatched"
    assert captured["routes"] == [
        {
            "provider": "deepseek",
            "model": "deepseek-v4-pro",
            "reasoning_effort": "high",
        }
    ]
    serialized = json.dumps(captured["routes"], ensure_ascii=False)
    assert "api_key" not in serialized
    assert "base_url" not in serialized
