"""
MuteAgent 端對端實測腳本
=========================
這不是檢查檔案存不存在，而是實際打 API 看系統有沒有在動。

前置條件：
  1. python run.py 已經在跑（port 3000）
  2. providers.json 至少有一個能用的 LLM provider

用法：
  python e2e_test.py

它會依序測試：
  Test 1：agent 能不能回話
  Test 2：agent 能不能用 write_file tool 寫檔案
  Test 3：task queue 有沒有排隊（同時丟兩個任務）
  Test 4：context.json 有沒有被寫入（memory persistence）
  Test 5：code block 有沒有自動存檔
"""

import asyncio
import aiohttp
import json
import time
import os
from pathlib import Path

BASE = "http://localhost:3000"

G = "\033[92m"
R = "\033[91m"
Y = "\033[93m"
B = "\033[96m"
DIM = "\033[2m"
RESET = "\033[0m"

results = []

def log(status, test_name, detail=""):
    icon = {"PASS": f"{G}✅", "FAIL": f"{R}❌", "WARN": f"{Y}⚠️"}[status]
    results.append(status)
    print(f"  {icon} {test_name}{RESET}")
    if detail:
        print(f"     {DIM}{detail}{RESET}")


async def api_post(path, body, timeout=30):
    """POST 到 MuteAgent API，回傳 response body"""
    async with aiohttp.ClientSession() as session:
        try:
            async with session.post(
                f"{BASE}{path}",
                json=body,
                timeout=aiohttp.ClientTimeout(total=timeout)
            ) as resp:
                text = await resp.text()
                try:
                    return resp.status, json.loads(text)
                except:
                    return resp.status, {"raw": text}
        except asyncio.TimeoutError:
            return 0, {"error": "timeout"}
        except aiohttp.ClientConnectorError:
            return -1, {"error": "連不上 localhost:3000，確認 run.py 有在跑"}


async def api_get(path, timeout=10):
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(
                f"{BASE}{path}",
                timeout=aiohttp.ClientTimeout(total=timeout)
            ) as resp:
                text = await resp.text()
                try:
                    return resp.status, json.loads(text)
                except:
                    return resp.status, {"raw": text}
        except:
            return -1, {"error": "request failed"}


# ══════════════════════════════════════
#  Test 0：伺服器活著嗎
# ══════════════════════════════════════

async def test0_server_alive():
    print(f"\n{B}[Test 0] 伺服器連線{RESET}")
    
    status, body = await api_get("/api/agents")
    
    if status == -1:
        log("FAIL", "伺服器連不上", "請先執行 python run.py")
        return False
    
    if status == 200:
        agents = body if isinstance(body, list) else body.get("agents", [])
        log("PASS", f"伺服器正常，找到 {len(agents)} 個 agent")
        return True
    else:
        log("FAIL", f"伺服器回應異常: HTTP {status}", str(body)[:200])
        return False


# ══════════════════════════════════════
#  Test 1：Agent 能回話嗎
# ══════════════════════════════════════

async def test1_agent_can_respond():
    print(f"\n{B}[Test 1] Agent 基本對話{RESET}")
    
    # 用一個簡單問題測試
    status, body = await api_post("/api/agent/run", {
        "agent_id": "boss",
        "message": "回覆我一個字：OK"
    }, timeout=30)

    if status == 200:
        log("PASS", "POST /api/agent/run 回傳 200")
        
        # 檢查回覆內容
        response_text = ""
        if isinstance(body, dict):
            response_text = body.get("response", body.get("output", body.get("result", str(body))))
        
        if len(str(response_text)) > 0:
            log("PASS", "Agent 有回覆內容", str(response_text)[:100])
        else:
            log("WARN", "Agent 回覆為空（可能是 streaming 模式，需要 WebSocket 接收）")
    else:
        log("FAIL", f"Agent 無法回話: HTTP {status}", str(body)[:200])


# ══════════════════════════════════════
#  Test 2：Tool Calling 實測
# ══════════════════════════════════════

