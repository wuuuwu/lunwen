from __future__ import annotations

import sys
from pathlib import Path

import pytest
from pytest import MonkeyPatch

from paper_reviewer.application import app_state
from paper_reviewer.application.app_state import AppPaths
from paper_reviewer.gui.app import (
    configure_credential_namespace,
    run_batch_output_self_test,
    run_batch_resource_self_test,
    run_system_credential_backend_self_test,
)
from paper_reviewer.gui.resource_paths import bundled_config
from paper_reviewer.reporting.exporter import _FONT_CANDIDATES


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


def test_native_system_credential_backend_is_discoverable() -> None:
    if sys.platform in {"win32", "darwin"}:
        run_system_credential_backend_self_test()
    else:
        with pytest.raises(RuntimeError, match="requires Windows or macOS"):
            run_system_credential_backend_self_test()


def test_pdf_export_has_macos_chinese_font_candidates() -> None:
    assert _FONT_CANDIDATES[:4] == (
        "PingFang SC",
        "Hiragino Sans GB",
        "Songti SC",
        "Heiti SC",
    )


def test_macos_packaging_contract_is_arm64_and_separate_from_windows() -> None:
    project_root = Path(__file__).resolve().parents[2]
    spec = (project_root / "course-paper-reviewer-macos.spec").read_text(
        encoding="utf-8"
    )
    script = (project_root / "scripts/build_course_macos.sh").read_text(
        encoding="utf-8"
    )
    workflow = (
        project_root / ".github/workflows/build-course-macos.yml"
    ).read_text(encoding="utf-8")

    assert 'target_arch="arm64"' in spec
    assert 'name="CoursePaperReviewer.app"' in spec
    assert 'bundle_identifier="com.coursepaperreviewer.app"' in spec
    assert '"keyring.backends.macOS.api"' in spec
    assert "win32ctypes" not in spec
    assert '"$(uname -s)" != "Darwin"' in script
    assert '"$(uname -m)" != "arm64"' in script
    assert "--self-test-system-credential-backend" in script
    assert "/usr/bin/ditto -c -k --sequesterRsrc --keepParent" in script
    assert "CoursePaperReviewer-macos-arm64.zip.sha256" in workflow
    assert 'test "$(uname -m)" = "arm64"' in workflow


def test_windows_spec_remains_windows_specific() -> None:
    project_root = Path(__file__).resolve().parents[2]
    spec = (project_root / "course-paper-reviewer.spec").read_text(encoding="utf-8")

    assert "win32ctypes.pywin32.win32cred" in spec
    assert "BUNDLE(" not in spec
