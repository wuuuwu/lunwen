from __future__ import annotations

from collections import deque

import pytest

from paper_reviewer.application.metadata_extractor import (
    MAX_METADATA_INPUT_CHARACTERS,
    SUBMIT_METADATA_TOOL,
    extract_submission_metadata,
    filter_identity_blocks,
    is_identity_only_block,
    suggest_submission_metadata_locally,
)
from paper_reviewer.domain.document import DocumentBlock, DocumentInfo
from paper_reviewer.domain.submission import (
    SUBMISSION_METADATA_FIELDS,
    SubmissionFieldEvidence,
    SubmissionMetadata,
    SubmissionMetadataSource,
)
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
async def test_invalid_model_output_ignores_pdf_metadata_without_exposing_response() -> None:
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

    assert result.student_name == "王五"
    assert result.student_id == "20240001"
    assert result.major == "汉语言文学"
    assert result.paper_title == "文件名题目"
    assert all(
        detail.source is SubmissionMetadataSource.FILE_NAME
        for detail in result.field_evidence.values()
    )
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
    assert result.paper_title == "未识别题目"
    assert result.field_evidence["student_name"].source is SubmissionMetadataSource.PLACEHOLDER
    assert result.field_evidence["paper_title"].source is SubmissionMetadataSource.PLACEHOLDER
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


@pytest.mark.asyncio
async def test_name_stops_before_score_and_other_field_boundaries() -> None:
    cover = _block(
        "姓名：张三 得分：95 学号：2024556677 班级：示例252 "
        "任课教师：孙老师 考核方法：课程论文"
    )
    result = await extract_submission_metadata(
        model=RecordingModel([ModelResponse()]),
        document=_document("upload.pdf"),
        blocks=[cover],
        run_id="run-name-boundary",
    )

    assert result.student_name == "张三"
    assert result.student_id == "2024556677"
    assert result.field_evidence["student_name"].source is SubmissionMetadataSource.COVER_LABEL


@pytest.mark.asyncio
async def test_adjacent_name_value_stops_before_score_label() -> None:
    blocks = [_block("姓名", y=10), _block("张三 得分：", y=20)]
    result = await extract_submission_metadata(
        model=RecordingModel([ModelResponse()]),
        document=_document("upload.pdf"),
        blocks=blocks,
        run_id="run-adjacent-name-boundary",
    )

    assert result.student_name == "张三"


@pytest.mark.asyncio
async def test_name_stops_before_score_label_without_layout_whitespace() -> None:
    cover = _block("姓名：张三得分： 学号：2024556677")
    result = await extract_submission_metadata(
        model=RecordingModel([ModelResponse()]),
        document=_document("upload.pdf"),
        blocks=[cover],
        run_id="run-adjacent-name-score-without-space",
    )

    assert result.student_name == "张三"
    assert result.student_id == "2024556677"


@pytest.mark.asyncio
async def test_visible_title_beats_model_and_hidden_title_is_only_diagnostic() -> None:
    visible_line_1 = _block("数字经济背景下", page=2, y=20)
    visible_line_2 = _block("企业创新机制研究", page=2, y=35)
    model_block = _block("模型候选题目", page=1, y=80)
    document = _document("upload.pdf").model_copy(
        update={
            "title": "示例学院",
            "embedded_title": "示例学院",
            "visible_title": "数字经济背景下企业创新机制研究",
            "visible_title_page": 2,
            "visible_title_block_ids": [visible_line_1.block_id, visible_line_2.block_id],
        }
    )
    model = RecordingModel(
        [ModelResponse(tool_calls=[_model_call(model_block, paper_title="模型候选题目")])]
    )

    result = await extract_submission_metadata(
        model=model,
        document=document,
        blocks=[visible_line_1, visible_line_2, model_block],
        run_id="run-visible-title",
        pdf_metadata={"title": "示例学院", "author": "WPS 用户"},
    )

    assert result.paper_title == "数字经济背景下企业创新机制研究"
    evidence = result.field_evidence["paper_title"]
    assert evidence.source is SubmissionMetadataSource.VISIBLE_HEADING
    assert evidence.page == 2
    assert evidence.block_id == visible_line_1.block_id
    assert evidence.block_ids == [visible_line_1.block_id, visible_line_2.block_id]
    assert "PDF 隐藏标题与正文题目不一致" in result.warnings
    assert len(model.requests) == 1


