"""Session-bound /retain using Hermes' official command-plugin API.

The scheduler/exporter and its fixed remote target remain unchanged. Settings are
profile-local, explicit launch paths; this package is opt-in and performs no work
at import or registration time.
"""
from pathlib import Path

from gateway.config import load_gateway_config
from gateway.slash_access import policy_for_source
from hermes_constants import get_hermes_home
from fork_features.hindsight_retain.retain_integrity import schedule_retain


_PATH_SETTINGS = (
    "python_executable", "output_root", "journal_path", "state_db_path",
    "hindsight_config_path",
)


def register(ctx):
    owner_home = get_hermes_home().resolve()

    def retain(raw_args, *, session_id="", platform="", source=None):
        if get_hermes_home().resolve() != owner_home:
            return "Retain 无法排期：插件与当前会话的配置环境不匹配。"
        source_platform = getattr(getattr(source, "platform", None), "value", None)
        if source is None or not platform or platform == "local" or source_platform != platform:
            return "Retain 无法排期：此命令仅支持消息平台的当前会话。"
        if not isinstance(session_id, str) or not session_id:
            return "Retain 无法排期：当前聊天还没有可保存的会话。"
        try:
            policy = policy_for_source(load_gateway_config(), source)
            if not policy.can_run(source.user_id, "retain"):
                return "Retain 无法排期：当前用户没有此命令的权限。"
            settings = {key: ctx.get_config(key) for key in _PATH_SETTINGS}
            if any(not isinstance(value, str) or not Path(value).is_absolute()
                   for value in settings.values()):
                raise ValueError("Retain launch paths must be explicit absolute paths")
            schedule_retain(
                session_id=session_id,
                python_executable=settings["python_executable"],
                output_root=Path(settings["output_root"]),
                journal_path=Path(settings["journal_path"]),
                state_db_path=Path(settings["state_db_path"]),
                hindsight_config_path=Path(settings["hindsight_config_path"]),
                delay_seconds=1200,
                remote_expectation="expected",
            )
        except Exception as exc:
            # Match the existing CLI receipt, never expose credential-bearing errors.
            return f"Retain 排期失败（{type(exc).__name__}）。"
        return "Retain 已排期，将在 20 分钟后保存本次会话。"

    ctx.register_command("retain", retain, "延迟保存本次会话到记忆")
