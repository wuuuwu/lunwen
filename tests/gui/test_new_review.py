from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import QApplication

from paper_reviewer.application.app_state import GuiPreferences
from paper_reviewer.application.models import RubricValidationResult
from paper_reviewer.domain.rubric import RubricProfile
from paper_reviewer.gui.icons import FluentIconService
from paper_reviewer.gui.pages.new_review import NewReviewPage
from paper_reviewer.gui.resource_paths import bundled_config
from paper_reviewer.gui.theme import FluentThemeManager


class MutableCredentials:
    def __init__(self) -> None:
        self.available = False

    def has(self, _provider: str) -> bool:
        return self.available


class StubReviewService:
    def __init__(self) -> None:
        self.credentials = MutableCredentials()
        self.valid = True
        self.rubric = RubricProfile.model_validate(
            {
                "rubric_id": "undergraduate",
                "version": "1",
                "title": "本科论文评阅",
                "applicable_levels": ["undergraduate"],
                "scoring_enabled": False,
            }
        )

    def validate_rubric(self, _path: Path, *, profile_path: Path) -> RubricValidationResult:
        del profile_path
        return RubricValidationResult(
            valid=self.valid,
            rubric=self.rubric if self.valid else None,
            errors=[] if self.valid else ["Rubric 已被外部修改为无效内容"],
            profile_compatible=self.valid,
        )


def test_development_defaults_use_top_level_canonical_configs() -> None:
    project_root = Path(__file__).resolve().parents[2]

    assert bundled_config("unscored_draft.yaml").samefile(
        project_root / "configs/rubrics/unscored_draft.yaml"
    )
    assert bundled_config("three_reviewer.yaml").samefile(
        project_root / "configs/review_profiles/three_reviewer.yaml"
    )


def test_credentials_refresh_start_state_and_start_revalidates_rubric(
    qapp: QApplication,
    qtbot: object,
    tmp_path: Path,
) -> None:
    service = StubReviewService()
    page = NewReviewPage(
        service,  # type: ignore[arg-type]
        GuiPreferences(),
        FluentIconService(FluentThemeManager(qapp)),
    )
    qtbot.addWidget(page)  # type: ignore[attr-defined]
    paper = tmp_path / "paper.pdf"
    paper.write_bytes(b"%PDF-1.4\n")
    page.discipline_name.setText("计算机科学与技术")
    page.cloud_processing_authorized.setChecked(True)
    page.non_classified_confirmation.setChecked(True)
    page.paper_picker.set_path(paper)

    assert not page.start_button.isEnabled()
    service.credentials.available = True
    page.refresh_credentials()
    assert page.start_button.isEnabled()

    started: list[object] = []
    page.start_requested.connect(started.append)
    service.valid = False
    page._start()

    assert started == []
    assert not page.start_button.isEnabled()
    assert "外部修改" in page.message.message_label.text()


def test_integrity_report_is_reserved_without_file_dialog(
    qapp: QApplication,
    qtbot: object,
) -> None:
    page = NewReviewPage(
        StubReviewService(),  # type: ignore[arg-type]
        GuiPreferences(),
        FluentIconService(FluentThemeManager(qapp)),
    )
    qtbot.addWidget(page)  # type: ignore[attr-defined]

    assert "后续版本" in page.integrity_report_button.accessibleName()
    assert "后续版本" in page.integrity_report_button.toolTip()
    assert not hasattr(page, "integrity_report_path")

    page.integrity_report_button.click()

    assert "功能已预留" in page.message.message_label.text()


def test_review_request_contains_policy_context_and_no_report_path(
    qapp: QApplication,
    qtbot: object,
    tmp_path: Path,
) -> None:
    service = StubReviewService()
    service.credentials.available = True
    page = NewReviewPage(
        service,  # type: ignore[arg-type]
        GuiPreferences(),
        FluentIconService(FluentThemeManager(qapp)),
    )
    qtbot.addWidget(page)  # type: ignore[attr-defined]
    paper = tmp_path / "paper.pdf"
    paper.write_bytes(b"%PDF-1.4\n")
    page.discipline_name.setText("计算机科学与技术")
    page.paper_picker.set_path(paper)
    page.cloud_processing_authorized.setChecked(True)
    page.non_classified_confirmation.setChecked(True)

    requests: list[object] = []
    page.start_requested.connect(requests.append)
    page._start()

    assert len(requests) == 1
    request = requests[0]
    assert request.discipline_name == "计算机科学与技术"
    assert request.discipline_profile is None
    assert request.cloud_processing_authorized is True
    assert request.contains_classified_material is False
    assert not hasattr(request, "integrity_report")
