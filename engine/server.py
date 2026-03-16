"""Multi-Agent-System Engine — 本地 Web Server + Runner
用戶從 git clone 下來，pip install，python run.py 就能用。
"""
import asyncio
import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
import uvicorn

logging.basicConfig(format="%(asctime)s [%(levelname)s] %(message)s", level=logging.INFO)
logger = logging.getLogger("mas")

# ─── 路徑 ───
BASE_DIR = Path(__file__).parent.parent
INBOX = BASE_DIR / "_inbox"
OUTPUT = BASE_DIR / "_output"
REVIEW = BASE_DIR / "_review"
ARCHIVE = BASE_DIR / "_archive"
TEAM = BASE_DIR / "_team"
MODELS = BASE_DIR / "_models"
PM_DIR = BASE_DIR / "_pm"
CONFIG = BASE_DIR / "_config"

for d in [INBOX, OUTPUT, REVIEW, ARCHIVE, TEAM, MODELS, PM_DIR, CONFIG]:
    d.mkdir(exist_ok=True)

app = FastAPI(title="Multi-Agent-System", version="0.1.0")


# ═══════════════════════════════════════
# API Models
# ═══════════════════════════════════════

class BriefRequest(BaseModel):
    title: str
    content: str

class PMResponse(BaseModel):
    action: str  # approve / reject / revise
    task_id: str = ""
    message: str = ""

class AgentCreate(BaseModel):
    name: str
    role: str
    tags: list[str] = []
    prompt: str = ""

class ModelCreate(BaseModel):
    name: str
    provider: str
    model_id: str
    api_key_env: str = ""
    endpoint: str = ""
    tags: list[str] = []


# ═══════════════════════════════════════
# 檔案操作
# ═══════════════════════════════════════

def read_md(path: Path) -> dict:
    """讀 markdown 檔案，解析 frontmatter"""
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8")
    meta = {}
    body = text

    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) >= 3:
            for line in parts[1].strip().split("\n"):
                if ":" in line:
                    k, v = line.split(":", 1)
                    meta[k.strip()] = v.strip().strip('"')
            body = parts[2].strip()

    meta["_body"] = body
    meta["_path"] = str(path)
    return meta


def list_agents() -> list[dict]:
    """列出所有 Agent"""
    agents = []
    for f in TEAM.glob("*.md"):
        if f.name.startswith("_"):
            continue
        data = read_md(f)
        data["filename"] = f.stem
        agents.append(data)
    return agents


def list_models() -> list[dict]:
    """列出所有模型"""
    models = []
    for f in MODELS.glob("*.md"):
        if f.name.startswith("_"):
            continue
        data = read_md(f)
        data["filename"] = f.stem
        models.append(data)
    return models


def list_tasks(folder: Path) -> list[dict]:
    """列出資料夾裡的所有任務"""
    tasks = []
    for f in sorted(folder.glob("*.md"), key=lambda x: x.stat().st_mtime, reverse=True):
        if f.name.startswith("_"):
            continue
        data = read_md(f)
        data["filename"] = f.stem
        data["modified"] = datetime.fromtimestamp(f.stat().st_mtime).isoformat()
        tasks.append(data)
    return tasks


def get_status_board() -> dict:
    """讀 PM status board"""
    path = PM_DIR / "status-board.md"
    if path.exists():
        return {"content": path.read_text(encoding="utf-8")}
    return {"content": "No active projects."}


# ═══════════════════════════════════════
# API Routes
# ═══════════════════════════════════════

# ─── Dashboard ───

@app.get("/")
async def index(request: Request):
    """首次使用 → setup 頁面；已設定 → 主 UI"""
    # 如果 URL 有 ?ready=1 → 直接進主 UI
    if request.query_params.get("ready"):
        return FileResponse(BASE_DIR / "ui.html")
    # 檢查是否有任何 provider ready
    try:
        from engine.llm_proxy import token_store
        status = token_store.get_status()
        has_ready = any(v.get("ready") for v in status.values())
        if has_ready:
            return FileResponse(BASE_DIR / "ui.html")
    except Exception:
        pass
    # 無 provider → setup 頁面
    setup_file = BASE_DIR / "setup.html"
    if setup_file.exists():
        return FileResponse(setup_file)
    return FileResponse(BASE_DIR / "ui.html")


@app.get("/api/dashboard")
async def dashboard():
    """首頁統計"""
    return {
        "agents": len(list_agents()),
        "models": len(list_models()),
        "inbox": len(list_tasks(INBOX)),
        "output": len(list_tasks(OUTPUT)),
        "review": len(list_tasks(REVIEW)),
    }


# ─── Agents (Team) ───

@app.get("/api/agents")
async def get_agents():
    return list_agents()


