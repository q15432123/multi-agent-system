"""Agent Runner — API 模式的 Agent 執行器

取代 PTY 終端機，每個 Agent 透過 LLM API 執行任務。
管理：
- 每個 agent 的 system prompt（從 _team/{agent}.md 讀取）
- 對話歷史（context_history）
- 串流輸出（透過 asyncio.Queue 推送到 WebSocket）
- 完成信號偵測
"""
import asyncio
import json
import logging
import re
import threading
import time
from pathlib import Path
from typing import Optional

from engine import llm_client
from engine.llm_client import resolve_provider_and_model

# Import deferred to avoid circular — used inside methods

logger = logging.getLogger("agent_runner")

TEAM_DIR = Path(__file__).parent.parent / "_team"
PM_DIR = Path(__file__).parent.parent / "_pm"

# 完成信號
DONE_PATTERNS = [
    re.compile(r'\{\s*"sys_status"\s*:\s*"DONE"\s*\}', re.IGNORECASE),
    re.compile(r'markTaskComplete\s*\(', re.IGNORECASE),
    re.compile(r'\[DONE\]', re.IGNORECASE),
    re.compile(r'\[COMPLETE\]', re.IGNORECASE),
    re.compile(r'\[完成\]'),
    re.compile(r'task completed', re.IGNORECASE),
    re.compile(r'已完成'),
]

# 注入到每個 agent 的完成指令
COMPLETION_INSTRUCTION = (
    '\n\n[IMPORTANT] When your task is complete, you MUST call the mark_complete tool '
    'with a brief summary of what you accomplished. '
    'If mark_complete is not available, output: {"sys_status":"DONE"}'
)

# Task budget
MAX_TOOL_ROUNDS = 10
MAX_OUTPUT_CHARS = 100_000  # 100KB 截斷


class AgentContext:
    """單一 Agent 的運行狀態"""

    def __init__(self, agent_id: str) -> None:
        self.agent_id = agent_id
        self.system_prompt: str = ""
        self.provider: str = ""
        self.model: str = ""
        self.tags: list[str] = []
        self.history: list[dict] = []  # conversation memory
        self.status: str = "idle"  # idle | running | done | error
        self.last_output: str = ""
        self.output_buffer: str = ""  # 累積完整回應
        self.ws_queues: set[asyncio.Queue] = set()  # WebSocket subscribers
        self.is_pm: bool = False

    def load_from_md(self) -> None:
        """從 _team/{agent_id}.md 讀取 system prompt 和 tags"""
        path = TEAM_DIR / f"{self.agent_id}.md"
        if not path.exists():
            self.system_prompt = f"You are {self.agent_id}."
            return

        text = path.read_text(encoding="utf-8")
        self.tags = []
        body = text

        # Parse frontmatter
        if text.startswith("---"):
            parts = text.split("---", 2)
            if len(parts) >= 3:
                for line in parts[1].strip().split("\n"):
                    if ":" in line:
                        k, v = line.split(":", 1)
                        k, v = k.strip(), v.strip().strip('"')
                        if k == "tags":
                            # Parse [tag1, tag2]
                            v = v.strip("[]")
                            self.tags = [t.strip() for t in v.split(",") if t.strip()]
                        if k == "role" and "project manager" in v.lower():
                            self.is_pm = True
                body = parts[2].strip()

        if "pm" in self.tags:
            self.is_pm = True

        # PM 用 _pm/PM.md 作為 system prompt
        if self.is_pm:
            pm_path = PM_DIR / "PM.md"
            if pm_path.exists():
                body = pm_path.read_text(encoding="utf-8")

        self.system_prompt = body
        self.provider, self.model = resolve_provider_and_model(self.tags)
        logger.info(f"[AgentRunner] Loaded {self.agent_id}: provider={self.provider}, model={self.model}, tags={self.tags}")


