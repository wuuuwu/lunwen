# Rubric specification

A rubric is a versioned YAML document validated by `RubricProfile` before any paid model call.

Schema v1 scored rubrics must satisfy all of the following:

1. Dimension identifiers are unique.
2. Dimension weights total 100.
3. Every dimension contains at least one check and one score anchor.
4. Score anchors are within the dimension range and do not overlap.
5. An aggregation policy is present.
6. Every dimension is assigned to at least one reviewer by ID or tag.

Schema v2 additionally rejects every unknown field and unsupported schema version. It requires:

1. Traceable policy source, document number, effective date, and SHA-256.
2. First-level groups that cover each second-level dimension exactly once.
3. A strict integer 0-4 scale with exact anchors for every dimension.
4. `evaluation_mode: dual_advisory`, `aggregation.method: weighted_rating`, and
   `passing_score: null`.
5. Structured hard rules whose AI states are limited to `not_detected`, `suspected`, and
   `not_assessable`; confirmation and dismissal are human decisions.
6. The deterministic 3+2 independent-panel strategy.
7. An experimental 0.x version and an explicit educational-validity warning.

The built-in Zhejiang configuration lives at
`configs/rubrics/zhejiang_undergraduate_thesis_v2.yaml`. Its percentage score is a project-defined
diagnostic transformation, not a policy-defined course grade or passing threshold. Schema v1 and
unscored snapshots remain readable.
