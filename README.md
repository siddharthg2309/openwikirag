# OpenWikiRAG

OpenWikiRAG is a production-oriented enterprise knowledge platform combining WikiRAG, knowledge graphs, hybrid retrieval, citation-grounded generation, and voice interfaces.

Read the implementation and learning blueprint first:

- [`docs/OPENWIKIRAG_ENGINEERING_GUIDE.md`](docs/OPENWIKIRAG_ENGINEERING_GUIDE.md)
- [`metrics.md`](metrics.md) — verified metrics and resume evidence ledger

## Local quickstart

Requirements: Python 3.13, `uv`, Node.js, and Docker Desktop.

```bash
cp .env.example .env
uv sync --dev
docker compose up -d
uv run alembic upgrade head
uv run uvicorn apps.api.app.main:app --reload
```

Run the asynchronous worker in a second terminal:

```bash
uv run python -m apps.worker.app.main
```

Use `--once` for one bounded relay/reclaim/consume cycle during local smoke
checks. The current worker produces deterministic normalized artifacts for
UTF-8 text, Markdown, text-bearing digital PDFs, and DOCX. PDF quality
metadata identifies pages that may need the opt-in OCR fallback.

Then open:

- API docs: <http://127.0.0.1:8000/docs>
- Liveness: <http://127.0.0.1:8000/healthz>
- Readiness: <http://127.0.0.1:8000/readyz>

Run the Phase 0 checks:

```bash
uv run pytest
uv run ruff check .
uv run mypy
```

For local development authentication:

```bash
curl -X POST http://127.0.0.1:8000/api/v1/auth/register \
  -H 'Content-Type: application/json' \
  -d '{"tenant_name":"Acme Engineering","email":"alice@example.com","password":"a-strong-local-password"}'
```

The registration response provides a tenant id. Exchange the email, password,
and tenant id at `/api/v1/auth/token` for a short-lived access token and an
opaque refresh token. Use the access token with `/api/v1/me`.

Document intake currently validates and registers PDF, DOCX, Markdown, and
UTF-8 text uploads. The raw bytes go to the configured local object root and
PostgreSQL stores the document/version metadata plus a pending ingestion job.
The worker relays the outbox event through Redis Streams and extracts UTF-8
text, Markdown, text-bearing digital PDFs, and DOCX into immutable normalized
artifacts. DOCX extraction is a bounded standard-library OOXML parser that
preserves paragraph spans and heading paths. PDF artifacts also record
deterministic page-coverage quality metadata and an OCR handoff signal; OCR is
disabled by default.

Phase 4 currently includes a deterministic metadata contract over normalized
artifacts for title, language, headings, dates, and authors, plus a typed
WikiRAG page skeleton with stable section ids. Its evidence ranges point back
to normalized text; LLM-generated fields, page persistence, and human review
are later slices.

The OCR boundary is implemented behind replaceable page-renderer and engine
ports, with optional Poppler/Tesseract process adapters. Native OCR is disabled
by default; enable it with `OPENWIKIRAG_OCR_ENABLED=true` after installing the
native tools with `brew install poppler tesseract`. The repository does not
claim native recognition unless that runtime has been exercised on the target
machine.

```bash
curl -X POST http://127.0.0.1:8000/api/v1/documents \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -F 'upload=@./example.pdf;type=application/pdf'
```

The default compose setup creates a privileged migration/admin role and a
separate `openwikirag_app` runtime role on a fresh PostgreSQL volume. Existing
volumes are not modified automatically; recreate or migrate them deliberately
if their initialized roles differ from `.env`.

PostgreSQL integration and RLS proof:

```bash
OPENWIKIRAG_TEST_POSTGRES_URL=postgresql+asyncpg://... \
  uv run pytest apps/api/tests/test_postgres_integration.py
```

The integration test applies Alembic migrations, uses a non-superuser role,
and verifies that membership and audit rows are restricted by transaction-local
tenant context.
