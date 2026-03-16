"""LLM Proxy Server — 本地 OpenAI-compatible API gateway (port 4000)

自動從各家 CLI 讀取 OAuth token，統一包成 OpenAI-compatible API。
MuteAgent 打 localhost:4000/v1/chat/completions 就能用所有模型。

Token 來源：
  Kimi:   ~/.kimi/credentials/kimi-code.json
  Claude: ~/.claude/.credentials.json
  Gemini: ~/.gemini/oauth_creds.json

每 5 分鐘自動重新掃描 token（處理過期/refresh）。
"""
import asyncio
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Optional

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
import uvicorn

logger = logging.getLogger("llm_proxy")

app = FastAPI(title="LLM Proxy", version="1.0")
HOME = Path.home()

# ═══════════════════════════════════════
# Provider 定義
# ═══════════════════════════════════════

PROVIDERS = {
    "kimi": {
        "name": "Moonshot Kimi",
        "token_paths": [
            HOME / ".kimi" / "credentials" / "kimi-code.json",
        ],
        "token_key": "access_token",
        "expires_key": "expires_at",
        "base_url": "https://api.kimi.com/coding/v1",
        "ua_spoof": "claude-code/2.1.76",
        "refresh_cmd": "kimi --version",
        "models": {
            "kimi-v1-2p5":   {"name": "Kimi v1 2.5"},
            "kimi-2":        {"name": "Kimi 2"},
            "kimi-k2":       {"name": "Kimi K2"},
            "kimi-k2-turbo": {"name": "Kimi K2 Turbo"},
        },
        "default_model": "kimi-v1-2p5",
    },
    "anthropic": {
        "name": "Anthropic Claude (via CLI)",
        "token_paths": [
            HOME / ".claude" / ".credentials.json",
        ],
        "token_key": "claudeAiOauth.accessToken",
        "base_url": "https://api.anthropic.com/v1",
        "use_subprocess": True,  # 用 claude -p 呼叫，繞過 API OAuth 限制
        "models": {
            "claude-opus-4-6":          {"name": "Claude Opus 4.6"},
            "claude-opus-4-20250514":   {"name": "Claude Opus 4"},
            "claude-sonnet-4":          {"name": "Claude Sonnet 4"},
            "claude-sonnet-4-20250514": {"name": "Claude Sonnet 4"},
            "claude-haiku-4-5-20241022":{"name": "Claude Haiku 4.5"},
        },
        "default_model": "claude-sonnet-4-20250514",
    },
    "google": {
        "name": "Google Gemini",
        "token_paths": [
            HOME / ".gemini" / "oauth_creds.json",
        ],
        "token_key": "access_token",
        "base_url": "https://generativelanguage.googleapis.com/v1beta",
        "needs_gemini_format": True,  # 用 native generateContent API
        "env_key": "GOOGLE_API_KEY",
        "refresh_cmd": "gemini --version",
        "models": {
            "gemini-2.5-flash":  {"name": "Gemini 2.5 Flash"},
            "gemini-2.5-pro":    {"name": "Gemini 2.5 Pro"},
            "gemini-3.0":        {"name": "Gemini 3.0"},
        },
        "default_model": "gemini-2.5-flash",
    },
    "openai": {
        "name": "OpenAI",
        "base_url": "https://api.openai.com/v1",
        "env_key": "OPENAI_API_KEY",
        "models": {
            "gpt-4o":      {"name": "GPT-4o"},
            "gpt-4o-mini": {"name": "GPT-4o mini"},
            "o3-mini":     {"name": "o3 mini"},
        },
        "default_model": "gpt-4o",
    },
}

# model → provider 路由表（啟動時建立）
MODEL_ROUTER: dict[str, str] = {}
for pid, pcfg in PROVIDERS.items():
    for mid in pcfg.get("models", {}):
        MODEL_ROUTER[mid] = pid


def _resolve_model(model: str) -> tuple[str, str]:
    """model string → (provider_id, model_id)"""
    # 精確匹配
    if model in MODEL_ROUTER:
        return MODEL_ROUTER[model], model

    # 模糊匹配
    ml = model.lower()
    if any(k in ml for k in ("claude", "opus", "sonnet", "haiku")):
        return "anthropic", model
    if any(k in ml for k in ("gemini",)):
        return "google", model
    if any(k in ml for k in ("kimi",)):
        return "kimi", model
    if any(k in ml for k in ("gpt", "o1", "o3")):
        return "openai", model

    return "", model


