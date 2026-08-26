from __future__ import annotations

from collections import deque

import pytest

from paper_reviewer.application.metadata_extractor import (
    MAX_METADATA_INPUT_CHARACTERS,
    SUBMIT_METADATA_TOOL,
    extract_submission_metadata,
    filter_identity_blocks,
    is_identity_only_block,
)
from paper_reviewer.domain.document import DocumentBlock, DocumentInfo
from paper_reviewer.domain.submission import SubmissionMetadataSource
from paper_reviewer.ports.model import ModelRequest, ModelResponse, ToolCall


class RecordingModel:
    def __init__(self, responses: list[ModelResponse] | None = None) -> None:
        self.responses = deque(responses or [ModelResponse()])
        self.requests: list[ModelRequest] = []

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        return self.responses.popleft()


class FailingModel:
    async def complete(self, request: ModelRequest) -> ModelResponse:
        raise ConnectionError("provider unavailable")


def _document(path: str = "王五_20240001_汉语言文学_文件名题目.pdf") -> DocumentInfo:
    return DocumentInfo(
        document_id="doc-1",
        source_path=path,
        sha256="a" * 64,
        title="PDF 元数据题目",
        page_count=5,
    )


def _block(text: str, *, page: int = 1, y: float = 10) -> DocumentBlock:
    return DocumentBlock.create(
        document_id="doc-1",
        page=page,
        text=text,
        bbox=(1, y, 100, y + 10),
    )


def _model_call(block: DocumentBlock, **values: str) -> ToolCall:
    arguments: dict[str, object] = {}
    for field in ("student_name", "student_id", "major", "paper_title"):
        value = values.get(field)
        arguments[field] = (
            {
                "value": value,
                "block_id": block.block_id,
                "page": block.page,
                "quote": block.text,
                "confidence": 0.88,
            }
            if value is not None
            else None
        )
    return ToolCall(id="call-1", name=SUBMIT_METADATA_TOOL, arguments=arguments)


@pytest.mark.asyncio
async def test_extracts_cover_labels_before_model_and_uses_one_forced_request() -> None:
    cover = _block(
        "姓名：张三 学号：2024123456 专业：计算机科学与技术 "
        "论文题目：课程平台的设计与实现"
    )
    model = RecordingModel(
        [
            ModelResponse(
                tool_calls=[
                    _model_call(
                        cover,
                        student_name="李四",
                        student_id="2024999999",
                        major="软件工程",
                        paper_title="模型题目",
                    )
                ]
            )
        ]
    )

    result = await extract_submission_metadata(
        model=model,
        document=_document(),
        blocks=[cover],
        run_id="run-1",
        pdf_metadata={"author": "PDF 作者", "title": "PDF 题目"},
    )

    assert result.student_name == "张三"
    assert result.student_id == "2024123456"
    assert result.major == "计算机科学与技术"
    assert result.paper_title == "课程平台的设计与实现"
    assert all(
        detail.source is SubmissionMetadataSource.COVER_LABEL
        for detail in result.field_evidence.values()
    )
    assert result.needs_review is False
    assert len(model.requests) == 1
    request = model.requests[0]
    assert request.forced_tool_name == SUBMIT_METADATA_TOOL
    assert request.temperature == 0
    assert request.max_output_tokens == 1024
    assert request.idempotency_key == "run-1:submission-metadata"
    assert [tool.name for tool in request.tools] == [SUBMIT_METADATA_TOOL]


@pytest.mark.asyncio
async def test_verified_model_evidence_beats_pdf_and_filename() -> None:
    block = _block("学生信息：赵六 2024332211 经济学 模型识别的课程论文题目")
    model = RecordingModel(
        [
            ModelResponse(
                tool_calls=[
                    _model_call(
                        block,
                        student_name="赵六",
                        student_id="2024332211",
                        major="经济学",
                        paper_title="模型识别的课程论文题目",
                    )
                ]
            )
        ]
    )

    result = await extract_submission_metadata(
        model=model,
        document=_document(),
        blocks=[block],
        run_id="run-2",
        pdf_metadata={"author": "PDF 作者", "title": "PDF 题目"},
    )

    assert result.student_name == "赵六"
    assert result.student_id == "2024332211"
    assert result.major == "经济学"
    assert result.paper_title == "模型识别的课程论文题目"
    assert all(
        detail.source is SubmissionMetadataSource.MODEL_EVIDENCE
        for detail in result.field_evidence.values()
    )
    assert result.needs_review is False


