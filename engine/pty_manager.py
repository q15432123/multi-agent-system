"""Multi-Agent-System PTY Manager — winpty + thread 讀取"""
import asyncio
import logging
import os
import sys
import threading
import time
from typing import Optional

logger = logging.getLogger("pty")

# 找到 CLI 的完整路徑
def _which(cmd: str) -> str:
    """找指令的完整路徑"""
    import shutil
    path = shutil.which(cmd)
    if path:
        return path
    # Windows npm global
    npm_path = os.path.join(os.environ.get('APPDATA', ''), 'npm', f'{cmd}.cmd')
    if os.path.exists(npm_path):
        return npm_path
    return cmd


_MOCK_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "_mock")

CLI_COMMANDS = {
    "claude": {"cmd": _which("claude"), "args": "--dangerously-skip-permissions", "name": "Claude Code"},
    "gemini": {"cmd": _which("gemini"), "args": "", "name": "Gemini CLI"},
    "codex": {"cmd": _which("codex"), "args": "", "name": "Codex CLI"},
    "kimi": {"cmd": _which("kimi"), "args": "--yolo", "name": "Kimi CLI"},
    "cmd": {"cmd": "cmd.exe", "args": "/Q", "name": "Windows CMD"},
    "powershell": {"cmd": "powershell.exe", "args": "-NoLogo", "name": "PowerShell"},
    "bash": {"cmd": _which("bash"), "args": "", "name": "Bash"},
    # Mock: python script per agent — looks for _mock/{agent_id}.py
    "mock": {"cmd": _which("python"), "args": "", "name": "Mock Agent", "mock": True},
}

# Log CLI paths
for k, v in CLI_COMMANDS.items():
    logger.info(f"CLI [{k}]: {v['cmd']}")


class AgentTerminal:
    def __init__(self, agent_id: str, cli_type: str, cwd: str = "") -> None:
        self.agent_id = agent_id
        self.cli_type = cli_type
        self.cwd = cwd
        self.process = None
        self.ws_clients: set[asyncio.Queue] = set()
        self._running = False
        self._loop = None

    def start(self) -> bool:
        from winpty import PtyProcess

        cli = CLI_COMMANDS.get(self.cli_type)
        if not cli:
            logger.error(f"[{self.agent_id}] Unknown CLI: {self.cli_type}")
            return False

        if not self.cwd:
            base = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "_workspaces")
            self.cwd = os.path.join(base, self.agent_id)
        os.makedirs(self.cwd, exist_ok=True)

        cmd = cli["cmd"]
        args = cli["args"]

        # Mock agent: run python _mock/{agent_id}.py
        if cli.get("mock"):
            script = os.path.join(_MOCK_DIR, f"{self.agent_id}.py")
            if not os.path.exists(script):
                logger.error(f"[{self.agent_id}] Mock script not found: {script}")
                return False
            full_cmd = f'{cmd} -u "{script}"'
        else:
            full_cmd = f'{cmd} {args}'.strip()

        try:
            self.process = PtyProcess.spawn(full_cmd, cwd=self.cwd)
            self._running = True
            self._loop = asyncio.get_event_loop()

            # winpty reader — 推到 WebSocket 給前端顯示
            # 前端收到後會回傳給 /api/terminal/output/ 讓 Runner 看到
            t = threading.Thread(target=self._reader_thread, daemon=True)
            t.start()

            logger.info(f"[{self.agent_id}] Started {cli['name']} in {self.cwd} (PID: {self.process.pid})")
            return True
        except Exception as e:
            logger.error(f"[{self.agent_id}] Failed: {e}")
            return False

    def _reader_thread(self) -> None:
        """讀 winpty 輸出 → 推到前端 WebSocket（不依賴 server 讀取做 Runner）"""
        while self._running:
            try:
                if not self.process or not self.process.isalive():
                    break
                data = self.process.read(4096)
                if data and self._loop:
                    # 只推到 WebSocket，不走 Runner（Runner 靠前端回傳）
                    asyncio.run_coroutine_threadsafe(self._ws_only(data), self._loop)
            except EOFError:
                if self._loop:
                    asyncio.run_coroutine_threadsafe(
                        self._ws_only("\r\n[Process exited]\r\n"), self._loop)
                break
            except Exception:
                time.sleep(0.05)

    async def _ws_only(self, data: str) -> None:
        """只推 WebSocket，不走 Runner（Runner 靠前端回傳 /api/terminal/output/）"""
        dead = set()
        for q in self.ws_clients:
            try:
                q.put_nowait(data)
            except asyncio.QueueFull:
                dead.add(q)
        self.ws_clients -= dead

    def stop(self) -> None:
        self._running = False
        if self.process and self.process.isalive():
            try:
                self.process.terminate()
            except Exception:
                pass
        logger.info(f"[{self.agent_id}] Stopped")

    def write(self, text: str) -> None:
        """寫入 PTY"""
        if not self.process or not self.process.isalive():
            return

        if self.cli_type in ("gemini", "kimi"):
            # gemini/kimi: 文字和 \r\n 必須一起發，不能分開
            # 如果是從 xterm 來的逐字輸入（長度=1），直接透傳
            if len(text) <= 2:
                self.process.write(text)
            else:
                # Runner 注入的完整指令：確保結尾有 \r\n
                clean = text.rstrip("\r\n")
                if clean:
                    self.process.write(clean + "\r\n")
        else:
            self.process.write(text)

    async def _broadcast(self, data: str) -> None:
        dead = set()
        for q in self.ws_clients:
            try:
                q.put_nowait(data)
            except asyncio.QueueFull:
                dead.add(q)
        self.ws_clients -= dead

        # Runner
        try:
            from engine.runner import runner
            if runner.is_running:
                is_pm = self._check_is_pm()
                if is_pm:
                    runner.on_pm_output(data)
                else:
                    runner.on_agent_output(self.agent_id, data)
        except Exception:
            pass

    def _check_is_pm(self) -> bool:
        if 'pm' in self.agent_id.lower():
            return True
        team_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "_team")
        f = os.path.join(team_dir, f"{self.agent_id}.md")
        if os.path.exists(f):
            try:
                with open(f, 'r', encoding='utf-8') as fh:
                    return 'pm' in fh.read().lower()
            except Exception:
                pass
        return False

    @property
    def is_alive(self) -> bool:
        return self.process is not None and self.process.isalive()


