from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from paper_reviewer.application import service as service_module
from paper_reviewer.application.runtime import review_runtime
from paper_reviewer.application.service import ReviewApplicationService
from paper_reviewer.application.unit_of_work import ApplicationUnitOfWork
from paper_reviewer.config import Settings
from paper_reviewer.domain.provider import (
    ModelApiProtocol,
    ProviderSnapshot,
    endpoint_fingerprint,
)
from paper_reviewer.domain.run import RunRecord


def test_importing_application_service_does_not_load_optional_heavy_modules() -> None:
    script = """
import json
import sys
import paper_reviewer.application.service

blocked = sorted(
    name
    for name in sys.modules
    if name == "pymupdf"
    or name == "openai"
    or name.startswith("openai.")
    or name == "ddgs"
    or name.startswith("ddgs.")
)
print(json.dumps(blocked))
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=True,
        capture_output=True,
        text=True,
    )

    assert json.loads(completed.stdout) == []


@pytest.mark.asyncio
async def test_service_resume_rejects_snapshot_model_change_before_key_lookup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    run = RunRecord(
        run_id="run-provider",
        input_path="paper.pdf",
        input_hash="input",
        config_hash="config",
        rubric_id="rubric",
        provider="openai",
        model="recorded-model",
    )
    base_url = "https://api.openai.com/v1"
    tampered = ProviderSnapshot(
        provider_ref="openai",
        display_name="OpenAI",
        protocol=ModelApiProtocol.CHAT_COMPLETIONS,
        base_url=base_url,
        endpoint_fingerprint=endpoint_fingerprint(
            base_url, ModelApiProtocol.CHAT_COMPLETIONS
        ),
        model="different-model",
    )
    key_requested = False

    class FakeUnitOfWork:
        def __init__(self, _database_url: str) -> None:
            pass

        async def __aenter__(self) -> FakeUnitOfWork:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        @staticmethod
        def require_sessions() -> object:
            return object()

    class FakeRunRepository:
        def __init__(self, _sessions: object) -> None:
            pass

        async def get(self, _run_id: str) -> RunRecord:
            return run

    class FakeProviders:
        def get_snapshot_api_key(self, _snapshot: ProviderSnapshot) -> str:
            nonlocal key_requested
            key_requested = True
            raise AssertionError("API Key must not be requested")

    service = ReviewApplicationService.__new__(ReviewApplicationService)
    service.settings = SimpleNamespace(
        database_url="sqlite+aiosqlite://",
        runs_dir=tmp_path,
    )
    service.providers = FakeProviders()  # type: ignore[assignment]
    monkeypatch.setattr(service_module, "ApplicationUnitOfWork", FakeUnitOfWork)
    monkeypatch.setattr(service_module, "RunRepository", FakeRunRepository)
    monkeypatch.setattr(
        service_module,
        "load_run_snapshots",
        lambda _run_dir: (object(), object()),
    )
    monkeypatch.setattr(service_module, "load_provider_snapshot", lambda _run_dir: tampered)

    with pytest.raises(ValueError, match="Provider 或模型不一致"):
        await service.resume_review(run.run_id)

    assert not key_requested


class _FakeEngine:
    def __init__(self) -> None:
        self.disposed = False

    async def dispose(self) -> None:
        self.disposed = True


@pytest.mark.asyncio
async def test_unit_of_work_disposes_engine_when_initialization_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _FakeEngine()

    async def fail_initialization(candidate: Any) -> None:
        assert candidate is engine
        raise RuntimeError("initialization failed")

    monkeypatch.setattr(
        "paper_reviewer.application.unit_of_work.create_engine", lambda _url: engine
    )
    monkeypatch.setattr(
        "paper_reviewer.application.unit_of_work.initialize_database", fail_initialization
    )

    with pytest.raises(RuntimeError, match="initialization failed"):
        async with ApplicationUnitOfWork("sqlite+aiosqlite://"):
            pytest.fail("the context must not be entered")

    assert engine.disposed


@pytest.mark.asyncio
async def test_unit_of_work_disposes_engine_when_session_factory_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _FakeEngine()

    async def initialize(candidate: Any) -> None:
        assert candidate is engine

    def fail_session_factory(candidate: Any) -> object:
        assert candidate is engine
        raise RuntimeError("session factory failed")

    monkeypatch.setattr(
        "paper_reviewer.application.unit_of_work.create_engine", lambda _url: engine
    )
    monkeypatch.setattr(
        "paper_reviewer.application.unit_of_work.initialize_database", initialize
    )
    monkeypatch.setattr(
        "paper_reviewer.application.unit_of_work.create_session_factory",
        fail_session_factory,
    )

    with pytest.raises(RuntimeError, match="session factory failed"):
        async with ApplicationUnitOfWork("sqlite+aiosqlite://"):
            pytest.fail("the context must not be entered")

    assert engine.disposed


@pytest.mark.asyncio
async def test_unit_of_work_never_reuses_engine_between_operations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engines: list[_FakeEngine] = []

    def make_engine(_url: str) -> _FakeEngine:
        engine = _FakeEngine()
        engines.append(engine)
        return engine

    async def initialize(_engine: Any) -> None:
        return None

    session_factories: list[object] = []

    def make_sessions(_engine: Any) -> object:
        sessions = object()
        session_factories.append(sessions)
        return sessions

    monkeypatch.setattr("paper_reviewer.application.unit_of_work.create_engine", make_engine)
    monkeypatch.setattr(
        "paper_reviewer.application.unit_of_work.initialize_database", initialize
    )
    monkeypatch.setattr(
        "paper_reviewer.application.unit_of_work.create_session_factory", make_sessions
    )

    async with ApplicationUnitOfWork("sqlite+aiosqlite://") as first:
        first_sessions = first.require_sessions()
    async with ApplicationUnitOfWork("sqlite+aiosqlite://") as second:
        second_sessions = second.require_sessions()

    assert len(engines) == 2
    assert all(engine.disposed for engine in engines)
    assert first_sessions is not second_sessions


@pytest.mark.asyncio
async def test_review_runtime_closes_model_after_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeAdapter:
        closed = False

        async def close(self) -> None:
            self.closed = True

    adapter = FakeAdapter()
    monkeypatch.setattr(
        "paper_reviewer.application.runtime.create_model_adapter",
        lambda *args, **kwargs: adapter,
    )
    settings = Settings(
        database_url="sqlite+aiosqlite:///:memory:",
        runs_dir="runs",
    )
    base_url = "https://api.openai.com/v1"
    snapshot = ProviderSnapshot(
        provider_ref="openai",
        display_name="OpenAI",
        protocol=ModelApiProtocol.CHAT_COMPLETIONS,
        base_url=base_url,
        endpoint_fingerprint=endpoint_fingerprint(
            base_url, ModelApiProtocol.CHAT_COMPLETIONS
        ),
        model="test-model",
    )

    with pytest.raises(RuntimeError, match="operation failed"):
        async with review_runtime(
            settings=settings,
            provider_snapshot=snapshot,
            api_key="test-key",
            external_search=False,
        ):
            raise RuntimeError("operation failed")

    assert adapter.closed
