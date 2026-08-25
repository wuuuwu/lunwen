from __future__ import annotations

import asyncio
import inspect
import os
from pathlib import Path
from typing import Annotated, Any, cast

import typer
from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table

from paper_reviewer.adapters.documents.pymupdf_parser import PyMuPDFParser
from paper_reviewer.adapters.persistence.database import (
    create_engine,
    create_session_factory,
    initialize_database,
)
from paper_reviewer.adapters.persistence.repositories import (
    DocumentRepository,
    EvidenceRepository,
    ReviewRepository,
    RunRepository,
)
from paper_reviewer.application.orchestrator import (
    ReviewOrchestrator,
    load_provider_snapshot,
    load_run_request_context,
    load_run_snapshots,
)
from paper_reviewer.application.providers import builtin_provider_connections
from paper_reviewer.application.runtime import review_runtime
from paper_reviewer.config import Settings, load_review_profile, load_rubric
from paper_reviewer.domain.provider import (
    ModelApiProtocol,
    ProviderSnapshot,
    normalize_base_url,
)
from paper_reviewer.domain.run import RunRecord

load_dotenv()

app = typer.Typer(no_args_is_help=True, help="Evidence-grounded academic paper review harness.")
rubric_app = typer.Typer(no_args_is_help=True, help="Rubric operations.")
profile_app = typer.Typer(no_args_is_help=True, help="Reviewer profile operations.")
app.add_typer(rubric_app, name="rubric")
app.add_typer(profile_app, name="profile")
console = Console()
DEFAULT_RUBRIC = Path("configs/rubrics/zhejiang_undergraduate_thesis_v2.yaml")
DEFAULT_PROFILE = Path("configs/review_profiles/zhejiang_undergraduate_specialists_v1.yaml")
DEFAULT_PANEL_PROFILE = Path("configs/review_profiles/zhejiang_independent_panel_v1.yaml")


@app.command("init")
def initialize() -> None:
    """Initialize local storage and run directories."""
    settings = Settings()
    asyncio.run(_initialize(settings))
    settings.runs_dir.mkdir(parents=True, exist_ok=True)
    console.print(f"[green]Initialized[/green] database and {settings.runs_dir.resolve()}")


@app.command()
def doctor() -> None:
    """Check the local runtime and optional provider credentials."""
    table = Table(title="Paper Reviewer environment")
    table.add_column("Check")
    table.add_column("Status")
    table.add_row("Python package", "ok")
    table.add_row("OPENAI_API_KEY", "configured" if os.getenv("OPENAI_API_KEY") else "missing")
    table.add_row("DEEPSEEK_API_KEY", "configured" if os.getenv("DEEPSEEK_API_KEY") else "missing")
    table.add_row("Default rubric", "ok" if DEFAULT_RUBRIC.is_file() else "missing")
    table.add_row("Default profile", "ok" if DEFAULT_PROFILE.is_file() else "missing")
    console.print(table)


@rubric_app.command("validate")
def validate_rubric(path: Path) -> None:
    rubric = load_rubric(path)
    console.print(
        f"[green]Valid[/green] {rubric.rubric_id}@{rubric.version}; "
        f"dimensions={len(rubric.dimensions)}, scoring={rubric.scoring_enabled}"
    )


@profile_app.command("validate")
def validate_profile(path: Path) -> None:
    profile = load_review_profile(path)
    console.print(
        f"[green]Valid[/green] {profile.profile_id}@{profile.version}; "
        f"reviewers={len(profile.reviewers)}"
    )


