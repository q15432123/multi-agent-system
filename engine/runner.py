"""Multi-Agent-System Runner — PTY 文字解析層（僅 PTY 模式用）

API 模式的 agent 由 agent_runner.py 直接處理，不經過 Runner。
Runner 只負責 PTY 模式的 mock agent / cmd / bash 終端輸出解析。
"""
import logging
import re
import time

logger = logging.getLogger("runner")

# Agent 完成信號 — 多種格式，優先度由上到下
DONE_PATTERNS = [
    re.compile(r'\{\s*"sys_status"\s*:\s*"DONE"\s*\}', re.IGNORECASE),
    re.compile(r'markTaskComplete\s*\(', re.IGNORECASE),
    re.compile(r'\[DONE\]', re.IGNORECASE),
    re.compile(r'\[COMPLETE\]', re.IGNORECASE),
    re.compile(r'\[完成\]'),
    re.compile(r'task completed', re.IGNORECASE),
    re.compile(r'已完成'),
    re.compile(r'all tasks? (?:are |is )?(?:done|completed|finished)', re.IGNORECASE),
]

TASK_PATTERNS = [
    re.compile(r'@(\w[\w\-]*)\s*[:：]\s*(.+?)(?=@\w|\Z)', re.DOTALL),
    re.compile(r'\[TASK\s*[:：]\s*(\w[\w\-]*)\]\s*(.+?)(?=\[TASK|\Z)', re.IGNORECASE | re.DOTALL),
]

FILTER_TEXTS = {'寫任務內容', '這裡寫任務', '任務描述'}


class Runner:
    """PTY 文字解析器 — 解析 PTY 終端輸出，發事件給 Dispatcher"""

    def __init__(self) -> None:
        self._running = False
        self._pm_buffer = ""
        self._agent_buffers: dict[str, str] = {}
        self._dispatched_hashes: set[str] = set()
        self._done_times: dict[str, float] = {}
        self._start_time = 0.0
        self._pty = None
        self._dispatcher = None
        self._watcher = None

    def start(self, pty_manager) -> None:
        self._running = True
        self._pty = pty_manager
        self._start_time = time.time()

        from engine.dispatcher import dispatcher
        from engine.watcher import watcher
        from engine.queue import task_queue
        self._dispatcher = dispatcher
        self._watcher = watcher

        # Dispatcher 啟動時會自動啟動 agent_runner
        dispatcher.start(pty_manager)
        watcher.start(pty_manager, dispatcher, self)

        # 啟動 task queue
        try:
            loop = asyncio.get_event_loop()
            task_queue.start(loop)
        except Exception:
            pass

        logger.info("[Runner] 啟動 — PTY 解析 + API agent_runner + Task Queue")

    def stop(self) -> None:
        self._running = False
        logger.info("[Runner] 停止")

    @property
    def is_running(self) -> bool:
        return self._running

    # ─── PM 輸出處理（PTY 模式 PM 用） ───

    def on_pm_output(self, data: str) -> None:
        if not self._running:
            return
        if time.time() - self._start_time < 10:
            return
        self._pm_buffer += data
        if '\n' in data or '\r' in data or len(self._pm_buffer) > 500:
            self._parse_pm_tasks()

    def _parse_pm_tasks(self) -> None:
        text = self._pm_buffer
        self._pm_buffer = ""
        clean = re.sub(r'\x1b\[[0-9;]*[a-zA-Z]', '', text)
        clean = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', clean)
        pm_id = self._find_pm_id()

        for pattern in TASK_PATTERNS:
            for match in pattern.finditer(clean):
                agent_name = match.group(1).strip().lower()
                content = match.group(2).strip()
                if not content or len(content) < 3:
                    continue
                if ' — ' in content and len(content) < 60:
                    continue
                if content.strip() in FILTER_TEXTS:
                    continue
                if len(content) > 5000:
                    content = content[:5000]

                h = f"{agent_name}:{content[:100]}"
                if h in self._dispatched_hashes:
                    continue
                self._dispatched_hashes.add(h)
                if len(self._dispatched_hashes) > 200:
                    self._dispatched_hashes = set(list(self._dispatched_hashes)[-100:])

                real_id = self._resolve_agent(agent_name)
                if not real_id:
                    continue

                source = pm_id or "pm"
                self._dispatcher.on_task_dispatch(source, real_id, content)

    # ─── Agent 輸出處理（PTY 模式） ───

    def on_agent_output(self, agent_id: str, data: str) -> None:
        if not self._running:
            return
        if self._watcher:
            self._watcher.on_agent_output(agent_id)
        if agent_id not in self._agent_buffers:
            self._agent_buffers[agent_id] = ""
        self._agent_buffers[agent_id] += data
        if '\n' in data or len(self._agent_buffers[agent_id]) > 1000:
            self._check_done(agent_id)

    def _check_done(self, agent_id: str) -> None:
        text = self._agent_buffers.get(agent_id, "")
        clean = re.sub(r'\x1b\[[0-9;]*[a-zA-Z]', '', text)
        for pattern in DONE_PATTERNS:
            if pattern.search(clean):
                now = time.time()
                key = f"done_{agent_id}"
                if self._done_times.get(key, 0) > now - 5:
                    self._agent_buffers[agent_id] = ""
                    return
                self._done_times[key] = now
                result = clean[-500:] if len(clean) > 500 else clean
                if self._watcher:
                    self._watcher.on_task_complete(agent_id)
                self._report_to_pm(agent_id, result)
                self._dispatcher.on_task_complete(agent_id, result)
                self._agent_buffers[agent_id] = ""
                return
        if len(self._agent_buffers[agent_id]) > 5000:
            self._agent_buffers[agent_id] = self._agent_buffers[agent_id][-2000:]

    # ─── 輔助 ───

    def _find_pm_id(self) -> str:
        if not self._pty:
            return ""
        for aid, term in self._pty.terminals.items():
            if term._check_is_pm():
                return aid
        return ""

    def _resolve_agent(self, name: str) -> str:
        if not self._pty:
            return ""
        for aid in self._pty.terminals:
            if aid.lower() == name:
                return aid
            if name in aid.lower() and not self._pty.get(aid)._check_is_pm():
                return aid
        return ""

    def _report_to_pm(self, agent_id: str, result: str) -> None:
        if not self._pty:
            return
        for aid, term in self._pty.terminals.items():
            if term._check_is_pm() and term.is_alive:
                report = f"\n[Report from {agent_id}]: Task completed.\n{result[:200]}\n"
                term.write(report)
                term.write("\r")
                return

    def get_task_log(self) -> list[dict]:
        if self._dispatcher:
            return self._dispatcher.get_task_log()
        return []


runner = Runner()