class PtyManager:
    def __init__(self) -> None:
        self.terminals: dict[str, AgentTerminal] = {}

    def create(self, agent_id: str, cli_type: str, cwd: str = "") -> bool:
        if agent_id in self.terminals:
            self.terminals[agent_id].stop()

        term = AgentTerminal(agent_id, cli_type, cwd)
        if term.start():
            self.terminals[agent_id] = term

            # PM 自動注入提示詞
            if term._check_is_pm():
                prompt = self._build_pm_prompt()
                asyncio.create_task(self._delayed_inject(term, prompt))
            return True
        return False

    async def _delayed_inject(self, term, prompt: str) -> None:
        await asyncio.sleep(8)
        if term.is_alive:
            term.write(prompt + "\r")
            logger.info(f"[{term.agent_id}] 團隊提示詞已注入")

    def _build_pm_prompt(self) -> str:
        team_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "_team")
        workers = []
        if os.path.exists(team_dir):
            for f in os.listdir(team_dir):
                if f.endswith('.md') and not f.startswith('_'):
                    try:
                        with open(os.path.join(team_dir, f), 'r', encoding='utf-8') as fh:
                            content = fh.read()
                        name = f.replace('.md', '')
                        role = ''
                        for line in content.split('\n'):
                            if line.strip().startswith('name:'): name = line.split(':',1)[1].strip().strip('"')
                            if line.strip().startswith('role:'): role = line.split(':',1)[1].strip().strip('"')
                        if 'pm' not in name.lower() and 'project manager' not in role.lower():
                            workers.append({"name": name, "filename": f.replace('.md',''), "role": role})
                    except Exception:
                        pass

        if not workers:
            return "You are PM. No team members yet."

        team_list = "\n".join([f"  - @{a['filename']}: {a['name']} — {a['role']}" for a in workers])
        examples = "\n".join([f"  @{a['filename']}: 寫任務內容" for a in workers[:2]])

        return f"你是PM。你有{len(workers)}個隊友在線。\n隊友：\n{team_list}\n分配任務格式：\n{examples}\n你只負責拆任務分配，不要自己做。"

    def stop(self, agent_id: str):
        if agent_id in self.terminals:
            self.terminals[agent_id].stop()
            del self.terminals[agent_id]

    def stop_all(self):
        for t in self.terminals.values(): t.stop()
        self.terminals.clear()

    def write(self, agent_id: str, text: str) -> bool:
        t = self.terminals.get(agent_id)
        if t and t.is_alive:
            t.write(text)
            return True
        return False

    def get(self, agent_id: str) -> Optional[AgentTerminal]:
        return self.terminals.get(agent_id)

    def list_active(self) -> list[dict]:
        return [{"agent_id": a, "cli_type": t.cli_type, "alive": t.is_alive} for a, t in self.terminals.items()]


pty_manager = PtyManager()
