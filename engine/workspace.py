"""WorkspaceManager — Agent 工作空間管理

每個 Agent 有獨立的 _workspaces/{agent_id}/ 目錄。
- Dispatcher 啟動 agent 時自動建立
- Agent 只能在自己的目錄下讀寫
- 提供檔案清單 API
"""
import os
from pathlib import Path
from typing import Optional

WORKSPACE_ROOT = Path(__file__).parent.parent / "_workspaces"


def ensure_workspace(agent_id: str) -> str:
    """確保 agent 的 workspace 存在，回傳絕對路徑"""
    ws = WORKSPACE_ROOT / agent_id
    ws.mkdir(parents=True, exist_ok=True)
    return str(ws.resolve())


def get_workspace_path(agent_id: str) -> str:
    """取得 agent 的 workspace 路徑（不自動建立）"""
    ws = WORKSPACE_ROOT / agent_id
    return str(ws.resolve()) if ws.exists() else ""


def list_files(agent_id: str, max_depth: int = 3) -> list[dict]:
    """遞迴列出 agent workspace 的檔案結構。

    Returns:
        [
            {"name": "src", "type": "dir", "children": [...]},
            {"name": "index.js", "type": "file", "size": 1234},
        ]
    """
    ws = WORKSPACE_ROOT / agent_id
    if not ws.exists():
        return []

    def _scan(path: Path, depth: int) -> list[dict]:
        items = []
        try:
            entries = sorted(path.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        except PermissionError:
            return items

        for entry in entries:
            if entry.name.startswith(".") or entry.name == "node_modules" or entry.name == "__pycache__":
                continue

            if entry.is_dir():
                children = _scan(entry, depth + 1) if depth < max_depth else []
                items.append({"name": entry.name, "type": "dir", "children": children})
            else:
                try:
                    size = entry.stat().st_size
                except Exception:
                    size = 0
                items.append({"name": entry.name, "type": "file", "size": size})

        return items

    return _scan(ws, 0)


def read_file(agent_id: str, file_path: str, max_size: int = 50000) -> dict:
    """讀取 agent workspace 中的檔案內容（安全：不允許 ../ 逃逸）"""
    ws = WORKSPACE_ROOT / agent_id
    target = (ws / file_path).resolve()

    # 安全檢查：不允許逃出 workspace
    if not str(target).startswith(str(ws.resolve())):
        return {"ok": False, "error": "Path traversal blocked"}

    if not target.exists():
        return {"ok": False, "error": "File not found"}

    if not target.is_file():
        return {"ok": False, "error": "Not a file"}

    size = target.stat().st_size
    if size > max_size:
        return {"ok": False, "error": f"File too large: {size} bytes (max {max_size})"}

    # 判斷是否為二進位檔案
    ext = target.suffix.lower()
    binary_exts = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".pdf",
                   ".zip", ".tar", ".gz", ".exe", ".dll", ".so", ".wasm"}
    if ext in binary_exts:
        return {"ok": True, "binary": True, "size": size, "name": target.name, "ext": ext}

    try:
        content = target.read_text(encoding="utf-8")
        return {"ok": True, "content": content, "size": size, "name": target.name, "ext": ext}
    except UnicodeDecodeError:
        return {"ok": True, "binary": True, "size": size, "name": target.name, "ext": ext}
