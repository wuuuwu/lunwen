from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from pydantic import BaseModel

from paper_reviewer.application.artifacts import RunArtifactStore


class _ArtifactModel(BaseModel):
    label: str
    count: int


def test_run_artifact_store_round_trips_models_lists_and_unicode(tmp_path: Path) -> None:
    store = RunArtifactStore(tmp_path / "run-1")
    model = _ArtifactModel(label="中文", count=2)

    store.write_model("model.json", model)
    store.write_model_list("models.json", [model])
    store.write_json("payload.json", {"说明": "保留 UTF-8"})

    assert store.load_model("model.json", _ArtifactModel) == model
    assert store.load_model_list("models.json", _ArtifactModel) == [model]
    assert store.read_json("payload.json") == {"说明": "保留 UTF-8"}
    assert "中文" in store.path("model.json").read_text(encoding="utf-8")
    assert not list(store.run_dir.glob("*.tmp"))


def test_run_artifact_store_preserves_missing_and_invalid_list_contract(tmp_path: Path) -> None:
    store = RunArtifactStore(tmp_path)

    assert store.load_optional_model("missing.json", _ArtifactModel) is None
    assert store.load_model_list("missing.json", _ArtifactModel) == []

    store.path("invalid.json").write_text(json.dumps({"not": "a list"}), encoding="utf-8")
    with pytest.raises(ValueError, match="旧快照格式无效"):
        store.load_model_list(
            "invalid.json",
            _ArtifactModel,
            invalid_message="旧快照格式无效。",
        )


def test_run_artifact_store_failed_replace_preserves_destination_and_cleans_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = RunArtifactStore(tmp_path)
    destination = store.path("payload.json")
    destination.write_text('{"old": true}', encoding="utf-8")

    def fail_replace(source: Path, target: Path) -> None:
        raise OSError("simulated replace failure")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(OSError, match="simulated replace failure"):
        store.write_json("payload.json", {"new": True})

    assert destination.read_text(encoding="utf-8") == '{"old": true}'
    assert not store.path("payload.json.tmp").exists()


@pytest.mark.parametrize("name", ["", ".", "..", "nested/value.json", "../value.json"])
def test_run_artifact_store_rejects_paths_outside_run_root(tmp_path: Path, name: str) -> None:
    with pytest.raises(ValueError, match="invalid run artifact name"):
        RunArtifactStore(tmp_path).path(name)
