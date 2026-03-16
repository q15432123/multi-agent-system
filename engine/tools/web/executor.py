"""Web tool executor"""
from engine.tools.executor import execute_tool

async def run(tool_name: str, args: dict, agent_id: str) -> dict:
    return await execute_tool(tool_name, args, agent_id)
