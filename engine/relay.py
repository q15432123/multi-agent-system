"""AgentRelay — 訊息格式化 + API 執行器

責任：
1. extract_payload() — 從 Agent 輸出中擷取 code/json，過濾廢話
2. execute_api() — Node D 專用，收到格式化資料後發 HTTP request
3. format_for_target() — 組合乾淨的 relay 訊息給下游 agent
"""
import json
import logging
import re
from typing import Optional

logger = logging.getLogger("relay")

# ─── Code/JSON 擷取 ───

# 匹配 ```lang\n...\n``` 區塊
_CODE_BLOCK = re.compile(r'```[\w]*\n(.*?)```', re.DOTALL)
# 匹配 JSON object/array
_JSON_BLOCK = re.compile(r'(\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}|\[[^\[\]]*(?:\[[^\[\]]*\][^\[\]]*)*\])', re.DOTALL)


def extract_payload(raw: str) -> dict:
    """從 Agent 的原始輸出中擷取有用的 payload。

    Returns:
        {
            "code": [str, ...],     # 擷取到的程式碼區塊
            "json": [dict, ...],    # 擷取到的 JSON 物件
            "text": str,            # 清理後的純文字摘要（前 500 字）
            "raw_len": int,         # 原始長度
        }
    """
    # 清 ANSI
    clean = re.sub(r'\x1b\[[0-9;]*[a-zA-Z]', '', raw)
    clean = re.sub(r'[\x00-\x08\x0e-\x1f]', '', clean)

    result = {"code": [], "json": [], "text": "", "raw_len": len(raw)}

    # 擷取 code blocks
    for m in _CODE_BLOCK.finditer(clean):
        block = m.group(1).strip()
        if len(block) > 5:
            result["code"].append(block)

    # 擷取 JSON
    for m in _JSON_BLOCK.finditer(clean):
        try:
            obj = json.loads(m.group(1))
            result["json"].append(obj)
        except (json.JSONDecodeError, ValueError):
            pass

    # 清理文字：移除 code blocks，取前 500 字
    text = _CODE_BLOCK.sub('[CODE]', clean).strip()
    # 移除連續空行
    text = re.sub(r'\n{3,}', '\n\n', text)
    result["text"] = text[:500]

    return result


def format_for_target(source_id: str, payload: dict) -> str:
    """組合乾淨的 relay 訊息給下游 agent。

    優先送 json > code > text，避免重複。
    """
    parts = [f"[Input from {source_id}]"]

    if payload["json"]:
        parts.append("Data:")
        for obj in payload["json"][:3]:
            parts.append(json.dumps(obj, ensure_ascii=False, indent=2)[:1000])
    elif payload["code"]:
        parts.append("Code:")
        for block in payload["code"][:3]:
            parts.append(block[:1000])
    elif payload["text"]:
        parts.append(payload["text"])
    else:
        parts.append("(empty output)")

    return "\n".join(parts)


# ─── API 執行器 (Node D) ───

async def execute_api(payload: dict, config: dict) -> dict:
    """Node D 專用：收到格式化資料後發 HTTP request。

    config = {
        "url": "https://api.example.com/endpoint",
        "method": "POST",          # GET/POST/PUT/DELETE
        "headers": {"Authorization": "Bearer xxx"},
        "body_mode": "json",       # json / form / raw
    }

    Returns:
        {"status": int, "body": str, "ok": bool, "error": str|None}
    """
    import aiohttp

    url = config.get("url", "")
    method = config.get("method", "POST").upper()
    headers = config.get("headers", {})
    body_mode = config.get("body_mode", "json")

    if not url:
        return {"status": 0, "body": "", "ok": False, "error": "No URL configured"}

    # 從 payload 中取出要發送的資料
    body_data = None
    if payload.get("json"):
        body_data = payload["json"][0]  # 取第一個 JSON 物件
    elif payload.get("code"):
        body_data = payload["code"][0]
    elif payload.get("text"):
        body_data = payload["text"]

    try:
        async with aiohttp.ClientSession() as session:
            kwargs = {"headers": headers, "timeout": aiohttp.ClientTimeout(total=30)}

            if method in ("POST", "PUT", "PATCH"):
                if body_mode == "json" and isinstance(body_data, dict):
                    kwargs["json"] = body_data
                elif body_mode == "form" and isinstance(body_data, dict):
                    kwargs["data"] = body_data
                else:
                    kwargs["data"] = str(body_data) if body_data else ""

            async with session.request(method, url, **kwargs) as resp:
                resp_text = await resp.text()
                result = {
                    "status": resp.status,
                    "body": resp_text[:2000],
                    "ok": 200 <= resp.status < 300,
                    "error": None if 200 <= resp.status < 300 else f"HTTP {resp.status}",
                }
                logger.info(f"[Relay/API] {method} {url} → {resp.status}")
                return result

    except Exception as e:
        logger.error(f"[Relay/API] {method} {url} failed: {e}")
        return {"status": 0, "body": "", "ok": False, "error": str(e)}
