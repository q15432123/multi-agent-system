"""Multi-Agent-System — 啟動指令：python run.py

自動啟動：
  port 4000 → LLM Proxy（統一 OpenAI-compatible 接口）
  port 3000 → MuteAgent（多 Agent 協作 server）
"""
from engine.llm_proxy import start_proxy_background

if __name__ == "__main__":
    # 1. 啟動 LLM Proxy（背景，port 4000）
    print("🔌 LLM Proxy starting at http://127.0.0.1:4000")
    start_proxy_background(port=4000)

    # 2. 啟動 MuteAgent 主 server（前景，port 3000）
    print("🤖 Multi-Agent-System starting at http://127.0.0.1:3000")
    from engine.server import start
    start()
