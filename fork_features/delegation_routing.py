"""Fork-owned per-invocation delegation route policy.

The host delegation tool keeps input normalization, credential resolution,
child construction, execution, persistence, and completion delivery.  This
module owns only the fork contract that turns per-call provider/model/reasoning
fields into fully resolved child routes before any child is constructed.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional


logger = logging.getLogger(__name__)

_DELEGATION_FREQUENT_MODELS_MAX = 10
_DELEGATION_SIMILAR_MODELS_MAX = 10
_DELEGATION_SIMILARITY_MIN = 0.55
_DELEGATION_SUGGESTION_MARKDOWN_CHAR_CAP = 1800

INHERIT_REASONING = object()
CredentialResolver = Callable[[dict, Any], dict]
ToolErrorFactory = Callable[..., str]


@dataclass(frozen=True)
class ResolvedDelegationRoute:
    """One validated internal route ready for child construction.

    ``credentials`` may contain runtime secrets and must never be persisted or
    rendered.  Public observability is derived later from the constructed child
    by :func:`child_route_metadata`.
    """

    credentials: dict
    reasoning_config: Any


class DelegationTaskRouteError(ValueError):
    """Attach a failing task index without changing the underlying route error."""

    def __init__(self, cause: ValueError, *, task_index: int):
        super().__init__(str(cause))
        self.cause = cause
        self.task_index = task_index



class DelegationRouteError(ValueError):
    """A safe, model-visible routing error with bounded retry metadata."""

    def __init__(self, message: str, *, error_code: str, **details: Any):
        super().__init__(message)
        self.error_code = error_code
        self.details = details


def _recent_delegation_route_usage(*, days: int = 30) -> List[Dict[str, Any]]:
    """Read recent per-route usage from this profile's state DB, fail-open.

    This is a local, read-only analytics lookup. It never refreshes provider
    catalogs or invokes a model. Newer databases use ``session_model_usage``;
    legacy databases fall back to the aggregate route stored on ``sessions``.
    """
    import sqlite3

    from hermes_constants import get_hermes_home

    db_path = get_hermes_home() / "state.db"
    if not db_path.is_file():
        return []
    cutoff = time.time() - max(1, int(days)) * 86400
    uri = f"file:{db_path.resolve()}?mode=ro"
    conn = None
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=1.0)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                """
                SELECT
                    COALESCE(NULLIF(TRIM(u.billing_provider), ''),
                             NULLIF(TRIM(s.billing_provider), ''), '') AS provider,
                    TRIM(u.model) AS model,
                    SUM(COALESCE(u.api_call_count, 0)) AS api_calls,
                    COUNT(DISTINCT u.session_id) AS sessions,
                    SUM(COALESCE(u.input_tokens, 0)
                        + COALESCE(u.output_tokens, 0)
                        + COALESCE(u.cache_read_tokens, 0)
                        + COALESCE(u.cache_write_tokens, 0)
                        + COALESCE(u.reasoning_tokens, 0)) AS total_tokens,
                    MAX(s.started_at) AS last_used_at
                FROM session_model_usage u
                JOIN sessions s ON s.id = u.session_id
                WHERE s.started_at >= ?
                GROUP BY 1, 2
                """,
                (cutoff,),
            ).fetchall()
        except sqlite3.OperationalError:
            rows = conn.execute(
                """
                SELECT
                    COALESCE(NULLIF(TRIM(billing_provider), ''), '') AS provider,
                    TRIM(model) AS model,
                    SUM(COALESCE(api_call_count, 0)) AS api_calls,
                    COUNT(*) AS sessions,
                    SUM(COALESCE(input_tokens, 0)
                        + COALESCE(output_tokens, 0)
                        + COALESCE(cache_read_tokens, 0)
                        + COALESCE(cache_write_tokens, 0)) AS total_tokens,
                    MAX(started_at) AS last_used_at
                FROM sessions
                WHERE started_at >= ?
                GROUP BY 1, 2
                """,
                (cutoff,),
            ).fetchall()
    except Exception as exc:
        logger.debug("Could not read recent delegation route usage: %s", exc)
        return []
    finally:
        if conn is not None:
            conn.close()

    usage = [
        dict(row)
        for row in rows
        if str(row["provider"] or "").strip() and str(row["model"] or "").strip()
    ]
    usage.sort(
        key=lambda row: (
            -int(row.get("api_calls") or 0),
            -int(row.get("sessions") or 0),
            -int(row.get("total_tokens") or 0),
            -float(row.get("last_used_at") or 0),
            str(row.get("provider") or "").casefold(),
            str(row.get("model") or "").casefold(),
        )
    )
    return usage


def _available_catalog_routes(
    payload: dict, *, provider: Optional[str] = None
) -> List[Dict[str, str]]:
    """Return stable, deduplicated provider/model routes from picker inventory."""
    provider_key = str(provider or "").strip().casefold()
    routes: List[Dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for row in payload.get("providers") or []:
        if not isinstance(row, dict):
            continue
        slug = str(row.get("slug") or "").strip()
        if not slug or (provider_key and slug.casefold() != provider_key):
            continue
        for raw_model in row.get("models") or []:
            model = str(raw_model or "").strip()
            key = (slug.casefold(), model.casefold())
            if not model or key in seen:
                continue
            seen.add(key)
            routes.append({"provider": slug, "model": model})
    return routes


def _model_name_similarity(requested_model: str, candidate_model: str) -> float:
    from difflib import SequenceMatcher

    requested = str(requested_model or "").strip().casefold()
    candidate = str(candidate_model or "").strip().casefold()
    if not requested or not candidate:
        return 0.0
    return SequenceMatcher(None, requested, candidate).ratio()


def _route_suggestion_groups(
    payload: dict, *, requested_model: str, provider: Optional[str] = None
) -> tuple[List[Dict[str, str]], List[Dict[str, str]]]:
    """Return available frequent and similar routes without stale history."""
    available = _available_catalog_routes(payload, provider=provider)
    available_by_key = {
        (route["provider"].casefold(), route["model"].casefold()): route
        for route in available
    }

    frequent: List[Dict[str, str]] = []
    used_keys: set[tuple[str, str]] = set()
    for row in _recent_delegation_route_usage():
        key = (
            str(row.get("provider") or "").strip().casefold(),
            str(row.get("model") or "").strip().casefold(),
        )
        route = available_by_key.get(key)
        if route is None or key in used_keys:
            continue
        frequent.append(route)
        used_keys.add(key)
        if len(frequent) >= _DELEGATION_FREQUENT_MODELS_MAX:
            break

    ranked_similar = sorted(
        available,
        key=lambda route: (
            -_model_name_similarity(requested_model, route["model"]),
            route["provider"].casefold(),
            route["model"].casefold(),
        ),
    )
    similar: List[Dict[str, str]] = []
    for route in ranked_similar:
        key = (route["provider"].casefold(), route["model"].casefold())
        if key in used_keys:
            continue
        if _model_name_similarity(requested_model, route["model"]) < _DELEGATION_SIMILARITY_MIN:
            continue
        similar.append(route)
        used_keys.add(key)
        if len(similar) >= _DELEGATION_SIMILAR_MODELS_MAX:
            break
    return frequent, similar


def _markdown_route_cell(value: Any) -> str:
    return str(value or "").replace("\r", " ").replace("\n", " ").replace("|", "\\|")


def _render_route_markdown_sections(
    sections: List[tuple[str, List[Dict[str, str]]]],
) -> str:
    """Render route sections under one bounded Markdown budget."""
    bounded = [(title, list(routes)) for title, routes in sections if routes]
    if not bounded:
        return ""

    def render(current: List[tuple[str, List[Dict[str, str]]]]) -> str:
        blocks = []
        for title, routes in current:
            rows = [f"**{title}**", "| Provider | Model |", "|---|---|"]
            rows.extend(
                f"| {_markdown_route_cell(route['provider'])} | "
                f"{_markdown_route_cell(route['model'])} |"
                for route in routes
            )
            blocks.append("\n".join(rows))
        return "\n\n".join(blocks)

    text = render(bounded)
    while len(text) > _DELEGATION_SUGGESTION_MARKDOWN_CHAR_CAP:
        removable = [item for item in bounded if len(item[1]) > 1]
        if not removable:
            if len(bounded) > 1:
                bounded.pop()
                text = render(bounded)
                continue
            return ""
        max(removable, key=lambda item: len(item[1]))[1].pop()
        text = render(bounded)
    return text


def _route_suggestion_markdown(
    payload: dict, *, requested_model: str, provider: Optional[str] = None
) -> str:
    """Render bounded current route suggestions as compact Markdown tables."""
    frequent, similar = _route_suggestion_groups(
        payload, requested_model=requested_model, provider=provider
    )
    sections: List[tuple[str, List[Dict[str, str]]]] = []
    if frequent:
        sections.append(("Frequently used available models", frequent))
    if similar:
        sections.append(("Similar available models", similar))
    return _render_route_markdown_sections(sections)


def _provider_failure_candidate_details(
    *, provider: Optional[str], requested_model: str
) -> Dict[str, Any]:
    """Return bounded authenticated routes that can repair a provider failure."""
    from hermes_cli.inventory import build_models_payload, load_picker_context

    try:
        payload = build_models_payload(
            load_picker_context(),
            explicit_only=True,
            include_unconfigured=False,
            probe_custom_providers=False,
            probe_current_custom_provider=True,
        )
    except Exception as exc:
        logger.debug("Could not build delegation provider alternatives: %s", exc)
        return {}

    suggestions = _route_suggestion_markdown(
        payload, requested_model=requested_model, provider=provider
    )
    if not suggestions:
        suggestions = _route_suggestion_markdown(
            payload, requested_model=requested_model
        )
    return {"suggestion_markdown": suggestions} if suggestions else {}


def _infer_delegation_provider_for_model(model: str) -> str:
    """Return the one authenticated provider whose curated catalog has model.

    The provider picker inventory is the authority here: unlike a raw
    ``/models`` response, it is credential-aware and excludes non-agent model
    families. Model IDs are compared case-insensitively but otherwise exactly;
    provider-qualified IDs such as ``deepseek/deepseek-v4`` therefore keep
    their routing meaning.
    """
    from hermes_cli.inventory import build_models_payload, load_picker_context

    requested = str(model or "").strip()
    if not requested:
        raise ValueError(
            "The delegate_task 'model' field is blank. Omit it to inherit the "
            "normal delegation route, or provide a model shown by `hermes model`."
        )

    try:
        payload = build_models_payload(
            load_picker_context(),
            explicit_only=True,
            include_unconfigured=False,
            probe_custom_providers=False,
            probe_current_custom_provider=True,
        )
    except Exception as exc:
        raise ValueError(
            f"Cannot inspect configured providers for model '{requested}': {exc}. "
            "Run `hermes auth list` to verify credentials and `hermes model "
            "--refresh` to refresh the model picker, then retry."
        ) from exc

    requested_key = requested.casefold()
    matches: list[str] = []
    for row in payload.get("providers") or []:
        if not isinstance(row, dict):
            continue
        slug = str(row.get("slug") or "").strip()
        if not slug:
            continue
        model_keys = {
            str(candidate).strip().casefold()
            for candidate in (row.get("models") or [])
            if str(candidate).strip()
        }
        if requested_key in model_keys and slug not in matches:
            matches.append(slug)

    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        matches.sort(key=str.casefold)
        choices = ", ".join(matches)
        all_candidates = [
            {"provider": candidate_provider, "model": requested}
            for candidate_provider in matches
        ]
        route_markdown = _render_route_markdown_sections(
            [("Available routes", all_candidates)]
        )
        raise DelegationRouteError(
            f"Model '{requested}' is available from multiple configured "
            f"providers: {choices}. Retry delegate_task with both 'provider' "
            "and 'model' so Hermes uses the intended route.\n\n"
            f"{route_markdown}",
            error_code="ambiguous_model",
        )
    suggestion_markdown = _route_suggestion_markdown(
        payload, requested_model=requested
    )
    if suggestion_markdown:
        message = (
            f"Model '{requested}' was not found under any configured provider "
            "with usable credentials. Choose an available route and retry with "
            "both 'provider' and 'model'.\n\n"
            f"{suggestion_markdown}"
        )
    else:
        message = (
            f"Model '{requested}' was not found under any configured provider "
            "with usable credentials. Run `hermes model --refresh` to refresh and "
            "inspect available models, and `hermes auth list` to verify provider "
            "credentials."
        )
    raise DelegationRouteError(
        message,
        error_code="model_not_found",
    )


def _validate_explicit_provider_model_catalog(provider: str, model: str) -> None:
    """Reject known provider/model mismatches without blocking unknown catalogs."""
    from hermes_cli.inventory import build_models_payload, load_picker_context

    try:
        payload = build_models_payload(
            load_picker_context(),
            explicit_only=True,
            include_unconfigured=False,
            probe_custom_providers=False,
            probe_current_custom_provider=True,
        )
    except Exception as exc:
        logger.debug("Could not validate explicit delegation model catalog: %s", exc)
        return

    provider_key = str(provider or "").strip().casefold()
    requested = str(model or "").strip()
    requested_keys = {requested.casefold()}
    try:
        from hermes_cli.model_normalize import normalize_model_for_provider

        normalized = normalize_model_for_provider(requested, provider)
        if normalized:
            requested_keys.add(str(normalized).strip().casefold())
    except Exception:
        pass
    if provider_key in {"opencode-go", "opencode-zen"} and "/" in requested:
        requested_keys.add(requested.rsplit("/", 1)[-1].casefold())

    for row in payload.get("providers") or []:
        if not isinstance(row, dict):
            continue
        if str(row.get("slug") or "").strip().casefold() != provider_key:
            continue
        catalog_models = [
            str(candidate).strip()
            for candidate in (row.get("models") or [])
            if str(candidate).strip()
        ]
        catalog = {candidate.casefold() for candidate in catalog_models}
        if not catalog or requested_keys.intersection(catalog):
            return
        suggestion_markdown = _route_suggestion_markdown(
            payload, requested_model=requested, provider=provider
        )
        if suggestion_markdown:
            guidance = (
                "Choose an available model from this provider and retry.\n\n"
                f"{suggestion_markdown}"
            )
        else:
            guidance = (
                f"Run `hermes model --refresh` to inspect that provider's current "
                f"model catalog and `hermes auth list {provider}` to verify its credential."
            )
        raise DelegationRouteError(
            f"Model '{requested}' is not available under configured provider "
            f"'{provider}'. {guidance}",
            error_code="model_not_on_provider",
        )


def _extract_reasoning_wire_effort(value: Any) -> Optional[str]:
    """Find a provider-emitted reasoning level in nested request additions."""
    if isinstance(value, dict):
        direct = value.get("reasoning_effort")
        if isinstance(direct, str) and direct.strip():
            return direct.strip().lower()
        if "effort" in value:
            effort = value.get("effort")
            if isinstance(effort, str) and effort.strip():
                return effort.strip().lower()
        for nested in value.values():
            found = _extract_reasoning_wire_effort(nested)
            if found:
                return found
    elif isinstance(value, (list, tuple)):
        for nested in value:
            found = _extract_reasoning_wire_effort(nested)
            if found:
                return found
    return None


def _contains_explicit_reasoning_disable(value: Any) -> bool:
    """Return True only when a provider payload carries a real disable marker."""
    if isinstance(value, dict):
        if value.get("enabled") is False or value.get("includeThoughts") is False:
            return True
        if str(value.get("type") or "").strip().lower() == "disabled":
            return True
        return any(_contains_explicit_reasoning_disable(v) for v in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_explicit_reasoning_disable(v) for v in value)
    return False


def _exact_reasoning_efforts_for_route(
    *, provider: str, model: str, api_mode: str, base_url: str
) -> list[str]:
    """Probe existing request builders for levels they preserve exactly.

    This is intentionally provider/request-builder based, not a model-name
    guess. A level is advertised only when the same production path would put
    that exact level on the wire. Values that are clamped, reduced to a binary
    thinking toggle, or omitted are excluded.
    """
    from hermes_constants import VALID_REASONING_EFFORTS, parse_reasoning_effort

    candidates = ("none", *VALID_REASONING_EFFORTS)
    exact: list[str] = []
    normalized_mode = str(api_mode or "chat_completions").strip().lower()

    if normalized_mode == "codex_responses":
        from agent.transports.codex import ResponsesApiTransport

        provider_key = str(provider or "").strip().lower()
        is_xai = provider_key in {"xai", "xai-oauth"} or "api.x.ai" in str(
            base_url or ""
        ).lower()
        for candidate in candidates:
            config = parse_reasoning_effort(candidate)
            try:
                kwargs = ResponsesApiTransport().build_kwargs(
                    model=model,
                    instructions="",
                    messages=[{"role": "user", "content": "capability probe"}],
                    tools=None,
                    reasoning_config=config,
                    provider=provider,
                    base_url=base_url,
                    is_xai_responses=is_xai,
                    replay_encrypted_reasoning=False,
                )
            except Exception:
                continue
            emitted = _extract_reasoning_wire_effort(kwargs.get("reasoning"))
            if candidate != "none" and emitted == candidate:
                exact.append(candidate)
        return exact

    if normalized_mode == "anthropic_messages":
        from agent.anthropic_adapter import build_anthropic_kwargs

        for candidate in candidates:
            config = parse_reasoning_effort(candidate)
            try:
                kwargs = build_anthropic_kwargs(
                    model=model,
                    messages=[{"role": "user", "content": "capability probe"}],
                    tools=None,
                    max_tokens=1024,
                    reasoning_config=config,
                    base_url=base_url,
                )
            except Exception:
                continue
            emitted_payload = {
                "thinking": kwargs.get("thinking"),
                "output_config": kwargs.get("output_config"),
            }
            if candidate == "none":
                if _contains_explicit_reasoning_disable(emitted_payload):
                    exact.append(candidate)
                continue
            if _extract_reasoning_wire_effort(emitted_payload) == candidate:
                exact.append(candidate)
        return exact

    if normalized_mode != "chat_completions":
        return exact

    try:
        from providers import get_provider_profile

        profile = get_provider_profile(provider)
    except Exception:
        profile = None
    if profile is None:
        return exact

    for candidate in candidates:
        config = parse_reasoning_effort(candidate)
        try:
            extra_body, top_level = profile.build_api_kwargs_extras(
                reasoning_config=config,
                model=model,
                base_url=base_url,
                supports_reasoning=True,
            )
            profile_body = profile.build_extra_body(
                reasoning_config=config,
                model=model,
                base_url=base_url,
            )
        except Exception:
            continue
        emitted_payload = {
            "extra_body": extra_body,
            "top_level": top_level,
            "profile_body": profile_body,
        }
        if candidate == "none":
            if _contains_explicit_reasoning_disable(emitted_payload):
                exact.append(candidate)
            continue
        if _extract_reasoning_wire_effort(emitted_payload) == candidate:
            exact.append(candidate)
    return exact


def _explicit_reasoning_effort_is_exact(
    effort: str, *, creds: dict, parent_agent
) -> bool:
    """Return whether the selected route emits the requested level unchanged."""
    provider = str(
        creds.get("provider") or getattr(parent_agent, "provider", "") or ""
    ).strip()
    model = str(
        creds.get("model") or getattr(parent_agent, "model", "") or ""
    ).strip()
    api_mode = str(
        creds.get("api_mode") or getattr(parent_agent, "api_mode", "") or ""
    ).strip()
    base_url = str(
        creds.get("base_url") or getattr(parent_agent, "base_url", "") or ""
    ).strip()
    exact = _exact_reasoning_efforts_for_route(
        provider=provider,
        model=model,
        api_mode=api_mode,
        base_url=base_url,
    )
    return effort in exact


def _resolve_target_model_reasoning_config(
    *, creds: dict, requested_model: Optional[str], parent_agent
) -> Any:
    """Resolve the normal configured reasoning for a routed target model."""
    try:
        from hermes_cli.config import load_config_readonly
        from hermes_constants import resolve_reasoning_config

        full_cfg = load_config_readonly() or {}
        target_model = str(
            creds.get("model")
            or requested_model
            or getattr(parent_agent, "model", "")
            or ""
        )
        return resolve_reasoning_config(full_cfg, target_model)
    except Exception as exc:
        logger.debug("Could not resolve target-model reasoning config: %s", exc)
        return None


def resolve_delegation_invocation_route(
    *,
    cfg: dict,
    parent_agent,
    model: Any = None,
    provider: Any = None,
    reasoning_effort: Any = None,
    credential_resolver: CredentialResolver,
) -> ResolvedDelegationRoute:
    """Resolve one invocation/task route and its reasoning contract."""
    requested_model = str(model or "").strip() or None
    requested_provider = str(provider or "").strip() or None
    if model is not None and requested_model is None:
        raise ValueError(
            "The delegate_task 'model' field is blank. Omit it to inherit the "
            "normal delegation route, or provide a model shown by `hermes model`."
        )
    if provider is not None and requested_provider is None:
        raise ValueError(
            "The delegate_task 'provider' field is blank. Omit it to let Hermes "
            "infer the provider from 'model', or use a provider shown by "
            "`hermes auth list`."
        )

    route_cfg = cfg
    if requested_model is not None or requested_provider is not None:
        route_cfg = dict(cfg)
        for stale_key in ("base_url", "api_key", "api_mode"):
            route_cfg.pop(stale_key, None)
        if requested_model is not None:
            route_cfg["model"] = requested_model
        elif requested_provider is not None:
            route_cfg.pop("model", None)
        if requested_provider is not None:
            route_cfg["provider"] = requested_provider
        else:
            route_cfg["provider"] = _infer_delegation_provider_for_model(
                requested_model or ""
            )

    try:
        creds = credential_resolver(route_cfg, parent_agent)
    except ValueError as exc:
        failed_provider = str(route_cfg.get("provider") or "").strip() or None
        raise DelegationRouteError(
            str(exc),
            error_code="provider_unavailable",
            **_provider_failure_candidate_details(
                provider=failed_provider,
                requested_model=requested_model or "",
            ),
        ) from exc
    if requested_provider is not None and requested_model is not None:
        _validate_explicit_provider_model_catalog(
            str(creds.get("provider") or requested_provider), requested_model
        )
    reasoning_override: Any = INHERIT_REASONING
    if reasoning_effort is not None:
        from hermes_constants import VALID_REASONING_EFFORTS, parse_reasoning_effort

        raw_effort = str(reasoning_effort).strip().lower()
        allowed_efforts = ("none", *VALID_REASONING_EFFORTS)
        if raw_effort not in allowed_efforts:
            raise DelegationRouteError(
                "The delegate_task 'reasoning_effort' field must be one of "
                f"{', '.join(allowed_efforts)}; got {reasoning_effort!r}.",
                error_code="invalid_reasoning_effort",
                supported_reasoning_efforts=list(allowed_efforts),
            )
        reasoning_is_exact = _explicit_reasoning_effort_is_exact(
            raw_effort, creds=creds, parent_agent=parent_agent
        )
        if not reasoning_is_exact:
            reasoning_override = _resolve_target_model_reasoning_config(
                creds=creds,
                requested_model=requested_model,
                parent_agent=parent_agent,
            )
        else:
            reasoning_override = parse_reasoning_effort(raw_effort)
    elif requested_model is not None or requested_provider is not None:
        # A routed child must resolve per-model/global reasoning against its
        # target model. Do not copy a parent-model override across providers.
        reasoning_override = _resolve_target_model_reasoning_config(
            creds=creds,
            requested_model=requested_model,
            parent_agent=parent_agent,
        )
    return ResolvedDelegationRoute(credentials=creds, reasoning_config=reasoning_override)


def resolve_delegation_task_routes(
    *,
    cfg: dict,
    parent_agent: Any,
    tasks: List[Dict[str, Any]],
    provider: Any = None,
    model: Any = None,
    reasoning_effort: Any = None,
    credential_resolver: CredentialResolver,
) -> List[ResolvedDelegationRoute]:
    """Resolve every task route before child construction.

    Top-level fields are defaults and task fields override them.  Repeated
    route requests are resolved once per invocation.  Any failure aborts the
    complete fan-out with the original task index attached.
    """
    resolved: List[ResolvedDelegationRoute] = []
    cache: Dict[
        tuple[Optional[str], Optional[str], Optional[str]],
        ResolvedDelegationRoute,
    ] = {}
    for task_index, task in enumerate(tasks):
        task_provider = task["provider"] if "provider" in task else provider
        task_model = task["model"] if "model" in task else model
        task_effort = (
            task["reasoning_effort"]
            if "reasoning_effort" in task
            else reasoning_effort
        )
        route_key = (
            None if task_provider is None else str(task_provider),
            None if task_model is None else str(task_model),
            None if task_effort is None else str(task_effort),
        )
        cached = cache.get(route_key)
        if cached is not None:
            resolved.append(cached)
            continue
        try:
            route = resolve_delegation_invocation_route(
                cfg=cfg,
                parent_agent=parent_agent,
                provider=task_provider,
                model=task_model,
                reasoning_effort=task_effort,
                credential_resolver=credential_resolver,
            )
        except ValueError as exc:
            raise DelegationTaskRouteError(exc, task_index=task_index) from exc
        cache[route_key] = route
        resolved.append(route)
    return resolved



def delegation_route_tool_error(
    failure: DelegationTaskRouteError, *, tool_error_factory: ToolErrorFactory
) -> str:
    """Serialize only bounded, explicitly safe route metadata for the tool."""
    exc = failure.cause
    message = f"Task {failure.task_index} routing invalid: {exc}"
    if not isinstance(exc, DelegationRouteError):
        return tool_error_factory(message)
    metadata = {"error_code": exc.error_code, **exc.details}
    suggestion_markdown = str(metadata.pop("suggestion_markdown", "") or "").strip()
    if suggestion_markdown:
        message = f"{message}\n\n{suggestion_markdown}"
    metadata["task_index"] = failure.task_index
    return tool_error_factory(message, **metadata)



def child_route_metadata(child: Any) -> Dict[str, Any]:
    """Return safe effective route metadata for results and async records."""
    metadata: Dict[str, Any] = {}
    provider = getattr(child, "provider", None)
    model = getattr(child, "model", None)
    reasoning_config = getattr(child, "reasoning_config", None)
    if isinstance(provider, str) and provider:
        metadata["provider"] = provider
    if isinstance(model, str) and model:
        metadata["model"] = model
    if isinstance(reasoning_config, dict):
        if reasoning_config.get("enabled") is False:
            metadata["reasoning_effort"] = "none"
        else:
            effort = reasoning_config.get("effort")
            if isinstance(effort, str) and effort:
                metadata["reasoning_effort"] = effort
    return metadata


__all__ = [
    "DelegationRouteError",
    "DelegationTaskRouteError",
    "INHERIT_REASONING",
    "ResolvedDelegationRoute",
    "child_route_metadata",
    "delegation_route_tool_error",
    "resolve_delegation_invocation_route",
    "resolve_delegation_task_routes",
]
