"""TestCase 4: 死機 — 測試 FORCE NEXT
啟動 → 印出 partial result → 永久靜默 → 不回應任何 nudge
預期: STALLED@15s → TIMEOUT@45s → 使用者點 FORCE NEXT 手動推進

stdin 完全不讀取（模擬真正的掛死進程）
stdout 在開頭印出 partial JSON 讓 force-complete 有東西可抓
"""
import sys
import time

print("[TC4] Starting task...", flush=True)
time.sleep(1)
print("[TC4] Initializing database connection...", flush=True)
time.sleep(1)
print("[TC4] Querying user table...", flush=True)
time.sleep(0.5)

# 輸出 partial result — force-complete 時 Watcher 會截取這段
print('[TC4] partial: {"component":"AuthService","methods":["login","register","verify"],"status":"incomplete"}', flush=True)
time.sleep(0.5)

print("[TC4] Connecting to external API...", flush=True)

# 永久掛死 — 不讀 stdin，不輸出任何東西
# 模擬真正的進程卡死（網路超時、死鎖等）
while True:
    time.sleep(3600)
