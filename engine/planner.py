"""Planner — 把使用者目標拆成 subtask list

輸入：使用者目標 + 可用 agent 清單
輸出：[{"task": "...", "agent": "agent_id", "depends_on": ["task_id", ...]}, ...]
"""
import json
import logging
from pathlib import Path

from engine import llm_client

logger = logging.getLogger("planner")

TEAM_DIR = Path(__file__).parent.parent / "_team"

PLANNER_SYSTEM_PROMPT = """You are a project planner. Given a goal and a list of available agents, break the goal into concrete subtasks.

Rules:
- Each subtask must be assigned to exactly one agent
- Use depends_on to express task ordering (task can only start after its dependencies complete)
- Keep tasks small and specific — each should take one LLM call to complete
- task_id must be a short unique string (e.g. "t1", "t2")
- Output ONLY valid JSON, no markdown, no explanation

Output format:
[
  {"task_id": "t1", "task": "description", "agent": "agent_id", "depends_on": []},
  {"task_id": "t2", "task": "description", "agent": "agent_id", "depends_on": ["t1"]},
]"""


def _get_available_agents() -> list[dict]:
    """讀取 _team/ 取得所有可用 agent"""
    agents = []
    for f in TEAM_DIR.glob("*.md"):
        if f.name.startswith("_"):
            continue
        name = f.stem
        role = ""
        tags = []
        try:
            text = f.read_text(encoding="utf-8")
            if text.startswith("---"):
                parts = text.split("---", 2)
                if len(parts) >= 3:
                    for line in parts[1].strip().split("\n"):
                        if ":" in line:
                            k, v = line.split(":", 1)
                            k, v = k.strip(), v.strip().strip('"')
                            if k == "role":
                                role = v
                            if k == "tags":
                                tags = [t.strip() for t in v.strip("[]").split(",") if t.strip()]
        except Exception:
            pass
        if "pm" not in tags and "mock" not in tags:
            agents.append({"id": name, "role": role, "tags": tags})
    return agents


async def plan(goal: str, provider: str = "", model: str = "") -> list[dict]:
    """把目標拆成 subtask list

    Returns:
        [{"task_id": "t1", "task": "...", "agent": "...", "depends_on": []}, ...]
    """
    agents = _get_available_agents()
    if not agents:
        return [{"task_id": "t1", "task": goal, "agent": "unknown", "depends_on": []}]

    agent_list = "\n".join([f"- {a['id']}: {a['role']} (tags: {', '.join(a['tags'])})" for a in agents])

    user_msg = f"""Goal: {goal}

Available agents:
{agent_list}

Break this goal into subtasks. Assign each to the most suitable agent. Use depends_on for ordering."""

    # 用 sync chat（planner 不需要串流）
    if not provider:
        provider = "openrouter"
    if not model:
        model = llm_client.resolve_model(["claude"])

    result = llm_client.chat(
        model=model,
        messages=[
            {"role": "system", "content": PLANNER_SYSTEM_PROMPT},
            {"role": "user", "content": user_msg},
        ],
        temperature=0.3,
        max_tokens=2000,
        provider=provider,
        agent_id="planner",
    )

    if not result.get("ok"):
        logger.error(f"[Planner] LLM error: {result.get('error')}")
        return [{"task_id": "t1", "task": goal, "agent": agents[0]["id"], "depends_on": []}]

    # 解析 JSON
    content = result["content"]
    try:
        # 嘗試直接解析
        tasks = json.loads(content)
        if isinstance(tasks, list):
            logger.info(f"[Planner] Generated {len(tasks)} subtasks")
            return tasks
    except json.JSONDecodeError:
        pass

    # Fallback: 從回覆中提取 JSON array
    import re
    m = re.search(r'\[.*\]', content, re.DOTALL)
    if m:
        try:
            tasks = json.loads(m.group())
            if isinstance(tasks, list):
                logger.info(f"[Planner] Extracted {len(tasks)} subtasks from response")
                return tasks
        except json.JSONDecodeError:
            pass

    logger.warning("[Planner] Failed to parse subtasks, returning single task")
    return [{"task_id": "t1", "task": goal, "agent": agents[0]["id"], "depends_on": []}]


async def execute_plan(tasks: list[dict]) -> None:
    """按依賴順序執行 plan"""
    from engine.dispatcher import dispatcher

    completed = set()
    pending = list(tasks)

    while pending:
        ready = [t for t in pending if all(d in completed for d in t.get("depends_on", []))]
        if not ready:
            logger.warning("[Planner] Deadlock: no tasks ready but pending remain")
            break

        for task in ready:
            tid = task["task_id"]
            agent = task["agent"]
            content = task["task"]
            dispatcher.on_task_dispatch("planner", agent, content)
            completed.add(tid)
            pending.remove(task)
            logger.info(f"[Planner] Dispatched {tid} → {agent}: {content[:60]}")
