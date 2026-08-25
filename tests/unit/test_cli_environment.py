from __future__ import annotations

import os
from pathlib import Path

import pytest
from dotenv import load_dotenv

from paper_reviewer.cli import _cli_provider_api_key


def test_dotenv_can_load_provider_credentials(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("OPENAI_API_KEY=test-key\n", encoding="utf-8")
    original = os.environ.pop("OPENAI_API_KEY", None)
    try:
        assert load_dotenv(env_file, override=True)
        assert os.environ["OPENAI_API_KEY"] == "test-key"
    finally:
        os.environ.pop("OPENAI_API_KEY", None)
        if original is not None:
            os.environ["OPENAI_API_KEY"] = original


@pytest.mark.parametrize(
    ("provider", "variable"),
    [("openai", "OPENAI_API_KEY"), ("deepseek", "DEEPSEEK_API_KEY")],
)
def test_cli_provider_api_key_uses_builtin_environment_variable(
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
    variable: str,
) -> None:
    monkeypatch.setenv(variable, "test-key")

    assert _cli_provider_api_key(provider) == "test-key"


def test_cli_provider_api_key_rejects_desktop_only_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    with pytest.raises(ValueError, match="unsupported model provider"):
        _cli_provider_api_key("openai_responses")