# ═══════════════════════════════════════
# Token Store — 自動掃描 + 定時刷新
# ═══════════════════════════════════════

def _read_nested(data, dotpath: str):
    """讀巢狀 key，如 'claudeAiOauth.accessToken'"""
    for k in dotpath.split("."):
        if isinstance(data, dict):
            data = data.get(k)
        else:
            return None
    return data


class TokenStore:
    def __init__(self):
        self._tokens: dict[str, dict] = {}
        self._last_scan: float = 0

    def harvest_all(self) -> dict[str, bool]:
        results = {}
        for pid, pcfg in PROVIDERS.items():
            results[pid] = self._harvest(pid, pcfg)
        self._last_scan = time.time()
        return results

    def _harvest(self, provider: str, pcfg: dict) -> bool:
        # 1. 從 CLI token 檔案讀取
        for path in pcfg.get("token_paths", []):
            if Path(path).exists():
                try:
                    raw = json.loads(Path(path).read_text(encoding="utf-8"))
                    tk = pcfg.get("token_key", "access_token")
                    token = _read_nested(raw, tk) if "." in tk else raw.get(tk, "")
                    ek = pcfg.get("expires_key", "expires_at")
                    exp = _read_nested(raw, ek) if "." in ek else raw.get(ek, 0)
                    if exp and pcfg.get("expires_ms"):
                        exp = exp / 1000  # ms → s
                    if token:
                        self._tokens[provider] = {
                            "token": token, "expires_at": exp or 0, "source": "cli",
                        }
                        return True
                except Exception as e:
                    logger.warning(f"[Proxy] {provider} token read failed: {e}")

        # 2. 環境變數 fallback
        env_key = pcfg.get("env_key", "")
        if env_key and os.getenv(env_key):
            self._tokens[provider] = {"token": os.getenv(env_key), "expires_at": 0, "source": "env"}
            return True

        return False

    def get_token(self, provider: str) -> Optional[str]:
        """取 token，過期就自動 refresh"""
        info = self._tokens.get(provider)
        if not info:
            return None

        exp = info.get("expires_at", 0)
        if exp > 0 and time.time() > exp - 120:
            # 過期了，嘗試 refresh
            pcfg = PROVIDERS.get(provider, {})
            cmd = pcfg.get("refresh_cmd")
            if cmd:
                try:
                    import subprocess
                    subprocess.run(cmd, shell=True, capture_output=True, timeout=15)
                    logger.info(f"[Proxy] {provider} CLI refresh triggered")
                except Exception:
                    pass
            self._harvest(provider, pcfg)
            info = self._tokens.get(provider)

        return info.get("token") if info else None

    def get_status(self) -> dict:
        now = time.time()
        result = {}
        for pid, pcfg in PROVIDERS.items():
            info = self._tokens.get(pid)
            ready = bool(info and info.get("token"))
            expired = False
            if info and info.get("expires_at", 0) > 0:
                expired = now > info["expires_at"]
            result[pid] = {
                "name": pcfg["name"],
                "ready": ready and not expired,
                "source": info.get("source", "") if info else "",
                "expired": expired,
                "models": list(pcfg.get("models", {}).keys()),
            }
        return result


token_store = TokenStore()


# ═══════════════════════════════════════
# Routes
# ═══════════════════════════════════════

@app.on_event("startup")
async def startup():
    results = token_store.harvest_all()
    for pid, ok in results.items():
        name = PROVIDERS[pid]["name"]
        print(f"  {'✅' if ok else '❌'} {name}: {'ready' if ok else 'no token'}")

    # 定時重新掃描（每 5 分鐘）
    async def _periodic_scan():
        while True:
            await asyncio.sleep(300)
            token_store.harvest_all()
            logger.info("[Proxy] Token re-scan complete")
    asyncio.create_task(_periodic_scan())


@app.get("/health")
async def health():
    return {"status": "ok", "providers": token_store.get_status()}