@pytest.mark.asyncio
async def test_explicit_title_label_beats_visible_heading() -> None:
    cover = _block("论文题目：明确标注的论文题目")
    document = _document("upload.pdf").model_copy(
        update={
            "visible_title": "版面识别题目",
            "visible_title_page": 2,
            "visible_title_block_ids": ["visible-block"],
        }
    )

    result = await extract_submission_metadata(
        model=RecordingModel([ModelResponse()]),
        document=document,
        blocks=[cover],
        run_id="run-labeled-title",
    )

    assert result.paper_title == "明确标注的论文题目"
    assert result.field_evidence["paper_title"].source is SubmissionMetadataSource.COVER_LABEL


@pytest.mark.asyncio
async def test_hidden_title_cannot_be_selected_without_body_evidence() -> None:
    document = _document("ordinary-platform-upload.pdf").model_copy(
        update={"title": "示例学院", "embedded_title": "示例学院"}
    )

    result = await extract_submission_metadata(
        model=RecordingModel([ModelResponse()]),
        document=document,
        blocks=[],
        run_id="run-hidden-title-only",
        pdf_metadata={"title": "示例学院", "author": "WPS 用户"},
    )

    assert result.student_name == "未识别姓名"
    assert result.paper_title == "未识别题目"
    assert result.field_evidence["paper_title"].source is SubmissionMetadataSource.PLACEHOLDER


@pytest.mark.asyncio
async def test_generic_visible_title_fallback_is_not_reported_as_hidden_metadata() -> None:
    cover = _block("论文题目：明确正文题目")
    document = _document("upload.pdf").model_copy(
        update={"title": "解析器通用可见标题", "embedded_title": None}
    )

    result = await extract_submission_metadata(
        model=RecordingModel([ModelResponse()]),
        document=document,
        blocks=[cover],
        run_id="run-no-embedded-title",
    )

    assert result.paper_title == "明确正文题目"
    assert "PDF 隐藏标题与正文题目不一致" not in result.warnings


@pytest.mark.asyncio
async def test_chinese_layout_whitespace_is_normalized_for_model_title_evidence() -> None:
    block = _block("数字化时代的\n企业管理创新研究", page=2)
    model = RecordingModel(
        [ModelResponse(tool_calls=[_model_call(block, paper_title="数字化时代的企业管理创新研究")])]
    )

    result = await extract_submission_metadata(
        model=model,
        document=_document("upload.pdf"),
        blocks=[block],
        run_id="run-layout-whitespace",
    )

    assert result.paper_title == "数字化时代的企业管理创新研究"
    assert result.field_evidence["paper_title"].source is SubmissionMetadataSource.MODEL_EVIDENCE


@pytest.mark.asyncio
async def test_visible_title_keeps_original_chinese_punctuation() -> None:
    title = "课程评分：人工智能时代大学生能力重构、挑战与培养路径"
    document = _document("upload.pdf").model_copy(
        update={
            "visible_title": title,
            "visible_title_page": 2,
            "visible_title_block_ids": ["visible-title"],
        }
    )

    result = await extract_submission_metadata(
        model=RecordingModel([ModelResponse()]),
        document=document,
        blocks=[],
        run_id="run-title-punctuation",
    )

    assert result.paper_title == title


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "title",
    [
        "课程评分：人工智能时代大学生能力重构、挑战与培养路径",
        "专业：课程目标一致性评价的机制研究",
    ],
)
async def test_title_internal_field_label_is_not_treated_as_layout_boundary(
    title: str,
) -> None:
    document = _document("upload.pdf").model_copy(
        update={
            "visible_title": title,
            "visible_title_page": 2,
            "visible_title_block_ids": ["visible-title"],
        }
    )

    result = await extract_submission_metadata(
        model=RecordingModel([ModelResponse()]),
        document=document,
        blocks=[],
        run_id="run-title-internal-label",
    )

    assert result.paper_title == title


@pytest.mark.asyncio
async def test_labeled_title_internal_field_word_is_not_truncated() -> None:
    title = "课程评分：人工智能时代大学生能力重构"
    cover = _block(f"论文题目：{title}")

    result = await extract_submission_metadata(
        model=RecordingModel([ModelResponse()]),
        document=_document("upload.pdf"),
        blocks=[cover],
        run_id="run-labeled-title-internal-field",
    )

    assert result.paper_title == title
    assert result.field_evidence["paper_title"].source is (
        SubmissionMetadataSource.COVER_LABEL
    )


