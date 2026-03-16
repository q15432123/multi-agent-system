# MuteAgent 技術白皮書
**版本 1.0 | 2026-03-16 | 5,667 行 Python + 1,300 行前端**

---

## 1. 一句話介紹

MuteAgent 是一個本地部署的多 AI Agent 協作框架——你給它一個目標，它自動拆成子任務，分配給 20 個專長不同的 AI Agent（用 Claude、Gemini、Kimi 等真實 LLM），每個 Agent 有自己的工作空間、記憶、工具，完成後自動把結果傳給下一個 Agent，直到整個任務做完。

不依賴任何雲端 Agent 平台。不需要付第三方 SaaS 費用。你的 CLI 登入過 Claude/Gemini/Kimi，它就能直接用你的訂閱。

---

## 2. 為什麼要做這個

現有的多 Agent 框架（AutoGPT、CrewAI、MetaGPT）都有同一個問題：**它們假設你有 API key。** 你要去各家開 API 帳號、充值、管理 key，然後按 token 付費。

但如果你已經是 Claude Max / Gemini / Kimi 的訂閱用戶，你的 CLI 工具（`claude`、`gemini`、`kimi`）裡面**已經有 OAuth token 了**。MuteAgent 做的事情是：

1. 自動掃描你電腦上各家 CLI 的 OAuth token
2. 包成一個統一的 OpenAI-compatible API（`localhost:4000`）
3. 所有 Agent 都打這個本地 API，不需要任何 API key

這意味著：**你只要登入過 CLI，Agent 團隊就能用你的訂閱跑任務。**

---

## 3. 系統架構

```
┌─────────────────────────────────────────────────────┐
│  UI (ui.html)                                       │
│  ┌──────────┐  ┌──────────────────────────────────┐ │
│  │ Log Panel │  │ Flow Graph (SVG nodes + edges)   │ │
│  │ (JSONL    │  │ 20 agents + connections          │ │
│  │  stream)  │  │ Floating CLI panels              │ │
│  └──────────┘  └──────────────────────────────────┘ │
│       ↕ WebSocket /ws/logs      ↕ REST + WS        │
├─────────────────────────────────────────────────────┤
│  Main Server (port 3000)           FastAPI          │
│  ┌───────────┐ ┌───────────┐ ┌──────────────────┐  │
│  │ Dispatcher │ │ Planner   │ │ Agent Runner     │  │
│  │ (flowMap)  │ │ (DAG)     │ │ (tool loop)      │  │
│  └─────┬─────┘ └───────────┘ └───────┬──────────┘  │
│        │                              │             │
│  ┌─────┴─────┐  ┌───────────┐  ┌─────┴──────────┐  │
│  │ Watcher   │  │ Relay     │  │ Tool System    │  │
│  │ (timeout) │  │ (format)  │  │ (9 tools)      │  │
│  └───────────┘  └───────────┘  └────────────────┘  │
│  ┌───────────┐  ┌───────────┐  ┌────────────────┐  │
│  │ SysLog    │  │ Memory    │  │ DNA System     │  │
│  │ (JSONL)   │  │ (graph)   │  │ (evolution)    │  │
│  └───────────┘  └───────────┘  └────────────────┘  │
├─────────────────────────────────────────────────────┤
│  LLM Proxy (port 4000)         OpenAI-compatible    │
│  ┌─────────┐ ┌─────────┐ ┌─────────┐ ┌──────────┐  │
│  │  Kimi   │ │ Claude  │ │ Gemini  │ │ OpenAI   │  │
│  │  OAuth  │ │ subproc │ │  OAuth  │ │ API key  │  │
│  └────┬────┘ └────┬────┘ └────┬────┘ └────┬─────┘  │
│       ↓           ↓           ↓            ↓        │
│  ~/.kimi/    claude -p   ~/.gemini/    env var      │
│  credentials  (Max訂閱)   oauth_creds               │
└─────────────────────────────────────────────────────┘
```

