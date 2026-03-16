"""Tool Router — 解析 LLM response 的 tool_calls → 執行 → 回傳結果"""
import json
import logging
from typing import Optional

from engine.tools.executor import execute_tool

logger = logging.getLogger("tools")


async def route_tool_calls(tool_calls: list, agent_id: str) -> list[dict]:
    """解析並執行所有 tool_calls，回傳結果 messages。

    Args:
        tool_calls: OpenAI API 回傳的 tool_calls 列表
        agent_id: 執行工具的 agent ID

    Returns:
        list of {"role": "tool", "tool_call_id": ..., "content": ...}
        可直接 append 到 messages 陣列送回 LLM
    """
    results = []

    for tc in tool_calls:
        fn_name = tc.function.name
        call_id = tc.id

        # 解析參數
        try:
            args = json.loads(tc.function.arguments) if tc.function.arguments else {}
        except json.JSONDecodeError:
            args = {}

        logger.info(f"[Router] {agent_id} → {fn_name}({json.dumps(args, ensure_ascii=False)[:80]})")

        # 執行
        result = await execute_tool(fn_name, args, agent_id)

        # 組裝回傳 message
        results.append({
            "role": "tool",
            "tool_call_id": call_id,
            "content": result["result"][:4000],
        })

    return results


def parse_tool_calls_from_text(text: str) -> list[dict]:
    """Fallback：從純文字中解析 tool call 格式（非 function calling 的模型用）

    偵測格式：
        <tool>write_file</tool>
        <args>{"path": "x.py", "content": "..."}</args>

    或：
        ```tool:write_file
        {"path": "x.py", "content": "..."}
        ```
    """
    import re

    calls = []

    # Pattern 1: <tool>name</tool><args>json</args>
    for m in re.finditer(r'<tool>(\w+)</tool>\s*<args>(.*?)</args>', text, re.DOTALL):
        name = m.group(1)
        try:
            args = json.loads(m.group(2).strip())
        except Exception:
            args = {"raw": m.group(2).strip()}
        calls.append({"name": name, "args": args})

    # Pattern 2: ```tool:name\njson\n```
    for m in re.finditer(r'```tool:(\w+)\n(.*?)```', text, re.DOTALL):
        name = m.group(1)
        try:
            args = json.loads(m.group(2).strip())
        except Exception:
            args = {"raw": m.group(2).strip()}
        calls.append({"name": name, "args": args})

    return calls
