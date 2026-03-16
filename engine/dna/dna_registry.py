"""DNA Registry — 啟動時載入所有 DNA，提供查詢介面"""
import json
import logging
from pathlib import Path

logger = logging.getLogger("dna")

DNA_DIR = Path(__file__).parent.parent.parent / "_agent_dna"
_registry: dict[str, dict] = {}


def load_all() -> int:
    """掃描 _agent_dna/*.json 載入到 registry"""
    DNA_DIR.mkdir(exist_ok=True)
    _registry.clear()
    count = 0
    for f in DNA_DIR.glob("*.json"):
        try:
            dna = json.loads(f.read_text(encoding="utf-8"))
            name = dna.get("name", f.stem)
            _registry[name] = dna
            count += 1
        except Exception as e:
            logger.warning(f"[DNA] Failed to load {f.name}: {e}")
    logger.info(f"[DNA] Registry loaded: {count} DNA profiles")
    return count


def get(name: str) -> dict | None:
    """取得單一 DNA"""
    if not _registry:
        load_all()
    return _registry.get(name)


def list_all() -> list[dict]:
    """列出所有 DNA"""
    if not _registry:
        load_all()
    return list(_registry.values())


def exists(name: str) -> bool:
    """檢查 DNA 是否存在"""
    if not _registry:
        load_all()
    return name in _registry
