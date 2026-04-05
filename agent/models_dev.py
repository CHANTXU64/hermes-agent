"""Models.dev registry integration for provider-aware context length detection.

Fetches model metadata from https://models.dev/api.json — a community-maintained
database of 3800+ models across 100+ providers, including per-provider context
windows, pricing, and capabilities.

Data is cached in memory (1hr TTL) and on disk (~/.hermes/models_dev_cache.json)
to avoid cold-start network latency.
"""

import difflib
import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from utils import atomic_json_write

import requests

logger = logging.getLogger(__name__)

MODELS_DEV_URL = "https://models.dev/api.json"
_MODELS_DEV_CACHE_TTL = 3600  # 1 hour in-memory

# In-memory cache
_models_dev_cache: Dict[str, Any] = {}
_models_dev_cache_time: float = 0

# Provider ID mapping: Hermes provider names → models.dev provider IDs
PROVIDER_TO_MODELS_DEV: Dict[str, str] = {
    "openrouter": "openrouter",
    "anthropic": "anthropic",
    "zai": "zai",
    "kimi-coding": "kimi-for-coding",
    "minimax": "minimax",
    "minimax-cn": "minimax-cn",
    "deepseek": "deepseek",
    "alibaba": "alibaba",
    "copilot": "github-copilot",
    "ai-gateway": "vercel",
    "opencode-zen": "opencode",
    "opencode-go": "opencode-go",
    "kilocode": "kilo",
    "fireworks": "fireworks-ai",
}


def _get_cache_path() -> Path:
    """Return path to disk cache file."""
    env_val = os.environ.get("HERMES_HOME", "")
    hermes_home = Path(env_val) if env_val else Path.home() / ".hermes"
    return hermes_home / "models_dev_cache.json"