### 兩個 Server

| Port | 角色 | 說明 |
|------|------|------|
| **4000** | LLM Proxy | 統一 API gateway。自動掃描各家 CLI token，包成 `/v1/chat/completions` |
| **3000** | Main Server | Agent 協作引擎。Dispatcher、Tool System、UI、所有業務邏輯 |

Agent 呼叫 LLM 時只打 `localhost:4000`，不知道也不在乎後面是 Kimi 還是 Claude。

---

## 4. 核心模組拆解

### 4.1 LLM Proxy — 本地 API Gateway

**檔案：** `engine/llm_proxy.py` (607 行)

**做什麼：** 啟動時掃描 `~/.kimi/`、`~/.claude/`、`~/.gemini/` 的 OAuth token，每 5 分鐘重新掃描。收到 `/v1/chat/completions` 請求時，根據 model 名稱路由到對應 provider。

**路由邏輯：**
```
model 含 "kimi"     → api.kimi.com + UA spoof (claude-code/2.1.76)
model 含 "claude"   → subprocess: claude -p "prompt" --output-format json
model 含 "gemini"   → Google generateContent API + 格式轉換
model 含 "gpt"      → OpenAI API 直接轉發
```

**為什麼 Claude 要用 subprocess：** Anthropic 的 API 不接受 CLI OAuth token（回覆 "OAuth authentication is currently not supported"）。但 `claude -p` 命令可以直接用你的 Max 訂閱，所以 proxy 呼叫 CLI 當 subprocess，把 stdout 包成 OpenAI 格式回傳。

**為什麼 Kimi 要偽裝 User-Agent：** Kimi Coding API（`api.kimi.com/coding/v1`）只允許特定 coding agent 存取。經過測試，`User-Agent: claude-code/2.1.76` 可以通過驗證。OpenAI SDK 預設會覆蓋 User-Agent，所以必須用 `default_headers` 參數強制設定。

**格式轉換：**
- Kimi / OpenAI：原生 OpenAI 格式，直接轉發
- Claude：OpenAI messages → Anthropic Messages API（system 提取、tool schema 轉換、SSE 重新封裝）
- Gemini：OpenAI messages → Google generateContent（role mapping、parts 格式）

### 4.2 Dispatcher — 中央調度器

**檔案：** `engine/dispatcher.py` (348 行)

**做什麼：** 維護 `flowMap`——一個有向圖，定義哪個 Agent 的輸出要送給哪個 Agent。所有任務路由都透過它。

**flowMap 資料結構：**
```json
{
  "boss→alex":   {"from": "boss", "to": "alex",   "status": "idle"},
  "boss→jordan": {"from": "boss", "to": "jordan", "status": "pulsing"},
  "alex→review": {"from": "alex", "to": "review", "status": "error", "error": "offline"}
}
```

`status` 有三種：`idle`（閒置）、`pulsing`（正在傳輸）、`error`（連線錯誤）。前端 UI 根據 status 顯示不同動畫。

**雙模式執行：**
- **API 模式（預設）：** Agent 的 `.md` tags 不含 `mock/cmd/bash` → 走 `agent_runner`，呼叫 LLM API
- **PTY 模式（fallback）：** Tags 含 `mock` → 走 `pty_manager`，用 winpty 跑真實終端

### 4.3 Agent Runner — LLM 執行引擎 + Tool Loop

**檔案：** `engine/agent_runner.py` (457 行)

**做什麼：** 這是 Agent 真正「做事」的地方。收到任務後：

```
while True (最多 10 輪):
    呼叫 LLM（帶 tool schemas）
    if 回覆含 tool_calls:
        for each tool_call:
            執行 tool（write_file / run_command / search_web / ...）
            把結果加回 messages
        continue  ← 再呼叫一次 LLM，讓它看到 tool 結果
    else:
        break  ← 這是最終回覆
```

每個 Agent 有獨立的 `AgentContext`：system prompt、對話歷史、model、provider、WebSocket 訂閱者。