@app.command("run")
def run_review(
    paper: Annotated[Path, typer.Argument(exists=True, readable=True, dir_okay=False)],
    provider: Annotated[str, typer.Option(help="openai or deepseek")],
    model: Annotated[str, typer.Option(help="Provider model name")],
    rubric: Annotated[Path, typer.Option(exists=True, readable=True)] = DEFAULT_RUBRIC,
    profile: Annotated[Path, typer.Option(exists=True, readable=True)] = DEFAULT_PROFILE,
    no_external_search: Annotated[
        bool,
        typer.Option(
            help="Disable DDGS web search, scholarly metadata search, and reference checks."
        ),
    ] = False,
    discipline_name: Annotated[
        str | None, typer.Option("--discipline-name", help="本科论文所属专业名称。")
    ] = None,
    discipline_profile: Annotated[
        Path | None,
        typer.Option(
            "--discipline-profile",
            exists=True,
            readable=True,
            dir_okay=False,
            help="可选的专业培养目标 YAML。",
        ),
    ] = None,
    cloud_processing_authorized: Annotated[
        bool,
        typer.Option(
            "--allow-cloud-processing",
            "--cloud-processing-authorized",
            help="确认拥有将论文发送至云端模型处理的授权。",
        ),
    ] = False,
    non_classified: Annotated[
        bool,
        typer.Option(
            "--non-classified",
            help="确认论文不包含涉密材料。",
        ),
    ] = False,
    contains_classified_material: Annotated[
        bool,
        typer.Option(
            "--contains-classified-material",
            help="声明论文包含涉密材料（将被拒绝）。",
        ),
    ] = False,
) -> None:
    """Start a new paper review run."""
    _validate_cli_safety(
        discipline_name=discipline_name,
        cloud_processing_authorized=cloud_processing_authorized,
        non_classified=non_classified,
        contains_classified_material=contains_classified_material,
    )
    result = asyncio.run(
        _run_new(
            paper=paper,
            provider=provider,
            model_name=model,
            rubric_path=rubric,
            profile_path=profile,
            external_search=not no_external_search,
            discipline_name=discipline_name,
            discipline_profile=discipline_profile,
            cloud_processing_authorized=cloud_processing_authorized,
            contains_classified_material=contains_classified_material,
        )
    )
    console.print(f"[green]Completed[/green] run {result.run_id}")
    console.print(f"Report: {(Settings().runs_dir / result.run_id / 'report.md').resolve()}")


@app.command()
def resume(run_id: str) -> None:
    """Resume an interrupted run from its latest successful checkpoint."""
    result = asyncio.run(_resume(run_id))
    console.print(f"[green]Completed[/green] run {result.run_id}")


@app.command()
def status(run_id: str) -> None:
    """Display the persisted status of a run."""
    record = asyncio.run(_get_run(run_id))
    if record is None:
        raise typer.BadParameter(f"unknown run id: {run_id}")
    console.print_json(record.model_dump_json(indent=2))


@app.command()
def report(run_id: str) -> None:
    """Print the location of a completed Markdown report."""
    path = Settings().runs_dir / run_id / "report.md"
    if not path.is_file():
        raise typer.BadParameter(f"report does not exist for run: {run_id}")
    console.print(str(path.resolve()))


async def _initialize(settings: Settings) -> None:
    engine = create_engine(settings.database_url)
    try:
        await initialize_database(engine)
    finally:
        await engine.dispose()


async def _run_new(
    *,
    paper: Path,
    provider: str,
    model_name: str,
    rubric_path: Path,
    profile_path: Path,
    external_search: bool,
    discipline_name: str | None = None,
    discipline_profile: Path | None = None,
    cloud_processing_authorized: bool = False,
    contains_classified_material: bool = False,
) -> RunRecord:
    _validate_cli_safety(
        discipline_name=discipline_name,
        cloud_processing_authorized=cloud_processing_authorized,
        non_classified=not contains_classified_material,
        contains_classified_material=contains_classified_material,
    )
    settings = Settings()
    rubric = load_rubric(rubric_path)
    profile = load_review_profile(profile_path)
    panel_profile = (
        load_review_profile(DEFAULT_PANEL_PROFILE) if rubric.schema_version == "2" else None
    )
    engine = create_engine(settings.database_url)
    try:
        await initialize_database(engine)
        sessions = create_session_factory(engine)
        provider_snapshot = _cli_builtin_provider_snapshot(provider, model_name)
        api_key = _cli_provider_api_key(provider_snapshot.provider_ref)
        async with review_runtime(
            settings=settings,
            provider_snapshot=provider_snapshot,
            api_key=api_key,
            external_search=external_search,
            sessions=sessions,
        ) as runtime:
            orchestrator = _orchestrator(
                settings,
                runtime.model,
                runtime.sessions,
                runtime.scholarly_clients,
                runtime.web_search_client,
            )
            return await _create_and_execute(
                orchestrator,
                input_path=paper,
                rubric=rubric,
                profile=profile,
                panel_profile=panel_profile,
                provider=provider,
                model_name=model_name,
                discipline_name=discipline_name,
                discipline_profile=discipline_profile,
                cloud_processing_authorized=cloud_processing_authorized,
                contains_classified_material=contains_classified_material,
                external_search=external_search,
            )
    finally:
        await engine.dispose()


