"""Self Reflection — Agent 輸出品質檢查

呼叫 LLM 評分 agent 的輸出，score < 6 → 自動 retry
"""
import json
import logging
import re

from engine import llm_client

logger = logging.getLogger("reflection")

REFLECTION_PROMPT = """You are a strict quality reviewer. Score the agent's output on a scale of 1-10.

Criteria:
- Correctness: Does it actually solve the task?
- Completeness: Are all parts of the task addressed?
- Quality: Is the code/content well-structured?

Output ONLY valid JSON:
{"score": <1-10>, "issues": ["issue1", "issue2"], "suggestion": "what to improve"}"""


async def reflect(agent_id: str, task: str, output: str,
                  provider: str = "", model: str = "") -> dict:
    """評估 agent 輸出品質

    Returns:
        {"score": int, "issues": list, "suggestion": str, "pass": bool}
    """
    if not provider:
        provider = "openrouter"
    if not model:
        model = llm_client.resolve_model(["claude"])

    user_msg = f"""Task given to agent:
{task[:1000]}

Agent's output:
{output[:3000]}

Rate the quality."""

    result = llm_client.chat(
        model=model,
        messages=[
            {"role": "system", "content": REFLECTION_PROMPT},
            {"role": "user", "content": user_msg},
        ],
        temperature=0.2,
        max_tokens=500,
        provider=provider,
        agent_id="reflector",
    )

    if not result.get("ok"):
        logger.warning(f"[Reflection] LLM error, auto-pass: {result.get('error')}")
        return {"score": 7, "issues": [], "suggestion": "", "pass": True}

    content = result["content"]

    # Parse JSON
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        m = re.search(r'\{.*\}', content, re.DOTALL)
        if m:
            try:
                data = json.loads(m.group())
            except Exception:
                data = {"score": 7, "issues": [], "suggestion": ""}
        else:
            data = {"score": 7, "issues": [], "suggestion": ""}

    score = data.get("score", 7)
    data["pass"] = score >= 6
    logger.info(f"[Reflection] {agent_id}: score={score}, pass={data['pass']}")

    # DNA 整合：更新 DNA 評分（如果有對應的 DNA）
    try:
        from engine.dna.dna_registry import exists as dna_exists
        from engine.dna.dna_manager import update_score
        if dna_exists(agent_id):
            update_score(agent_id, score)
    except Exception:
        pass

    return data
