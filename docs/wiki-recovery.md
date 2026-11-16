# Wiki corpus recovery

The resumable corpus runner uses `scripts/wiki_batch.py` with the current Wiki tools.
The approved repair campaign started on 2026-11-15 with 122 failed PDFs.
Two workers recover failures while one continues remaining PDFs; publication and shared
indexes are serialized. The persistent monitor checks each worker every minute.

Paper, Idea and Experiment paths preserve Unicode identifiers consistently. Duplicate
page/figure anchors receive stable occurrence suffixes. No more than one representative
architecture image is published per paper.

Saved drafts are reused only with matching full-source document ID and PDF SHA256.
`wiki_recovery.py` restores exact reference quote spans when only display markup or
whitespace differed, and fills unambiguous identifiers actually printed in that entry.
It cannot invent identifiers from memory or arbitrary link destinations. A complete
visible DOI may be reconciled with its source PDF DOI link only when the two differ
solely in hyphens or whitespace; unrelated labels and different numbers are rejected.
Original drafts, source,
automatic changes, model edits and independent reviews are retained under
`.paper-agent/wiki-preprocessing/<document_id>/batch-recovery/`.
Journal DOI suffixes are excluded from arXiv detection, so a suffix such as
`2026.12865` cannot be reinserted as an arXiv identifier after a reviewer removes it.
The explicit `10.48550/arXiv.<ID>` namespace remains supported.

Abstract provenance may exclude explicitly identified contact/publication footnotes
and ACS navigation inserted between abstract paragraphs. Scientific text is never
discarded by this normalization. If the layout extraction corrupts the abstract,
the batch adds independent native text from the same SHA256-checked PDF's first
three pages. Raw native extraction and physical line-end hyphen normalization are
recorded; the original full MinerU source stays intact. Reviewers see both sources.
Malformed model edits get one bounded correction attempt against the unchanged draft.

Corrections use bounded JSON pointer edits to the Wiki draft. Paper identity is frozen;
classification and bibliography can be corrected. Bibliography order, labels and count
are preserved. All edits are validated and independently reviewed before publication.
Unresolved factual errors remain failures; source conflicts and accepted coverage gaps
remain explicit in quality records. Existing completed papers are preserved.

State and partial-node migration backups are under
`.paper-agent/wiki-rebuild-20261110/`. `report.json` remains the progress source of truth.
