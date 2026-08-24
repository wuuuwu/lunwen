# Paper Reviewer

Paper Reviewer is a Python 3.12 harness for evidence-grounded academic paper review. It uses a
deterministic workflow around bounded LLM reviewers. Models make semantic judgments; Python code
controls state, tools, budgets, validation, persistence, and reporting.

## Current capabilities

- Searchable-PDF ingestion with stable page/block references
- Validated, versioned YAML rubrics and reviewer profiles
- OpenAI and DeepSeek through a common provider boundary
- Five specialist reviewers, an isolated 3+2 expert panel, and a summary-only meta-reviewer
- Allow-listed paper, scholarly-evidence, and live `web_search` tools
- Zero-key DDGS metasearch plus OpenAlex, Crossref, and arXiv with graceful degradation
- Automatic bibliography verification with stable evidence IDs and explicit human-check warnings
- SQLite checkpoints and resumable runs
- Markdown, JSON, evidence, and run-summary artifacts
- Zhejiang undergraduate-thesis Schema v2 with nine discrete 0-4 diagnostic ratings
- Human confirmation gates for political-direction and academic-integrity suspicions
- Deterministic AI-assisted risk decisions that remain separate from the experimental score
- Legacy Schema v1 and unscored-task compatibility
- A native PySide6 Fluent 2 desktop app with dynamic rubric/report rendering
- Windows Credential Manager integration for provider keys

## Install

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python -m pip install uv
.\.venv\Scripts\uv sync --extra dev
```

Copy `.env.example` to `.env` and set at least one provider key.

## Use

```powershell
.\.venv\Scripts\paper-review init
.\.venv\Scripts\paper-review doctor
.\.venv\Scripts\paper-review rubric validate configs/rubrics/zhejiang_undergraduate_thesis_v2.yaml
.\.venv\Scripts\paper-review profile validate configs/review_profiles/zhejiang_undergraduate_specialists_v1.yaml

.\.venv\Scripts\paper-review run paper.pdf `
  --provider openai `
  --model YOUR_MODEL_NAME `
  --discipline-name "计算机科学与技术" `
  --allow-cloud-processing `
  --non-classified
```

Resume a failed or interrupted run:

```powershell
.\.venv\Scripts\paper-review resume RUN_ID
```

The default v2 rubric produces an experimental diagnostic score and an independent risk review.
It has no passing score. It is an AI-assisted pre-review tool, not an official Zhejiang Education
Department inspection result, and it has not completed educational-measurement validity testing.
It must not be used for automatic discipline, degree decisions, or official inspection findings.
The legacy unscored v1 rubric remains available at `configs/rubrics/unscored_draft.yaml`.

When external search is enabled, the harness automatically extracts detected bibliography entries,
checks DOI/title/year matches across DDGS and scholarly indexes, and writes
`reference-checks.json` beside the report. Verified matches become citable evidence. Probable,
conflicting, unavailable, or missing matches are retained as report warnings that explicitly request
manual checking; a search outage does not fail the whole review. DDGS needs no API key but sends
queries to public search engines, so disable external search for offline or network-restricted runs.

## Desktop app

Start the Chinese PySide6 desktop client during development:

```powershell
.\.venv\Scripts\paper-review-app.exe
```

The client stores its SQLite database, reports, logs, and non-secret preferences under
`%LOCALAPPDATA%\PaperReviewer`. OpenAI and DeepSeek API keys are stored through Windows Credential
Manager. Rubric dimensions, anchors, hard rules, scores, and report findings are rendered from the
selected YAML/report data rather than hard-coded in the interface.

The “添加查重/学术不端检测报告” control is intentionally a placeholder in this release. It does
not open a file picker and no detection-report path or content is stored, traced, or sent to a
model. Teachers may record the result of an offline check in the structured human-review reason.

Completed reports can be exported from the report page as the canonical Markdown file or as a
searchable, white-background A4 PDF. PDF generation is fully local and does not call an LLM or load
external images. The last successful export directory is remembered for the next save operation.

Open the development-only Fluent control gallery:

```powershell
.\.venv\Scripts\paper-review-gallery.exe
```

Build the portable Windows directory and ZIP:

```powershell
.\scripts\build_portable.ps1
```

The executable is written to `dist\PaperReviewer\PaperReviewer.exe`; Python does not need to be
installed on the target computer.

## Architecture

The package follows a ports-and-adapters layout:

```text
CLI -> application services -> pure domain models
             |                       ^
             v                       |
            ports <- infrastructure adapters
```

See `docs/architecture.md` and `docs/rubric-spec.md` for extension contracts.
