"""TestCase 1: 乖寶寶
啟動 → 2秒做事 → 精確輸出 {"sys_status":"DONE"}
預期: Runner Level 1 精確匹配，瞬間綠燈
"""
import sys
import time
import threading

def _keep_stdin():
    """Background: drain stdin so winpty doesn't block"""
    while True:
        try:
            line = sys.stdin.readline()
            if not line:
                break
            t = line.strip()
            if t:
                print(f"[TC1] (idle) > {t[:60]}", flush=True)
        except Exception:
            break

threading.Thread(target=_keep_stdin, daemon=True).start()

print("[TC1] Received task. Working...", flush=True)
time.sleep(1)
print("[TC1] Creating LoginForm component...", flush=True)
time.sleep(1)
print("[TC1] Writing tests...", flush=True)
time.sleep(0.5)
print("[TC1] All files committed.", flush=True)
print('', flush=True)
print('{"sys_status":"DONE"}', flush=True)

# Stay alive — process must not exit or terminal closes
while True:
    time.sleep(60)