def _validate_cli_safety(
    *,
    discipline_name: str | None,
    cloud_processing_authorized: bool,
    non_classified: bool,
    contains_classified_material: bool,
) -> None:
    """Reject unsafe cloud runs before a PDF or API request is opened."""
    if not discipline_name or not discipline_name.strip():
        raise typer.BadParameter("必须提供 --discipline-name 专业名称。")
    if not cloud_processing_authorized:
        raise typer.BadParameter("必须显式提供 --allow-cloud-processing，确认拥有云端处理授权。")
    if contains_classified_material or not non_classified:
        raise typer.BadParameter("必须显式提供 --non-classified，并确认论文不包含涉密材料。")


async def _create_and_execute(orchestrator: Any, **kwargs: Any) -> RunRecord:
    """Call old or policy-aware orchestrators without dropping new context.

    During the schema migration the orchestrator signature is intentionally
    allowed to lag the CLI.  Passing only parameters supported by the loaded
    implementation keeps old runs working while new implementations receive
    the discipline and safety context.
    """
    method = orchestrator.create_and_execute
    parameters = inspect.signature(method).parameters
    accepts_kwargs = any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters.values()
    )
    if not accepts_kwargs:
        kwargs = {name: value for name, value in kwargs.items() if name in parameters}
    return cast(RunRecord, await method(**kwargs))


async def _resume(run_id: str) -> RunRecord:
    settings = Settings()
    engine = create_engine(settings.database_url)
    try:
        await initialize_database(engine)
        sessions = create_session_factory(engine)
        repository = RunRepository(sessions)
        run = await repository.get(run_id)
        if run is None:
            raise typer.BadParameter(f"unknown run id: {run_id}")
        run_dir = settings.runs_dir / run_id
        if _cli_resume_requires_desktop(run, run_dir):
            raise typer.BadParameter(
                "自定义 Provider 和 Responses API 任务首版仅支持在桌面端恢复，"
                "请打开应用后恢复该任务。"
            )
        rubric, profile = load_run_snapshots(run_dir)
        context = load_run_request_context(run_dir)
        external_search = context.get("external_search", True) is not False
        provider_snapshot = load_provider_snapshot(run_dir)
        if provider_snapshot is None:
            provider_snapshot = _cli_builtin_provider_snapshot(run.provider, run.model)
        else:
            _validate_cli_builtin_snapshot(run.provider, provider_snapshot)
        api_key = _cli_provider_api_key(provider_snapshot.provider_ref)
        async with review_runtime(
            settings=settings,
            provider_snapshot=provider_snapshot,
            api_key=api_key,
            external_search=external_search,
            sessions=sessions,
        ) as runtime:
            orchestrator = _orchestrator(
                settings,
                runtime.model,
                runtime.sessions,
                runtime.scholarly_clients,
                runtime.web_search_client,
            )
            return await orchestrator.execute(run, rubric=rubric, profile=profile)
    finally:
        await engine.dispose()


