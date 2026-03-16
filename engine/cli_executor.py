"""CLIExecutor — 安全 CLI 執行封裝

Agent 呼叫外部 CLI 工具時的安全層：
1. 強制非互動模式 — stdin=DEVNULL，CLI 要求輸入會直接報錯
2. API Key 安全注入 — 透過 env 傳入，不寫死在指令裡
3. Session 型工具的憑證繼承 — 自動掛載 ~/.aws/ 等

用法：
    result = cli_executor.run("aws s3 ls", env_keys=["AWS_ACCESS_KEY_ID"])
    result = cli_executor.run("gcloud compute list", credentials="gcloud")
"""
import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Optional

logger = logging.getLogger("cli_exec")

# 工作空間根目錄
WORKSPACE_ROOT = Path(__file__).parent.parent / "_workspaces"

# 已知的 Session 型工具 → 憑證目錄對照
CREDENTIAL_MOUNTS = {
    "aws":    {"src": "~/.aws",           "env": "AWS_SHARED_CREDENTIALS_FILE"},
    "gcloud": {"src": "~/.config/gcloud", "env": "CLOUDSDK_CONFIG"},
    "azure":  {"src": "~/.azure",         "env": "AZURE_CONFIG_DIR"},
    "docker": {"src": "~/.docker",        "env": "DOCKER_CONFIG"},
    "kube":   {"src": "~/.kube",          "env": "KUBECONFIG"},
    "ssh":    {"src": "~/.ssh",           "env": ""},
}

# 超時限制
DEFAULT_TIMEOUT = 60  # 秒


def run(
    cmd: str,
    agent_id: str = "",
    env_keys: list[str] | None = None,
    credentials: str = "",
    timeout: int = DEFAULT_TIMEOUT,
    cwd: str = "",
) -> dict:
    """安全執行 CLI 指令。

    Args:
        cmd: 要執行的指令
        agent_id: agent ID（用來決定工作目錄）
        env_keys: 需要注入的環境變數 key 列表（從 .env 或 os.environ 讀取）
        credentials: Session 型工具名稱（aws / gcloud / docker 等）
        timeout: 超時秒數
        cwd: 工作目錄（預設 _workspaces/{agent_id}/）

    Returns:
        {
            "ok": bool,
            "exit_code": int,
            "stdout": str,
            "stderr": str,
            "error": str | None,
        }
    """
    # 1. 準備工作目錄
    if not cwd and agent_id:
        cwd = str(WORKSPACE_ROOT / agent_id)
        os.makedirs(cwd, exist_ok=True)
    elif not cwd:
        cwd = str(WORKSPACE_ROOT)

    # 2. 組裝環境變數
    env = os.environ.copy()

    # 注入指定的 env keys（安全，不暴露在 cmd 字串裡）
    if env_keys:
        for key in env_keys:
            val = os.getenv(key, "")
            if val:
                env[key] = val
            else:
                logger.warning(f"[CLIExec] Env key {key} not found in environment")

    # 3. 掛載 Session 型工具的憑證
    if credentials:
        _mount = CREDENTIAL_MOUNTS.get(credentials)
        if _mount:
            src_path = Path(_mount["src"]).expanduser()
            if src_path.exists():
                if _mount["env"]:
                    # 直接設定環境變數指向主機的憑證目錄
                    if credentials == "aws":
                        # AWS 特殊：指向 credentials 檔案
                        cred_file = src_path / "credentials"
                        if cred_file.exists():
                            env["AWS_SHARED_CREDENTIALS_FILE"] = str(cred_file)
                        config_file = src_path / "config"
                        if config_file.exists():
                            env["AWS_CONFIG_FILE"] = str(config_file)
                    elif credentials == "kube":
                        kube_config = src_path / "config"
                        if kube_config.exists():
                            env["KUBECONFIG"] = str(kube_config)
                    else:
                        env[_mount["env"]] = str(src_path)

                    logger.info(f"[CLIExec] Mounted {credentials} credentials from {src_path}")
                else:
                    logger.info(f"[CLIExec] {credentials} credentials at {src_path} (no env mapping)")
            else:
                logger.warning(f"[CLIExec] {credentials} credentials not found at {src_path}")

    # 4. 執行（強制非互動）
    from engine.syslog import syslog
    import time as _time
    t0 = _time.time()
    logger.info(f"[CLIExec] Running: {cmd[:80]} (agent={agent_id}, timeout={timeout}s)")
    try:
        result = subprocess.run(
            cmd,
            shell=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            cwd=cwd,
            timeout=timeout,
            text=True,
        )
        dur = int((_time.time() - t0) * 1000)

        ok = result.returncode == 0
        out = {
            "ok": ok,
            "exit_code": result.returncode,
            "stdout": result.stdout[:5000] if result.stdout else "",
            "stderr": result.stderr[:2000] if result.stderr else "",
            "error": None if ok else f"Exit code {result.returncode}",
        }

        syslog.log_cli_exec(agent_id or "system", cmd, result.returncode,
                            stderr=result.stderr[:500] if result.stderr else "",
                            duration_ms=dur)
        return out

    except subprocess.TimeoutExpired:
        dur = int((_time.time() - t0) * 1000)
        syslog.log_cli_exec(agent_id or "system", cmd, -1,
                            stderr=f"Timeout after {timeout}s", duration_ms=dur)
        return {
            "ok": False, "exit_code": -1, "stdout": "", "stderr": "",
            "error": f"Timeout after {timeout}s — CLI may require interactive input",
        }

    except Exception as e:
        dur = int((_time.time() - t0) * 1000)
        syslog.log_cli_exec(agent_id or "system", cmd, -1,
                            stderr=str(e), duration_ms=dur)
        return {
            "ok": False, "exit_code": -1, "stdout": "", "stderr": "",
            "error": str(e),
        }


def execute_with_api_key(
    cmd: str,
    env_keys: list[str],
    agent_id: str = "",
    timeout: int = DEFAULT_TIMEOUT,
) -> dict:
    """便捷方法：執行指令並注入 API key 到環境變數。

    Agent 只需說「我要跑這個指令，需要 STRIPE_API_KEY」，
    框架自動從 .env 讀取並安全注入。

    Example:
        execute_with_api_key("stripe charges list --limit 5", ["STRIPE_API_KEY"])
    """
    return run(cmd, agent_id=agent_id, env_keys=env_keys, timeout=timeout)


def mount_credentials(tool_name: str, agent_id: str = "") -> dict:
    """預先檢查並回報某工具的憑證狀態。

    Example:
        status = mount_credentials("aws")
        # {"mounted": True, "path": "C:\\Users\\xxx\\.aws", "tool": "aws"}
    """
    _mount = CREDENTIAL_MOUNTS.get(tool_name)
    if not _mount:
        return {"mounted": False, "error": f"Unknown tool: {tool_name}", "tool": tool_name}

    src_path = Path(_mount["src"]).expanduser()
    if not src_path.exists():
        return {"mounted": False, "error": f"Not found: {src_path}", "tool": tool_name}

    return {"mounted": True, "path": str(src_path), "tool": tool_name}
