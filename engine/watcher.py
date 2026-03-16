"""Watcher — 超時與靜默判定監控器

追蹤每個 active agent 的最後輸出時間。
超過 SILENCE_WARN_SEC 無輸出 → 自動詢問 agent 是否完成。
超過 SILENCE_TIMEOUT_SEC → 標記 timeout，允許手動 force-complete。

與 Runner/Dispatcher 整合，不獨立運行。
"""
import asyncio
import logging
import time
from typing import Optional

logger = logging.getLogger("watcher")

SILENCE_WARN_SEC = 15      # 靜默 15 秒 → 自動 nudge
SILENCE_TIMEOUT_SEC = 45   # 靜默 45 秒 → timeout
NUDGE_COOLDOWN_SEC = 20    # nudge 後至少等 20 秒才能再 nudge


class AgentState:
    """單一 agent 的監控狀態"""
    __slots__ = ('agent_id', 'last_output_time', 'task_start_time',
                 'last_nudge_time', 'status', 'task_content')

    def __init__(self, agent_id: str) -> None:
        self.agent_id = agent_id
        self.last_output_time: float = 0.0
        self.task_start_time: float = 0.0
        self.last_nudge_time: float = 0.0
        self.status: str = "idle"  # idle | working | warned | timeout | done
        self.task_content: str = ""


class Watcher:
    """監控所有 active agent 的靜默狀態"""

    def __init__(self) -> None:
        self._agents: dict[str, AgentState] = {}
        self._pty = None
        self._dispatcher = None
        self._runner = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._tick_handle = None

    def start(self, pty_manager, dispatcher, runner) -> None:
        self._pty = pty_manager
        self._dispatcher = dispatcher
        self._runner = runner
        try:
            self._loop = asyncio.get_event_loop()
        except RuntimeError:
            self._loop = asyncio.new_event_loop()
        self._schedule_tick()
        logger.info("[Watcher] 啟動 — 靜默監控")

    def _schedule_tick(self) -> None:
        """每 3 秒檢查一次"""
        if self._loop and not self._loop.is_closed():
            self._tick_handle = self._loop.call_later(3.0, self._tick)

    def _tick(self) -> None:
        """定期檢查所有 agent 的靜默狀態"""
        now = time.time()

        for aid, state in list(self._agents.items()):
            if state.status in ("idle", "done"):
                continue

            silence = now - state.last_output_time

            if state.status == "working" and silence >= SILENCE_WARN_SEC:
                if now - state.last_nudge_time >= NUDGE_COOLDOWN_SEC:
                    self._nudge_agent(aid, state)
                    state.status = "warned"
                    state.last_nudge_time = now
                    from engine.syslog import syslog
                    syslog.log_watcher_event(aid, "nudge", silence)

            elif state.status == "warned" and silence >= SILENCE_TIMEOUT_SEC:
                state.status = "timeout"
                from engine.syslog import syslog
                syslog.log_watcher_event(aid, "timeout", silence)

        self._schedule_tick()

    # ─── 外部呼叫 ───

    def on_task_dispatched(self, agent_id: str, content: str) -> None:
        """Dispatcher 派任務時呼叫"""
        if agent_id not in self._agents:
            self._agents[agent_id] = AgentState(agent_id)
        state = self._agents[agent_id]
        state.status = "working"
        state.task_start_time = time.time()
        state.last_output_time = time.time()
        state.task_content = content[:200]

    def on_agent_output(self, agent_id: str) -> None:
        """Runner 收到 agent 輸出時呼叫（只更新時間戳）"""
        if agent_id in self._agents:
            self._agents[agent_id].last_output_time = time.time()
            # 如果收到輸出了，從 warned 恢復為 working
            if self._agents[agent_id].status == "warned":
                self._agents[agent_id].status = "working"

    def on_task_complete(self, agent_id: str) -> None:
        """Runner 偵測到完成時呼叫"""
        if agent_id in self._agents:
            self._agents[agent_id].status = "done"

    def force_complete(self, agent_id: str) -> dict:
        """手動強制完成 — 取 buffer 內容當結果，推給下游"""
        state = self._agents.get(agent_id)
        if not state:
            return {"ok": False, "error": "agent not tracked"}

        # 從 runner 的 buffer 取最後的輸出
        result = ""
        if self._runner and agent_id in self._runner._agent_buffers:
            raw = self._runner._agent_buffers[agent_id]
            import re
            result = re.sub(r'\x1b\[[0-9;]*[a-zA-Z]', '', raw)
            result = result[-500:] if len(result) > 500 else result
            self._runner._agent_buffers[agent_id] = ""

        if not result:
            result = f"[Force completed by user — no output captured from {agent_id}]"

        state.status = "done"

        from engine.syslog import syslog
        syslog.log_watcher_event(agent_id, "force",
                                 time.time() - state.last_output_time if state.last_output_time else 0)

        if self._runner:
            self._runner._report_to_pm(agent_id, f"[FORCE COMPLETE] {result[:200]}")
        if self._dispatcher:
            self._dispatcher.on_task_complete(agent_id, result)

        logger.info(f"[Watcher] {agent_id} force completed")
        return {"ok": True, "agent_id": agent_id, "result_len": len(result)}

    def get_status(self) -> dict:
        """回傳所有 agent 的監控狀態（給 API）"""
        now = time.time()
        result = {}
        for aid, state in self._agents.items():
            silence = now - state.last_output_time if state.last_output_time > 0 else 0
            result[aid] = {
                "status": state.status,
                "silence_sec": round(silence, 1),
                "task_age_sec": round(now - state.task_start_time, 1) if state.task_start_time > 0 else 0,
                "task": state.task_content[:80],
            }
        return result

    # ─── 內部 ───

    def _nudge_agent(self, agent_id: str, state: AgentState) -> None:
        """自動詢問 agent 是否完成"""
        if not self._pty:
            return
        term = self._pty.get(agent_id)
        if not term or not term.is_alive:
            state.status = "timeout"
            return

        nudge_msg = (
            '\n[SYSTEM] Your task appears to be stalled. '
            'If you are done, please output: {"sys_status":"DONE"}\n'
            'If you are still working, please continue.\n'
        )
        term.write(nudge_msg)
        term.write("\r")


# 全局實例
watcher = Watcher()
