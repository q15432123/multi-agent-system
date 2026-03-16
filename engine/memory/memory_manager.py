"""Memory Manager — Agent 記憶儲存/檢索

category: episodic（事件）, semantic（知識）, tasks（任務歷史）
存儲：_workspaces/{agent_id}/memory/{category}.json
"""
import json
import logging
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger("memory")

WORKSPACE_ROOT = Path(__file__).parent.parent.parent / "_workspaces"


def _memory_path(agent_id: str, category: str) -> Path:
    p = WORKSPACE_ROOT / agent_id / "memory"
    p.mkdir(parents=True, exist_ok=True)
    return p / f"{category}.json"


def _load(agent_id: str, category: str) -> list[dict]:
    path = _memory_path(agent_id, category)
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []


def _save(agent_id: str, category: str, entries: list[dict]) -> None:
    path = _memory_path(agent_id, category)
    path.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")


async def store(agent_id: str, category: str, content: str, tags: list[str] | None = None) -> dict:
    """儲存一筆記憶

    Args:
        agent_id: agent ID
        category: episodic | semantic | tasks
        content: 記憶內容
        tags: 可選標籤

    Returns:
        {"stored": True, "index": int}
    """
    entries = _load(agent_id, category)
    entry = {
        "timestamp": time.time(),
        "content": content[:5000],
        "tags": tags or [],
    }
    entries.append(entry)

    # 限制每個 category 最多 200 筆
    if len(entries) > 200:
        entries = entries[-200:]

    _save(agent_id, category, entries)
    logger.info(f"[Memory] Stored {category} for {agent_id} ({len(content)} chars)")
    return {"stored": True, "index": len(entries) - 1}


async def retrieve(agent_id: str, category: str,
                   query: str = "", limit: int = 10) -> list[dict]:
    """檢索記憶

    Args:
        query: keyword 搜尋（空字串 = 回傳最近的）
        limit: 最多回傳幾筆
    """
    entries = _load(agent_id, category)

    if query:
        # Keyword matching（未來可換成 vector search）
        q_lower = query.lower()
        scored = []
        for e in entries:
            text = (e.get("content", "") + " ".join(e.get("tags", []))).lower()
            score = sum(1 for word in q_lower.split() if word in text)
            if score > 0:
                scored.append((score, e))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [e for _, e in scored[:limit]]
    else:
        return entries[-limit:]


async def list_categories(agent_id: str) -> list[str]:
    """列出 agent 有哪些 memory category"""
    mem_dir = WORKSPACE_ROOT / agent_id / "memory"
    if not mem_dir.exists():
        return []
    return [f.stem for f in mem_dir.glob("*.json")]