@app.post("/api/agents")
async def create_agent(req: AgentCreate):
    """新增 Agent"""
    filename = req.name.lower().replace(" ", "-")
    path = TEAM / f"{filename}.md"

    tags_str = ", ".join(req.tags) if req.tags else ""

    content = f"""---
name: "{req.name}"
role: "{req.role}"
tags: [{tags_str}]
---

# {req.name}

## Identity
{req.prompt or f'You are {req.name}, a {req.role}.'}

## Capabilities
- {req.role}

## Tags
{tags_str}

## Input
Read tasks from `_inbox/` assigned to you.

## Output
Write results to `_output/task-{{id}}/`

## Work Process
1. Read the task file
2. Understand requirements
3. Do the work
4. Write output
5. Mark task as complete
"""
    path.write_text(content, encoding="utf-8")
    return {"status": "ok", "filename": filename}


@app.delete("/api/agents/{name}")
async def delete_agent(name: str):
    path = TEAM / f"{name}.md"
    if path.exists():
        path.unlink()
    return {"status": "ok"}


# ─── Models ───

@app.get("/api/models")
async def get_models():
    return list_models()


@app.post("/api/models")
async def create_model(req: ModelCreate):
    filename = req.name.lower().replace(" ", "-")
    path = MODELS / f"{filename}.md"

    tags_str = ", ".join(req.tags)
    content = f"""---
name: "{req.name}"
provider: "{req.provider}"
model_id: "{req.model_id}"
api_key_env: "{req.api_key_env}"
endpoint: "{req.endpoint}"
tags: [{tags_str}]
max_tokens: 4096
---

# {req.name}

Provider: {req.provider}
Model: {req.model_id}
Tags: {tags_str}
"""
    path.write_text(content, encoding="utf-8")
    return {"status": "ok", "filename": filename}


# ─── Inbox (Briefs & Tasks) ───

@app.get("/api/inbox")
async def get_inbox():
    return list_tasks(INBOX)


@app.post("/api/inbox")
async def create_brief(req: BriefRequest):
    """用戶提交需求"""
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    filename = f"brief-{ts}"
    path = INBOX / f"{filename}.md"

    content = f"""---
type: brief
title: "{req.title}"
status: pending
created: "{datetime.now().isoformat()}"
---

# {req.title}

{req.content}
"""
    path.write_text(content, encoding="utf-8")
    return {"status": "ok", "filename": filename}


# ─── Output ───

@app.get("/api/output")
async def get_output():
    return list_tasks(OUTPUT)


@app.get("/api/output/{filename}")
async def get_output_file(filename: str):
    path = OUTPUT / f"{filename}.md"
    if not path.exists():
        # 也查子資料夾
        for f in OUTPUT.rglob(f"{filename}.md"):
            path = f
            break
    if not path.exists():
        raise HTTPException(404)
    return {"content": path.read_text(encoding="utf-8"), "filename": filename}


# ─── Review ───

@app.get("/api/review")
async def get_review():
    return list_tasks(REVIEW)


# ─── PM ───

@app.get("/api/pm/prompt")
async def get_pm_prompt():
    """取得 PM 的完整 prompt"""
    path = PM_DIR / "PM.md"
    if path.exists():
        return {"prompt": path.read_text(encoding="utf-8")}
    return {"prompt": ""}


@app.get("/api/pm/status")
async def get_pm_status():
    return get_status_board()


@app.post("/api/pm/approve")
async def pm_approve(req: PMResponse):
    """PM 確認/退回任務"""
    if req.action == "approve":
        # 把 brief 狀態改成 approved
        for f in INBOX.glob("*.md"):
            if req.task_id in f.stem:
                text = f.read_text(encoding="utf-8")
                text = text.replace("status: pending", "status: approved")
                f.write_text(text, encoding="utf-8")
                return {"status": "approved"}
    elif req.action == "reject":
        for f in INBOX.glob("*.md"):
            if req.task_id in f.stem:
                text = f.read_text(encoding="utf-8")
                text = text.replace("status: pending", "status: rejected")
                f.write_text(text, encoding="utf-8")
                return {"status": "rejected"}

    return {"status": "ok"}


# ─── PM Context Builder ───

@app.get("/api/pm/context")
async def get_pm_context():
    """
    組合 PM 需要的完整 context：
    PM prompt + 可用 agents + 可用 models + 待處理 briefs
    一次給 PM 所有它需要的資訊。
    """
    pm_prompt = (PM_DIR / "PM.md").read_text(encoding="utf-8") if (PM_DIR / "PM.md").exists() else ""

    agents = list_agents()
    agent_summary = "\n".join([
        f"- **{a.get('name', a['filename'])}**: {a.get('role', a.get('_body', '')[:100])}"
        for a in agents
    ])

    models = list_models()
    model_summary = "\n".join([
        f"- **{m.get('name', m['filename'])}**: {m.get('provider', '')} / {m.get('model_id', '')} [{m.get('tags', '')}]"
        for m in models
    ])

    pending = [t for t in list_tasks(INBOX) if t.get("status") == "pending"]
    briefs_summary = "\n".join([
        f"- [{t['filename']}] {t.get('title', 'Untitled')}"
        for t in pending
    ])

    context = f"""{pm_prompt}

---

## Current Team ({len(agents)} agents)

{agent_summary or 'No agents configured.'}

## Available Models ({len(models)} models)

{model_summary or 'No models configured.'}

## Pending Briefs ({len(pending)})

{briefs_summary or 'No pending briefs.'}
"""
    return {"context": context, "agent_count": len(agents), "model_count": len(models), "pending": len(pending)}