@pytest.mark.asyncio
async def test_only_structured_filename_provides_title_fallback() -> None:
    unstructured = await extract_submission_metadata(
        model=RecordingModel([ModelResponse()]),
        document=_document("platform_upload_final.pdf").model_copy(update={"title": None}),
        blocks=[],
        run_id="run-unstructured-name",
    )
    structured = await extract_submission_metadata(
        model=RecordingModel([ModelResponse()]),
        document=_document("王五_20240001_汉语言文学_结构化题目.pdf").model_copy(
            update={"title": None}
        ),
        blocks=[],
        run_id="run-structured-name",
    )

    assert unstructured.paper_title == "未识别题目"
    assert unstructured.field_evidence["paper_title"].source is SubmissionMetadataSource.PLACEHOLDER
    assert structured.paper_title == "结构化题目"
    assert structured.field_evidence["paper_title"].source is SubmissionMetadataSource.FILE_NAME


def test_schema_11_defaults_and_old_10_snapshot_remains_readable() -> None:
    evidence = {
        field: SubmissionFieldEvidence(
            source=SubmissionMetadataSource.COVER_LABEL,
            confidence=0.95,
        )
        for field in SUBMISSION_METADATA_FIELDS
    }
    old = SubmissionMetadata.model_validate(
        {
            "schema_version": "1.0",
            "student_name": "张三",
            "student_id": "2024556677",
            "major": "工商管理",
            "paper_title": "企业治理研究",
            "field_evidence": {
                field: detail.model_dump(exclude={"block_ids"})
                for field, detail in evidence.items()
            },
            "warnings": ["一条不再触发核对的历史告警"],
            "needs_review": True,
        }
    )
    current = old.model_copy(update={"schema_version": "1.1"})

    assert old.schema_version == "1.0"
    assert old.human_reviewed is False
    assert old.field_evidence["paper_title"].block_ids is None
    assert old.pending_review_fields == ()
    assert old.needs_review is False
    assert current.schema_version == "1.1"
    assert SubmissionMetadata.model_validate(
        {
            **old.model_dump(exclude={"schema_version", "pending_review_fields", "needs_review"}),
            "schema_version": "1.1",
        }
    ).schema_version == "1.1"


def test_pending_review_uses_field_evidence_boundary_and_human_confirmation() -> None:
    evidence = {
        field: SubmissionFieldEvidence(
            source=SubmissionMetadataSource.COVER_LABEL,
            confidence=0.95,
        )
        for field in SUBMISSION_METADATA_FIELDS
    }
    evidence["student_name"] = SubmissionFieldEvidence(
        source=SubmissionMetadataSource.MODEL_EVIDENCE,
        confidence=0.75,
    )
    evidence["paper_title"] = SubmissionFieldEvidence(
        source=SubmissionMetadataSource.MODEL_EVIDENCE,
        confidence=0.749,
    )
    metadata = SubmissionMetadata(
        student_name="张三",
        student_id="2024556677",
        major="工商管理",
        paper_title="企业治理研究",
        field_evidence=evidence,
        warnings=["历史告警不应扩大待核对范围"],
    )

    assert metadata.pending_review_fields == ("paper_title",)
    assert metadata.needs_review is True
    confirmed = metadata.model_copy(update={"human_reviewed": True})
    assert confirmed.pending_review_fields == ()
    assert confirmed.needs_review is False


def test_local_suggestion_revalidates_existing_model_evidence_without_model_port() -> None:
    block = _block("论文正文中的可靠模型题目", page=2)
    evidence = {
        field: SubmissionFieldEvidence(
            source=SubmissionMetadataSource.PLACEHOLDER,
            confidence=0,
        )
        for field in SUBMISSION_METADATA_FIELDS
    }
    evidence["paper_title"] = SubmissionFieldEvidence(
        source=SubmissionMetadataSource.MODEL_EVIDENCE,
        confidence=0.88,
        page=block.page,
        block_id=block.block_id,
        evidence=block.text,
    )
    current = SubmissionMetadata(
        schema_version="1.0",
        student_name="未识别姓名",
        student_id="未识别学号",
        major="未识别专业",
        paper_title="可靠模型题目",
        field_evidence=evidence,
    )

    suggestion = suggest_submission_metadata_locally(
        document=_document("upload.pdf"),
        blocks=[block],
        current=current,
    )
    stale = suggest_submission_metadata_locally(
        document=_document("upload.pdf"),
        blocks=[_block("已经变化的正文", page=2)],
        current=current,
    )

    assert suggestion.paper_title == "可靠模型题目"
    assert suggestion.field_evidence["paper_title"].source is (
        SubmissionMetadataSource.MODEL_EVIDENCE
    )
    assert stale.paper_title == "未识别题目"
