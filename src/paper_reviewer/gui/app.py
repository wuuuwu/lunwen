from __future__ import annotations

import asyncio
import inspect
import logging
import sys
from importlib import resources
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QApplication

if TYPE_CHECKING:
    from paper_reviewer.application.app_state import AppPaths

CREDENTIAL_SELF_TEST_FLAG = "--self-test-credentials"
DATABASE_SELF_TEST_FLAG = "--self-test-database"
RESOURCE_SELF_TEST_FLAG = "--self-test-resources"
REPORT_EXPORT_SELF_TEST_FLAG = "--self-test-report-export"

_REQUIRED_CONFIGS = (
    "zhejiang_undergraduate_thesis_v2.yaml",
    "zhejiang_undergraduate_specialists_v1.yaml",
    "zhejiang_independent_panel_v1.yaml",
)
_REQUIRED_ICONS = (
    "add_document.svg",
    "arrow_download.svg",
    "check.svg",
    "error.svg",
    "folder.svg",
    "history.svg",
    "info.svg",
    "play.svg",
    "refresh.svg",
    "rubric.svg",
    "search.svg",
    "settings.svg",
    "stop.svg",
    "warning.svg",
)

_REPORT_DISCLAIMER = "本结果不是浙江省教育厅正式抽检结论"


def configure_logging(paths: AppPaths) -> None:
    paths.ensure()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=[
            logging.FileHandler(paths.logs_dir / "paper-reviewer.log", encoding="utf-8"),
        ],
    )


def run_database_self_test() -> None:
    """Create, query, and dispose a temporary SQLite database through the app service."""
    from paper_reviewer.application.app_state import AppPaths
    from paper_reviewer.application.service import ReviewApplicationService

    with TemporaryDirectory(prefix="paper-reviewer-db-probe-") as temporary:
        root = Path(temporary)
        paths = AppPaths(
            root=root,
            data_dir=root / "data",
            runs_dir=root / "runs",
            logs_dir=root / "logs",
            config_dir=root / "config",
        )
        service = ReviewApplicationService(paths=paths)
        asyncio.run(service.list_runs())


def run_packaging_resource_self_test() -> None:
    package_resources = resources.files("paper_reviewer.resources")
    prompt_resources = resources.files("paper_reviewer.agents.prompts")
    gui_resources = resources.files("paper_reviewer.gui.resources")
    icon_resources = resources.files("paper_reviewer.gui.resources.icons")
    reporting_style_resources = resources.files("paper_reviewer.reporting.resources")
    bundle_root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[3]))
    required = [
        prompt_resources.joinpath("panel_reviewer.txt"),
        gui_resources.joinpath("fluent.qss.in"),
        gui_resources.joinpath("fluent2-qt-tokens.json"),
        reporting_style_resources.joinpath("report_print.css"),
        *(icon_resources.joinpath(name) for name in _REQUIRED_ICONS),
    ]
    # Keep the reporting package resource lookup above even though the
    # exporter module is imported lazily: a frozen build must carry its print
    # stylesheet alongside the renderer.
    missing = [str(item) for item in required if not item.is_file()]
    source_config_directories = (
        bundle_root / "configs" / "rubrics",
        bundle_root / "configs" / "review_profiles",
    )
    for name in _REQUIRED_CONFIGS:
        candidates = (
            package_resources.joinpath("configs", name),
            *(directory / name for directory in source_config_directories),
        )
        if not any(candidate.is_file() for candidate in candidates):
            missing.append(str(candidates[0]))
    migration = bundle_root / "migrations" / "versions" / "0003_evaluation_persistence.py"
    if not migration.is_file():
        missing.append(str(migration))
    if missing:
        raise FileNotFoundError("Missing packaged resources: " + ", ".join(missing))


def _call_report_pdf_renderer(
    renderer: object,
    markdown: str,
    markdown_path: Path,
    destination: Path,
) -> object:
    """Call the public PDF renderer while allowing its stable API to evolve.

    The export implementation is owned by the reporting package.  During the
    transition from the renderer prototype, accepted parameter names differed
    (``markdown`` vs ``markdown_path`` and ``destination`` vs ``output_path``).
    This adapter intentionally maps only those semantic names and fails fast
    for a new required argument rather than silently producing an empty PDF.
    """
    render = renderer
    if not callable(render):
        raise TypeError("paper_reviewer.reporting.exporter.render_pdf is not callable")
    signature = inspect.signature(render)
    kwargs: dict[str, object] = {}
    positional: list[object] = []
    for parameter in signature.parameters.values():
        if parameter.kind in {
            inspect.Parameter.VAR_POSITIONAL,
            inspect.Parameter.VAR_KEYWORD,
        }:
            continue
        name = parameter.name.casefold()
        value: object | None = None
        mapped = True
        if "markdown" in name and "path" in name:
            value = markdown_path
        elif name in {"markdown", "markdown_text", "markdown_content", "content", "text"}:
            value = markdown
        elif any(token in name for token in ("destination", "output_path", "target_path")):
            value = destination
        elif name in {"output", "target", "path", "pdf_path"}:
            value = destination
        elif name in {"title", "document_title"}:
            value = "中文论文 AI 辅助评测报告"
        elif name in {"author", "creator"}:
            value = "Paper Reviewer packaging self-test"
        else:
            mapped = False

        if mapped:
            if parameter.kind is inspect.Parameter.POSITIONAL_ONLY:
                positional.append(value)
            else:
                kwargs[parameter.name] = value
            continue
        if parameter.default is inspect.Parameter.empty:
            raise TypeError(f"unsupported required render_pdf argument: {parameter.name}")

    return render(*positional, **kwargs)


