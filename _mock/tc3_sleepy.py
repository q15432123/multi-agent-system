"""TestCase 3: 睡仙 — 測試 Nudge 機制
啟動 → 印一行就沉默 → Watcher 15s 觸發 nudge
收到 nudge 後立即甦醒輸出 DONE

關鍵實作：
- threading.Thread 持續監聽 stdin（非阻塞主線程）
- 主線程用 threading.Event.wait() 短間隔循環（非長 sleep）
- 收到含 "stalled" / "SYSTEM" 的 stdin → 立即設 event → 主線程甦醒
"""
import sys
import time
import threading

wake_event = threading.Event()
nudge_received = False


def stdin_watcher():
    """持續讀 stdin，偵測到 nudge 關鍵字就設 event"""
    global nudge_received
    while True:
        try:
            line = sys.stdin.readline()
            if not line:
                time.sleep(0.1)
                continue
            text = line.strip()
            if not text:
                continue

            print(f"[TC3] stdin> {text[:80]}", flush=True)

            # 偵測 nudge 關鍵字（Watcher 注入的 [SYSTEM] 訊息）
            lower = text.lower()
            if any(kw in lower for kw in ("stalled", "system", "sys_status", "finished")):
                nudge_received = True
                wake_event.set()  # 喚醒主線程
                return

        except EOFError:
            break
        except Exception:
            time.sleep(0.1)


# 啟動 stdin 監聽線程
reader = threading.Thread(target=stdin_watcher, daemon=True)
reader.start()

# 主線程：印一行後假裝工作然後沉默
print("[TC3] Starting task... initializing...", flush=True)
time.sleep(0.5)
print("[TC3] Loading dependencies...", flush=True)

# 沉默等待 — 用短間隔 Event.wait 而非長 sleep
# 這樣 wake_event.set() 能在毫秒內喚醒
for _ in range(120):  # 最多等 120 秒
    if wake_event.wait(timeout=1.0):
        break

if nudge_received:
    print("[TC3] *wakes up* Oh! I was sleeping. Let me finish...", flush=True)
    time.sleep(0.5)
    print("[TC3] Compiling final output...", flush=True)
    time.sleep(0.5)
    print("[TC3] Result: login page with validation built successfully.", flush=True)
    print('{"sys_status":"DONE"}', flush=True)
else:
    print("[TC3] Waking up on timeout fallback.", flush=True)
    print('{"sys_status":"DONE"}', flush=True)

# Stay alive
while True:
    time.sleep(60)
