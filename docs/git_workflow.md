# Git Workflow & Review Convention

1. **One branch per build task**
   - Create a descriptive branch from `main` for each task (e.g., `phase-0/db-foundation`, `phase-1/gstin-validator`, `phase-1/importer`).
   - Never commit directly to `main`.

2. **Peer PR review before merge**
   - Open a Pull Request for every completed task.
   - **A second intern must review and approve the PR** before merging into `main`.

3. **Pre-merge checks**
   - `pytest` passes cleanly.
   - No `.env`, API keys, or real client `.csv` / `.xlsx` files are staged or committed (`git status` verified; only anonymized fixtures under `tests/fixtures/` are allowed).
