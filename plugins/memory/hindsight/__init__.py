"""Hindsight memory plugin — MemoryProvider interface.

Long-term memory with knowledge graph, entity resolution, and multi-strategy
retrieval. Supports cloud (API key) and local modes.

Configurable request timeout via HINDSIGHT_TIMEOUT env var or config.json.
Configurable embedded daemon idle timeout via HINDSIGHT_IDLE_TIMEOUT env var
or config.json idle_timeout.

Original PR #1811 by benfrank241, adapted to MemoryProvider ABC.

Config via environment variables:
  HINDSIGHT_API_KEY                — API key for Hindsight Cloud
  HINDSIGHT_BANK_ID                — memory bank identifier (default: hermes)
  HINDSIGHT_BUDGET                 — recall budget: low/mid/high (default: mid)
  HINDSIGHT_API_URL                — API endpoint
  HINDSIGHT_MODE                   — cloud or local (default: cloud)
  HINDSIGHT_TIMEOUT                — API request timeout in seconds (default: 120)
  HINDSIGHT_IDLE_TIMEOUT           — embedded daemon idle timeout seconds; 0 disables shutdown (default: 300)
  HINDSIGHT_EMBED_PORT_HEALTH_GRACE_TIMEOUT — seconds to wait for a slow embedded daemon /health before treating it as stale (default: 30; set via config.json port_health_grace_timeout)
  HINDSIGHT_RETAIN_TAGS            — comma-separated tags attached to retained memories
  HINDSIGHT_RETAIN_OBSERVATION_SCOPES — observation scoping for retained memories: per_tag/combined/all_combinations, or a JSON list of tag-lists for custom scopes
  HINDSIGHT_RETAIN_SOURCE          — metadata source value attached to retained memories
  HINDSIGHT_RETAIN_USER_PREFIX     — label used before user turns in retained transcripts
  HINDSIGHT_RETAIN_ASSISTANT_PREFIX — label used before assistant turns in retained transcripts

Or via $HERMES_HOME/hindsight/config.json (profile-scoped), falling back to
~/.hindsight/config.json (legacy, shared) for backward compatibility.
"""

from __future__ import annotations

import asyncio
import atexit
import concurrent.futures
import importlib
import json
import logging
import os
import queue
import re
import sqlite3
import sys
import threading
import time

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List

from agent.secret_scope import get_secret

from agent.memory_provider import MemoryProvider
from hermes_constants import get_hermes_home
from tools.registry import tool_error
from hermes_cli.config import cfg_get
from .recall_preprocessor import run_recall_preprocessor

logger = logging.getLogger(__name__)

_DEFAULT_API_URL = "https://api.hindsight.vectorize.io"
_DEFAULT_LOCAL_URL = "http://localhost:8888"
# Keep in sync with tools/lazy_deps.py ("memory.hindsight") and plugin.yaml.
_MIN_CLIENT_VERSION = "0.6.1"
_DEFAULT_TIMEOUT = 120  # seconds — cloud API can take 30-40s per request
_DEFAULT_IDLE_TIMEOUT = 300  # seconds — Hindsight embedded daemon default
_PREFETCH_MAX_SEQUENTIAL_SYNC_RECALLS = 2
_PREFETCH_OUTER_TIMEOUT_GRACE_SECONDS = 1.0
# Mirrors hindsight-integrations/openclaw — Hindsight 0.5.0 added
# `update_mode='append'` semantics on retain (vectorize-io/hindsight#932).
# Without it, reusing a stable session-scoped document_id silently
# overwrites prior turns server-side, so we keep the per-process
# unique document_id fallback for older APIs.
_MIN_VERSION_FOR_UPDATE_MODE_APPEND = "0.5.0"
_VALID_BUDGETS = {"low", "mid", "high"}
_PROVIDER_DEFAULT_MODELS = {
    "openai": "gpt-4o-mini",
    "anthropic": "claude-haiku-4-5",
    "gemini": "gemini-3.6-flash",
    "groq": "openai/gpt-oss-120b",
    "openrouter": "qwen/qwen3.5-9b",
    "minimax": "MiniMax-M2.7",
    "ollama": "gemma3:12b",
    "lmstudio": "local-model",
    "openai_compatible": "your-model-name",
}


def _parse_int_setting(value: Any, default: int) -> int:
    """Parse an integer config/env value, falling back on invalid input."""
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        logger.warning("Invalid integer Hindsight setting %r; using default %s", value, default)
        return default

def _parse_float_setting(value: Any, default: float) -> float:
    """Parse a float config/env value, falling back on invalid input."""
    if value is None or value == "":
        return default
    try:
        parsed = float(value)
        return parsed if parsed > 0 else default
    except (TypeError, ValueError):
        logger.warning("Invalid float Hindsight setting %r; using default %s", value, default)
        return default


def _parse_bool_setting(value: Any, default: bool) -> bool:
    """Parse a boolean config/env value, accepting common string forms."""
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    return bool(value)


# Env var the embedded daemon manager reads (at import time, as a module-level
# constant) to size the grace window it waits for a slow /health before
# declaring a daemon stale and killing it. Default upstream is 30s; on
# resource-contended hosts a busy daemon can exceed a single 2s health check
# and get needlessly killed + restarted (issue #13125 comment thread). We
# surface it as plugin config so users can raise it without hand-setting an
# env var, consistent with "config.json, not raw env vars".
_PORT_HEALTH_GRACE_ENV = "HINDSIGHT_EMBED_PORT_HEALTH_GRACE_TIMEOUT"


def _export_port_health_grace_timeout(config: dict[str, Any]) -> None:
    """Export the embedded-daemon health grace timeout to the process env.

    Must run BEFORE ``hindsight_embed.daemon_embed_manager`` is imported,
    because the package reads the env var into a module-level constant at
    import time. We only set it when the user configured a value AND the
    env var isn't already set, so an explicit env override always wins.
    """
    raw = config.get("port_health_grace_timeout")
    if raw is None or raw == "":
        return
    try:
        seconds = float(raw)
    except (TypeError, ValueError):
        logger.warning(
            "Invalid Hindsight port_health_grace_timeout %r; ignoring.", raw
        )
        return
    if seconds < 0:
        logger.warning(
            "Negative Hindsight port_health_grace_timeout %r; ignoring.", raw
        )
        return
    # setdefault: an explicit env var the operator set wins over config.
    os.environ.setdefault(_PORT_HEALTH_GRACE_ENV, repr(seconds))


def _check_local_runtime() -> tuple[bool, str | None]:
    """Return whether local embedded Hindsight imports cleanly.

    On older CPUs, importing the local Hindsight stack can raise a runtime
    error from NumPy before the daemon starts. Treat that as "unavailable"
    so Hermes can degrade gracefully instead of repeatedly trying to start
    a broken local memory backend.

    The embedded daemon computes embeddings via ``sentence_transformers``
    (transformers + huggingface-hub). Importing ``hindsight`` /
    ``hindsight_embed`` alone succeeds even when that stack is broken, so
    without importing it here the probe would falsely report the backend
    healthy and ``hermes memory status`` would stay green while the daemon
    aborts at startup on every retain/recall. Import it too so the probe (and
    status) reports the real ImportError.
    """
    try:
        importlib.import_module("hindsight")
        importlib.import_module("hindsight_embed.daemon_embed_manager")
        importlib.import_module("sentence_transformers")
        return True, None
    except Exception as exc:
        return False, str(exc)


def _ensure_cloud_client_dependency() -> None:
    """Install the Hindsight cloud client lazily before importing it."""
    try:
        from tools.lazy_deps import ensure as _lazy_ensure
        _lazy_ensure("memory.hindsight", prompt=False)
    except ImportError:
        pass
    except Exception as exc:
        raise ImportError(str(exc)) from exc


# ---------------------------------------------------------------------------
# Hindsight API capability probe — mirrors hindsight-integrations/openclaw.
# ---------------------------------------------------------------------------

# Cache of API_URL -> bool (whether that API supports update_mode='append').
# Probed once per URL per process — every provider talking to the same API
# gets the same answer without re-hitting /version on each initialize().
_append_capability_cache: Dict[str, bool] = {}
_append_capability_lock = threading.Lock()


def _meets_minimum_version(actual: str | None, required: str) -> bool:
    """Return True if *actual* ≥ *required* (semver). False on missing/invalid."""
    if not actual:
        return False
    try:
        from packaging.version import Version
        return Version(actual) >= Version(required)
    except Exception:
        return False


def _fetch_hindsight_api_version(api_url: str, api_key: str | None = None,
                                 timeout: float = 5.0) -> str | None:
    """GET ``<api_url>/version`` and return the version string (or None on failure).

    Hindsight's `/version` endpoint returns ``{"version": "0.5.6", ...}``.
    Any failure (timeout, 404, malformed JSON, missing key) → None, which
    the caller treats as "legacy API, no update_mode support".
    """
    import urllib.error
    import urllib.request
    if not api_url:
        return None
    url = api_url.rstrip("/") + "/version"
    req = urllib.request.Request(url)
    if api_key:
        req.add_header("Authorization", f"Bearer {api_key}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            payload = resp.read().decode("utf-8", errors="replace")
        data = json.loads(payload)
    except Exception as exc:
        logger.debug("Hindsight /version probe failed for %s: %s", url, exc)
        return None
    if not isinstance(data, dict):
        return None
    version = data.get("version") or data.get("api_version")
    return str(version) if version else None


def _check_api_supports_update_mode_append(
    api_url: str,
    api_key: str | None = None,
    timeout: float | None = None,
) -> bool:
    """Cached capability check for ``update_mode='append'`` on *api_url*.

    Probes once per URL per process. Returns False on any probe failure —
    that's the safe default: a per-process unique ``document_id`` and no
    ``update_mode`` keeps the resume-overwrite fix (#6654) intact.
    """
    if not api_url:
        return False
    with _append_capability_lock:
        if api_url in _append_capability_cache:
            return _append_capability_cache[api_url]
    probe_timeout = 5.0 if timeout is None else max(0.001, min(5.0, timeout))
    version = _fetch_hindsight_api_version(
        api_url,
        api_key,
        timeout=probe_timeout,
    )
    supported = _meets_minimum_version(version, _MIN_VERSION_FOR_UPDATE_MODE_APPEND)
    with _append_capability_lock:
        # Re-check after acquiring the lock in case a concurrent probe filled it.
        cached = _append_capability_cache.get(api_url)
        if cached is None:
            _append_capability_cache[api_url] = supported
        else:
            supported = cached
    if not supported:
        logger.warning(
            "Hindsight API at %s reports version %r, older than %s. "
            "Falling back to per-process document_id — retains across "
            "processes/sessions create separate documents instead of "
            "appending to a session-scoped one. Upgrade Hindsight to "
            "%s+ to enable update_mode='append' deduplication.",
            api_url, version, _MIN_VERSION_FOR_UPDATE_MODE_APPEND,
            _MIN_VERSION_FOR_UPDATE_MODE_APPEND,
        )
    else:
        logger.debug("Hindsight API %s version %s supports update_mode='append'",
                     api_url, version)
    return supported


# ---------------------------------------------------------------------------
# Dedicated event loop for Hindsight async calls (one per process, reused).
# Avoids creating ephemeral loops that leak aiohttp sessions.
# ---------------------------------------------------------------------------

_loop: asyncio.AbstractEventLoop | None = None
_loop_thread: threading.Thread | None = None
_loop_lock = threading.Lock()

# Sentinel pushed to the per-provider retain queue to wake the writer for a
# clean exit. A unique object so it can never collide with a real job.
_WRITER_SENTINEL = object()


def _get_loop() -> asyncio.AbstractEventLoop:
    """Return a long-lived event loop running on a background thread."""
    global _loop, _loop_thread
    with _loop_lock:
        if _loop is not None and _loop.is_running():
            return _loop
        _loop = asyncio.new_event_loop()

        def _run():
            asyncio.set_event_loop(_loop)
            _loop.run_forever()

        _loop_thread = threading.Thread(target=_run, daemon=True, name="hindsight-loop")
        _loop_thread.start()
        return _loop


def _run_sync(coro, timeout: float = _DEFAULT_TIMEOUT):
    """Schedule *coro* on the shared loop and block until done."""
    from agent.async_utils import safe_schedule_threadsafe
    loop = _get_loop()
    future = safe_schedule_threadsafe(coro, loop)
    if future is None:
        raise RuntimeError("Hindsight loop unavailable")
    try:
        return future.result(timeout=timeout)
    except concurrent.futures.TimeoutError:
        future.cancel()
        raise


# ---------------------------------------------------------------------------
# Backward-compatible alias — instances use self._run_sync() instead.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Tool schemas
# ---------------------------------------------------------------------------

RETAIN_SCHEMA = {
    "name": "hindsight_retain",
    "description": (
        "Store information to long-term memory. Hindsight automatically "
        "extracts structured facts, resolves entities, and indexes for retrieval."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "content": {"type": "string", "description": "The information to store."},
            "context": {"type": "string", "description": "Short label (e.g. 'user preference', 'project decision')."},
            "tags": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional per-call tags to merge with configured default retain tags.",
            },
        },
        "required": ["content"],
    },
}

RECALL_SCHEMA = {
    "name": "hindsight_recall",
    "description": (
        "Search long-term memory. Returns memories ranked by relevance using "
        "semantic search, keyword matching, entity graph traversal, and reranking."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "What to search for."},
        },
        "required": ["query"],
    },
}

REFLECT_SCHEMA = {
    "name": "hindsight_reflect",
    "description": (
        "Synthesize a reasoned answer from long-term memories. Unlike recall, "
        "this reasons across all stored memories to produce a coherent response."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "The question to reflect on."},
        },
        "required": ["query"],
    },
}

RETAIN_SESSION_SCHEMA = {
    "name": "hindsight_retain_session",
    "description": (
        "Manually flush the Hindsight provider's buffered conversation turns "
        "using the same document, metadata, tags, and serialization path as automatic retain."
    ),
    "parameters": {
        "type": "object",
        "properties": {},
        "required": [],
    },
}


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def _load_config() -> dict:
    """Load config from profile-scoped path, legacy path, or env vars.

    Resolution order:
      1. $HERMES_HOME/hindsight/config.json  (profile-scoped)
      2. ~/.hindsight/config.json             (legacy, shared)
      3. Environment variables
    """
    from pathlib import Path

    # Profile-scoped path (preferred)
    profile_path = get_hermes_home() / "hindsight" / "config.json"
    if profile_path.exists():
        try:
            return json.loads(profile_path.read_text(encoding="utf-8"))
        except Exception:
            pass

    # Legacy shared path (backward compat)
    legacy_path = Path.home() / ".hindsight" / "config.json"
    if legacy_path.exists():
        try:
            return json.loads(legacy_path.read_text(encoding="utf-8"))
        except Exception:
            pass

    return {
        "mode": os.environ.get("HINDSIGHT_MODE", "cloud"),
        "apiKey": get_secret("HINDSIGHT_API_KEY", ""),
        "timeout": _parse_int_setting(os.environ.get("HINDSIGHT_TIMEOUT"), _DEFAULT_TIMEOUT),
        "idle_timeout": _parse_int_setting(os.environ.get("HINDSIGHT_IDLE_TIMEOUT"), _DEFAULT_IDLE_TIMEOUT),
        "retain_tags": os.environ.get("HINDSIGHT_RETAIN_TAGS", ""),
        "observation_scopes": os.environ.get("HINDSIGHT_RETAIN_OBSERVATION_SCOPES", ""),
        "retain_source": os.environ.get("HINDSIGHT_RETAIN_SOURCE", ""),
        "retain_user_prefix": os.environ.get("HINDSIGHT_RETAIN_USER_PREFIX", "User"),
        "retain_assistant_prefix": os.environ.get("HINDSIGHT_RETAIN_ASSISTANT_PREFIX", "Assistant"),
        "banks": {
            "hermes": {
                "bankId": os.environ.get("HINDSIGHT_BANK_ID", "hermes"),
                "budget": os.environ.get("HINDSIGHT_BUDGET", "mid"),
                "enabled": True,
            }
        },
    }


def get_retain_on_new_settings() -> tuple[bool, float]:
    """Return profile-scoped retain-before-reset settings without starting a provider."""
    config = _load_config()
    enabled = _parse_bool_setting(config.get("retain_on_new"), False)
    timeout = max(
        0.1,
        _parse_float_setting(config.get("retain_on_new_timeout_seconds"), 30.0),
    )
    return enabled, timeout


def _normalize_retain_tags(value: Any) -> List[str]:
    """Normalize tag config/tool values to a deduplicated list of strings."""
    if value is None:
        return []

    raw_items: list[Any]
    if isinstance(value, list):
        raw_items = value
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        if text.startswith("["):
            try:
                parsed = json.loads(text)
            except Exception:
                parsed = None
            if isinstance(parsed, list):
                raw_items = parsed
            else:
                raw_items = text.split(",")
        else:
            raw_items = text.split(",")
    else:
        raw_items = [value]

    normalized = []
    seen = set()
    for item in raw_items:
        tag = str(item).strip()
        if not tag or tag in seen:
            continue
        seen.add(tag)
        normalized.append(tag)
    return normalized


_OBSERVATION_SCOPE_KEYWORDS = {"per_tag", "combined", "all_combinations"}


def _normalize_observation_scopes(value: Any) -> Any:
    """Normalize an observation_scopes config value to a Hindsight-accepted form.

    Returns one of:
      * ``None`` — nothing configured; Hindsight applies its ``combined`` default.
      * a keyword string — ``"per_tag"`` / ``"combined"`` / ``"all_combinations"``.
      * ``list[list[str]]`` — custom scopes, one inner list per consolidation pass.

    Accepts a keyword string, a JSON-encoded list, a flat list of tags (treated as
    a single scope), or a list of tag-lists. Anything unrecognized yields ``None``
    so we never send an invalid payload.
    """
    if value is None:
        return None

    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text in _OBSERVATION_SCOPE_KEYWORDS:
            return text
        if text.startswith("["):
            try:
                parsed = json.loads(text)
            except Exception:
                return None
            return _normalize_observation_scopes(parsed)
        return None

    if isinstance(value, (list, tuple)):
        # A flat list of tag strings is one scope; a list of lists is many.
        if all(isinstance(entry, str) for entry in value):
            inner = [entry.strip() for entry in value if entry.strip()]
            return [inner] if inner else None
        scopes: list[list[str]] = []
        for entry in value:
            if isinstance(entry, (list, tuple)):
                inner = [str(tag).strip() for tag in entry if str(tag).strip()]
                if inner:
                    scopes.append(inner)
            elif isinstance(entry, str) and entry.strip():
                scopes.append([entry.strip()])
        return scopes or None

    return None


def _utc_timestamp() -> str:
    """Return current UTC timestamp in ISO-8601 with milliseconds and Z suffix."""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _embedded_profile_name(config: dict[str, Any]) -> str:
    """Return the Hindsight embedded profile name for this Hermes config."""
    profile = config.get("profile", "hermes")
    return str(profile or "hermes")


def _load_simple_env(path) -> dict[str, str]:
    """Parse a simple KEY=VALUE env file, ignoring comments and blank lines."""
    if not path.exists():
        return {}

    values: dict[str, str] = {}
    # utf-8-sig, not plain utf-8: this is also used on the Hermes .env during
    # post_setup, and a Notepad BOM would otherwise stick to the first key.
    for line in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def _build_embedded_profile_env(config: dict[str, Any], *, llm_api_key: str | None = None) -> dict[str, str]:
    """Build the profile-scoped env file that standalone hindsight-embed consumes."""
    current_key = llm_api_key
    if current_key is None:
        current_key = (
            config.get("llmApiKey")
            or config.get("llm_api_key")
            or get_secret("HINDSIGHT_LLM_API_KEY", "")
        )

    current_provider = config.get("llm_provider", "")
    current_model = config.get("llm_model", "")
    current_base_url = config.get("llm_base_url") or os.environ.get("HINDSIGHT_API_LLM_BASE_URL", "")

    # The embedded daemon expects OpenAI wire format for these providers.
    daemon_provider = "openai" if current_provider in {"openai_compatible", "openrouter"} else current_provider

    env_values = {
        "HINDSIGHT_API_LLM_PROVIDER": str(daemon_provider),
        "HINDSIGHT_API_LLM_API_KEY": str(current_key or ""),
        "HINDSIGHT_API_LLM_MODEL": str(current_model),
        "HINDSIGHT_API_LOG_LEVEL": "info",
    }
    if current_base_url:
        env_values["HINDSIGHT_API_LLM_BASE_URL"] = str(current_base_url)

    idle_timeout = (
        config.get("idle_timeout")
        if config.get("idle_timeout") is not None
        else os.environ.get("HINDSIGHT_IDLE_TIMEOUT")
    )
    if idle_timeout is not None and idle_timeout != "":
        env_values["HINDSIGHT_EMBED_DAEMON_IDLE_TIMEOUT"] = str(
            _parse_int_setting(idle_timeout, _DEFAULT_IDLE_TIMEOUT)
        )
    return env_values


def _embedded_profile_env_path(config: dict[str, Any]):
    from pathlib import Path

    return Path.home() / ".hindsight" / "profiles" / f"{_embedded_profile_name(config)}.env"


