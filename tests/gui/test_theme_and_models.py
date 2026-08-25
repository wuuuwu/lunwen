from __future__ import annotations

from datetime import UTC, datetime

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication

from paper_reviewer.application.models import RunSummary
from paper_reviewer.domain.run import RunStatus
from paper_reviewer.gui.icons import FluentIconService
from paper_reviewer.gui.models import RunsFilterProxyModel, RunsTableModel
from paper_reviewer.gui.theme import FluentThemeManager, ThemeMode, TokenRepository


def test_token_repository_has_traceable_source_and_both_themes() -> None:
    repository = TokenRepository()

    assert repository.metadata["package_version"] == "9.2.1"
    assert "Qt adaptation" in repository.metadata["metric_source"]
    assert repository.resolve("light")["brand_background"]
    assert repository.resolve("dark")["brand_background"]


def test_fluent_radius_metrics_use_moderate_modern_scale() -> None:
    repository = TokenRepository()

    for mode in ("light", "dark"):
        tokens = repository.resolve(mode)
        assert tokens["radius_small"] == "6px"
        assert tokens["radius_medium"] == "8px"
        assert tokens["radius_large"] == "12px"


def test_theme_manager_renders_global_qss_without_placeholders(qapp: QApplication) -> None:
    manager = FluentThemeManager(qapp)

    manager.set_mode(ThemeMode.DARK)

    assert manager.resolved_mode == "dark"
    assert "@{" not in qapp.styleSheet()
    assert manager.color("text_primary").isValid()


def test_existing_icon_recolors_when_theme_changes(qapp: QApplication) -> None:
    manager = FluentThemeManager(qapp)
    manager.set_mode(ThemeMode.LIGHT)
    icon = FluentIconService(manager).icon("folder")
    light = icon.pixmap(20, 20).toImage()

    manager.set_mode(ThemeMode.DARK)
    dark = icon.pixmap(20, 20).toImage()

    assert light != dark


def test_unknown_theme_falls_back_and_high_contrast_uses_system_palette(
    qapp: QApplication,
) -> None:
    manager = FluentThemeManager(qapp)
    system_palette = QPalette(qapp.palette())
    system_palette.setColor(QPalette.ColorRole.Window, QColor("#123456"))
    manager._system_palette = system_palette

    manager.set_mode("future-theme")
    assert manager.mode is ThemeMode.SYSTEM
    manager.set_mode(ThemeMode.HIGH_CONTRAST)

    assert manager.color("window_background").name() == "#123456"


def test_runs_table_model_exposes_chinese_status() -> None:
    now = datetime.now(UTC)
    model = RunsTableModel()
    model.set_items(
        [
            RunSummary(
                run_id="run-1",
                paper_name="paper.pdf",
                rubric_id="rubric@1",
                provider="openai",
                model="model",
                status=RunStatus.REPORTED,
                created_at=now,
                updated_at=now,
            )
        ]
    )

    assert model.rowCount() == 1
    assert model.data(model.index(0, 4)) == "已完成"
    assert model.run_id(0) == "run-1"


def test_runs_table_model_exposes_v2_status_text_and_accessible_icon(qapp: QApplication) -> None:
    manager = FluentThemeManager(qapp)
    icons = FluentIconService(manager)
    now = datetime.now(UTC)
    model = RunsTableModel(icons)
    model.set_items(
        [
            RunSummary(
                run_id="run-v2",
                paper_name="paper.pdf",
                rubric_id="rubric@2",
                provider="openai",
                model="model",
                status=RunStatus.AWAITING_HARD_RULE_CONFIRMATION,
                created_at=now,
                updated_at=now,
            )
        ]
    )

    status_index = model.index(0, 4)
    assert model.data(status_index) == "待人工复核"
    assert not model.data(status_index, Qt.ItemDataRole.DecorationRole).isNull()
    assert "等待人工确认否决项" in model.data(
        status_index,
        Qt.ItemDataRole.AccessibleDescriptionRole,
    )


def test_runs_filter_includes_v2_scoring_and_panel_states() -> None:
    assert "scoring" in RunsFilterProxyModel.STATUS_GROUPS["active"]
    assert RunsFilterProxyModel.STATUS_GROUPS["hard_rule"] == {
        "awaiting_hard_rule_confirmation",
        "awaiting_panel_review",
        "reported_pending_human_review",
    }
    assert RunsFilterProxyModel.STATUS_GROUPS["panel"] == {
        "panel_reviewing",
        "supplemental_reviewing",
        "awaiting_panel_review",
    }


def test_runs_filter_combines_status_and_paper_name() -> None:
    now = datetime.now(UTC)
    model = RunsTableModel()
    model.set_items(
        [
            RunSummary(
                run_id="active-paper",
                paper_name="本科论文.pdf",
                rubric_id="rubric@1",
                provider="openai",
                model="model",
                status=RunStatus.REVIEWING,
                created_at=now,
                updated_at=now,
            ),
            RunSummary(
                run_id="done-paper",
                paper_name="本科论文.pdf",
                rubric_id="rubric@1",
                provider="openai",
                model="model",
                status=RunStatus.REPORTED,
                created_at=now,
                updated_at=now,
            ),
            RunSummary(
                run_id="other-paper",
                paper_name="其他文档.pdf",
                rubric_id="rubric@1",
                provider="openai",
                model="model",
                status=RunStatus.REPORTED,
                created_at=now,
                updated_at=now,
            ),
        ]
    )
    proxy = RunsFilterProxyModel()
    proxy.setSourceModel(model)

    proxy.set_status_mode("reported")
    proxy.set_search_text("本科")

    assert proxy.rowCount() == 1
    assert proxy.data(proxy.index(0, 0)) == "本科论文.pdf"


def test_runs_table_treats_naive_database_times_as_utc() -> None:
    timestamp = datetime(2026, 1, 2, 3, 4)
    model = RunsTableModel()
    model.set_items(
        [
            RunSummary(
                run_id="run-1",
                paper_name="paper.pdf",
                rubric_id="rubric@1",
                provider="openai",
                model="model",
                status=RunStatus.REPORTED,
                created_at=timestamp,
                updated_at=timestamp,
            )
        ]
    )
    expected = timestamp.replace(tzinfo=UTC).astimezone().strftime("%Y-%m-%d %H:%M")

    assert model.data(model.index(0, 3)) == expected
    assert model.data(
        model.index(0, 3),
        role=Qt.ItemDataRole.AccessibleTextRole,
    ) == f"创建时间：{expected}"
