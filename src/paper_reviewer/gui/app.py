from __future__ import annotations

import asyncio
import inspect
import logging
import os
import sys
from importlib import resources
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QApplication

if TYPE_CHECKING:
    from paper_reviewer.adapters.security.keyring_store import SystemCredentialStore
    from paper_reviewer.application.app_state import AppPaths

CREDENTIAL_SELF_TEST_FLAG = "--self-test-credentials"
DATABASE_SELF_TEST_FLAG = "--self-test-database"
RESOURCE_SELF_TEST_FLAG = "--self-test-resources"
REPORT_EXPORT_SELF_TEST_FLAG = "--self-test-report-export"
BATCH_RESOURCE_SELF_TEST_FLAG = "--self-test-batch-resources"
BATCH_OUTPUT_SELF_TEST_FLAG = "--self-test-batch-output"
GUI_STARTUP_SELF_TEST_FLAG = "--self-test-gui-startup"

_REQUIRED_CONFIGS = (
    "course_paper_v1.yaml",
    "course_paper_reviewers_v1.yaml",
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

_REPORT_DISCLAIMER = "本结果是课程论文评分辅助，不自动成为教师正式成绩"


def configure_credential_namespace() -> SystemCredentialStore:
    """Keep course-edition secrets separate from the thesis-review product."""

    from paper_reviewer.adapters.security.keyring_store import SystemCredentialStore
    from paper_reviewer.application.app_state import COURSE_CREDENTIAL_SERVICE_NAME

    return SystemCredentialStore(
        service_name=COURSE_CREDENTIAL_SERVICE_NAME,
        # Course and thesis desktop products may import a credential once, but
        # do not share mutable environment-variable fallback afterwards.
        environments={},
    )


def configure_logging(paths: AppPaths) -> None:
    paths.ensure()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=[
            logging.FileHandler(paths.logs_dir / "course-paper-reviewer.log", encoding="utf-8"),
        ],
    )


