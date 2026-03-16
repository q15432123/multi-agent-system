"""AgentSysLogger — 結構化 JSONL 日誌 + 即時推送

所有日誌寫入 _logs/YYYY-MM-DD.jsonl
同時透過 WebSocket /ws/logs 即時推送到前端

三層攔截點：
1. LLM API 層 — token 消耗、耗時、API 錯誤
2. CLI 執行層 — returncode != 0 的 stderr
3. 狀態流轉層 — Dispatcher relay、Watcher nudge/timeout

用法：
    from engine.syslog import syslog
    syslog.info("jordan", "TASK_DISPATCHED", "Build login page", extra={...})
    syslog.error("tc4_dead", "WATCHER_TIMEOUT", "Silent 45s", raw_output="...")
"""
import asyncio
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

LOGS_DIR = Path(__file__).parent.parent / "_logs"
LOGS_DIR.mkdir(exist_ok=True)

# 日誌等級
INFO = "INFO"
WARN = "WARN"
ERROR = "ERROR"
DEBUG = "DEBUG"


class SysLogger:
    """結構化 JSONL 日誌 + WebSocket 即時推送"""

    def __init__(self) -> None:
        self._ws_queues: set[asyncio.Queue] = set()
        self._current_file: Optional[str] = None
        self._file_handle = None
        self._buffer: list[dict] = []  # 最近 200 條，供 API 讀取

    # ─── 寫入 ───

    def _emit(self, level: str, agent_id: str, event_type: str,
              message: str, raw_output: str = "", extra: dict | None = None) -> dict:
        """核心寫入方法"""
        now = datetime.now(timezone.utc)
        entry = {
            "timestamp": now.isoformat(),
            "ts": time.time(),
            "level": level,
            "agent_id": agent_id,
            "event_type": event_type,
            "message": message[:500],
        }
        if raw_output:
            entry["raw_output"] = raw_output[:2000]
        if extra:
            entry["extra"] = extra

        # 寫入 JSONL 檔案（按日期分檔）
        date_str = now.strftime("%Y-%m-%d")
        self._write_to_file(date_str, entry)

        # 記錄到記憶體 buffer
        self._buffer.append(entry)
        if len(self._buffer) > 200:
            self._buffer = self._buffer[-200:]

        # 推送到 WebSocket subscribers
        self._push_ws(entry)

        return entry

    def info(self, agent_id: str, event_type: str, message: str, **kwargs) -> dict:
        return self._emit(INFO, agent_id, event_type, message, **kwargs)

    def warn(self, agent_id: str, event_type: str, message: str, **kwargs) -> dict:
        return self._emit(WARN, agent_id, event_type, message, **kwargs)

    def error(self, agent_id: str, event_type: str, message: str, **kwargs) -> dict:
        return self._emit(ERROR, agent_id, event_type, message, **kwargs)

    def debug(self, agent_id: str, event_type: str, message: str, **kwargs) -> dict:
        return self._emit(DEBUG, agent_id, event_type, message, **kwargs)

    # ─── 讀取 ───

    def get_recent(self, limit: int = 50, agent_id: str = "", level: str = "") -> list[dict]:
        """取得最近的日誌"""
        entries = self._buffer
        if agent_id:
            entries = [e for e in entries if e["agent_id"] == agent_id]
        if level:
            entries = [e for e in entries if e["level"] == level]
        return entries[-limit:]

    def get_today_file(self) -> str:
        """取得今天的日誌檔路徑"""
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        return str(LOGS_DIR / f"{date_str}.jsonl")

    # ─── 檔案 I/O ───

    def _write_to_file(self, date_str: str, entry: dict) -> None:
        """寫入 JSONL 檔案"""
        try:
            filepath = LOGS_DIR / f"{date_str}.jsonl"
            with open(filepath, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception:
            pass  # 日誌系統本身不能拋異常

    # ─── WebSocket 推送 ───

    def subscribe(self, queue: asyncio.Queue) -> None:
        self._ws_queues.add(queue)

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._ws_queues.discard(queue)

    def _push_ws(self, entry: dict) -> None:
        """推送到所有 WebSocket subscribers"""
        msg = json.dumps(entry, ensure_ascii=False)
        dead = set()
        for q in self._ws_queues:
            try:
                q.put_nowait(msg)
            except (asyncio.QueueFull, Exception):
                dead.add(q)
        self._ws_queues -= dead

    # ═══════════════════════════════════════
    # 攔截器 — 植入各模組使用
    # ═══════════════════════════════════════

    def log_llm_request(self, agent_id: str, provider: str, model: str,
                        prompt_tokens: int = 0, completion_tokens: int = 0,
                        duration_ms: int = 0, error: str = "",
                        status_code: int = 0) -> None:
        """LLM API 層攔截"""
        if error:
            err_type = "LLM_RATE_LIMIT" if "429" in error else \
                       "LLM_SERVER_ERROR" if "500" in error else \
                       "LLM_AUTH_ERROR" if "401" in error or "403" in error else \
                       "LLM_ERROR"
            self.error(agent_id, err_type, f"{provider}/{model}: {error}",
                       extra={"provider": provider, "model": model,
                              "status_code": status_code})
        else:
            self.info(agent_id, "LLM_RESPONSE", f"{provider}/{model} OK",
                      extra={"provider": provider, "model": model,
                             "prompt_tokens": prompt_tokens,
                             "completion_tokens": completion_tokens,
                             "total_tokens": prompt_tokens + completion_tokens,
                             "duration_ms": duration_ms})

    def log_cli_exec(self, agent_id: str, cmd: str, exit_code: int,
                     stderr: str = "", duration_ms: int = 0) -> None:
        """CLI 執行層攔截"""
        if exit_code != 0:
            self.error(agent_id, "CLI_EXEC_FAILED",
                       f"cmd='{cmd[:60]}' exit={exit_code}",
                       raw_output=stderr,
                       extra={"cmd": cmd[:200], "exit_code": exit_code,
                              "duration_ms": duration_ms})
        else:
            self.info(agent_id, "CLI_EXEC_OK", f"cmd='{cmd[:60]}' OK",
                      extra={"cmd": cmd[:200], "duration_ms": duration_ms})

    def log_dispatch(self, source: str, target: str, content: str,
                     status: str = "dispatched") -> None:
        """Dispatcher 狀態流轉攔截"""
        self.info(target, "TASK_DISPATCHED",
                  f"{source} → {target}: {content[:80]}",
                  extra={"source": source, "status": status})

    def log_relay(self, source: str, target: str, payload_len: int) -> None:
        """Relay 轉發攔截"""
        self.info(target, "RELAY_RECEIVED",
                  f"Relay from {source} ({payload_len} chars)",
                  extra={"source": source, "payload_len": payload_len})

    def log_watcher_event(self, agent_id: str, event: str,
                          silence_sec: float = 0) -> None:
        """Watcher 事件攔截"""
        if event == "nudge":
            self.warn(agent_id, "WATCHER_NUDGE",
                      f"Silent {silence_sec:.0f}s → nudge sent",
                      extra={"silence_sec": silence_sec})
        elif event == "timeout":
            self.error(agent_id, "WATCHER_TIMEOUT",
                       f"Silent {silence_sec:.0f}s → TIMEOUT",
                       extra={"silence_sec": silence_sec})
        elif event == "force":
            self.warn(agent_id, "WATCHER_FORCE_COMPLETE",
                      "Force completed by user",
                      extra={"silence_sec": silence_sec})

    def log_complete(self, agent_id: str, signal: str, output_len: int) -> None:
        """Agent 完成攔截"""
        self.info(agent_id, "TASK_COMPLETE",
                  f"Done (signal={signal}, {output_len} chars)",
                  extra={"signal": signal, "output_len": output_len})


# 全局實例
syslog = SysLogger()
