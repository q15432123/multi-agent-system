"""Tool Registry — 自動掃描 tools/ 子目錄的 manifest.json

也保留硬編碼的 BUILTIN_TOOLS 作為 fallback。
"""
import json
import logging
from pathlib import Path

logger = logging.getLogger("tools")

TOOLS_DIR = Path(__file__).parent

# 硬編碼的內建工具（fallback）
BUILTIN_TOOLS = [
    {"type": "function", "function": {"name": "write_file", "description": "Write content to a file in the agent's workspace. Creates directories as needed.", "parameters": {"type": "object", "properties": {"path": {"type": "string", "description": "Relative file path within the workspace"}, "content": {"type": "string", "description": "The full file content to write"}}, "required": ["path", "content"]}}},
    {"type": "function", "function": {"name": "read_file", "description": "Read the content of a file in the agent's workspace.", "parameters": {"type": "object", "properties": {"path": {"type": "string", "description": "Relative file path"}}, "required": ["path"]}}},
    {"type": "function", "function": {"name": "run_command", "description": "Execute a shell command in the agent's workspace. Runs non-interactively with a 60s timeout.", "parameters": {"type": "object", "properties": {"command": {"type": "string", "description": "Shell command to execute"}}, "required": ["command"]}}},
    {"type": "function", "function": {"name": "search_web", "description": "Search the web and return results.", "parameters": {"type": "object", "properties": {"query": {"type": "string", "description": "The search query"}}, "required": ["query"]}}},
    {"type": "function", "function": {"name": "call_api", "description": "Make an HTTP request to an external API endpoint.", "parameters": {"type": "object", "properties": {"url": {"type": "string", "description": "Full URL"}, "method": {"type": "string", "enum": ["GET", "POST", "PUT", "DELETE", "PATCH"]}, "body": {"type": "string", "description": "Request body (JSON string)"}}, "required": ["url", "method"]}}},
    {"type": "function", "function": {"name": "create_agent", "description": "Create a new specialized AI agent for a specific task. Only PM can use this.", "parameters": {"type": "object", "properties": {"task_description": {"type": "string", "description": "What this agent needs to do"}, "name_hint": {"type": "string", "description": "Suggested name for the agent"}}, "required": ["task_description"]}}},
    {"type": "function", "function": {"name": "mark_complete", "description": "Call this when your task is fully done. You MUST call this tool when finished.", "parameters": {"type": "object", "properties": {"summary": {"type": "string", "description": "Brief summary of what you accomplished"}}, "required": ["summary"]}}},
]

_cached_tools: list[dict] | None = None


def _scan_manifests() -> list[dict]:
    """掃描 tools/ 子目錄的 manifest.json"""
    tools = []
    seen_names = set()

    for manifest_path in TOOLS_DIR.glob("*/manifest.json"):
        try:
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
            for tool in data.get("tools", []):
                name = tool.get("function", {}).get("name", "")
                if name and name not in seen_names:
                    tools.append(tool)
                    seen_names.add(name)
        except Exception as e:
            logger.warning(f"[Registry] Failed to load {manifest_path}: {e}")

    return tools


def get_tool_schemas() -> list[dict]:
    """回傳所有工具的 schema（傳給 LLM API 的 tools 參數）"""
    global _cached_tools
    if _cached_tools is not None:
        return _cached_tools

    # 先掃描子目錄
    tools = _scan_manifests()

    # 沒掃到就用 builtin
    if not tools:
        tools = BUILTIN_TOOLS

    # 補上 builtin 中沒有的
    seen = {t["function"]["name"] for t in tools}
    for bt in BUILTIN_TOOLS:
        if bt["function"]["name"] not in seen:
            tools.append(bt)

    _cached_tools = tools
    logger.info(f"[Registry] Loaded {len(tools)} tools")
    return tools


def get_tool_names() -> list[str]:
    """回傳所有工具名稱"""
    return [t["function"]["name"] for t in get_tool_schemas()]


def reload():
    """強制重新掃描"""
    global _cached_tools
    _cached_tools = None
    return get_tool_schemas()