# ─── Archive ───

@app.post("/api/archive/{filename}")
async def archive_task(filename: str):
    """把完成的任務移到 archive"""
    for folder in [INBOX, OUTPUT, REVIEW]:
        src = folder / f"{filename}.md"
        if src.exists():
            dst = ARCHIVE / f"{filename}.md"
            src.rename(dst)
            return {"status": "archived"}
    raise HTTPException(404)


# ─── 靜態檔案 ───

# Serve vault files for Obsidian-like browsing
@app.get("/api/file/{folder}/{filename}")
async def read_file(folder: str, filename: str):
    path = BASE_DIR / folder / filename
    if not path.exists():
        raise HTTPException(404)
    return {"content": path.read_text(encoding="utf-8")}


@app.put("/api/file/{folder}/{filename}")
async def write_file(folder: str, filename: str, data: dict):
    path = BASE_DIR / folder / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data.get("content", ""), encoding="utf-8")
    return {"status": "ok"}


# ═══════════════════════════════════════
# Flow (Dynamic Routing) Routes
# ═══════════════════════════════════════

class FlowConnectReq(BaseModel):
    source: str  # from agent_id (node ID)
    target: str  # to agent_id (node ID)


@app.get("/api/flow")
async def get_flow():
    """取得完整 flowMap（唯一真相）"""
    from engine.dispatcher import dispatcher
    return dispatcher.get_flow()


@app.post("/api/flow/connect")
async def flow_connect(req: FlowConnectReq):
    """新增 A → B 連線"""
    from engine.dispatcher import dispatcher
    conn = dispatcher.connect(req.source, req.target)
    return {"status": "ok", "connection": conn}


@app.delete("/api/flow/connect")
async def flow_disconnect(req: FlowConnectReq):
    """移除 A → B 連線"""
    from engine.dispatcher import dispatcher
    dispatcher.disconnect(req.source, req.target)
    return {"status": "ok"}


@app.get("/api/flow/api-result/{agent_id}")
async def get_api_result(agent_id: str):
    """取得 API 節點的最後執行結果"""
    from engine.dispatcher import dispatcher
    result = dispatcher.get_api_result(agent_id)
    if result:
        return result
    return {"status": 0, "body": "", "ok": False, "error": "No result yet"}


# ═══════════════════════════════════════
# LLM / API Agent Routes
# ═══════════════════════════════════════

from fastapi import WebSocket as WS


class LLMConfigReq(BaseModel):
    api_key: str = ""
    base_url: str = ""
    default_model: str = ""


@app.get("/api/llm/config")
async def get_llm_config():
    """取得 LLM 設定（不含 API key 明文）"""
    from engine.llm_client import save_config, get_provider_config
    cfg = get_provider_config("openrouter")
    return {
        "base_url": cfg.get("base_url", "https://openrouter.ai/api/v1"),
        "default_model": cfg.get("model", ""),
        "has_key": bool(cfg.get("api_key")),
    }


@app.post("/api/llm/config")
async def set_llm_config(req: LLMConfigReq):
    """設定 LLM API key / base_url / default_model"""
    from engine.llm_client import save_config
    save_config(api_key=req.api_key, base_url=req.base_url, default_model=req.default_model)
    return {"status": "ok"}


# ═══════════════════════════════════════
# Provider Auth Routes（多供應商認證）
# ═══════════════════════════════════════

@app.get("/api/providers")
async def list_providers():
    """列出所有 provider 的狀態（已連接/未連接）"""
    from engine.llm_client import get_all_providers_status
    return get_all_providers_status()


@app.post("/api/providers/detect")
async def detect_providers():
    """掃描所有 CLI token 檔案，回傳偵測結果"""
    from engine.llm_proxy import token_store, PROVIDERS
    results = token_store.harvest_all()
    status = token_store.get_status()
    return status


@app.get("/api/providers/status")
async def providers_status():
    """取得所有 provider 的當前狀態"""
    from engine.llm_proxy import token_store
    return token_store.get_status()


class ProviderConfigReq(BaseModel):
    provider: str
    auth: str = "api_key"      # api_key | oauth
    api_key: str = ""
    base_url: str = ""
    model: str = ""
    client_id: str = ""
    client_secret: str = ""


