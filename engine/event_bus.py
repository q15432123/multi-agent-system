"""Event Bus — asyncio-based pub/sub

事件類型：task_start, task_complete, tool_called, error, agent_spawned
未來可替換為 Redis/Kafka，目前用純 Python callback。

用法：
    from engine.event_bus import bus
    bus.on("task_complete", my_handler)
    await bus.emit("task_complete", {"agent": "jordan", "output": "..."})
"""
import asyncio
import logging
from typing import Callable

logger = logging.getLogger("event_bus")


class EventBus:
    """簡單的 asyncio event bus"""

    def __init__(self) -> None:
        self._handlers: dict[str, list[Callable]] = {}
        self._async_handlers: dict[str, list[Callable]] = {}

    def on(self, event_type: str, handler: Callable) -> None:
        """註冊同步 handler"""
        if event_type not in self._handlers:
            self._handlers[event_type] = []
        self._handlers[event_type].append(handler)

    def on_async(self, event_type: str, handler: Callable) -> None:
        """註冊 async handler"""
        if event_type not in self._async_handlers:
            self._async_handlers[event_type] = []
        self._async_handlers[event_type].append(handler)

    async def emit(self, event_type: str, data: dict) -> None:
        """發送事件到所有 handler"""
        # Sync handlers
        for handler in self._handlers.get(event_type, []):
            try:
                handler(event_type, data)
            except Exception as e:
                logger.error(f"[EventBus] Sync handler error for {event_type}: {e}")

        # Async handlers
        for handler in self._async_handlers.get(event_type, []):
            try:
                await handler(event_type, data)
            except Exception as e:
                logger.error(f"[EventBus] Async handler error for {event_type}: {e}")

        # Wildcard handlers ("*")
        for handler in self._handlers.get("*", []):
            try:
                handler(event_type, data)
            except Exception:
                pass
        for handler in self._async_handlers.get("*", []):
            try:
                await handler(event_type, data)
            except Exception:
                pass

    def emit_sync(self, event_type: str, data: dict) -> None:
        """同步發送（從非 async 環境呼叫）"""
        for handler in self._handlers.get(event_type, []):
            try:
                handler(event_type, data)
            except Exception as e:
                logger.error(f"[EventBus] Sync handler error: {e}")
        for handler in self._handlers.get("*", []):
            try:
                handler(event_type, data)
            except Exception:
                pass

    def off(self, event_type: str, handler: Callable) -> None:
        """取消註冊"""
        if event_type in self._handlers:
            self._handlers[event_type] = [h for h in self._handlers[event_type] if h != handler]
        if event_type in self._async_handlers:
            self._async_handlers[event_type] = [h for h in self._async_handlers[event_type] if h != handler]

    def clear(self) -> None:
        """清除所有 handler"""
        self._handlers.clear()
        self._async_handlers.clear()


# 全局實例
bus = EventBus()
