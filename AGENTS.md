# Repository continuation notes

For Linga legacy-Burmese PDF extraction or acquisition/processing integration,
read `experiments/linga-vector/HANDOFF.md` and its `README.md` before working.
They record executed evidence, the user's design constraints, and unfinished
connections. Do not require the user to explain that design again.

The current extraction experiment is **NOT GREEN**. Do not integrate its
processing into the acquisition pipeline based on SVG export, unit tests,
nearest hypotheses, raw-byte votes, or zero dictionary unknowns alone. Preserve
source documents and unresolved evidence; no source deletion is authorized.

The user needs acquisition and processing to avoid repeated network transfers.
Separate internal modules must not automatically become separate download/upload
stages. The transfer integration is not implemented or approved merely by these
notes. Follow the handoff's evidence/validation checklist before proposing shipment.

These notes do not change existing crawler behavior or authorize new architecture.
