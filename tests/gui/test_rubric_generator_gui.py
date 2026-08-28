from __future__ import annotations

import asyncio
from pathlib import Path

from PySide6.QtWidgets import QApplication

from paper_reviewer.application.app_state import GuiPreferences
from paper_reviewer.application.models import RubricValidationResult
from paper_reviewer.application.providers import builtin_provider_connections
from paper_reviewer.application.rubric_generator import (
    RubricPackageStore,
    compile_rubric_generation,
    default_rubric_draft,
    resolve_companion_profile,
)
from paper_reviewer.config import load_rubric
from paper_reviewer.domain.rubric_generation import (
    RubricGenerationRequest,
    RubricGenerationResult,
    SavedRubricPackage,
)
from paper_reviewer.gui.icons import FluentIconService
from paper_reviewer.gui.operations import AsyncOperationRegistry
from paper_reviewer.gui.pages.course_batch_new import CourseBatchNewPage
from paper_reviewer.gui.pages.rubric_generator import RubricGeneratorWidget
from paper_reviewer.gui.pages.rubrics import RubricsPage
from paper_reviewer.gui.resource_paths import bundled_config
from paper_reviewer.gui.theme import FluentThemeManager


class _Service:
    def __init__(self, root: Path) -> None:
        self.store = RubricPackageStore(root / "rubric_packages")

    def list_provider_connections(self, *, include_archived: bool = False) -> list[object]:
        del include_archived
        return list(builtin_provider_connections())

    def provider_has_key(self, _provider_ref: str) -> bool:
        return True

    def list_rubric_packages(self) -> list[SavedRubricPackage]:
        return self.store.list()

    def resolve_profile_for_rubric(
        self,
        rubric_path: Path,
        *,
        fallback_profile_path: Path,
    ) -> Path:
        return resolve_companion_profile(rubric_path) or fallback_profile_path

    def validate_rubric(
        self,
        path: Path,
        *,
        profile_path: Path,
    ) -> RubricValidationResult:
        rubric = load_rubric(path)
        return RubricValidationResult(
            valid=profile_path.is_file(),
            rubric=rubric,
            weight_total=sum(item.weight for item in rubric.dimensions),
            profile_compatible=profile_path.is_file(),
        )

    async def generate_rubric(
        self,
        request: RubricGenerationRequest,
        *,
        provider_ref: str,
        model: str,
    ) -> RubricGenerationResult:
        assert provider_ref
        assert model
        return compile_rubric_generation(request, default_rubric_draft(request))

    async def revise_rubric(
        self,
        current: RubricGenerationResult,
        instruction: str,
        *,
        provider_ref: str,
        model: str,
    ) -> RubricGenerationResult:
        assert instruction and provider_ref and model
        return current

    def save_rubric_generation(
        self,
        result: RubricGenerationResult,
        *,
        provider_ref: str,
        model: str,
        parent_package_id: str | None = None,
    ) -> SavedRubricPackage:
        return self.store.save(
            result,
            provider_ref=provider_ref,
            model=model,
            parent_package_id=parent_package_id,
        )


def _widget(
    qapp: QApplication,
    tmp_path: Path,
    registry: AsyncOperationRegistry | None = None,
) -> RubricGeneratorWidget:
    return RubricGeneratorWidget(
        _Service(tmp_path),  # type: ignore[arg-type]
        GuiPreferences(),
        FluentIconService(FluentThemeManager(qapp)),
        operation_registry=registry,
    )


def _complete_teacher_inputs(widget: RubricGeneratorWidget) -> None:
    widget.course_name.setText("数据库原理")
    widget.assignment_requirements.setPlainText("完成数据库设计与分析课程论文。")
    widget.learning_outcomes.setPlainText("能够运用关系模型分析并设计数据库")
    widget.subject_name.setText("计算机科学与技术")
    widget.core_topics.setPlainText("关系模型\n数据库规范化")


def test_teacher_wizard_builds_valid_request_and_weight_feedback(
    qapp: QApplication,
    qtbot: object,
    tmp_path: Path,
) -> None:
    widget = _widget(qapp, tmp_path)
    qtbot.addWidget(widget)  # type: ignore[attr-defined]
    _complete_teacher_inputs(widget)

    assert widget.dimension_total.text() == "权重合计 100%"
    assert len(widget.dimension_rows) == 6
    assert widget._build_request().brief.course_name == "数据库原理"

    widget.dimension_rows[0].weight.setValue(21)
    assert "超出 1%" in widget.dimension_total.text()
    assert not widget._validate_step(2)
    assert "必须合计 100%" in widget.message.message_label.text()


