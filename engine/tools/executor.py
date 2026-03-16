"""Tool Executor — 每個工具的實作"""
import json
import logging
import os
from pathlib import Path

logger = logging.getLogger("tools")

WORKSPACE_ROOT = Path(__file__).parent.parent.parent / "_workspaces"


async def execute_tool(tool_name: str, args: dict, agent_id: str) -> dict:
    """執行工具，回傳結果。

    Returns:
        {"tool_name": str, "result": str, "success": bool}
    """
    from engine.syslog import syslog

    try:
        if tool_name == "write_file":
            result = _write_file(agent_id, args.get("path", ""), args.get("content", ""))
        elif tool_name == "read_file":
            result = _read_file(agent_id, args.get("path", ""))
        elif tool_name == "run_command":
            result = _run_command(agent_id, args.get("command", ""))
        elif tool_name == "search_web":
            result = await _search_web(args.get("query", ""))
        elif tool_name == "call_api":
            result = await _call_api(args.get("url", ""), args.get("method", "GET"), args.get("body", ""))
        elif tool_name.startswith("git_"):
            from engine.tools.git.executor import run as git_run
            result = await git_run(tool_name, args, agent_id)
        elif tool_name == "create_agent":
            result = await _create_agent(agent_id, args)
        elif tool_name == "mark_complete":
            summary = args.get("summary", "Task completed")
            result = {"tool_name": "mark_complete", "result": summary, "success": True, "is_complete": True}
        else:
            result = {"tool_name": tool_name, "result": f"Unknown tool: {tool_name}", "success": False}

        syslog.info(agent_id, "TOOL_EXEC",
                    f"{tool_name} → {'OK' if result['success'] else 'FAIL'}",
                    extra={"tool": tool_name, "args_preview": str(args)[:100]})
        return result

    except Exception as e:
        logger.error(f"[Tool] {tool_name} error: {e}")
        syslog.error(agent_id, "TOOL_ERROR", f"{tool_name}: {e}")
        return {"tool_name": tool_name, "result": str(e), "success": False}


# ─── 工具實作 ───

def _write_file(agent_id: str, path: str, content: str) -> dict:
    ws = WORKSPACE_ROOT / agent_id
    ws.mkdir(parents=True, exist_ok=True)
    target = (ws / path).resolve()

    # 安全檢查
    if not str(target).startswith(str(ws.resolve())):
        return {"tool_name": "write_file", "result": "Path traversal blocked", "success": False}

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    logger.info(f"[Tool] write_file: {path} ({len(content)} chars) → {agent_id}")
    return {"tool_name": "write_file", "result": f"Written {len(content)} chars to {path}", "success": True}


def _read_file(agent_id: str, path: str) -> dict:
    ws = WORKSPACE_ROOT / agent_id
    target = (ws / path).resolve()

    if not str(target).startswith(str(ws.resolve())):
        return {"tool_name": "read_file", "result": "Path traversal blocked", "success": False}

    if not target.exists():
        return {"tool_name": "read_file", "result": f"File not found: {path}", "success": False}

    try:
        content = target.read_text(encoding="utf-8")
        if len(content) > 10000:
            content = content[:10000] + f"\n... (truncated, total {len(content)} chars)"
        return {"tool_name": "read_file", "result": content, "success": True}
    except UnicodeDecodeError:
        return {"tool_name": "read_file", "result": f"Binary file: {path}", "success": False}


def _run_command(agent_id: str, command: str) -> dict:
    from engine.cli_executor import run as cli_run
    result = cli_run(cmd=command, agent_id=agent_id, timeout=60)
    output = result["stdout"]
    if result["stderr"]:
        output += "\nSTDERR:\n" + result["stderr"]
    if len(output) > 5000:
        output = output[:5000] + "\n... (truncated)"
    return {"tool_name": "run_command", "result": output or "(no output)", "success": result["ok"]}


async def _search_web(query: str) -> dict:
    import aiohttp
    # DuckDuckGo Instant Answer API (no key needed)
    url = "https://api.duckduckgo.com/"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params={"q": query, "format": "json", "no_html": "1"},
                                   timeout=aiohttp.ClientTimeout(total=10)) as resp:
                data = await resp.json(content_type=None)
                abstract = data.get("AbstractText", "")
                results = []
                for r in data.get("RelatedTopics", [])[:5]:
                    if isinstance(r, dict) and "Text" in r:
                        results.append(r["Text"])
                text = abstract or "\n".join(results) or "No results found."
                return {"tool_name": "search_web", "result": text[:3000], "success": True}
    except Exception as e:
        return {"tool_name": "search_web", "result": str(e), "success": False}


async def _create_agent(caller_id: str, args: dict) -> dict:
    """create_agent tool — 只有 PM 可以用"""
    # 檢查是否為 PM
    team_dir = Path(__file__).parent.parent.parent / "_team"
    caller_md = team_dir / f"{caller_id}.md"
    is_pm = False
    if caller_md.exists():
        text = caller_md.read_text(encoding="utf-8").lower()[:300]
        is_pm = "pm" in text

    if not is_pm:
        return {"tool_name": "create_agent", "result": "Only PM can create agents", "success": False}

    # 檢查數量限制
    existing = len(list(team_dir.glob("*.md"))) - 1
    if existing >= 20:
        return {"tool_name": "create_agent", "result": f"Agent limit reached ({existing}/20)", "success": False}

    task_desc = args.get("task_description", "")
    name_hint = args.get("name_hint", "")

    try:
        from engine.dna.dna_generator import generate_dna
        from engine.dna.dna_manager import save_dna
        from engine.workspace import ensure_workspace
        from engine.dispatcher import dispatcher

        dna = await generate_dna(task_desc, requested_by=caller_id)
        if name_hint:
            dna["name"] = name_hint.lower().replace(" ", "_").replace("-", "_")

        # 防重複
        if (team_dir / f"{dna['name']}.md").exists():
            return {"tool_name": "create_agent", "result": f"Agent {dna['name']} already exists", "success": False}

        save_dna(dna)

        # 建立 agent .md
        name = dna["name"]
        tags = dna.get("skills", []) + [dna.get("provider", "openrouter")]
        md_content = f"""---\nname: "{name}"\nrole: "{dna.get('description','')}"\ntags: [{', '.join(tags)}]\n---\n\n# {name}\n\n{dna.get('prompt', f'You are {name}.')}\n"""
        (team_dir / f"{name}.md").write_text(md_content, encoding="utf-8")
        ensure_workspace(name)
        dispatcher.connect(caller_id, name)

        return {"tool_name": "create_agent",
                "result": f"Created agent '{name}' with skills: {dna.get('skills',[])}",
                "success": True}
    except Exception as e:
        return {"tool_name": "create_agent", "result": str(e), "success": False}


async def _call_api(url: str, method: str, body: str) -> dict:
    import aiohttp
    try:
        async with aiohttp.ClientSession() as session:
            kwargs = {"timeout": aiohttp.ClientTimeout(total=30)}
            if body and method in ("POST", "PUT", "PATCH"):
                try:
                    kwargs["json"] = json.loads(body)
                except json.JSONDecodeError:
                    kwargs["data"] = body

            async with session.request(method, url, **kwargs) as resp:
                text = await resp.text()
                ok = 200 <= resp.status < 300
                result = f"HTTP {resp.status}\n{text[:3000]}"
                return {"tool_name": "call_api", "result": result, "success": ok}
    except Exception as e:
        return {"tool_name": "call_api", "result": str(e), "success": False}