def _cli_resume_requires_desktop(run: RunRecord, run_dir: Path) -> bool:
    """Keep desktop-only Provider credentials and protocols out of the CLI."""

    provider_snapshot = load_provider_snapshot(run_dir)
    return (
        run.provider.startswith("custom:")
        or run.provider == "openai_responses"
        or (
            provider_snapshot is not None
            and provider_snapshot.protocol is ModelApiProtocol.RESPONSES
        )
    )


async def _get_run(run_id: str) -> RunRecord | None:
    settings = Settings()
    engine = create_engine(settings.database_url)
    await initialize_database(engine)
    sessions = create_session_factory(engine)
    try:
        return await RunRepository(sessions).get(run_id)
    finally:
        await engine.dispose()


def _orchestrator(
    settings: Settings,
    model: Any,
    sessions: Any,
    scholarly: list[Any],
    web_search: Any | None,
) -> ReviewOrchestrator:
    from sqlalchemy.ext.asyncio import async_sessionmaker

    if not isinstance(sessions, async_sessionmaker):
        raise TypeError("invalid SQLAlchemy session factory")
    return ReviewOrchestrator(
        settings=settings,
        model=model,
        parser=PyMuPDFParser(),
        run_repository=RunRepository(sessions),
        document_repository=DocumentRepository(sessions),
        evidence_repository=EvidenceRepository(sessions),
        review_repository=ReviewRepository(sessions),
        scholarly_clients=scholarly,
        web_search_client=web_search,
    )


def _cli_builtin_provider_snapshot(provider: str, model: str) -> ProviderSnapshot:
    """Build the immutable connection input required by ``review_runtime``.

    The CLI intentionally only accepts the built-in Chat Completions providers.
    Responses and custom providers remain desktop-only, including on resume.
    """

    normalized = provider.lower()
    connection = next(
        (
            candidate
            for candidate in builtin_provider_connections()
            if candidate.provider_ref == normalized
            and candidate.protocol is ModelApiProtocol.CHAT_COMPLETIONS
        ),
        None,
    )
    if connection is None:
        raise ValueError(f"unsupported model provider: {provider}")
    return ProviderSnapshot(
        provider_ref=connection.provider_ref,
        display_name=connection.display_name,
        protocol=connection.protocol,
        base_url=connection.base_url,
        endpoint_fingerprint=connection.endpoint_fingerprint,
        model=model,
    )


def _validate_cli_builtin_snapshot(provider: str, snapshot: ProviderSnapshot) -> None:
    """Reject mutable or cross-provider snapshots before reading credentials.

    Legacy CLI tasks may contain a provider artifact written by the desktop
    application.  The CLI only supports the fixed built-in Chat endpoints, so
    every field that controls the destination must match the built-in catalog.
    """

    normalized_provider = provider.lower()
    expected = next(
        (
            candidate
            for candidate in builtin_provider_connections()
            if candidate.provider_ref == normalized_provider
            and candidate.protocol is ModelApiProtocol.CHAT_COMPLETIONS
        ),
        None,
    )
    if expected is None:
        raise ValueError(f"unsupported model provider: {provider}")
    try:
        normalized_base_url = normalize_base_url(snapshot.base_url)
    except ValueError as error:
        raise ValueError("CLI Provider 快照的 Base URL 无效。") from error
    if (
        snapshot.provider_ref != expected.provider_ref
        or snapshot.protocol is not expected.protocol
        or snapshot.base_url != normalized_base_url
        or normalized_base_url != expected.base_url
        or snapshot.endpoint_fingerprint != expected.endpoint_fingerprint
    ):
        raise ValueError("CLI Provider 快照与内置 Provider 固定端点不一致。")


def _cli_provider_api_key(provider: str) -> str:
    if provider == "openai":
        variable = "OPENAI_API_KEY"
    elif provider == "deepseek":
        variable = "DEEPSEEK_API_KEY"
    else:
        raise ValueError(f"unsupported model provider: {provider}")
    value = os.getenv(variable)
    if not value:
        raise ValueError(f"{variable} is not configured")
    return value


if __name__ == "__main__":
    app()
