from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from paper_reviewer.application.app_state import GuiPreferences
from paper_reviewer.application.models import RubricValidationResult
from paper_reviewer.application.service import ReviewApplicationService
from paper_reviewer.domain.rubric_generation import SavedRubricPackage
from paper_reviewer.gui.icons import FluentIconService
from paper_reviewer.gui.operations import AsyncOperationRegistry
from paper_reviewer.gui.pages.rubric_generator import RubricGeneratorWidget
from paper_reviewer.gui.resource_paths import bundled_config
from paper_reviewer.gui.widgets import MessageBar, PageHeader, PathPicker, RubricPreview


def _optional_bundled_config(*names: str) -> Path | None:
    """Resolve the newest built-in config while retaining v1 fallback."""

    for name in names:
        try:
            path = bundled_config(name)
        except ValueError:
            continue
        if path.is_file():
            return path
    return None


class RubricsPage(QWidget):
    preferences_changed = Signal()

    def __init__(
        self,
        service: ReviewApplicationService,
        preferences: GuiPreferences,
        icons: FluentIconService,
        *,
        operation_registry: AsyncOperationRegistry | None = None,
        profile_path: Path | None = None,
        default_rubric_path: Path | None = None,
    ) -> None:
        super().__init__()
        self.service = service
        self.preferences = preferences
        self.profile_path = profile_path or (
            _optional_bundled_config("zhejiang_undergraduate_specialists_v1.yaml")
            or bundled_config("three_reviewer.yaml")
        )
        self.current_profile_path = self.profile_path
        self.default_rubric_path = default_rubric_path
        self.current_valid = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(12)
        layout.addWidget(
            PageHeader(
                "Rubric 管理",
                "导入已有 Rubric，或通过教师向导和 AI 创建新的课程评价方案。",
            )
        )
        self.tabs = QTabWidget()
        self.tabs.setObjectName("rubricManagementTabs")
        self.tabs.setAccessibleName("Rubric 管理方式")
        manage = QWidget()
        manage_layout = QVBoxLayout(manage)
        manage_layout.setContentsMargins(12, 12, 12, 12)
        manage_layout.setSpacing(12)
        self.message = MessageBar(icons)
        manage_layout.addWidget(self.message)
        packages = QHBoxLayout()
        packages.addWidget(QLabel("已保存评价方案"))
        self.package_combo = QComboBox()
        self.package_combo.setObjectName("savedRubricPackages")
        self.package_combo.setAccessibleName("已保存评价方案")
        self.package_combo.currentIndexChanged.connect(self._package_selected)
        self.refresh_packages_button = QPushButton("刷新")
        self.refresh_packages_button.setObjectName("refreshRubricPackages")
        self.refresh_packages_button.clicked.connect(self.refresh_packages)
        packages.addWidget(self.package_combo, 1)
        packages.addWidget(self.refresh_packages_button)
        manage_layout.addLayout(packages)
        self.picker = PathPicker(suffix=".yaml", placeholder="选择 Rubric YAML")
        self.picker.browse_requested.connect(self._browse)
        self.picker.path_changed.connect(self._validate)
        manage_layout.addWidget(self.picker)
        self.preview = RubricPreview()
        manage_layout.addWidget(self.preview, 1)
        actions = QHBoxLayout()
        self.default_button = QPushButton("设为默认 Rubric")
        self.default_button.clicked.connect(self._set_default)
        self.folder_button = QPushButton("打开所在目录")
        self.folder_button.setIcon(icons.icon("folder"))
        self.folder_button.clicked.connect(self._open_folder)
        actions.addWidget(self.default_button)
        actions.addWidget(self.folder_button)
        actions.addStretch(1)
        manage_layout.addLayout(actions)
        self.tabs.addTab(manage, "评价方案库")
        self.generator = RubricGeneratorWidget(
            service,
            preferences,
            icons,
            operation_registry=operation_registry,
        )
        self.generator.package_saved.connect(self._package_saved)
        self.tabs.addTab(self.generator, "AI 创建")
        layout.addWidget(self.tabs, 1)
        self.refresh_packages()
        default = self._default_rubric(preferences)
        self.picker.set_path(default)
        self._validate(str(default))

    def apply_preferences(self) -> None:
        self.refresh_packages()
        default = self._default_rubric(self.preferences)
        self.picker.set_path(default)
        self.generator.refresh_providers()

    def _default_rubric(self, preferences: GuiPreferences) -> Path:
        if preferences.default_rubric and Path(preferences.default_rubric).is_file():
            return Path(preferences.default_rubric)
        if self.default_rubric_path is not None:
            return self.default_rubric_path
        return (
            _optional_bundled_config("zhejiang_undergraduate_thesis_v2.yaml")
            or bundled_config("unscored_draft.yaml")
        )

    def show_preferences_error(self, message: str) -> None:
        self.message.show_message(f"默认 Rubric 保存失败：{message}", severity="danger")

    def _browse(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "选择 Rubric", "", "YAML (*.yaml *.yml)")
        if path:
            self.picker.set_path(path)

    def _validate(self, text: str) -> None:
        path = Path(text.strip()) if text.strip() else None
        if path is None or not path.is_file():
            self.current_valid = False
            message = "请选择存在的 YAML 文件"
            self.picker.set_invalid(message)
            self.default_button.setEnabled(False)
            self.preview.set_result(
                RubricValidationResult(valid=False, errors=[message])
            )
            self.message.show_message(message, severity="danger")
            return
        resolver = getattr(self.service, "resolve_profile_for_rubric", None)
        self.current_profile_path = (
            resolver(path, fallback_profile_path=self.profile_path)
            if callable(resolver)
            else self.profile_path
        )
        result = self.service.validate_rubric(path, profile_path=self.current_profile_path)
        self.preview.set_result(result)
        self.current_valid = result.valid
        self.default_button.setEnabled(result.valid)
        if result.valid:
            self.picker.set_invalid(None)
            message = "Rubric 结构和 Reviewer 覆盖校验通过。"
            if result.warnings:
                message += " " + " ".join(result.warnings)
            self.message.show_message(message, severity="success")
        else:
            message = "；".join(result.errors)
            self.picker.set_invalid(message)
            self.message.show_message(message, severity="danger")

    def _set_default(self) -> None:
        path = self.picker.path()
        if not self.current_valid or path is None:
            return
        self.preferences.default_rubric = str(path.resolve())
        self.message.show_message("已设为默认 Rubric。", severity="success")
        self.preferences_changed.emit()

    def _open_folder(self) -> None:
        path = self.picker.path()
        if path is not None and path.exists():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path.parent)))

    def refresh_packages(self) -> None:
        list_packages = getattr(self.service, "list_rubric_packages", None)
        try:
            packages = list_packages() if callable(list_packages) else []
        except (OSError, ValueError) as error:
            packages = []
            self.message.show_message(f"评价方案库读取失败：{error}", severity="danger")
        current_path = self.picker.path()
        current = str(current_path.resolve()) if current_path is not None else ""
        self.package_combo.blockSignals(True)
        self.package_combo.clear()
        self.package_combo.addItem("选择已保存评价方案", None)
        selected = 0
        for package in packages:
            created = package.manifest.created_at.astimezone().strftime("%Y-%m-%d %H:%M")
            label = f"{package.manifest.title} · {package.manifest.version} · {created}"
            path = str(package.rubric_path.resolve())
            self.package_combo.addItem(label, path)
            if path == current:
                selected = self.package_combo.count() - 1
        self.package_combo.setCurrentIndex(selected)
        self.package_combo.setEnabled(bool(packages))
        self.package_combo.blockSignals(False)

    def _package_selected(self, index: int) -> None:
        path = self.package_combo.itemData(index)
        if path:
            self.picker.set_path(str(path))

    def _package_saved(self, value: object, set_default: bool) -> None:
        if not isinstance(value, SavedRubricPackage):
            return
        self.picker.set_path(value.rubric_path)
        self.refresh_packages()
        self.tabs.setCurrentIndex(0)
        if set_default:
            self.preferences.default_rubric = str(value.rubric_path.resolve())
            self.preferences_changed.emit()
            self.message.show_message("评价方案已保存并设为默认 Rubric。", severity="success")
        else:
            self.message.show_message("评价方案已保存，可预览或设为默认。", severity="success")
