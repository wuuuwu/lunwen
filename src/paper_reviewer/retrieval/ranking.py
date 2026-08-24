from __future__ import annotations

import math
import re
from collections import Counter

from pydantic import BaseModel

from paper_reviewer.domain.document import DocumentBlock


class RankedBlock(BaseModel):
    block: DocumentBlock
    score: float


def search_blocks(blocks: list[DocumentBlock], query: str, *, limit: int = 8) -> list[RankedBlock]:
    query_terms = _tokens(query)
    if not query_terms:
        return []
    document_frequencies: Counter[str] = Counter()
    block_tokens: list[list[str]] = []
    for block in blocks:
        tokens = _tokens(block.text)
        block_tokens.append(tokens)
        document_frequencies.update(set(tokens))
    total = max(len(blocks), 1)
    average_length = sum(map(len, block_tokens)) / total or 1
    ranked: list[RankedBlock] = []
    for block, tokens in zip(blocks, block_tokens, strict=True):
        frequencies = Counter(tokens)
        score = 0.0
        for term in query_terms:
            frequency = frequencies[term]
            if not frequency:
                continue
            inverse = math.log(
                1 + (total - document_frequencies[term] + 0.5) / (document_frequencies[term] + 0.5)
            )
            normalization = frequency + 1.5 * (1 - 0.75 + 0.75 * len(tokens) / average_length)
            score += inverse * frequency * 2.5 / normalization
        if score > 0:
            ranked.append(RankedBlock(block=block, score=score))
    return sorted(ranked, key=lambda item: item.score, reverse=True)[:limit]


def _tokens(value: str) -> list[str]:
    return re.findall(r"[a-zA-Z0-9_]{2,}|[\u4e00-\u9fff]", value.lower())
