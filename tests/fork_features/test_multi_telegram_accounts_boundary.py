from __future__ import annotations

import ast
from pathlib import Path


def _repo() -> Path:
    return Path(__file__).resolve().parents[2]


def _class_methods(path: Path, class_name: str) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    cls = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == class_name
    )
    return {
        node.name
        for node in cls.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _top_level_functions(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def test_high_churn_hosts_use_telegram_fork_facade_instead_of_owning_policy() -> None:
    repo = _repo()
    runner_methods = _class_methods(repo / "gateway/run.py", "GatewayRunner")
    assert {
        "_named_telegram_account_id",
        "_handle_telegram_account_fatal_error",
        "_reconnect_failed_telegram_accounts",
        "_start_telegram_account_adapters",
    }.isdisjoint(runner_methods)

    session_functions = _top_level_functions(repo / "gateway/session.py")
    assert {
        "normalize_account_id",
        "append_account_session_key",
        "split_account_session_key",
    }.isdisjoint(session_functions)

    authz_source = (repo / "gateway/authz_mixin.py").read_text(encoding="utf-8")
    assert "_telegram_account_adapters" not in authz_source

    slash_source = (repo / "gateway/slash_commands.py").read_text(encoding="utf-8")
    assert "split_account_session_key" not in slash_source

    config_source = (repo / "gateway/config.py").read_text(encoding="utf-8")
    assert "discover_named_telegram_accounts" in config_source
