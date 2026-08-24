from __future__ import annotations

import sys

from PySide6.QtGui import QStandardItem, QStandardItemModel
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListView,
    QMainWindow,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from paper_reviewer.gui.icons import FluentIconService
from paper_reviewer.gui.theme import FluentThemeManager, set_fluent_property
from paper_reviewer.gui.widgets import MessageBar, PageHeader


class WidgetGallery(QMainWindow):
    def __init__(self, theme: FluentThemeManager) -> None:
        super().__init__()
        self.theme = theme
        self.icons = FluentIconService(theme)
        self.setWindowTitle("Paper Reviewer · Fluent Widget Gallery")
        self.resize(900, 680)
        content = QWidget()
        content.setObjectName("pageCanvas")
        layout = QVBoxLayout(content)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(16)
        layout.addWidget(
            PageHeader("Fluent Widget Gallery", "用于强制检查主题、语义变体和交互状态。")
        )
        theme_row = QHBoxLayout()
        theme_selector = QComboBox()
        for text, value in (
            ("跟随系统", "system"),
            ("浅色", "light"),
            ("深色", "dark"),
            ("高对比度", "high_contrast"),
        ):
            theme_selector.addItem(text, value)
        theme_selector.currentIndexChanged.connect(
            lambda: theme.set_mode(str(theme_selector.currentData()))
        )
        theme_row.addWidget(QLabel("主题"))
        theme_row.addWidget(theme_selector)
        theme_row.addStretch(1)
        layout.addLayout(theme_row)

        buttons = QFrame()
        buttons.setProperty("fluentRole", "card")
        buttons_layout = QHBoxLayout(buttons)
        for text, appearance in (
            ("主要操作", "primary"),
            ("次要操作", "secondary"),
            ("低强调", "subtle"),
            ("危险操作", "danger"),
        ):
            button = QPushButton(text)
            set_fluent_property(button, "fluentAppearance", appearance)
            buttons_layout.addWidget(button)
        disabled = QPushButton("已禁用")
        disabled.setDisabled(True)
        buttons_layout.addWidget(disabled)
        layout.addWidget(buttons)

        inputs = QFrame()
        inputs.setProperty("fluentRole", "card")
        inputs_layout = QVBoxLayout(inputs)
        normal = QLineEdit()
        normal.setPlaceholderText("普通输入")
        invalid = QLineEdit("无效内容")
        invalid.setAccessibleDescription("示例校验错误")
        set_fluent_property(invalid, "fluentInvalid", True)
        read_only = QLineEdit("只读内容")
        read_only.setReadOnly(True)
        output = QPlainTextEdit("只读多行输出用于检查现代圆角、内边距和焦点边界。")
        output.setReadOnly(True)
        output.setMinimumHeight(output.fontMetrics().lineSpacing() * 5)
        busy = QPushButton("处理中")
        set_fluent_property(busy, "fluentBusy", True)
        busy.setDisabled(True)
        inputs_layout.addWidget(normal)
        inputs_layout.addWidget(invalid)
        inputs_layout.addWidget(read_only)
        inputs_layout.addWidget(output)
        inputs_layout.addWidget(busy)
        layout.addWidget(inputs)

        data_views = QFrame()
        data_views.setProperty("fluentRole", "card")
        data_layout = QHBoxLayout(data_views)
        data_layout.setContentsMargins(16, 16, 16, 16)
        data_layout.setSpacing(16)
        navigation = QListView()
        navigation.setObjectName("primaryNavigation")
        navigation.setAccessibleName("Gallery 主导航示例")
        navigation_model = QStandardItemModel(navigation)
        for label in ("新建评测", "任务记录", "Rubric 管理", "设置"):
            navigation_model.appendRow(QStandardItem(label))
        navigation.setModel(navigation_model)
        navigation.setCurrentIndex(navigation_model.index(1, 0))
        table = QTableView()
        table.setAccessibleName("Gallery 表格示例")
        table_model = QStandardItemModel(table)
        table_model.setHorizontalHeaderLabels(["论文", "状态", "Rubric"])
        for values in (
            ("本科论文示例.pdf", "已完成", "0.1.0-experimental"),
            ("论文二.pdf", "待人工复核", "0.1.0-experimental"),
        ):
            table_model.appendRow([QStandardItem(value) for value in values])
        table.setModel(table_model)
        table.horizontalHeader().setStretchLastSection(True)
        data_layout.addWidget(navigation, 1)
        data_layout.addWidget(table, 3)
        layout.addWidget(data_views)

        for severity, message in (
            ("info", "信息状态包含图标和文字。"),
            ("success", "操作已成功完成。"),
            ("warning", "此配置需要注意。"),
            ("danger", "发生阻塞错误。"),
        ):
            bar = MessageBar(self.icons)
            bar.show_message(message, severity=severity)
            layout.addWidget(bar)
        layout.addStretch(1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(content)
        self.setCentralWidget(scroll)


def main() -> int:
    app = QApplication(sys.argv)
    theme = FluentThemeManager(app)
    theme.apply()
    window = WidgetGallery(theme)
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
