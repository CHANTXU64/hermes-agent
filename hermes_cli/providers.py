"""
Single source of truth for provider identity in Hermes Agent.

Every provider known to the system is defined here exactly once.  Other
modules (auth, models, inference) import from this file rather than
maintaining their own parallel registries.

Concepts
--------
- **ProviderDef** -- immutable description of one inference provider.
- **PROVIDERS** -- canonical ``{id: ProviderDef}`` mapping.
- **ALIASES** -- maps human-friendly or legacy names to a canonical id.
- **LABELS** -- short display names for UI / CLI menus.
- **transport** -- wire protocol the provider speaks
  (``openai_chat``, ``anthropic_messages``, ``codex_responses``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


# -- Provider definition ------------------------------------------------------

@dataclass(frozen=True)
class ProviderDef:
    """Immutable description of a known inference provider."""

    id: str
    name: str
    transport: str          # 'openai_chat' | 'anthropic_messages' | 'codex_responses'
    api_key_env_vars: tuple  # env vars to check, in priority order
    base_url: str = ""
    base_url_env_var: str = ""
    is_aggregator: bool = False
    auth_type: str = "api_key"  # api_key | oauth_device_code | oauth_external | external_process


# -- Canonical provider table -------------------------------------------------

PROVIDERS: dict[str, ProviderDef] = {
    "openrouter": ProviderDef(
        id="openrouter",
        name="OpenRouter",
        transport="openai_chat",
        api_key_env_vars=("OPENROUTER_API_KEY", "OPENAI_API_KEY"),
        base_url="https://openrouter.ai/api/v1",
        base_url_env_var="OPENROUTER_BASE_URL",
        is_aggregator=True,
    ),
    "nous": ProviderDef(
        id="nous",
        name="Nous Portal",
        transport="openai_chat",
        api_key_env_vars=(),
        base_url="https://inference-api.nousresearch.com/v1",
        auth_type="oauth_device_code",
    ),
    "openai-codex": ProviderDef(
        id="openai-codex",
        name="OpenAI Codex",
        transport="codex_responses",
        api_key_env_vars=(),
        base_url="https://chatgpt.com/backend-api/codex",
        auth_type="oauth_external",
    ),
    "copilot": ProviderDef(
        id="copilot",
        name="GitHub Copilot",
        transport="openai_chat",
        api_key_env_vars=("COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"),
        base_url="https://api.githubcopilot.com",
    ),
    "copilot-acp": ProviderDef(
        id="copilot-acp",
        name="GitHub Copilot ACP",
        transport="codex_responses",
        api_key_env_vars=(),
        base_url="acp://copilot",
        base_url_env_var="COPILOT_ACP_BASE_URL",
        auth_type="external_process",
    ),
    "zai": ProviderDef(
        id="zai",
        name="Z.AI / GLM",
        transport="openai_chat",
        api_key_env_vars=("GLM_API_KEY", "ZAI_API_KEY", "Z_AI_API_KEY"),
        base_url="https://api.z.ai/api/paas/v4",
        base_url_env_var="GLM_BASE_URL",
    ),
    "kimi-coding": ProviderDef(
        id="kimi-coding",
        name="Kimi / Moonshot",
        transport="openai_chat",
        api_key_env_vars=("KIMI_API_KEY",),
        base_url="https://api.moonshot.ai/v1",
        base_url_env_var="KIMI_BASE_URL",
    ),
    "minimax": ProviderDef(
        id="minimax",
        name="MiniMax",
        transport="openai_chat",
        api_key_env_vars=("MINIMAX_API_KEY",),
        base_url="https://api.minimax.io/anthropic",
        base_url_env_var="MINIMAX_BASE_URL",
    ),
    "minimax-cn": ProviderDef(
        id="minimax-cn",
        name="MiniMax (China)",
        transport="openai_chat",
        api_key_env_vars=("MINIMAX_CN_API_KEY",),
        base_url="https://api.minimaxi.com/anthropic",
        base_url_env_var="MINIMAX_CN_BASE_URL",
    ),
    "anthropic": ProviderDef(
        id="anthropic",
        name="Anthropic",
        transport="anthropic_messages",
        api_key_env_vars=("ANTHROPIC_API_KEY", "ANTHROPIC_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN"),
        base_url="https://api.anthropic.com",
    ),
    "deepseek": ProviderDef(
        id="deepseek",
        name="DeepSeek",
        transport="openai_chat",
        api_key_env_vars=("DEEPSEEK_API_KEY",),
        base_url="https://api.deepseek.com/v1",
        base_url_env_var="DEEPSEEK_BASE_URL",
    ),
    "ai-gateway": ProviderDef(
        id="ai-gateway",
        name="AI Gateway",
        transport="openai_chat",
        api_key_env_vars=("AI_GATEWAY_API_KEY",),
        base_url="https://ai-gateway.vercel.sh/v1",
        base_url_env_var="AI_GATEWAY_BASE_URL",
        is_aggregator=True,
    ),
    "opencode-zen": ProviderDef(
        id="opencode-zen",
        name="OpenCode Zen",
        transport="openai_chat",
        api_key_env_vars=("OPENCODE_ZEN_API_KEY",),
        base_url="https://opencode.ai/zen/v1",
        base_url_env_var="OPENCODE_ZEN_BASE_URL",
        is_aggregator=True,
    ),
    "opencode-go": ProviderDef(
        id="opencode-go",
        name="OpenCode Go",
        transport="openai_chat",
        api_key_env_vars=("OPENCODE_GO_API_KEY",),
        base_url="https://opencode.ai/zen/go/v1",
        base_url_env_var="OPENCODE_GO_BASE_URL",
        is_aggregator=True,
    ),
    "kilocode": ProviderDef(
        id="kilocode",
        name="Kilo Code",
        transport="openai_chat",
        api_key_env_vars=("KILOCODE_API_KEY",),
        base_url="https://api.kilo.ai/api/gateway",
        base_url_env_var="KILOCODE_BASE_URL",
        is_aggregator=True,
    ),
    "alibaba": ProviderDef(
        id="alibaba",
        name="Alibaba Cloud (DashScope)",
        transport="openai_chat",
        api_key_env_vars=("DASHSCOPE_API_KEY",),
        base_url="https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
        base_url_env_var="DASHSCOPE_BASE_URL",
    ),
    "huggingface": ProviderDef(
        id="huggingface",
        name="Hugging Face",
        transport="openai_chat",
        api_key_env_vars=("HF_TOKEN",),
        base_url="https://router.huggingface.co/v1",
        base_url_env_var="HF_BASE_URL",
        is_aggregator=True,
    ),
}


# -- Aliases ------------------------------------------------------------------
# Merged from auth.py _PROVIDER_ALIASES and models.py _PROVIDER_ALIASES.
# Every human-friendly / legacy name maps to exactly one canonical id.

ALIASES: dict[str, str] = {
    # openrouter
    "openai": "openrouter",

    # zai
    "glm": "zai",
    "z-ai": "zai",
    "z.ai": "zai",
    "zhipu": "zai",

    # kimi-coding
    "kimi": "kimi-coding",
    "moonshot": "kimi-coding",

    # minimax-cn
    "minimax-china": "minimax-cn",
    "minimax_cn": "minimax-cn",

    # anthropic
    "claude": "anthropic",
    "claude-code": "anthropic",

    # copilot
    "github": "copilot",
    "github-copilot": "copilot",
    "github-models": "copilot",
    "github-model": "copilot",

    # copilot-acp
    "github-copilot-acp": "copilot-acp",
    "copilot-acp-agent": "copilot-acp",

    # ai-gateway
    "aigateway": "ai-gateway",
    "vercel": "ai-gateway",
    "vercel-ai-gateway": "ai-gateway",

    # opencode-zen
    "opencode": "opencode-zen",
    "zen": "opencode-zen",

    # opencode-go
    "go": "opencode-go",
    "opencode-go-sub": "opencode-go",

    # kilocode
    "kilo": "kilocode",
    "kilo-code": "kilocode",
    "kilo-gateway": "kilocode",

    # deepseek
    "deep-seek": "deepseek",

    # alibaba
    "dashscope": "alibaba",
    "aliyun": "alibaba",
    "qwen": "alibaba",
    "alibaba-cloud": "alibaba",

    # huggingface
    "hf": "huggingface",
    "hugging-face": "huggingface",
    "huggingface-hub": "huggingface",

    # Local server aliases -- route to the virtual "custom" provider
    "lmstudio": "custom",
    "lm-studio": "custom",
    "lm_studio": "custom",
    "ollama": "custom",
    "vllm": "custom",
    "llamacpp": "custom",
    "llama.cpp": "custom",
    "llama-cpp": "custom",
}


# -- Display labels -----------------------------------------------------------
# Short human-readable names for CLI menus and status output.

LABELS: dict[str, str] = {pid: pdef.name for pid, pdef in PROVIDERS.items()}
# Virtual "custom" provider is not in PROVIDERS but needs a label.
LABELS["custom"] = "Custom endpoint"


# -- Transport -> API mode mapping --------------------------------------------

TRANSPORT_TO_API_MODE: dict[str, str] = {
    "openai_chat": "chat_completions",
    "anthropic_messages": "anthropic_messages",
    "codex_responses": "codex_responses",
}


# -- Helper functions ---------------------------------------------------------

def normalize_provider(name: str) -> str:
    """Resolve aliases and normalise casing to a canonical provider id.

    Returns the canonical id string.  Does *not* validate that the id
    corresponds to a known provider -- callers should check ``PROVIDERS``
    if they need that guarantee.
    """
    key = name.strip().lower()
    return ALIASES.get(key, key)


def get_provider(name: str) -> Optional[ProviderDef]:
    """Look up a provider by id or alias.

    Returns the ``ProviderDef`` or ``None`` if the provider is unknown.
    """
    canonical = normalize_provider(name)
    return PROVIDERS.get(canonical)


def is_aggregator(provider: str) -> bool:
    """Return True when the provider is a multi-model aggregator.

    Aggregators (OpenRouter, AI Gateway, etc.) expose models from many
    upstream providers through a single API key and endpoint.
    """
    pdef = get_provider(provider)
    return pdef.is_aggregator if pdef else False


def determine_api_mode(provider: str, base_url: str = "") -> str:
    """Determine the API mode (wire protocol) for a provider/endpoint.

    Resolution order:

    1. Known provider -> transport -> ``TRANSPORT_TO_API_MODE``.
    2. URL heuristics for unknown / custom providers.
    3. Default: ``'chat_completions'``.
    """
    # 1. Known provider lookup
    pdef = get_provider(provider)
    if pdef is not None:
        return TRANSPORT_TO_API_MODE.get(pdef.transport, "chat_completions")

    # 2. URL-based heuristics for custom / unknown providers
    if base_url:
        url_lower = base_url.rstrip("/").lower()
        if url_lower.endswith("/anthropic") or "api.anthropic.com" in url_lower:
            return "anthropic_messages"
        if "api.openai.com" in url_lower:
            return "codex_responses"

    # 3. Default
    return "chat_completions"
