from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QFileDialog, QHBoxLayout, QPushButton, QVBoxLayout, QWidget

from paper_reviewer.application.app_state import GuiPreferences
from paper_reviewer.application.models import RubricValidationResult
from paper_reviewer.application.service import ReviewApplicationService
from paper_reviewer.gui.icons import FluentIconService
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
        self.default_rubric_path = default_rubric_path
        self.current_valid = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(12)
        layout.addWidget(
            PageHeader(
                "Rubric 管理", "校验、预览并设置默认 Rubric；首版不在应用内修改 YAML。"
            )
        )
        self.message = MessageBar(icons)
        layout.addWidget(self.message)
        self.picker = PathPicker(suffix=".yaml", placeholder="选择 Rubric YAML")
        self.picker.browse_requested.connect(self._browse)
        self.picker.path_changed.connect(self._validate)
        layout.addWidget(self.picker)
        self.preview = RubricPreview()
        layout.addWidget(self.preview, 1)
        actions = QHBoxLayout()
        self.default_button = QPushButton("设为默认 Rubric")
        self.default_button.clicked.connect(self._set_default)
        self.folder_button = QPushButton("打开所在目录")
        self.folder_button.setIcon(icons.icon("folder"))
        self.folder_button.clicked.connect(self._open_folder)
        actions.addWidget(self.default_button)
        actions.addWidget(self.folder_button)
        actions.addStretch(1)
        layout.addLayout(actions)
        default = self._default_rubric(preferences)
        self.picker.set_path(default)
        self._validate(str(default))

    def apply_preferences(self) -> None:
        default = self._default_rubric(self.preferences)
        self.picker.set_path(default)

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
        result = self.service.validate_rubric(path, profile_path=self.profile_path)
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
