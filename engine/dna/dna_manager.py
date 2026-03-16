"""DNA Manager — 保存、刪除、評分、淘汰 DNA"""
import json
import logging
from pathlib import Path

from engine.dna.dna_registry import DNA_DIR, load_all

logger = logging.getLogger("dna")


def save_dna(dna: dict) -> str:
    """存 DNA 到 _agent_dna/，回傳路徑"""
    DNA_DIR.mkdir(exist_ok=True)
    name = dna.get("name", "unnamed")
    path = DNA_DIR / f"{name}.json"
    path.write_text(json.dumps(dna, indent=2, ensure_ascii=False), encoding="utf-8")
    load_all()  # 重新載入 registry
    logger.info(f"[DNA] Saved: {name}")
    return str(path)


def delete_dna(name: str) -> bool:
    """刪除 DNA"""
    path = DNA_DIR / f"{name}.json"
    if path.exists():
        path.unlink()
        load_all()
        logger.info(f"[DNA] Deleted: {name}")
        return True
    return False


def update_score(name: str, score: float) -> None:
    """更新 DNA 的評分（來自 Reflection）— 加權平均"""
    path = DNA_DIR / f"{name}.json"
    if not path.exists():
        return
    try:
        dna = json.loads(path.read_text(encoding="utf-8"))
        old = dna.get("score")
        dna["score"] = score if old is None else round(old * 0.7 + score * 0.3, 2)
        dna["usage_count"] = dna.get("usage_count", 0) + 1
        path.write_text(json.dumps(dna, indent=2, ensure_ascii=False), encoding="utf-8")
        logger.info(f"[DNA] Score updated: {name} → {dna['score']} (uses={dna['usage_count']})")
    except Exception as e:
        logger.error(f"[DNA] Score update failed for {name}: {e}")


def garbage_collect(min_score: float = 4.0, min_uses: int = 3) -> list[str]:
    """淘汰低分 DNA — 用過 min_uses 次且平均分低於 min_score → 移到 _archived/"""
    archive = DNA_DIR / "_archived"
    archive.mkdir(exist_ok=True)

    removed = []
    for f in DNA_DIR.glob("*.json"):
        try:
            dna = json.loads(f.read_text(encoding="utf-8"))
            uses = dna.get("usage_count", 0)
            score = dna.get("score")
            if uses >= min_uses and score is not None and score < min_score:
                f.rename(archive / f.name)
                removed.append(dna["name"])
                logger.info(f"[DNA] Archived: {dna['name']} (score={score}, uses={uses})")
        except Exception:
            pass

    if removed:
        load_all()
    return removed
