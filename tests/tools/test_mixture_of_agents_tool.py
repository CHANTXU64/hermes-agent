import importlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

moa = importlib.import_module("tools.mixture_of_agents_tool")


def test_moa_defaults_are_well_formed():
    # Invariants, not a catalog snapshot: the exact model list churns with
    # OpenRouter availability (see PR #6636 where gemini-3-pro-preview was
    # removed upstream). What we care about is that the defaults are present
    # and valid vendor/model slugs.
    assert isinstance(moa.REFERENCE_MODELS, list)
    assert len(moa.REFERENCE_MODELS) >= 1
    for m in moa.REFERENCE_MODELS:
        assert isinstance(m, str) and "/" in m and not m.startswith("/")
    assert isinstance(moa.AGGREGATOR_MODEL, str)
    assert "/" in moa.AGGREGATOR_MODEL


@pytest.mark.asyncio
async def test_reference_model_retry_warnings_avoid_exc_info_until_terminal_failure(monkeypatch):
    fake_client = SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(
                create=AsyncMock(side_effect=RuntimeError("rate limited"))
            )
        )
    )
    warn = MagicMock()
    err = MagicMock()

    monkeypatch.setattr(moa.logger, "warning", warn)
    monkeypatch.setattr(moa.logger, "error", err)

    runtime = {"provider": "custom", "base_url": "http://test", "api_key": "test"}
    model, message, success = await moa._run_reference_model_safe(
        fake_client, runtime, "deepseek-v3.2", "hello", max_retries=2
    )

    assert model == "deepseek-v3.2"
    assert success is False
    assert "failed after 2 attempts" in message
    assert warn.call_count == 2
    assert all(call.kwargs.get("exc_info") is None for call in warn.call_args_list)
    err.assert_called_once()
    assert err.call_args.kwargs.get("exc_info") is True


@pytest.mark.asyncio
async def test_moa_top_level_error_logs_single_traceback_on_aggregator_failure(monkeypatch):
    fake_runtime = {"provider": "custom", "base_url": "http://test", "api_key": "test"}
    fake_client = SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(
                create=AsyncMock(side_effect=RuntimeError("aggregator boom"))
            )
        )
    )

    monkeypatch.setattr(moa, "resolve_runtime_provider", lambda requested="auto": fake_runtime)
    monkeypatch.setattr(moa, "_build_async_client", lambda runtime: fake_client)
    monkeypatch.setattr(
        moa,
        "_resolve_default_models",
        lambda runtime: (["qwen3.6-plus"], "qwen3.6-plus"),
    )
    monkeypatch.setattr(
        moa,
        "_run_reference_model_safe",
        AsyncMock(return_value=("qwen3.6-plus", "ok", True)),
    )
    monkeypatch.setattr(
        moa,
        "_run_aggregator_model",
        AsyncMock(side_effect=RuntimeError("aggregator boom")),
    )
    monkeypatch.setattr(
        moa,
        "_debug",
        SimpleNamespace(log_call=MagicMock(), save=MagicMock(), active=False),
    )

    err = MagicMock()
    monkeypatch.setattr(moa.logger, "error", err)

    result = json.loads(
        await moa.mixture_of_agents_tool(
            "solve this",
            reference_models=["qwen3.6-plus"],
        )
    )

    assert result["success"] is False
    assert "Error in MoA processing" in result["error"]
    err.assert_called_once()
    assert err.call_args.kwargs.get("exc_info") is True