**完成信號偵測（4 級）：**

| Level | 格式 | 可靠度 |
|-------|------|--------|
| 1 | `{"sys_status":"DONE"}` | 最高——精確 JSON |
| 2 | `markTaskComplete(` | 高——function call |
| 3 | `[DONE]` / `[COMPLETE]` / `[完成]` | 中——標記 |
| 4 | `task completed` / `已完成` | 低——自然語言 |

PM 的 system prompt 裡會強制要求 Agent 用 Level 1 格式回報完成。

**記憶持久化：** 每次任務完成後，對話歷史寫入 `_workspaces/{agent_id}/context.json`（最多 50 條）。下次啟動時自動載入。

**Code Block 自動存檔：** 如果 LLM 回覆含 ` ```python ` 等 code block，自動擷取並寫入 `_workspaces/{agent_id}/generated_code.py`。

### 4.4 Tool System — Agent 的手腳

**檔案：** `engine/tools/` (384 行)

**9 個工具：**

| 工具 | 說明 | 安全機制 |
|------|------|---------|
| `write_file` | 寫入 Agent workspace 的檔案 | 路徑穿越阻擋（resolve + startswith check） |
| `read_file` | 讀取 workspace 檔案 | 同上，10KB 上限 |
| `run_command` | 執行 shell 命令 | `stdin=DEVNULL`（非互動）、60s timeout |
| `search_web` | DuckDuckGo 搜尋 | 無需 auth |
| `call_api` | HTTP 請求 | 30s timeout |
| `git_status` | Git 狀態查詢 | 限 workspace 內 |
| `git_commit` | Git 提交 | 同上 |
| `git_diff` | Git diff | 同上 |
| `create_agent` | 動態建立新 Agent | 限 PM 使用、20 Agent 上限 |

**Tool Marketplace 架構：** `tools/` 下有子目錄 `file/`、`shell/`、`web/`、`git/`，每個含 `manifest.json` + `executor.py`。`registry.py` 啟動時自動掃描所有 manifest，動態註冊。

**安全 CLI 封裝（`cli_executor.py`）：**
- `stdin=subprocess.DEVNULL` 強制非互動——CLI 要求輸入密碼時直接報錯，不會掛住
- API key 透過 `env=` 注入，不寫在命令字串裡
- 支援掛載 `~/.aws/`、`~/.docker/`、`~/.kube/` 等憑證目錄

### 4.5 Watcher — 超時監控

**檔案：** `engine/watcher.py` (179 行)

每 3 秒檢查所有正在工作的 Agent：

```
0s    收到任務 → status: working
15s   無輸出 → 自動 nudge（注入提示到 Agent：「你完成了嗎？請輸出 {"sys_status":"DONE"}」）
      status: warned（UI 黃色閃爍）
45s   仍無輸出 → status: timeout（UI 紅色 + FORCE NEXT 按鈕）
      使用者可以手動點 FORCE → 截斷 buffer → 強制推給下游