def test_teacher_wizard_generates_previews_and_saves_package(
    qapp: QApplication,
    qtbot: object,
    tmp_path: Path,
) -> None:
    registry = AsyncOperationRegistry()
    widget = _widget(qapp, tmp_path, registry)
    qtbot.addWidget(widget)  # type: ignore[attr-defined]
    _complete_teacher_inputs(widget)
    for expected_step in range(1, 5):
        widget.next_button.click()
        assert widget.stack.currentIndex() == expected_step

    assert widget.generate_button.isEnabled()
    widget.generate_button.click()
    qtbot.waitUntil(lambda: widget._current_result is not None, timeout=5000)  # type: ignore[attr-defined]
    qapp.processEvents()

    assert widget.preview.model.rowCount() > 0
    assert widget.save_button.isEnabled()
    assert "通过结构、权重和 Reviewer 覆盖校验" in widget.message.message_label.text()

    saved: list[tuple[object, bool]] = []
    widget.package_saved.connect(lambda package, default: saved.append((package, default)))
    widget.save_default_button.click()

    assert len(saved) == 1
    package, set_default = saved[0]
    assert isinstance(package, SavedRubricPackage)
    assert package.rubric_path.is_file()
    assert package.profile_path.is_file()
    assert set_default is True
    assert not widget.save_button.isEnabled()


def test_no_subject_mode_removes_subject_reviewer_defaults(
    qapp: QApplication,
    qtbot: object,
    tmp_path: Path,
) -> None:
    widget = _widget(qapp, tmp_path)
    qtbot.addWidget(widget)  # type: ignore[attr-defined]

    widget.subject_mode.setCurrentIndex(0)

    assert not widget.subject_name.isEnabled()
    assert len(widget.dimension_rows) == 5
    assert all(row.role.currentData() != "subject_matter" for row in widget.dimension_rows)
    assert sum(row.weight.value() for row in widget.dimension_rows) == 100


def test_rubric_page_lists_saved_generated_packages(
    qapp: QApplication,
    qtbot: object,
    tmp_path: Path,
) -> None:
    service = _Service(tmp_path)
    generator = RubricGeneratorWidget(
        service,  # type: ignore[arg-type]
        GuiPreferences(),
        FluentIconService(FluentThemeManager(qapp)),
    )
    _complete_teacher_inputs(generator)
    result = compile_rubric_generation(
        generator._build_request(),
        default_rubric_draft(generator._build_request()),
    )
    saved = service.save_rubric_generation(
        result,
        provider_ref="openai",
        model="test-model",
    )
    generator.deleteLater()

    page = RubricsPage(
        service,  # type: ignore[arg-type]
        GuiPreferences(),
        FluentIconService(FluentThemeManager(qapp)),
        profile_path=bundled_config("course_paper_reviewers_v1.yaml"),
        default_rubric_path=bundled_config("course_paper_v1.yaml"),
    )
    qtbot.addWidget(page)  # type: ignore[attr-defined]

    assert page.package_combo.count() == 2
    page.package_combo.setCurrentIndex(1)
    assert page.picker.path() == saved.rubric_path
    assert page.current_profile_path == saved.profile_path
    assert page.current_valid


def test_course_batch_uses_generated_profile_and_dynamic_request_estimate(
    qapp: QApplication,
    qtbot: object,
    tmp_path: Path,
) -> None:
    service = _Service(tmp_path)
    generator = RubricGeneratorWidget(
        service,  # type: ignore[arg-type]
        GuiPreferences(),
        FluentIconService(FluentThemeManager(qapp)),
    )
    _complete_teacher_inputs(generator)
    request = generator._build_request()
    saved = service.save_rubric_generation(
        compile_rubric_generation(request, default_rubric_draft(request)),
        provider_ref="openai",
        model="test-model",
    )
    generator.deleteLater()
    source = tmp_path / "papers"
    source.mkdir()
    (source / "paper.pdf").write_bytes(b"%PDF-1.4\n")

    page = CourseBatchNewPage(
        service,  # type: ignore[arg-type]
        GuiPreferences(default_rubric=str(saved.rubric_path)),
        FluentIconService(FluentThemeManager(qapp)),
    )
    qtbot.addWidget(page)  # type: ignore[attr-defined]
    page.source_picker.set_path(source)

    assert page.profile_path == saved.profile_path
    assert "4 次专项评阅" in page.request_estimate.text()
    assert "最低约 6 次模型请求" in page.request_estimate.text()


def test_rubric_generation_busy_state_can_be_cancelled_and_cleaned_up(
    qapp: QApplication,
    qtbot: object,
    tmp_path: Path,
) -> None:
    registry = AsyncOperationRegistry()
    widget = _widget(qapp, tmp_path, registry)
    qtbot.addWidget(widget)  # type: ignore[attr-defined]

    async def operation(_emit: object) -> None:
        await asyncio.Event().wait()

    widget._start_worker(operation, "正在测试取消…")
    assert widget._worker is not None
    worker = widget._worker
    qtbot.waitUntil(worker.isRunning, timeout=3000)  # type: ignore[attr-defined]
    assert not widget.cancel_button.isHidden()
    assert not widget.stack.isEnabled()

    with qtbot.waitSignal(worker.task_cancelled, timeout=3000):  # type: ignore[attr-defined]
        widget.cancel_button.click()
    assert worker.wait(3000)
    qapp.processEvents()

    assert not widget._busy
    assert registry.workers == []
    assert widget.stack.isEnabled()
