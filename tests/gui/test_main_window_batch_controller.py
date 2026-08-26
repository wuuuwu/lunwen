from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from PySide6.QtWidgets import QMessageBox

from paper_reviewer.domain.batch import (
    BatchEvent,
    BatchRecord,
    BatchReviewRequest,
    BatchStatus,
)
from paper_reviewer.gui.main_window import MainWindow


class _Worker:
    def __init__(self, batch_id: str, *, running: bool = True) -> None:
        self.values = {"batchId": batch_id}
        self.running = running
        self.cancel_calls = 0

    def property(self, name: str) -> object:
        return self.values.get(name)

    def setProperty(self, name: str, value: object) -> None:
        self.values[name] = value

    def isRunning(self) -> bool:
        return self.running

    def cancel_task(self) -> None:
        self.cancel_calls += 1


class _Message:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str]] = []

    def show_message(self, message: str, *, severity: str) -> None:
        self.messages.append((message, severity))


class _Button:
    def __init__(self, *, hidden: bool = False) -> None:
        self.hidden = hidden
        self.enabled = True
        self.tooltip = ""
        self.description = ""

    def isHidden(self) -> bool:
        return self.hidden

    def setEnabled(self, enabled: bool) -> None:
        self.enabled = enabled

    def setToolTip(self, value: str) -> None:
        self.tooltip = value

    def setAccessibleDescription(self, value: str) -> None:
        self.description = value

    def property(self, _name: str) -> object:
        return False


class _BatchDetail:
    def __init__(self, batch_id: str = "") -> None:
        self._batch_id = batch_id
        self.batch: BatchRecord | None = None
        self.events: list[BatchEvent] = []
        self.busy: list[tuple[bool, str]] = []
        self.errors: list[str] = []
        self.message = _Message()

    @property
    def batch_id(self) -> str:
        return self._batch_id

    def set_batch(self, record: BatchRecord) -> None:
        self.batch = record
        self._batch_id = record.batch_id

    def apply_event(self, event: BatchEvent) -> None:
        self.events.append(event)

    def set_busy(self, busy: bool, *, action: str = "") -> None:
        self.busy.append((busy, action))

    def show_error(self, message: str) -> None:
        self.errors.append(message)

    def clear(self) -> None:
        self.batch = None
        self._batch_id = ""


class _Pages:
    def __init__(self, current: object) -> None:
        self.current = current

    def currentWidget(self) -> object:
        return self.current

    def setCurrentWidget(self, value: object) -> None:
        self.current = value


def _record(batch_id: str, status: BatchStatus) -> BatchRecord:
    return BatchRecord.model_construct(
        batch_id=batch_id,
        status=status,
        items=[],
    )


def _event(batch_id: str, *, run_id: str = "") -> BatchEvent:
    payload: dict[str, object] = {}
    if run_id:
        payload["run_id"] = run_id
    return BatchEvent(
        batch_id=batch_id,
        event_type="batch_run_event",
        status=BatchStatus.RUNNING,
        message=f"{batch_id} 实时事件",
        payload=payload,
    )


def _event_window(worker: _Worker, detail_batch_id: str) -> Any:
    detail = _BatchDetail(detail_batch_id)
    window = SimpleNamespace(
        _batch_worker=worker,
        _batch_worker_generation=2,
        _running_batch_id=worker.values["batchId"],
        _running_batch_run_ids=set(),
        _run_to_batch={},
        preferences=SimpleNamespace(active_batch_id=None),
        batch_detail_page=detail,
        pages=_Pages(detail),
        statuses=[],
    )
    window._worker_batch_id = MainWindow._worker_batch_id
    window._set_active_batch_preference = lambda value: setattr(
        window.preferences, "active_batch_id", value
    )
    window._remember_batch_record = lambda _value: None
    window._can_apply_live_batch_event = (
        lambda batch_id: MainWindow._can_apply_live_batch_event(window, batch_id)
    )
    window._set_batch_detail_record = lambda _record, live_worker=False: None
    window._is_batch_detail_visible = (
        lambda batch_id: MainWindow._is_batch_detail_visible(window, batch_id)
    )
    window._set_global_status = lambda *args, **kwargs: window.statuses.append(
        kwargs.get("fallback", "")
    )
    return window