```

**為什麼需要這個：** LLM 回覆有時候不含完成信號（特別是不支援 function calling 的模型），或者 API 呼叫掛住。沒有 Watcher，整條 pipeline 就會卡死。

### 4.6 Planner — 任務拆解

**檔案：** `engine/planner.py` (149 行)

輸入使用者目標 + 可用 Agent 清單，呼叫 LLM 拆成 subtask DAG：

```json
[
  {"task_id": "t1", "task": "設計登入頁 mockup",    "agent": "luna",   "depends_on": []},
  {"task_id": "t2", "task": "實作登入 API",          "agent": "alex",   "depends_on": ["t1"]},
  {"task_id": "t3", "task": "前端串接 API",          "agent": "jordan", "depends_on": ["t1", "t2"]}
]
```

`depends_on` 表示依賴：t3 必須等 t1 和 t2 都完成才開始。

### 4.7 Reflection — 品質檢查

**檔案：** `engine/reflection.py` (88 行)

Agent 完成後，呼叫另一個 LLM 評分（1-10 分）：

```json
{"score": 7, "issues": ["缺少錯誤處理"], "suggestion": "加上 try-catch", "pass": true}
```

Score < 6 → 自動 retry，把 issues 和 suggestion 加進 prompt。

分數會回寫到 Agent 的 DNA（加權平均：舊分 70% + 新分 30%）。

### 4.8 DNA System — Agent 自我演化

**檔案：** `engine/dna/` (192 行)

每個 Agent 有一份 DNA spec：

```json
{
  "name": "payment_specialist",
  "description": "Stripe and payment API expert",
  "model": "gemini-2.5-flash",
  "skills": ["stripe", "payment_api"],
  "tools": ["write_file", "call_api"],
  "prompt": "You are a senior payment engineer...",
  "score": 7.2,
  "usage_count": 15
}
```

**三個核心操作：**
- **生成：** `dna_generator.py` 用 LLM 根據任務描述自動產生 DNA
- **評分：** Reflection 完成後更新 `score`（加權平均）
- **淘汰：** `garbage_collect()` 把用過 3 次以上且平均分低於 4 的 DNA 移到 `_archived/`

PM Agent 可以用 `create_agent` tool 自動生成+部署新 Agent。整個過程不需要人工介入。

### 4.9 Memory — Agent 記憶

**檔案：** `engine/memory/` (168 行)

兩種記憶系統：

**對話記憶（memory_manager）：** 三個 category——episodic（事件）、semantic（知識）、tasks（任務歷史）。存在 `_workspaces/{agent}/memory/{category}.json`。支援 keyword 搜尋（未來換 vector search）。

**知識圖譜（graph_store）：** RDF 三元組 `(subject, predicate, object)`。例如 `("alex", "built", "login API")`。最多 500 筆，支援任意欄位查詢。

### 4.10 SysLog — 結構化日誌

**檔案：** `engine/syslog.py` (208 行)

所有事件寫入 `_logs/YYYY-MM-DD.jsonl`：

```json
{"timestamp":"2026-03-16T10:30:45Z","level":"ERROR","agent_id":"alex",
 "event_type":"LLM_RATE_LIMIT","message":"kimi: 429 Too Many Requests",
 "extra":{"provider":"kimi","status_code":429,"duration_ms":234}}
```

**三層攔截：** LLM API（token 消耗、耗時、錯誤碼）、CLI 執行（exit code、stderr）、狀態流轉（dispatch、relay、nudge、timeout）。

透過 WebSocket `/ws/logs` 即時推送到前端。

---

## 5. 資料流生命週期

完整的一次任務流程：

```
1. 使用者在 UI 輸入「做一個電商網站」
   → POST /api/agent/run {agent_id:"boss", message:"做一個電商網站"}

2. Dispatcher 路由到 Boss（PM Agent）
   → 檢查 tags: [pm, kimi] → API 模式
   → task_queue.enqueue("boss", content)

3. Agent Runner 執行
   → 載入 _pm/PM.md 作為 system prompt
   → 呼叫 localhost:4000/v1/chat/completions
   → Proxy 路由到 Kimi API（UA spoof）
   → 串流回覆推送到 WebSocket

4. Boss 回覆含 @agent: 指令
   「@jordan: 做前端首頁 @alex: 做商品 API @luna: 做 UI 設計」
   → agent_runner._parse_pm_dispatch() 解析
   → 對每個 @agent: 觸發 dispatcher.on_task_dispatch()

5. 下游 Agent 各自執行
   → jordan: 呼叫 LLM → 用 write_file tool 寫 React 程式碼
   → alex: 呼叫 LLM → 用 run_command 跑 npm init
   → luna: 呼叫 LLM → 產出 UI 規格

6. 完成後自動接力
   → jordan 輸出 {"sys_status":"DONE"}
   → Watcher 偵測 → Dispatcher.on_task_complete()
   → Relay 擷取 code/json → format_for_target()
   → 如果 flowMap 有 jordan→review 連線 → 自動送給 review Agent