async def test2_tool_calling():
    print(f"\n{B}[Test 2] Tool Calling（write_file）{RESET}")
    
    test_agent = "boss"
    test_file = f"_workspaces/{test_agent}/e2e_test_proof.txt"
    
    # 清理之前的測試檔
    Path(test_file).unlink(missing_ok=True)
    
    # 叫 agent 用 write_file 工具
    status, body = await api_post("/api/agent/run", {
        "agent_id": test_agent,
        "message": '請使用 write_file 工具，寫入一個檔案 path="e2e_test_proof.txt" content="TOOL_WORKS"。只執行這個工具，不要做其他事。'
    }, timeout=45)

    if status != 200:
        log("FAIL", f"API 呼叫失敗: HTTP {status}", str(body)[:200])
        return

    # 等一下讓 tool 執行完
    await asyncio.sleep(3)
    
    # 檢查檔案有沒有被建立
    if Path(test_file).exists():
        content = Path(test_file).read_text(encoding="utf-8").strip()
        if "TOOL_WORKS" in content:
            log("PASS", "write_file tool 成功寫入檔案", f"內容: {content}")
        else:
            log("WARN", "檔案存在但內容不符", f"期望含 TOOL_WORKS，實際: {content[:100]}")
        Path(test_file).unlink(missing_ok=True)
    else:
        log("FAIL", "write_file tool 沒有產生檔案",
            "可能原因：LLM 沒有呼叫 tool / tool schema 沒傳給 LLM / executor 路徑錯誤")


# ══════════════════════════════════════
#  Test 3：Task Queue 測試
# ══════════════════════════════════════

async def test3_task_queue():
    print(f"\n{B}[Test 3] Task Queue（同時發兩個任務）{RESET}")
    
    # 同時發兩個任務給同一個 agent
    task1 = api_post("/api/agent/run", {
        "agent_id": "boss",
        "message": "第一個任務：回覆 TASK_ONE"
    }, timeout=45)
    
    task2 = api_post("/api/agent/run", {
        "agent_id": "boss",
        "message": "第二個任務：回覆 TASK_TWO"
    }, timeout=45)
    
    results_pair = await asyncio.gather(task1, task2, return_exceptions=True)
    
    success_count = 0
    for i, r in enumerate(results_pair):
        if isinstance(r, Exception):
            log("FAIL", f"任務 {i+1} 拋出異常", str(r))
        else:
            status, body = r
            if status == 200:
                success_count += 1
            elif status == 202:
                success_count += 1  # 202 = accepted / queued
            else:
                log("WARN", f"任務 {i+1} 回傳 HTTP {status}", str(body)[:100])
    
    if success_count == 2:
        log("PASS", "兩個任務都被接受（沒有覆蓋 / 沒有 crash）")
    elif success_count == 1:
        log("WARN", "只有一個任務成功，另一個可能被拒絕或排隊中")
    else:
        log("FAIL", "兩個任務都失敗")


# ══════════════════════════════════════
#  Test 4：Memory Persistence
# ══════════════════════════════════════

async def test4_memory():
    print(f"\n{B}[Test 4] Memory Persistence（context.json）{RESET}")
    
    # 先發一個任務
    await api_post("/api/agent/run", {
        "agent_id": "boss",
        "message": "記住這個暗號：ELEPHANT_42"
    }, timeout=30)
    
    # 等 agent 執行完
    await asyncio.sleep(5)
    
    # 檢查 context.json 有沒有被建立
    ctx_path = Path("_workspaces/boss/context.json")
    if ctx_path.exists():
        try:
            with open(ctx_path, "r", encoding="utf-8") as f:
                ctx = json.load(f)
            
            if isinstance(ctx, list) and len(ctx) > 0:
                log("PASS", f"context.json 存在，包含 {len(ctx)} 條歷史")
                
                # 檢查最近的訊息有沒有包含我們的暗號
                recent = json.dumps(ctx[-5:]) if len(ctx) >= 5 else json.dumps(ctx)
                if "ELEPHANT_42" in recent:
                    log("PASS", "最近歷史中包含測試訊息")
                else:
                    log("WARN", "歷史中沒找到測試訊息（可能被截斷或格式不同）")
            else:
                log("WARN", "context.json 存在但內容異常", f"type: {type(ctx)}, len: {len(ctx) if isinstance(ctx, list) else 'N/A'}")
        except json.JSONDecodeError:
            log("FAIL", "context.json 不是合法 JSON")
    else:
        log("FAIL", "context.json 不存在", "agent_runner 沒有寫入歷史")


