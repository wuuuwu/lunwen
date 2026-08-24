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
