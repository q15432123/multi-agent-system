"""LLM Client — 多供應商認證 + 統一 API 介面

支援三種認證方式：
1. API Key 直接輸入
2. OAuth 登入（Google / Anthropic / Kimi）
3. OpenRouter 統一 gateway（一把 key 打全部）

每個 provider 獨立設定，存在 _config/providers.json：
{
  "google":    {"auth": "oauth", "token": "ya29...", "model": "gemini-2.5-flash"},
  "anthropic": {"auth": "oauth", "token": "sk-ant...", "model": "claude-sonnet-4"},
  "kimi":      {"auth": "api_key", "api_key": "sk-kimi...", "model": "kimi-k2"},
  "openrouter":{"auth": "api_key", "api_key": "sk-or...", "model": ""},
}
"""
import json
import logging
import os
import time
from pathlib import Path
from typing import Generator, Optional

logger = logging.getLogger("llm")

CONFIG_DIR = Path(__file__).parent.parent / "_config"
PROVIDERS_FILE = CONFIG_DIR / "providers.json"

# ═══════════════════════════════════════
# Provider 定義
# ═══════════════════════════════════════

PROVIDERS = {
    "google": {
        "name": "Google Gemini",
        "auth_methods": ["oauth", "api_key"],
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
        "oauth_url": "https://accounts.google.com/o/oauth2/v2/auth",
        "token_url": "https://oauth2.googleapis.com/token",
        "scopes": "https://www.googleapis.com/auth/generative-language",
        "default_model": "gemini-2.5-flash",
    },
    "anthropic": {
        "name": "Anthropic Claude",
        "auth_methods": ["oauth", "api_key"],
        "base_url": "https://api.anthropic.com/v1/",
        "oauth_url": "https://console.anthropic.com/oauth/authorize",
        "token_url": "https://console.anthropic.com/oauth/token",
        "scopes": "",
        "default_model": "claude-sonnet-4-20250514",
    },
    "kimi": {
        "name": "Moonshot Kimi",
        "auth_methods": ["api_key", "oauth"],
        "base_url": "https://api.moonshot.cn/v1",
        "oauth_url": "https://kimi.moonshot.cn/oauth/authorize",
        "token_url": "https://kimi.moonshot.cn/oauth/token",
        "scopes": "",
        "default_model": "kimi-k2",
    },
    "local_proxy": {
        "name": "Local LLM Proxy (all CLI tokens)",
        "auth_methods": ["none"],
        "base_url": "http://127.0.0.1:4000/v1",
        "default_model": "kimi-v1-2p5",
    },
    "openrouter": {
        "name": "OpenRouter (All Models)",
        "auth_methods": ["api_key"],
        "base_url": "https://openrouter.ai/api/v1",
        "default_model": "anthropic/claude-sonnet-4",
    },
    "openai": {
        "name": "OpenAI",
        "auth_methods": ["api_key"],
        "base_url": "https://api.openai.com/v1",
        "default_model": "gpt-4o",
    },
    "deepseek": {
        "name": "DeepSeek",
        "auth_methods": ["api_key"],
        "base_url": "https://api.deepseek.com",
        "default_model": "deepseek-chat",
    },
}

# Agent tag → provider mapping
# 預設全部走 local_proxy（localhost:4000），除非明確指定
TAG_TO_PROVIDER = {
    "claude": "local_proxy",
    "gemini": "local_proxy",
    "kimi": "local_proxy",
    "gpt": "local_proxy",
    "openai": "local_proxy",
    "deepseek": "deepseek",
    "qwen": "openrouter",
}


# ═══════════════════════════════════════
# Provider 設定 CRUD
# ═══════════════════════════════════════

