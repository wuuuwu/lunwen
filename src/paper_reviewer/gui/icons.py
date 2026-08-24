from __future__ import annotations

from importlib import resources

from PySide6.QtCore import QByteArray, QRect, QRectF, QSize, Qt
from PySide6.QtGui import QIcon, QIconEngine, QPainter, QPixmap, QPixmapCache
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QApplication

from paper_reviewer.gui.theme import FluentThemeManager


class FluentIconEngine(QIconEngine):
    """Render a Fluent SVG against the theme that is active at paint time."""

    def __init__(
        self,
        theme: FluentThemeManager,
        svg: str,
        color_role: str,
    ) -> None:
        super().__init__()
        self.theme = theme
        self.svg = svg
        self.color_role = color_role

    def clone(self) -> QIconEngine:
        return FluentIconEngine(self.theme, self.svg, self.color_role)

    def key(self) -> str:
        return "PaperReviewer.FluentIconEngine"

    def paint(
        self,
        painter: QPainter,
        rect: QRect,
        mode: QIcon.Mode,
        state: QIcon.State,
    ) -> None:
        del state
        renderer = self._renderer(mode)
        renderer.render(painter, QRectF(rect))

    def pixmap(
        self,
        size: QSize,
        mode: QIcon.Mode,
        state: QIcon.State,
    ) -> QPixmap:
        del state
        pixmap = QPixmap(size)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        self._renderer(mode).render(painter, QRectF(0, 0, size.width(), size.height()))
        painter.end()
        return pixmap

    def _renderer(self, mode: QIcon.Mode) -> QSvgRenderer:
        color_role = "text_disabled" if mode is QIcon.Mode.Disabled else self.color_role
        color = self.theme.color(color_role).name()
        payload = self.svg.replace("currentColor", color)
        renderer = QSvgRenderer(QByteArray(payload.encode("utf-8")))
        if not renderer.isValid():
            raise ValueError("invalid Fluent SVG icon resource")
        return renderer


class FluentIconService:
    def __init__(self, theme: FluentThemeManager) -> None:
        self.theme = theme
        self._cache: dict[tuple[str, int, str], QIcon] = {}
        theme.theme_changed.connect(self._theme_changed)

    def icon(self, name: str, *, size: int = 20, color_role: str = "text_secondary") -> QIcon:
        key = (name, size, color_role)
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        resource = resources.files("paper_reviewer.gui.resources.icons").joinpath(f"{name}.svg")
        svg = resource.read_text(encoding="utf-8")
        if not QSvgRenderer(QByteArray(svg.encode("utf-8"))).isValid():
            raise ValueError(f"invalid SVG icon resource: {name}")
        icon = QIcon(FluentIconEngine(self.theme, svg, color_role))
        self._cache[key] = icon
        return icon

    def _theme_changed(self, _mode: str) -> None:
        QPixmapCache.clear()
        app = QApplication.instance()
        if isinstance(app, QApplication):
            for widget in app.allWidgets():
                widget.update()
