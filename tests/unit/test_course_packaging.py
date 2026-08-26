from __future__ import annotations

from pathlib import Path

from pytest import MonkeyPatch

from paper_reviewer.application import app_state
from paper_reviewer.application.app_state import AppPaths
from paper_reviewer.gui.app import (
    configure_credential_namespace,
    run_batch_output_self_test,
    run_batch_resource_self_test,
)
from paper_reviewer.gui.resource_paths import bundled_config


def test_course_app_paths_are_isolated_and_include_batches(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    def fake_user_data_path(
        app_name: str,
        app_author: str,
        *,
        roaming: bool,
    ) -> Path:
        assert (app_name, app_author, roaming) == (
            "CoursePaperReviewer",
            "CoursePaperReviewer",
            False,
        )
        return tmp_path / app_author / app_name

    monkeypatch.setattr(app_state, "user_data_path", fake_user_data_path)

    paths = AppPaths.for_current_user()

    assert paths.root == tmp_path / "CoursePaperReviewer" / "CoursePaperReviewer"
    assert paths.batches_dir == paths.root / "batches"
    paths.ensure()
    assert paths.batches_dir.is_dir()


def test_legacy_explicit_app_paths_derive_batches_directory(tmp_path: Path) -> None:
    paths = AppPaths(
        root=tmp_path,
        data_dir=tmp_path / "data",
        runs_dir=tmp_path / "runs",
        logs_dir=tmp_path / "logs",
        config_dir=tmp_path / "config",
    )

    assert paths.batches_dir == tmp_path / "batches"


def test_course_configs_resolve_from_source_tree() -> None:
    project_root = Path(__file__).resolve().parents[2]

    assert bundled_config("course_paper_v1.yaml").samefile(
        project_root / "configs/rubrics/course_paper_v1.yaml"
    )
    assert bundled_config("course_paper_reviewers_v1.yaml").samefile(
        project_root / "configs/review_profiles/course_paper_reviewers_v1.yaml"
    )


def test_course_credential_namespace_is_independent(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "thesis-environment-key")

    credentials = configure_credential_namespace()

    assert credentials.service_name == "CoursePaperReviewer"
    assert credentials.environments == {}


def test_course_batch_packaging_self_tests() -> None:
    run_batch_resource_self_test()
    run_batch_output_self_test()