def test_running_batch_identity_is_independent_from_viewed_batch_and_stop_target(
    monkeypatch: Any,
) -> None:
    worker = _Worker("batch-a")
    window = _event_window(worker, "batch-b")

    MainWindow._batch_event(window, _event("batch-a", run_id="run-a"), worker, 2)

    assert window.preferences.active_batch_id == "batch-a"
    assert window._running_batch_run_ids == {"run-a"}
    assert window._run_to_batch == {"run-a": "batch-a"}
    assert window.batch_detail_page.events == []
    assert window.statuses == ["batch-a 实时事件"]

    paused: list[tuple[str, int]] = []
    window._batch_worker_owns = lambda batch_id: MainWindow._batch_worker_owns(
        window, batch_id
    )
    window._is_batch_detail_visible = lambda batch_id: batch_id == "batch-b"
    window._persist_paused_batch = (
        lambda batch_id, generation: paused.append((batch_id, generation))
    )
    window._batch_view_generation = 7
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *args, **kwargs: QMessageBox.StandardButton.Yes,
    )

    MainWindow.stop_batch(window, "batch-b")

    assert worker.cancel_calls == 0
    assert paused == [("batch-b", 7)]


def test_new_worker_accepts_second_batch_realtime_events_without_old_id_filter() -> None:
    first = _Worker("batch-a")
    window = _event_window(first, "batch-a")
    MainWindow._batch_event(window, _event("batch-a"), first, 2)
    assert [event.batch_id for event in window.batch_detail_page.events] == ["batch-a"]

    second = _Worker("")
    window._batch_worker = second
    window._batch_worker_generation = 3
    window._running_batch_id = ""
    window._running_batch_run_ids.clear()
    window.batch_detail_page._batch_id = "batch-b"

    MainWindow._batch_event(window, _event("batch-b"), second, 3)

    assert window._running_batch_id == "batch-b"
    assert second.property("batchId") == "batch-b"
    assert [event.batch_id for event in window.batch_detail_page.events] == [
        "batch-a",
        "batch-b",
    ]


def test_starting_new_batch_clears_previous_execution_identity() -> None:
    detail = _BatchDetail("batch-old")
    started: list[tuple[str, str]] = []
    window = SimpleNamespace(
        _running_batch_id="batch-old",
        _running_batch_run_ids={"run-old"},
        _running_batch_record=_record("batch-old", BatchStatus.PAUSED),
        _batch_view_generation=5,
        preferences=SimpleNamespace(active_batch_id="batch-old"),
        new_review_page=SimpleNamespace(set_busy=lambda _busy: None),
        batch_detail_page=detail,
        pages=_Pages(object()),
        service=object(),
    )
    window._has_active_evaluation_worker = lambda: False
    window._save_preferences = lambda: True
    window._start_batch_worker = (
        lambda _operation, *, action, batch_id: started.append((action, batch_id))
    )
    window._set_global_status = lambda *args, **kwargs: None
    request = BatchReviewRequest.model_construct()

    MainWindow.start_batch(window, request)

    assert window._running_batch_id == ""
    assert window._running_batch_run_ids == set()
    assert window._running_batch_record is None
    assert window.preferences.active_batch_id is None
    assert window._batch_view_generation == 6
    assert detail.batch_id == ""
    assert started == [("start", "")]


def test_late_batch_load_never_replaces_a_newer_view() -> None:
    detail = _BatchDetail("batch-b")
    pages = _Pages(detail)
    window = SimpleNamespace(
        _batch_view_generation=4,
        batch_detail_page=detail,
        pages=pages,
        remembered=[],
        statuses=[],
    )
    window._remember_batch_record = window.remembered.append
    window._set_batch_detail_record = detail.set_batch
    window._set_global_status = lambda *args, **kwargs: window.statuses.append(
        kwargs.get("fallback", "")
    )
    window._batch_load_failed = lambda *args, **kwargs: None

    MainWindow._batch_loaded(window, _record("batch-a", BatchStatus.PAUSED), "batch-a", 3)

    assert detail.batch_id == "batch-b"
    assert window.remembered == []
    assert pages.currentWidget() is detail

    MainWindow._batch_loaded(window, _record("batch-c", BatchStatus.PAUSED), "batch-c", 4)

    assert detail.batch_id == "batch-c"
    assert [record.batch_id for record in window.remembered] == ["batch-c"]
    assert window.statuses == ["批次详情已加载"]