def _secure_write_profile_env(profile_env, content: str) -> None:
    """Create/overwrite *profile_env* with owner-only (0600) permissions.

    The file carries the embedded daemon's plaintext LLM API key
    (``HINDSIGHT_API_LLM_API_KEY``), so it must never be created with the
    default umask-derived mode. A pre-existing file is tightened *before*
    the new secret bytes are written.
    """
    if profile_env.exists():
        try:
            os.chmod(profile_env, 0o600)
        except OSError:
            pass
    fd = os.open(str(profile_env), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(content)


def _validate_profile_env_permissions(profile_env) -> None:
    """Post-write validation: the secret file must be owner-only on POSIX."""
    if os.name != "posix":
        # POSIX mode bits do not model Windows ACLs.
        return
    import stat

    mode = stat.S_IMODE(profile_env.stat().st_mode)
    if mode != 0o600:
        try:
            os.chmod(profile_env, 0o600)
        except OSError:
            pass
        mode = stat.S_IMODE(profile_env.stat().st_mode)
        if mode != 0o600:
            raise PermissionError(
                f"Embedded Hindsight profile environment is not owner-only: {profile_env}"
            )


def _materialize_embedded_profile_env(config: dict[str, Any], *, llm_api_key: str | None = None):
    """Write the profile-scoped env file that standalone hindsight-embed uses."""
    profile_env = _embedded_profile_env_path(config)
    profile_env.parent.mkdir(parents=True, exist_ok=True)
    env_values = _build_embedded_profile_env(config, llm_api_key=llm_api_key)
    content = "".join(f"{key}={value}\n" for key, value in env_values.items())
    try:
        _secure_write_profile_env(profile_env, content)
        _validate_profile_env_permissions(profile_env)
    except BaseException:
        # Never leave a plaintext API key behind in a file whose permissions
        # could not be verified.
        try:
            profile_env.unlink()
        except OSError:
            pass
        raise
    return profile_env

def _sanitize_bank_segment(value: str) -> str:
    """Sanitize a bank_id_template placeholder value.

    Bank IDs should be safe for URL paths and filesystem use. Replaces any
    character that isn't alphanumeric, dash, or underscore with a dash, and
    collapses runs of dashes.
    """
    if not value:
        return ""
    out = []
    prev_dash = False
    for ch in str(value):
        if ch.isalnum() or ch == "-" or ch == "_":
            out.append(ch)
            prev_dash = False
        else:
            if not prev_dash:
                out.append("-")
                prev_dash = True
    return "".join(out).strip("-_")


def _resolve_bank_id_template(template: str, fallback: str, **placeholders: str) -> str:
    """Resolve a bank_id template string with the given placeholders.

    Supported placeholders (each is sanitized before substitution):
      {profile}   — active Hermes profile name (from agent_identity)
      {workspace} — Hermes workspace name (from agent_workspace)
      {platform}  — "cli", "telegram", "discord", etc.
      {user}      — platform user id (gateway sessions)
      {session}   — current session id

    Missing/empty placeholders are rendered as the empty string and then
    collapsed — e.g. ``hermes-{user}`` with no user becomes ``hermes``.

    If the template is empty, resolution falls back to *fallback*.
    Returns the sanitized bank id.
    """
    if not template:
        return fallback
    sanitized = {k: _sanitize_bank_segment(v) for k, v in placeholders.items()}
    try:
        rendered = template.format(**sanitized)
    except (KeyError, IndexError) as exc:
        logger.warning("Invalid bank_id_template %r: %s — using fallback %r",
                       template, exc, fallback)
        return fallback
    while "--" in rendered:
        rendered = rendered.replace("--", "-")
    while "__" in rendered:
        rendered = rendered.replace("__", "_")
    rendered = rendered.strip("-_")
    return rendered or fallback


# ---------------------------------------------------------------------------
# MemoryProvider implementation
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class _RecallSnapshot:
    query: str
    results: tuple[str, ...]


class HindsightMemoryProvider(MemoryProvider):
    """Hindsight long-term memory with knowledge graph and multi-strategy retrieval."""

    def backup_paths(self) -> List[str]:
        """Hindsight's legacy shared config and embedded-mode profile env
        files live under ~/.hindsight (see _load_config / line ~509)."""
        try:
            from pathlib import Path
            legacy_dir = Path.home() / ".hindsight"
            return [str(legacy_dir)]
        except Exception:
            return []

    def __init__(self):
        self._config = None
        self._api_key = None
        self._api_url = _DEFAULT_API_URL
        self._bank_id = "hermes"
        self._budget = "mid"
        self._mode = "cloud"
        self._llm_base_url = ""
        self._memory_mode = "hybrid"  # "context", "tools", or "hybrid"
        self._prefetch_method = "recall"  # "recall" or "reflect"
        self._retain_tags: List[str] = []
        self._retain_source = ""
        self._retain_user_prefix = "User"
        self._retain_assistant_prefix = "Assistant"
        self._platform = ""
        self._user_id = ""
        self._user_name = ""
        self._chat_id = ""
        self._chat_name = ""
        self._chat_type = ""
        self._thread_id = ""
        self._agent_identity = ""
        self._agent_workspace = ""
        self._turn_index = 0
        self._client = None
        self._timeout = _DEFAULT_TIMEOUT
        self._idle_timeout = _DEFAULT_IDLE_TIMEOUT
        self._prefetch_result = ""
        self._prefetch_snapshot: _RecallSnapshot | None = None
        self._prefetch_lock = threading.Lock()
        self._prefetch_generation = 0
        self._active_prefetch_turn: tuple[str, int] | None = None
        # Single-writer model for retain. sync_turn() enqueues; the writer
        # thread drains sequentially. Avoids spawning ad-hoc threads that
        # can race the interpreter shutdown and emit "cannot schedule new
        # futures after interpreter shutdown" / "Unclosed client session".
        self._retain_queue: queue.Queue = queue.Queue()
        self._writer_thread: threading.Thread | None = None
        self._shutting_down = threading.Event()
        self._atexit_registered = False

        # Legacy alias — older tests/callers reference _sync_thread directly.
        # Points at _writer_thread once the writer is running.
        self._sync_thread = None
        self._session_id = ""
        self._parent_session_id = ""
        self._retain_document_id = ""
        self._document_id = ""
        self._retain_store_path = get_hermes_home() / "hindsight" / "retain_turns.sqlite3"

        # Tags
        self._tags: list[str] | None = None
        self._recall_tags: list[str] | None = None
        self._recall_tags_match = "any"

        # Retain controls
        self._auto_retain = True
        self._retain_every_n_turns = 1
        self._retain_async = True
        self._retain_on_new = False
        self._retain_on_new_timeout_seconds = 30.0

        self._retain_context = "conversation between Hermes Agent and the User"
        self._turn_counter = 0
        self._session_turns: list[str] = []  # accumulates ALL turns for the session
        # Track retain flush progress. Append-capable APIs only receive turns
        # after _last_queued_flush_count; legacy/overwrite APIs still resend the
        # full session. Separate queued/flushed counters let failed async append
        # jobs roll back safely, and the generation guard prevents old jobs from
        # mutating a newer session.
        self._last_flushed_turn_count = 0
        self._last_queued_flush_count = 0
        self._retain_flush_pending = False
        self._retain_force_replace = False
        self._retain_generation = 0
        self._retain_flush_lock = threading.Lock()

        # Recall controls
        self._auto_recall = True
        self._recall_max_tokens = 4096
        # Default to observation-only recall. Observations are Hindsight's
        # consolidated knowledge layer — deduplicated, evidence-grounded
        # beliefs built from many raw facts, with proof counts and
        # freshness signals (see hindsight.vectorize.io/developer/observations).
        # Including raw world/experience facts re-ships the supporting
        # evidence that observations already summarize, burning the
        # `recall_max_tokens` budget. Users can restore the broader
        # recall via the `recall_types` config key.
        self._recall_types: list[str] = ["observation"]
        self._recall_prompt_preamble = ""
        self._recall_max_input_chars = 800
        self._recall_sync_on_cache_miss = True
        self._recall_sync_timeout_seconds = 5.0

        # Bank
        self._bank_mission = ""
        self._bank_retain_mission: str | None = None
        self._bank_id_template = ""

    @property
    def name(self) -> str:
        return "hindsight"

    def prefetch_timeout_seconds(self) -> float:
        """Budget the complete bounded prefetch pipeline for MemoryManager."""
        from .recall_preprocessor import get_recall_preprocessor_budget_seconds

        sync_recall_attempts = (
            _PREFETCH_MAX_SEQUENTIAL_SYNC_RECALLS
            if self._recall_sync_on_cache_miss
            else 1
        )
        return (
            get_recall_preprocessor_budget_seconds()
            + (sync_recall_attempts * self._recall_sync_timeout_seconds)
            + _PREFETCH_OUTER_TIMEOUT_GRACE_SECONDS
        )

    def is_available(self) -> bool:
        try:
            cfg = _load_config()
            mode = cfg.get("mode", "cloud")
            if mode in {"local", "local_embedded"}:
                available, _ = _check_local_runtime()
                return available
            if mode == "local_external":
                return True
            has_key = bool(
                cfg.get("apiKey")
                or cfg.get("api_key")
                or get_secret("HINDSIGHT_API_KEY", "")
            )
            has_url = bool(cfg.get("api_url") or os.environ.get("HINDSIGHT_API_URL", ""))
            return has_key or has_url
        except Exception:
            return False

    def save_config(self, values, hermes_home):
        """Write config to $HERMES_HOME/hindsight/config.json."""
        import json
        from pathlib import Path
        config_dir = Path(hermes_home) / "hindsight"
        config_dir.mkdir(parents=True, exist_ok=True)
        config_path = config_dir / "config.json"
        existing = {}
        if config_path.exists():
            try:
                existing = json.loads(config_path.read_text(encoding="utf-8"))
            except Exception:
                pass
        existing.update(values)
        from utils import atomic_json_write
        atomic_json_write(config_path, existing, mode=0o600)

    def post_setup(self, hermes_home: str, config: dict) -> None:
        """Custom setup wizard — installs only the deps needed for the selected mode."""
        import subprocess
        import shutil
        import sys
        from pathlib import Path

        from hermes_cli.config import save_config
        from hermes_cli.secret_prompt import masked_secret_prompt

        from hermes_cli.memory_setup import _CANCELLED, _curses_select, _print_cancelled_setup

        print("\n  Configuring Hindsight memory:\n")

        existing_config = self._config if isinstance(self._config, dict) else _load_config()
        if not isinstance(existing_config, dict):
            existing_config = {}

        # Step 1: Mode selection
        mode_values = ["cloud", "local_embedded", "local_external"]
        mode_items = [
            ("Cloud", "Hindsight Cloud API (lightweight, just needs an API key)"),
            ("Local Embedded", "Run Hindsight locally (downloads ~200MB, needs LLM key)"),
            ("Local External", "Connect to an existing Hindsight instance"),
        ]
        existing_mode = existing_config.get("mode")
        mode_default_idx = mode_values.index(existing_mode) if existing_mode in mode_values else 0
        mode_idx = _curses_select("  Select mode", mode_items, default=mode_default_idx, cancel_returns=_CANCELLED)
        if mode_idx == _CANCELLED:
            _print_cancelled_setup()
            return
        mode = mode_values[mode_idx]

        provider_config: dict = dict(existing_config)
        provider_config["mode"] = mode
        env_writes: dict = {}

        # Step 2: Install/upgrade deps for selected mode
        cloud_dep = f"hindsight-client>={_MIN_CLIENT_VERSION}"
        local_dep = "hindsight-all"
        if mode == "local_embedded":
            deps_to_install = [local_dep]
        elif mode == "local_external":
            deps_to_install = [cloud_dep]
        else:
            deps_to_install = [cloud_dep]

        llm_provider = ""
        if mode == "local_embedded":
            providers_list = list(_PROVIDER_DEFAULT_MODELS.keys())
            llm_items = [
                (p, f"default model: {_PROVIDER_DEFAULT_MODELS[p]}")
                for p in providers_list
            ]
            existing_llm_provider = provider_config.get("llm_provider")
            llm_default_idx = providers_list.index(existing_llm_provider) if existing_llm_provider in providers_list else 0
            llm_idx = _curses_select(
                "  Select LLM provider",
                llm_items,
                default=llm_default_idx,
                cancel_returns=_CANCELLED,
            )
            if llm_idx == _CANCELLED:
                _print_cancelled_setup()
                return
            llm_provider = providers_list[llm_idx]
            provider_config["llm_provider"] = llm_provider

        print("\n  Checking dependencies...")
        # Environment-aware install: sealed hosted venvs redirect to the durable
        # data-volume target instead of writing to /opt/hermes (NS-605).
        from tools.lazy_deps import install_specs

        outcome = install_specs(deps_to_install, timeout=120)
        if outcome.ok:
            print("  ✓ Dependencies up to date")
        elif outcome.blocked:
            print(f"  ⚠ Cannot install dependencies: {outcome.reason}")
        else:
            print(f"  ⚠ Install failed:\n{(outcome.stderr or '').strip()}")
            print(f"  Run manually: uv pip install --python {sys.executable} {' '.join(deps_to_install)}")

        # Step 3: Mode-specific config
        if mode == "cloud":
            print("\n  Get your API key at https://ui.hindsight.vectorize.io\n")
            existing_key = get_secret("HINDSIGHT_API_KEY", "") or ""
            if existing_key:
                masked = f"...{existing_key[-4:]}" if len(existing_key) > 4 else "set"
                sys.stdout.write(f"  API key (current: {masked}, blank to keep): ")
                sys.stdout.flush()
                api_key = masked_secret_prompt("") if sys.stdin.isatty() else sys.stdin.readline().strip()
            else:
                sys.stdout.write("  API key: ")
                sys.stdout.flush()
                api_key = masked_secret_prompt("") if sys.stdin.isatty() else sys.stdin.readline().strip()
            if api_key:
                env_writes["HINDSIGHT_API_KEY"] = api_key

            val = input(f"  API URL [{_DEFAULT_API_URL}]: ").strip()
            if val:
                provider_config["api_url"] = val

        elif mode == "local_external":
            val = input(f"  Hindsight API URL [{_DEFAULT_LOCAL_URL}]: ").strip()
            provider_config["api_url"] = val or _DEFAULT_LOCAL_URL

            sys.stdout.write("  API key (optional, blank to skip): ")
            sys.stdout.flush()
            api_key = masked_secret_prompt("") if sys.stdin.isatty() else sys.stdin.readline().strip()
            if api_key:
                env_writes["HINDSIGHT_API_KEY"] = api_key

        else:  # local_embedded
            if llm_provider == "openai_compatible":
                existing_base_url = provider_config.get("llm_base_url", "")
                prompt = "  LLM endpoint URL (e.g. http://192.168.1.10:8080/v1)"
                if existing_base_url:
                    prompt += f" [{existing_base_url}]"
                prompt += ": "
                val = input(prompt).strip()
                if val:
                    provider_config["llm_base_url"] = val
            elif llm_provider == "openrouter":
                provider_config["llm_base_url"] = "https://openrouter.ai/api/v1"

            provider_default_model = _PROVIDER_DEFAULT_MODELS.get(llm_provider, "gpt-4o-mini")
            current_model = provider_config.get("llm_model") or provider_default_model
            val = input(f"  LLM model [{current_model}]: ").strip()
            provider_config["llm_model"] = val or current_model

            sys.stdout.write("  LLM API key: ")
            sys.stdout.flush()
            llm_key = masked_secret_prompt("") if sys.stdin.isatty() else sys.stdin.readline().strip()
            if llm_key:
                env_writes["HINDSIGHT_LLM_API_KEY"] = llm_key
            else:
                env_path = Path(hermes_home) / ".env"
                existing_llm_key = ""
                if env_path.exists():
                    # utf-8-sig: a Notepad BOM must not hide the first key.
                    for line in env_path.read_text(encoding="utf-8-sig").splitlines():
                        if line.startswith("HINDSIGHT_LLM_API_KEY="):
                            existing_llm_key = line.split("=", 1)[1]
                            break
                env_writes["HINDSIGHT_LLM_API_KEY"] = existing_llm_key

        # Step 4: Save everything
        provider_config.setdefault("bank_id", "hermes")
        provider_config.setdefault("recall_budget", "mid")
        # Read existing timeout from config if present, otherwise use default.
        # Preserve explicit 0 values instead of treating them as blank.
        existing_timeout = provider_config.get("timeout")
        timeout_val = existing_timeout if existing_timeout is not None else _DEFAULT_TIMEOUT
        provider_config["timeout"] = timeout_val
        env_writes["HINDSIGHT_TIMEOUT"] = str(timeout_val)
        if mode == "local_embedded":
            existing_idle_timeout = provider_config.get("idle_timeout")
            idle_timeout_val = existing_idle_timeout if existing_idle_timeout is not None else _DEFAULT_IDLE_TIMEOUT
            provider_config["idle_timeout"] = idle_timeout_val
            env_writes["HINDSIGHT_IDLE_TIMEOUT"] = str(idle_timeout_val)
        config["memory"]["provider"] = "hindsight"
        save_config(config)

        self.save_config(provider_config, hermes_home)

        if env_writes:
            env_path = Path(hermes_home) / ".env"
            env_path.parent.mkdir(parents=True, exist_ok=True)
            existing_lines = []
            if env_path.exists():
                # utf-8-sig: a Notepad BOM would glue U+FEFF onto the first
                # key, defeating the in-place update below and appending a
                # duplicate line instead.
                existing_lines = env_path.read_text(encoding="utf-8-sig").splitlines()
            updated_keys = set()
            new_lines = []
            for line in existing_lines:
                key_match = line.split("=", 1)[0].strip() if "=" in line and not line.startswith("#") else None
                if key_match and key_match in env_writes:
                    new_lines.append(f"{key_match}={env_writes[key_match]}")
                    updated_keys.add(key_match)
                else:
                    new_lines.append(line)
            for k, v in env_writes.items():
                if k not in updated_keys:
                    new_lines.append(f"{k}={v}")
            env_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")

        if mode == "local_embedded":
            materialized_config = dict(provider_config)
            config_path = Path(hermes_home) / "hindsight" / "config.json"
            try:
                materialized_config = json.loads(config_path.read_text(encoding="utf-8"))
            except Exception:
                pass

            llm_api_key = env_writes.get("HINDSIGHT_LLM_API_KEY", "")
            if not llm_api_key:
                llm_api_key = _load_simple_env(Path(hermes_home) / ".env").get("HINDSIGHT_LLM_API_KEY", "")
            if not llm_api_key:
                llm_api_key = _load_simple_env(_embedded_profile_env_path(materialized_config)).get(
                    "HINDSIGHT_API_LLM_API_KEY",
                    "",
                )

            _materialize_embedded_profile_env(
                materialized_config,
                llm_api_key=llm_api_key or None,
            )

        print(f"\n  ✓ Hindsight memory configured ({mode} mode)")
        if env_writes:
            print("  API keys saved to .env")
        print("\n  Start a new session to activate.\n")

    def get_config_schema(self):
        return [
            {"key": "mode", "description": "Connection mode", "default": "cloud", "choices": ["cloud", "local_embedded", "local_external"]},
            # Cloud mode
            {"key": "api_url", "description": "Hindsight Cloud API URL", "default": _DEFAULT_API_URL, "when": {"mode": "cloud"}},
            {"key": "api_key", "description": "Hindsight Cloud API key", "secret": True, "env_var": "HINDSIGHT_API_KEY", "url": "https://ui.hindsight.vectorize.io", "when": {"mode": "cloud"}},
            # Local external mode
            {"key": "api_url", "description": "Hindsight API URL", "default": _DEFAULT_LOCAL_URL, "when": {"mode": "local_external"}},
            {"key": "api_key", "description": "API key (optional)", "secret": True, "env_var": "HINDSIGHT_API_KEY", "when": {"mode": "local_external"}},
            # Local embedded mode
            {"key": "llm_provider", "description": "LLM provider", "default": "openai", "choices": ["openai", "anthropic", "gemini", "groq", "openrouter", "minimax", "ollama", "lmstudio", "openai_compatible"], "when": {"mode": "local_embedded"}},
            {"key": "llm_base_url", "description": "Endpoint URL (e.g. http://192.168.1.10:8080/v1)", "default": "", "when": {"mode": "local_embedded", "llm_provider": "openai_compatible"}},
            {"key": "llm_api_key", "description": "LLM API key (optional for openai_compatible)", "secret": True, "env_var": "HINDSIGHT_LLM_API_KEY", "when": {"mode": "local_embedded"}},
            {"key": "llm_model", "description": "LLM model", "default": "gpt-4o-mini", "default_from": {"field": "llm_provider", "map": _PROVIDER_DEFAULT_MODELS}, "when": {"mode": "local_embedded"}},
            {"key": "bank_id", "description": "Memory bank name (static fallback when bank_id_template is unset)", "default": "hermes"},
            {"key": "bank_id_template", "description": "Optional template to derive bank_id dynamically. Placeholders: {profile}, {workspace}, {platform}, {user}, {session}. Example: hermes-{profile}", "default": ""},
            {"key": "bank_mission", "description": "Mission/purpose description for the memory bank"},
            {"key": "bank_retain_mission", "description": "Custom extraction prompt for memory retention"},
            {"key": "recall_budget", "description": "Recall thoroughness", "default": "mid", "choices": ["low", "mid", "high"]},
            {"key": "memory_mode", "description": "Memory integration mode", "default": "hybrid", "choices": ["hybrid", "context", "tools"]},
            {"key": "recall_prefetch_method", "description": "Auto-recall method", "default": "recall", "choices": ["recall", "reflect"]},
            {"key": "retain_tags", "description": "Default tags applied to retained memories (comma-separated)", "default": ""},
            {"key": "observation_scopes", "description": "How observations are scoped during consolidation: 'combined' (default — one pass over all tags), 'per_tag' (one isolated observation per tag), 'all_combinations' (every tag subset — expensive), or a JSON list of tag-lists for explicit custom scopes. Empty uses Hindsight's 'combined' default.", "default": ""},
            {"key": "retain_source", "description": "Metadata source value attached to retained memories", "default": ""},
            {"key": "retain_user_prefix", "description": "Label used before user turns in retained transcripts", "default": "User"},
            {"key": "retain_assistant_prefix", "description": "Label used before assistant turns in retained transcripts", "default": "Assistant"},
            {"key": "recall_tags", "description": "Tags to filter when searching memories (comma-separated)", "default": ""},
            {"key": "recall_tags_match", "description": "Tag matching mode for recall", "default": "any", "choices": ["any", "all", "any_strict", "all_strict"]},
            {"key": "recall_types", "description": "Fact types to surface on recall — applies to both auto-recall and the hindsight_recall tool (comma-separated or list). Defaults to observation-only — observations are Hindsight's consolidated, deduplicated, evidence-grounded knowledge layer; raw world/experience facts are the supporting evidence observations already summarize. Set to e.g. 'observation,world,experience' to also include raw facts.", "default": "observation"},
            {"key": "auto_recall", "description": "Automatically recall memories before each turn", "default": True},
            {"key": "auto_retain", "description": "Automatically retain conversation turns", "default": True},
            {"key": "retain_on_new", "description": "Retain the current persisted session before explicit /new or /reset; abort reset on failure", "default": False},
            {"key": "retain_on_new_timeout_seconds", "description": "Maximum seconds /new or /reset waits for pending memory work and the Hindsight retain request", "default": 30},
            {"key": "retain_every_n_turns", "description": "Retain every N turns (1 = every turn)", "default": 1},
            {"key": "retain_async","description": "Process retain asynchronously on the Hindsight server", "default": True},

            {"key": "retain_context", "description": "Context label for retained memories", "default": "conversation between Hermes Agent and the User"},
            {"key": "recall_max_tokens", "description": "Maximum tokens for recall results", "default": 4096},
            {"key": "recall_max_input_chars", "description": "Maximum input query length for auto-recall", "default": 800},
            {"key": "recall_sync_on_cache_miss", "description": "Synchronously recall on first/cache-miss prefetch", "default": True},
            {"key": "recall_sync_timeout_seconds", "description": "Short timeout for synchronous cache-miss recall", "default": 5},
            {"key": "recall_prompt_preamble", "description": "Custom preamble for recalled memories in context"},
            {"key": "timeout", "description": "API request timeout in seconds", "default": _DEFAULT_TIMEOUT},
            {"key": "idle_timeout", "description": "Embedded daemon idle timeout in seconds (0 disables auto-shutdown)", "default": _DEFAULT_IDLE_TIMEOUT, "when": {"mode": "local_embedded"}},
            {"key": "port_health_grace_timeout", "description": "Seconds to wait for a slow daemon /health before treating it as stale (raise on busy/low-resource hosts; blank uses the 30s default)", "default": "", "when": {"mode": "local_embedded"}},
        ]

    def _get_client(self):
        """Return the cached Hindsight client (created once, reused)."""
        if self._client is None:
            if self._mode == "local_embedded":
                available, reason = _check_local_runtime()
                if not available:
                    raise RuntimeError(
                        "Hindsight local runtime is unavailable"
                        + (f": {reason}" if reason else "")
                    )
                try:
                    from tools.lazy_deps import ensure as _lazy_ensure
                    _lazy_ensure("memory.hindsight", prompt=False)
                except ImportError:
                    pass
                except Exception as _e:
                    raise ImportError(str(_e))
                from hindsight import HindsightEmbedded
                HindsightEmbedded.__del__ = lambda self: None
                llm_provider = self._config.get("llm_provider", "")
                if llm_provider in {"openai_compatible", "openrouter"}:
                    llm_provider = "openai"
                logger.debug("Creating HindsightEmbedded client (profile=%s, provider=%s)",
                             self._config.get("profile", "hermes"), llm_provider)
                kwargs = dict(
                    profile=self._config.get("profile", "hermes"),
                    llm_provider=llm_provider,
                    llm_api_key=self._config.get("llmApiKey") or self._config.get("llm_api_key") or get_secret("HINDSIGHT_LLM_API_KEY", ""),
                    llm_model=self._config.get("llm_model", ""),
                )
                if self._llm_base_url:
                    kwargs["llm_base_url"] = self._llm_base_url
                idle_timeout = _parse_int_setting(
                    self._config.get("idle_timeout")
                    if self._config.get("idle_timeout") is not None
                    else os.environ.get("HINDSIGHT_IDLE_TIMEOUT", self._idle_timeout),
                    _DEFAULT_IDLE_TIMEOUT,
                )
                self._idle_timeout = idle_timeout
                kwargs["idle_timeout"] = idle_timeout
                self._client = HindsightEmbedded(**kwargs)
            else:
                _ensure_cloud_client_dependency()
                from hindsight_client import Hindsight
                timeout = self._timeout or _DEFAULT_TIMEOUT
                kwargs = {"base_url": self._api_url, "timeout": float(timeout)}
                if self._api_key:
                    kwargs["api_key"] = self._api_key
                logger.debug("Creating Hindsight cloud client (url=%s, has_key=%s, timeout=%s)",
                             self._api_url, bool(self._api_key), kwargs["timeout"])
                self._client = Hindsight(**kwargs)
        return self._client

    def _run_sync(self, coro, timeout: float | None = None):
        """Schedule *coro* on the shared loop using the configured timeout."""
        return _run_sync(coro, timeout=timeout if timeout is not None else self._timeout)

    def _is_retriable_embedded_connection_error(self, exc: Exception) -> bool:
        """Return True for stale embedded-daemon connection failures."""
        if self._mode != "local_embedded":
            return False
        text = f"{type(exc).__name__}: {exc}".lower()
        return any(
            marker in text
            for marker in (
                "cannot connect to host",
                "connection refused",
                "connect call failed",
                "clientconnectorerror",
            )
        )

    def _ensure_writer(self) -> None:
        """Lazy-start the single retain-writer thread.

        We don't start the writer in initialize() so providers that never
        retain (e.g. tools-only mode) don't pay for an idle thread.
        """
        thread = self._writer_thread
        if thread is not None and thread.is_alive():
            return
        # If the previous writer exited (e.g. after a prior shutdown), reset
        # the flag so this fresh writer is allowed to drain new jobs.
        self._shutting_down.clear()
        thread = threading.Thread(
            target=self._writer_loop,
            daemon=True,
            name="hindsight-writer",
        )
        self._writer_thread = thread
        # Keep the legacy _sync_thread alias pointing at the writer so any
        # external code that joins _sync_thread keeps working.
        self._sync_thread = thread
        thread.start()


    def _writer_loop(self) -> None:
        """Drain the retain queue serially. Exits on sentinel.

        Each job() is wrapped so a single failure can't kill the writer.
        task_done() always fires so queue.join() works in tests.
        """
        while True:
            try:
                job = self._retain_queue.get(timeout=1.0)
            except queue.Empty:
                if self._shutting_down.is_set():
                    return
                continue
            try:
                if job is _WRITER_SENTINEL:
                    return
                try:
                    job()
                except Exception as exc:
                    logger.warning("Hindsight retain failed: %s", exc, exc_info=True)
            finally:
                self._retain_queue.task_done()

    def _register_atexit(self) -> None:
        """Register an idempotent atexit hook to drain the writer.

        Without this, a CLI exit that doesn't go through MemoryManager.
        shutdown_all() would leave in-flight retain jobs racing interpreter
        teardown, producing "cannot schedule new futures" warnings and
        unclosed aiohttp sessions.
        """
        if self._atexit_registered:
            return
        self._atexit_registered = True
        atexit.register(self._atexit_shutdown)

    def _atexit_shutdown(self) -> None:
        if self._shutting_down.is_set():
            return
        try:
            self.shutdown()
        except Exception as exc:
            logger.debug("Hindsight atexit shutdown failed: %s", exc)

    def _run_hindsight_operation(self, operation, *, timeout: float | None = None):
        """Run an async Hindsight client operation, retrying once after idle shutdown."""
        client = self._get_client()
        try:
            return self._run_sync(operation(client), timeout=timeout)
        except Exception as exc:
            if not self._is_retriable_embedded_connection_error(exc):
                raise
            logger.info(
                "Hindsight embedded daemon appears unreachable; recreating client and retrying once: %s",
                exc,
            )
            self._client = None
            client = self._get_client()
            self._client = client
            return self._run_sync(operation(client), timeout=timeout)

    def _probe_url(self) -> str:
        """Return the URL to probe /version on.

        For local_embedded the daemon is on a per-profile dynamic port,
        so we prefer the running client's URL when available; otherwise
        fall back to the configured api_url.
        """
        if self._mode == "local_embedded" and self._client is not None:
            url = getattr(self._client, "url", None)
            if url:
                return str(url)
        return self._api_url or ""

    def _resolve_retain_target_for_session(self, session_id: str, fallback_document_id: str) -> tuple[str, str | None]:
        """Pick (document_id, update_mode) for incremental append retains."""
        if not session_id:
            return fallback_document_id, None
        if _check_api_supports_update_mode_append(self._probe_url(), self._api_key):
            return session_id, "append"
        return fallback_document_id, None

    def _resolve_full_retain_target_for_session(
        self,
        session_id: str,
        fallback_document_id: str,
        *,
        probe_timeout: float | None = None,
    ) -> tuple[str, str | None]:
        """Pick (document_id, update_mode) for full manual session retains.

        Manual `/retain` submits the complete reconstructed session document, so
        retries must replace the logical document instead of appending another
        copy. Legacy APIs without explicit update modes already replace on
        stable document-id upsert, so keep the stable session id there too.
        """
        if not session_id:
            return fallback_document_id, None
        if _check_api_supports_update_mode_append(
            self._probe_url(),
            self._api_key,
            timeout=probe_timeout,
        ):
            return session_id, "replace"
        return session_id, None

    def _resolve_retain_target(self, fallback_document_id: str) -> tuple[str, str | None]:
        """Pick (document_id, update_mode) based on live API capability.

        On Hindsight ≥ 0.5.0 the API supports ``update_mode='append'``,
        which lets us reuse a stable session-scoped ``document_id`` across
        process lifecycles without overwriting prior turns. On older APIs
        we fall back to *fallback_document_id* (the per-process unique
        ``f"{session_id}-{start_ts}"`` minted at initialize / switch time)
        and don't pass ``update_mode`` at all — that's the only way the
        resume-overwrite fix (#6654) keeps working on legacy servers.

        Probe is cached at module level per API URL, so this is one HTTP
        round-trip per (process, api_url) pair regardless of how many
        retains fire.
        """
        return self._resolve_retain_target_for_session(self._session_id, fallback_document_id)

    def initialize(self, session_id: str, **kwargs) -> None:
        self._session_id = str(session_id or "").strip()
        self._parent_session_id = str(kwargs.get("parent_session_id", "") or "").strip()
        self._retain_document_id = self._parent_session_id or self._session_id

        # Each process lifecycle gets its own document_id. Reusing session_id
        # alone caused overwrites on /resume — the reloaded session starts
        # with an empty _session_turns, so the next retain would replace the
        # previously stored content. session_id stays in tags so processes
        # for the same session remain filterable together.
        start_ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        self._document_id = f"{self._session_id}-{start_ts}"

        # Check client version and auto-upgrade if needed
        try:
            from importlib.metadata import version as pkg_version
            from packaging.version import Version
            installed = pkg_version("hindsight-client")
            if Version(installed) < Version(_MIN_CLIENT_VERSION):
                logger.warning("hindsight-client %s is outdated (need >=%s), attempting upgrade...",
                               installed, _MIN_CLIENT_VERSION)
                # Environment-aware install: sealed hosted venvs redirect to the
                # durable data-volume target instead of /opt/hermes (NS-605).
                from tools.lazy_deps import install_specs
                outcome = install_specs([f"hindsight-client>={_MIN_CLIENT_VERSION}"], timeout=120)
                if outcome.ok:
                    logger.info("hindsight-client upgraded to >=%s", _MIN_CLIENT_VERSION)
                elif outcome.blocked:
                    logger.warning("Auto-upgrade unavailable: %s. Run: uv pip install 'hindsight-client>=%s'",
                                   outcome.reason, _MIN_CLIENT_VERSION)
                else:
                    logger.warning("Auto-upgrade failed: %s. Run: uv pip install 'hindsight-client>=%s'",
                                   (outcome.stderr or "").strip() or "install error", _MIN_CLIENT_VERSION)
        except Exception:
            pass  # packaging not available or other issue — proceed anyway

        self._config = _load_config()
        self._platform = str(kwargs.get("platform") or "").strip()
        self._user_id = str(kwargs.get("user_id") or "").strip()
        self._user_name = str(kwargs.get("user_name") or "").strip()
        self._chat_id = str(kwargs.get("chat_id") or "").strip()
        self._chat_name = str(kwargs.get("chat_name") or "").strip()
        self._chat_type = str(kwargs.get("chat_type") or "").strip()
        self._thread_id = str(kwargs.get("thread_id") or "").strip()
        self._agent_identity = str(kwargs.get("agent_identity") or "").strip()
        self._agent_workspace = str(kwargs.get("agent_workspace") or "").strip()
        self._turn_index = 0
        self._session_turns = []
        with self._retain_flush_lock:
            self._retain_generation += 1
            self._last_flushed_turn_count = 0
            self._last_queued_flush_count = 0
            self._retain_flush_pending = False
            self._retain_force_replace = False
        self._mode = self._config.get("mode", "cloud")
        # Read timeout from config or env var, fall back to default
        self._timeout = _parse_int_setting(
            self._config.get("timeout") if self._config.get("timeout") is not None else os.environ.get("HINDSIGHT_TIMEOUT"),
            _DEFAULT_TIMEOUT,
        )
        self._idle_timeout = _parse_int_setting(
            self._config.get("idle_timeout") if self._config.get("idle_timeout") is not None else os.environ.get("HINDSIGHT_IDLE_TIMEOUT"),
            _DEFAULT_IDLE_TIMEOUT,
        )
        # "local" is a legacy alias for "local_embedded"
        if self._mode == "local":
            self._mode = "local_embedded"
        if self._mode == "local_embedded":
            # Export the daemon health grace timeout BEFORE importing
            # daemon_embed_manager (which reads it at import time).
            _export_port_health_grace_timeout(self._config)
            available, reason = _check_local_runtime()
            if not available:
                logger.warning(
                    "Hindsight local mode disabled because its runtime could not be imported: %s",
                    reason,
                )
                self._mode = "disabled"
                return
        self._api_key = self._config.get("apiKey") or self._config.get("api_key") or get_secret("HINDSIGHT_API_KEY", "")
        default_url = _DEFAULT_LOCAL_URL if self._mode in {"local_embedded", "local_external"} else _DEFAULT_API_URL
        self._api_url = self._config.get("api_url") or os.environ.get("HINDSIGHT_API_URL", default_url)
        self._llm_base_url = self._config.get("llm_base_url", "")

        banks = cfg_get(self._config, "banks", "hermes", default={})
        static_bank_id = self._config.get("bank_id") or banks.get("bankId", "hermes")
        self._bank_id_template = self._config.get("bank_id_template", "") or ""
        self._bank_id = _resolve_bank_id_template(
            self._bank_id_template,
            fallback=static_bank_id,
            profile=self._agent_identity,
            workspace=self._agent_workspace,
            platform=self._platform,
            user=self._user_id,
            session=self._session_id,
        )
        budget = self._config.get("recall_budget") or self._config.get("budget") or banks.get("budget", "mid")
        self._budget = budget if budget in _VALID_BUDGETS else "mid"

        memory_mode = self._config.get("memory_mode", "hybrid")
        self._memory_mode = memory_mode if memory_mode in {"context", "tools", "hybrid"} else "hybrid"

        prefetch_method = self._config.get("recall_prefetch_method") or self._config.get("prefetch_method", "recall")
        self._prefetch_method = prefetch_method if prefetch_method in {"recall", "reflect"} else "recall"

        # Bank options
        self._bank_mission = self._config.get("bank_mission", "")
        self._bank_retain_mission = self._config.get("bank_retain_mission") or None

        # Tags
        self._retain_tags = _normalize_retain_tags(
            self._config.get("retain_tags")
            or os.environ.get("HINDSIGHT_RETAIN_TAGS", "")
        )
        self._tags = self._retain_tags or None
        self._observation_scopes = _normalize_observation_scopes(
            self._config.get("observation_scopes")
            or os.environ.get("HINDSIGHT_RETAIN_OBSERVATION_SCOPES", "")
        )
        self._recall_tags = self._config.get("recall_tags") or None
        self._recall_tags_match = self._config.get("recall_tags_match", "any")
        self._retain_source = str(
            self._config.get("retain_source") or os.environ.get("HINDSIGHT_RETAIN_SOURCE", "")
        ).strip()
        self._retain_user_prefix = str(
            self._config.get("retain_user_prefix") or os.environ.get("HINDSIGHT_RETAIN_USER_PREFIX", "User")
        ).strip() or "User"
        self._retain_assistant_prefix = str(
            self._config.get("retain_assistant_prefix") or os.environ.get("HINDSIGHT_RETAIN_ASSISTANT_PREFIX", "Assistant")
        ).strip() or "Assistant"

        # Retain controls
        self._auto_retain = self._config.get("auto_retain", True)
        self._retain_every_n_turns = max(1, int(self._config.get("retain_every_n_turns", 1)))
        self._retain_on_new = _parse_bool_setting(
            self._config.get("retain_on_new"),
            False,
        )
        self._retain_on_new_timeout_seconds = max(
            0.1,
            _parse_float_setting(
                self._config.get("retain_on_new_timeout_seconds"),
                30.0,
            ),
        )
        self._retain_context = self._config.get("retain_context", "conversation between Hermes Agent and the User")

        # Recall controls
        self._auto_recall = self._config.get("auto_recall", True)
        self._recall_max_tokens = int(self._config.get("recall_max_tokens", 4096))
        # Default narrows recall to observation-only; pass an explicit
        # `recall_types` list in config.json to broaden (e.g. include
        # "world" / "experience") or to disable the filter entirely.
        configured_types = self._config.get("recall_types")
        if configured_types is None:
            self._recall_types = ["observation"]
        elif isinstance(configured_types, str):
            # Allow comma-separated strings for parity with recall_tags.
            self._recall_types = [t.strip() for t in configured_types.split(",") if t.strip()]
        else:
            self._recall_types = list(configured_types) or ["observation"]
        self._recall_prompt_preamble = self._config.get("recall_prompt_preamble", "")
        self._recall_max_input_chars = int(self._config.get("recall_max_input_chars", 800))
        self._recall_sync_on_cache_miss = _parse_bool_setting(
            self._config.get("recall_sync_on_cache_miss"),
            True,
        )
        self._recall_sync_timeout_seconds = _parse_float_setting(
            self._config.get("recall_sync_timeout_seconds"),
            5.0,
        )
        self._retain_async = self._config.get("retain_async", True)


        _client_version = "unknown"
        try:
            from importlib.metadata import version as pkg_version
            _client_version = pkg_version("hindsight-client")
        except Exception:
            pass
        logger.info("Hindsight initialized: mode=%s, api_url=%s, bank=%s, budget=%s, memory_mode=%s, prefetch_method=%s, client=%s",
                     self._mode, self._api_url, self._bank_id, self._budget, self._memory_mode, self._prefetch_method, _client_version)
        if self._bank_id_template:
            logger.debug("Hindsight bank resolved from template %r: profile=%s workspace=%s platform=%s user=%s -> bank=%s",
                         self._bank_id_template, self._agent_identity, self._agent_workspace,
                         self._platform, self._user_id, self._bank_id)
        logger.debug("Hindsight config: auto_retain=%s, auto_recall=%s, retain_every_n=%d, "
                     "retain_async=%s, retain_context=%s, recall_max_tokens=%d, recall_max_input_chars=%d, tags=%s, recall_tags=%s",
                     self._auto_retain, self._auto_recall, self._retain_every_n_turns,
                     self._retain_async, self._retain_context, self._recall_max_tokens, self._recall_max_input_chars,
                     self._tags, self._recall_tags)

        # For local mode, start the embedded daemon in the background so it
        # doesn't block the chat. Redirect stdout/stderr to a log file to
        # prevent rich startup output from spamming the terminal.
        if self._mode == "local_embedded":
            # PostgreSQL's initdb refuses to run as root by design, so the
            # embedded daemon can never initialize its data directory under
            # root. Without this guard the daemon-start thread would fail,
            # retry, and loop forever — each cycle reloading embedding models
            # (~958MB RAM, ~33% CPU) with no user-visible error. Detect root
            # up front and skip daemon startup with a clear message instead.
            if hasattr(os, "geteuid") and os.geteuid() == 0:
                msg = (
                    "Hindsight local_embedded mode cannot run as root "
                    "(PostgreSQL initdb refuses root). Skipping the embedded "
                    "memory daemon. Run Hermes as a non-root user, or switch "
                    "to cloud / local_external mode via 'hermes memory setup'."
                )
                logger.warning(msg)
                # Surface to the terminal too — a daemon that never starts
                # would otherwise fail silently and the user would only see
                # Hermes get sluggish. (issue #13125)
                try:
                    print(f"  ⚠ {msg}", file=sys.stderr, flush=True)
                except Exception:
                    pass
                self._mode = "disabled"
                return

            def _start_daemon():
                import traceback
                log_dir = get_hermes_home() / "logs"
                log_dir.mkdir(parents=True, exist_ok=True)
                log_path = log_dir / "hindsight-embed.log"
                try:
                    # Redirect the daemon manager's Rich console to our log file
                    # instead of stderr. This avoids global fd redirects that
                    # would capture output from other threads.
                    import hindsight_embed.daemon_embed_manager as dem
                    from rich.console import Console
                    dem.console = Console(file=open(log_path, "a", encoding="utf-8"), force_terminal=False)

                    client = self._get_client()
                    profile = self._config.get("profile", "hermes")

                    # Update the profile .env to match our current config so
                    # the daemon always starts with the right settings.
                    # If the config changed and the daemon is running, stop it.
                    profile_env = _embedded_profile_env_path(self._config)
                    expected_env = _build_embedded_profile_env(self._config)
                    saved = _load_simple_env(profile_env)
                    config_changed = saved != expected_env

                    if config_changed:
                        profile_env = _materialize_embedded_profile_env(self._config)
                        if client._manager.is_running(profile):
                            with open(log_path, "a", encoding="utf-8") as f:
                                f.write("\n=== Config changed, restarting daemon ===\n")
                            client._manager.stop(profile)

                    client._ensure_started()
                    with open(log_path, "a", encoding="utf-8") as f:
                        f.write("\n=== Daemon started successfully ===\n")
                except Exception as e:
                    with open(log_path, "a", encoding="utf-8") as f:
                        f.write(f"\n=== Daemon startup failed: {e} ===\n")
                        traceback.print_exc(file=f)

            t = threading.Thread(target=_start_daemon, daemon=True, name="hindsight-daemon-start")
            t.start()

    def system_prompt_block(self) -> str:
        if self._memory_mode == "context":
            return (
                f"# Hindsight Memory\n"
                f"Active (context mode). Bank: {self._bank_id}, budget: {self._budget}.\n"
                f"Relevant memories are automatically injected into context."
            )
        if self._memory_mode == "tools":
            return (
                f"# Hindsight Memory\n"
                f"Active (tools mode). Bank: {self._bank_id}, budget: {self._budget}.\n"
                f"Use hindsight_recall to search, hindsight_reflect for synthesis, "
                f"hindsight_retain to store facts."
            )
        return (
            f"# Hindsight Memory\n"
            f"Active. Bank: {self._bank_id}, budget: {self._budget}.\n"
            f"Relevant memories are automatically injected into context. "
            f"Use hindsight_recall to search, hindsight_reflect for synthesis, "
            f"hindsight_retain to store facts."
        )

    def _format_prefetch_context(self, result: str) -> str:
        header = self._recall_prompt_preamble or (
            "# Hindsight Memory (persistent cross-session context)\n"
            "Use this to answer questions about the user and prior sessions. "
            "Do not call tools to look up information that is already present here."
        )
        return f"{header}\n\n{result}"

    @staticmethod
    def _recall_snapshot_text(snapshot: _RecallSnapshot) -> str:
        return "\n".join(f"- {text}" for text in snapshot.results if text)

    def _recall_snapshot_for_query(
        self,
        query: str,
        *,
        timeout: float | None = None,
    ) -> _RecallSnapshot:
        query = str(query or "").strip()
        if not query:
            return _RecallSnapshot(query="", results=())
        if self._recall_max_input_chars and len(query) > self._recall_max_input_chars:
            query = query[:self._recall_max_input_chars]
        if self._prefetch_method == "reflect":
            logger.debug("Prefetch: calling reflect (bank=%s, query_len=%d)", self._bank_id, len(query))
            resp = self._run_hindsight_operation(
                lambda client: client.areflect(bank_id=self._bank_id, query=query, budget=self._budget),
                timeout=timeout,
            )
            text = str(resp.text or "").strip()
            return _RecallSnapshot(query=query, results=(text,) if text else ())

        recall_kwargs: dict = {
            "bank_id": self._bank_id,
            "query": query,
            "budget": self._budget,
            "max_tokens": self._recall_max_tokens,
        }
        if self._recall_tags:
            recall_kwargs["tags"] = self._recall_tags
            recall_kwargs["tags_match"] = self._recall_tags_match
        if self._recall_types:
            recall_kwargs["types"] = self._recall_types
        logger.debug(
            "Prefetch: calling recall (bank=%s, query_len=%d, budget=%s)",
            self._bank_id, len(query), self._budget,
        )
        resp = self._run_hindsight_operation(
            lambda client: client.arecall(**recall_kwargs),
            timeout=timeout,
        )
        num_results = len(resp.results) if resp.results else 0
        logger.debug("Prefetch: recall returned %d results", num_results)
        results = tuple(
            str(result.text)
            for result in (resp.results or [])
            if getattr(result, "text", None)
        )
        return _RecallSnapshot(query=query, results=results)

    def _recall_for_query(self, query: str, *, timeout: float | None = None) -> str:
        """Compatibility wrapper returning the historical formatted text shape."""
        return self._recall_snapshot_text(
            self._recall_snapshot_for_query(query, timeout=timeout)
        )

    def _carry_recall_snapshot_to_next_turn(
        self,
        snapshot: _RecallSnapshot,
        *,
        expected_generation: int,
        session_id: str = "",
    ) -> None:
        """Keep the recall actually used this turn for the next P5 decision."""
        carried_snapshot = _RecallSnapshot(
            query=str(snapshot.query or ""),
            results=tuple(str(text) for text in snapshot.results),
        )
        carried_text = self._recall_snapshot_text(carried_snapshot)
        expected_session_id = str(session_id or "").strip()
        with self._prefetch_lock:
            if expected_generation != self._prefetch_generation:
                logger.debug(
                    "Prefetch: discarded carried snapshot from stale generation %s",
                    expected_generation,
                )
                return
            current_session_id = str(self._session_id or "").strip()
            if (
                expected_session_id
                and current_session_id
                and expected_session_id != current_session_id
            ):
                logger.debug(
                    "Prefetch: discarded carried snapshot for stale session %s "
                    "(current=%s)",
                    expected_session_id,
                    current_session_id,
                )
                return
            self._prefetch_snapshot = carried_snapshot
            self._prefetch_result = carried_text

    def prefetch(
        self,
        query: str,
        *,
        session_id: str = "",
        turn_id: str = "",
        previous_assistant_message: str = "",
    ) -> str:
        if self._memory_mode == "tools" or not self._auto_recall:
            logger.debug("Prefetch: skipped (automatic recall inactive)")
            return ""
        if self._shutting_down.is_set():
            logger.debug("Prefetch: skipped (shutting down)")
            return ""

        requested_session_id = str(session_id or "").strip()
        requested_turn_id = str(turn_id or "").strip()
        with self._prefetch_lock:
            current_session_id = str(self._session_id or "").strip()
            if (
                requested_session_id
                and current_session_id
                and requested_session_id != current_session_id
            ):
                logger.debug(
                    "Prefetch: skipped stale session %s (current=%s)",
                    requested_session_id,
                    current_session_id,
                )
                return ""
            self._prefetch_generation += 1
            snapshot_generation = self._prefetch_generation
            self._active_prefetch_turn = (requested_turn_id, snapshot_generation)
            result = self._prefetch_result
            snapshot = self._prefetch_snapshot
            self._prefetch_result = ""
            self._prefetch_snapshot = None

        preprocessor_snapshot = snapshot
        if (
            preprocessor_snapshot is None
            and not result
            and str(previous_assistant_message or "").strip()
        ):
            preprocessor_snapshot = _RecallSnapshot(query="", results=())

        if preprocessor_snapshot is not None:
            original_results = tuple(preprocessor_snapshot.results)
            selected_query = preprocessor_snapshot.query
            fall_back_to_current_query = False
            try:
                decision = run_recall_preprocessor(
                    current_user_message=str(query or ""),
                    previous_assistant_message=str(previous_assistant_message or ""),
                    previous_recall_query=preprocessor_snapshot.query,
                    previous_recall_results=original_results,
                )
            except Exception as exc:
                logger.warning(
                    "Hindsight recall preprocessor failed; using full cached recall: %s",
                    exc,
                )
                if original_results:
                    selected_results = original_results
                else:
                    selected_results = ()
                    fall_back_to_current_query = True
            else:
                dropped = set(decision.drop_old_refs)
                selected_results = tuple(
                    text
                    for ref, text in enumerate(original_results, 1)
                    if ref not in dropped
                )
                if decision.new_query is None:
                    if original_results and not selected_results:
                        selected_query = ""
                    logger.debug(
                        "Prefetch: preprocessor skipped new recall; reusing %d "
                        "selected old results",
                        len(selected_results),
                    )
                else:
                    try:
                        new_snapshot = self._recall_snapshot_for_query(
                            decision.new_query,
                            timeout=self._recall_sync_timeout_seconds,
                        )
                    except Exception as exc:
                        logger.warning(
                            "Hindsight recall for preprocessor query failed; "
                            "restoring full cached recall: %s",
                            exc,
                        )
                        if original_results:
                            selected_results = original_results
                        else:
                            selected_results = ()
                            fall_back_to_current_query = True
                    else:
                        selected_query = new_snapshot.query
                        selected_results += tuple(new_snapshot.results)

            if not fall_back_to_current_query:
                selected_snapshot = _RecallSnapshot(
                    query=selected_query,
                    results=selected_results,
                )
                result = self._recall_snapshot_text(selected_snapshot)
                self._carry_recall_snapshot_to_next_turn(
                    selected_snapshot,
                    expected_generation=snapshot_generation,
                    session_id=session_id,
                )
                if not result:
                    logger.debug("Prefetch: preprocessor selected no recall context")
                    return ""

        if not result:
            if (
                self._memory_mode == "tools"
                or not self._auto_recall
                or self._shutting_down.is_set()
                or not self._recall_sync_on_cache_miss
                or not str(query or "").strip()
            ):
                logger.debug("Prefetch: no results available")
                return ""
            try:
                sync_snapshot = self._recall_snapshot_for_query(
                    query,
                    timeout=self._recall_sync_timeout_seconds,
                )
                result = self._recall_snapshot_text(sync_snapshot)
                self._carry_recall_snapshot_to_next_turn(
                    sync_snapshot,
                    expected_generation=snapshot_generation,
                    session_id=session_id,
                )
            except Exception as e:
                logger.debug("Hindsight sync prefetch failed: %s", e, exc_info=True)
                return ""
            if not result:
                logger.debug("Prefetch: sync fallback returned no results")
                return ""
        logger.debug("Prefetch: returning %d chars of context", len(result))
        return self._format_prefetch_context(result)

    def on_prefetch_timeout(
        self,
        *,
        session_id: str = "",
        turn_id: str = "",
    ) -> None:
        """Invalidate only the abandoned Hindsight prefetch generation."""
        timed_out_session_id = str(session_id or "").strip()
        timed_out_turn_id = str(turn_id or "").strip()
        with self._prefetch_lock:
            current_session_id = str(self._session_id or "").strip()
            if (
                timed_out_session_id
                and current_session_id
                and timed_out_session_id != current_session_id
            ):
                return
            active_turn = self._active_prefetch_turn
            if active_turn is None:
                return
            active_turn_id, active_generation = active_turn
            if timed_out_turn_id and timed_out_turn_id != active_turn_id:
                return
            if active_generation != self._prefetch_generation:
                return
            self._prefetch_generation += 1
            self._prefetch_result = ""
            self._prefetch_snapshot = None
            self._active_prefetch_turn = None

    def queue_prefetch(
        self,
        query: str,
        *,
        session_id: str = "",
        turn_id: str = "",
    ) -> None:
        """Do not recall the completed turn's raw user text.

        ``prefetch()`` carries the exact query/results used for the current turn
        into the next P5 decision, so the generic post-turn hook is deliberately
        a no-op for Hindsight.
        """
        logger.debug("Prefetch: skipped post-turn raw-query recall")

    @staticmethod
    def _retain_message_timestamp(value: Any = None, *, fallback_now: bool = True) -> str:
        def _local_seconds(dt: datetime) -> str:
            if dt.tzinfo is None:
                # Retained timestamps are serialized as naive local wall-clock
                # strings. Treating one of those strings as UTC during replay
                # shifts it by the host offset and can split a real user/assistant
                # turn at the replay cutoff. Re-attach the local zone so this
                # normalization stays idempotent.
                dt = dt.astimezone()
            return dt.astimezone().replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%S")

        if value is None:
            return (
                datetime.now().astimezone().replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%S")
                if fallback_now
                else ""
            )
        if isinstance(value, datetime):
            return _local_seconds(value)
        if isinstance(value, bool):
            return (
                datetime.now().astimezone().replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%S")
                if fallback_now
                else ""
            )
        if isinstance(value, (int, float)):
            seconds = float(value)
            if abs(seconds) > 10_000_000_000:
                seconds = seconds / 1000.0
            return _local_seconds(datetime.fromtimestamp(seconds, timezone.utc))
        text = str(value).strip()
        if not text:
            return (
                datetime.now().astimezone().replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%S")
                if fallback_now
                else ""
            )
        try:
            return _local_seconds(datetime.fromisoformat(text.replace("Z", "+00:00")))
        except ValueError:
            return text

    def _build_turn_messages(
        self,
        user_content: str,
        assistant_content: str,
        *,
        user_timestamp: Any = None,
        assistant_timestamp: Any = None,
        user_occurrence_id: str = "",
        fallback_timestamp_now: bool = True,
    ) -> List[Dict[str, str]]:
        user_message = {
                "role": "user",
                "content": f"{self._retain_user_prefix}: {user_content}",
                "timestamp": self._retain_message_timestamp(
                    user_timestamp,
                    fallback_now=fallback_timestamp_now,
                ),
            }
        if user_occurrence_id:
            user_message["_hermes_source_occurrence_id"] = user_occurrence_id
        return [
            user_message,
            {
                "role": "assistant",
                "content": f"{self._retain_assistant_prefix}: {assistant_content}",
                "timestamp": self._retain_message_timestamp(
                    assistant_timestamp,
                    fallback_now=fallback_timestamp_now,
                ),
            },
        ]

    @staticmethod
    def _stringify_retain_content(content: Any) -> str:
        if content is None:
            return ""
        if isinstance(content, str):
            return content
        return json.dumps(content, ensure_ascii=False)

    # Synthetic Hermes/LCM/runtime events that must never enter retained
    # conversation documents. Keep this marker-based, not business-topic-based.
    # LCM depth labels: Recent (d0), Session Arc (d1), Durable (d2), Depth-N (d>=3).
    _RETAIN_OBJECTIVE_HEADER = "[Current user objective preserved from compacted history]"
    _RETAIN_MODEL_SWITCH_NOTE_PREFIX = "[Note: model was just switched from "
    _RETAIN_MODEL_SWITCH_NOTE_SUFFIX = "Adjust your self-identification accordingly.]"
    _RETAIN_TOOL_BUDGET_EXHAUSTED_NOTICE = (
        "You've reached the maximum number of tool-calling iterations allowed. "
        "Please provide a final response summarizing what you've found and accomplished so far, "
        "without calling any more tools."
    )
    _RETAIN_EMPTY_TOOL_RESPONSE_NUDGE = (
        "You just executed tool calls but returned an empty response. "
        "Please process the tool results above and continue with the task."
    )
    _RETAIN_LCM_SUMMARY_HEADER_RE = re.compile(
        r"(?:\[(?:Recent|Session Arc|Durable) Summary \(d\d+(?:,\s*node\s+\d+)?\)\]"
        r"|\[Depth-\d+ Summary \(d\d+(?:,\s*node\s+\d+)?\)\])"
    )
    _RETAIN_RECENT_SUMMARY_HEADER_RE = re.compile(
        r"\[Recent Summary \(d0(?:,\s*node\s+\d+)?\)\]"
    )
    _RETAIN_ASYNC_COMPLETION_MARKERS = (
        "[ASYNC DELEGATION BATCH COMPLETE",
        "[ASYNC DELEGATION COMPLETE",
    )
    _RETAIN_SYSTEM_CONTINUATION_PREFIXES = (
        "[System: Your previous response was truncated",
        "[System: The previous response was cut off",
        "[System: Your previous tool call",
    )
    _RETAIN_BACKGROUND_PROCESS_PREFIX = "[IMPORTANT: Background process "
    _RETAIN_WATCH_DISABLED_PREFIX = "[IMPORTANT: Watch patterns disabled for process "
    _RETAIN_HANDOFF_PREFIX = "[Session was just handed off from CLI ("
    _RETAIN_OOB_USER_OPEN = (
        "[OUT-OF-BAND USER MESSAGE — a direct message from the user, delivered mid-turn; "
        "not tool output]"
    )
    _RETAIN_OOB_USER_CLOSE = "[/OUT-OF-BAND USER MESSAGE]"
    _RETAIN_OOB_TRUSTED_FIELD = "_hermes_oob_user_messages"
    _RETAIN_NOISE_MARKERS = (
        _RETAIN_OBJECTIVE_HEADER,
        "[Your active task list was preserved across context compression]",
        "[Externalized payload:",
        *_RETAIN_ASYNC_COMPLETION_MARKERS,
        "[OUT-OF-BAND USER MESSAGE",
    )

    @classmethod
    def _is_lcm_summary_header_line(cls, content: str) -> bool:
        return bool(cls._RETAIN_LCM_SUMMARY_HEADER_RE.fullmatch((content or "").strip()))

    @classmethod
    def _starts_with_lcm_summary_block(cls, content: str) -> bool:
        first_line = (content or "").lstrip().split("\n", 1)[0]
        return cls._is_lcm_summary_header_line(first_line)

    @classmethod
    def _starts_with_recent_summary_block(cls, content: str) -> bool:
        first_line = (content or "").lstrip().split("\n", 1)[0].strip()
        return bool(cls._RETAIN_RECENT_SUMMARY_HEADER_RE.fullmatch(first_line))

    @classmethod
    def _is_tool_budget_exhausted_notice(cls, content: Any) -> bool:
        text = cls._stringify_retain_content(content).replace("\r\n", "\n").strip()
        return text == cls._RETAIN_TOOL_BUDGET_EXHAUSTED_NOTICE

    @classmethod
    def _is_empty_tool_response_nudge(cls, content: Any) -> bool:
        text = cls._stringify_retain_content(content).replace("\r\n", "\n").strip()
        return text == cls._RETAIN_EMPTY_TOOL_RESPONSE_NUDGE

    @classmethod
    def _strip_leading_retain_runtime_injections(cls, content: str) -> str:
        """Strip recognized leading runtime injections without scanning user prose."""
        text = (content or "").replace("\r\n", "\n").strip()
        saw_runtime_marker = False
        while True:
            without_model_switch = cls._strip_model_switch_note(text)
            if without_model_switch != text:
                text = without_model_switch
                saw_runtime_marker = True
                continue

            prefix = f"{cls._RETAIN_TOOL_BUDGET_EXHAUSTED_NOTICE}\n"
            if not text.startswith(prefix):
                break
            remainder = text[len(prefix):].lstrip("\n")
            has_runtime_marker = saw_runtime_marker or any(
                cls._is_lcm_summary_header_line(line.strip())
                or any(line.strip().startswith(marker) for marker in cls._RETAIN_NOISE_MARKERS)
                or cls._strip_model_switch_note(line.strip()) != line.strip()
                for line in remainder.split("\n")
            )
            if not has_runtime_marker:
                break
            text = remainder
        return text

    @classmethod
    def _is_skill_invocation_runtime_event(cls, content: Any) -> bool:
        """Match a complete Hermes skill-invocation header, not quoted prefixes."""
        text = cls._stringify_retain_content(content).replace("\r\n", "\n").lstrip()
        first_line = text.split("\n", 1)[0].strip()
        single_skill = re.fullmatch(
            r'\[IMPORTANT: The user has invoked the "[^"\n]+" skill, '
            r'indicating they want you to follow its instructions\. '
            r'The full skill content is loaded below\.\]',
            first_line,
        )
        skill_bundle = re.fullmatch(
            r'\[IMPORTANT: The user has invoked the "[^"\n]+" (?:stacked )?skill bundle, '
            r'loading \d+ skills together\. Treat every skill below as active '
            r'guidance for this turn\.\]',
            first_line,
        )
        return bool(single_skill or skill_bundle)

    @classmethod
    def _extract_skill_invocation_user_instruction(cls, content: Any) -> str:
        if not cls._is_skill_invocation_runtime_event(content):
            return ""
        try:
            from agent.skill_commands import extract_user_instruction_from_skill_message

            instruction = extract_user_instruction_from_skill_message(
                cls._stringify_retain_content(content)
            )
        except Exception:
            return ""
        return cls._stringify_retain_content(instruction).strip() if instruction else ""

    @classmethod
    def _has_closed_runtime_envelope(cls, text: str) -> bool:
        normalized = (text or "").strip()
        saw_internal_boundary = False
        for boundary in re.finditer(r"\]\n\n", normalized):
            saw_internal_boundary = True
            remainder = normalized[boundary.end():].lstrip()
            while remainder.startswith("---"):
                remainder = remainder[3:].lstrip()
            if (
                cls._starts_with_lcm_summary_block(remainder)
                or any(remainder.startswith(marker) for marker in cls._RETAIN_NOISE_MARKERS)
            ):
                return True
        if saw_internal_boundary:
            # A closed runtime-shaped prefix followed by ordinary prose is a
            # user quotation/extension, even when that prose also ends in `]`.
            return False
        return normalized.endswith("]")

    @classmethod
    def _is_assistant_producing_runtime_event(cls, content: Any) -> bool:
        """Return whether an exact synthetic row can produce visible output.

        Runtime envelopes are recognized by reserved framework markers, not by
        command/output or business text. System continuation and handoff rows
        require their complete closing bracket; process and skill-injection rows
        may contain or be followed by larger synthetic payload blocks.
        """
        text = cls._stringify_retain_content(content).strip()
        if text.startswith(cls._RETAIN_OBJECTIVE_HEADER):
            text = text[len(cls._RETAIN_OBJECTIVE_HEADER):].lstrip("\n").strip()
        if not text:
            return False
        if cls._is_skill_invocation_runtime_event(text):
            return True
        if (
            text.startswith(cls._RETAIN_BACKGROUND_PROCESS_PREFIX)
            and "\nCommand: " in text
            and ("\nOutput:\n" in text or "\nMatched output:\n" in text)
            and cls._has_closed_runtime_envelope(text)
        ):
            return True
        if not text.endswith("]"):
            return False
        if text.startswith(cls._RETAIN_SYSTEM_CONTINUATION_PREFIXES):
            return True
        return text.startswith((cls._RETAIN_WATCH_DISABLED_PREFIX, cls._RETAIN_HANDOFF_PREFIX))

    @classmethod
    def _starts_with_retain_noise_marker(cls, content: str) -> bool:
        text = (content or "").lstrip()
        return (
            cls._starts_with_lcm_summary_block(text)
            or cls._is_assistant_producing_runtime_event(text)
            or any(text.startswith(marker) for marker in cls._RETAIN_NOISE_MARKERS)
        )

    @classmethod
    def _is_async_completion_user_content(cls, content: Any) -> bool:
        text = cls._stringify_retain_content(content).lstrip()
        return any(text.startswith(marker) for marker in cls._RETAIN_ASYNC_COMPLETION_MARKERS)

    @classmethod
    def _is_orphan_assistant_trigger_user_content(cls, content: Any) -> bool:
        """Return whether a synthetic user row may close with visible output.

        Async completion rows and recent-summary rehydration are internal
        runtime inputs, but the assistant response immediately following either
        row is user-visible conversation evidence. Other summary/noise-only rows
        remain dropped as a unit.
        """
        text = cls._stringify_retain_content(content).lstrip()
        return (
            cls._is_tool_budget_exhausted_notice(text)
            or cls._is_empty_tool_response_nudge(text)
            or cls._starts_with_recent_summary_block(text)
            or cls._is_assistant_producing_runtime_event(text)
            or any(text.startswith(marker) for marker in cls._RETAIN_ASYNC_COMPLETION_MARKERS)
        )

    @classmethod
    def _strip_model_switch_note(cls, text: str) -> str:
        normalized = (text or "").replace("\r\n", "\n").strip()
        if not normalized.startswith(cls._RETAIN_MODEL_SWITCH_NOTE_PREFIX):
            return normalized
        note_end = normalized.find(cls._RETAIN_MODEL_SWITCH_NOTE_SUFFIX)
        if note_end < 0:
            return normalized
        return normalized[note_end + len(cls._RETAIN_MODEL_SWITCH_NOTE_SUFFIX):].lstrip()

    @classmethod
    def _clean_multimodal_model_switch_note(cls, text: str) -> str | None:
        """Clean a model-switch note from serialized OpenAI content parts.

        Require both a text part and a non-text part so ordinary user text that
        happens to be a JSON array is not treated as runtime multimodal content.
        Return ``None`` when the value is not a matching multimodal payload or no
        model-switch note was removed.
        """
        if not text.startswith("["):
            return None
        try:
            parts = json.loads(text)
        except Exception:
            return None
        if not isinstance(parts, list):
            return None
        has_text = any(isinstance(part, dict) and part.get("type") == "text" for part in parts)
        has_non_text = any(isinstance(part, dict) and part.get("type") != "text" for part in parts)
        if not (has_text and has_non_text):
            return None

        cleaned_parts: List[Any] = []
        note_removed = False
        for part in parts:
            if not note_removed and isinstance(part, dict) and part.get("type") == "text":
                original_text = cls._stringify_retain_content(part.get("text"))
                cleaned_text = cls._strip_model_switch_note(original_text)
                if cleaned_text != original_text.replace("\r\n", "\n").strip():
                    note_removed = True
                    if not cleaned_text:
                        continue
                    cleaned_part = dict(part)
                    cleaned_part["text"] = cleaned_text
                    cleaned_parts.append(cleaned_part)
                    continue
                # Runtime injection always targets the first text part. If that
                # part does not start with the exact marker, later text parts are
                # user content and must not be scanned or rewritten.
                return None
            cleaned_parts.append(part)
        if not note_removed:
            return None
        return json.dumps(cleaned_parts, ensure_ascii=False)

    @staticmethod
    def _normalize_retain_image_markers(text: str) -> str:
        """Canonicalize runtime image hints without retaining paths or pixels."""
        normalized = re.sub(
            r"\[Image attached at(?::)?\s*(https?://[^\]\s]+)\]",
            lambda match: f"[Image attached]\nImage URL: {match.group(1)}",
            str(text or ""),
            flags=re.IGNORECASE,
        )
        normalized = re.sub(
            r"\[Image attached at(?::)?\s*[^\]\n]+\]",
            "[Image attached]",
            normalized,
            flags=re.IGNORECASE,
        )
        normalized = re.sub(
            r"(?m)^\s*\[screenshot\]\s*$",
            "",
            normalized,
            flags=re.IGNORECASE,
        )
        return re.sub(r"\n{3,}", "\n\n", normalized).strip()

    @classmethod
    def _canonicalize_retain_multimodal_content(cls, content: Any) -> str | None:
        """Return text-only retain content for an OpenAI image-part payload."""
        parts = content
        if isinstance(parts, str):
            stripped = parts.strip()
            if not stripped.startswith("["):
                return None
            try:
                parts = json.loads(stripped)
            except Exception:
                return None
        if not isinstance(parts, list):
            return None
        image_types = {"image", "image_url", "input_image"}
        has_image = any(
            isinstance(part, dict) and str(part.get("type") or "") in image_types
            for part in parts
        )
        if not has_image:
            return None
        texts = [
            cls._stringify_retain_content(part.get("text")).strip()
            for part in parts
            if isinstance(part, dict)
            and part.get("type") == "text"
            and cls._stringify_retain_content(part.get("text")).strip()
        ]
        safe_image_urls: list[str] = []
        for part in parts:
            if not isinstance(part, dict) or str(part.get("type") or "") not in image_types:
                continue
            image_value = part.get("image_url")
            if isinstance(image_value, dict):
                image_value = image_value.get("url")
            if image_value is None:
                image_value = part.get("url")
            image_url = str(image_value or "").strip()
            if image_url.startswith(("https://", "http://")) and image_url not in safe_image_urls:
                safe_image_urls.append(image_url)
        text = "\n\n".join(texts)
        normalized = cls._normalize_retain_image_markers(text)
        if "[Image attached]" not in normalized:
            normalized = f"{normalized}\n\n[Image attached]" if normalized else "[Image attached]"
        if safe_image_urls:
            missing_url_lines = [
                f"Image URL: {image_url}"
                for image_url in safe_image_urls
                if f"Image URL: {image_url}" not in normalized.splitlines()
            ]
            if missing_url_lines:
                normalized += "\n" + "\n".join(missing_url_lines)
        return normalized

    @classmethod
    def _clean_retain_user_content(cls, content: Any) -> str:
        """Return retainable user text after stripping synthetic runtime noise.

        Compression rehydration, task-list rehydration, externalized payload
        placeholders, and async-delegation completion injections are useful for
        the live agent loop, but they are not conversation evidence. A pure
        noise message becomes empty and is dropped. Mixed messages keep only
        the real user text that remains after marker blocks are removed.
        """
        canonical_multimodal = cls._canonicalize_retain_multimodal_content(content)
        text = (
            canonical_multimodal
            if canonical_multimodal is not None
            else cls._stringify_retain_content(content)
        ).replace("\r\n", "\n").strip()
        if not text:
            return ""

        cleaned_multimodal = cls._clean_multimodal_model_switch_note(text)
        if cleaned_multimodal is not None:
            text = cleaned_multimodal
        else:
            text = cls._strip_leading_retain_runtime_injections(text)
        if not text:
            return ""

        if text.startswith(cls._RETAIN_OBJECTIVE_HEADER):
            text = text[len(cls._RETAIN_OBJECTIVE_HEADER):].lstrip("\n").strip()
            if not text:
                return ""
            text = cls._strip_leading_retain_runtime_injections(text)

        if cls._is_tool_budget_exhausted_notice(text) or cls._is_empty_tool_response_nudge(text):
            return ""

        if cls._is_skill_invocation_runtime_event(text):
            return cls._extract_skill_invocation_user_instruction(text)

        # Pure synthetic messages (todo/async/externalized/LCM summary blocks).
        # Objective header was already stripped above when present.
        if cls._starts_with_retain_noise_marker(text):
            return ""

        lines = text.split("\n")
        cleaned_lines: List[str] = []
        skipping_block = False
        for line in lines:
            stripped = line.strip()
            if cls._is_lcm_summary_header_line(stripped) or any(
                stripped.startswith(marker) for marker in cls._RETAIN_NOISE_MARKERS
            ):
                # Synthetic blocks are injected as whole segments; once a marker
                # starts, drop the remainder of that segment/message body.
                skipping_block = True
                continue
            if skipping_block:
                continue
            cleaned_lines.append(line)

        cleaned = "\n".join(cleaned_lines).strip()
        while cleaned.startswith("---"):
            cleaned = cleaned[3:].lstrip("\n").strip()
        while cleaned.endswith("---"):
            cleaned = cleaned[:-3].rstrip("\n").strip()
        if cleaned in {"", "---", "-"}:
            return ""
        return cls._normalize_retain_image_markers(cleaned)

    @classmethod
    def _is_retain_noise_assistant_content(cls, content: Any) -> bool:
        text = cls._stringify_retain_content(content).strip()
        if not text:
            return True
        if text == "(empty)":
            return True
        if text.startswith("Operation interrupted"):
            return True
        # Assistant-side synthetic rows are limited to compaction summaries.
        # User-side runtime markers can also be legitimate quoted Assistant
        # output and must not be filtered solely by matching their text.
        return cls._starts_with_lcm_summary_block(text)

    @classmethod
    def _extract_retain_out_of_band_user_messages(
        cls,
        message: Dict[str, Any],
    ) -> List[str]:
        """Extract the contiguous suffix of exactly bounded mid-turn user steers."""
        role = str(message.get("role") or "").strip()
        if role not in {"tool", "user"}:
            return []
        raw_content = message.get("content")
        candidates: List[str] = []
        if isinstance(raw_content, str):
            candidates.append(raw_content)
        elif isinstance(raw_content, list):
            for block in raw_content:
                if isinstance(block, dict) and block.get("type") == "text":
                    text = block.get("text")
                    if isinstance(text, str):
                        candidates.append(text)
        marker_start = f"{cls._RETAIN_OOB_USER_OPEN}\n"
        marker_end = f"\n{cls._RETAIN_OOB_USER_CLOSE}"
        extracted: List[str] = []
        for candidate in reversed(candidates):
            remaining = candidate.rstrip()
            candidate_messages: List[str] = []
            while remaining.endswith(marker_end):
                start_index = remaining.rfind(marker_start)
                if start_index < 0:
                    break
                prefix = remaining[:start_index]
                if prefix and not prefix.endswith("\n\n"):
                    break
                user_text = remaining[
                    start_index + len(marker_start):-len(marker_end)
                ].strip()
                if not user_text:
                    break
                candidate_messages.append(user_text)
                remaining = prefix[:-2] if prefix.endswith("\n\n") else prefix
                remaining = remaining.rstrip()
            if not candidate_messages:
                break
            extracted = list(reversed(candidate_messages)) + extracted
        if role == "tool":
            trusted_values = message.get(cls._RETAIN_OOB_TRUSTED_FIELD)
            if not isinstance(trusted_values, list):
                return []
            trusted = [
                cls._stringify_retain_content(value).strip()
                for value in trusted_values
                if cls._stringify_retain_content(value).strip()
            ]
            trusted_cursor = 0
            verified: List[str] = []
            for extracted_value in extracted:
                try:
                    match_index = trusted.index(extracted_value, trusted_cursor)
                except ValueError:
                    continue
                verified.append(extracted_value)
                trusted_cursor = match_index + 1
            return verified
        return extracted

    @classmethod
    def _extract_retain_out_of_band_user_message(
        cls,
        message: Dict[str, Any],
    ) -> str | None:
        messages = cls._extract_retain_out_of_band_user_messages(message)
        return messages[-1] if messages else None

    @staticmethod
    def _retain_source_occurrence_id(message: Dict[str, Any]) -> str:
        """Return a stable runtime/platform identity when the source exposes one."""
        persisted = str(message.get("_hermes_source_occurrence_id") or "").strip()
        if persisted:
            return persisted
        for key in ("_hermes_source_message_id", "message_id", "platform_message_id"):
            value = str(message.get(key) or "").strip()
            if value:
                return f"{key}:{value}"
        return ""

    @classmethod
    def _extract_retain_clarify_exchange(
        cls,
        message: Dict[str, Any],
    ) -> tuple[str, str | None] | None:
        """Extract the user-visible clarify card and its real user response."""
        if str(message.get("role") or "").strip() != "tool":
            return None
        if str(message.get("tool_name") or "").strip() != "clarify":
            return None
        raw_content = message.get("content")
        if isinstance(raw_content, str):
            marker_boundary = f"\n\n{cls._RETAIN_OOB_USER_OPEN}\n"
            if marker_boundary in raw_content and raw_content.rstrip().endswith(
                cls._RETAIN_OOB_USER_CLOSE
            ):
                raw_content = raw_content.split(marker_boundary, 1)[0]
        try:
            payload = json.loads(cls._stringify_retain_content(raw_content))
        except Exception:
            return None
        if not isinstance(payload, dict):
            return None
        question = cls._stringify_retain_content(payload.get("question")).strip()
        if not question:
            return None
        choices = payload.get("choices_offered")
        choice_texts = [
            cls._stringify_retain_content(choice).strip()
            for choice in choices
            if cls._stringify_retain_content(choice).strip()
        ] if isinstance(choices, list) else []
        visible_question = question
        if choice_texts:
            visible_question += "\n\nChoices offered:\n" + "\n".join(
                f"- {choice}" for choice in choice_texts
            )
        response = cls._stringify_retain_content(payload.get("user_response")).strip()
        if not response or re.fullmatch(r"\[user did not respond within \d+m\]", response):
            response = None
        return visible_question, response

    def _sanitize_persisted_turn_json(self, turn_json: str) -> str | None:
        """Rewrite/drop a provider-owned retained turn before reuse or submit.

        Historical rows written before noise cleaning may still contain synthetic
        user injections. Manual `/retain` and in-memory mirror-on-restart both
        load those rows, so re-clean here instead of trusting raw turn_json.
        """
        try:
            payload = json.loads(turn_json)
        except Exception:
            return None
        if not isinstance(payload, list):
            return None

        user_prefix = f"{self._retain_user_prefix}: "
        assistant_prefix = f"{self._retain_assistant_prefix}: "
        cleaned_msgs: List[Dict[str, Any]] = []
        kept_user = False
        kept_assistant = False
        recovery_projection = False

        for msg in payload:
            if not isinstance(msg, dict):
                continue
            role = str(msg.get("role") or "").strip()
            content = self._stringify_retain_content(msg.get("content"))
            if role == "user":
                body = content[len(user_prefix):] if content.startswith(user_prefix) else content
                cleaned_body = self._clean_retain_user_content(body)
                if not cleaned_body:
                    if self._is_empty_tool_response_nudge(body):
                        recovery_projection = True
                    if self._is_orphan_assistant_trigger_user_content(body):
                        # Drop the internal trigger payload but keep scanning:
                        # its final assistant response was visible to the user.
                        continue
                    # Other noise-only user turns are dropped as a unit.
                    return None
                kept_user = True
                new_msg = dict(msg)
                new_msg["content"] = f"{user_prefix}{cleaned_body}"
                cleaned_msgs.append(new_msg)
                continue
            if role == "assistant":
                body = content[len(assistant_prefix):] if content.startswith(assistant_prefix) else content
                if self._is_retain_noise_assistant_content(body):
                    continue
                kept_assistant = True
                new_msg = dict(msg)
                if recovery_projection:
                    new_msg["_hermes_empty_recovery_projection"] = True
                cleaned_msgs.append(new_msg)
                continue
            # Non user/assistant roles are not part of Hindsight turn payloads.
            continue

        if not cleaned_msgs or not (kept_user or kept_assistant):
            return None
        return json.dumps(cleaned_msgs, ensure_ascii=False)

    def _build_orphan_user_turn(
        self,
        user_content: str,
        *,
        user_timestamp: Any = None,
        user_occurrence_id: str = "",
        fallback_timestamp_now: bool = True,
    ) -> List[Dict[str, str]]:
        user_message = {
                "role": "user",
                "content": f"{self._retain_user_prefix}: {user_content}",
                "timestamp": self._retain_message_timestamp(
                    user_timestamp,
                    fallback_now=fallback_timestamp_now,
                ),
            }
        if user_occurrence_id:
            user_message["_hermes_source_occurrence_id"] = user_occurrence_id
        return [user_message]

    def _build_orphan_assistant_turn(
        self,
        assistant_content: str,
        *,
        assistant_timestamp: Any = None,
        fallback_timestamp_now: bool = True,
    ) -> List[Dict[str, str]]:
        return [
            {
                "role": "assistant",
                "content": f"{self._retain_assistant_prefix}: {assistant_content}",
                "timestamp": self._retain_message_timestamp(
                    assistant_timestamp,
                    fallback_now=fallback_timestamp_now,
                ),
            }
        ]

    def _build_turn_group_from_conversation_messages(self, messages: List[Dict[str, Any]]) -> List[str]:
        turns: List[str] = []
        pending_user: tuple[str, Any, str] | None = None
        pending_assistant: tuple[str, Any] | None = None
        pending_async_completion = False
        pending_async_assistant: tuple[str, Any] | None = None
        seen_user_occurrences: set[str] = set()
        duplicate_anchor_assistant = ""

        def _flush_pending_turn() -> None:
            nonlocal pending_user, pending_assistant
            if not pending_user:
                return
            user_content, user_timestamp, user_occurrence_id = pending_user
            if pending_assistant:
                assistant_content, assistant_timestamp = pending_assistant
                turns.append(json.dumps(
                    self._build_turn_messages(
                        user_content,
                        assistant_content,
                        user_timestamp=user_timestamp,
                        assistant_timestamp=assistant_timestamp,
                        user_occurrence_id=user_occurrence_id,
                        fallback_timestamp_now=False,
                    ),
                    ensure_ascii=False,
                ))
            else:
                turns.append(json.dumps(
                    self._build_orphan_user_turn(
                        user_content,
                        user_timestamp=user_timestamp,
                        user_occurrence_id=user_occurrence_id,
                        fallback_timestamp_now=False,
                    ),
                    ensure_ascii=False,
                ))
            pending_user = None
            pending_assistant = None

        def _flush_pending_async_assistant() -> None:
            nonlocal pending_async_completion, pending_async_assistant
            if not pending_async_assistant:
                return
            assistant_content, assistant_timestamp = pending_async_assistant
            turns.append(json.dumps(
                self._build_orphan_assistant_turn(
                    assistant_content,
                    assistant_timestamp=assistant_timestamp,
                    fallback_timestamp_now=False,
                ),
                ensure_ascii=False,
            ))
            pending_async_completion = False
            pending_async_assistant = None

        for msg in messages or []:
            role = str(msg.get("role") or "").strip()
            content = self._stringify_retain_content(msg.get("content")).strip()
            if not role or not content:
                continue
            if role == "assistant" and any(
                bool(msg.get(marker))
                for marker in (
                    "_empty_recovery_synthetic",
                    "_empty_terminal_sentinel",
                    "_thinking_prefill",
                )
            ):
                continue
            if role == "tool":
                clarify_exchange = self._extract_retain_clarify_exchange(msg)
                out_of_band_users = self._extract_retain_out_of_band_user_messages(msg)
                if not clarify_exchange and not out_of_band_users:
                    continue
                event_timestamp = msg.get("_timestamp", msg.get("timestamp"))
                if clarify_exchange:
                    visible_question, user_response = clarify_exchange
                    _flush_pending_async_assistant()
                    pending_async_completion = False
                    if pending_user and not pending_assistant:
                        pending_assistant = (visible_question, event_timestamp)
                        _flush_pending_turn()
                    else:
                        _flush_pending_turn()
                        turns.append(json.dumps(
                            self._build_orphan_assistant_turn(
                                visible_question,
                                assistant_timestamp=event_timestamp,
                                fallback_timestamp_now=False,
                            ),
                            ensure_ascii=False,
                        ))
                    if user_response:
                        pending_user = (user_response, event_timestamp, "")
                        pending_assistant = None
                    else:
                        # A timeout is framework state, not user speech. The
                        # model can still emit a visible timeout/follow-up.
                        pending_async_completion = True
                for out_of_band_user in out_of_band_users:
                    _flush_pending_async_assistant()
                    _flush_pending_turn()
                    pending_async_completion = False
                    pending_user = (out_of_band_user, event_timestamp, "")
                    pending_assistant = None
                continue
            if role == "user":
                out_of_band_user = self._extract_retain_out_of_band_user_message(msg)
                cleaned_user = out_of_band_user or self._clean_retain_user_content(content)
                if not cleaned_user:
                    # Synthetic user injections are not conversation turns.
                    # A tool-budget notice arrives while the real request may
                    # still be pending; keep that user open so the following
                    # visible final answer closes the correct turn. Other
                    # assistant-producing runtime triggers form an independent
                    # assistant-only event instead of borrowing an earlier user.
                    tool_budget_notice = self._is_tool_budget_exhausted_notice(content)
                    empty_recovery_nudge = bool(msg.get("_empty_recovery_synthetic")) or (
                        self._is_empty_tool_response_nudge(content)
                    )
                    if empty_recovery_nudge:
                        continue
                    preserves_visible_assistant = self._is_orphan_assistant_trigger_user_content(content)
                    if tool_budget_notice and pending_user and not pending_assistant:
                        _flush_pending_async_assistant()
                        pending_async_completion = False
                        continue
                    if pending_user and (pending_assistant or preserves_visible_assistant):
                        _flush_pending_turn()
                    _flush_pending_async_assistant()
                    if preserves_visible_assistant:
                        pending_async_completion = True
                    # Pure runtime-noise rows are transparent while an async or
                    # rehydration trigger is still waiting for its visible final
                    # assistant. A real user, a completed assistant event, or a
                    # new assistant-producing trigger closes/switches the boundary.
                    continue
                occurrence_id = self._retain_source_occurrence_id(msg)
                if occurrence_id and occurrence_id in seen_user_occurrences:
                    duplicate_anchor_assistant = pending_assistant[0] if pending_assistant else ""
                    _flush_pending_async_assistant()
                    _flush_pending_turn()
                    pending_async_completion = True
                    continue
                if occurrence_id:
                    seen_user_occurrences.add(occurrence_id)
                _flush_pending_async_assistant()
                _flush_pending_turn()
                pending_async_completion = False
                duplicate_anchor_assistant = ""
                pending_user = (
                    cleaned_user,
                    msg.get("_timestamp", msg.get("timestamp")),
                    occurrence_id,
                )
                pending_assistant = None
                continue
            if role == "assistant" and self._is_retain_noise_assistant_content(content):
                continue
            if role != "assistant":
                continue
            if msg.get("tool_calls") or msg.get("finish_reason") == "tool_calls":
                continue
            if not pending_user:
                if pending_async_completion:
                    if duplicate_anchor_assistant and content == duplicate_anchor_assistant:
                        pending_async_completion = False
                        duplicate_anchor_assistant = ""
                        continue
                    # Mirror normal user segments: retain the last eligible
                    # assistant, not an intermediate progress/draft message.
                    pending_async_assistant = (
                        content,
                        msg.get("_timestamp", msg.get("timestamp")),
                    )
                continue
            # A single user turn can have intermediate assistant scratch or
            # progress messages before the final user-visible response is
            # persisted. Keep the last eligible assistant in the segment rather
            # than letting the first one swallow the real final response.
            pending_assistant = (content, msg.get("_timestamp", msg.get("timestamp")))

        _flush_pending_turn()
        _flush_pending_async_assistant()
        return self._collapse_adjacent_replay_representations(turns)

    @classmethod
    def _collapse_adjacent_replay_representations(cls, turns: List[str]) -> List[str]:
        """Merge adjacent copies whose identical messages carry complementary timestamps."""
        collapsed: List[str] = []
        for turn_json in turns:
            if not collapsed:
                collapsed.append(turn_json)
                continue
            previous_identity = cls._retain_turn_replay_identity(collapsed[-1])
            current_identity = cls._retain_turn_replay_identity(turn_json)
            if cls._retain_closes_orphan_user(
                collapsed[-1],
                previous_identity,
                turn_json,
                current_identity,
            ):
                try:
                    previous_payload = json.loads(collapsed[-1])
                    current_payload = json.loads(turn_json)
                except Exception:
                    previous_payload = []
                    current_payload = []
                if (
                    isinstance(previous_payload, list)
                    and len(previous_payload) == 1
                    and isinstance(previous_payload[0], dict)
                    and isinstance(current_payload, list)
                    and len(current_payload) > 1
                    and isinstance(current_payload[0], dict)
                ):
                    old_user = previous_payload[0]
                    merged_user = dict(current_payload[0])
                    old_occurrence = str(
                        old_user.get("_hermes_source_occurrence_id") or ""
                    ).strip()
                    if old_occurrence:
                        merged_user["_hermes_source_occurrence_id"] = old_occurrence
                        if old_user.get("timestamp"):
                            merged_user["timestamp"] = old_user["timestamp"]
                    current_payload[0] = merged_user
                    collapsed[-1] = json.dumps(current_payload, ensure_ascii=False)
                    continue

            try:
                previous_payload = json.loads(collapsed[-1])
                current_payload = json.loads(turn_json)
            except Exception:
                previous_payload = []
                current_payload = []
            if (
                isinstance(previous_payload, list)
                and previous_payload
                and isinstance(previous_payload[-1], dict)
                and isinstance(current_payload, list)
                and len(current_payload) == 1
                and isinstance(current_payload[0], dict)
                and current_payload[0].get("_hermes_empty_recovery_projection")
                and previous_payload[-1].get("role") == "assistant"
                and current_payload[0].get("role") == "assistant"
                and str(previous_payload[-1].get("content") or "")
                == str(current_payload[0].get("content") or "")
            ):
                merged_assistant = dict(previous_payload[-1])
                merged_assistant["timestamp"] = str(
                    previous_payload[-1].get("timestamp")
                    or current_payload[0].get("timestamp")
                    or ""
                )
                merged_assistant.pop("_hermes_empty_recovery_projection", None)
                previous_payload[-1] = merged_assistant
                collapsed[-1] = json.dumps(previous_payload, ensure_ascii=False)
                continue
            exact_match = (
                cls._retain_turn_canonical(collapsed[-1])
                == cls._retain_turn_canonical(turn_json)
            )
            old_urls = cls._retain_turn_safe_image_urls(collapsed[-1])
            new_urls = cls._retain_turn_safe_image_urls(turn_json)
            representation_match = (
                bool(old_urls) != bool(new_urls)
                and cls._retain_turn_representation_canonical(collapsed[-1])
                == cls._retain_turn_representation_canonical(turn_json)
            )
            if not exact_match and not representation_match:
                collapsed.append(turn_json)
                continue
            try:
                previous = json.loads(collapsed[-1])
                current = json.loads(turn_json)
            except Exception:
                collapsed.append(turn_json)
                continue
            if (
                not isinstance(previous, list)
                or not isinstance(current, list)
                or len(previous) != len(current)
            ):
                collapsed.append(turn_json)
                continue
            merged: List[Dict[str, Any]] = []
            complementary = False
            compatible = True
            for old_message, new_message in zip(previous, current):
                if not isinstance(old_message, dict) or not isinstance(new_message, dict):
                    compatible = False
                    break
                old_timestamp = str(old_message.get("timestamp") or "")
                new_timestamp = str(new_message.get("timestamp") or "")
                if old_timestamp and new_timestamp and old_timestamp != new_timestamp:
                    compatible = False
                    break
                complementary = complementary or bool(old_timestamp) != bool(new_timestamp)
                merged_message = dict(old_message)
                old_content = str(old_message.get("content") or "")
                new_content = str(new_message.get("content") or "")
                if (
                    not cls._retain_safe_image_urls_from_text(old_content)
                    and cls._retain_safe_image_urls_from_text(new_content)
                ):
                    merged_message["content"] = new_content
                merged_message["timestamp"] = old_timestamp or new_timestamp
                merged.append(merged_message)
            if not compatible or not complementary:
                collapsed.append(turn_json)
                continue
            collapsed[-1] = json.dumps(merged, ensure_ascii=False)
        cleaned: List[str] = []
        for turn_json in collapsed:
            try:
                payload = json.loads(turn_json)
            except Exception:
                cleaned.append(turn_json)
                continue
            changed = False
            if isinstance(payload, list):
                for message in payload:
                    if isinstance(message, dict) and "_hermes_empty_recovery_projection" in message:
                        message.pop("_hermes_empty_recovery_projection", None)
                        changed = True
            cleaned.append(json.dumps(payload, ensure_ascii=False) if changed else turn_json)
        return cleaned

    @staticmethod
    def _retain_safe_image_urls_from_text(text: str) -> tuple[str, ...]:
        return tuple(
            re.findall(r"(?m)^Image URL: (https?://\S+)\s*$", str(text or ""))
        )

    @classmethod
    def _retain_turn_safe_image_urls(cls, turn_json: str) -> tuple[str, ...]:
        try:
            payload = json.loads(turn_json)
        except Exception:
            return tuple()
        if not isinstance(payload, list):
            return tuple()
        return tuple(
            image_url
            for message in payload
            if isinstance(message, dict)
            for image_url in cls._retain_safe_image_urls_from_text(
                str(message.get("content") or "")
            )
        )

    @classmethod
    def _retain_turn_representation_canonical(cls, turn_json: str) -> tuple:
        try:
            payload = json.loads(turn_json)
        except Exception:
            return (("raw", str(turn_json)),)
        if not isinstance(payload, list):
            return (("raw", str(payload)),)
        canonical = []
        for message in payload:
            if not isinstance(message, dict):
                canonical.append(("raw", str(message)))
                continue
            content = re.sub(
                r"(?m)^Image URL: https?://\S+\s*$",
                "",
                str(message.get("content") or ""),
            )
            content = re.sub(r"\n{3,}", "\n\n", content).strip()
            canonical.append((str(message.get("role") or ""), content))
        return tuple(canonical)

    @staticmethod
    def _retain_turn_source_occurrence_id(turn_json: str) -> str:
        try:
            payload = json.loads(turn_json)
        except Exception:
            return ""
        if not isinstance(payload, list):
            return ""
        for message in payload:
            if not isinstance(message, dict) or message.get("role") != "user":
                continue
            occurrence_id = str(
                message.get("_hermes_source_occurrence_id") or ""
            ).strip()
            if occurrence_id:
                return occurrence_id
        return ""

    @classmethod
    def _retain_closes_orphan_user(
        cls,
        old_turn_json: str,
        old_identity: tuple,
        incoming_turn_json: str,
        incoming_identity: tuple,
    ) -> bool:
        """Whether *incoming* completes a persisted orphan user-only turn.

        Compression/replay can rehydrate the same user event with a later
        timestamp. Prefer stable source occurrence ids when both sides expose
        one: equal ids close, unequal ids never close. Without ids, allow
        same full identity, or same content when completing with an assistant
        (needed for compressed windows that lost platform ids; replace still
        matches sequentially). One-sided id + completing also allows same
        content so a lost id on the compressed window can still finish the
        orphan.
        """
        if not (
            len(old_identity) == 1
            and old_identity[0][0] == "user"
            and incoming_identity
            and incoming_identity[0][0] == "user"
        ):
            return False

        old_occ = cls._retain_turn_source_occurrence_id(old_turn_json)
        new_occ = cls._retain_turn_source_occurrence_id(incoming_turn_json)
        if old_occ and new_occ:
            return old_occ == new_occ

        if incoming_identity[0] == old_identity[0]:
            return True
        if incoming_identity[0][1] != old_identity[0][1]:
            return False
        # Completing an orphan (user + assistant) with same content.
        return len(incoming_identity) > 1

    @staticmethod
    def _retain_turn_canonical(turn_json: str) -> tuple:
        try:
            payload = json.loads(turn_json)
        except Exception:
            return (("raw", str(turn_json)),)
        canonical = []
        if not isinstance(payload, list):
            return (("raw", str(payload)),)
        for msg in payload:
            if not isinstance(msg, dict):
                canonical.append(("raw", str(msg)))
                continue
            canonical.append((str(msg.get("role") or ""), str(msg.get("content") or "")))
        return tuple(canonical)

    @staticmethod
    def _retain_turn_replay_identity(turn_json: str) -> tuple:
        try:
            payload = json.loads(turn_json)
        except Exception:
            return tuple()
        if not isinstance(payload, list):
            return tuple()
        identity = []
        for msg in payload:
            if not isinstance(msg, dict):
                continue
            identity.append(
                (
                    str(msg.get("role") or ""),
                    str(msg.get("content") or ""),
                    str(msg.get("timestamp") or ""),
                )
            )
        return tuple(identity)

    @classmethod
    def _retain_turn_replay_timestamp(cls, turn_json: str) -> str:
        identity = cls._retain_turn_replay_identity(turn_json)
        return str(identity[0][2]) if identity else ""

    @staticmethod
    def _retain_timestamp_order_value(value: str) -> float | None:
        text = str(value or "").strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()

    @classmethod
    def _retain_turns_strictly_after(
        cls,
        incoming_turns: List[str],
        *,
        cutoff: str,
        seen_message_ids: set[tuple],
        allow_unknown_assistant_only: bool = False,
    ) -> List[str]:
        """Keep only replay messages provably newer than persisted history."""
        retained: list[str] = []
        seen = set(seen_message_ids)
        for turn_json in incoming_turns:
            try:
                payload = json.loads(turn_json)
            except Exception:
                continue
            if not isinstance(payload, list):
                continue
            identities = cls._retain_turn_replay_identity(turn_json)
            has_known_old_message = any(
                identity[2] and identity[2] <= cutoff
                for identity in identities
            )
            has_known_new_message = any(
                identity[2] and identity[2] > cutoff and identity not in seen
                for identity in identities
            )
            if not has_known_new_message:
                unknown_assistant_only = bool(
                    allow_unknown_assistant_only
                    and identities
                    and all(
                        identity[0] == "assistant"
                        and not identity[2]
                        and identity not in seen
                        for identity in identities
                    )
                )
                if not unknown_assistant_only:
                    continue

            later_messages: list[dict] = []
            for message, identity in zip(payload, identities):
                if not isinstance(message, dict) or identity in seen:
                    continue
                if has_known_old_message and not (identity[2] and identity[2] > cutoff):
                    # A missing timestamp in an overlapping historical turn is
                    # unknown, not evidence that the message happened after the
                    # persisted cutoff. Only an explicit newer timestamp can
                    # split a genuinely later event out of such a turn.
                    continue
                later_messages.append(message)
                seen.add(identity)
            if later_messages:
                retained.append(json.dumps(later_messages, ensure_ascii=False))
        return retained

    @classmethod
    def _merge_overlapping_replayed_turns(
        cls,
        existing_turns: List[str],
        incoming_turns: List[str],
    ) -> List[str] | None:
        """Merge an anchored partial transcript window into persisted history."""
        existing_ids = [cls._retain_turn_replay_identity(turn) for turn in existing_turns]
        incoming_ids = [cls._retain_turn_replay_identity(turn) for turn in incoming_turns]
        existing_canonical = [cls._retain_turn_canonical(turn) for turn in existing_turns]
        incoming_canonical = [cls._retain_turn_canonical(turn) for turn in incoming_turns]
        matched_pairs: list[tuple[int, int, str]] = []
        existing_cursor = 0

        def _next_exact_existing_index(
            incoming_index: int,
            existing_start: int,
        ) -> int | None:
            for future_index in range(incoming_index + 1, len(incoming_ids)):
                future_exact = next(
                    (
                        existing_index
                        for existing_index in range(existing_start, len(existing_ids))
                        if incoming_ids[future_index]
                        and incoming_ids[future_index] == existing_ids[existing_index]
                    ),
                    None,
                )
                if future_exact is not None:
                    return future_exact
            return None

        for incoming_index, incoming_value in enumerate(incoming_canonical):
            if not incoming_value:
                continue
            candidates = [
                existing_index
                for existing_index in range(existing_cursor, len(existing_canonical))
                if incoming_value == existing_canonical[existing_index]
            ]
            exact_canonical_identity = next(
                (
                    existing_index
                    for existing_index in candidates
                    if incoming_ids[incoming_index] == existing_ids[existing_index]
                ),
                None,
            )
            if candidates and exact_canonical_identity is None:
                incoming_timestamps = [
                    timestamp
                    for _role, _content, timestamp in incoming_ids[incoming_index]
                    if timestamp
                ]
                candidate_timestamps = [
                    timestamp
                    for existing_index in candidates
                    for _role, _content, timestamp in existing_ids[existing_index]
                    if timestamp
                ]
                incoming_has_complete_timestamps = (
                    len(incoming_timestamps) == len(incoming_ids[incoming_index])
                )
                if (
                    incoming_has_complete_timestamps
                    and candidate_timestamps
                    and min(incoming_timestamps) > max(candidate_timestamps)
                ):
                    future_exact = _next_exact_existing_index(
                        incoming_index,
                        existing_cursor,
                    )
                    has_candidate_before_future_anchor = (
                        future_exact is not None
                        and any(candidate < future_exact for candidate in candidates)
                    )
                    has_novel_prefix_before_future_anchor = (
                        future_exact is not None
                        and any(
                            incoming_canonical[prefix_index]
                            and incoming_canonical[prefix_index]
                            not in existing_canonical[:future_exact]
                            for prefix_index in range(incoming_index)
                        )
                    )
                    if not (
                        has_candidate_before_future_anchor
                        and has_novel_prefix_before_future_anchor
                    ):
                        # Stable timestamps prove that this identical text recurred
                        # after every persisted candidate. It is not a replay anchor.
                        continue
            match_kind = "exact"
            if not candidates:
                incoming_identity = incoming_ids[incoming_index]
                candidates = [
                    existing_index
                    for existing_index in range(existing_cursor, len(existing_ids))
                    if incoming_identity
                    and existing_ids[existing_index]
                    and incoming_identity[0][0] == "user"
                    and incoming_identity[0] == existing_ids[existing_index][0]
                ]
                if candidates:
                    match_kind = "shared_user"
            if not candidates and incoming_ids[incoming_index]:
                incoming_message_ids = set(incoming_ids[incoming_index])
                candidates = [
                    existing_index
                    for existing_index in range(existing_cursor, len(existing_ids))
                    if incoming_message_ids.intersection(existing_ids[existing_index])
                ]
                if candidates:
                    match_kind = "shared_message"
            if not candidates and incoming_ids[incoming_index]:
                incoming_identity = incoming_ids[incoming_index]
                incoming_user = incoming_identity[0][:2]
                if incoming_user[0] == "user":
                    incoming_occ = cls._retain_turn_source_occurrence_id(
                        incoming_turns[incoming_index]
                    )
                    user_candidates = []
                    for existing_index in range(existing_cursor, len(existing_ids)):
                        if not (
                            existing_ids[existing_index]
                            and existing_ids[existing_index][0][:2] == incoming_user
                        ):
                            continue
                        existing_occ = cls._retain_turn_source_occurrence_id(
                            existing_turns[existing_index]
                        )
                        # Distinct platform occurrences of the same short text
                        # (e.g. two later 「继续」) are different events.
                        if (
                            incoming_occ
                            and existing_occ
                            and incoming_occ != existing_occ
                        ):
                            continue
                        user_candidates.append(existing_index)
                    incoming_user_count = sum(
                        1
                        for candidate_identity in incoming_ids[incoming_index:]
                        if candidate_identity and candidate_identity[0][:2] == incoming_user
                    )
                    if len(user_candidates) == 1 and incoming_user_count == 1:
                        candidates = user_candidates
                        match_kind = "shared_user"
            if not candidates:
                continue
            exact_identity = next(
                (
                    existing_index
                    for existing_index in candidates
                    if incoming_ids[incoming_index] == existing_ids[existing_index]
                ),
                None,
            )
            if exact_identity is None:
                # A later exact anchor bounds which repeated canonical candidate
                # can represent this partial replay position. Timestamp drift on
                # the repeated item must not force the earliest textual match.
                next_exact_existing = _next_exact_existing_index(
                    incoming_index,
                    existing_cursor,
                )
                if next_exact_existing is not None:
                    bounded = [
                        candidate for candidate in candidates
                        if candidate < next_exact_existing
                    ]
                    if not bounded:
                        continue
                    candidates = bounded

                    if len(candidates) > 1:
                        incoming_time = cls._retain_timestamp_order_value(
                            cls._retain_turn_replay_timestamp(incoming_turns[incoming_index])
                        )
                        timed_candidates = []
                        if incoming_time is not None:
                            for candidate in candidates:
                                candidate_time = cls._retain_timestamp_order_value(
                                    cls._retain_turn_replay_timestamp(existing_turns[candidate])
                                )
                                if candidate_time is not None:
                                    timed_candidates.append((abs(candidate_time - incoming_time), candidate))
                        if timed_candidates:
                            candidates = [min(timed_candidates)[1]]
            existing_index = exact_identity if exact_identity is not None else candidates[0]
            matched_pairs.append((incoming_index, existing_index, match_kind))
            existing_cursor = existing_index + 1
        if not matched_pairs:
            return None

        merged: list[str] = []
        seen_ids = set(existing_ids)
        seen_message_ids = {
            message_identity
            for turn_identity in existing_ids
            for message_identity in turn_identity
        }
        incoming_cursor = 0
        existing_cursor = 0
        for incoming_index, existing_index, match_kind in matched_pairs:
            merged.extend(existing_turns[existing_cursor:existing_index])
            unmatched_prefix = incoming_turns[incoming_cursor:incoming_index]
            previous_existing_timestamps = [
                timestamp
                for turn_json in existing_turns[:existing_index]
                for _role, _content, timestamp in cls._retain_turn_replay_identity(turn_json)
                if timestamp
            ]
            if unmatched_prefix and previous_existing_timestamps:
                safe_prefix = cls._retain_turns_strictly_after(
                    unmatched_prefix,
                    cutoff=max(previous_existing_timestamps),
                    seen_message_ids=seen_message_ids,
                    allow_unknown_assistant_only=True,
                )
            else:
                safe_prefix = unmatched_prefix
            for candidate_turn in safe_prefix:
                candidate_identity = cls._retain_turn_replay_identity(candidate_turn)
                if candidate_identity not in seen_ids:
                    merged.append(candidate_turn)
                    seen_ids.add(candidate_identity)
                    seen_message_ids.update(candidate_identity)
            recovers_orphan_assistant = bool(
                match_kind == "shared_message"
                and len(existing_ids[existing_index]) == 1
                and existing_ids[existing_index][0][0] == "assistant"
                and incoming_ids[incoming_index]
                and incoming_ids[incoming_index][0][0] == "user"
                and existing_ids[existing_index][0] in incoming_ids[incoming_index]
            )
            if (
                match_kind == "shared_user"
                and len(existing_ids[existing_index]) == 1
            ) or recovers_orphan_assistant:
                # A later replay can complete either side of an orphan event:
                # append the Assistant to an orphan User, or restore the real
                # User that belongs to an already persisted Assistant.
                merged.append(incoming_turns[incoming_index])
                seen_ids.add(incoming_ids[incoming_index])
                seen_message_ids.update(incoming_ids[incoming_index])
            else:
                merged.append(existing_turns[existing_index])
                if match_kind == "shared_user":
                    # A completed persisted answer is authoritative. A later
                    # assistant message before the next user is a separate visible
                    # event, even though transcript grouping folds it into the same
                    # user turn during replay.  Preserve both without matching any
                    # incident-specific recovery text.
                    try:
                        incoming_payload = json.loads(incoming_turns[incoming_index])
                    except Exception:
                        incoming_payload = []
                    for message, message_identity in zip(
                        incoming_payload[1:] if isinstance(incoming_payload, list) else [],
                        incoming_ids[incoming_index][1:],
                    ):
                        if message_identity in seen_message_ids:
                            continue
                        singleton = json.dumps([message], ensure_ascii=False)
                        singleton_id = cls._retain_turn_replay_identity(singleton)
                        merged.append(singleton)
                        seen_ids.add(singleton_id)
                        seen_message_ids.add(message_identity)
            incoming_cursor = incoming_index + 1
            existing_cursor = existing_index + 1

        existing_tail = existing_turns[existing_cursor:]
        incoming_tail: list[str] = []
        for index in range(incoming_cursor, len(incoming_turns)):
            if incoming_ids[index] in seen_ids:
                continue
            incoming_tail.append(incoming_turns[index])
            seen_ids.add(incoming_ids[index])
        if not existing_tail:
            merged.extend(incoming_tail)
            return merged
        if not incoming_tail:
            merged.extend(existing_tail)
            return merged

        # Both sides have an unmatched tail. Preserve each side's order and use
        # timestamps only to interleave the tails; anchored middle blocks above
        # intentionally follow transcript order because rehydrated timestamps
        # can differ from the provider-owned persisted timestamps.
        existing_tail_cursor = 0
        incoming_tail_cursor = 0
        while existing_tail_cursor < len(existing_tail) and incoming_tail_cursor < len(incoming_tail):
            existing_timestamp = cls._retain_turn_replay_timestamp(existing_tail[existing_tail_cursor])
            incoming_timestamp = cls._retain_turn_replay_timestamp(incoming_tail[incoming_tail_cursor])
            if incoming_timestamp and existing_timestamp and incoming_timestamp < existing_timestamp:
                merged.append(incoming_tail[incoming_tail_cursor])
                incoming_tail_cursor += 1
            else:
                merged.append(existing_tail[existing_tail_cursor])
                existing_tail_cursor += 1
        merged.extend(existing_tail[existing_tail_cursor:])
        merged.extend(incoming_tail[incoming_tail_cursor:])
        return merged

    def _dedupe_replayed_turn_groups(self, turn_groups: List[List[str]]) -> List[str]:
        """Remove only exact parent/child boundary replay overlap.

        Compression/resume can replay the tail of a parent transcript at the
        start of a child session. Drop the longest child prefix whose retain
        turns exactly match the already-accumulated suffix. Do not global-dedupe
        by content: a user may legitimately repeat the same sentence later.
        """
        merged: List[str] = []
        merged_canon: List[tuple] = []
        for group in turn_groups:
            if not group:
                continue
            group_canon = [self._retain_turn_canonical(turn) for turn in group]
            overlap = 0
            max_overlap = min(len(merged_canon), len(group_canon))
            for size in range(max_overlap, 0, -1):
                if merged_canon[-size:] == group_canon[:size]:
                    overlap = size
                    break
            merged.extend(group[overlap:])
            merged_canon.extend(group_canon[overlap:])
        return merged

    def _build_turns_from_conversation_messages(self, messages: List[Dict[str, Any]]) -> List[str]:
        """Build retain turn JSON from an explicit transcript payload.

        This helper is retained for compatibility with callers that deliberately
        pass a transcript. It is not the authoritative path for user-facing
        manual `/retain`: that command must read provider-owned persisted turns
        from `retain_turns.sqlite3` via `retain_persisted_session_lineage()`.
        """
        messages = list(messages or [])
        if not any(str(msg.get("_session_id") or "").strip() for msg in messages):
            return self._build_turn_group_from_conversation_messages(messages)

        groups: List[List[Dict[str, Any]]] = []
        current_sid = object()
        current_group: List[Dict[str, Any]] = []
        for msg in messages:
            sid = str(msg.get("_session_id") or "").strip()
            marker = sid or current_sid
            if current_group and marker != current_sid:
                groups.append(current_group)
                current_group = []
            current_group.append(msg)
            current_sid = marker
        if current_group:
            groups.append(current_group)

        turn_groups = [self._build_turn_group_from_conversation_messages(group) for group in groups]
        return self._dedupe_replayed_turn_groups(turn_groups)

    def _retain_store_connect(self) -> sqlite3.Connection:
        self._retain_store_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self._retain_store_path), timeout=30)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS hindsight_retain_turns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                bank_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                parent_session_id TEXT NOT NULL DEFAULT '',
                retain_document_id TEXT NOT NULL DEFAULT '',
                turn_index INTEGER NOT NULL,
                turn_json TEXT NOT NULL,
                created_at REAL NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_hindsight_retain_turns_session "
            "ON hindsight_retain_turns(bank_id, session_id, id)"
        )
        try:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(hindsight_retain_turns)").fetchall()}
            if "retain_document_id" not in columns:
                conn.execute("ALTER TABLE hindsight_retain_turns ADD COLUMN retain_document_id TEXT NOT NULL DEFAULT ''")
            if "active" not in columns:
                conn.execute("ALTER TABLE hindsight_retain_turns ADD COLUMN active INTEGER NOT NULL DEFAULT 1")
            if "rewound_at" not in columns:
                conn.execute("ALTER TABLE hindsight_retain_turns ADD COLUMN rewound_at REAL")
        except Exception as e:
            logger.debug("Hindsight retain store migration skipped/failed: %s", e)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_hindsight_retain_turns_document "
            "ON hindsight_retain_turns(bank_id, retain_document_id, id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_hindsight_retain_turns_session_active "
            "ON hindsight_retain_turns(session_id, active, id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_hindsight_retain_turns_document_active "
            "ON hindsight_retain_turns(retain_document_id, active, id)"
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS hindsight_retain_submissions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                bank_id TEXT NOT NULL,
                document_id TEXT NOT NULL,
                update_mode TEXT NOT NULL DEFAULT '',
                content_json TEXT NOT NULL,
                status TEXT NOT NULL,
                queued_at REAL NOT NULL,
                completed_at REAL,
                error TEXT NOT NULL DEFAULT ''
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_hindsight_retain_submissions_document "
            "ON hindsight_retain_submissions(bank_id, document_id, id)"
        )
        return conn

    def _begin_retain_submission(
        self,
        *,
        bank_id: str,
        document_id: str,
        update_mode: str | None,
        content: str,
    ) -> int:
        """Persist the exact outbound document payload before it is queued."""
        with self._retain_store_connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO hindsight_retain_submissions
                (bank_id, document_id, update_mode, content_json, status, queued_at)
                VALUES (?, ?, ?, ?, 'queued', ?)
                """,
                (bank_id, document_id, update_mode or "", content, time.time()),
            )
            if cursor.lastrowid is None:
                raise RuntimeError("Hindsight retain submission ledger did not return an id")
            return int(cursor.lastrowid)

    def _finish_retain_submission(self, submission_id: int, error: BaseException | None = None) -> None:
        try:
            with self._retain_store_connect() as conn:
                conn.execute(
                    """
                    UPDATE hindsight_retain_submissions
                    SET status = ?, completed_at = ?, error = ?
                    WHERE id = ?
                    """,
                    (
                        "failed" if error is not None else "succeeded",
                        time.time(),
                        str(error) if error is not None else "",
                        int(submission_id),
                    ),
                )
        except Exception:
            # The remote side effect may already have happened. Leave the row
            # queued/unresolved rather than turning a successful API call into
            # an apparent retain failure that could be retried and duplicated.
            logger.warning(
                "Failed to finish Hindsight retain submission ledger row %s",
                submission_id,
                exc_info=True,
            )

    def _lookup_retain_document_id(self, conn: sqlite3.Connection, session_id: str) -> str:
        sid = str(session_id or "").strip()
        if not sid:
            return ""
        row = conn.execute(
            """
            SELECT retain_document_id
            FROM hindsight_retain_turns
            WHERE session_id = ?
              AND retain_document_id != ''
            ORDER BY id DESC
            LIMIT 1
            """,
            (sid,),
        ).fetchone()
        return str(row[0] if row and row[0] else "").strip()

    def _lookup_root_retain_document_id(self, conn: sqlite3.Connection, session_id: str) -> str:
        """Return the earliest non-empty retain document for a session.

        Continuation sessions can later write split rows under their own
        session id. For a child resolving through its parent, the first retained
        document observed for that parent is the inherited logical root; the
        latest row may be a split child/parent document and must not hide the
        original session lineage.
        """
        sid = str(session_id or "").strip()
        if not sid:
            return ""
        row = conn.execute(
            """
            SELECT retain_document_id
            FROM hindsight_retain_turns
            WHERE session_id = ?
              AND retain_document_id != ''
            ORDER BY id ASC
            LIMIT 1
            """,
            (sid,),
        ).fetchone()
        return str(row[0] if row and row[0] else "").strip()

    def _resolve_retain_document_id(self, conn: sqlite3.Connection, session_id: str, parent_session_id: str = "") -> str:
        sid = str(session_id or "").strip()
        parent = str(parent_session_id or "").strip()
        if parent:
            parent_doc = self._lookup_root_retain_document_id(conn, parent)
            return parent_doc or parent
        existing = self._lookup_retain_document_id(conn, sid)
        if existing:
            return existing
        return sid

    def _persist_retain_turn(self, turn_json: str) -> None:
        if not self._session_id:
            return
        try:
            with self._retain_store_connect() as conn:
                retain_document_id = self._retain_document_id or self._resolve_retain_document_id(
                    conn, self._session_id, self._parent_session_id
                )
                self._retain_document_id = retain_document_id
                conn.execute(
                    """
                    INSERT INTO hindsight_retain_turns
                    (bank_id, session_id, parent_session_id, retain_document_id, turn_index, turn_json, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        self._bank_id,
                        self._session_id,
                        self._parent_session_id,
                        retain_document_id,
                        self._turn_index,
                        turn_json,
                        time.time(),
                    ),
                )
        except Exception as e:
            logger.warning("Hindsight retain store write failed: %s", e, exc_info=True)

    def _replace_active_persisted_turns(
        self,
        turns: List[str],
        *,
        lineage_session_ids: List[str],
        retain_document_id: str,
    ) -> bool:
        """Soft-replace a reconciled replay while preserving lineage ownership."""
        sid = str(self._session_id or "").strip()
        if not sid or not turns:
            return False
        lineage = [str(value).strip() for value in lineage_session_ids if str(value).strip()]
        if not lineage:
            lineage = [sid]
        document_id = str(retain_document_id or self._retain_document_id or sid).strip() or sid
        now = time.time()
        try:
            with self._retain_store_connect() as conn:
                rows = conn.execute(
                    """
                    SELECT bank_id, session_id, parent_session_id, turn_json
                    FROM hindsight_retain_turns
                    WHERE retain_document_id = ? AND active = 1
                    ORDER BY id ASC
                    """,
                    (document_id,),
                ).fetchall()
                matched_by_document = bool(rows)
                if not rows:
                    placeholders = ",".join("?" for _ in lineage)
                    rows = conn.execute(
                        f"""
                        SELECT bank_id, session_id, parent_session_id, turn_json
                        FROM hindsight_retain_turns
                        WHERE active = 1 AND session_id IN ({placeholders})
                        ORDER BY id ASC
                        """,
                        tuple(lineage),
                    ).fetchall()
                if not rows:
                    return False

                old_entries: list[tuple[str, tuple, tuple[str, str, str]]] = []
                for bank_id, row_session_id, row_parent_session_id, turn_json in rows:
                    cleaned = self._sanitize_persisted_turn_json(str(turn_json or ""))
                    if not cleaned:
                        continue
                    old_entries.append(
                        (
                            cleaned,
                            self._retain_turn_replay_identity(cleaned),
                            (
                                str(bank_id or self._bank_id),
                                str(row_session_id or sid),
                                str(row_parent_session_id or ""),
                            ),
                        )
                    )
                if not old_entries:
                    return False

                incoming = [
                    (turn_json, self._retain_turn_replay_identity(turn_json))
                    for turn_json in turns
                ]
                owners: dict[int, tuple[str, str, str]] = {}
                old_index = 0
                for incoming_index, (turn_json, identity) in enumerate(incoming):
                    old_turn_json, old_identity, old_owner = old_entries[old_index]
                    closes_orphan_user = self._retain_closes_orphan_user(
                        old_turn_json,
                        old_identity,
                        turn_json,
                        identity,
                    )
                    recovers_orphan_assistant = bool(
                        len(old_identity) == 1
                        and old_identity[0][0] == "assistant"
                        and identity
                        and identity[0][0] == "user"
                        and old_identity[0] in identity
                    )
                    if identity == old_identity or closes_orphan_user or recovers_orphan_assistant:
                        owners[incoming_index] = old_owner
                        old_index += 1
                        if old_index == len(old_entries):
                            break
                if old_index != len(old_entries):
                    return False

                # Missing events belong to the next matched session segment;
                # trailing additions belong to the active session.
                next_owner: tuple[str, str, str] | None = None
                active_owner = (self._bank_id, sid, self._parent_session_id)
                for incoming_index in range(len(incoming) - 1, -1, -1):
                    if incoming_index in owners:
                        next_owner = owners[incoming_index]
                    else:
                        owners[incoming_index] = next_owner or active_owner

                if matched_by_document:
                    conn.execute(
                        """
                        UPDATE hindsight_retain_turns
                        SET active = 0, rewound_at = ?
                        WHERE retain_document_id = ? AND active = 1
                        """,
                        (now, document_id),
                    )
                else:
                    placeholders = ",".join("?" for _ in lineage)
                    conn.execute(
                        f"""
                        UPDATE hindsight_retain_turns
                        SET active = 0, rewound_at = ?
                        WHERE active = 1 AND session_id IN ({placeholders})
                        """,
                        (now, *lineage),
                    )
                per_session_index: dict[str, int] = {}
                for incoming_index, (turn_json, _canonical) in enumerate(incoming):
                    owner_bank, owner_session, owner_parent = owners[incoming_index]
                    per_session_index[owner_session] = per_session_index.get(owner_session, 0) + 1
                    conn.execute(
                        """
                        INSERT INTO hindsight_retain_turns
                        (bank_id, session_id, parent_session_id, retain_document_id,
                         turn_index, turn_json, created_at, active)
                        VALUES (?, ?, ?, ?, ?, ?, ?, 1)
                        """,
                        (
                            owner_bank,
                            owner_session,
                            owner_parent,
                            document_id,
                            per_session_index[owner_session],
                            turn_json,
                            now + ((incoming_index + 1) * 0.000001),
                        ),
                    )
            return True
        except Exception as e:
            logger.warning("Hindsight retain store replay reconciliation failed: %s", e, exc_info=True)
            return False

    def _retain_turn_contains_real_user(self, turn_json: str) -> bool:
        try:
            payload = json.loads(turn_json)
        except Exception:
            return False
        if not isinstance(payload, list):
            return False
        user_prefix = f"{self._retain_user_prefix}: "
        for msg in payload:
            if not isinstance(msg, dict) or str(msg.get("role") or "").strip() != "user":
                continue
            content = self._stringify_retain_content(msg.get("content"))
            body = content[len(user_prefix):] if content.startswith(user_prefix) else content
            if self._clean_retain_user_content(body):
                return True
        return False

    def _retain_rewind_suffix_size(self, turns: List[str], user_turns: int) -> int:
        if not turns:
            return 0
        remaining = max(1, int(user_turns))
        for suffix_size, turn_json in enumerate(reversed(turns), 1):
            if self._retain_turn_contains_real_user(turn_json):
                remaining -= 1
                if remaining == 0:
                    return suffix_size
        # Match the old best-effort behavior when the caller asks to rewind
        # more user turns than this local buffer currently contains.
        return len(turns)

    def mark_persisted_turns_rewound(self, session_id: str, turns_undone: int = 1) -> int:
        """Soft-exclude the last N active persisted turns for a session.

        `/undo N` rewinds N user turns in SessionDB. Hindsight keeps a
        provider-owned retain-turn store for manual `/retain`, so mirror the
        rewind there with `active=0` rather than hard-deleting rows.
        """
        sid = str(session_id or "").strip()
        if not sid:
            return 0
        try:
            limit = int(turns_undone)
        except (TypeError, ValueError):
            limit = 1
        if limit < 1:
            limit = 1
        try:
            with self._retain_store_connect() as conn:
                rows = conn.execute(
                    """
                    SELECT id, turn_json
                    FROM hindsight_retain_turns
                    WHERE session_id = ?
                      AND active = 1
                    ORDER BY id ASC
                    """,
                    (sid,),
                ).fetchall()
                suffix_size = self._retain_rewind_suffix_size(
                    [str(row[1]) for row in rows],
                    limit,
                )
                ids = [int(row[0]) for row in rows[-suffix_size:]] if suffix_size else []
                if not ids:
                    return 0
                placeholders = ",".join("?" for _ in ids)
                conn.execute(
                    f"UPDATE hindsight_retain_turns SET active = 0, rewound_at = ? WHERE id IN ({placeholders})",
                    (time.time(), *ids),
                )
                return len(ids)
        except Exception as e:
            logger.warning("Hindsight retain store rewind failed: %s", e, exc_info=True)
            return 0

    def on_session_rewind(self, session_id: str, *, turns_undone: int = 1, **kwargs) -> None:
        """Handle `/undo` without flushing buffered turns to Hindsight."""
        sid = str(session_id or self._session_id or "").strip()
        if not sid:
            return
        marked = self.mark_persisted_turns_rewound(sid, turns_undone)
        try:
            count = int(turns_undone)
        except (TypeError, ValueError):
            count = 1
        if count < 1:
            count = 1
        if sid == self._session_id:
            remove_count = self._retain_rewind_suffix_size(self._session_turns, count)
            keep = max(0, len(self._session_turns) - remove_count)
            self._session_turns = self._session_turns[:keep]
            with self._retain_flush_lock:
                self._retain_generation += 1
                self._last_flushed_turn_count = min(self._last_flushed_turn_count, keep)
                self._last_queued_flush_count = min(self._last_queued_flush_count, keep)
                self._retain_flush_pending = False
            self._turn_counter = keep
            self._turn_index = keep
        with self._prefetch_lock:
            self._prefetch_generation += 1
            self._prefetch_result = ""
            self._prefetch_snapshot = None
            self._active_prefetch_turn = None
        logger.debug(
            "Hindsight on_session_rewind: session=%s turns_undone=%s marked=%s",
            sid, count, marked,
        )

    def _lineage_session_ids(self, session_id: str, fallback_parent_session_id: str = "") -> list[str]:
        current = str(session_id or "").strip()
        if not current:
            return []
        lineage: list[str] = []
        seen: set[str] = set()
        fallback_parent = str(fallback_parent_session_id or "").strip()
        try:
            with self._retain_store_connect() as conn:
                while current and current not in seen:
                    lineage.append(current)
                    seen.add(current)
                    row = conn.execute(
                        """
                        SELECT parent_session_id
                        FROM hindsight_retain_turns
                        WHERE session_id = ?
                          AND parent_session_id != ''
                        ORDER BY id DESC
                        LIMIT 1
                        """,
                        (current,),
                    ).fetchone()
                    parent = str(row[0] if row and row[0] else "").strip()
                    if not parent and current == lineage[0]:
                        parent = fallback_parent
                    current = parent
        except Exception as e:
            logger.warning("Hindsight retain lineage lookup failed: %s", e, exc_info=True)
            return [str(session_id).strip()]
        return list(reversed(lineage))

    def _load_persisted_retain_turns(
        self,
        session_id: str,
        *,
        parent_session_id: str = "",
    ) -> tuple[list[str], list[str], str]:
        target_session_id = str(session_id or "").strip()
        turns: list[str] = []
        try:
            with self._retain_store_connect() as conn:
                # Standalone /new chats often still carry the previous chat as
                # StateDB parent_session_id, while their provider-owned turns
                # were written under the new session document. If this session
                # already owns active turns under its own document id, retain
                # that document instead of rewriting to the parent lineage.
                if target_session_id:
                    own_rows = conn.execute(
                        """
                        SELECT retain_document_id, turn_json
                        FROM hindsight_retain_turns
                        WHERE session_id = ?
                          AND active = 1
                        ORDER BY id ASC
                        """,
                        (target_session_id,),
                    ).fetchall()
                    own_docs = {
                        str(doc or "").strip()
                        for doc, _turn in own_rows
                        if str(doc or "").strip()
                    }
                    if own_docs == {target_session_id}:
                        lineage = [target_session_id]
                        for _doc, turn_json in own_rows:
                            if not turn_json:
                                continue
                            cleaned = self._sanitize_persisted_turn_json(str(turn_json))
                            if cleaned:
                                turns.append(cleaned)
                        if turns:
                            return (
                                self._collapse_adjacent_replay_representations(turns),
                                lineage,
                                target_session_id,
                            )

                retain_document_id = self._resolve_retain_document_id(
                    conn, target_session_id, parent_session_id
                )
                if retain_document_id:
                    rows = conn.execute(
                        """
                        SELECT session_id, turn_json
                        FROM hindsight_retain_turns
                        WHERE retain_document_id = ?
                          AND active = 1
                        ORDER BY id ASC
                        """,
                        (retain_document_id,),
                    ).fetchall()
                    if rows:
                        lineage: list[str] = []
                        seen: set[str] = set()
                        for sid, turn_json in rows:
                            sid = str(sid or "").strip()
                            if sid and sid not in seen:
                                lineage.append(sid)
                                seen.add(sid)
                            if turn_json:
                                cleaned = self._sanitize_persisted_turn_json(str(turn_json))
                                if cleaned:
                                    turns.append(cleaned)
                        return (
                            self._collapse_adjacent_replay_representations(turns),
                            lineage,
                            retain_document_id,
                        )
        except Exception as e:
            logger.warning("Hindsight retain document lookup failed: %s", e, exc_info=True)

        lineage = self._lineage_session_ids(target_session_id, parent_session_id)
        if not lineage:
            return [], [], ""
        try:
            with self._retain_store_connect() as conn:
                for sid in lineage:
                    rows = conn.execute(
                        """
                        SELECT turn_json
                        FROM hindsight_retain_turns
                        WHERE session_id = ?
                          AND active = 1
                        ORDER BY id ASC
                        """,
                        (sid,),
                    ).fetchall()
                    for row in rows:
                        if not row or not row[0]:
                            continue
                        cleaned = self._sanitize_persisted_turn_json(str(row[0]))
                        if cleaned:
                            turns.append(cleaned)
        except Exception as e:
            logger.warning("Hindsight retain store read failed: %s", e, exc_info=True)
            return [], lineage, ""
        return (
            self._collapse_adjacent_replay_representations(turns),
            lineage,
            target_session_id,
        )

    def _build_metadata(self, *, message_count: int, turn_index: int) -> Dict[str, str]:
        metadata: Dict[str, str] = {
            "retained_at": _utc_timestamp(),
            "message_count": str(message_count),
            "turn_index": str(turn_index),
        }
        if self._retain_source:
            metadata["source"] = self._retain_source
        if self._session_id:
            metadata["session_id"] = self._session_id
        if self._platform:
            metadata["platform"] = self._platform
        if self._user_id:
            metadata["user_id"] = self._user_id
        if self._user_name:
            metadata["user_name"] = self._user_name
        if self._chat_id:
            metadata["chat_id"] = self._chat_id
        if self._chat_name:
            metadata["chat_name"] = self._chat_name
        if self._chat_type:
            metadata["chat_type"] = self._chat_type
        if self._thread_id:
            metadata["thread_id"] = self._thread_id
        if self._agent_identity:
            metadata["agent_identity"] = self._agent_identity
        return metadata

    def _build_retain_kwargs(
        self,
        content: str,
        *,
        context: str | None = None,
        document_id: str | None = None,
        metadata: Dict[str, str] | None = None,
        tags: List[str] | None = None,
        retain_async: bool | None = None,
    ) -> Dict[str, Any]:
        kwargs: Dict[str, Any] = {
            "bank_id": self._bank_id,
            "content": content,
            "metadata": metadata or self._build_metadata(message_count=1, turn_index=self._turn_index),
        }
        if context is not None:
            kwargs["context"] = context
        if document_id:
            kwargs["document_id"] = document_id
        if retain_async is not None:
            kwargs["retain_async"] = retain_async
        merged_tags = _normalize_retain_tags(self._retain_tags)
        for tag in _normalize_retain_tags(tags):
            if tag not in merged_tags:
                merged_tags.append(tag)
        if merged_tags:
            kwargs["tags"] = merged_tags
        if self._observation_scopes:
            kwargs["observation_scopes"] = self._observation_scopes
        return kwargs

    def retain_conversation_messages(
        self,
        messages: List[Dict[str, Any]],
        *,
        session_id: str = "",
        parent_session_id: str = "",
    ) -> Dict[str, Any]:
        """Queue retain from an explicit caller-provided transcript.

        This is a compatibility helper for callers that intentionally provide a
        transcript. User-facing manual `/retain` must not call this path:
        manual `/retain` content is authoritative only when it comes from the
        provider-owned `retain_turns.sqlite3` store via
        `retain_persisted_session_lineage()`. SessionDB transcripts can contain
        LCM/compression summaries and must not be used as retained content.
        """
        if self._shutting_down.is_set():
            raise RuntimeError("Hindsight provider is shutting down")

        turns = self._build_turns_from_conversation_messages(messages)
        if not turns:
            return {"queued": False, "turn_count": 0, "message": "No conversation turns to retain."}

        target_session_id = str(session_id or self._session_id or "").strip()
        parent_id = str(parent_session_id or self._parent_session_id or "").strip()
        retain_document_id = ""
        lineage: list[str] = []
        persisted_rows: list[tuple[str, str]] = []
        if target_session_id:
            try:
                with self._retain_store_connect() as conn:
                    retain_document_id = self._resolve_retain_document_id(conn, target_session_id, parent_id)
                    if retain_document_id:
                        rows = conn.execute(
                            """
                            SELECT session_id, turn_json
                            FROM hindsight_retain_turns
                            WHERE retain_document_id = ?
                              AND active = 1
                            ORDER BY id ASC
                            """,
                            (retain_document_id,),
                        ).fetchall()
                        seen: set[str] = set()
                        for sid, turn_json in rows:
                            sid = str(sid or "").strip()
                            if sid and sid not in seen:
                                lineage.append(sid)
                                seen.add(sid)
                            if turn_json:
                                cleaned = self._sanitize_persisted_turn_json(str(turn_json))
                            if cleaned:
                                persisted_rows.append((sid, cleaned))
            except Exception as e:
                logger.warning("Hindsight transcript retain document lookup failed: %s", e, exc_info=True)

        has_lineage_transcript = any(str(msg.get("_session_id") or "").strip() for msg in messages)
        if persisted_rows and target_session_id and not has_lineage_transcript:
            transcript_turns = turns
            merged_turns: list[str] = []
            inserted_transcript = False
            for sid, turn_json in persisted_rows:
                if sid == target_session_id:
                    if not inserted_transcript:
                        merged_turns.extend(transcript_turns)
                        inserted_transcript = True
                    continue
                merged_turns.append(turn_json)
            if not inserted_transcript:
                merged_turns.extend(transcript_turns)
                if target_session_id and target_session_id not in lineage:
                    lineage.append(target_session_id)
            turns = merged_turns

        fallback_document_id = self._document_id
        if target_session_id and target_session_id != self._session_id:
            fallback_document_id = f"{target_session_id}-{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}"
        document_id, update_mode = self._resolve_full_retain_target_for_session(retain_document_id or target_session_id, fallback_document_id)
        content = "[" + ",".join(turns) + "]"
        bank_id = self._bank_id
        retain_async_flag = self._retain_async
        retain_context = self._retain_context
        num_turns = len(turns)
        submission_id = self._begin_retain_submission(
            bank_id=bank_id,
            document_id=document_id,
            update_mode=update_mode,
            content=content,
        )

        def _do_retain() -> None:
            item: Dict[str, Any] = {"content": content}
            if retain_context:
                item["context"] = retain_context
            if update_mode is not None:
                item["update_mode"] = update_mode
            logger.debug(
                "Hindsight transcript retain: bank=%s, doc=%s, mode=%s, async=%s, content_len=%d, num_turns=%d",
                bank_id, document_id, update_mode, retain_async_flag, len(content), num_turns,
            )
            try:
                self._run_hindsight_operation(
                    lambda client: client.aretain_batch(
                        bank_id=bank_id,
                        items=[item],
                        document_id=document_id,
                        retain_async=retain_async_flag,
                    )
                )
            except BaseException as exc:
                self._finish_retain_submission(submission_id, exc)
                raise
            self._finish_retain_submission(submission_id)
            logger.debug("Hindsight transcript retain succeeded")

        self._ensure_writer()
        self._register_atexit()
        self._retain_queue.put(_do_retain)
        result = {
            "queued": True,
            "turn_count": num_turns,
            "document_id": document_id,
            "update_mode": update_mode,
            "content_chars": len(content),
        }
        if lineage:
            result["lineage_session_ids"] = lineage
        return result

    @property
    def retain_on_new_enabled(self) -> bool:
        """Whether explicit /new and /reset must retain before rotating."""
        return self._retain_on_new

    @property
    def retain_on_new_timeout_seconds(self) -> float:
        """Maximum time a session reset may wait for retain acknowledgement."""
        return self._retain_on_new_timeout_seconds

    def retain_before_session_reset(
        self,
        *,
        session_id: str,
        parent_session_id: str = "",
        flush_pending: Callable[..., bool] | None = None,
    ) -> Dict[str, Any]:
        """Synchronously retain the old session before an explicit reset."""
        if not self._retain_on_new:
            return {"enabled": False, "queued": False}
        timeout = self._retain_on_new_timeout_seconds
        started = time.monotonic()
        if flush_pending is not None and not flush_pending(timeout=timeout):
            raise TimeoutError(
                f"Pending memory work did not finish within {timeout:g}s"
            )
        remaining = timeout - (time.monotonic() - started)
        if remaining <= 0:
            raise TimeoutError(
                f"Hindsight retain did not finish within {timeout:g}s"
            )
        return self.retain_persisted_session_lineage(
            session_id=session_id,
            parent_session_id=parent_session_id,
            wait=True,
            timeout=remaining,
        )

    def retain_persisted_session_lineage(
        self,
        *,
        session_id: str = "",
        parent_session_id: str = "",
        wait: bool = False,
        timeout: float | None = None,
    ) -> Dict[str, Any]:
        """Retain a session reconstructed from the provider-owned turn store.

        Manual ``/retain`` keeps the historical non-blocking behavior. Session
        rotation may pass ``wait=True`` so a failed retain can abort ``/new``
        before the old session is discarded.
        """
        if self._shutting_down.is_set():
            raise RuntimeError("Hindsight provider is shutting down")

        deadline = None
        if wait and timeout is not None:
            deadline = time.monotonic() + timeout

        def _remaining_timeout() -> float | None:
            if deadline is None:
                return None
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(
                    f"Hindsight retain did not finish within {timeout:g}s"
                )
            return remaining

        _remaining_timeout()

        target_session_id = str(session_id or self._session_id or "").strip()
        parent_id = str(parent_session_id or self._parent_session_id or "").strip()
        turns, lineage, retain_document_id = self._load_persisted_retain_turns(
            target_session_id,
            parent_session_id=parent_id,
        )
        _remaining_timeout()
        if not turns:
            return {"queued": False, "turn_count": 0, "message": "No persisted turns to retain."}

        fallback_document_id = self._document_id
        if target_session_id and target_session_id != self._session_id:
            fallback_document_id = f"{target_session_id}-{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}"
        document_id, update_mode = self._resolve_full_retain_target_for_session(
            retain_document_id or target_session_id,
            fallback_document_id,
            probe_timeout=_remaining_timeout(),
        )
        _remaining_timeout()
        content = "[" + ",".join(turns) + "]"
        bank_id = self._bank_id
        retain_async_flag = self._retain_async
        retain_context = self._retain_context
        num_turns = len(turns)
        submission_id = self._begin_retain_submission(
            bank_id=bank_id,
            document_id=document_id,
            update_mode=update_mode,
            content=content,
        )

        def _do_retain() -> None:
            item: Dict[str, Any] = {"content": content}
            if retain_context:
                item["context"] = retain_context
            if update_mode is not None:
                item["update_mode"] = update_mode
            logger.debug(
                "Hindsight persisted retain: bank=%s, doc=%s, mode=%s, async=%s, content_len=%d, num_turns=%d, lineage=%s",
                bank_id, document_id, update_mode, retain_async_flag, len(content), num_turns, lineage,
            )
            try:
                self._run_hindsight_operation(
                    lambda client: client.aretain_batch(
                        bank_id=bank_id,
                        items=[item],
                        document_id=document_id,
                        retain_async=retain_async_flag,
                    )
                )
            except BaseException as exc:
                self._finish_retain_submission(submission_id, exc)
                raise
            self._finish_retain_submission(submission_id)
            logger.debug("Hindsight persisted retain succeeded")

        completed = threading.Event() if wait else None
        failure: list[BaseException] = []

        def _run_retain() -> None:
            try:
                _do_retain()
            except BaseException as exc:
                failure.append(exc)
                raise
            finally:
                if completed is not None:
                    completed.set()

        self._ensure_writer()
        self._register_atexit()
        _remaining_timeout()
        self._retain_queue.put(_run_retain if wait else _do_retain)
        if completed is not None:
            if not completed.wait(timeout=_remaining_timeout()):
                raise TimeoutError(
                    f"Hindsight retain did not finish within {timeout:g}s"
                    if timeout is not None
                    else "Hindsight retain did not finish"
                )
            if failure:
                raise failure[0]
        return {
            "queued": True,
            "turn_count": num_turns,
            "document_id": document_id,
            "update_mode": update_mode,
            "content_chars": len(content),
            "lineage_session_ids": lineage,
        }

    def flush_retained_turns(self) -> Dict[str, Any]:
        """Flush buffered turns that have not already been queued for retain."""
        if self._shutting_down.is_set():
            raise RuntimeError("Hindsight provider is shutting down")

        with self._retain_flush_lock:
            total_turns = len(self._session_turns)
            if total_turns == 0:
                return {"queued": False, "turn_count": 0, "message": "No buffered turns to retain."}
            if total_turns <= self._last_queued_flush_count:
                return {
                    "queued": False,
                    "turn_count": 0,
                    "message": "No new buffered turns to retain.",
                }

            document_id, update_mode = self._resolve_retain_target(self._document_id)
            force_replace = self._retain_force_replace
            if force_replace and update_mode == "append":
                update_mode = "replace"
            if update_mode == "append" and self._retain_flush_pending:
                return {
                    "queued": False,
                    "turn_count": 0,
                    "message": "A retain flush is already queued.",
                }
            if update_mode == "append":
                start_index = self._last_queued_flush_count
                flush_turns = self._session_turns[start_index:total_turns]
            else:
                start_index = 0
                flush_turns = self._session_turns[:total_turns]
            if not flush_turns:
                return {
                    "queued": False,
                    "turn_count": 0,
                    "message": "No new buffered turns to retain.",
                }

            content = "[" + ",".join(flush_turns) + "]"
            lineage_tags: list[str] = []
            if self._session_id:
                lineage_tags.append(f"session:{self._session_id}")
            if self._parent_session_id:
                lineage_tags.append(f"parent:{self._parent_session_id}")

            metadata_snapshot = self._build_metadata(
                message_count=len(flush_turns) * 2,
                turn_index=self._turn_index,
            )
            num_turns = len(flush_turns)
            flush_up_to = total_turns
            retain_generation = self._retain_generation
            submission_id = self._begin_retain_submission(
                bank_id=self._bank_id,
                document_id=document_id,
                update_mode=update_mode,
                content=content,
            )
            self._last_queued_flush_count = flush_up_to
            if update_mode == "append":
                self._retain_flush_pending = True
        bank_id = self._bank_id
        retain_async_flag = self._retain_async
        retain_context = self._retain_context

        def _do_retain() -> None:
            remote_succeeded = False
            try:
                item = self._build_retain_kwargs(
                    content,
                    context=retain_context,
                    metadata=metadata_snapshot,
                    tags=lineage_tags or None,
                )
                item.pop("bank_id", None)
                item.pop("retain_async", None)
                if update_mode is not None:
                    item["update_mode"] = update_mode
                logger.debug("Hindsight retain: bank=%s, doc=%s, mode=%s, async=%s, content_len=%d, num_turns=%d",
                             bank_id, document_id, update_mode, retain_async_flag, len(content), num_turns)
                self._run_hindsight_operation(
                    lambda client: client.aretain_batch(
                        bank_id=bank_id,
                        items=[item],
                        document_id=document_id,
                        retain_async=retain_async_flag,
                    )
                )
                remote_succeeded = True
                self._finish_retain_submission(submission_id)
                with self._retain_flush_lock:
                    if self._retain_generation == retain_generation:
                        self._last_flushed_turn_count = max(self._last_flushed_turn_count, flush_up_to)
                        if force_replace:
                            self._retain_force_replace = False
                logger.debug("Hindsight retain succeeded")
            except BaseException as exc:
                if not remote_succeeded:
                    self._finish_retain_submission(submission_id, exc)
                with self._retain_flush_lock:
                    if self._retain_generation == retain_generation:
                        if self._last_queued_flush_count == flush_up_to:
                            self._last_queued_flush_count = self._last_flushed_turn_count
                raise
            finally:
                with self._retain_flush_lock:
                    if self._retain_generation == retain_generation:
                        self._retain_flush_pending = False

        try:
            self._ensure_writer()
            self._register_atexit()
            self._retain_queue.put(_do_retain)
        except BaseException as exc:
            self._finish_retain_submission(submission_id, exc)
            with self._retain_flush_lock:
                if self._retain_generation == retain_generation:
                    if self._last_queued_flush_count == flush_up_to:
                        self._last_queued_flush_count = self._last_flushed_turn_count
                    self._retain_flush_pending = False
            raise
        return {
            "queued": True,
            "document_id": document_id,
            "turn_count": num_turns,
            "content_chars": len(content),
            "flush_up_to": flush_up_to,
            "start_index": start_index,
        }

    @classmethod
    def _drop_leading_replayed_assistant_projection(
        cls,
        existing_turns: List[str],
        incoming_turns: List[str],
    ) -> List[str]:
        """Drop a timestamp-less leading projection of the persisted final assistant.

        This is deliberately bounded to a replay window that also contains a
        timestamped event after the persisted cutoff.  Same-content assistant
        events outside that source/order overlap remain distinct.
        """
        if not existing_turns or len(incoming_turns) < 2:
            return incoming_turns
        try:
            existing_payload = json.loads(existing_turns[-1])
            projection_payload = json.loads(incoming_turns[0])
        except Exception:
            return incoming_turns
        if not (
            isinstance(existing_payload, list)
            and existing_payload
            and isinstance(existing_payload[-1], dict)
            and isinstance(projection_payload, list)
            and len(projection_payload) == 1
            and isinstance(projection_payload[0], dict)
        ):
            return incoming_turns
        persisted_assistant = existing_payload[-1]
        projected_assistant = projection_payload[0]
        if not (
            persisted_assistant.get("role") == "assistant"
            and projected_assistant.get("role") == "assistant"
            and str(persisted_assistant.get("content") or "")
            == str(projected_assistant.get("content") or "")
            and str(persisted_assistant.get("timestamp") or "")
            and not str(projected_assistant.get("timestamp") or "")
        ):
            return incoming_turns

        existing_times = [
            cls._retain_timestamp_order_value(timestamp)
            for turn in existing_turns
            for _role, _content, timestamp in cls._retain_turn_replay_identity(turn)
            if timestamp
        ]
        later_times = [
            cls._retain_timestamp_order_value(timestamp)
            for turn in incoming_turns[1:]
            for _role, _content, timestamp in cls._retain_turn_replay_identity(turn)
            if timestamp
        ]
        existing_times = [value for value in existing_times if value is not None]
        later_times = [value for value in later_times if value is not None]
        if not existing_times or not later_times or min(later_times) <= max(existing_times):
            return incoming_turns
        return incoming_turns[1:]

    def _append_session_turns(self, turns: List[str]) -> tuple[int, int, int]:
        """Append transcript-derived turn JSONs that are not already buffered.

        ``messages`` passed through MemoryManager is the best available
        completed-turn transcript: it can include an earlier user message that
        was followed by tool work and then interrupted by a later user
        correction before the final assistant answer.  Persist the transcript
        prefix too, while avoiding duplicates when the next sync receives the
        same full conversation plus one new tail turn.
        """
        if not turns:
            return 0, self._turn_counter, self._turn_counter

        persisted_lineage: List[str] = []
        persisted_document_id = ""
        if self._session_id:
            persisted_turns, persisted_lineage, persisted_document_id = self._load_persisted_retain_turns(
                self._session_id,
                parent_session_id=self._parent_session_id,
            )
            if persisted_turns:
                # A restarted provider has an empty in-memory buffer but the
                # provider-owned retain store can already contain active turns
                # for the logical document.  Mirror that active persisted view
                # before comparing the full transcript replay, otherwise the
                # old turns are inserted again and manual /retain repeats them.
                self._session_turns = list(persisted_turns)
                self._turn_counter = len(self._session_turns)
                self._turn_index = self._turn_counter

        before_counter = self._turn_counter
        start_index = 0
        if self._session_turns:
            turns = self._drop_leading_replayed_assistant_projection(
                self._session_turns,
                turns,
            )
            if not turns:
                return 0, before_counter, before_counter
            existing = [self._retain_turn_canonical(turn) for turn in self._session_turns]
            incoming = [self._retain_turn_canonical(turn) for turn in turns]
            existing_occurrences = [
                self._retain_turn_source_occurrence_id(turn)
                for turn in self._session_turns
            ]
            incoming_occurrences = [
                self._retain_turn_source_occurrence_id(turn) for turn in turns
            ]
            if (
                incoming_occurrences
                and all(incoming_occurrences)
                and len(incoming_occurrences) <= len(existing_occurrences)
                and existing_occurrences[: len(incoming_occurrences)]
                == incoming_occurrences
            ):
                return 0, before_counter, before_counter
            if (
                existing_occurrences
                and all(existing_occurrences)
                and len(incoming_occurrences) > len(existing_occurrences)
                and incoming_occurrences[: len(existing_occurrences)]
                == existing_occurrences
            ):
                new_turns = turns[len(existing_occurrences) :]
                for turn in new_turns:
                    self._session_turns.append(turn)
                    self._turn_counter += 1
                    self._turn_index = self._turn_counter
                    self._persist_retain_turn(turn)
                return len(new_turns), before_counter, self._turn_counter
            existing_ids = [self._retain_turn_replay_identity(turn) for turn in self._session_turns]
            incoming_ids = [self._retain_turn_replay_identity(turn) for turn in turns]
            existing_times = [
                timestamp
                for identity in existing_ids
                for _role, _content, timestamp in identity
                if timestamp
            ]
            incoming_times = [
                timestamp
                for identity in incoming_ids
                for _role, _content, timestamp in identity
                if timestamp
            ]
            incoming_follows_existing = bool(
                existing_times
                and incoming_times
                and min(incoming_times) > max(existing_times)
            )
            canonical_prefix_overlap = (
                existing[: len(incoming)] == incoming
                if len(incoming) <= len(existing)
                else incoming[: len(existing)] == existing
            )
            identity_prefix_exact = (
                existing_ids[: len(incoming_ids)] == incoming_ids
                if len(incoming_ids) <= len(existing_ids)
                else incoming_ids[: len(existing_ids)] == existing_ids
            )
            anchored_canonical_rewrite = bool(
                canonical_prefix_overlap
                and not identity_prefix_exact
                and any(identity in set(existing_ids) for identity in incoming_ids)
            )
            if (
                len(incoming) <= len(existing)
                and existing[: len(incoming)] == incoming
                and not anchored_canonical_rewrite
            ):
                if existing_ids[: len(incoming_ids)] == incoming_ids:
                    return 0, before_counter, before_counter
                if not incoming_follows_existing:
                    return 0, before_counter, before_counter
                # The same user-visible sequence can legitimately recur later.
                # Distinct identities whose complete timestamp range follows the
                # persisted history are new turns, not replay duplication.
                start_index = 0
            elif incoming[: len(existing)] == existing and not anchored_canonical_rewrite:
                if incoming_ids[: len(existing_ids)] == existing_ids:
                    start_index = len(existing)
                elif incoming_follows_existing:
                    # The replay-shaped prefix is itself a later legitimate
                    # repetition; retain it together with any additional tail.
                    start_index = 0
                else:
                    # Rehydration can shift some timestamps while preserving the
                    # same role/content prefix. Only append the genuinely new tail.
                    start_index = len(existing)
            else:
                merged_turns = self._merge_overlapping_replayed_turns(self._session_turns, turns)
                if merged_turns is not None:
                    if merged_turns == self._session_turns:
                        return 0, before_counter, before_counter
                    # A compressed/restarted transcript can be only a tail
                    # window of the persisted document. Merge timestamped
                    # missing events into the full active history, then replace;
                    # appending the window would duplicate all of its anchors.
                    if not self._replace_active_persisted_turns(
                        merged_turns,
                        lineage_session_ids=persisted_lineage,
                        retain_document_id=persisted_document_id,
                    ):
                        return 0, before_counter, before_counter
                    self._session_turns = list(merged_turns)
                    self._turn_counter = len(merged_turns)
                    self._turn_index = self._turn_counter
                    with self._retain_flush_lock:
                        self._retain_generation += 1
                        self._last_flushed_turn_count = 0
                        self._last_queued_flush_count = 0
                        self._retain_flush_pending = False
                        self._retain_force_replace = True
                    return max(1, len(merged_turns) - len(existing)), before_counter, self._turn_counter

                existing_timestamps = [
                    self._retain_turn_replay_timestamp(turn) for turn in self._session_turns
                ]
                incoming_timestamps = [self._retain_turn_replay_timestamp(turn) for turn in turns]
                known_existing = [value for value in existing_timestamps if value]
                known_existing_events = [
                    timestamp
                    for turn_identity in existing_ids
                    for _role, _content, timestamp in turn_identity
                    if timestamp
                ]
                known_incoming = [value for value in incoming_timestamps if value]
                if known_existing and known_incoming and min(known_incoming) <= max(known_existing_events):
                    # A divergent replay prefix may still contain events whose
                    # timestamps are strictly newer than every persisted event.
                    # Preserve only that provably-new suffix; never append the
                    # overlapping representation itself without a safe anchor.
                    seen_message_ids = {
                        message_identity
                        for turn_identity in existing_ids
                        for message_identity in turn_identity
                    }
                    strictly_new_turns = self._retain_turns_strictly_after(
                        turns,
                        cutoff=max(known_existing_events),
                        seen_message_ids=seen_message_ids,
                    )
                    if not strictly_new_turns:
                        return 0, before_counter, before_counter
                    turns = strictly_new_turns
                    incoming = [self._retain_turn_canonical(turn) for turn in turns]

                max_overlap = min(len(existing), len(incoming))
                for size in range(max_overlap, 0, -1):
                    if existing[-size:] == incoming[:size]:
                        start_index = size
                        break

        new_turns = turns[start_index:]
        for turn in new_turns:
            self._session_turns.append(turn)
            self._turn_counter += 1
            self._turn_index = self._turn_counter
            self._persist_retain_turn(turn)
        return len(new_turns), before_counter, self._turn_counter

    def _snapshot_conversation_messages_to_retain(
        self,
        messages: List[Dict[str, Any]] | None,
        *,
        final_assistant_content: str = "",
    ) -> tuple[int, int, int]:
        """Persist newly retainable turns from a transcript without remote flush.

        Returns ``(added, before_counter, after_counter)``. Used by both
        completed-turn ``sync_turn`` and pre-compression snapshots so long
        tool work that is later compressed still leaves the original real
        user turn in the provider-owned ledger.
        """
        transcript_messages = [dict(message) for message in (messages or [])]
        final_assistant = self._stringify_retain_content(final_assistant_content).strip()
        if final_assistant:
            for message in reversed(transcript_messages):
                if str(message.get("role") or "").strip() != "assistant":
                    continue
                if message.get("tool_calls") or message.get("finish_reason") == "tool_calls":
                    continue
                content = self._stringify_retain_content(message.get("content")).strip()
                if content != final_assistant:
                    continue
                if message.get("_timestamp", message.get("timestamp")) in (None, ""):
                    # Only the response completing this sync is known to be new.
                    # Historical replay messages with no source timestamp stay
                    # unknown so overlap recovery cannot promote them via now().
                    message["_timestamp"] = self._retain_message_timestamp()
                break

        transcript_turns = self._build_turns_from_conversation_messages(transcript_messages)
        if not transcript_turns:
            return 0, self._turn_counter, self._turn_counter
        return self._append_session_turns(transcript_turns)

    def on_pre_compress(self, messages: List[Dict[str, Any]]) -> str:
        """Snapshot retainable turns before compression discards live context.

        Context compression can drop a real first user request that never got a
        final assistant answer yet (long tool work, recovery, then ``继续``).
        Without this snapshot the later compressed window only contains the
        continuation turn, so local retain and the Hindsight Document start at
        the wrong message.

        Writes the provider-owned local ledger. Newly snapshotted turns are not
        made remote-flush-eligible on their own: if nothing was already pending
        for automatic retain, the remote watermark advances with the snapshot so
        ``on_session_switch`` does not publish an incomplete orphan Document.
        A later completed ``sync_turn`` / replace still submits the full lineage.
        """
        if self._shutting_down.is_set():
            return ""
        try:
            before_counter = self._turn_counter
            pending_remote_before = False
            with self._retain_flush_lock:
                pending_remote_before = len(self._session_turns) > self._last_queued_flush_count
            added, _, after_counter = self._snapshot_conversation_messages_to_retain(
                messages or []
            )
            if added:
                logger.debug(
                    "on_pre_compress: buffered %d retain turn(s) before compression",
                    added,
                )
                # Local ledger already has the rows. Only suppress remote
                # eligibility for the newly snapshotted range when there was no
                # prior unflushed auto-retain tail; otherwise leave the old
                # watermark so completed-but-unflushed turns still flush.
                if not pending_remote_before:
                    with self._retain_flush_lock:
                        if self._last_queued_flush_count <= before_counter:
                            self._last_queued_flush_count = max(
                                self._last_queued_flush_count,
                                after_counter,
                            )
        except Exception as exc:
            logger.warning(
                "Hindsight on_pre_compress retain snapshot failed: %s",
                exc,
                exc_info=True,
            )
        return ""

    def sync_turn(
        self,
        user_content: str,
        assistant_content: str,
        *,
        session_id: str = "",
        messages: List[Dict[str, Any]] | None = None,
    ) -> None:
        """Enqueue a retain for the current turn. Non-blocking.

        The actual aretain_batch runs on a single long-lived writer thread
        that drains an in-memory queue. Once shutdown() has been called,
        further sync_turn() calls are dropped — this prevents post-exit
        retains from reaching aiohttp after interpreter shutdown begins.

        When MemoryManager provides the completed OpenAI-style ``messages``
        transcript, prefer it over the scalar ``user_content``/``assistant``
        pair.  The scalar pair only describes the final completed exchange;
        a gateway interrupt can place an earlier user message, tool calls, and
        a later correction into one logical completed turn.  Rebuilding the
        retain turns from ``messages`` preserves that real first user message
        while filtering tool output, summaries, interrupt notices, empty
        assistant shells, intermediate assistant drafts, compression/task-list
        rehydration markers, externalized payload placeholders, tool-budget
        exhaustion notices, and async delegation completion injections.
        """
        if self._shutting_down.is_set():
            logger.debug("sync_turn: skipped (shutting down)")
            return

        if session_id:
            self._session_id = str(session_id).strip()

        if messages is not None:
            transcript_turns = self._build_turns_from_conversation_messages(
                [dict(message) for message in (messages or [])]
            )
            if transcript_turns:
                added, before_counter, after_counter = self._snapshot_conversation_messages_to_retain(
                    messages,
                    final_assistant_content=assistant_content,
                )
                if added == 0:
                    logger.debug("sync_turn: transcript supplied no new retained turns")
                    return
                if not self._auto_retain:
                    logger.debug(
                        "sync_turn: buffered %d transcript turn(s) (auto_retain disabled)",
                        added,
                    )
                    return
                if after_counter == before_counter:
                    logger.debug("sync_turn: retaining same-length replay correction")
                    self.flush_retained_turns()
                    return
                before_bucket = before_counter // self._retain_every_n_turns
                after_bucket = after_counter // self._retain_every_n_turns
                if after_bucket == before_bucket:
                    next_turn = (before_bucket + 1) * self._retain_every_n_turns
                    logger.debug(
                        "sync_turn: buffered transcript through turn %d (will retain at turn %d)",
                        after_counter,
                        next_turn,
                    )
                    return
                logger.debug(
                    "sync_turn: retaining %d turns after transcript sync, total session content %d chars",
                    len(self._session_turns),
                    sum(len(t) for t in self._session_turns),
                )
                self.flush_retained_turns()
                return
            logger.debug("sync_turn: transcript supplied no retainable turns")
            # Fall through to the scalar pair when the transcript rebuild could
            # not produce turns (empty/noise-only window).

        clean_user = self._clean_retain_user_content(user_content)
        if clean_user:
            turn_messages = self._build_turn_messages(clean_user, assistant_content)
        elif (
            self._is_orphan_assistant_trigger_user_content(user_content)
            and not self._is_retain_noise_assistant_content(assistant_content)
        ):
            # Scalar callers lack the completed transcript but still describe a
            # user-visible response to an internal async-completion trigger.
            # Persist the response without inventing a user utterance.
            turn_messages = self._build_orphan_assistant_turn(assistant_content)
        else:
            logger.debug("sync_turn: skipped synthetic/noise user content")
            return

        turn = json.dumps(turn_messages, ensure_ascii=False)
        self._session_turns.append(turn)
        self._turn_counter += 1
        self._turn_index = self._turn_counter
        self._persist_retain_turn(turn)

        if not self._auto_retain:
            logger.debug("sync_turn: buffered turn %d (auto_retain disabled)", self._turn_counter)
            return

        if self._turn_counter % self._retain_every_n_turns != 0:
            logger.debug(
                "sync_turn: buffered turn %d (will retain at turn %d)",
                self._turn_counter,
                self._turn_counter
                + (self._retain_every_n_turns - self._turn_counter % self._retain_every_n_turns),
            )
            return

        logger.debug(
            "sync_turn: retaining %d turns, total session content %d chars",
            len(self._session_turns),
            sum(len(t) for t in self._session_turns),
        )
        self.flush_retained_turns()

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        if self._memory_mode == "context":
            return []
        return [RETAIN_SCHEMA, RECALL_SCHEMA, REFLECT_SCHEMA]

    def handle_tool_call(self, tool_name: str, args: dict, **kwargs) -> str:
        if tool_name == "hindsight_retain":
            content = args.get("content", "")
            if not content:
                return tool_error("Missing required parameter: content")
            context = args.get("context")
            try:
                item = self._build_retain_kwargs(
                    content,
                    context=context,
                    tags=args.get("tags"),
                )
                # aretain_batch takes bank_id/retain_async as call args, not item keys.
                item.pop("bank_id", None)
                item.pop("retain_async", None)
                logger.debug("Tool hindsight_retain: bank=%s, content_len=%d, context=%s",
                             self._bank_id, len(content), context)
                self._run_hindsight_operation(
                    lambda client: client.aretain_batch(bank_id=self._bank_id, items=[item])
                )
                logger.debug("Tool hindsight_retain: success")
                return json.dumps({"result": "Memory stored successfully."})
            except Exception as e:
                logger.warning("hindsight_retain failed: %s", e, exc_info=True)
                return tool_error(f"Failed to store memory: {e}")

        elif tool_name == "hindsight_recall":
            query = args.get("query", "")
            if not query:
                return tool_error("Missing required parameter: query")
            try:
                recall_kwargs: dict = {
                    "bank_id": self._bank_id, "query": query, "budget": self._budget,
                    "max_tokens": self._recall_max_tokens,
                }
                if self._recall_tags:
                    recall_kwargs["tags"] = self._recall_tags
                    recall_kwargs["tags_match"] = self._recall_tags_match
                if self._recall_types:
                    recall_kwargs["types"] = self._recall_types
                logger.debug("Tool hindsight_recall: bank=%s, query_len=%d, budget=%s",
                             self._bank_id, len(query), self._budget)
                resp = self._run_hindsight_operation(lambda client: client.arecall(**recall_kwargs))
                num_results = len(resp.results) if resp.results else 0
                logger.debug("Tool hindsight_recall: %d results", num_results)
                if not resp.results:
                    return json.dumps({"result": "No relevant memories found."})
                lines = [f"{i}. {r.text}" for i, r in enumerate(resp.results, 1)]
                return json.dumps({"result": "\n".join(lines)})
            except Exception as e:
                logger.warning("hindsight_recall failed: %s", e, exc_info=True)
                return tool_error(f"Failed to search memory: {e}")

        elif tool_name == "hindsight_reflect":
            query = args.get("query", "")
            if not query:
                return tool_error("Missing required parameter: query")
            try:
                logger.debug("Tool hindsight_reflect: bank=%s, query_len=%d, budget=%s",
                             self._bank_id, len(query), self._budget)
                resp = self._run_hindsight_operation(
                    lambda client: client.areflect(
                        bank_id=self._bank_id, query=query, budget=self._budget
                    )
                )
                logger.debug("Tool hindsight_reflect: response_len=%d", len(resp.text or ""))
                return json.dumps({"result": resp.text or "No relevant memories found."})
            except Exception as e:
                logger.warning("hindsight_reflect failed: %s", e, exc_info=True)
                return tool_error(f"Failed to reflect: {e}")

        elif tool_name == "hindsight_retain_session":
            try:
                info = self.retain_persisted_session_lineage()
                if not info.get("queued"):
                    return json.dumps({"result": info.get("message", "No persisted turns to retain.")}, ensure_ascii=False)
                return json.dumps(
                    {"result": "Buffered session turns queued for retain.", **info},
                    ensure_ascii=False,
                )
            except Exception as e:
                logger.warning("hindsight_retain_session failed: %s", e, exc_info=True)
                return tool_error(f"Failed to store session: {e}")

        return tool_error(f"Unknown tool: {tool_name}")

    def on_session_switch(
        self,
        new_session_id: str,
        *,
        parent_session_id: str = "",
        reset: bool = False,
        **kwargs,
    ) -> None:
        """Refresh cached per-session state when the agent rotates session_id.

        Fires on /resume, /branch, /reset, /new, and context compression.
        Without this hook, initialize()-cached state (``_session_id``,
        ``_document_id``, ``_session_turns``, ``_turn_counter``) would keep
        pointing at the previous session and writes would land in the wrong
        document. See hermes-agent#6672.

        Always update ``_session_id`` so metadata and tags on subsequent
        retains reflect the active session. Always mint a fresh
        ``_document_id`` so the new session's retain doesn't overwrite the
        old session's document on vectorize-io/hindsight#1303. Always clear
        the accumulated batch buffers (``_session_turns``, ``_turn_counter``,
        ``_turn_index``) — even for /resume and /branch, the new session's
        batching must start from zero so an in-flight retain doesn't flush
        under the wrong ``_document_id``.

        Before clearing, flush any buffered turns under the *old*
        ``_document_id`` only when automatic retain is enabled. Users who set
        ``retain_every_n_turns > 1`` would otherwise silently lose whatever's
        in ``_session_turns`` at the moment of switch. When ``auto_retain`` is
        false, switching sessions intentionally clears the manual-only buffer
        without writing it.

        Also invalidate the carried-recall generation and clear both recall
        representations. A timed-out or overlapping ``prefetch()`` from the old
        session can still finish later, but its generation/session guard then
        prevents it from carrying stale recall into the new session.

        ``parent_session_id`` is recorded for lineage tags on future retains.
        ``reset`` is accepted but not needed for Hindsight's state model —
        buffer clearing is correct for every session switch, not only /reset.
        """
        new_id = str(new_session_id or "").strip()
        if not new_id:
            return
        if kwargs.get("rewound"):
            self.on_session_rewind(
                new_id,
                turns_undone=kwargs.get("turns_undone", 1),
            )
            return

        # 1. Flush any buffered turns under the OLD identifiers. Snapshot
        # everything before mutating self._* so metadata + tags + doc_id
        # all reference the old session consistently.
        if self._auto_retain and len(self._session_turns) > self._last_queued_flush_count:
            old_total_turns = len(self._session_turns)
            old_start_index = self._last_queued_flush_count
            old_session_id = self._session_id
            old_parent_session_id = self._parent_session_id
            old_turn_index = self._turn_index
            old_document_id, old_update_mode = self._resolve_retain_target(self._document_id)
            if self._retain_force_replace and old_update_mode == "append":
                old_update_mode = "replace"
            if old_update_mode == "append":
                old_turns = list(self._session_turns[old_start_index:old_total_turns])
            else:
                old_turns = list(self._session_turns[:old_total_turns])
            # Do not remote-publish trailing user-only orphans on switch.
            # Pre-compress snapshots and interrupted turns stay in the local
            # ledger; a later completed sync/replace submits the full document.
            while old_turns:
                trailing_identity = self._retain_turn_replay_identity(old_turns[-1])
                if (
                    len(trailing_identity) == 1
                    and trailing_identity[0][0] == "user"
                ):
                    old_turns.pop()
                    continue
                break
            if not old_turns:
                self._last_queued_flush_count = old_total_turns
            else:
                old_metadata = self._build_metadata(
                    message_count=len(old_turns) * 2,
                    turn_index=old_turn_index,
                )
                old_lineage_tags: list[str] = []
                if old_session_id:
                    old_lineage_tags.append(f"session:{old_session_id}")
                if old_parent_session_id:
                    old_lineage_tags.append(f"parent:{old_parent_session_id}")
                old_content = "[" + ",".join(old_turns) + "]"
                submission_id: int | None = None

                def _flush():
                    try:
                        item = self._build_retain_kwargs(
                            old_content,
                            context=self._retain_context,
                            metadata=old_metadata,
                            tags=old_lineage_tags or None,
                        )
                        item.pop("bank_id", None)
                        item.pop("retain_async", None)
                        if old_update_mode is not None:
                            item["update_mode"] = old_update_mode
                        logger.debug(
                            "Hindsight flush-on-switch: bank=%s, doc=%s, mode=%s, num_turns=%d",
                            self._bank_id, old_document_id, old_update_mode, len(old_turns),
                        )
                        self._run_hindsight_operation(
                            lambda client: client.aretain_batch(
                                bank_id=self._bank_id,
                                items=[item],
                                document_id=old_document_id,
                                retain_async=self._retain_async,
                            )
                        )
                        if submission_id is not None:
                            self._finish_retain_submission(submission_id)
                    except BaseException as e:
                        if submission_id is not None:
                            self._finish_retain_submission(submission_id, e)
                        logger.warning("Hindsight flush-on-switch failed: %s", e, exc_info=True)

                # Route the flush through the same writer queue sync_turn
                # uses. That serializes it behind any still-queued retains
                # from the old session (FIFO by document_id), avoids racing
                # two threads on aretain_batch against the same document, and
                # keeps shutdown's drain semantics intact. Skip enqueue if
                # shutdown has already fired — the writer is draining/gone.
                if not self._shutting_down.is_set():
                    submission_id = self._begin_retain_submission(
                        bank_id=self._bank_id,
                        document_id=old_document_id,
                        update_mode=old_update_mode,
                        content=old_content,
                    )
                    try:
                        self._ensure_writer()
                        self._register_atexit()
                        self._retain_queue.put(_flush)
                    except BaseException as exc:
                        self._finish_retain_submission(submission_id, exc)
                        raise
                    self._last_queued_flush_count = old_total_turns

        # 2. Drop the carried recall so the new session cannot see stale memory.
        with self._prefetch_lock:
            self._prefetch_generation += 1
            self._prefetch_result = ""
            self._prefetch_snapshot = None
            self._active_prefetch_turn = None
            self._session_id = new_id

        # 3. Now rotate to the new session.
        parent_id = str(parent_session_id or "").strip()
        if parent_id:
            self._parent_session_id = parent_id
            try:
                with self._retain_store_connect() as conn:
                    self._retain_document_id = self._resolve_retain_document_id(conn, new_id, parent_id)
            except Exception:
                self._retain_document_id = parent_id
        else:
            self._retain_document_id = new_id
        start_ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        self._document_id = f"{self._session_id}-{start_ts}"
        self._session_turns = []
        with self._retain_flush_lock:
            self._retain_generation += 1
            self._last_flushed_turn_count = 0
            self._last_queued_flush_count = 0
            self._retain_flush_pending = False
            self._retain_force_replace = False
        self._turn_counter = 0
        self._turn_index = 0
        logger.debug(
            "Hindsight on_session_switch: new_session=%s parent=%s reset=%s doc=%s",
            self._session_id, self._parent_session_id, reset, self._document_id,
        )

    def shutdown(self) -> None:
        logger.debug("Hindsight shutdown: stopping writer and closing client")
        # Stop accepting new retain jobs first so anyone still calling
        # sync_turn() during teardown is dropped, not enqueued.
        self._shutting_down.set()
        # Drain the writer: it will finish in-flight work, then exit on
        # the sentinel. Bounded join keeps shutdown predictable even if
        # the daemon is wedged.
        writer = self._writer_thread
        if writer is not None and writer.is_alive():
            try:
                self._retain_queue.put(_WRITER_SENTINEL)
            except Exception:
                pass
            writer.join(timeout=10.0)
            if writer.is_alive():
                logger.warning(
                    "Hindsight writer did not stop within 10s; "
                    "abandoning %d pending retain(s)",
                    self._retain_queue.qsize(),
                )
        if self._client is not None:
            try:
                if self._mode == "local_embedded":
                    # HindsightEmbedded.close() delegates to its sync client.close().
                    # When Hermes created/used that client on the shared async loop,
                    # closing it from this thread can raise "attached to a different
                    # loop" before aiohttp releases the session. Close the embedded
                    # inner async client on the shared loop first, then let the
                    # wrapper clean up daemon/UI bookkeeping.
                    inner_client = getattr(self._client, "_client", None)
                    if inner_client is not None and hasattr(inner_client, "aclose"):
                        _run_sync(inner_client.aclose())
                        try:
                            self._client._client = None
                        except Exception:
                            pass
                    try:
                        self._client.close()
                    except RuntimeError:
                        pass
                else:
                    self._run_sync(self._client.aclose())
            except Exception:
                pass
            self._client = None
        # The module-global background event loop (_loop / _loop_thread)
        # is intentionally NOT stopped here. It is shared across every
        # HindsightMemoryProvider instance in the process — the plugin
        # loader creates a new provider per AIAgent, and the gateway
        # creates one AIAgent per concurrent chat session. Stopping the
        # loop from one provider's shutdown() strands the aiohttp
        # ClientSession + TCPConnector owned by every sibling provider
        # on a dead loop, which surfaces as the "Unclosed client session"
        # / "Unclosed connector" warnings reported in #11923. The loop
        # runs on a daemon thread and is reclaimed on process exit;
        # per-session cleanup happens via self._client.aclose() above.


def register(ctx) -> None:
    """Register Hindsight as a memory provider plugin."""
    ctx.register_memory_provider(HindsightMemoryProvider())
    ctx.register_auxiliary_task(
        "hindsight_recall_preprocessor",
        display_name="Hindsight recall preprocessor",
        description="Filter prior Hindsight recall and generate the next retrieval query",
        defaults={
            "provider": "openai-codex",
            "model": "gpt-5.6-luna",
            "timeout": 30,
            "extra_body": {"service_tier": "priority"},
        },
    )