@app.post("/api/providers/config")
async def set_provider_config(req: ProviderConfigReq):
    """設定某 provider 的認證資訊"""
    from engine.llm_client import set_provider_config as _set
    cfg = {"auth": req.auth}
    if req.api_key:
        cfg["api_key"] = req.api_key
    if req.base_url:
        cfg["base_url"] = req.base_url
    if req.model:
        cfg["model"] = req.model
    if req.client_id:
        cfg["client_id"] = req.client_id
    if req.client_secret:
        cfg["client_secret"] = req.client_secret
    _set(req.provider, cfg)
    return {"status": "ok", "provider": req.provider}


@app.get("/api/auth/login/{provider}")
async def oauth_login(provider: str):
    """取得 OAuth 登入 URL — 前端 redirect 到這個 URL"""
    from engine.llm_client import get_oauth_params
    params = get_oauth_params(provider)
    if "error" in params:
        raise HTTPException(400, params["error"])

    # 組合 OAuth URL
    from urllib.parse import urlencode
    query = {
        "client_id": params["client_id"],
        "redirect_uri": params["redirect_uri"],
        "response_type": "code",
        "scope": params.get("scope", ""),
        "access_type": "offline",
        "prompt": "consent",
    }
    url = params["auth_url"] + "?" + urlencode({k: v for k, v in query.items() if v})
    return {"auth_url": url, "provider": provider}


from fastapi import Request
from fastapi.responses import RedirectResponse


@app.get("/api/auth/callback/{provider}")
async def oauth_callback(provider: str, request: Request):
    """OAuth callback — 用 auth code 換 access token"""
    import aiohttp
    from engine.llm_client import (
        PROVIDERS, get_provider_config, store_oauth_token,
    )

    code = request.query_params.get("code", "")
    if not code:
        return JSONResponse({"error": "No auth code received"}, status_code=400)

    pdef = PROVIDERS.get(provider)
    if not pdef:
        return JSONResponse({"error": f"Unknown provider: {provider}"}, status_code=400)

    cfg = get_provider_config(provider)
    client_id = cfg.get("client_id") or os.getenv(f"{provider.upper()}_CLIENT_ID", "")
    client_secret = cfg.get("client_secret") or os.getenv(f"{provider.upper()}_CLIENT_SECRET", "")
    redirect_uri = cfg.get("redirect_uri", f"http://127.0.0.1:3000/api/auth/callback/{provider}")

    if not client_id or not client_secret:
        return JSONResponse({
            "error": f"Missing client_id or client_secret for {provider}. "
                     f"Set {provider.upper()}_CLIENT_ID and {provider.upper()}_CLIENT_SECRET env vars."
        }, status_code=400)

    # Exchange code for token
    token_url = pdef.get("token_url", "")
    async with aiohttp.ClientSession() as session:
        async with session.post(token_url, data={
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        }) as resp:
            if resp.status != 200:
                body = await resp.text()
                logger.error(f"[OAuth] {provider} token exchange failed: {body}")
                return JSONResponse({"error": f"Token exchange failed: {body[:200]}"}, status_code=400)

            token_data = await resp.json()

    store_oauth_token(provider, token_data)
    logger.info(f"[OAuth] {provider} login successful")

    # Redirect back to UI
    return RedirectResponse("/?auth=success&provider=" + provider)


class CLIRunReq(BaseModel):
    cmd: str
    agent_id: str = ""
    env_keys: list[str] = []
    credentials: str = ""
    timeout: int = 60


@app.post("/api/cli/run")
async def run_cli(req: CLIRunReq):
    """安全執行 CLI 指令（非互動、API key 注入、憑證掛載）"""
    from engine.cli_executor import run as cli_run
    result = cli_run(
        cmd=req.cmd,
        agent_id=req.agent_id,
        env_keys=req.env_keys or None,
        credentials=req.credentials,
        timeout=req.timeout,
    )
    return result


@app.get("/api/cli/credentials/{tool}")
async def check_credentials(tool: str):
    """檢查某工具的憑證狀態"""
    from engine.cli_executor import mount_credentials
    return mount_credentials(tool)


class AgentTaskReq(BaseModel):
    agent_id: str
    message: str


@app.post("/api/agent/run")
async def run_agent_task(req: AgentTaskReq):
    """直接呼叫 agent 執行任務（透過 Dispatcher，自動判斷 API/PTY）"""
    from engine.dispatcher import dispatcher
    ok = dispatcher.on_task_dispatch("user", req.agent_id, req.message)
    return {"status": "ok" if ok else "error", "agent_id": req.agent_id}


@app.get("/api/agent/status/{agent_id}")
async def get_agent_status(agent_id: str):
    """取得 agent 的 API 執行狀態"""
    from engine.agent_runner import agent_runner
    return agent_runner.get_status(agent_id)


