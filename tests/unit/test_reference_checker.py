from __future__ import annotations

import asyncio

import pytest

from paper_reviewer.application.reference_checker import (
    PROBABLE_SCORE_THRESHOLD,
    VERIFIED_SCORE_THRESHOLD,
    check_references,
    extract_references,
)
from paper_reviewer.domain.document import BlockType, DocumentBlock
from paper_reviewer.domain.evidence import EvidenceLevel
from paper_reviewer.domain.reference import ReferenceEntry, ReferenceVerificationStatus
from paper_reviewer.ports.scholarly_search import ScholarlyWork
from paper_reviewer.ports.web_search import WebSearchResult


class FakeWebSearch:
    def __init__(
        self,
        responses: dict[str, list[WebSearchResult]] | None = None,
        *,
        error: Exception | None = None,
    ) -> None:
        self.responses = responses or {}
        self.error = error
        self.calls: list[tuple[str, int]] = []

    async def search(self, query: str, *, limit: int = 5) -> list[WebSearchResult]:
        self.calls.append((query, limit))
        if self.error is not None:
            raise self.error
        return self.responses.get(query, [])


class FakeScholarlySearch:
    def __init__(
        self,
        responses: dict[str, list[ScholarlyWork]] | None = None,
        *,
        error: Exception | None = None,
    ) -> None:
        self.responses = responses or {}
        self.error = error
        self.calls: list[tuple[str, int]] = []

    async def search(self, query: str, *, limit: int = 10) -> list[ScholarlyWork]:
        self.calls.append((query, limit))
        if self.error is not None:
            raise self.error
        return self.responses.get(query, [])


class ConcurrencyTrackingWebSearch:
    def __init__(self, release_at: int) -> None:
        self.release_at = release_at
        self.active = 0
        self.max_active = 0
        self.started = 0
        self.release = asyncio.Event()

    async def search(self, query: str, *, limit: int = 5) -> list[WebSearchResult]:
        self.active += 1
        self.started += 1
        self.max_active = max(self.max_active, self.active)
        if self.started == self.release_at:
            self.release.set()
        await self.release.wait()
        self.active -= 1
        return []


def _block(
    text: str,
    *,
    page: int,
    block_type: BlockType = BlockType.PARAGRAPH,
    section_path: list[str] | None = None,
) -> DocumentBlock:
    return DocumentBlock.create(
        document_id="doc",
        page=page,
        text=text,
        block_type=block_type,
        section_path=section_path,
    )


def _entry(text: str, *, suffix: str, doi: str | None = None) -> ReferenceEntry:
    return ReferenceEntry.create(
        text=text,
        block_id=f"block-{suffix}",
        page=1,
        doi=doi,
        year=int(text[text.index("20") : text.index("20") + 4]) if "20" in text else None,
    )


def test_extracts_english_and_chinese_references_merges_and_deduplicates() -> None:
    heading = _block("References", page=1, block_type=BlockType.HEADING)
    english = _block(
        "[1] Smith, J. Reliable Agent Evaluation. DOI: 10.1000/ABC.123",
        page=2,
        block_type=BlockType.REFERENCE,
        section_path=["References"],
    )
    continuation = _block(
        "Journal of Harness Research, 2022.",
        page=2,
        section_path=["References"],
    )
    duplicate = _block(
        "[2] Smith, J. Reliable Agent Evaluation. https://doi.org/10.1000/abc.123",
        page=3,
        block_type=BlockType.REFERENCE,
        section_path=["References"],
    )
    chinese = _block(
        "[3] 王伟. 大语言模型学术评测研究. 计算机学报, 2023.",
        page=3,
        section_path=["7 参考文献"],
    )

    entries = extract_references([heading, english, continuation, duplicate, chinese])

    assert len(entries) == 2
    assert entries[0].doi == "10.1000/abc.123"
    assert entries[0].year == 2022
    assert entries[0].text.startswith("Smith, J.")
    assert "Journal of Harness Research" in entries[0].text
    assert entries[0].block_id == english.block_id
    assert entries[1].year == 2023
    assert "大语言模型学术评测研究" in entries[1].text
    assert all(not entry.text.startswith("[") for entry in entries)

    repeated = extract_references([heading, english, continuation, duplicate, chinese])
    assert [entry.reference_id for entry in repeated] == [
        entry.reference_id for entry in entries
    ]