@app.get("/v1/models")
async def list_models():
    status = token_store.get_status()
    data = []
    for pid, info in status.items():
        for mid in info.get("models", []):
            data.append({
                "id": mid,
                "object": "model",
                "owned_by": pid,
                "provider": pid,
                "ready": info["ready"],
            })
    return {"object": "list", "data": data}


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    body = await request.json()
    model = body.get("model", "")
    stream = body.get("stream", False)

    provider, model_id = _resolve_model(model)
    if not provider:
        return JSONResponse(
            {"error": {"message": f"Unknown model: {model}. Use GET /v1/models to see available.", "type": "invalid_model"}},
            status_code=400,
        )

    token = token_store.get_token(provider)
    if not token:
        return JSONResponse(
            {"error": {"message": f"No token for {provider}. Run its CLI login first.", "type": "auth_error"}},
            status_code=401,
        )

    pcfg = PROVIDERS[provider]
    base_url = pcfg["base_url"].rstrip("/")
    ua = pcfg.get("ua_spoof", "")

    # Claude: 用 subprocess 呼叫 claude CLI（繞過 API 限制）
    if pcfg.get("use_subprocess"):
        return await _proxy_claude_subprocess(body, stream)

    # Gemini: 用 native generateContent API + 格式轉換
    if pcfg.get("needs_gemini_format"):
        return await _proxy_gemini(base_url, token, body, stream)

    if pcfg.get("needs_anthropic_format"):
        return await _proxy_anthropic(base_url, token, body, stream, ua)
    return await _proxy_openai_compat(base_url, token, body, stream, ua)


# ═══════════════════════════════════════
# Gemini native API 轉換
# ═══════════════════════════════════════

async def _proxy_gemini(base_url: str, token: str, body: dict, stream: bool):
    """OpenAI 格式 → Gemini generateContent API"""
    model = body.get("model", "gemini-2.5-flash")
    url = f"{base_url}/models/{model}:generateContent"

    # 轉換 messages → Gemini contents 格式
    contents = []
    system_text = ""
    for m in body.get("messages", []):
        role = m.get("role", "user")
        content = m.get("content", "")
        if role == "system":
            system_text += content + "\n"
        elif role == "assistant":
            contents.append({"role": "model", "parts": [{"text": content}]})
        else:
            contents.append({"role": "user", "parts": [{"text": content}]})

    gbody = {"contents": contents}
    if system_text.strip():
        gbody["systemInstruction"] = {"parts": [{"text": system_text.strip()}]}
    if body.get("max_tokens"):
        gbody["generationConfig"] = {"maxOutputTokens": body["max_tokens"]}
    if body.get("temperature") is not None:
        gbody.setdefault("generationConfig", {})["temperature"] = body["temperature"]

    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    async with httpx.AsyncClient(timeout=120) as c:
        r = await c.post(url, json=gbody, headers=headers)
        if r.status_code != 200:
            return JSONResponse(r.json(), status_code=r.status_code)

        # 轉換回 OpenAI 格式
        data = r.json()
        text = ""
        for candidate in data.get("candidates", []):
            for part in candidate.get("content", {}).get("parts", []):
                text += part.get("text", "")

        return JSONResponse({
            "id": f"gemini-{int(time.time())}",
            "object": "chat.completion",
            "model": model,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
            "usage": {
                "prompt_tokens": data.get("usageMetadata", {}).get("promptTokenCount", 0),
                "completion_tokens": data.get("usageMetadata", {}).get("candidatesTokenCount", 0),
            },
        })


# ═══════════════════════════════════════
# Claude subprocess（用 claude -p 呼叫，不需要 API key）
# ═══════════════════════════════════════

CLAUDE_TIMEOUT = 60       # 硬性 timeout
CLAUDE_MAX_OUTPUT = 102400  # 100KB stdout 上限

