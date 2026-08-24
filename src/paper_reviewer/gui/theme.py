from __future__ import annotations

import json
import re
import sys
from enum import StrEnum
from importlib import resources
from typing import Any

from PySide6.QtCore import QEvent, QObject, QSettings, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QFontDatabase, QGuiApplication, QPalette
from PySide6.QtWidgets import QApplication, QWidget


class ThemeMode(StrEnum):
    SYSTEM = "system"
    LIGHT = "light"
    DARK = "dark"
    HIGH_CONTRAST = "high_contrast"


class TokenRepository:
    def __init__(self) -> None:
        resource = resources.files("paper_reviewer.gui.resources").joinpath(
            "fluent2-qt-tokens.json"
        )
        payload: Any = json.loads(resource.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("Fluent token resource must be an object")
        self.metadata = dict(payload.get("metadata", {}))
        self.themes = {
            "light": dict(payload.get("light", {})),
            "dark": dict(payload.get("dark", {})),
        }
        self.metrics = dict(payload.get("metrics", {}))
        if not self.themes["light"] or not self.themes["dark"]:
            raise ValueError("Fluent token resource is missing light/dark themes")

    def resolve(self, mode: str) -> dict[str, str]:
        if mode not in self.themes:
            raise ValueError(f"unknown resolved theme: {mode}")
        return {**self.themes[mode], **self.metrics}


class FluentThemeManager(QObject):
    theme_changed = Signal(str)

    def __init__(self, app: QApplication, repository: TokenRepository | None = None) -> None:
        super().__init__(app)
        self.app = app
        self.repository = repository or TokenRepository()
        self.mode = ThemeMode.SYSTEM
        self.resolved_mode = "light"
        self.tokens = self.repository.resolve("light")
        self._system_palette = QPalette(app.palette())
        self._applying = False
        app.installEventFilter(self)
        self._style_hints = QGuiApplication.styleHints()
        color_signal = getattr(self._style_hints, "colorSchemeChanged", None)
        if color_signal is not None:
            color_signal.connect(self._system_theme_changed)
        accessibility_getter = getattr(self._style_hints, "accessibility", None)
        accessibility_hints: QObject | None = None
        if callable(accessibility_getter):
            accessibility_hints = accessibility_getter()
        contrast_signal = getattr(
            accessibility_hints or self._style_hints,
            "contrastPreferenceChanged",
            None,
        )
        if contrast_signal is not None:
            contrast_signal.connect(self._system_contrast_changed)

    def set_mode(self, mode: ThemeMode | str) -> None:
        try:
            self.mode = ThemeMode(mode)
        except ValueError:
            self.mode = ThemeMode.SYSTEM
        self.apply()

    def apply(self) -> None:
        resolved = self._resolved_mode()
        self.resolved_mode = resolved
        self.tokens = self.repository.resolve(resolved)
        if self.mode is ThemeMode.HIGH_CONTRAST or self._system_prefers_high_contrast():
            self.tokens = self._high_contrast_tokens(self.tokens)
        self._applying = True
        try:
            self.app.setFont(self._application_font())
            self.app.setPalette(self._build_palette(self.tokens))
            self.app.setStyleSheet(self._render_qss(self.tokens))
        finally:
            self._applying = False
        self.theme_changed.emit(self.mode.value)

    def color(self, alias: str) -> QColor:
        value = self.tokens.get(alias)
        if value is None:
            raise KeyError(alias)
        return QColor(value)

    def _resolved_mode(self) -> str:
        if self.mode is ThemeMode.LIGHT:
            return "light"
        if self.mode is ThemeMode.DARK:
            return "dark"
        scheme = QGuiApplication.styleHints().colorScheme()
        return "dark" if scheme is Qt.ColorScheme.Dark else "light"

    def _system_theme_changed(self, _scheme: Qt.ColorScheme) -> None:
        if self.mode in {ThemeMode.SYSTEM, ThemeMode.HIGH_CONTRAST}:
            QTimer.singleShot(0, self.apply)

    def _system_contrast_changed(self, _preference: object) -> None:
        self._system_palette = QPalette(self.app.style().standardPalette())
        if self.mode in {ThemeMode.SYSTEM, ThemeMode.HIGH_CONTRAST}:
            QTimer.singleShot(0, self.apply)

    def _system_prefers_high_contrast(self) -> bool:
        if self.mode is not ThemeMode.SYSTEM:
            return False
        contrast_getter = getattr(self._style_hints, "contrastPreference", None)
        if not callable(contrast_getter):
            accessibility_getter = getattr(self._style_hints, "accessibility", None)
            try:
                accessibility_hints = (
                    accessibility_getter() if callable(accessibility_getter) else None
                )
                contrast_getter = getattr(
                    accessibility_hints,
                    "contrastPreference",
                    None,
                )
            except RuntimeError:
                contrast_getter = None
        if callable(contrast_getter):
            preference = contrast_getter()
            return getattr(preference, "name", "") == "HighContrast"
        if sys.platform == "win32":
            settings = QSettings(
                r"HKEY_CURRENT_USER\Control Panel\Accessibility\HighContrast",
                QSettings.Format.NativeFormat,
            )
            try:
                return bool(int(str(settings.value("Flags", "0"))) & 1)
            except ValueError:
                return False
        return False

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if (
            watched is self.app
            and event.type() is QEvent.Type.ApplicationPaletteChange
            and not self._applying
        ):
            self._system_palette = QPalette(self.app.palette())
            if self.mode in {ThemeMode.SYSTEM, ThemeMode.HIGH_CONTRAST}:
                QTimer.singleShot(0, self.apply)
        return super().eventFilter(watched, event)

    def _application_font(self) -> QFont:
        font = QFontDatabase.systemFont(QFontDatabase.SystemFont.GeneralFont)
        families = set(QFontDatabase.families())
        preferred: list[str] = []
        if "Segoe UI Variable" in families:
            preferred.append("Segoe UI Variable")
        elif "Segoe UI" in families:
            preferred.append("Segoe UI")
        for family in ("Microsoft YaHei UI", "Microsoft YaHei"):
            if family in families:
                preferred.append(family)
        if preferred:
            font.setFamilies(preferred)
        return font

    def _build_palette(self, tokens: dict[str, str]) -> QPalette:
        palette = QPalette()
        roles = {
            QPalette.ColorRole.Window: "window_background",
            QPalette.ColorRole.WindowText: "text_primary",
            QPalette.ColorRole.Base: "surface_background",
            QPalette.ColorRole.AlternateBase: "surface_secondary",
            QPalette.ColorRole.Text: "text_primary",
            QPalette.ColorRole.PlaceholderText: "text_tertiary",
            QPalette.ColorRole.Button: "surface_background",
            QPalette.ColorRole.ButtonText: "text_primary",
            QPalette.ColorRole.Highlight: "brand_background",
            QPalette.ColorRole.HighlightedText: "text_on_brand",
            QPalette.ColorRole.Link: "brand_foreground",
            QPalette.ColorRole.ToolTipBase: "tooltip_background",
            QPalette.ColorRole.ToolTipText: "tooltip_foreground",
        }
        for role, alias in roles.items():
            palette.setColor(QPalette.ColorGroup.All, role, QColor(tokens[alias]))
        palette.setColor(
            QPalette.ColorGroup.Disabled,
            QPalette.ColorRole.Text,
            QColor(tokens["text_disabled"]),
        )
        palette.setColor(
            QPalette.ColorGroup.Disabled,
            QPalette.ColorRole.ButtonText,
            QColor(tokens["text_disabled"]),
        )
        return palette

    def _high_contrast_tokens(self, tokens: dict[str, str]) -> dict[str, str]:
        resolved = dict(tokens)
        palette = self._system_palette
        mappings = {
            "window_background": QPalette.ColorRole.Window,
            "canvas_background": QPalette.ColorRole.Base,
            "surface_background": QPalette.ColorRole.Button,
            "surface_secondary": QPalette.ColorRole.Window,
            "text_primary": QPalette.ColorRole.WindowText,
            "text_secondary": QPalette.ColorRole.Text,
            "text_tertiary": QPalette.ColorRole.PlaceholderText,
            "focus_border": QPalette.ColorRole.Highlight,
            "brand_background": QPalette.ColorRole.Highlight,
            "brand_background_hover": QPalette.ColorRole.Highlight,
            "brand_background_pressed": QPalette.ColorRole.Highlight,
            "text_on_brand": QPalette.ColorRole.HighlightedText,
            "selection_subtle_background": QPalette.ColorRole.Highlight,
            "selection_subtle_foreground": QPalette.ColorRole.HighlightedText,
        }
        for alias, role in mappings.items():
            resolved[alias] = palette.color(role).name()
        return resolved

    def _render_qss(self, tokens: dict[str, str]) -> str:
        template = resources.files("paper_reviewer.gui.resources").joinpath(
            "fluent.qss.in"
        ).read_text(encoding="utf-8")

        def replace(match: re.Match[str]) -> str:
            alias = match.group(1)
            if alias not in tokens:
                raise KeyError(f"unresolved QSS token: {alias}")
            return tokens[alias]

        rendered = re.sub(r"@\{([a-zA-Z0-9_]+)\}", replace, template)
        if "@{" in rendered:
            raise ValueError("QSS contains unresolved token placeholders")
        return rendered


def set_fluent_property(widget: QWidget, name: str, value: object) -> None:
    if widget.property(name) == value:
        return
    widget.setProperty(name, value)
    style = widget.style()
    style.unpolish(widget)
    style.polish(widget)
    widget.update()