def test_late_metadata_or_pause_result_does_not_overwrite_current_batch() -> None:
    detail = _BatchDetail("batch-b")
    window = SimpleNamespace(
        _batch_view_generation=9,
        batch_detail_page=detail,
        pages=_Pages(detail),
        remembered=[],
        completed=[],
        refreshed_batches=0,
        refreshed_runs=0,
    )
    window._remember_batch_record = window.remembered.append
    window._remember_batch_completion = window.completed.append
    window._is_batch_detail_visible = (
        lambda batch_id: MainWindow._is_batch_detail_visible(window, batch_id)
    )
    window._set_batch_detail_record = detail.set_batch
    window._set_global_status = lambda *args, **kwargs: None
    window.refresh_batches = lambda: setattr(
        window, "refreshed_batches", window.refreshed_batches + 1
    )
    window.refresh_runs = lambda: setattr(
        window, "refreshed_runs", window.refreshed_runs + 1
    )
    window._batch_mutation_failed = lambda *args, **kwargs: None

    MainWindow._batch_mutation_completed(
        window,
        _record("batch-a", BatchStatus.PAUSED),
        expected_batch_id="batch-a",
        view_generation=8,
        success_message="不应显示",
    )

    assert detail.batch_id == "batch-b"
    assert detail.busy == []
    assert [record.batch_id for record in window.remembered] == ["batch-a"]
    assert window.refreshed_batches == 1
    assert window.refreshed_runs == 1


def test_stale_running_record_is_presented_as_resumable_without_mutating_source() -> None:
    record = _record("interrupted", BatchStatus.RUNNING)
    window = SimpleNamespace(_batch_worker=None, _running_batch_id="")
    window._batch_worker_owns = lambda batch_id: MainWindow._batch_worker_owns(
        window, batch_id
    )

    displayed = MainWindow._batch_record_for_display(window, record)

    assert record.status is BatchStatus.RUNNING
    assert displayed.status is BatchStatus.PAUSED


def test_batch_owned_run_cannot_be_cancelled_or_resumed_directly(
    monkeypatch: Any,
) -> None:
    worker = _Worker("batch-a")
    message = _Message()
    cancel_button = _Button()
    resume_button = _Button()
    detail = SimpleNamespace(
        message=message,
        cancel_button=cancel_button,
        resume_button=resume_button,
    )
    window = SimpleNamespace(
        _batch_worker=worker,
        _running_batch_id="batch-a",
        _running_batch_run_ids={"run-a"},
        _run_to_batch={"run-a": "batch-a"},
        _batch_locked_run_id="",
        run_detail_page=detail,
        statuses=[],
        _review_worker=None,
    )
    window._is_run_managed_by_live_batch = (
        lambda run_id: MainWindow._is_run_managed_by_live_batch(window, run_id)
    )
    window._guard_batch_managed_run_action = (
        lambda run_id: MainWindow._guard_batch_managed_run_action(window, run_id)
    )
    window._set_global_status = lambda *args, **kwargs: window.statuses.append(
        kwargs.get("fallback", "")
    )
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("batch-owned run must not open the direct cancel dialog")
        ),
    )

    MainWindow._apply_batch_managed_run_policy(window, "run-a")
    MainWindow.cancel_review(window, "run-a")
    MainWindow.resume_review(window, "run-a")

    assert not cancel_button.enabled
    assert not resume_button.enabled
    assert window._batch_locked_run_id == "run-a"
    assert len(message.messages) == 3
    assert all("停止批次" in value[0] for value in message.messages)
    assert worker.cancel_calls == 0
