"""Shared relationship vocabulary for Wiki writers and readers."""

PAPER_RELATIONS = frozenset({
    "extends", "contradicts", "supersedes", "shares_method", "addresses_same_problem",
})
RELATION_TYPES = PAPER_RELATIONS | {
    "addresses_gap", "inspired_by", "tested_by", "supports", "invalidates",
    "depends_on", "refutes", "uses", "cites",
}
# Historical transfer hypotheses remain readable, but cannot be newly published.
READABLE_RELATION_TYPES = RELATION_TYPES | {"may_apply_to"}