async def _proxy_claude_subprocess(body: dict, stream: bool):
    """呼叫 claude CLI subprocess，用你的 Max 訂閱。

    防護機制：
    - Popen（不用 shell=True）
    - 硬性 60s timeout + kill
    - stdout 截斷 100KB
    - 所有異常回傳 error response，不 crash proxy
    """
    import subprocess as sp
    import shutil

    messages = body.get("messages", [])
    model = body.get("model", "claude-sonnet-4-20250514")

    parts = []
    for m in messages:
        role = m.get("role", "")
        content = m.get("content", "")
        if role == "system":
            parts.append(f"[System instructions]\n{content}\n")
        elif role == "user":
            parts.append(content)
        elif role == "assistant":
            parts.append(f"[Previous response]\n{content}\n")

    prompt = "\n".join(parts)
    if len(prompt) > 50000:
        prompt = prompt[:50000]

    # 找 claude 執行檔（不用 shell=True）
    claude_path = shutil.which("claude")
    if not claude_path:
        return JSONResponse(
            {"error": {"message": "claude CLI not found in PATH", "type": "not_found"}},
            status_code=500,
        )

    cmd = [claude_path, "-p", prompt, "--output-format", "json", "--model", model]
    proc = None

    try:
        proc = sp.Popen(
            cmd,
            stdout=sp.PIPE,
            stderr=sp.PIPE,
            stdin=sp.DEVNULL,  # 不接受 stdin
            cwd=str(HOME / "Desktop"),
            text=True,
        )

        try:
            stdout, stderr = proc.communicate(timeout=CLAUDE_TIMEOUT)
        except sp.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
            logger.warning(f"[Proxy] Claude subprocess killed after {CLAUDE_TIMEOUT}s timeout")
            return JSONResponse(
                {"error": {"message": f"Claude CLI timeout ({CLAUDE_TIMEOUT}s)", "type": "timeout"}},
                status_code=504,
            )

        # 截斷 stdout
        if len(stdout) > CLAUDE_MAX_OUTPUT:
            stdout = stdout[:CLAUDE_MAX_OUTPUT]
            logger.warning(f"[Proxy] Claude stdout truncated at {CLAUDE_MAX_OUTPUT} bytes")

        # 檢查退出碼
        if proc.returncode != 0:
            error_msg = (stderr or "")[:500] or f"claude exited with code {proc.returncode}"
            logger.warning(f"[Proxy] Claude subprocess exit {proc.returncode}: {error_msg[:100]}")
            return JSONResponse(
                {"error": {"message": error_msg, "type": "subprocess_error"}},
                status_code=500,
            )

        # 解析 JSON 輸出
        output = stdout.strip()
        try:
            data = json.loads(output)
            text = data.get("result", data.get("content", output))
        except json.JSONDecodeError:
            text = output

        return JSONResponse({
            "id": f"claude-sub-{int(time.time())}",
            "object": "chat.completion",
            "model": model,
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": len(prompt) // 4, "completion_tokens": len(str(text)) // 4},
        })

    except FileNotFoundError:
        return JSONResponse(
            {"error": {"message": "claude CLI not found", "type": "not_found"}},
            status_code=500,
        )
    except Exception as e:
        logger.error(f"[Proxy] Claude subprocess exception: {e}")
        return JSONResponse(
            {"error": {"message": str(e)[:500], "type": "subprocess_error"}},
            status_code=500,
        )
    finally:
        # 確保 process 不會殘留
        if proc and proc.poll() is None:
            try:
                proc.kill()
                proc.wait(timeout=3)
                logger.warning("[Proxy] Killed orphan claude process")
            except Exception:
                pass


# ═══════════════════════════════════════
# OpenAI-compatible 轉發（Kimi / Gemini / OpenAI）
# ═══════════════════════════════════════