class AgentRunner:
    """管理所有 Agent 的 API 執行"""

    def __init__(self) -> None:
        self.agents: dict[str, AgentContext] = {}
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    def start(self) -> None:
        try:
            self._loop = asyncio.get_event_loop()
        except RuntimeError:
            self._loop = asyncio.new_event_loop()
        logger.info("[AgentRunner] Started — API mode")

    def get_or_create(self, agent_id: str) -> AgentContext:
        """取得或建立 agent context，自動載入持久化歷史"""
        if agent_id not in self.agents:
            ctx = AgentContext(agent_id)
            ctx.load_from_md()
            self.agents[agent_id] = ctx
            self._load_history(agent_id)  # Step 4: 從 context.json 載入
        return self.agents[agent_id]

    def run_task(self, agent_id: str, user_message: str) -> None:
        """在背景線程執行 agent 任務（非阻塞）"""
        ctx = self.get_or_create(agent_id)

        if ctx.status == "running":
            logger.warning(f"[AgentRunner] {agent_id} already running, queuing...")

        ctx.status = "running"
        ctx.output_buffer = ""
        ctx.last_output = ""

        # 背景線程執行 API 呼叫（避免阻塞 event loop）
        t = threading.Thread(
            target=self._run_sync,
            args=(agent_id, user_message),
            daemon=True,
        )
        t.start()

    def _run_sync(self, agent_id: str, user_message: str) -> None:
        """同步執行 API 呼叫 + Tool Loop + 串流推送"""
        from engine.tools.registry import get_tool_schemas
        from engine.tools.router import route_tool_calls, parse_tool_calls_from_text
        from engine.syslog import syslog

        ctx = self.agents.get(agent_id)
        if not ctx:
            return

        # 組裝 messages
        messages = [{"role": "system", "content": ctx.system_prompt}]
        for msg in ctx.history[-40:]:
            messages.append(msg)

        task_with_instruction = user_message + COMPLETION_INSTRUCTION
        messages.append({"role": "user", "content": task_with_instruction})
        ctx.history.append({"role": "user", "content": user_message})

        all_tools = get_tool_schemas()
        # PM agent 只能用 mark_complete，不能寫檔案/跑命令
        PM_ONLY_TOOLS = {"mark_complete"}
        if ctx.is_pm:
            tools = [t for t in all_tools if t["function"]["name"] in PM_ONLY_TOOLS]
        else:
            tools = all_tools
        full_response = ""
        mark_complete_summary = ""
        total_output_chars = 0

        try:
            for _round in range(MAX_TOOL_ROUNDS):
                # 呼叫 LLM（帶 tools）
                client = llm_client._get_client_for_provider(ctx.provider)
                response = client.chat.completions.create(
                    model=ctx.model,
                    messages=messages,
                    tools=tools,
                    temperature=0.7,
                    max_tokens=4096,
                )

                choice = response.choices[0]
                msg = choice.message

                # Log token usage
                if response.usage:
                    syslog.log_llm_request(
                        agent_id, ctx.provider, ctx.model,
                        prompt_tokens=response.usage.prompt_tokens,
                        completion_tokens=response.usage.completion_tokens,
                    )

                # 檢查是否有 tool_calls
                if msg.tool_calls:
                    tool_names = [tc.function.name for tc in msg.tool_calls]
                    self._push_to_ws(agent_id, f"\n[Round {_round+1}/{MAX_TOOL_ROUNDS}] Tools: {', '.join(tool_names)}\n")

                    # 加入 assistant message（含 tool_calls）
                    messages.append(msg.model_dump())

                    # 檢查是否有 mark_complete
                    has_mark_complete = False
                    for tc in msg.tool_calls:
                        if tc.function.name == "mark_complete":
                            try:
                                args = json.loads(tc.function.arguments) if tc.function.arguments else {}
                            except Exception:
                                args = {}
                            mark_complete_summary = args.get("summary", "Task completed")
                            has_mark_complete = True
                            # 回傳 tool result 讓對話記錄完整
                            messages.append({"role": "tool", "tool_call_id": tc.id, "content": mark_complete_summary})
                            self._push_to_ws(agent_id, f"\n[mark_complete] {mark_complete_summary}\n")
                            logger.info(f"[AgentRunner] {agent_id} called mark_complete: {mark_complete_summary[:80]}")

                    if has_mark_complete:
                        full_response = mark_complete_summary
                        break  # 任務完成，跳出 tool loop

                    # 執行其他 tools
                    if self._loop:
                        future = asyncio.run_coroutine_threadsafe(
                            route_tool_calls(msg.tool_calls, agent_id),
                            self._loop,
                        )
                        tool_results = future.result(timeout=120)
                    else:
                        tool_results = []

                    for tr in tool_results:
                        preview = tr["content"][:200]
                        self._push_to_ws(agent_id, f"\n[Tool Result] {preview}\n")

                    messages.extend(tool_results)

                    # Budget check: 累計 output 字數
                    total_output_chars += sum(len(tr.get("content", "")) for tr in tool_results)
                    if total_output_chars > MAX_OUTPUT_CHARS:
                        self._push_to_ws(agent_id, f"\n[Budget exceeded: {total_output_chars} chars]\n")
                        logger.warning(f"[AgentRunner] {agent_id} output budget exceeded at round {_round+1}")
                        break

                    continue

                else:
                    # 沒有 tool_calls → 這是最終回覆
                    full_response = msg.content or ""
                    ctx.output_buffer = full_response
                    self._push_to_ws(agent_id, full_response)
                    break
            else:
                # for loop 正常結束（跑完 MAX_TOOL_ROUNDS）
                self._push_to_ws(agent_id, f"\n[Max tool rounds ({MAX_TOOL_ROUNDS}) reached]\n")
                logger.warning(f"[AgentRunner] {agent_id} hit max tool rounds")

            # ─── 完成處理 ───
            ctx.last_output = full_response
            ctx.output_buffer = full_response
            ctx.history.append({"role": "assistant", "content": full_response})

            # 修剪歷史（保留最近 50 條）
            if len(ctx.history) > 50:
                ctx.history = ctx.history[-50:]

            # 持久化對話歷史到 workspace
            self._save_history(agent_id)

            # 自動擷取 code blocks 寫入 workspace
            self._extract_and_save_code(agent_id, full_response)

            done = self.check_completion(full_response)
            ctx.status = "done"
            signal = "json" if done else "api_complete"
            syslog.log_complete(agent_id, signal, len(full_response))
            logger.info(f"[AgentRunner] {agent_id} finished: {len(full_response)} chars")

            if self._loop:
                asyncio.run_coroutine_threadsafe(
                    self._notify_complete(agent_id, full_response, done),
                    self._loop,
                )

        except Exception as e:
            ctx.status = "error"
            ctx.last_output = f"[ERROR] {e}"
            self._push_to_ws(agent_id, f"\n[ERROR] {e}\n")
            logger.error(f"[AgentRunner] {agent_id} error: {e}")
            if self._loop:
                asyncio.run_coroutine_threadsafe(
                    self._notify_error(agent_id, str(e)),
                    self._loop,
                )

    async def _notify_complete(self, agent_id: str, output: str, has_done_signal: bool) -> None:
        """通知 Dispatcher agent 完成"""
        from engine.dispatcher import dispatcher
        from engine.watcher import watcher

        watcher.on_task_complete(agent_id)

        # 不管有沒有 DONE signal，API 呼叫結束就是完成
        # （API 模式不會卡住，不像 PTY 需要偵測信號）
        dispatcher.on_task_complete(agent_id, output)

        # 如果是 PM，解析 @agent: 指令
        ctx = self.agents.get(agent_id)
        if ctx and ctx.is_pm:
            self._parse_pm_dispatch(agent_id, output)

    async def _notify_error(self, agent_id: str, error: str) -> None:
        from engine.watcher import watcher
        watcher.on_task_complete(agent_id)  # 停止監控

    MAX_DISPATCH_PER_REPLY = 5  # 單次 PM 回覆最多 dispatch 幾個 agent

    def _parse_pm_dispatch(self, pm_id: str, output: str) -> None:
        """解析 PM 輸出中的 @agent: 指令（只 dispatch 明確 @ 到的 agent）"""
        import re as _re
        pattern = _re.compile(r'@(\w[\w\-]*)\s*[:：]\s*(.+?)(?=@\w|\Z)', _re.DOTALL)

        dispatched = 0
        for match in pattern.finditer(output):
            agent_name = match.group(1).strip().lower()
            content = match.group(2).strip()
            if not content or len(content) < 3:
                continue
            if ' — ' in content and len(content) < 60:
                continue

            # 過濾 mock agent（tc 開頭）和 _disabled
            if agent_name.startswith("tc") or agent_name.startswith("_"):
                continue

            # Dispatch 上限
            if dispatched >= self.MAX_DISPATCH_PER_REPLY:
                logger.warning(f"[AgentRunner] PM dispatch limit reached ({self.MAX_DISPATCH_PER_REPLY}), skipping @{agent_name}")
                break

            # 只找 _team/ 裡存在的 agent（精確匹配優先）
            target = None
            for f in TEAM_DIR.glob("*.md"):
                if f.name.startswith("_"):
                    continue
                stem = f.stem.lower()
                if stem == agent_name:
                    target = f.stem
                    break
            # 模糊匹配 fallback
            if not target:
                for f in TEAM_DIR.glob("*.md"):
                    if f.name.startswith("_"):
                        continue
                    if agent_name in f.stem.lower():
                        target = f.stem
                        break

            if target and target != pm_id:
                from engine.dispatcher import dispatcher
                logger.info(f"[AgentRunner] PM dispatch: @{target}: {content[:60]}")
                dispatcher.on_task_dispatch(pm_id, target, content)
                dispatched += 1

    @staticmethod
    def check_completion(text: str) -> bool:
        """檢查 agent 輸出是否包含完成信號"""
        for pattern in DONE_PATTERNS:
            if pattern.search(text):
                return True
        return False

    def _push_to_ws(self, agent_id: str, chunk: str) -> None:
        """推送串流 chunk 到所有 WebSocket subscribers"""
        ctx = self.agents.get(agent_id)
        if not ctx:
            return
        dead = set()
        for q in ctx.ws_queues:
            try:
                q.put_nowait(chunk)
            except asyncio.QueueFull:
                dead.add(q)
            except Exception:
                dead.add(q)
        ctx.ws_queues -= dead

    def get_status(self, agent_id: str) -> dict:
        """取得 agent 狀態"""
        ctx = self.agents.get(agent_id)
        if not ctx:
            return {"status": "unknown", "agent_id": agent_id}
        return {
            "agent_id": agent_id,
            "status": ctx.status,
            "model": ctx.model,
            "is_pm": ctx.is_pm,
            "history_len": len(ctx.history),
            "last_output_len": len(ctx.last_output),
            "tags": ctx.tags,
        }

    def list_active(self) -> list[dict]:
        """列出所有已載入的 agent"""
        return [
            {
                "agent_id": aid,
                "status": ctx.status,
                "model": ctx.model,
                "alive": ctx.status in ("idle", "running", "done"),
            }
            for aid, ctx in self.agents.items()
        ]

    def get_output(self, agent_id: str) -> str:
        """取得 agent 的當前輸出 buffer"""
        ctx = self.agents.get(agent_id)
        return ctx.output_buffer if ctx else ""

    def get_history(self, agent_id: str) -> list[dict]:
        """取得 agent 的對話歷史"""
        ctx = self.agents.get(agent_id)
        return ctx.history if ctx else []

    def clear_history(self, agent_id: str) -> None:
        """清除 agent 的對話歷史"""
        ctx = self.agents.get(agent_id)
        if ctx:
            ctx.history = []

    def force_complete(self, agent_id: str) -> str:
        """強制完成：取當前 buffer 作為結果"""
        ctx = self.agents.get(agent_id)
        if not ctx:
            return ""
        output = ctx.output_buffer or ctx.last_output or f"[Force completed: {agent_id}]"
        ctx.status = "done"
        return output

    # ─── Memory Persistence (Step 4) ───

    def _save_history(self, agent_id: str) -> None:
        """持久化對話歷史到 _workspaces/{agent_id}/context.json"""
        ctx = self.agents.get(agent_id)
        if not ctx:
            return
        ws = Path(__file__).parent.parent / "_workspaces" / agent_id
        ws.mkdir(parents=True, exist_ok=True)
        ctx_file = ws / "context.json"
        try:
            # 只存最近 50 條
            data = {"history": ctx.history[-50:], "model": ctx.model, "provider": ctx.provider}
            ctx_file.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as e:
            logger.warning(f"[AgentRunner] Failed to save history for {agent_id}: {e}")

    def _load_history(self, agent_id: str) -> None:
        """從 _workspaces/{agent_id}/context.json 載入歷史"""
        ctx = self.agents.get(agent_id)
        if not ctx:
            return
        ctx_file = Path(__file__).parent.parent / "_workspaces" / agent_id / "context.json"
        if ctx_file.exists():
            try:
                data = json.loads(ctx_file.read_text(encoding="utf-8"))
                ctx.history = data.get("history", [])[-50:]
                logger.info(f"[AgentRunner] Loaded {len(ctx.history)} history entries for {agent_id}")
            except Exception as e:
                logger.warning(f"[AgentRunner] Failed to load history for {agent_id}: {e}")

    # ─── Code Block Auto-Save (Step 6) ───

    def _extract_and_save_code(self, agent_id: str, text: str) -> None:
        """擷取 LLM 回覆中的 code blocks 並寫入 workspace"""
        import re as _re
        blocks = _re.findall(r'```(\w*)\n(.*?)```', text, _re.DOTALL)
        if not blocks:
            return

        ws = Path(__file__).parent.parent / "_workspaces" / agent_id
        ws.mkdir(parents=True, exist_ok=True)

        ext_map = {
            "python": "py", "javascript": "js", "typescript": "ts",
            "jsx": "jsx", "tsx": "tsx", "html": "html", "css": "css",
            "json": "json", "yaml": "yaml", "yml": "yaml",
            "bash": "sh", "shell": "sh", "sql": "sql",
            "rust": "rs", "go": "go", "java": "java",
            "": "txt",
        }

        for i, (lang, code) in enumerate(blocks):
            code = code.strip()
            if len(code) < 10:
                continue
            ext = ext_map.get(lang.lower(), lang.lower() or "txt")
            fname = f"generated_code{'_'+str(i+1) if i > 0 else ''}.{ext}"
            fpath = ws / fname
            fpath.write_text(code, encoding="utf-8")
            logger.info(f"[AgentRunner] Saved code block → {agent_id}/{fname} ({len(code)} chars)")


# 全局實例
agent_runner = AgentRunner()
