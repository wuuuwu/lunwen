# Architecture

## Responsibility boundary

The harness owns workflow state, retries, timeouts, tool permissions, evidence validation, scoring
policy, and artifacts. The model owns only bounded semantic judgments.

The legacy v1 workflow is:

```text
CREATED -> INGESTING -> INGESTED -> BUILDING_EVIDENCE -> EVIDENCE_READY
        -> REVIEWING -> AUDITING -> META_REVIEWING -> VALIDATING -> REPORTED
```

Each successful stage is checkpointed. A retryable failure resumes at the first unfinished stage.
Provider-specific payloads are normalized at `ModelPort`, so the domain and orchestration layers do
not depend on OpenAI or DeepSeek types.

The Schema v2 dual-advisory workflow is:

```text
INGEST -> EVIDENCE -> SCORING -> AUDIT -> HUMAN HARD-RULE GATE
       -> 3 INITIAL EXPERTS -> OPTIONAL 2 SUPPLEMENTAL EXPERTS
       -> SYNTHESIS -> REPORT
```

Specialist results and every panel opinion are persisted immediately. Resume invokes only missing
reviewers. A confirmed human hard-rule decision deterministically triggers risk and cannot be
overridden by the diagnostic score or expert votes. `unable_to_assess` pauses at the human-panel
state. The meta-reviewer writes only a summary; Python recomputes and audits the score, human
decisions, votes, and final decision path.

## Reviewer isolation

Specialist reviewer agents receive the paper, evidence ledger, professional context, and assigned
dimensions. Each policy panel expert receives the complete rubric and validated findings, but the
panel API cannot accept other experts' opinions. The meta-reviewer receives validated structured
results only. It may merge and explain disagreements, but cannot change scores, human decisions,
votes, the deterministic decision, or invent a new major or critical finding.

## Memory

- Working memory: messages and tool results inside one bounded reviewer job.
- Evidence memory: stable paper blocks and collected scholarly metadata for one run.
- Cache memory: provider-independent public metadata that may be reused.
- Long-term review memory: intentionally absent from the MVP.

External documents are data, never instructions. Tools are registered by trusted code and filtered
through a static allowlist before every execution.

## Web research and reference verification

External-search runs use the provider-neutral `WebSearchPort`; the default adapter is the keyless,
MIT-licensed DDGS metasearch client in `auto` mode. Search calls are bounded, rate-limited, and
return snippets plus source URLs as metadata-level evidence. Reviewers may call `web_search` only
when their profile allowlists it, and tool results are treated as untrusted evidence rather than
instructions.

Before reviewers start, the harness extracts numbered bibliography entries and checks DOI, title,
and year matches against DDGS, OpenAlex, Crossref, and arXiv. Verified/probable matches are frozen
into the run evidence ledger; unresolved, conflicting, or unavailable results are written to
`reference-checks.json` and surfaced as audit warnings requesting manual verification. The existing
human gates for policy and integrity decisions remain unchanged.

The implementation borrows the provider-neutral function-tool and rate-limit pattern documented by
[smolagents built-in tools](https://huggingface.co/docs/smolagents/reference/default_tools), while
using the MIT-licensed [DDGS metasearch library](https://github.com/deedy5/ddgs) directly instead
of requiring a commercial search API account. It intentionally does not fetch arbitrary result-page
content: snippets remain metadata-level evidence, which keeps the first release within a smaller
SSRF and prompt-injection surface.