@app.get("/api/agent/output/{agent_id}")
async def get_agent_output(agent_id: str):
    """取得 agent 的當前輸出"""
    from engine.agent_runner import agent_runner
    return {"agent_id": agent_id, "output": agent_runner.get_output(agent_id)}


@app.get("/api/agent/history/{agent_id}")
async def get_agent_history(agent_id: str):
    """取得 agent 的對話歷史"""
    from engine.agent_runner import agent_runner
    return {"agent_id": agent_id, "history": agent_runner.get_history(agent_id)}


@app.delete("/api/agent/history/{agent_id}")
async def clear_agent_history(agent_id: str):
    """清除 agent 的對話歷史"""
    from engine.agent_runner import agent_runner
    agent_runner.clear_history(agent_id)
    return {"status": "ok"}


@app.get("/api/agent/list")
async def list_api_agents():
    """列出所有 API 模式的 agent"""
    from engine.agent_runner import agent_runner
    return agent_runner.list_active()


class SpawnReq(BaseModel):
    name: str
    role: str
    skills: list[str] = []
    provider: str = "openrouter"


@app.post("/api/agent/spawn")
async def spawn_agent(req: SpawnReq):
    """動態建立新 agent — 自動產生 .md + 加入 flowMap"""
    from engine.dispatcher import dispatcher

    filename = req.name.lower().replace(" ", "-")
    path = TEAM / f"{filename}.md"

    # 從 _TEMPLATE.md 為基礎
    template_path = TEAM / "_TEMPLATE.md"
    if template_path.exists():
        template = template_path.read_text(encoding="utf-8")
    else:
        template = ""

    tags = req.skills + [req.provider]
    tags_str = ", ".join(tags)

    content = f"""---
name: "{req.name}"
role: "{req.role}"
tags: [{tags_str}]
---

# {req.name}

## Identity
You are {req.name}, a {req.role}. Your skills: {', '.join(req.skills)}.

## Capabilities
- {req.role}

## Tags
{tags_str}
"""
    path.write_text(content, encoding="utf-8")

    # 自動建立 PM → 新 agent 的連線
    pm_ids = [f.stem for f in TEAM.glob("*.md")
              if not f.name.startswith("_") and "pm" in f.read_text(encoding="utf-8").lower()[:200]]
    for pid in pm_ids:
        dispatcher.connect(pid, filename)

    logger.info(f"[Spawn] Created agent: {filename} ({req.role})")
    return {"status": "ok", "agent_id": filename, "tags": tags}


@app.delete("/api/agent/{agent_id}")
async def remove_agent(agent_id: str):
    """移除 agent — 刪除 .md + 清除 flowMap"""
    from engine.dispatcher import dispatcher
    from engine.agent_runner import agent_runner

    # 刪除 .md
    path = TEAM / f"{agent_id}.md"
    if path.exists():
        path.unlink()

    # 清除 flowMap
    keys_to_remove = [k for k in dispatcher.flow_map if agent_id in k]
    for k in keys_to_remove:
        del dispatcher.flow_map[k]
    dispatcher._save_flow()

    # 清除 agent_runner 狀態
    agent_runner.agents.pop(agent_id, None)

    return {"status": "ok", "removed": agent_id}


@app.post("/api/plan")
async def plan_goal(data: dict):
    """使用 Planner 把目標拆成 subtask list"""
    from engine.planner import plan
    goal = data.get("goal", "")
    if not goal:
        raise HTTPException(400, "goal required")
    tasks = await plan(goal)
    return {"goal": goal, "tasks": tasks}


@app.post("/api/plan/execute")
async def execute_plan_endpoint(data: dict):
    """執行 plan 中的任務"""
    from engine.planner import execute_plan
    tasks = data.get("tasks", [])
    if not tasks:
        raise HTTPException(400, "tasks required")
    await execute_plan(tasks)
    return {"status": "ok", "dispatched": len(tasks)}


# ═══════════════════════════════════════
# DNA Routes (Phase 3)
# ═══════════════════════════════════════

@app.get("/api/dna")
async def list_dna():
    """列出所有 DNA"""
    from engine.dna.dna_registry import list_all, load_all
    load_all()
    return list_all()


@app.get("/api/dna/{name}")
async def get_dna(name: str):
    """取得單一 DNA"""
    from engine.dna.dna_registry import get
    dna = get(name)
    if not dna:
        raise HTTPException(404, f"DNA not found: {name}")
    return dna


@app.post("/api/dna/generate")
async def generate_dna_endpoint(data: dict):
    """用 LLM 自動生成 Agent DNA"""
    from engine.dna.dna_generator import generate_dna
    from engine.dna.dna_manager import save_dna
    task = data.get("task", "")
    if not task:
        raise HTTPException(400, "task required")
    dna = await generate_dna(task, requested_by=data.get("requested_by", "user"))
    path = save_dna(dna)
    return {"status": "ok", "dna": dna, "path": path}