def run_database_self_test() -> None:
    """Create, query, and dispose a temporary SQLite database through the app service."""
    from paper_reviewer.application.app_state import AppPaths
    from paper_reviewer.application.service import ReviewApplicationService

    with TemporaryDirectory(prefix="course-paper-reviewer-db-probe-") as temporary:
        root = Path(temporary)
        paths = AppPaths(
            root=root,
            data_dir=root / "data",
            runs_dir=root / "runs",
            logs_dir=root / "logs",
            config_dir=root / "config",
            batches_dir=root / "batches",
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
        prompt_resources.joinpath("reviewer.txt"),
        prompt_resources.joinpath("meta_reviewer.txt"),
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


def run_batch_resource_self_test() -> None:
    """Validate the bundled course Rubric/Profile through their public loaders."""

    from paper_reviewer.config import load_review_profile, load_rubric
    from paper_reviewer.gui.resource_paths import bundled_config

    run_packaging_resource_self_test()
    rubric = load_rubric(bundled_config("course_paper_v1.yaml"))
    profile = load_review_profile(bundled_config("course_paper_reviewers_v1.yaml"))
    if len(rubric.dimensions) != 6:
        raise RuntimeError("course Rubric must contain exactly six built-in dimensions")
    if len(profile.reviewers) != 3:
        raise RuntimeError("course Reviewer Profile must contain exactly three reviewers")


def run_batch_output_self_test() -> None:
    """Exercise batch naming and UTF-8-BOM CSV output without user data."""

    from paper_reviewer.application.batch_output import (
        build_report_filename,
        write_batch_summary_csv,
    )
    from paper_reviewer.config import load_review_profile, load_rubric
    from paper_reviewer.domain.batch import (
        BatchItem,
        BatchItemStatus,
        BatchRecord,
        BatchReviewRequest,
        BatchSourceSnapshot,
    )
    from paper_reviewer.domain.provider import (
        ModelApiProtocol,
        ProviderSnapshot,
        endpoint_fingerprint,
    )
    from paper_reviewer.domain.submission import (
        SUBMISSION_METADATA_FIELDS,
        SubmissionFieldEvidence,
        SubmissionMetadata,
        SubmissionMetadataSource,
    )
    from paper_reviewer.gui.resource_paths import bundled_config

    with TemporaryDirectory(prefix="course-paper-reviewer-batch-output-probe-") as temporary:
        root = Path(temporary)
        source = root / "submission.pdf"
        source.write_bytes(b"%PDF-1.4\n% packaging probe\n")
        evidence = {
            field: SubmissionFieldEvidence(
                source=SubmissionMetadataSource.FILE_NAME,
                confidence=0.9,
            )
            for field in SUBMISSION_METADATA_FIELDS
        }
        metadata = SubmissionMetadata(
            student_name="=2+2",
            student_id="20260001",
            major="课程测试",
            paper_title="中文课程论文",
            field_evidence=evidence,
        )
        filename = build_report_filename(metadata, "a" * 32)
        if not filename.endswith("_课程论文评测报告.pdf"):
            raise RuntimeError("batch report filename contract is unavailable")
        rubric_path = bundled_config("course_paper_v1.yaml")
        profile_path = bundled_config("course_paper_reviewers_v1.yaml")
        request = BatchReviewRequest(
            source_dir=root,
            output_dir=root / "reports",
            provider="openai",
            model="packaging-probe",
            rubric=rubric_path,
            profile=profile_path,
        )
        item = BatchItem(
            item_id="item-1",
            source=BatchSourceSnapshot(
                path=source.resolve(strict=True),
                filename=source.name,
                sha256="0" * 64,
                size_bytes=source.stat().st_size,
                modified_time_ns=source.stat().st_mtime_ns,
            ),
            status=BatchItemStatus.COMPLETED,
            run_id="a" * 32,
            metadata=metadata,
            total_score=82,
            grade="良好",
            conclusion="达到课程论文基本要求",
            report_path=root / "reports" / filename,
        )
        protocol = ModelApiProtocol.CHAT_COMPLETIONS
        base_url = "https://api.openai.com/v1"
        batch = BatchRecord(
            batch_id="batch-probe",
            request=request,
            rubric_snapshot=load_rubric(rubric_path),
            profile_snapshot=load_review_profile(profile_path),
            provider_snapshot=ProviderSnapshot(
                provider_ref="openai",
                display_name="OpenAI · Chat Completions",
                protocol=protocol,
                base_url=base_url,
                endpoint_fingerprint=endpoint_fingerprint(base_url, protocol),
                model="packaging-probe",
            ),
            items=[item],
        )
        destination = root / "reports" / "summary.csv"
        write_batch_summary_csv(destination, batch, [("task_completion", "课程任务完成度")])
        payload = destination.read_bytes()
        if not payload.startswith(b"\xef\xbb\xbf"):
            raise RuntimeError("batch CSV is missing its UTF-8 BOM")
        text = payload.decode("utf-8-sig")
        if "'=2+2" not in text or "课程任务完成度" not in text:
            raise RuntimeError("batch CSV safety or dynamic columns self-test failed")


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
            value = "Course Paper Reviewer packaging self-test"
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
        with TemporaryDirectory(prefix="course-paper-reviewer-export-probe-") as temporary:
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

            credentials = configure_credential_namespace()
            SystemCredentialStore.self_test(service_name=credentials.service_name)
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
    if BATCH_RESOURCE_SELF_TEST_FLAG in sys.argv[1:]:
        try:
            run_batch_resource_self_test()
        except Exception:
            return 1
        return 0
    if BATCH_OUTPUT_SELF_TEST_FLAG in sys.argv[1:]:
        try:
            run_batch_output_self_test()
        except Exception:
            return 1
        return 0
    gui_startup_self_test = GUI_STARTUP_SELF_TEST_FLAG in sys.argv[1:]
    if gui_startup_self_test:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    app = QApplication(sys.argv)
    from paper_reviewer.application.app_state import (
        COURSE_APP_DISPLAY_NAME,
        COURSE_ORGANIZATION_NAME,
        COURSE_SETTINGS_NAME,
        AppPaths,
        PreferencesStore,
    )

    app.setApplicationName(COURSE_SETTINGS_NAME)
    app.setApplicationDisplayName(COURSE_APP_DISPLAY_NAME)
    app.setOrganizationName(COURSE_ORGANIZATION_NAME)
    app.setDesktopFileName(COURSE_SETTINGS_NAME)

    temporary_paths = (
        TemporaryDirectory(prefix="course-paper-reviewer-gui-probe-")
        if gui_startup_self_test
        else None
    )
    if temporary_paths is not None:
        root = Path(temporary_paths.name)
        paths = AppPaths(
            root=root,
            data_dir=root / "data",
            runs_dir=root / "runs",
            logs_dir=root / "logs",
            config_dir=root / "config",
            batches_dir=root / "batches",
        )
    else:
        paths = AppPaths.for_current_user()
    configure_logging(paths)
    if not gui_startup_self_test:
        try:
            from paper_reviewer.application.course_import import (
                import_thesis_provider_settings,
            )

            import_thesis_provider_settings(paths)
        except Exception:
            # Import is a convenience-only, read-only migration.  A damaged source
            # catalog or unavailable Credential Manager must never prevent the
            # isolated course application from starting.
            logging.getLogger(__name__).warning(
                "Thesis provider configuration import was skipped",
                exc_info=False,
            )
    credentials = configure_credential_namespace()
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
    service = ReviewApplicationService(paths=paths, credentials=credentials)
    window = MainWindow(
        service=service,
        paths=paths,
        preferences=preferences,
        preferences_store=store,
        theme=theme,
    )
    window.show()
    if gui_startup_self_test:
        def finish_gui_probe() -> None:
            valid = bool(
                window.isVisible()
                and window.windowTitle().startswith(COURSE_APP_DISPLAY_NAME)
                and window.navigation_model.rowCount() >= 4
                and window.new_review_page.objectName() == "courseBatchNewPage"
            )
            app.exit(0 if valid else 2)

        QTimer.singleShot(250, finish_gui_probe)
    exit_code = app.exec()
    window.close()
    if temporary_paths is not None:
        logging.shutdown()
        temporary_paths.cleanup()
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
