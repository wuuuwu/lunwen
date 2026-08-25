from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from paper_reviewer import cli
from paper_reviewer.cli import (
    _cli_builtin_provider_snapshot,
    _cli_resume_requires_desktop,
    _validate_cli_builtin_snapshot,
)
from paper_reviewer.domain.provider import (
    ModelApiProtocol,
    ProviderSnapshot,
    endpoint_fingerprint,
)
from paper_reviewer.domain.run import RunRecord


def _run(provider: str) -> RunRecord:
    return RunRecord(
        run_id="run-provider",
        input_path="paper.pdf",
        input_hash="input",
        config_hash="config",
        rubric_id="rubric",
        provider=provider,
        model="model",
    )


def test_cli_resume_keeps_legacy_chat_tasks_supported(tmp_path: Path) -> None:
    assert not _cli_resume_requires_desktop(_run("openai"), tmp_path)
    assert not _cli_resume_requires_desktop(_run("deepseek"), tmp_path)


def test_cli_resume_directs_responses_and_custom_tasks_to_desktop(tmp_path: Path) -> None:
    assert _cli_resume_requires_desktop(_run("openai_responses"), tmp_path)
    assert _cli_resume_requires_desktop(_run("custom:" + "a" * 32), tmp_path)

    base_url = "https://api.openai.com/v1"
    snapshot = ProviderSnapshot(
        provider_ref="openai_responses",
        display_name="OpenAI",
        protocol=ModelApiProtocol.RESPONSES,
        base_url=base_url,
        endpoint_fingerprint=endpoint_fingerprint(base_url, ModelApiProtocol.RESPONSES),
        model="model",
    )
    (tmp_path / "provider.json").write_text(snapshot.model_dump_json(), encoding="utf-8")
    assert _cli_resume_requires_desktop(_run("openai"), tmp_path)


@pytest.mark.parametrize("provider", ["openai", "deepseek"])
def test_cli_runtime_snapshot_is_chat_only(provider: str) -> None:
    snapshot = _cli_builtin_provider_snapshot(provider, "test-model")

    assert snapshot.provider_ref == provider
    assert snapshot.protocol is ModelApiProtocol.CHAT_COMPLETIONS
    assert snapshot.model == "test-model"


def test_cli_runtime_snapshot_rejects_responses_provider() -> None:
    with pytest.raises(ValueError, match="unsupported model provider"):
        _cli_builtin_provider_snapshot("openai_responses", "test-model")


def test_cli_rejects_rehashed_malicious_builtin_snapshot() -> None:
    malicious_url = "https://attacker.example/v1"
    snapshot = ProviderSnapshot(
        provider_ref="openai",
        display_name="OpenAI",
        protocol=ModelApiProtocol.CHAT_COMPLETIONS,
        base_url=malicious_url,
        endpoint_fingerprint=endpoint_fingerprint(
            malicious_url, ModelApiProtocol.CHAT_COMPLETIONS
        ),
        model="model",
    )

    with pytest.raises(ValueError, match="固定端点不一致"):
        _validate_cli_builtin_snapshot("openai", snapshot)


@pytest.mark.asyncio
@pytest.mark.parametrize("tampering", ["endpoint", "model"])
async def test_cli_rejects_tampered_snapshot_before_key_or_adapter(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    tampering: str,
) -> None:
    malicious_url = (
        "https://attacker.example/v1"
        if tampering == "endpoint"
        else "https://api.openai.com/v1"
    )
    malicious = ProviderSnapshot(
        provider_ref="openai",
        display_name="OpenAI",
        protocol=ModelApiProtocol.CHAT_COMPLETIONS,
        base_url=malicious_url,
        endpoint_fingerprint=endpoint_fingerprint(
            malicious_url, ModelApiProtocol.CHAT_COMPLETIONS
        ),
        model="different-model" if tampering == "model" else "model",
    )
    run = _run("openai")
    engine = SimpleNamespace()
    key_requested = False
    runtime_entered = False

    async def initialize(_engine: object) -> None:
        return None

    async def _dispose() -> None:
        return None

    engine.dispose = _dispose

    class Repository:
        async def get(self, _run_id: str) -> RunRecord:
            return run

    def fail_key(_provider: str) -> str:
        nonlocal key_requested
        key_requested = True
        raise AssertionError("API Key must not be requested")

    @asynccontextmanager
    async def fail_runtime(**_kwargs: object) -> AsyncIterator[object]:
        nonlocal runtime_entered
        runtime_entered = True
        raise AssertionError("runtime must not be entered")
        yield object()

    monkeypatch.setattr(
        cli,
        "Settings",
        lambda: SimpleNamespace(database_url="sqlite+aiosqlite://", runs_dir=tmp_path),
    )
    monkeypatch.setattr(cli, "create_engine", lambda _url: engine)
    monkeypatch.setattr(cli, "initialize_database", initialize)
    monkeypatch.setattr(cli, "create_session_factory", lambda _engine: object())
    monkeypatch.setattr(cli, "RunRepository", lambda _sessions: Repository())
    monkeypatch.setattr(cli, "load_provider_snapshot", lambda _run_dir: malicious)
    monkeypatch.setattr(cli, "load_run_snapshots", lambda _run_dir: (object(), object()))
    monkeypatch.setattr(cli, "load_run_request_context", lambda _run_dir: {})
    monkeypatch.setattr(cli, "_cli_provider_api_key", fail_key)
    monkeypatch.setattr(cli, "review_runtime", fail_runtime)

    expected = "Provider 或模型不一致" if tampering == "model" else "固定端点不一致"
    with pytest.raises(ValueError, match=expected):
        await cli._resume(run.run_id)

    assert not key_requested
    assert not runtime_entered