async def _proxy_openai_compat(base_url: str, token: str, body: dict, stream: bool, ua: str):
    url = f"{base_url}/chat/completions"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    if ua:
        headers["User-Agent"] = ua

    if not stream:
        async with httpx.AsyncClient(timeout=120) as c:
            r = await c.post(url, json=body, headers=headers)
            return JSONResponse(r.json(), status_code=r.status_code)

    return StreamingResponse(
        _sse_passthrough(url, headers, body),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def _sse_passthrough(url, headers, body):
    body["stream"] = True
    async with httpx.AsyncClient(timeout=120) as c:
        async with c.stream("POST", url, json=body, headers=headers) as r:
            async for line in r.aiter_lines():
                yield line + "\n"


# ═══════════════════════════════════════
# Anthropic Messages API 轉發 + 格式轉換
# ═══════════════════════════════════════

async def _proxy_anthropic(base_url: str, token: str, body: dict, stream: bool, ua: str):
    url = f"{base_url}/messages"
    # CLI OAuth token 用 Bearer，API key 用 x-api-key
    is_oauth = token.startswith("sk-ant-oat") or token.startswith("eyJ")
    headers = {
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    if is_oauth:
        headers["Authorization"] = f"Bearer {token}"
    else:
        headers["x-api-key"] = token
    if ua:
        headers["User-Agent"] = ua

    # OpenAI messages → Anthropic messages（提取 system）
    messages = body.get("messages", [])
    system_text = ""
    chat_msgs = []
    for m in messages:
        if m["role"] == "system":
            system_text += m.get("content", "") + "\n"
        else:
            chat_msgs.append({"role": m["role"], "content": m.get("content", "")})

    abody = {
        "model": body.get("model", "claude-sonnet-4-20250514"),
        "max_tokens": body.get("max_tokens", 4096),
        "messages": chat_msgs,
    }
    if system_text.strip():
        abody["system"] = system_text.strip()
    if body.get("temperature") is not None:
        abody["temperature"] = body["temperature"]
    if body.get("tools"):
        abody["tools"] = [
            {"name": t["function"]["name"], "description": t["function"].get("description", ""),
             "input_schema": t["function"].get("parameters", {})}
            for t in body["tools"]
        ]

    if not stream:
        async with httpx.AsyncClient(timeout=120) as c:
            r = await c.post(url, json=abody, headers=headers)
            if r.status_code == 200:
                return JSONResponse(_a2o(r.json(), body.get("model", "")))
            return JSONResponse(r.json(), status_code=r.status_code)

    abody["stream"] = True
    return StreamingResponse(
        _sse_anthropic_to_openai(url, headers, abody),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _a2o(data: dict, model: str) -> dict:
    """Anthropic response → OpenAI format"""
    text = ""
    tool_calls = []
    for block in data.get("content", []):
        if block.get("type") == "text":
            text += block.get("text", "")
        elif block.get("type") == "tool_use":
            tool_calls.append({
                "id": block.get("id", ""),
                "type": "function",
                "function": {
                    "name": block.get("name", ""),
                    "arguments": json.dumps(block.get("input", {})),
                },
            })

    msg = {"role": "assistant", "content": text}
    if tool_calls:
        msg["tool_calls"] = tool_calls

    return {
        "id": data.get("id", ""),
        "object": "chat.completion",
        "model": model,
        "choices": [{"index": 0, "message": msg, "finish_reason": data.get("stop_reason", "stop")}],
        "usage": {
            "prompt_tokens": data.get("usage", {}).get("input_tokens", 0),
            "completion_tokens": data.get("usage", {}).get("output_tokens", 0),
        },
    }


async def _sse_anthropic_to_openai(url, headers, body):
    """Anthropic SSE → OpenAI SSE chunk 格式"""
    async with httpx.AsyncClient(timeout=120) as c:
        async with c.stream("POST", url, json=body, headers=headers) as r:
            async for line in r.aiter_lines():
                if not line.startswith("data: "):
                    if line.strip():
                        yield line + "\n"
                    continue
                raw = line[6:]
                if raw.strip() == "[DONE]":
                    yield "data: [DONE]\n\n"
                    break
                try:
                    evt = json.loads(raw)
                    et = evt.get("type", "")
                    if et == "content_block_delta":
                        txt = evt.get("delta", {}).get("text", "")
                        if txt:
                            yield f"data: {json.dumps({'object':'chat.completion.chunk','choices':[{'index':0,'delta':{'content':txt}}]})}\n\n"
                    elif et == "message_stop":
                        yield "data: [DONE]\n\n"
                except Exception:
                    pass


# ═══════════════════════════════════════
# Start
# ═══════════════════════════════════════

def start_proxy(port: int = 4000):
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="info")


def start_proxy_background(port: int = 4000):
    import threading
    t = threading.Thread(
        target=lambda: uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning"),
        daemon=True,
    )
    t.start()
    logger.info(f"[Proxy] Background on port {port}")
