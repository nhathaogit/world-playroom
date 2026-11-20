"""Provider-specific settings. Credentials never belong in public API responses."""
import os
import math
import httpx
from threading import Lock


def request_timeout():
    """Bound slow generation separately from connection/upload/pool failures.

    HTTPX read is an inactivity timeout, not an end-to-end scene deadline.
    Never disable timeouts or enable hidden retries to mask a slow provider.
    """
    try:
        seconds = float(os.getenv('LLM_READ_TIMEOUT_SECONDS', '180'))
    except ValueError as error:
        raise ValueError('LLM_READ_TIMEOUT_SECONDS must be a finite number from 15 to 600') from error
    if not math.isfinite(seconds) or not 15 <= seconds <= 600:
        raise ValueError('LLM_READ_TIMEOUT_SECONDS must be a finite number from 15 to 600')
    return httpx.Timeout(seconds, connect=15.0, write=30.0, pool=15.0)

DEFAULT_ENDPOINTS = {
    "apiz": "https://api.apiz.vn/v1",
    "gemini": "https://generativelanguage.googleapis.com/v1beta/openai/",
}
GEMINI_REASONING_EFFORTS = {
    'gemini-3.1-flash-lite': 'high',
    'gemini-3.5-flash-lite': 'high',
    'gemini-3.8-flash': 'medium',
    'gemini-4-argon': 'low',
}
# Selection IDs include the provider; the API receives only the upstream ID.
APIZ_GEMINI_MODELS = {
    'apiz:gemini-3.8-flash': {'model': 'gemini-3.8-flash', 'reasoning_effort': 'medium'},
    'apiz:gemini-3.1-pro-preview': {'model': 'gemini-3.1-pro-preview', 'reasoning_effort': 'high'},
    'apiz:gemini-3.1-pro': {'model': 'gemini-3.1-pro', 'reasoning_effort': 'high'},
    'apiz:gemini-4-argon': {'model': 'gemini-4-argon', 'reasoning_effort': 'low'},
    'apiz:gemini-4-hydrogen': {'model': 'gemini-4-hydrogen', 'reasoning_effort': 'medium'},
    'apiz:gemini-4-carbon': {'model': 'gemini-4-carbon', 'reasoning_effort': 'high'},
}


def provider_name():
    provider = os.getenv("LLM_PROVIDER", "apiz").strip().lower()
    # Migrate the legacy provider name, never its credential.
    return "apiz" if provider == "shopaikey" else provider


def provider_for_model(model):
    """A model has one explicit destination, never an error-driven fallback."""
    if model in APIZ_GEMINI_MODELS:
        return 'apiz'
    if model in GEMINI_REASONING_EFFORTS:
        return 'gemini'
    if model.startswith(('grok-', 'gpt-', 'claude-', 'deepseek-')):
        return 'apiz'
    raise ValueError('Unsupported model route')


def upstream_model(model):
    provider_for_model(model)  # Reject unknown namespaced routes, never strip blindly.
    return APIZ_GEMINI_MODELS[model]['model'] if model in APIZ_GEMINI_MODELS else model


def api_key(provider=None):
    provider = provider or provider_name()
    if provider == "apiz":
        key = os.getenv("APIZ_API_KEY", "").strip()
        # Never send a legacy Google key to a third-party gateway.
        return "" if key.startswith(("AQ.", "AIza")) or key == "YOUR_API_KEY" else key
    if provider == "gemini":
        # An explicitly blank new key must not resurrect a legacy credential.
        key = os.environ.get("GEMINI_API_KEY")
        key = key.strip() if key is not None else os.getenv("gemini_api_key", "").strip()
        return "" if key == "YOUR_API_KEY" else key
    return ""


def endpoint(provider=None):
    explicit_route = provider is not None
    provider = provider or provider_name()
    if provider not in DEFAULT_ENDPOINTS:
        raise ValueError("Unsupported LLM_PROVIDER")
    configured = os.getenv("LLM_BASE_URL", "").strip().rstrip("/")
    expected = DEFAULT_ENDPOINTS[provider].rstrip("/")
    if not explicit_route and configured and configured != expected:
        raise ValueError("LLM_BASE_URL does not match the selected provider")
    return DEFAULT_ENDPOINTS[provider]


def request_options(model):
    if model in APIZ_GEMINI_MODELS:
        return {'reasoning_effort': APIZ_GEMINI_MODELS[model]['reasoning_effort']}
    if provider_for_model(model)=='gemini' and model in GEMINI_REASONING_EFFORTS:
        # Project policy for the official Google connection. Deliberately
        # independent of old gateway/global reasoning environment settings.
        return {'reasoning_effort':GEMINI_REASONING_EFFORTS[model]}
    if model.startswith("claude-"):
        return {}  # Do not send OpenAI-specific thinking knobs to Anthropic models.
    if model.startswith(("grok-", "deepseek-")):
        # Use gateway defaults; GPT reasoning levels are not a Grok contract.
        return {}
    effort = os.getenv("LLM_REASONING_EFFORT", "medium").strip().lower()
    if effort not in {"none", "low", "medium", "high", "xhigh", "max"}:
        raise ValueError("Invalid LLM_REASONING_EFFORT")
    return {"reasoning_effort": effort}


class ModelRouter:
    """SDK-compatible facade routing each request to its own credential/host.

    Connections are lazy and reused. Constructing/selecting a model never makes
    an inference request. No retry/fallback can cross the provider boundary.
    """
    def __init__(self, client_factory):
        self._factory = client_factory
        self._clients = {}
        self._lock = Lock()
        self.chat = self
        self.completions = self

    def create(self, *, model, **kwargs):
        provider = provider_for_model(model)
        key = api_key(provider)
        if not key:
            variable = 'GEMINI_API_KEY' if provider == 'gemini' else 'APIZ_API_KEY'
            raise ValueError(f'Chưa cấu hình {variable}; không gửi request')
        with self._lock:
            if provider not in self._clients:
                self._clients[provider] = self._factory(
                    api_key=key, base_url=endpoint(provider),
                    timeout=request_timeout(), max_retries=0)
            sdk = self._clients[provider]
        return sdk.chat.completions.create(model=upstream_model(model), **kwargs)

    def close(self):
        for sdk in self._clients.values():
            sdk.close()