def test_reference_id_is_stable_across_location_whitespace_and_doi_forms() -> None:
    first = ReferenceEntry.create(
        text="Smith, J.  A Reliable Study.",
        block_id="first",
        page=1,
        doi="https://doi.org/10.5555/ABC.9",
        year=2020,
    )
    second = ReferenceEntry.create(
        text="Different formatting and location",
        block_id="second",
        page=7,
        doi="doi: 10.5555/abc.9.",
        year=2020,
    )
    whitespace = ReferenceEntry.create(
        text="Smith, J. A\nReliable   Study.",
        block_id="third",
        page=2,
    )
    normalized = ReferenceEntry.create(
        text="Smith, J. A Reliable Study.",
        block_id="fourth",
        page=8,
    )

    assert first.reference_id == second.reference_id
    assert whitespace.reference_id == normalized.reference_id
    assert len(first.reference_id) == 24


def test_splits_concatenated_bracketed_entries_without_splitting_version_number() -> None:
    combined = _block(
        "［1］ Smith. Harness Version 2. Methods. 2020. "  # noqa: RUF001
        "［2］ Wang. Reliable Reference Checking. 2021.",  # noqa: RUF001
        page=4,
        block_type=BlockType.REFERENCE,
    )

    entries = extract_references([combined])

    assert [entry.year for entry in entries] == [2020, 2021]
    assert "Version 2. Methods" in entries[0].text
    assert entries[1].text.startswith("Wang")


@pytest.mark.asyncio
async def test_checks_doi_probable_chinese_and_unresolved_without_fake_evidence() -> None:
    doi_entry = _entry(
        "Smith. Reliable Harnesses. 2020. doi: 10.1000/reliable",
        suffix="doi",
        doi="10.1000/reliable",
    )
    probable_entry = _entry(
        "Lee, Q. Reliable Agent Evaluation. Journal, 2021.", suffix="probable"
    )
    chinese_entry = _entry(
        "王伟. 大语言模型学术评测研究. 2023.", suffix="chinese"
    )
    unresolved_entry = _entry(
        "Unknown, K. Quantum Banana Methods. 2017.", suffix="unresolved"
    )
    responses = {
        "10.1000/reliable": [
            WebSearchResult(
                title="An unrelated display title",
                url="https://example.test/doi",
                snippet="Record metadata",
                source="fake-web",
                metadata={"doi": "https://doi.org/10.1000/RELIABLE", "year": 2020},
            )
        ],
        probable_entry.text: [
            WebSearchResult(
                title="Reliable Agent Evaluation Study",
                url="https://example.test/probable",
                snippet="A related publication",
                source="fake-web",
                metadata={"year": 2021},
            )
        ],
        chinese_entry.text: [
            WebSearchResult(
                title="大语言模型学术评测研究",
                url="https://example.test/chinese",
                snippet="期刊条目",
                source="fake-web",
                metadata={"year": 2023},
            )
        ],
        unresolved_entry.text: [
            WebSearchResult(
                title="Marine Biology Field Notes",
                url="https://example.test/unrelated",
                snippet="A different work",
                source="fake-web",
                metadata={"year": 2017},
            )
        ],
    }
    web = FakeWebSearch(responses)
    scholarly = FakeScholarlySearch()

    report, evidence = await check_references(
        run_id="run-1",
        entries=[doi_entry, probable_entry, chinese_entry, unresolved_entry],
        web_search=web,
        scholarly_clients=[scholarly],
        per_source_limit=3,
        max_concurrency=2,
    )

    assert [check.status for check in report.checks] == [
        ReferenceVerificationStatus.VERIFIED,
        ReferenceVerificationStatus.PROBABLE,
        ReferenceVerificationStatus.VERIFIED,
        ReferenceVerificationStatus.UNRESOLVED,
    ]
    assert report.verified_count == 2
    assert report.probable_count == 1
    assert report.unresolved_count == 1
    assert report.checks[0].score == 1.0
    assert PROBABLE_SCORE_THRESHOLD <= report.checks[1].score < VERIFIED_SCORE_THRESHOLD
    assert len(evidence) == 3
    assert report.checks[-1].matched_evidence_ids == []
    assert all(item.metadata["reference_id"] for item in evidence)
    assert all(item.metadata["reference_text"] for item in evidence)
    assert all(item.metadata["verification_status"] != "unresolved" for item in evidence)
    assert all(item.metadata["query"] for item in evidence)
    assert any(
        unresolved_entry.reference_id in warning and "建议人工核对" in warning
        for warning in report.warnings
    )
    assert all(limit == 3 for _, limit in web.calls)
    assert all(limit == 3 for _, limit in scholarly.calls)

    repeated_report, repeated_evidence = await check_references(
        run_id="run-2",
        entries=[doi_entry, probable_entry, chinese_entry, unresolved_entry],
        web_search=web,
        scholarly_clients=[scholarly],
        per_source_limit=3,
    )
    assert repeated_report.checks[0].entry.reference_id == doi_entry.reference_id
    assert [item.evidence_id for item in repeated_evidence] == [
        item.evidence_id for item in evidence
    ]


