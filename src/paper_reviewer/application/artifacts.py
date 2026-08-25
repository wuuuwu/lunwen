from __future__ import annotations

import json
import os
from collections.abc import Sequence
from pathlib import Path

from pydantic import BaseModel


class RunArtifactStore:
    """Typed, atomic file access limited to one run directory.

    This store deliberately does not cover preferences, provider profiles, or
    credentials: those stores have different validation and recovery rules.
    """

    def __init__(self, run_dir: Path) -> None:
        self.run_dir = run_dir

    def path(self, name: str) -> Path:
        candidate = Path(name)
        if candidate.name != name or name in {"", ".", ".."}:
            raise ValueError(f"invalid run artifact name: {name}")
        return self.run_dir / name

    def exists(self, name: str) -> bool:
        return self.path(name).is_file()

    def read_json(self, name: str) -> object:
        return json.loads(self.path(name).read_text(encoding="utf-8"))

    def load_model[ModelT: BaseModel](self, name: str, model_type: type[ModelT]) -> ModelT:
        return model_type.model_validate_json(self.path(name).read_text(encoding="utf-8"))

    def load_optional_model[ModelT: BaseModel](
        self, name: str, model_type: type[ModelT]
    ) -> ModelT | None:
        if not self.exists(name):
            return None
        return self.load_model(name, model_type)

    def load_model_list[ModelT: BaseModel](
        self,
        name: str,
        model_type: type[ModelT],
        *,
        invalid_message: str | None = None,
    ) -> list[ModelT]:
        if not self.exists(name):
            return []
        payload = self.read_json(name)
        if not isinstance(payload, list):
            raise ValueError(invalid_message or f"invalid run artifact: {name}")
        return [model_type.model_validate(item) for item in payload]

    def write_json(self, name: str, payload: object) -> None:
        self._write_text_atomic(
            name,
            json.dumps(payload, ensure_ascii=False, indent=2),
        )

    def write_model(self, name: str, model: BaseModel) -> None:
        self._write_text_atomic(name, model.model_dump_json(indent=2))

    def write_model_list(self, name: str, items: Sequence[BaseModel]) -> None:
        payload = [item.model_dump(mode="json") for item in items]
        self.write_json(name, payload)

    def _write_text_atomic(self, name: str, content: str) -> None:
        destination = self.path(name)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(destination.suffix + ".tmp")
        try:
            # Match Path.write_text's platform newline behavior used by legacy
            # artifacts while adding durability and atomic replacement.
            with temporary.open("w", encoding="utf-8") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