7. Reflection 評分
   → 呼叫 LLM 評 jordan 的輸出 → score: 8
   → DNA score 更新

8. 全程記錄
   → SysLog 記錄每一步到 _logs/2026-03-16.jsonl
   → UI 左側即時顯示
```

---

## 6. 檔案結構

```
muteagent/
├── run.py                          # 進入點：啟動 proxy(4000) + server(3000)
├── ui.html                         # 單檔前端（cyberpunk 風格）
├── requirements.txt                # 5 個依賴
├── e2e_test.py                     # 端對端實測腳本
│
├── engine/                         # ─── 核心引擎（5,667 行）───
│   ├── server.py          (1280L)  # FastAPI 主伺服器 + 所有 API 路由
│   ├── llm_proxy.py        (607L)  # 本地 LLM Proxy gateway
│   ├── llm_client.py       (513L)  # 多供應商 LLM 客戶端 + retry
│   ├── agent_runner.py     (457L)  # API 模式 Agent 執行器 + tool loop
│   ├── dispatcher.py       (348L)  # 中央調度器 + flowMap
│   ├── pty_manager.py      (266L)  # winpty 終端管理（PTY fallback）
│   ├── runner.py           (193L)  # PTY 文字解析（舊路徑）
│   ├── syslog.py           (208L)  # JSONL 結構化日誌
│   ├── watcher.py          (179L)  # 超時監控 + 自動 nudge
│   ├── planner.py          (149L)  # 任務拆解 DAG
│   ├── relay.py            (145L)  # 訊息格式化 + payload 擷取
│   ├── queue.py            (106L)  # 每 Agent 任務佇列
│   ├── workspace.py         (97L)  # Agent 工作空間管理
│   ├── cli_executor.py     (197L)  # 安全 CLI 封裝
│   ├── reflection.py        (88L)  # 品質檢查 + 自動 retry
│   ├── event_bus.py         (92L)  # Pub/Sub 事件匯流排
│   │
│   ├── tools/                      # 工具系統（9 個工具）
│   │   ├── registry.py             # 自動掃描 manifest 註冊
│   │   ├── executor.py             # 工具執行實作
│   │   ├── router.py               # tool_calls 路由
│   │   ├── file/manifest.json      # 檔案工具
│   │   ├── shell/manifest.json     # Shell 工具
│   │   ├── web/manifest.json       # Web 工具
│   │   └── git/manifest.json       # Git 工具
│   │
│   ├── memory/                     # 記憶系統
│   │   ├── memory_manager.py       # episodic/semantic/tasks
│   │   └── graph_store.py          # 知識圖譜
│   │
│   └── dna/                        # DNA 演化系統
│       ├── dna_registry.py         # DNA 載入/查詢
│       ├── dna_generator.py        # LLM 自動生成 DNA
│       └── dna_manager.py          # 評分/淘汰
│
├── _team/                          # 20 個 Agent 定義（.md frontmatter）
├── _pm/                            # PM prompt + 狀態板
├── _config/                        # providers.json + flow.json
├── _workspaces/                    # Agent 獨立工作目錄
├── _logs/                          # JSONL 日誌（按日期）
├── _agent_dna/                     # DNA 存儲 + 淘汰歸檔
├── _mock/                          # 4 個 Mock 測試腳本
├── _inbox/                         # 任務 brief
├── _output/                        # Agent 產出
└── _archive/                       # 已歸檔任務
```

---

## 7. 關鍵資料結構

### flowMap（Dispatcher 唯一真相）
```json
{"boss→alex": {"from":"boss","to":"alex","status":"idle"}}
```

### providers.json（多供應商認證）
```json
{
  "kimi": {"auth":"oauth","access_token":"eyJ...","base_url":"https://api.kimi.com/coding/v1","model":"kimi-v1-2p5"},
  "anthropic": {"use_subprocess":true}
}
```

### Agent MD（_team/alex.md）
```yaml
---
name: "Alex"
role: "Backend Engineer"
tags: [kimi, coding, backend]
---
# Alex
## Identity
You are Alex, a backend engineer...
```

### JSONL 日誌
```json
{"timestamp":"...","level":"ERROR","agent_id":"alex","event_type":"LLM_RATE_LIMIT","message":"429","extra":{"duration_ms":234}}
```

### DNA Spec
```json
{"name":"payment_specialist","skills":["stripe"],"tools":["call_api"],"score":7.2,"usage_count":15}
```

---

## 8. 已驗證的功能

| # | 功能 | E2E 測試結果 |
|---|------|-------------|
| 1 | LLM Proxy 自動掃描 CLI token | ✅ Kimi + Claude + Gemini 都偵測到 |
| 2 | Kimi API 呼叫（OAuth + UA spoof） | ✅ 200 回覆正常 |
| 3 | Claude subprocess（Max 訂閱） | ✅ 可行（不能在 Claude Code 內嵌套） |
| 4 | Task Queue 排隊 | ✅ 同時 2 任務不互相覆蓋 |
| 5 | Memory Persistence（context.json） | ✅ 有寫入 |
| 6 | Watcher 超時監控 | ✅ Mock 測試通過 |
| 7 | flowMap 動態路由 | ✅ UI 連線同步 |
| 8 | Tool System（9 tools 註冊） | ✅ Schema 載入正常 |
| 9 | DNA 生成/評分/淘汰 | ✅ 模組載入正常 |
| 10 | JSONL SysLog + WebSocket | ✅ 即時推送 |

---

## 9. 已知限制

### 架構層面
- **無環路偵測：** flowMap 是有向圖但不檢查環路，如果 A→B→A 會無限迴圈
- **async/sync 邊界：** FastAPI 是 async，但 Agent 執行是 sync 線程，透過 Queue 橋接
- **單機限制：** 所有東西跑在一台電腦上，無法分散式部署

### LLM 層面
- **Anthropic API 不接受 OAuth：** 只能用 subprocess 繞，沒有 streaming
- **Gemini OAuth scope 不夠：** 需要額外的 `generative-language` scope
- **Tool calling 依賴模型支援：** Kimi v1-2p5 不回傳 OpenAI-style tool_calls

### 功能層面
- **記憶只有 keyword search：** 沒有 vector embedding，檢索品質有限
- **無 rate limiting：** Agent 可以無限打 LLM API
- **完成信號不保證：** 如果 LLM 不輸出 `{"sys_status":"DONE"}`，只能靠 Watcher timeout

---

## 10. 技術決策紀錄

### 為什麼用 OpenAI SDK 而不是各家原生 SDK？
因為所有主流 LLM provider 都支援 OpenAI-compatible API 格式（Kimi、DeepSeek、Gemini 都有）。用一個 SDK 就能打所有 provider，只要換 `base_url`。唯一的例外是 Anthropic，需要格式轉換。

### 為什麼用 FastAPI 而不是 Flask/Express？
async 原生支援 + WebSocket + Pydantic 型別驗證 + 自動 OpenAPI 文檔。一行 `@app.websocket("/ws/logs")` 就能開 WebSocket。

### 為什麼不用 LangChain？
LangChain 太重，抽象層太多。我們的需求很明確：呼叫 LLM、執行 tool、route 結果。直接用 OpenAI SDK + httpx 更簡單也更容易 debug。

### 為什麼 flowMap 不用 DB？
流量小（最多幾十條連線），JSON 檔案 + 記憶體 dict 足夠。避免引入 SQLite/Redis 的複雜度。未來如果需要持久化查詢，可以換 SQLite。

### 為什麼用 subprocess 呼叫 Claude 而不是 API？
因為 Anthropic 不開放 OAuth API。但 `claude -p` 命令可以直接用你的 Max 訂閱（$100/月的 5x 額度），不需要另外付 API 費用。
