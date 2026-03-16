"""Task Queue — 每個 Agent 一個 asyncio.Queue

確保同一 agent 同一時間只處理一個任務，新任務排隊不覆蓋。
"""
import asyncio
import logging
import threading
import time
from typing import Optional

logger = logging.getLogger("queue")


class TaskQueue:
    """每個 agent 的任務佇列管理"""

    def __init__(self) -> None:
        self._queues: dict[str, asyncio.Queue] = {}
        self._workers: dict[str, bool] = {}  # agent_id → is_running
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        logger.info("[Queue] Started")

    def enqueue(self, agent_id: str, content: str, source: str = "") -> int:
        """將任務加入 agent 的 queue，回傳目前 queue 深度"""
        if agent_id not in self._queues:
            self._queues[agent_id] = asyncio.Queue(maxsize=50)

        q = self._queues[agent_id]
        task = {"content": content, "source": source, "time": time.time()}

        try:
            q.put_nowait(task)
        except asyncio.QueueFull:
            logger.warning(f"[Queue] {agent_id} queue full, dropping task")
            return -1

        depth = q.qsize()
        logger.info(f"[Queue] +Task → {agent_id} (depth={depth})")

        # 如果 worker 沒在跑，啟動
        if not self._workers.get(agent_id):
            self._start_worker(agent_id)

        return depth

    def _start_worker(self, agent_id: str) -> None:
        """啟動 agent 的 worker（背景消費 queue）"""
        if self._workers.get(agent_id):
            return
        self._workers[agent_id] = True

        t = threading.Thread(target=self._worker_loop, args=(agent_id,), daemon=True)
        t.start()

    def _worker_loop(self, agent_id: str) -> None:
        """Worker 迴圈：逐一處理 queue 中的任務"""
        from engine.agent_runner import agent_runner

        q = self._queues.get(agent_id)
        if not q:
            self._workers[agent_id] = False
            return

        while True:
            try:
                # 非阻塞取任務
                try:
                    task = q.get_nowait()
                except asyncio.QueueEmpty:
                    break

                content = task["content"]
                logger.info(f"[Queue] Processing: {agent_id} ← {content[:60]}")

                # 同步執行（agent_runner._run_sync 是阻塞的）
                ctx = agent_runner.get_or_create(agent_id)
                ctx.status = "running"
                ctx.output_buffer = ""
                ctx.last_output = ""
                agent_runner._run_sync(agent_id, content)

                # 短暫等待避免連續任務打太快
                time.sleep(0.5)

            except Exception as e:
                logger.error(f"[Queue] Worker error for {agent_id}: {e}")
                time.sleep(1)

        self._workers[agent_id] = False
        logger.info(f"[Queue] Worker idle: {agent_id}")

    def get_depth(self, agent_id: str) -> int:
        """取得某 agent 的 queue 深度"""
        q = self._queues.get(agent_id)
        return q.qsize() if q else 0

    def get_all_depths(self) -> dict[str, int]:
        """取得所有 agent 的 queue 深度"""
        return {aid: q.qsize() for aid, q in self._queues.items()}


# 全局實例
task_queue = TaskQueue()