@pytest.mark.asyncio
async def test_single_backend_failure_degrades_to_successful_scholarly_result() -> None:
    entry = _entry("Smith. Reliable Agent Evaluation. 2022.", suffix="degraded")
    scholarly = FakeScholarlySearch(
        {
            entry.text: [
                ScholarlyWork(
                    source="crossref",
                    source_id="work-1",
                    title="Reliable Agent Evaluation",
                    abstract="Verified scholarly abstract.",
                    year=2022,
                    level=EvidenceLevel.ABSTRACT,
                )
            ]
        }
    )

    report, evidence = await check_references(
        run_id="run",
        entries=[entry],
        web_search=FakeWebSearch(error=RuntimeError("offline")),
        scholarly_clients=[scholarly],
    )

    assert report.checks[0].status is ReferenceVerificationStatus.VERIFIED
    assert len(evidence) == 1
    assert evidence[0].source_name == "crossref"
    assert any("web:FakeWebSearch" in warning for warning in report.warnings)
    assert all("offline" not in warning for warning in report.warnings)
    assert all("已由其他来源完成核验" in warning for warning in report.warnings)
    assert all("建议人工核对" not in warning for warning in report.warnings)


@pytest.mark.asyncio
async def test_all_backends_failing_is_unresolved_and_never_fabricates_evidence() -> None:
    entry = _entry("No Search Result Available. 2020.", suffix="failure")

    report, evidence = await check_references(
        run_id="run",
        entries=[entry],
        web_search=FakeWebSearch(error=TimeoutError("secret query")),
        scholarly_clients=[FakeScholarlySearch(error=RuntimeError("down"))],
    )

    assert report.checks[0].status is ReferenceVerificationStatus.UNRESOLVED
    assert report.checks[0].matched_evidence_ids == []
    assert report.checks[0].score == 0.0
    assert evidence == []
    assert len(report.warnings) == 3
    assert all("建议人工核对" in warning for warning in report.warnings)
    assert all("secret query" not in warning for warning in report.warnings)


@pytest.mark.asyncio
async def test_short_generic_title_cannot_be_auto_verified() -> None:
    entry = _entry("Smith. Reliable Agent Evaluation Research. 2022.", suffix="generic")
    web = FakeWebSearch(
        {
            entry.text: [
                WebSearchResult(
                    title="Research",
                    url="https://example.test/generic",
                    snippet="Unrelated generic result.",
                    source="fake-web",
                    metadata={"year": 2022},
                )
            ]
        }
    )

    report, evidence = await check_references(
        run_id="run",
        entries=[entry],
        web_search=web,
        scholarly_clients=[],
    )

    assert report.checks[0].status is ReferenceVerificationStatus.UNRESOLVED
    assert evidence == []
    assert any("建议人工核对" in warning for warning in report.warnings)


@pytest.mark.asyncio
async def test_search_calls_respect_global_concurrency_limit_and_preserve_order() -> None:
    entries = [
        _entry(f"Reference Number {index}. 2020.", suffix=str(index)) for index in range(3)
    ]
    web = ConcurrencyTrackingWebSearch(release_at=2)

    report, _ = await check_references(
        run_id="run",
        entries=entries,
        web_search=web,
        scholarly_clients=[],
        max_concurrency=2,
    )

    assert web.max_active == 2
    assert [check.entry.reference_id for check in report.checks] == [
        entry.reference_id for entry in entries
    ]
