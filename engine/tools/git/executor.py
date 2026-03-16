"""Git tool executor"""
from engine.cli_executor import run as cli_run


async def run(tool_name: str, args: dict, agent_id: str) -> dict:
    if tool_name == "git_status":
        r = cli_run("git status", agent_id=agent_id)
        return {"tool_name": "git_status", "result": r["stdout"] or r["stderr"], "success": r["ok"]}

    elif tool_name == "git_commit":
        msg = args.get("message", "auto commit")
        # Stage all + commit
        cli_run("git add -A", agent_id=agent_id)
        r = cli_run(f'git commit -m "{msg}"', agent_id=agent_id)
        return {"tool_name": "git_commit", "result": r["stdout"] or r["stderr"], "success": r["ok"]}

    elif tool_name == "git_diff":
        r = cli_run("git diff", agent_id=agent_id)
        output = r["stdout"][:5000] if r["stdout"] else "(no diff)"
        return {"tool_name": "git_diff", "result": output, "success": r["ok"]}

    return {"tool_name": tool_name, "result": "Unknown git tool", "success": False}