def _load_providers() -> dict:
    if PROVIDERS_FILE.exists():
        try:
            return json.loads(PROVIDERS_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _save_providers(data: dict) -> None:
    CONFIG_DIR.mkdir(exist_ok=True)
    PROVIDERS_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def get_provider_config(provider: str) -> dict:
    """取得某 provider 的設定"""
    all_cfg = _load_providers()
    return all_cfg.get(provider, {})


def set_provider_config(provider: str, config: dict) -> None:
    """設定某 provider（api_key / token / model 等）"""
    all_cfg = _load_providers()
    existing = all_cfg.get(provider, {})
    existing.update(config)
    all_cfg[provider] = existing
    _save_providers(all_cfg)
    logger.info(f"[LLM] Provider {provider} config updated")


def get_all_providers_status() -> dict:
    """取得所有 provider 的狀態（不含敏感 key）"""
    all_cfg = _load_providers()
    result = {}
    for pid, pdef in PROVIDERS.items():
        cfg = all_cfg.get(pid, {})
        auth = cfg.get("auth", "")
        has_key = bool(cfg.get("api_key"))
        has_token = bool(cfg.get("access_token"))
        token_expiry = cfg.get("token_expiry", 0)
        expired = token_expiry > 0 and time.time() > token_expiry

        result[pid] = {
            "name": pdef["name"],
            "auth_methods": pdef["auth_methods"],
            "auth": auth,
            "ready": (auth == "api_key" and has_key) or (auth == "oauth" and has_token and not expired),
            "has_key": has_key,
            "has_token": has_token,
            "token_expired": expired,
            "model": cfg.get("model", pdef.get("default_model", "")),
        }

    # 也檢查環境變數
    for env_key, provider in [
        ("OPENROUTER_API_KEY", "openrouter"),
        ("ANTHROPIC_API_KEY", "anthropic"),
        ("GOOGLE_API_KEY", "google"),
        ("MOONSHOT_API_KEY", "kimi"),
        ("OPENAI_API_KEY", "openai"),
    ]:
        if os.getenv(env_key) and provider in result:
            result[provider]["ready"] = True
            result[provider]["has_key"] = True
            result[provider]["auth"] = "env"

    return result


# ═══════════════════════════════════════
# OAuth Token 管理
# ═══════════════════════════════════════

def store_oauth_token(provider: str, token_data: dict) -> None:
    """儲存 OAuth token（從 callback 收到的）"""
    cfg = get_provider_config(provider)
    cfg["auth"] = "oauth"
    cfg["access_token"] = token_data.get("access_token", "")
    cfg["refresh_token"] = token_data.get("refresh_token", "")
    cfg["token_expiry"] = time.time() + token_data.get("expires_in", 3600)
    set_provider_config(provider, cfg)
    logger.info(f"[LLM] OAuth token stored for {provider}")


def get_oauth_params(provider: str) -> dict:
    """取得 OAuth 登入 URL 需要的參數"""
    pdef = PROVIDERS.get(provider)
    if not pdef or "oauth" not in pdef.get("auth_methods", []):
        return {"error": f"{provider} does not support OAuth"}

    # OAuth client ID 從環境變數或 providers.json 讀取
    cfg = get_provider_config(provider)
    client_id = cfg.get("client_id") or os.getenv(f"{provider.upper()}_CLIENT_ID", "")
    redirect_uri = cfg.get("redirect_uri", "http://127.0.0.1:3000/api/auth/callback/" + provider)

    if not client_id:
        return {
            "error": f"No client_id for {provider}. "
                     f"Set {provider.upper()}_CLIENT_ID env var or configure in provider settings."
        }

    params = {
        "provider": provider,
        "auth_url": pdef["oauth_url"],
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": pdef.get("scopes", ""),
        "response_type": "code",
    }
    return params


# ═══════════════════════════════════════
# Client 建立（per-provider）
# ═══════════════════════════════════════

def _refresh_token_if_needed(provider: str, cfg: dict) -> str:
    """檢查 OAuth token 是否過期，自動用 refresh_token 換新"""
    import aiohttp

    expiry = cfg.get("token_expiry", 0)
    refresh = cfg.get("refresh_token", "")

    if not refresh or not expiry:
        return cfg.get("access_token", "")

    # 還有 5 分鐘以上 → 不刷新
    if time.time() < expiry - 300:
        return cfg.get("access_token", "")

    # 需要刷新
    pdef = PROVIDERS.get(provider, {})
    token_url = pdef.get("token_url", "")
    client_id = cfg.get("client_id") or os.getenv(f"{provider.upper()}_CLIENT_ID", "")
    client_secret = cfg.get("client_secret") or os.getenv(f"{provider.upper()}_CLIENT_SECRET", "")

    if not token_url or not client_id or not client_secret:
        logger.warning(f"[LLM] Cannot refresh token for {provider}: missing credentials")
        return cfg.get("access_token", "")

    try:
        import urllib.request
        import urllib.parse
        data = urllib.parse.urlencode({
            "grant_type": "refresh_token",
            "refresh_token": refresh,
            "client_id": client_id,
            "client_secret": client_secret,
        }).encode()
        req = urllib.request.Request(token_url, data=data,
                                     headers={"Content-Type": "application/x-www-form-urlencoded"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            token_data = json.loads(resp.read().decode())

        new_token = token_data.get("access_token", "")
        if new_token:
            store_oauth_token(provider, token_data)
            logger.info(f"[LLM] Refreshed OAuth token for {provider}")
            return new_token

    except Exception as e:
        logger.error(f"[LLM] Token refresh failed for {provider}: {e}")

    return cfg.get("access_token", "")


def _get_client_for_provider(provider: str):
    """為特定 provider 建立 OpenAI-compatible client"""
    from openai import OpenAI

    pdef = PROVIDERS.get(provider, {})
    cfg = get_provider_config(provider)
    auth = cfg.get("auth", "")

    # 決定 API key
    api_key = ""
    if auth == "oauth":
        api_key = _refresh_token_if_needed(provider, cfg)  # Step 7: auto-refresh
    elif auth == "api_key":
        api_key = cfg.get("api_key", "")

    # 環境變數 fallback
    if not api_key:
        env_map = {
            "openrouter": "OPENROUTER_API_KEY",
            "anthropic": "ANTHROPIC_API_KEY",
            "google": "GOOGLE_API_KEY",
            "kimi": "MOONSHOT_API_KEY",
            "openai": "OPENAI_API_KEY",
            "deepseek": "DEEPSEEK_API_KEY",
        }
        api_key = os.getenv(env_map.get(provider, ""), "")

    # local_proxy 不需要 key（proxy 自己管 token）
    if provider == "local_proxy":
        api_key = "local-proxy-no-key-needed"

    if not api_key:
        raise RuntimeError(
            f"No credentials for {provider}. "
            f"Configure API key or OAuth in provider settings."
        )

    base_url = cfg.get("base_url", pdef.get("base_url", "https://openrouter.ai/api/v1"))

    # Kimi Coding API requires coding-agent User-Agent
    # OpenAI SDK overrides httpx headers, must use default_headers param
    extra = {}
    ua_spoof = cfg.get("ua_spoof", "")
    if provider == "kimi" and "kimi.com" in base_url:
        ua_spoof = ua_spoof or "claude-code/2.1.76"
    if ua_spoof:
        extra["default_headers"] = {"User-Agent": ua_spoof}

    return OpenAI(base_url=base_url, api_key=api_key, **extra)


def _resolve_provider(agent_tags: list[str]) -> str:
    """從 agent tags 決定用哪個 provider"""
    for tag in agent_tags:
        t = tag.lower()
        if t in TAG_TO_PROVIDER:
            return TAG_TO_PROVIDER[t]

    # Fallback: local_proxy 優先（自動吃 CLI token）
    return "local_proxy"


def resolve_model(agent_tags: list[str]) -> str:
    """從 agent tags 決定要用哪個 model"""
    provider = _resolve_provider(agent_tags)
    cfg = get_provider_config(provider)
    pdef = PROVIDERS.get(provider, {})

    # 優先用 provider 設定的 model
    model = cfg.get("model") or pdef.get("default_model", "")
    return model


def resolve_provider_and_model(agent_tags: list[str]) -> tuple[str, str]:
    """回傳 (provider_id, model_id)"""
    provider = _resolve_provider(agent_tags)
    model = resolve_model(agent_tags)
    return provider, model


# ═══════════════════════════════════════
# Retry Logic (Step 5)
# ═══════════════════════════════════════

MAX_RETRIES = 3
RETRY_DELAYS = [1, 2, 4]  # 指數退避（秒）

def _should_retry(error_str: str) -> bool:
    """判斷是否該重試"""
    retryable = ["429", "500", "502", "503", "timeout", "connection", "rate limit"]
    lower = error_str.lower()
    return any(kw in lower for kw in retryable)


# ═══════════════════════════════════════
# Chat API（per-provider）
# ═══════════════════════════════════════

def chat(
    model: str,
    messages: list[dict],
    temperature: float = 0.7,
    max_tokens: int = 4096,
    provider: str = "",
    agent_id: str = "",
) -> dict:
    """同步呼叫 LLM"""
    from engine.syslog import syslog
    if not provider:
        provider = _guess_provider_from_model(model)

    t0 = time.time()
    try:
        client = _get_client_for_provider(provider)
        resp = client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        content = resp.choices[0].message.content or ""
        pt = resp.usage.prompt_tokens if resp.usage else 0
        ct = resp.usage.completion_tokens if resp.usage else 0
        dur = int((time.time() - t0) * 1000)

        syslog.log_llm_request(agent_id or "system", provider, model,
                               prompt_tokens=pt, completion_tokens=ct, duration_ms=dur)

        return {"ok": True, "content": content, "model": model, "provider": provider,
                "usage": {"prompt_tokens": pt, "completion_tokens": ct}}

    except Exception as e:
        dur = int((time.time() - t0) * 1000)
        err_str = str(e)

        # Retry logic
        if _should_retry(err_str):
            for attempt in range(MAX_RETRIES):
                delay = RETRY_DELAYS[min(attempt, len(RETRY_DELAYS) - 1)]
                logger.warning(f"[LLM] Retry {attempt+1}/{MAX_RETRIES} in {delay}s: {err_str[:60]}")
                syslog.warn(agent_id or "system", "LLM_RETRY",
                            f"Retry {attempt+1}: {err_str[:80]}",
                            extra={"attempt": attempt+1, "delay": delay})
                time.sleep(delay)
                try:
                    client = _get_client_for_provider(provider)
                    resp = client.chat.completions.create(
                        model=model, messages=messages,
                        temperature=temperature, max_tokens=max_tokens)
                    content = resp.choices[0].message.content or ""
                    pt = resp.usage.prompt_tokens if resp.usage else 0
                    ct = resp.usage.completion_tokens if resp.usage else 0
                    dur2 = int((time.time() - t0) * 1000)
                    syslog.log_llm_request(agent_id or "system", provider, model,
                                           prompt_tokens=pt, completion_tokens=ct, duration_ms=dur2)
                    return {"ok": True, "content": content, "model": model,
                            "provider": provider, "usage": {"prompt_tokens": pt, "completion_tokens": ct}}
                except Exception as e2:
                    err_str = str(e2)
                    continue

        status = 429 if "429" in err_str else 500 if "500" in err_str else 0
        syslog.log_llm_request(agent_id or "system", provider, model,
                               error=err_str, status_code=status, duration_ms=dur)
        return {"ok": False, "error": err_str, "model": model, "provider": provider}


def chat_stream(
    model: str,
    messages: list[dict],
    temperature: float = 0.7,
    max_tokens: int = 4096,
    provider: str = "",
    agent_id: str = "",
) -> Generator[str, None, None]:
    """串流呼叫 LLM，逐 token yield"""
    from engine.syslog import syslog
    if not provider:
        provider = _guess_provider_from_model(model)

    t0 = time.time()
    total_chars = 0
    try:
        client = _get_client_for_provider(provider)
        stream = client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            stream=True,
        )
        for chunk in stream:
            delta = chunk.choices[0].delta if chunk.choices else None
            if delta and delta.content:
                total_chars += len(delta.content)
                yield delta.content

        dur = int((time.time() - t0) * 1000)
        syslog.log_llm_request(agent_id or "system", provider, model,
                               completion_tokens=total_chars // 4, duration_ms=dur)

    except Exception as e:
        dur = int((time.time() - t0) * 1000)
        err_str = str(e)
        status = 429 if "429" in err_str else 500 if "500" in err_str else 0
        syslog.log_llm_request(agent_id or "system", provider, model,
                               error=err_str, status_code=status, duration_ms=dur)
        yield f"\n[LLM ERROR: {provider}] {e}\n"


def _guess_provider_from_model(model: str) -> str:
    """從 model ID 猜 provider"""
    m = model.lower()
    if "claude" in m or "anthropic" in m:
        return "anthropic"
    if "gemini" in m or "google" in m:
        return "google"
    if "kimi" in m or "moonshot" in m:
        return "kimi"
    if "gpt" in m or "o1" in m or "o3" in m:
        return "openai"
    if "deepseek" in m:
        return "deepseek"
    if "/" in m:
        return "openrouter"  # model with prefix like "anthropic/claude-xxx"
    return "openrouter"


# ═══════════════════════════════════════
# 向後兼容
# ═══════════════════════════════════════

def save_config(api_key: str = "", base_url: str = "", default_model: str = "") -> None:
    """舊介面兼容 — 存到 openrouter provider"""
    cfg = {}
    if api_key:
        cfg["api_key"] = api_key
        cfg["auth"] = "api_key"
    if base_url:
        cfg["base_url"] = base_url
    if default_model:
        cfg["model"] = default_model
    if cfg:
        set_provider_config("openrouter", cfg)
