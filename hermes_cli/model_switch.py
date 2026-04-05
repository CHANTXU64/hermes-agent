"""Shared model-switching logic for CLI and gateway /model commands.

Both the CLI (cli.py) and gateway (gateway/run.py) /model handlers
share the same core pipeline:

  alias resolution -> parse_model_input -> aggregator slug fixup ->
  detect_provider -> credential resolution -> normalize model name ->
  capability lookup -> build result

This module ties together the foundation layers:

- ``hermes_cli.providers``        -- canonical provider identity
- ``hermes_cli.model_normalize``  -- per-provider name formatting
- ``agent.models_dev``            -- models.dev catalog & capabilities

Callers handle all platform-specific concerns: state mutation, config
persistence, output formatting.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import List, NamedTuple, Optional

from hermes_cli.providers import (
    ALIASES,
    LABELS,
    PROVIDERS,
    determine_api_mode,
    get_provider,
    is_aggregator,
    normalize_provider,
)
from hermes_cli.model_normalize import (
    detect_vendor,
    normalize_model_for_provider,
)
from agent.models_dev import (
    ModelCapabilities,
    get_model_capabilities,
    list_provider_models,
    search_models_dev,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Model aliases -- short names -> (vendor, family) with NO version numbers.
# Resolved dynamically against the live models.dev catalog.
# ---------------------------------------------------------------------------

class ModelIdentity(NamedTuple):
    """Vendor slug and family prefix used for catalog resolution."""
    vendor: str
    family: str


MODEL_ALIASES: dict[str, ModelIdentity] = {
    # Anthropic
    "sonnet":    ModelIdentity("anthropic", "claude-sonnet"),
    "opus":      ModelIdentity("anthropic", "claude-opus"),
    "haiku":     ModelIdentity("anthropic", "claude-haiku"),
    "claude":    ModelIdentity("anthropic", "claude"),

    # OpenAI
    "gpt5":      ModelIdentity("openai", "gpt-5"),
    "gpt":       ModelIdentity("openai", "gpt"),
    "codex":     ModelIdentity("openai", "codex"),
    "o3":        ModelIdentity("openai", "o3"),
    "o4":        ModelIdentity("openai", "o4"),

    # Google
    "gemini":    ModelIdentity("google", "gemini"),

    # DeepSeek
    "deepseek":  ModelIdentity("deepseek", "deepseek-chat"),

    # X.AI
    "grok":      ModelIdentity("x-ai", "grok"),

    # Meta
    "llama":     ModelIdentity("meta-llama", "llama"),

    # Qwen / Alibaba
    "qwen":      ModelIdentity("qwen", "qwen"),

    # MiniMax
    "minimax":   ModelIdentity("minimax", "minimax"),

    # Nvidia
    "nemotron":  ModelIdentity("nvidia", "nemotron"),

    # Moonshot / Kimi
    "kimi":      ModelIdentity("moonshotai", "kimi"),

    # Z.AI / GLM
    "glm":       ModelIdentity("z-ai", "glm"),

    # StepFun
    "step":      ModelIdentity("stepfun", "step"),

    # Xiaomi
    "mimo":      ModelIdentity("xiaomi", "mimo"),

    # Arcee
    "trinity":   ModelIdentity("arcee-ai", "trinity"),
}


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------

@dataclass
class ModelSwitchResult:
    """Result of a model switch attempt."""

    success: bool
    new_model: str = ""
    target_provider: str = ""
    provider_changed: bool = False
    api_key: str = ""
    base_url: str = ""
    api_mode: str = ""
    error_message: str = ""
    warning_message: str = ""
    provider_label: str = ""
    resolved_via_alias: str = ""
    capabilities: Optional[ModelCapabilities] = None
    is_global: bool = False


@dataclass
class CustomAutoResult:
    """Result of switching to bare 'custom' provider with auto-detect."""

    success: bool
    model: str = ""
    base_url: str = ""
    api_key: str = ""
    error_message: str = ""


# ---------------------------------------------------------------------------
# Known provider names (for parse_model_input colon-splitting)
# ---------------------------------------------------------------------------

_KNOWN_PROVIDER_NAMES: set[str] = (
    set(PROVIDERS.keys())
    | set(ALIASES.keys())
    | {"custom"}
)


# ---------------------------------------------------------------------------
# Alias resolution
# ---------------------------------------------------------------------------

def resolve_alias(
    raw_input: str,
    current_provider: str,
) -> Optional[tuple[str, str, str]]:
    """Resolve a short alias against the current provider's catalog.

    Looks up *raw_input* in :data:`MODEL_ALIASES`, then searches the
    current provider's models.dev catalog for the first model whose ID
    starts with ``vendor/family`` (or just ``family`` for non-aggregator
    providers).

    Args:
        raw_input: The user's raw model input (e.g. ``"sonnet"``).
        current_provider: The currently active Hermes provider id.

    Returns:
        ``(provider, resolved_model_id, alias_name)`` if a match is
        found on the current provider, or ``None`` if the alias doesn't
        exist or no matching model is available.
    """
    key = raw_input.strip().lower()
    identity = MODEL_ALIASES.get(key)
    if identity is None:
        return None

    vendor, family = identity

    # Search the provider's catalog from models.dev
    catalog = list_provider_models(current_provider)
    if not catalog:
        return None

    # For aggregators, models are vendor/model-name format
    aggregator = is_aggregator(current_provider)

    for model_id in catalog:
        mid_lower = model_id.lower()
        if aggregator:
            # Match vendor/family prefix -- e.g. "anthropic/claude-sonnet"
            prefix = f"{vendor}/{family}".lower()
            if mid_lower.startswith(prefix):
                return (current_provider, model_id, key)
        else:
            # Non-aggregator: bare names -- e.g. "claude-sonnet-4-6"
            # Match family prefix (dots/hyphens may vary)
            family_lower = family.lower()
            if mid_lower.startswith(family_lower):
                return (current_provider, model_id, key)

    return None


def _resolve_alias_fallback(
    raw_input: str,
    fallback_providers: tuple[str, ...] = ("openrouter", "nous"),
) -> Optional[tuple[str, str, str]]:
    """Try to resolve an alias on fallback providers.

    Called when the alias exists in MODEL_ALIASES but no matching model
    was found on the current provider.

    Returns:
        ``(provider, resolved_model_id, alias_name)`` or ``None``.
    """
    for provider in fallback_providers:
        result = resolve_alias(raw_input, provider)
        if result is not None:
            return result
    return None


# ---------------------------------------------------------------------------
# parse_model_input -- shared with models.py but using our provider set
# ---------------------------------------------------------------------------

def parse_model_input(raw: str, current_provider: str) -> tuple[str, str]:
    """Parse ``/model`` input into ``(provider, model)``.

    Supports ``provider:model`` syntax to switch providers at runtime::

        openrouter:anthropic/claude-sonnet-4.5  ->  ("openrouter", "anthropic/claude-sonnet-4.5")
        nous:hermes-3                           ->  ("nous", "hermes-3")
        anthropic/claude-sonnet-4.5             ->  (current_provider, "anthropic/claude-sonnet-4.5")
        gpt-5.4                                 ->  (current_provider, "gpt-5.4")

    The colon is only treated as a provider delimiter if the left side
    is a recognized provider name or alias.
    """
    stripped = raw.strip()
    colon = stripped.find(":")
    if colon > 0:
        provider_part = stripped[:colon].strip().lower()
        model_part = stripped[colon + 1:].strip()
        if provider_part and model_part and provider_part in _KNOWN_PROVIDER_NAMES:
            # Support custom:name:model triple syntax
            if provider_part == "custom" and ":" in model_part:
                second_colon = model_part.find(":")
                custom_name = model_part[:second_colon].strip()
                actual_model = model_part[second_colon + 1:].strip()
                if custom_name and actual_model:
                    return (f"custom:{custom_name}", actual_model)
            return (normalize_provider(provider_part), model_part)
    return (current_provider, stripped)


# ---------------------------------------------------------------------------
# Core model-switching pipeline
# ---------------------------------------------------------------------------

def switch_model(
    raw_input: str,
    current_provider: str,
    current_model: str,
    current_base_url: str = "",
    current_api_key: str = "",
    is_global: bool = False,
) -> ModelSwitchResult:
    """Core model-switching pipeline shared between CLI and gateway.

    Resolution chain:
      a. Try alias resolution on current provider
      b. If alias exists but not on current provider -> try fallback
         providers (openrouter, nous)
      c. If on aggregator and input has vendor:model where vendor is NOT
         a known hermes provider -> convert to vendor/model slug
      d. Standard parse_model_input()
      e. If on aggregator, try to resolve within aggregator catalog first
      f. Fall back to detect_provider_for_model()
      g. Resolve credentials via resolve_runtime_provider()
      h. Normalize model name for target provider
      i. Get capabilities from get_model_capabilities()
      j. Build result

    Args:
        raw_input: The user's model input (e.g. "sonnet", "claude-sonnet-4",
            "zai:glm-5", "custom:local:qwen").
        current_provider: The currently active provider.
        current_model: The currently active model name.
        current_base_url: The currently active base URL.
        current_api_key: The currently active API key.
        is_global: Whether this switch should be persisted globally.

    Returns:
        ModelSwitchResult with all information the caller needs.
    """
    from hermes_cli.models import (
        detect_provider_for_model,
        validate_requested_model,
        opencode_model_api_mode,
    )
    from hermes_cli.runtime_provider import resolve_runtime_provider

    resolved_alias = ""

    # --- Step a: Try alias resolution on current provider ---
    alias_result = resolve_alias(raw_input, current_provider)

    if alias_result is not None:
        target_provider, new_model, resolved_alias = alias_result
        logger.debug(
            "Alias '%s' resolved to %s on %s",
            resolved_alias, new_model, target_provider,
        )
    else:
        # --- Step b: Alias exists but not on current provider -> fallback ---
        key = raw_input.strip().lower()
        if key in MODEL_ALIASES:
            fallback_result = _resolve_alias_fallback(raw_input)
            if fallback_result is not None:
                target_provider, new_model, resolved_alias = fallback_result
                logger.debug(
                    "Alias '%s' resolved via fallback to %s on %s",
                    resolved_alias, new_model, target_provider,
                )
            else:
                # Alias exists but no model found anywhere
                identity = MODEL_ALIASES[key]
                return ModelSwitchResult(
                    success=False,
                    is_global=is_global,
                    error_message=(
                        f"Alias '{key}' maps to {identity.vendor}/{identity.family} "
                        f"but no matching model was found in any provider catalog. "
                        f"Try specifying the full model name."
                    ),
                )
        else:
            # --- Step c: aggregator vendor:model fixup ---
            # If on an aggregator and the input looks like vendor:model
            # where vendor is NOT a known Hermes provider, treat the colon
            # as a vendor/model separator instead of provider:model.
            target_provider = current_provider
            new_model = raw_input.strip()

            colon_pos = raw_input.find(":")
            if colon_pos > 0 and is_aggregator(current_provider):
                left = raw_input[:colon_pos].strip().lower()
                right = raw_input[colon_pos + 1:].strip()
                if left and right and left not in _KNOWN_PROVIDER_NAMES:
                    # Convert vendor:model -> vendor/model for aggregator
                    new_model = f"{left}/{right}"
                    target_provider = current_provider
                    logger.debug(
                        "Converted vendor:model '%s' to aggregator slug '%s'",
                        raw_input, new_model,
                    )

            # --- Step d: Standard parse_model_input() ---
            if new_model == raw_input.strip():
                target_provider, new_model = parse_model_input(
                    raw_input, current_provider,
                )

            # --- Step d2: If parsed model is a known alias, resolve it
            #     on the TARGET provider.  This handles "anthropic:sonnet"
            #     -> resolve "sonnet" alias on anthropic provider.
            model_as_alias = new_model.strip().lower()
            if model_as_alias in MODEL_ALIASES and target_provider != current_provider:
                alias_on_target = resolve_alias(new_model, target_provider)
                if alias_on_target is not None:
                    _, new_model, resolved_alias = alias_on_target
                    logger.debug(
                        "Resolved alias '%s' on target provider %s -> %s",
                        model_as_alias, target_provider, new_model,
                    )

    # --- Step e: If on aggregator, try to resolve within aggregator catalog ---
    if is_aggregator(target_provider) and not resolved_alias:
        catalog = list_provider_models(target_provider)
        if catalog:
            new_model_lower = new_model.lower()
            # Exact match
            for mid in catalog:
                if mid.lower() == new_model_lower:
                    new_model = mid
                    break
            else:
                # Try matching bare name against catalog entries
                for mid in catalog:
                    if "/" in mid:
                        _, bare = mid.split("/", 1)
                        if bare.lower() == new_model_lower:
                            new_model = mid
                            break

    # --- Step f: Fall back to detect_provider_for_model() ---
    # Only auto-detect when no explicit provider was given and we're not
    # on a custom endpoint.
    _base = current_base_url or ""
    is_custom = current_provider == "custom" or (
        "localhost" in _base or "127.0.0.1" in _base
    )

    if (
        target_provider == current_provider
        and not is_custom
        and not resolved_alias
    ):
        detected = detect_provider_for_model(new_model, current_provider)
        if detected:
            target_provider, new_model = detected

    provider_changed = target_provider != current_provider

    # --- Step g: Resolve credentials ---
    api_key = current_api_key
    base_url = current_base_url
    api_mode = ""
    provider_label = LABELS.get(target_provider, target_provider)

    if provider_changed:
        try:
            runtime = resolve_runtime_provider(requested=target_provider)
            api_key = runtime.get("api_key", "")
            base_url = runtime.get("base_url", "")
            api_mode = runtime.get("api_mode", "")
        except Exception as e:
            if target_provider == "custom":
                return ModelSwitchResult(
                    success=False,
                    target_provider=target_provider,
                    provider_label=provider_label,
                    is_global=is_global,
                    error_message=(
                        "No custom endpoint configured. Set model.base_url "
                        "in config.yaml, or set OPENAI_BASE_URL in .env, "
                        "or run: hermes setup -> Custom OpenAI-compatible endpoint"
                    ),
                )
            return ModelSwitchResult(
                success=False,
                target_provider=target_provider,
                provider_label=provider_label,
                is_global=is_global,
                error_message=(
                    f"Could not resolve credentials for provider "
                    f"'{provider_label}': {e}"
                ),
            )
    else:
        # Re-resolve for current provider to get accurate base_url
        try:
            runtime = resolve_runtime_provider(requested=current_provider)
            api_key = runtime.get("api_key", "")
            base_url = runtime.get("base_url", "")
            api_mode = runtime.get("api_mode", "")
        except Exception:
            pass

    # --- Step h: Normalize model name for target provider ---
    new_model = normalize_model_for_provider(new_model, target_provider)

    # --- Validate ---
    try:
        validation = validate_requested_model(
            new_model,
            target_provider,
            api_key=api_key,
            base_url=base_url,
        )
    except Exception:
        validation = {
            "accepted": True,
            "persist": True,
            "recognized": False,
            "message": None,
        }

    if not validation.get("accepted"):
        msg = validation.get("message", "Invalid model")
        return ModelSwitchResult(
            success=False,
            new_model=new_model,
            target_provider=target_provider,
            provider_label=provider_label,
            is_global=is_global,
            error_message=msg,
        )

    # --- OpenCode api_mode override ---
    if target_provider in {"opencode-zen", "opencode-go"}:
        api_mode = opencode_model_api_mode(target_provider, new_model)

    # --- Determine api_mode if not already set ---
    if not api_mode:
        api_mode = determine_api_mode(target_provider, base_url)

    # --- Step i: Get capabilities from models.dev ---
    capabilities = get_model_capabilities(target_provider, new_model)

    # --- Step j: Build result ---
    return ModelSwitchResult(
        success=True,
        new_model=new_model,
        target_provider=target_provider,
        provider_changed=provider_changed,
        api_key=api_key,
        base_url=base_url,
        api_mode=api_mode,
        warning_message=validation.get("message") or "",
        provider_label=provider_label,
        resolved_via_alias=resolved_alias,
        capabilities=capabilities,
        is_global=is_global,
    )


# ---------------------------------------------------------------------------
# Fuzzy suggestions
# ---------------------------------------------------------------------------

def suggest_models(raw_input: str, limit: int = 3) -> List[str]:
    """Return fuzzy model suggestions for a (possibly misspelled) input.

    Searches the models.dev catalog across all providers and returns up
    to *limit* model IDs that best match *raw_input*.

    Args:
        raw_input: The user's raw model input.
        limit: Maximum number of suggestions.

    Returns:
        List of model ID strings (may be empty).
    """
    query = raw_input.strip()
    if not query:
        return []

    results = search_models_dev(query, limit=limit)
    suggestions: list[str] = []
    for r in results:
        mid = r.get("model_id", "")
        prov = r.get("provider", "")
        if mid:
            # For aggregator providers, model_id already has vendor/ prefix
            if prov and is_aggregator(prov):
                suggestions.append(mid)
            elif prov:
                suggestions.append(f"{prov}:{mid}")
            else:
                suggestions.append(mid)

    return suggestions[:limit]


# ---------------------------------------------------------------------------
# Custom provider switch
# ---------------------------------------------------------------------------

def switch_to_custom_provider() -> CustomAutoResult:
    """Handle bare '/model custom' -- resolve endpoint and auto-detect model.

    Returns a result object; the caller handles persistence and output.
    """
    from hermes_cli.runtime_provider import (
        resolve_runtime_provider,
        _auto_detect_local_model,
    )

    try:
        runtime = resolve_runtime_provider(requested="custom")
    except Exception as e:
        return CustomAutoResult(
            success=False,
            error_message=f"Could not resolve custom endpoint: {e}",
        )

    cust_base = runtime.get("base_url", "")
    cust_key = runtime.get("api_key", "")

    if not cust_base or "openrouter.ai" in cust_base:
        return CustomAutoResult(
            success=False,
            error_message=(
                "No custom endpoint configured. "
                "Set model.base_url in config.yaml, or set OPENAI_BASE_URL "
                "in .env, or run: hermes setup -> Custom OpenAI-compatible endpoint"
            ),
        )

    detected_model = _auto_detect_local_model(cust_base)
    if not detected_model:
        return CustomAutoResult(
            success=False,
            base_url=cust_base,
            api_key=cust_key,
            error_message=(
                f"Custom endpoint at {cust_base} is reachable but no single "
                f"model was auto-detected. Specify the model explicitly: "
                f"/model custom:<model-name>"
            ),
        )

    return CustomAutoResult(
        success=True,
        model=detected_model,
        base_url=cust_base,
        api_key=cust_key,
    )
