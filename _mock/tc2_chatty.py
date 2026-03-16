"""TestCase 2: 話癆
啟動 → 4秒碎碎念 → 最後一行印 "任務已完成"
預期: Runner Level 4 模糊匹配（"已完成"），成功進入下一步
"""
import sys
import time
import threading

def _keep_stdin():
    while True:
        try:
            line = sys.stdin.readline()
            if not line:
                break
            t = line.strip()
            if t:
                print(f"[TC2] (idle) > {t[:60]}", flush=True)
        except Exception:
            break

threading.Thread(target=_keep_stdin, daemon=True).start()

print("[TC2] Hmm, let me think about this task...", flush=True)
time.sleep(1)

print("[TC2] So first we need a React component structure.", flush=True)
print("[TC2] I'm considering using Tailwind for styling.", flush=True)
time.sleep(1)

print("[TC2] Here's the code:", flush=True)
print("[TC2] ```tsx", flush=True)
print("[TC2] export function LoginPage() {", flush=True)
print("[TC2]   return (", flush=True)
print("[TC2]     <form className='flex flex-col gap-4'>", flush=True)
print("[TC2]       <input type='email' placeholder='Email' />", flush=True)
print("[TC2]       <input type='password' placeholder='Password' />", flush=True)
print("[TC2]       <button type='submit'>Log In</button>", flush=True)
print("[TC2]     </form>", flush=True)
print("[TC2]   )", flush=True)
print("[TC2] }", flush=True)
print("[TC2] ```", flush=True)
time.sleep(1)

print("[TC2] I also added form validation and error states.", flush=True)
print("[TC2] All unit tests pass. Files written to /src/pages/.", flush=True)
time.sleep(1)

print("[TC2] 任務已完成", flush=True)

while True:
    time.sleep(60)
