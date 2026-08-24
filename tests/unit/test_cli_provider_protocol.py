from __future__ import annotations

from pathlib import Path

from paper_reviewer.cli import _cli_resume_requires_desktop
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
