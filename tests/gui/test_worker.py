from __future__ import annotations

import asyncio

from PySide6.QtCore import QThread
from PySide6.QtWidgets import QApplication

from paper_reviewer.gui.operations import AsyncOperationRegistry
from paper_reviewer.gui.worker import AsyncTaskThread


def test_worker_honors_cancel_requested_before_thread_starts(qtbot: object) -> None:
    async def operation(_emit: object) -> None:
        await asyncio.Event().wait()

    worker = AsyncTaskThread(operation)
    worker.cancel_task()

    with qtbot.waitSignal(worker.task_cancelled, timeout=3000):  # type: ignore[attr-defined]
        worker.start()

    assert worker.wait(3000)


def test_worker_forwards_events_to_the_gui_thread(
    qapp: QApplication, qtbot: object
) -> None:
    event = {"status": "awaiting_hard_rule_confirmation"}
    received: list[object] = []
    receiving_threads: list[QThread] = []

    async def operation(emit: object) -> str:
        assert callable(emit)
        emit(event)  # type: ignore[operator]
        await asyncio.sleep(0)
        return "done"

    worker = AsyncTaskThread(operation)

    def on_event(value: object) -> None:
        received.append(value)
        receiving_threads.append(QThread.currentThread())

    worker.event_received.connect(on_event)
    with qtbot.waitSignal(worker.completed, timeout=3000):  # type: ignore[attr-defined]
        worker.start()

    assert worker.wait(3000)
    assert received == [event]
    assert receiving_threads == [qapp.thread()]


def test_operation_registry_cleans_finished_worker(
    qapp: QApplication, qtbot: object
) -> None:
    registry = AsyncOperationRegistry()
    cleaned: list[AsyncTaskThread] = []

    async def operation(_emit: object) -> str:
        await asyncio.sleep(0)
        return "done"

    worker = AsyncTaskThread(operation)
    registry.track(worker, cleaned.append)
    assert registry.workers == [worker]

    with qtbot.waitSignal(worker.finished, timeout=3000):  # type: ignore[attr-defined]
        worker.start()

    assert worker.wait(3000)
    qapp.processEvents()
    assert registry.workers == []
    assert cleaned == [worker]