def _load_disk_cache() -> Dict[str, Any]:
    """Load models.dev data from disk cache."""
    try:
        cache_path = _get_cache_path()
        if cache_path.exists():
            with open(cache_path, encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        logger.debug("Failed to load models.dev disk cache: %s", e)
    return {}


def _save_disk_cache(data: Dict[str, Any]) -> None:
    """Save models.dev data to disk cache atomically."""
    try:
        cache_path = _get_cache_path()
        atomic_json_write(cache_path, data, indent=None, separators=(",", ":"))
    except Exception as e:
        logger.debug("Failed to save models.dev disk cache: %s", e)


def fetch_models_dev(force_refresh: bool = False) -> Dict[str, Any]:
    """Fetch models.dev registry. In-memory cache (1hr) + disk fallback.

    Returns the full registry dict keyed by provider ID, or empty dict on failure.
    """
    global _models_dev_cache, _models_dev_cache_time

    # Check in-memory cache
    if (
        not force_refresh
        and _models_dev_cache
        and (time.time() - _models_dev_cache_time) < _MODELS_DEV_CACHE_TTL
    ):
        return _models_dev_cache

    # Try network fetch
    try:
        response = requests.get(MODELS_DEV_URL, timeout=15)
        response.raise_for_status()
        data = response.json()
        if isinstance(data, dict) and len(data) > 0:
            _models_dev_cache = data
            _models_dev_cache_time = time.time()
            _save_disk_cache(data)
            logger.debug(
                "Fetched models.dev registry: %d providers, %d total models",
                len(data),
                sum(len(p.get("models", {})) for p in data.values() if isinstance(p, dict)),
            )
            return data
    except Exception as e:
        logger.debug("Failed to fetch models.dev: %s", e)

    # Fall back to disk cache — use a short TTL (5 min) so we retry
    # the network fetch soon instead of serving stale data for a full hour.
    if not _models_dev_cache:
        _models_dev_cache = _load_disk_cache()
        if _models_dev_cache:
            _models_dev_cache_time = time.time() - _MODELS_DEV_CACHE_TTL + 300
            logger.debug("Loaded models.dev from disk cache (%d providers)", len(_models_dev_cache))

    return _models_dev_cache


def lookup_models_dev_context(provider: str, model: str) -> Optional[int]:
    """Look up context_length for a provider+model combo in models.dev.

    Returns the context window in tokens, or None if not found.
    Handles case-insensitive matching and filters out context=0 entries.
    """
    mdev_provider_id = PROVIDER_TO_MODELS_DEV.get(provider)
    if not mdev_provider_id:
        return None

    data = fetch_models_dev()
    provider_data = data.get(mdev_provider_id)
    if not isinstance(provider_data, dict):
        return None

    models = provider_data.get("models", {})
    if not isinstance(models, dict):
        return None

    # Exact match
    entry = models.get(model)
    if entry:
        ctx = _extract_context(entry)
        if ctx:
            return ctx

    # Case-insensitive match
    model_lower = model.lower()
    for mid, mdata in models.items():
        if mid.lower() == model_lower:
            ctx = _extract_context(mdata)
            if ctx:
                return ctx

    return None


def _extract_context(entry: Dict[str, Any]) -> Optional[int]:
    """Extract context_length from a models.dev model entry.

    Returns None for invalid/zero values (some audio/image models have context=0).
    """
    if not isinstance(entry, dict):
        return None
    limit = entry.get("limit")
    if not isinstance(limit, dict):
        return None
    ctx = limit.get("context")
    if isinstance(ctx, (int, float)) and ctx > 0:
        return int(ctx)
    return None


# ---------------------------------------------------------------------------
# Model capability metadata
# ---------------------------------------------------------------------------


@dataclass
class ModelCapabilities:
    """Structured capability metadata for a model from models.dev."""

    supports_tools: bool = True
    supports_vision: bool = False
    supports_reasoning: bool = False
    context_window: int = 200000
    max_output_tokens: int = 8192
    model_family: str = ""


def _get_provider_models(provider: str) -> Optional[Dict[str, Any]]:
    """Resolve a Hermes provider ID to its models dict from models.dev.

    Returns the models dict or None if the provider is unknown or has no data.
    """
    mdev_provider_id = PROVIDER_TO_MODELS_DEV.get(provider)
    if not mdev_provider_id:
        return None

    data = fetch_models_dev()
    provider_data = data.get(mdev_provider_id)
    if not isinstance(provider_data, dict):
        return None

    models = provider_data.get("models", {})
    if not isinstance(models, dict):
        return None

    return models


def _find_model_entry(models: Dict[str, Any], model: str) -> Optional[Dict[str, Any]]:
    """Find a model entry by exact match, then case-insensitive fallback."""
    # Exact match
    entry = models.get(model)
    if isinstance(entry, dict):
        return entry

    # Case-insensitive match
    model_lower = model.lower()
    for mid, mdata in models.items():
        if mid.lower() == model_lower and isinstance(mdata, dict):
            return mdata

    return None


def get_model_capabilities(provider: str, model: str) -> Optional[ModelCapabilities]:
    """Look up full capability metadata from models.dev cache.

    Uses the existing fetch_models_dev() and PROVIDER_TO_MODELS_DEV mapping.
    Returns None if model not found.

    Extracts from model entry fields:
      - reasoning  (bool)  → supports_reasoning
      - tool_call  (bool)  → supports_tools
      - attachment (bool)  → supports_vision
      - limit.context (int) → context_window
      - limit.output  (int) → max_output_tokens
      - family     (str)   → model_family
    """
    models = _get_provider_models(provider)
    if models is None:
        return None

    entry = _find_model_entry(models, model)
    if entry is None:
        return None

    # Extract capability flags (default to False if missing)
    supports_tools = bool(entry.get("tool_call", False))
    supports_vision = bool(entry.get("attachment", False))
    supports_reasoning = bool(entry.get("reasoning", False))

    # Extract limits
    limit = entry.get("limit", {})
    if not isinstance(limit, dict):
        limit = {}

    ctx = limit.get("context")
    context_window = int(ctx) if isinstance(ctx, (int, float)) and ctx > 0 else 200000

    out = limit.get("output")
    max_output_tokens = int(out) if isinstance(out, (int, float)) and out > 0 else 8192

    model_family = entry.get("family", "") or ""

    return ModelCapabilities(
        supports_tools=supports_tools,
        supports_vision=supports_vision,
        supports_reasoning=supports_reasoning,
        context_window=context_window,
        max_output_tokens=max_output_tokens,
        model_family=model_family,
    )


def list_provider_models(provider: str) -> List[str]:
    """Return all model IDs for a provider from models.dev.

    Returns an empty list if the provider is unknown or has no data.
    """
    models = _get_provider_models(provider)
    if models is None:
        return []
    return list(models.keys())


def search_models_dev(
    query: str, provider: str = None, limit: int = 5
) -> List[Dict[str, Any]]:
    """Fuzzy search across models.dev catalog. Returns matching model entries.

    Args:
        query: Search string to match against model IDs.
        provider: Optional Hermes provider ID to restrict search scope.
                  If None, searches across all providers in PROVIDER_TO_MODELS_DEV.
        limit: Maximum number of results to return.

    Returns:
        List of dicts, each containing 'provider', 'model_id', and the full
        model 'entry' from models.dev.
    """
    data = fetch_models_dev()
    if not data:
        return []

    # Build list of (provider_id, model_id, entry) candidates
    candidates: List[tuple] = []

    if provider is not None:
        # Search only the specified provider
        mdev_provider_id = PROVIDER_TO_MODELS_DEV.get(provider)
        if not mdev_provider_id:
            return []
        provider_data = data.get(mdev_provider_id, {})
        if isinstance(provider_data, dict):
            models = provider_data.get("models", {})
            if isinstance(models, dict):
                for mid, mdata in models.items():
                    candidates.append((provider, mid, mdata))
    else:
        # Search across all mapped providers
        for hermes_prov, mdev_prov in PROVIDER_TO_MODELS_DEV.items():
            provider_data = data.get(mdev_prov, {})
            if isinstance(provider_data, dict):
                models = provider_data.get("models", {})
                if isinstance(models, dict):
                    for mid, mdata in models.items():
                        candidates.append((hermes_prov, mid, mdata))

    if not candidates:
        return []

    # Use difflib for fuzzy matching — case-insensitive comparison
    model_ids_lower = [c[1].lower() for c in candidates]
    query_lower = query.lower()

    # First try exact substring matches (more intuitive than pure edit-distance)
    substring_matches = []
    for prov, mid, mdata in candidates:
        if query_lower in mid.lower():
            substring_matches.append({"provider": prov, "model_id": mid, "entry": mdata})

    # Then add difflib fuzzy matches for any remaining slots
    fuzzy_ids = difflib.get_close_matches(
        query_lower, model_ids_lower, n=limit * 2, cutoff=0.4
    )

    seen_ids: set = set()
    results: List[Dict[str, Any]] = []

    # Prioritize substring matches
    for match in substring_matches:
        key = (match["provider"], match["model_id"])
        if key not in seen_ids:
            seen_ids.add(key)
            results.append(match)
            if len(results) >= limit:
                return results

    # Add fuzzy matches
    for fid in fuzzy_ids:
        # Find original-case candidates matching this lowered ID
        for prov, mid, mdata in candidates:
            if mid.lower() == fid:
                key = (prov, mid)
                if key not in seen_ids:
                    seen_ids.add(key)
                    results.append({"provider": prov, "model_id": mid, "entry": mdata})
                    if len(results) >= limit:
                        return results

    return results