def run_report_export_self_test() -> None:
    """Render a deterministic Chinese multi-page PDF and verify its contents."""
    try:
        import pymupdf
    except ImportError as exc:  # pragma: no cover - dependency is in the app bundle
        raise RuntimeError("PyMuPDF is required for the report export self-test") from exc

    try:
        from paper_reviewer.reporting.exporter import render_pdf
    except ImportError as exc:  # pragma: no cover - exercised by an incomplete bundle
        raise RuntimeError("report PDF exporter is missing from the application bundle") from exc

    # A real QGuiApplication is required by QTextDocument/QPdfWriter on Qt
    # builds that resolve fonts through the application font database.
    app = QGuiApplication.instance()
    owns_application = app is None
    if app is None:
        app = QApplication([])
    try:
        sections = []
        for index in range(1, 36):
            sections.append(
                f"## 第 {index} 节：评测结果与证据\n\n"
                "本节用于验证中文字体、表格分页和长报告排版。评测记录应保持可审计，"
                "并明确区分论文证据、专家意见与确定性决策。\n\n"
                "| 指标 | 状态 | 说明 |\n| --- | --- | --- |\n"
                f"| 指标 {index} | 通过 | 第 {index} 页的证据摘要与改进建议 |\n\n"
            )
        markdown = (
            "# 中文论文 AI 辅助评测报告\n\n"
            "这是一份用于便携版打包自检的临时报告。\n\n" + "\n".join(sections) + "\n## 重要说明\n\n"
            f"- {_REPORT_DISCLAIMER}。\n"
            "- 百分制和五级锚点为本项目自定义诊断规则。\n"
        )
        with TemporaryDirectory(prefix="paper-reviewer-export-probe-") as temporary:
            root = Path(temporary)
            markdown_path = root / "probe.md"
            destination = root / "probe.pdf"
            markdown_path.write_text(markdown, encoding="utf-8")
            result = _call_report_pdf_renderer(render_pdf, markdown, markdown_path, destination)
            if isinstance(result, bytes) and not destination.is_file():
                destination.write_bytes(result)
            if not destination.is_file():
                raise RuntimeError("report PDF exporter did not create its destination")
            payload = destination.read_bytes()
            if not payload.startswith(b"%PDF-"):
                raise RuntimeError("report PDF exporter produced an invalid PDF signature")
            document = pymupdf.open(destination)  # type: ignore[no-untyped-call]
            try:
                if len(document) < 2:
                    raise RuntimeError("report PDF exporter produced fewer than two pages")
                extracted = "\n".join(
                    page.get_text()
                    for page in document  # type: ignore[attr-defined]
                )
            finally:
                document.close()  # type: ignore[no-untyped-call]
            if "中文论文 AI 辅助评测报告" not in extracted:
                raise RuntimeError("report PDF exporter lost Chinese text")
            if _REPORT_DISCLAIMER not in extracted:
                raise RuntimeError("report PDF exporter lost the report disclaimer")
    finally:
        if owns_application and app is not None:
            app.quit()


def main() -> int:
    if CREDENTIAL_SELF_TEST_FLAG in sys.argv[1:]:
        try:
            from paper_reviewer.adapters.security.keyring_store import SystemCredentialStore

            SystemCredentialStore.self_test()
        except Exception:
            return 1
        return 0
    if DATABASE_SELF_TEST_FLAG in sys.argv[1:]:
        try:
            run_database_self_test()
        except Exception:
            return 1
        return 0
    if RESOURCE_SELF_TEST_FLAG in sys.argv[1:]:
        try:
            run_packaging_resource_self_test()
        except Exception:
            return 1
        return 0
    if REPORT_EXPORT_SELF_TEST_FLAG in sys.argv[1:]:
        try:
            run_report_export_self_test()
        except Exception:
            return 1
        return 0
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    app = QApplication(sys.argv)
    app.setApplicationName("Paper Reviewer")
    app.setOrganizationName("PaperReviewer")
    from paper_reviewer.application.app_state import AppPaths, PreferencesStore

    paths = AppPaths.for_current_user()
    configure_logging(paths)
    # Keep module import and packaging self-tests independent from the full
    # window graph.  Heavy model, parser, search, and PDF-export dependencies
    # are themselves loaded lazily by the application service when used.
    # Imports remain on the normal startup path so frozen builds still discover
    # the same Python modules.
    from paper_reviewer.application.service import ReviewApplicationService
    from paper_reviewer.gui.main_window import MainWindow
    from paper_reviewer.gui.theme import FluentThemeManager

    store = PreferencesStore(paths.preferences_path)
    preferences = store.load()
    theme = FluentThemeManager(app)
    theme.set_mode(preferences.theme)
    service = ReviewApplicationService(paths=paths)
    window = MainWindow(
        service=service,
        paths=paths,
        preferences=preferences,
        preferences_store=store,
        theme=theme,
    )
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