@app.post("/api/dna/gc")
async def dna_gc():
    """淘汰低分 DNA"""
    from engine.dna.dna_manager import garbage_collect
    removed = garbage_collect()
    return {"status": "ok", "removed": removed}


@app.delete("/api/dna/{name}")
async def delete_dna(name: str):
    """刪除 DNA"""
    from engine.dna.dna_manager import delete_dna
    ok = delete_dna(name)
    return {"status": "ok" if ok else "not_found"}


class SpawnFromDNAReq(BaseModel):
    dna_name: str


@app.post("/api/agent/spawn-from-dna")
async def spawn_from_dna(req: SpawnFromDNAReq):
    """從現有 DNA 建立新 agent"""
    from engine.dna.dna_registry import get as get_dna_item
    from engine.dna.dna_manager import update_score
    from engine.dispatcher import dispatcher
    from engine.workspace import ensure_workspace

    dna = get_dna_item(req.dna_name)
    if not dna:
        raise HTTPException(404, f"DNA not found: {req.dna_name}")

    name = dna["name"]
    path = TEAM / f"{name}.md"
    tags = dna.get("skills", []) + [dna.get("provider", "openrouter")]
    tags_str = ", ".join(tags)

    content = f"""---
name: "{name}"
role: "{dna.get('description', '')}"
tags: [{tags_str}]
---

# {name}

{dna.get('prompt', f'You are {name}.')}
"""
    path.write_text(content, encoding="utf-8")
    ensure_workspace(name)

    # 連接 PM
    pm_ids = [f.stem for f in TEAM.glob("*.md")
              if not f.name.startswith("_") and "pm" in f.read_text(encoding="utf-8").lower()[:200]]
    for pid in pm_ids:
        dispatcher.connect(pid, name)

    return {"status": "ok", "agent_id": name, "dna": dna}


class SpawnAutoReq(BaseModel):
    task: str


@app.post("/api/agent/spawn-auto")
async def spawn_auto(req: SpawnAutoReq):
    """自動生成 DNA → 建立 agent（一步到位）"""
    from engine.dna.dna_generator import generate_dna
    from engine.dna.dna_manager import save_dna
    from engine.dispatcher import dispatcher
    from engine.workspace import ensure_workspace

    # 檢查 agent 數量限制
    existing = len(list(TEAM.glob("*.md"))) - 1  # 扣掉 _TEMPLATE
    if existing >= 20:
        raise HTTPException(400, f"Agent limit reached ({existing}/20)")

    dna = await generate_dna(req.task, requested_by="auto")
    save_dna(dna)

    name = dna["name"]
    path = TEAM / f"{name}.md"
    tags = dna.get("skills", []) + [dna.get("provider", "openrouter")]

    content = f"""---
name: "{name}"
role: "{dna.get('description', '')}"
tags: [{', '.join(tags)}]
---

# {name}

{dna.get('prompt', f'You are {name}.')}
"""
    path.write_text(content, encoding="utf-8")
    ensure_workspace(name)

    pm_ids = [f.stem for f in TEAM.glob("*.md")
              if not f.name.startswith("_") and "pm" in f.read_text(encoding="utf-8").lower()[:200]]
    for pid in pm_ids:
        dispatcher.connect(pid, name)

    return {"status": "ok", "agent_id": name, "dna": dna}


@app.websocket("/ws/agent/{agent_id}")
async def ws_agent_stream(websocket: WS, agent_id: str):
    """WebSocket: 串流 agent 的 LLM 輸出（API 模式）"""
    from engine.agent_runner import agent_runner

    await websocket.accept()
    ctx = agent_runner.get_or_create(agent_id)
    queue: asyncio.Queue = asyncio.Queue(maxsize=2000)
    ctx.ws_queues.add(queue)

    try:
        while True:
            chunk = await queue.get()
            await websocket.send_text(chunk)
    except Exception:
        pass
    finally:
        ctx.ws_queues.discard(queue)


# ═══════════════════════════════════════
# Terminal (PTY) Routes — fallback for mock/cmd/bash
# ═══════════════════════════════════════

class TerminalRequest(BaseModel):
    agent_id: str
    cli_type: str  # claude / gemini / kimi / codex / cmd / bash
    cwd: str = ""


@app.post("/api/terminal/start")
async def start_terminal(req: TerminalRequest):
    """啟動一個 Agent 的終端"""
    from engine.pty_manager import pty_manager
    ok = pty_manager.create(req.agent_id, req.cli_type, req.cwd)
    if ok:
        return {"status": "ok", "agent_id": req.agent_id, "cli_type": req.cli_type}
    raise HTTPException(500, f"Failed to start {req.cli_type}")


@app.post("/api/terminal/stop/{agent_id}")
async def stop_terminal(agent_id: str):
    from engine.pty_manager import pty_manager
    pty_manager.stop(agent_id)
    return {"status": "ok"}


