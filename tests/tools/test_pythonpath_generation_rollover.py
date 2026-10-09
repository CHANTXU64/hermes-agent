"""A live PM process must not leak its superseded dependencies into children."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

# Load the host before the fixture activates a deliberately minimal fake generation.
from gateway.run import GatewayRunner
from tests.tools._child_env_fixtures import child_env


@pytest.fixture
def activated_pm(child_env, monkeypatch):
    from pm import environments as runtime
    from tools.environments import local

    repo = Path(__file__).resolve().parents[2]
    state = runtime.install_state_dir(repo)
    old = state / "environments" / "old" / "venv"
    new = state / "environments" / "new" / "venv"
    for environment in (old, new):
        runtime.site_packages(environment).mkdir(parents=True)
        (environment / "pyvenv.cfg").write_text("home = test\n", encoding="utf-8")
    facts = runtime.runtime_facts_path(repo)

    def select(environment):
        facts.write_text(json.dumps({"packages": {"venv": {"environment": str(environment)}}}),
                         encoding="utf-8")

    # Exercise the real boot producer. PM's store Python does not change sys.prefix.
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.setattr(local, "_in_venv", False)
    monkeypatch.setattr(local, "_hermes_site_packages", None)
    monkeypatch.setattr(runtime, "_ACTIVATED_SITE_PACKAGES", {}, raising=False)
    # Synthetic installed state, not a sealed application payload.
    monkeypatch.setattr(runtime, "payload_command_dir", lambda root: None)
    select(old)
    runtime.activate_dependencies(repo)
    inherited = os.environ["PYTHONPATH"]
    select(new)  # publication changes disk selection, not the running interpreter
    assert os.environ["PYTHONPATH"] == inherited
    return repo, runtime.site_packages(old), runtime.site_packages(new), select


def test_child_import_survives_generation_switch(activated_pm, child_env, monkeypatch):
    from tools.environments import local

    _, old_site, _, _ = activated_pm
    user_lib = child_env / "user-library"
    user_lib.mkdir()
    (user_lib / "retain_dependency_probe.py").write_text("VALUE = 'child-owned'\n", encoding="utf-8")
    (old_site / "retain_dependency_probe.py").write_text(
        "raise ImportError('dependency belongs to the gateway interpreter')\n", encoding="utf-8")
    monkeypatch.setenv("PYTHONPATH", os.environ["PYTHONPATH"] + os.pathsep + str(user_lib))
    env = local.build_subprocess_env()
    result = subprocess.run(
        [sys.executable, "-c", "import retain_dependency_probe as p; print(p.VALUE)"],
        env=env, text=True, capture_output=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "child-owned"
    assert env["PYTHONPATH"] == str(user_lib)


def test_execute_code_own_interpreter_keeps_its_boot_dependencies(activated_pm, child_env):
    from tools.code_execution_env import _build_child_env

    _, old_site, new_site, _ = activated_pm
    (old_site / "pm_boot_probe.py").write_text("VALUE = 'boot-generation'\n", encoding="utf-8")
    env = _build_child_env(rpc_endpoint="unused", rpc_token="synthetic", tmpdir=str(child_env),
                           child_python=sys.executable)
    result = subprocess.run([sys.executable, "-c", "import pm_boot_probe; print(pm_boot_probe.VALUE)"],
                            env=env, text=True, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "boot-generation"
    assert str(new_site) not in env["PYTHONPATH"].split(os.pathsep)


@pytest.mark.parametrize("builder", ["quick", "terminal", "background"])
@pytest.mark.parametrize("selection", ["unchanged", "updated", "record-removed"])
def test_generation_cleanup_preserves_user_path_literals(activated_pm, child_env, monkeypatch, builder, selection):
    from pm import environments as runtime
    from tools.environments import local

    repo, old_site, new_site, select = activated_pm
    if selection == "unchanged":
        select(old_site.parents[2] if os.name != "nt" else old_site.parents[1])
    elif selection == "record-removed":
        runtime.runtime_facts_path(repo).unlink()
    user = [str(child_env / "other-install/environments/old/venv/lib/python3.99/site-packages"),
            "relative/../library", "", str(old_site / "user-subdirectory"), " /user/library ",
            str(repo / "tools"), "relative/../library"]
    owned = [str(repo), str(old_site), str(old_site)]
    if selection == "updated":
        owned.append(str(new_site))
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join([*owned, *user]))
    if builder == "quick":
        result = local.build_subprocess_env()
    elif builder == "terminal":
        result = local._make_run_env({})
    else:
        result = local._sanitize_subprocess_env(dict(os.environ))
    assert result["PYTHONPATH"].split(os.pathsep) == user


def test_cleanup_cache_survives_update_after_first_spawn(activated_pm, monkeypatch):
    from tools.environments import local

    _, old_site, new_site, select = activated_pm
    old = old_site.parents[2] if os.name != "nt" else old_site.parents[1]
    new = new_site.parents[2] if os.name != "nt" else new_site.parents[1]
    select(old)
    monkeypatch.setenv("PYTHONPATH", str(old_site))
    assert "PYTHONPATH" not in local.build_subprocess_env()
    select(new)
    assert "PYTHONPATH" not in local.build_subprocess_env()


def test_provenance_is_checkout_scoped_and_read_only(activated_pm, child_env):
    from pm import environments as runtime

    repo, old_site, _, _ = activated_pm
    assert runtime.activated_site_packages(repo) == (old_site,)
    assert runtime.activated_site_packages(child_env / "another-checkout") == ()
    assert not (child_env / "another-checkout").exists()


def test_execute_code_external_interpreter_drops_boot_dependencies(activated_pm, child_env, monkeypatch):
    from tools.code_execution_env import _build_child_env

    _, old_site, new_site, _ = activated_pm
    # A distinct path ensures the external-interpreter branch, without spawning it.
    external = str(child_env / "project-python")
    monkeypatch.setattr("tools.code_execution_env._uses_hermes_python_environment", lambda _: False)
    user = str(child_env / "project-library")
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join([str(old_site), str(new_site), user]))
    env = _build_child_env(rpc_endpoint="unused", rpc_token="synthetic", tmpdir=str(child_env),
                           child_python=external)
    assert env["PYTHONPATH"].split(os.pathsep) == [str(child_env), user]


@pytest.mark.asyncio
async def test_gateway_exec_quick_command_runs_after_generation_switch(activated_pm, child_env, monkeypatch):
    import shlex
    from gateway.config import Platform
    from gateway.session import SessionSource

    _, old_site, _, _ = activated_pm
    user_lib = child_env / "quick-command-library"
    user_lib.mkdir()
    (user_lib / "quick_dep.py").write_text("VALUE = 'quick-command-ok'\n", encoding="utf-8")
    (old_site / "quick_dep.py").write_text("raise ImportError('wrong-runtime')\n", encoding="utf-8")
    monkeypatch.setenv("PYTHONPATH", os.environ["PYTHONPATH"] + os.pathsep + str(user_lib))
    command = shlex.join([sys.executable, "-c", "import quick_dep; print(quick_dep.VALUE)"])
    runner = GatewayRunner.__new__(GatewayRunner)
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="synthetic", chat_type="dm")
    result = await runner._hm_run_exec_quick_command("probe", command, {}, source)
    assert result == "quick-command-ok"
