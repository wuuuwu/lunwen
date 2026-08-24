from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv


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
