from __future__ import annotations

import asyncio
import threading
import traceback
from collections.abc import Awaitable, Callable
from typing import Any

from PySide6.QtCore import QThread, Signal

EventEmitter = Callable[[object], None]
AsyncOperation = Callable[[EventEmitter], Awaitable[Any]]


class AsyncTaskThread(QThread):
    event_received = Signal(object)
    completed = Signal(object)
    failed = Signal(str, str)
    task_cancelled = Signal()

    def __init__(self, operation: AsyncOperation) -> None:
        super().__init__()
        self.operation = operation
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task[Any] | None = None
        self._cancel_requested = threading.Event()

    def run(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            async def invoke() -> Any:
                # Keep the event bridge as a method instead of passing the
                # Qt signal's bound ``emit`` directly.  This makes it clear
                # that events originate in the asyncio thread and are
                # delivered through Qt's queued signal connection to GUI
                # receivers; no widget is ever touched here.
                return await self.operation(self._emit_event)

            self._task = loop.create_task(invoke())
            if self._cancel_requested.is_set():
                self._task.cancel()
            result = loop.run_until_complete(self._task)
        except asyncio.CancelledError:
            self.task_cancelled.emit()
        except Exception as error:
            self.failed.emit(str(error), traceback.format_exc())
        else:
            self.completed.emit(result)
        finally:
            pending = asyncio.all_tasks(loop)
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            loop.run_until_complete(loop.shutdown_asyncgens())
            loop.close()
            self._task = None
            self._loop = None

    def _emit_event(self, value: object) -> None:
        """Forward a service event without crossing into the GUI directly."""

        self.event_received.emit(value)

    def cancel_task(self) -> None:
        self._cancel_requested.set()
        loop = self._loop
        task = self._task
        if loop is not None and task is not None and not task.done():
            try:
                loop.call_soon_threadsafe(task.cancel)
            except RuntimeError:
                pass