@app.post("/api/terminal/write/{agent_id}")
async def write_terminal(agent_id: str, data: dict):
    from engine.pty_manager import pty_manager
    from engine.runner import runner
    text = data.get("text", "")
    if pty_manager.write(agent_id, text):
        # 如果寫入的是 PM 終端，直接讓 Runner 也看到（不等前端回傳）
        term = pty_manager.get(agent_id)
        if term and term._check_is_pm() and runner.is_running:
            runner.on_pm_output(text)
        return {"status": "ok"}
    raise HTTPException(404, "Terminal not found or dead")


@app.post("/api/terminal/output/{agent_id}")
async def report_terminal_output(agent_id: str, data: dict):
    """前端 xterm 把收到的輸出回傳給 server（Runner 用）"""
    from engine.runner import runner
    text = data.get("text", "")
    if not text:
        return {"status": "ok"}

    # 判斷 PM 還是 Agent
    from engine.pty_manager import pty_manager
    term = pty_manager.get(agent_id)
    if term and term._check_is_pm():
        runner.on_pm_output(text)
    else:
        runner.on_agent_output(agent_id, text)
    return {"status": "ok"}


@app.get("/api/terminal/list")
async def list_terminals():
    from engine.pty_manager import pty_manager
    return pty_manager.list_active()


@app.websocket("/ws/terminal/{agent_id}")
async def ws_terminal(websocket: WS, agent_id: str):
    """WebSocket：連接 Agent 的 PTY 終端"""
    from engine.pty_manager import pty_manager

    await websocket.accept()
    term = pty_manager.get(agent_id)

    if not term:
        await websocket.send_text("[error] Terminal not found. Start it first.\r\n")
        await websocket.close()
        return

    queue: asyncio.Queue = asyncio.Queue(maxsize=1000)
    term.ws_clients.add(queue)

    async def reader():
        """PTY → WebSocket"""
        try:
            while True:
                data = await queue.get()
                await websocket.send_text(data)
        except Exception:
            pass

    async def writer():
        """WebSocket → PTY"""
        try:
            while True:
                msg = await websocket.receive_text()
                term.write(msg)
        except Exception:
            pass

    read_task = asyncio.create_task(reader())
    write_task = asyncio.create_task(writer())

    try:
        await asyncio.gather(read_task, write_task)
    except Exception:
        pass
    finally:
        read_task.cancel()
        write_task.cancel()
        term.ws_clients.discard(queue)


# ═══════════════════════════════════════
# Runner Routes
# ═══════════════════════════════════════

@app.post("/api/runner/start")
async def start_runner():
    """啟動 Runner（PM ↔ Agent 自動橋接）"""
    from engine.runner import runner
    from engine.pty_manager import pty_manager
    runner.start(pty_manager)
    return {"status": "ok"}


@app.post("/api/runner/stop")
async def stop_runner():
    from engine.runner import runner
    runner.stop()
    return {"status": "ok"}


@app.get("/api/runner/status")
async def runner_status():
    from engine.runner import runner
    return {
        "running": runner.is_running,
        "task_log": runner.get_task_log(),
    }


@app.on_event("startup")
async def auto_start_runner():
    """Server 啟動時自動開啟 Runner"""
    from engine.runner import runner
    from engine.pty_manager import pty_manager
    runner.start(pty_manager)
    logger.info("Runner auto-started")


# ═══════════════════════════════════════
# Watcher Routes
# ═══════════════════════════════════════

@app.get("/api/watcher/status")
async def watcher_status():
    """取得所有 agent 的靜默監控狀態"""
    from engine.watcher import watcher
    return watcher.get_status()


@app.post("/api/watcher/force/{agent_id}")
async def force_complete(agent_id: str):
    """手動強制完成某 agent 的任務，推給下游"""
    from engine.watcher import watcher
    result = watcher.force_complete(agent_id)
    return result


# ═══════════════════════════════════════
# Workspace Routes
# ═══════════════════════════════════════

@app.get("/api/workspace/{agent_id}/files")
async def workspace_files(agent_id: str):
    """取得 agent 的 workspace 檔案結構"""
    from engine.workspace import list_files, ensure_workspace
    ensure_workspace(agent_id)
    return {"agent_id": agent_id, "files": list_files(agent_id)}


@app.get("/api/workspace/{agent_id}/read")
async def workspace_read(agent_id: str, path: str = ""):
    """讀取 agent workspace 中的檔案"""
    from engine.workspace import read_file
    if not path:
        raise HTTPException(400, "path parameter required")
    return read_file(agent_id, path)


# ═══════════════════════════════════════
# SysLog Routes
# ═══════════════════════════════════════

@app.get("/api/logs")
async def get_logs(limit: int = 50, agent_id: str = "", level: str = ""):
    """取得最近的結構化日誌"""
    from engine.syslog import syslog
    return syslog.get_recent(limit=limit, agent_id=agent_id, level=level)


