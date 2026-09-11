from __future__ import annotations

import ast
import importlib
import json
from pathlib import Path
from unittest.mock import MagicMock, patch


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


def test_background_public_entry_dispatches_only_safe_effective_routes():
    routing = importlib.import_module("fork_features.delegation_routing")
    import tools.async_delegation as async_delegation
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
