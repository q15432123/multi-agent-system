"""Graph Store — JSON-based knowledge graph

簡單的三元組存儲：(subject, predicate, object)
例如：("alex", "built", "login API")
存儲：_workspaces/{agent_id}/memory/graph.json
"""
import json
import logging
from pathlib import Path

logger = logging.getLogger("memory")

WORKSPACE_ROOT = Path(__file__).parent.parent.parent / "_workspaces"


def _graph_path(agent_id: str) -> Path:
    p = WORKSPACE_ROOT / agent_id / "memory"
    p.mkdir(parents=True, exist_ok=True)
    return p / "graph.json"


def _load_graph(agent_id: str) -> list[dict]:
    path = _graph_path(agent_id)
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []


def _save_graph(agent_id: str, triples: list[dict]) -> None:
    path = _graph_path(agent_id)
    path.write_text(json.dumps(triples, ensure_ascii=False, indent=2), encoding="utf-8")


def add_relation(agent_id: str, subject: str, predicate: str, obj: str) -> dict:
    """新增三元組"""
    triples = _load_graph(agent_id)

    # 防重複
    for t in triples:
        if t["s"] == subject and t["p"] == predicate and t["o"] == obj:
            return {"added": False, "reason": "duplicate"}

    triples.append({"s": subject, "p": predicate, "o": obj})
    if len(triples) > 500:
        triples = triples[-500:]
    _save_graph(agent_id, triples)
    return {"added": True}


def query_relations(agent_id: str, subject: str = "", predicate: str = "",
                    obj: str = "") -> list[dict]:
    """查詢三元組"""
    triples = _load_graph(agent_id)
    results = []
    for t in triples:
        if subject and t["s"].lower() != subject.lower():
            continue
        if predicate and t["p"].lower() != predicate.lower():
            continue
        if obj and t["o"].lower() != obj.lower():
            continue
        results.append(t)
    return results


def get_all(agent_id: str) -> list[dict]:
    """取得所有三元組"""
    return _load_graph(agent_id)
