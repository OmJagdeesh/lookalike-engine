# Lookalike Engine

B2B lookalike lead-scoring engine for Indian SMBs. Given a client's best existing customers (and lost/churned deals), it enriches public business attributes, learns an explainable Ideal Customer Profile (ICP), expands candidates in a scoped city/industry, and ranks verified active leads with human-readable reasons.

## Tech Stack (v1)
- Python 3.11+
- PostgreSQL
- `pandas`, `openpyxl`, `httpx`, `pydantic`, `psycopg[binary]`, `sqlalchemy`, `python-dotenv`, `streamlit`, `pytest`

## Setup

1. Create and activate a virtual environment, then install dependencies:
   ```bash
   uv venv --python 3.11
   uv pip install -e ".[dev]"
   ```
   *(Or with standard `pip`: `python -m venv .venv && pip install -e ".[dev]"`)*.

2. Copy `.env.example` to `.env` and fill in `DATABASE_URL` and API keys:
   ```bash
   cp .env.example .env
   ```

3. Apply the PostgreSQL schema (`db/schema.sql`):
   ```bash
   python -c "from db import apply_schema; apply_schema()"
   ```

4. Run tests:
   ```bash
   pytest
   ```

## Governance & DPDP Compliance
- Follow [docs/client_onboarding_checklist.md](docs/client_onboarding_checklist.md) before importing any real client list (signed DPA required).
- Follow [docs/git_workflow.md](docs/git_workflow.md): one branch per build task, PR reviewed by a second intern before merging.
- Erase a client's proprietary data on contract end or erasure request:
  ```bash
  python -m db.erasure --client-id <CLIENT_ID> --yes
  ```