# ══════════════════════════════════════
#  Test 5：Code Block 自動存檔
# ══════════════════════════════════════

async def test5_code_block_save():
    print(f"\n{B}[Test 5] Code Block 自動存檔{RESET}")
    
    test_agent = "boss"
    workspace = Path(f"_workspaces/{test_agent}")
    
    # 清理之前的 generated_code 檔案
    for f in workspace.glob("generated_code*"):
        f.unlink(missing_ok=True)
    
    # 叫 agent 產生一段 python code
    status, body = await api_post("/api/agent/run", {
        "agent_id": test_agent,
        "message": "請寫一段簡單的 Python 程式碼（用 ```python code block），內容是 print('E2E_CODE_TEST')。只給我 code block，不要解釋。"
    }, timeout=30)
    
    await asyncio.sleep(3)
    
    # 檢查有沒有 generated_code 檔案
    code_files = list(workspace.glob("generated_code*"))
    if code_files:
        log("PASS", f"找到 {len(code_files)} 個自動存檔的 code 檔案")
        for cf in code_files:
            content = cf.read_text(encoding="utf-8")
            if "E2E_CODE_TEST" in content or "print" in content:
                log("PASS", f"  {cf.name} 內容正確", content.strip()[:80])
            else:
                log("WARN", f"  {cf.name} 內容可能不符", content.strip()[:80])
    else:
        log("FAIL", "沒有找到 generated_code 檔案",
            "可能原因：LLM 沒有回覆 code block / 提取 regex 沒匹配到 / 寫入路徑錯誤")


# ══════════════════════════════════════
#  主程式
# ══════════════════════════════════════

async def main():
    print(f"\n{B}╔══════════════════════════════════════════╗")
    print(f"║  MuteAgent 端對端實測（E2E Smoke Test）  ║")
    print(f"╚══════════════════════════════════════════╝{RESET}")
    print(f"  {DIM}目標：localhost:3000{RESET}")
    
    # Test 0
    alive = await test0_server_alive()
    if not alive:
        print(f"\n  {R}伺服器沒在跑，後續測試全部跳過。{RESET}")
        print(f"  {Y}請先執行：python run.py{RESET}\n")
        return
    
    # 依序測試
    await test1_agent_can_respond()
    await asyncio.sleep(2)
    
    await test2_tool_calling()
    await asyncio.sleep(2)
    
    await test3_task_queue()
    await asyncio.sleep(5)  # 等 queue 消化
    
    await test4_memory()
    await asyncio.sleep(2)
    
    await test5_code_block_save()
    
    # 總結
    pass_count = results.count("PASS")
    fail_count = results.count("FAIL")
    warn_count = results.count("WARN")
    total = len(results)
    
    print(f"\n{'═'*50}")
    print(f"  {G}{pass_count} PASS{RESET} / {R}{fail_count} FAIL{RESET} / {Y}{warn_count} WARN{RESET} / 共 {total} 項")
    
    if fail_count == 0 and warn_count == 0:
        print(f"\n  {G}🎉 全部通過！系統真的能動！{RESET}")
        print(f"  {G}   可以進 Phase 2 了。{RESET}")
    elif fail_count == 0:
        print(f"\n  {Y}⚠️  基本功能正常但有警告，建議檢查後再進 Phase 2{RESET}")
    else:
        print(f"\n  {R}❌ 有 {fail_count} 項核心功能失敗{RESET}")
        print(f"  {R}   把這段結果貼給 Claude Code 叫它修{RESET}")
    print(f"{'═'*50}\n")


if __name__ == "__main__":
    asyncio.run(main())
