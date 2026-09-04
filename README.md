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
WikiRAG page skeleton with stable section ids. A provider-neutral structured
generation boundary now accepts only schema-valid summaries, definitions, and
references whose evidence matches normalized source spans. The document is
packaged as untrusted JSON data for the future provider, and validated
generation results can now be persisted as immutable JSON objects with
tenant/version/checksum metadata in PostgreSQL. A second immutable page artifact
now packages the complete deterministic skeleton together with the validated
generated fields, so a future reader or reviewer can load one self-contained
page object without reconstructing the skeleton. Authenticated tenant members
can now read that page through an integrity-checked API path with success
auditing. Authorized editors/admins can also move its mutable review metadata
through `draft`, `needs_review`, and `approved` with compare-and-set protection
and audit evidence, while the page object remains immutable. The durable worker
now activates the complete deterministic path from normalized artifact through
metadata, page skeleton, structured-generation validation, generation artifact,
and self-contained page artifact before acknowledging Redis. Authorized
editors/admins can queue asynchronous regeneration for an existing page; the
worker validates tenant/version lineage and writes a new draft generation/page
identity under a server-owned configuration hash without overwriting the old
artifact. Repeated delivery reuses immutable artifacts. The local provider is
intentionally deterministic and title-grounded; no live LLM call, reviewer
history, or model-quality claim is active yet. A metadata-only listing endpoint
supports bounded pagination and review-status filtering without loading
object-storage payloads.

Phase 5 now has a deterministic hierarchical chunking boundary. A validated
normalized document is grouped by top-level heading into bounded parent
windows and smaller overlapping child windows, each carrying exact normalized
offsets, section path, page range, checksums, and stable ids. This is a pure
pre-embedding contract; persistence, model-specific tokenization, embeddings,
Qdrant, retrieval, and reranking remain later slices.

The next Phase 5 boundary persists that chunk result as one immutable,
checksum-addressed JSON manifest in object storage plus tenant/version/source
metadata in PostgreSQL. Repeated runs verify and reuse the manifest, while
changed chunking configuration creates a new identity; embedding and Qdrant
projection are still deferred.

Phase 5.3 defines a provider-neutral dense-embedding contract with exact input
checksums, model/provider/configuration identity, fixed dimensions, finite
cosine-normalized vectors, and deterministic canonical bytes. The local
feature-hashing adapter proves reproducibility and adapter shape only; it is not
semantic-search quality evidence.

Phase 5.4 adds a separate deterministic sparse/lexical representation for exact
terms such as identifiers, error codes, and product names. Unicode case-folded
terms are hashed into bounded indices, repeated terms receive positive
sublinear weights, and indices are emitted sorted and unique with exact
input/configuration/provider/model identity. This local adapter proves lexical
shape and replayability; corpus IDF, dense/sparse fusion, persistence, Qdrant,
and semantic ranking quality remain later slices.

Phase 5.5 adds provider-neutral bounded batch orchestration for dense or sparse
requests. It consumes streaming inputs in sequential batches, caps in-flight
provider calls with a concurrency limit, restores original request order, and
cancels unfinished work on failure or caller cancellation. Provider-native
batching, retries, model caching, persistence, and Qdrant projection remain
separate concerns.

Phase 5.6 adds a bounded in-process model/provider cache. Dense and sparse
representation, provider, model, and configuration identity form the cache key;
concurrent misses use single-flight loading, least-recently-used eviction
closes old instances, failed loads are not retained, and shutdown cancels or
closes resources. This caches loaded instances rather than embedding results;
distributed caching, TTL/invalidation, and Qdrant projection remain later
concerns.

Phase 5.7 adds a provider-neutral tenant-scoped vector projection contract. Each
validated chunk can become one immutable point with named dense and sparse
vectors plus document-version, source, page, language, pipeline, checksum, and
model/configuration provenance; raw chunk text remains in canonical artifacts.
Deterministic point identity and canonical-byte comparison make retries create,
reuse, or fail with an immutable conflict, while tenant filters and
tenant-scoped reads fail closed. The application has no Qdrant SDK dependency;
Phase 5.8 activates the live projection adapter with pinned `qdrant-client`
1.14.3 against the local Qdrant 1.14.1 service. It provisions one named dense
cosine vector and one named sparse vector, creates and validates 16 indexed
tenant/document/provenance payload fields, uses deterministic UUID point ids,
and supports tenant-scoped create/reuse/read behavior with validated
round-trips. Hybrid score fusion and retrieval-quality measurements remain
later slices.

Phase 5.9 activates that projection in the durable ingestion worker. After the
WikiRAG page artifact is committed, the worker creates or verifies one immutable
parent/child chunk manifest, runs bounded dense and sparse embedding batches,
and upserts one provenance-only Qdrant point per chunk before marking the job
successful and acknowledging Redis. A retry reuses the manifest and any points
written before a failure. The current deterministic hash providers prove
orchestration and replay behavior, not semantic embedding quality; fusion,
reranking, and projection-performance claims remain deferred.

Phase 6.1 defines the retrieval-side application boundary without coupling it
to Qdrant. A trusted tenant id, normalized and bounded query, bounded optional
filters, mode, and candidate limit produce independently ranked dense and
sparse lists with complete provenance and representation identity. The local
adapter proves mandatory tenant isolation, deterministic cosine-equivalent and
sparse-dot scoring, stable tie-breaking, and fail-closed output validation.
Hybrid mode does not fuse scores; Phase 6.2 below activates its live Qdrant
adapter, while canonical text resolution, fusion, reranking, HTTP authorization,
and retrieval-quality measurements remain later slices.

Phase 6.2 activates that candidate boundary on the live Qdrant adapter. Each
dense or sparse `query_points` call explicitly selects its named vector and
pushes the trusted tenant, provider/model/configuration identity, and bounded
optional metadata filters into Qdrant before top-k selection. UUID alternatives
use nested regular-match OR groups, keyword alternatives use `MatchAny`, and
only scored payloads—not stored vectors—return for strict application-level
validation and stable ranking. Local and Qdrant 1.14.1 tests prove tenant,
filter, and model isolation; cross-leg fusion, canonical text loading,
reranking, HTTP search, quality evaluation, and latency remain deferred.

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
