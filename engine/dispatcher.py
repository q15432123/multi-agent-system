"""Dispatcher — 中央事件調度器 + flowMap 唯一真相

flowMap 格式：
    {
        "pm→alex": {"from": "pm", "to": "alex", "status": "idle"},
        "alex→luna": {"from": "alex", "to": "luna", "status": "pulsing"},
    }

status: "idle" | "pulsing" | "error"

執行模式：
    API 模式（預設）— 透過 agent_runner 呼叫 LLM API
    PTY 模式（fallback）— mock agent / cmd / bash 用 pty_manager
"""
import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Optional

from engine.relay import extract_payload, format_for_target, execute_api

logger = logging.getLogger("dispatcher")

FLOW_FILE = Path(__file__).parent.parent / "_config" / "flow.json"
TEAM_DIR = Path(__file__).parent.parent / "_team"

# 這些 tag 表示用 PTY 模式執行（不走 API）
PTY_TAGS = {"mock", "cmd", "bash", "powershell"}


class Dispatcher:
    """中央調度器 — flowMap 是唯一真相"""

    def __init__(self) -> None:
        self.flow_map: dict[str, dict] = {}
        self._pty = None
        self._task_log: list[dict] = []
        self._api_results: dict[str, dict] = {}
        self._relay_hashes: set[str] = set()
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    def start(self, pty_manager=None) -> None:
        self._pty = pty_manager
        try:
            self._loop = asyncio.get_event_loop()
        except RuntimeError:
            self._loop = asyncio.new_event_loop()
        self._load_flow()

        # 啟動 API agent runner
        from engine.agent_runner import agent_runner
        agent_runner.start()

        logger.info(f"[Dispatcher] 啟動 — {len(self.flow_map)} 條連線, API+PTY 雙模式")

    # ═══════════════════════════════════════
    # flowMap CRUD（不變）
    # ═══════════════════════════════════════

    def connect(self, from_id: str, to_id: str) -> dict:
        key = f"{from_id}→{to_id}"
        if key in self.flow_map:
            return self.flow_map[key]
        conn = {"from": from_id, "to": to_id, "status": "idle"}
        self.flow_map[key] = conn
        self._save_flow()
        logger.info(f"[Dispatcher] +Wire {key}")
        return conn

    def disconnect(self, from_id: str, to_id: str) -> None:
        key = f"{from_id}→{to_id}"
        self.flow_map.pop(key, None)
        self._save_flow()
        logger.info(f"[Dispatcher] -Wire {key}")

    def get_downstream(self, agent_id: str) -> list[str]:
        targets = []
        aid = agent_id.lower()
        for key, conn in self.flow_map.items():
            if conn["from"].lower() == aid and conn["status"] != "error":
                targets.append(conn["to"])
        return targets

    def set_status(self, from_id: str, to_id: str, status: str, error: str = "") -> None:
        key = f"{from_id}→{to_id}"
        if key in self.flow_map:
            self.flow_map[key]["status"] = status
            if error:
                self.flow_map[key]["error"] = error
            elif "error" in self.flow_map[key] and status != "error":
                del self.flow_map[key]["error"]

    def get_flow(self) -> dict:
        return dict(self.flow_map)

    def get_api_result(self, agent_id: str) -> Optional[dict]:
        return self._api_results.get(agent_id)

    # ═══════════════════════════════════════
    # 模式判定
    # ═══════════════════════════════════════

    def _is_pty_agent(self, agent_id: str) -> bool:
        """檢查 agent 是否該用 PTY 模式（mock / cmd / bash）"""
        tags = self._read_agent_tags(agent_id)
        return bool(PTY_TAGS & set(t.lower() for t in tags))

    def _read_agent_tags(self, agent_id: str) -> list[str]:
        """從 agent.md 讀取 tags"""
        path = TEAM_DIR / f"{agent_id}.md"
        if not path.exists():
            return []
        try:
            text = path.read_text(encoding="utf-8")
            if text.startswith("---"):
                parts = text.split("---", 2)
                if len(parts) >= 3:
                    for line in parts[1].strip().split("\n"):
                        if line.strip().startswith("tags:"):
                            v = line.split(":", 1)[1].strip().strip("[]")
                            return [t.strip() for t in v.split(",") if t.strip()]
        except Exception:
            pass
        return []

    # ═══════════════════════════════════════
    # 事件處理 — 自動選擇 API 或 PTY
    # ═══════════════════════════════════════

    def on_task_dispatch(self, source: str, target: str, content: str) -> bool:
        """派任務給 agent — 自動判斷 API 或 PTY 模式"""
        from engine.syslog import syslog
        self.set_status(source, target, "pulsing")
        self._log_task(source, target, content, "dispatched")
        syslog.log_dispatch(source, target, content)
        logger.info(f"[Dispatcher] ⚡ {source} → {target}: {content[:60]}")

        # 通知 Watcher
        try:
            from engine.watcher import watcher
            watcher.on_task_dispatched(target, content)
        except Exception:
            pass

        if self._is_pty_agent(target):
            # PTY 模式（mock / cmd）
            return self._dispatch_pty(source, target, content)
        else:
            # API 模式（預設）
            return self._dispatch_api(source, target, content)

    def _dispatch_api(self, source: str, target: str, content: str) -> bool:
        """API 模式：透過 task_queue 排隊 → agent_runner 執行"""
        from engine.queue import task_queue
        from engine.workspace import ensure_workspace
        ensure_workspace(target)  # 自動建立 workspace
        try:
            depth = task_queue.enqueue(target, content, source=source)
            if depth < 0:
                self.set_status(source, target, "error", "queue full")
                return False
            if depth > 1:
                logger.info(f"[Dispatcher] {target} queued (depth={depth})")
            self._schedule_idle(source, target, 2.0)
            return True
        except Exception as e:
            logger.error(f"[Dispatcher] API dispatch failed: {e}")
            self.set_status(source, target, "error", str(e))
            return False

    def _dispatch_pty(self, source: str, target: str, content: str) -> bool:
        """PTY 模式：寫入終端（mock agent / cmd / bash）"""
        if not self._pty:
            self.set_status(source, target, "error", "no pty manager")
            return False

        term = self._find_terminal(target)
        if not term or not term.is_alive:
            self.set_status(source, target, "error", "offline")
            return False

        term.write(content)
        term.write("\r")
        self._schedule_idle(source, target, 1.5)
        return True

    def on_task_complete(self, agent_id: str, raw_output: str) -> None:
        """Agent 完成任務 — 擷取 payload → relay 到下游"""
        payload = extract_payload(raw_output)
        downstream = self.get_downstream(agent_id)

        if not downstream:
            logger.info(f"[Dispatcher] {agent_id} 完成，無下游")
            return

        for target_id in downstream:
            rh = f"{agent_id}→{target_id}:{payload['text'][:50]}"
            if rh in self._relay_hashes:
                continue
            self._relay_hashes.add(rh)
            if len(self._relay_hashes) > 300:
                self._relay_hashes = set(list(self._relay_hashes)[-150:])

            if self._is_http_node(target_id):
                self._handle_http_node(agent_id, target_id, payload)
                continue

            # Relay 到下游 agent（用 on_task_dispatch 自動判斷 API/PTY）
            msg = format_for_target(agent_id, payload)
            self.on_task_dispatch(agent_id, target_id, msg)

    # ═══════════════════════════════════════
    # HTTP Node（action: api 的節點）
    # ═══════════════════════════════════════

    def _is_http_node(self, agent_id: str) -> bool:
        path = TEAM_DIR / f"{agent_id}.md"
        if path.exists():
            try:
                content = path.read_text(encoding="utf-8")
                return "action: api" in content.lower() or "type: api" in content.lower()
            except Exception:
                pass
        return False

    def _get_http_config(self, agent_id: str) -> dict:
        path = TEAM_DIR / f"{agent_id}.md"
        config = {"url": "", "method": "POST", "headers": {}, "body_mode": "json"}
        if path.exists():
            try:
                content = path.read_text(encoding="utf-8")
                for line in content.split("\n"):
                    line = line.strip()
                    if line.startswith("api_url:"):
                        config["url"] = line.split(":", 1)[1].strip().strip('"')
                    elif line.startswith("api_method:"):
                        config["method"] = line.split(":", 1)[1].strip().strip('"')
                    elif line.startswith("api_header_"):
                        parts = line.split(":", 1)
                        hdr_name = parts[0].replace("api_header_", "")
                        config["headers"][hdr_name] = parts[1].strip().strip('"')
            except Exception:
                pass
        return config

    def _handle_http_node(self, source_id: str, target_id: str, payload: dict) -> None:
        config = self._get_http_config(target_id)
        self.set_status(source_id, target_id, "pulsing")
        self._log_task(source_id, target_id, f"HTTP: {config.get('url', '?')}", "executing")

        if self._loop:
            asyncio.run_coroutine_threadsafe(
                self._do_http_call(source_id, target_id, payload, config),
                self._loop,
            )

    async def _do_http_call(self, source_id, target_id, payload, config):
        try:
            result = await execute_api(payload, config)
            self._api_results[target_id] = result
            if result["ok"]:
                self.set_status(source_id, target_id, "idle")
                self._log_task(source_id, target_id,
                               f"HTTP {result['status']} OK", "api_ok")
            else:
                self.set_status(source_id, target_id, "error",
                                result.get("error", "HTTP failed"))
                self._log_task(source_id, target_id,
                               f"HTTP ERR: {result.get('error', '')}", "api_error")
        except Exception as e:
            self.set_status(source_id, target_id, "error", str(e))
            self._api_results[target_id] = {"status": 0, "ok": False, "error": str(e)}

    # ═══════════════════════════════════════
    # 內部工具
    # ═══════════════════════════════════════

    def _find_terminal(self, agent_id: str):
        if not self._pty:
            return None
        term = self._pty.get(agent_id)
        if term:
            return term
        for aid in self._pty.terminals:
            if aid.lower() == agent_id.lower():
                return self._pty.get(aid)
        return None

    def _schedule_idle(self, from_id: str, to_id: str, delay: float) -> None:
        key = f"{from_id}→{to_id}"
        if self._loop:
            self._loop.call_later(delay, lambda: self.set_status(from_id, to_id, "idle"))

    def _log_task(self, source: str, target: str, content: str, status: str) -> None:
        self._task_log.append({
            "source": source, "target": target,
            "content": content[:200], "status": status,
            "time": time.time(),
        })
        if len(self._task_log) > 100:
            self._task_log = self._task_log[-50:]

    def get_task_log(self) -> list[dict]:
        return self._task_log[-50:]

    # ═══════════════════════════════════════
    # 持久化
    # ═══════════════════════════════════════

    def _load_flow(self) -> None:
        if FLOW_FILE.exists():
            try:
                data = json.loads(FLOW_FILE.read_text(encoding="utf-8"))
                self.flow_map = {}
                for key, conn in data.items():
                    if isinstance(conn, dict) and "from" in conn and "to" in conn:
                        conn["status"] = "idle"
                        self.flow_map[key] = conn
                logger.info(f"[Dispatcher] 載入 {len(self.flow_map)} 條連線")
            except Exception as e:
                logger.error(f"[Dispatcher] 載入失敗: {e}")
                self.flow_map = {}
        elif (Path(__file__).parent.parent / "_config" / "topology.json").exists():
            self._migrate_topology()

    def _save_flow(self) -> None:
        FLOW_FILE.parent.mkdir(exist_ok=True)
        save_data = {}
        for key, conn in self.flow_map.items():
            save_data[key] = {"from": conn["from"], "to": conn["to"]}
        FLOW_FILE.write_text(json.dumps(save_data, ensure_ascii=False, indent=2), encoding="utf-8")

    def _migrate_topology(self) -> None:
        old_file = Path(__file__).parent.parent / "_config" / "topology.json"
        try:
            data = json.loads(old_file.read_text(encoding="utf-8"))
            for conn in data.get("connections", []):
                src, tgt = conn.get("source", ""), conn.get("target", "")
                if src and tgt:
                    self.connect(src, tgt)
        except Exception:
            pass


# 全局實例
dispatcher = Dispatcher()
