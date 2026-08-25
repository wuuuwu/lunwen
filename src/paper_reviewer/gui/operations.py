"""Bookkeeping helpers for GUI-owned asynchronous operations.

The application intentionally keeps one ``QThread`` per operation.  This
module owns only the lifecycle bookkeeping around those threads; it does not
know anything about a page, a service method, or a domain status.  Keeping the
bookkeeping in one place makes it harder for a newly added operation to miss
the common cleanup path used during window shutdown.
"""

from __future__ import annotations

from collections.abc import Callable

from paper_reviewer.gui.worker import AsyncTaskThread

WorkerFinished = Callable[[AsyncTaskThread], None]


class AsyncOperationRegistry:
    """Track GUI worker threads until Qt reports that they have finished.

    ``workers`` is intentionally exposed as the live list used by
    :class:`MainWindow`.  A few integrations inspect that list while handling
    application shutdown, so changing it to a copied snapshot would subtly
    change the shutdown race behavior.
    """

    def __init__(self) -> None:
        self.workers: list[AsyncTaskThread] = []

    def track(self, worker: AsyncTaskThread, on_finished: WorkerFinished) -> None:
        """Register *worker* and invoke ``on_finished`` exactly once."""

        self.workers.append(worker)
        finished_called = False

        def finished() -> None:
            nonlocal finished_called
            if finished_called:
                return
            finished_called = True
            if worker in self.workers:
                self.workers.remove(worker)
            on_finished(worker)
            worker.deleteLater()

        worker.finished.connect(finished)

    def running(self) -> list[AsyncTaskThread]:
        """Return a stable snapshot of currently running workers."""

        return [worker for worker in self.workers if worker.isRunning()]

    def cancel_running(self) -> list[AsyncTaskThread]:
        """Request cancellation for every currently running worker.

        Returning the snapshot lets the caller wait for exactly the workers
        that were active at the point of the request.  New operations started
        later are not accidentally included in the shutdown wait.
        """

        workers = self.running()
        for worker in workers:
            worker.cancel_task()
        return workers
