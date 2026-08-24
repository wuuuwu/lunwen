from __future__ import annotations

from paper_reviewer.domain.document import DocumentBlock
from paper_reviewer.retrieval.ranking import search_blocks


class PaperReaderTools:
    def __init__(self, blocks: list[DocumentBlock]) -> None:
        self.blocks = blocks
        self.by_id = {block.block_id: block for block in blocks}

    def search_paper(self, query: str, limit: int = 8) -> list[dict[str, object]]:
        return [
            {
                "block_id": item.block.block_id,
                "page": item.block.page,
                "section_path": item.block.section_path,
                "text": item.block.text,
                "score": round(item.score, 4),
            }
            for item in search_blocks(self.blocks, query, limit=min(max(limit, 1), 12))
        ]

    def read_blocks(self, block_ids: list[str]) -> list[dict[str, object]]:
        if len(block_ids) > 12:
            raise ValueError("at most 12 blocks can be read at once")
        return [
            {
                "block_id": block.block_id,
                "page": block.page,
                "section_path": block.section_path,
                "text": block.text,
            }
            for block_id in block_ids
            if (block := self.by_id.get(block_id)) is not None
        ]


def register_paper_tools(registry: object, tools: PaperReaderTools) -> None:
    from paper_reviewer.tools.registry import ToolRegistry

    if not isinstance(registry, ToolRegistry):
        raise TypeError("registry must be a ToolRegistry")
    registry.register(
        name="search_paper",
        description="Search the paper for blocks relevant to a query.",
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 12},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        handler=tools.search_paper,
    )
    registry.register(
        name="read_blocks",
        description="Read paper blocks by stable block id.",
        parameters={
            "type": "object",
            "properties": {
                "block_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "maxItems": 12,
                }
            },
            "required": ["block_ids"],
            "additionalProperties": False,
        },
        handler=tools.read_blocks,
    )