@pytest.mark.asyncio
async def test_invalid_model_output_falls_back_without_exposing_response() -> None:
    foreign = _block("不存在于输入证据的内容")
    model = RecordingModel(
        [
            ModelResponse(
                content="sensitive raw provider response",
                tool_calls=[_model_call(foreign, student_name="模型猜测姓名")],
            )
        ]
    )

    result = await extract_submission_metadata(
        model=model,
        document=_document(),
        blocks=[],
        run_id="run-3",
        pdf_metadata={"author": "PDF 作者", "title": "PDF 题目"},
    )

    assert result.student_name == "PDF 作者"
    assert result.student_id == "20240001"
    assert result.major == "汉语言文学"
    assert result.paper_title == "PDF 题目"
    assert result.needs_review is True
    assert "sensitive raw provider response" not in result.model_dump_json()


@pytest.mark.asyncio
async def test_missing_fields_use_fixed_placeholders() -> None:
    model = RecordingModel([ModelResponse()])
    document = _document("paper.pdf").model_copy(update={"title": None})

    result = await extract_submission_metadata(
        model=model,
        document=document,
        blocks=[],
        run_id="run-4",
    )

    assert result.student_name == "未识别姓名"
    assert result.student_id == "未识别学号"
    assert result.major == "未识别专业"
    assert result.paper_title == "paper"
    assert result.field_evidence["student_name"].source is SubmissionMetadataSource.PLACEHOLDER
    assert result.field_evidence["paper_title"].source is SubmissionMetadataSource.FILE_NAME
    assert result.needs_review is True


@pytest.mark.asyncio
async def test_model_transport_error_is_not_converted_to_local_fallback() -> None:
    with pytest.raises(ConnectionError, match="provider unavailable"):
        await extract_submission_metadata(
            model=FailingModel(),
            document=_document(),
            blocks=[],
            run_id="run-5",
        )


@pytest.mark.asyncio
async def test_prompt_uses_ordered_first_three_pages_and_character_limit() -> None:
    blocks = [
        _block("page four secret", page=4),
        _block("B" * 15_000, page=2, y=20),
        _block("A" * 15_000, page=1, y=10),
    ]
    model = RecordingModel([ModelResponse()])

    await extract_submission_metadata(
        model=model,
        document=_document(),
        blocks=blocks,
        run_id="run-6",
    )

    prompt = model.requests[0].messages[1].content or ""
    evidence_section = prompt.split("\n\n", maxsplit=1)[1]
    assert len(evidence_section) <= MAX_METADATA_INPUT_CHARACTERS
    assert evidence_section.startswith("[page=1")
    assert "page four secret" not in prompt


def test_identity_filter_removes_only_identity_front_matter() -> None:
    identity = _block("姓名：张三 学号：2024123456 专业：计算机科学与技术")
    titled_cover = _block("姓名：张三 论文题目：平台设计")
    content = _block("本文分析课程平台的架构与实现。", page=2)

    assert is_identity_only_block(identity) is True
    assert is_identity_only_block(titled_cover) is False
    assert filter_identity_blocks([identity, titled_cover, content]) == [titled_cover, content]


@pytest.mark.asyncio
async def test_cover_labels_and_values_in_adjacent_blocks_are_extracted() -> None:
    blocks = [
        _block("姓名", y=10),
        _block("周同学", y=20),
        _block("学号", y=30),
        _block("2024556677", y=40),
        _block("专业", y=50),
        _block("工商管理", y=60),
        _block("论文题目", y=70),
        _block("企业治理课程案例分析", y=80),
    ]
    model = RecordingModel([ModelResponse()])

    result = await extract_submission_metadata(
        model=model,
        document=_document("unknown.pdf"),
        blocks=blocks,
        run_id="run-7",
    )

    assert result.student_name == "周同学"
    assert result.student_id == "2024556677"
    assert result.major == "工商管理"
    assert result.paper_title == "企业治理课程案例分析"
    assert all(
        detail.source is SubmissionMetadataSource.COVER_LABEL
        for detail in result.field_evidence.values()
    )
    assert is_identity_only_block(blocks[0]) is True


@pytest.mark.asyncio
async def test_adjacent_metadata_labels_are_never_mistaken_for_values() -> None:
    blocks = [
        _block("姓名", y=10),
        _block("学号", y=20),
        _block("张三", y=30),
        _block("2024556677", y=40),
    ]
    model = RecordingModel([ModelResponse()])

    result = await extract_submission_metadata(
        model=model,
        document=_document("unknown.pdf"),
        blocks=blocks,
        run_id="run-misaligned-labels",
    )

    assert result.student_name != "学号"
