"""DNA Generator — 用 LLM 根據任務需求自動產生新 Agent DNA"""
import json
import logging
from datetime import datetime, timezone

from engine import llm_client
from engine.tools.registry import get_tool_names

logger = logging.getLogger("dna")

GENERATOR_PROMPT = """You are an AI architect. Based on the task below, design a new AI agent.

Available tools: {tools}

Return ONLY valid JSON (no markdown, no explanation):
{{
  "name": "snake_case_name",
  "description": "one line description",
  "model": "gemini-2.5-flash",
  "provider": "openrouter",
  "skills": ["skill1", "skill2"],
  "tools": ["tool_from_available_list"],
  "prompt": "You are a senior ... your job is to ..."
}}"""


async def generate_dna(task_description: str, requested_by: str = "system") -> dict:
    """輸入任務描述，LLM 產出一份 Agent DNA spec"""
    available_tools = get_tool_names()

    prompt = GENERATOR_PROMPT.format(tools=json.dumps(available_tools))
    user_msg = f"Task: {task_description}"

    model = llm_client.resolve_model(["claude"])
    result = llm_client.chat(
        model=model,
        messages=[
            {"role": "system", "content": prompt},
            {"role": "user", "content": user_msg},
        ],
        temperature=0.3,
        max_tokens=1000,
        agent_id="dna_generator",
    )

    if not result.get("ok"):
        raise RuntimeError(f"DNA generation failed: {result.get('error')}")

    text = result["content"].strip()

    # 清理 markdown code fence
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0]

    # 嘗試提取 JSON
    import re
    m = re.search(r'\{.*\}', text, re.DOTALL)
    if m:
        text = m.group()

    dna = json.loads(text)

    # 補上 metadata
    dna["created_by"] = requested_by
    dna["created_at"] = datetime.now(timezone.utc).isoformat()
    dna["score"] = None
    dna["usage_count"] = 0

    # 驗證 tools 只包含可用的
    dna["tools"] = [t for t in dna.get("tools", []) if t in available_tools]

    # 確保 name 是 snake_case
    dna["name"] = dna.get("name", "agent").lower().replace(" ", "_").replace("-", "_")

    logger.info(f"[DNA] Generated: {dna['name']} — {dna.get('description', '')[:60]}")
    return dna