@app.websocket("/ws/logs")
async def ws_logs(websocket: WS):
    """WebSocket: 即時串流結構化日誌"""
    from engine.syslog import syslog

    await websocket.accept()
    queue: asyncio.Queue = asyncio.Queue(maxsize=500)
    syslog.subscribe(queue)

    try:
        while True:
            msg = await queue.get()
            await websocket.send_text(msg)
    except Exception:
        pass
    finally:
        syslog.unsubscribe(queue)


# ═══════════════════════════════════════
# Mock Test Routes
# ═══════════════════════════════════════

MOCK_AGENTS = [
    {"id": "tc1_good",   "name": "TC1-Good",   "role": "Mock: outputs DONE in 2s",   "scenario": "乖寶寶"},
    {"id": "tc2_chatty", "name": "TC2-Chatty", "role": "Mock: chatty then fuzzy done", "scenario": "話癆"},
    {"id": "tc3_sleepy", "name": "TC3-Sleepy", "role": "Mock: sleeps, wakes on nudge", "scenario": "睡仙"},
    {"id": "tc4_dead",   "name": "TC4-Dead",   "role": "Mock: dead, needs FORCE",     "scenario": "死機"},
]


@app.post("/api/test/launch")
async def launch_mock_tests():
    """一鍵啟動全部 4 個 Mock Agent + 建立 PM→mock 流水線連線"""
    from engine.pty_manager import pty_manager
    from engine.dispatcher import dispatcher

    results = []
    for agent in MOCK_AGENTS:
        aid = agent["id"]

        # 建立 agent.md（如果不存在）
        agent_md = TEAM / f"{aid}.md"
        if not agent_md.exists():
            agent_md.write_text(
                f'---\nname: "{agent["name"]}"\nrole: "{agent["role"]}"\n'
                f'tags: [mock]\n---\n\n# {agent["name"]}\n{agent["scenario"]}\n',
                encoding="utf-8"
            )

        # 啟動 mock 終端
        ok = pty_manager.create(aid, "mock")
        results.append({"agent_id": aid, "started": ok, "scenario": agent["scenario"]})

        if ok:
            logger.info(f"[MockTest] Started {aid} ({agent['scenario']})")

    # 建立 PM → 所有 mock 的流水線連線
    # 也建立 tc1→tc2→tc3→tc4 串聯，測試 relay
    chain = [a["id"] for a in MOCK_AGENTS]
    for i in range(len(chain) - 1):
        dispatcher.connect(chain[i], chain[i + 1])

    # 模擬 PM dispatch：給 tc1 派任務（觸發整條流水線）
    # 延遲 2 秒讓終端啟動完成
    async def _delayed_dispatch():
        await asyncio.sleep(2)
        from engine.runner import runner
        if runner.is_running and runner._dispatcher:
            runner._dispatcher.on_task_dispatch("pm", "tc1_good", "Build the login page with React.")
            logger.info("[MockTest] Dispatched task to tc1_good → pipeline started")

    asyncio.create_task(_delayed_dispatch())

    return {
        "status": "ok",
        "agents": results,
        "pipeline": " → ".join(chain),
        "hint": "Watch the UI — tc1(2s done) → tc2(chatty) → tc3(nudge at 15s) → tc4(timeout at 45s, use FORCE)"
    }


@app.post("/api/test/stop")
async def stop_mock_tests():
    """停止所有 Mock Agent，清理配置檔，還原系統狀態"""
    from engine.pty_manager import pty_manager
    from engine.dispatcher import dispatcher
    from engine.watcher import watcher

    stopped = []
    for agent in MOCK_AGENTS:
        aid = agent["id"]

        # 1. 停止終端進程
        pty_manager.stop(aid)
        stopped.append(aid)

        # 2. 清除 flowMap 連線
        for other in MOCK_AGENTS:
            oid = other["id"]
            if oid != aid:
                dispatcher.disconnect(aid, oid)
                dispatcher.disconnect(oid, aid)

        # 3. 清除 watcher 追蹤狀態
        watcher._agents.pop(aid, None)

        # 4. 刪除測試用 agent.md
        agent_md = TEAM / f"{aid}.md"
        if agent_md.exists():
            agent_md.unlink()
            logger.info(f"[MockTest] Removed {agent_md.name}")

    # 5. 清除 dispatcher task_log 中的 mock 紀錄
    dispatcher._task_log = [
        t for t in dispatcher._task_log
        if t.get("source") not in [a["id"] for a in MOCK_AGENTS]
        and t.get("target") not in [a["id"] for a in MOCK_AGENTS]
    ]

    logger.info(f"[MockTest] Cleanup complete: {stopped}")
    return {"status": "ok", "stopped": stopped, "cleaned": True}


def start():
    uvicorn.run("engine.server:app", host="127.0.0.1", port=3000, reload=False)
