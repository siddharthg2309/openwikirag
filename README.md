# OpenWikiRAG

OpenWikiRAG is a production-oriented enterprise knowledge platform combining WikiRAG, knowledge graphs, hybrid retrieval, citation-grounded generation, and voice interfaces.

Read the implementation and learning blueprint first:

- [`docs/OPENWIKIRAG_ENGINEERING_GUIDE.md`](docs/OPENWIKIRAG_ENGINEERING_GUIDE.md)
- [`metrics.md`](metrics.md) — verified metrics and resume evidence ledger
- [`learning-checkpoints.md`](learning-checkpoints.md) — deferred slice objectives, flows, and quizzes

Slice 6.4 adds version-safe evidence deduplication after RRF: repeated vector
projections of one tenant/version/chunk share a representative, retain their
explanations, and cannot inflate its score. Distinct versions and passages are
preserved; conflicting source provenance fails closed.

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
filter, and model isolation. The adapter deliberately leaves cross-leg policy
to the application; canonical text loading, reranking, HTTP search, quality
evaluation, and latency remain deferred.

Phase 6.3 adds application-owned Reciprocal Rank Fusion. It deduplicates exact
point identities across dense and sparse lists and calculates each fused score
as the sum of `1 / (k + source_rank)`, avoiding any assumption that cosine and
sparse-dot raw scores share a scale. Every result retains its per-leg rank, raw
score, and RRF contribution, while ties resolve by best source rank and stable
point id. The default `k` is 60, both `k` and the final result limit are bounded
configuration, and conflicting provenance for the same point fails closed.
This verifies fusion mechanics only; no Recall@k, MRR, nDCG, or latency
improvement is claimed before a labeled evaluation.

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

### Optional cross-encoder runtime (Slice 6.6)

Install with `uv sync --extra models --dev`. The base service does not download
model weights. Provision the pinned model before serving; a missing model is an
explicit error. Reproduce the opt-in CPU smoke (downloads public model weights):

```sh
OPENWIKIRAG_TEST_RERANKER_MODEL=cross-encoder/ms-marco-MiniLM-L2-v2 \\
OPENWIKIRAG_TEST_RERANKER_REVISION=1b5cd67b15209f24824c50370e0397743aa9b787 \\
uv run --extra models pytest apps/worker/tests/test_reranking.py -k real_cross
```

Scores are model-dependent relevance signals, not calibrated probabilities.
The [official CrossEncoder interface](https://sbert.net/docs/package_reference/cross_encoder/model.html)
documents the pairwise scoring, revision and local-files-only settings.

### Authenticated search (Slice 6.7)

`POST /api/v1/search` accepts `query`, `mode` (dense/sparse/hybrid),
`candidate_limit` (1–100), `limit` (1–40), `rerank` and optional document,
version, source type, language, chunk kind and pipeline filters. Tenant comes
only from the authenticated membership. Responses contain canonical chunk text,
immutable references and RRF contributions, with optional pairwise scores.
Only current versions with a succeeded ingestion job are eligible.

To enable the separately provisioned cross-encoder, set
`OPENWIKIRAG_RERANKER_MODEL` and `OPENWIKIRAG_RERANKER_REVISION` to the pinned
model/revision documented above. Missing weights produce 503, not a fallback.
The default retrieval embeddings remain deterministic hash baselines.

### Retrieval quality regression (Slice 6.8)

Run `uv run --no-sync python -m openwikirag.evaluation_cli` for dense/sparse/hybrid.
Add `--model cross-encoder/ms-marco-MiniLM-L2-v2 --revision 1b5cd67b15209f24824c50370e0397743aa9b787`
with the models extra and locally provisioned weights for real pairwise reranking.
Corpus/qrels: `evals/retrieval.json`; measured output:
`evals/retrieval-results-2026-09-04.json`. Eight authored queries/ten documents,
k=3, candidate window=10. This is not a held-out benchmark or production workload.
Sparse outperformed hybrid on this fixture; the hash-based dense model is not a
semantic encoder. Model replacement must be evaluated against this baseline.

### Canonical graph facts (Slice 7.1)

Apply Alembic migration `0010_knowledge_artifacts` before running the updated
worker. After vector projection, the worker records a bounded PostgreSQL graph
artifact before job success. Supported explicit source lines:

```text
[Aurora] --calls--> [Borealis]
[Borealis] --depends_on--> [Cygnus]
[Aurora] --uses--> [Cache]
[Service] --owned_by--> [Platform Team]
```

Entity normalization is deterministic; every fact retains its source quotation
and exact offsets. Arbitrary prose extraction and alias disambiguation are not
implemented. Documents without these annotations produce an empty graph artifact.

### Neo4j projection and rebuild (Slice 7.2)

Configure `OPENWIKIRAG_NEO4J_URI`, `OPENWIKIRAG_NEO4J_USER`,
`OPENWIKIRAG_NEO4J_PASSWORD` and `OPENWIKIRAG_NEO4J_DATABASE`.
`uv run python -m openwikirag.graph_cli --tenant TENANT_UUID` replays canonical
artifacts from PostgreSQL. To explicitly clear that tenant's derived projection
first, additionally pass `--clear --confirm-tenant TENANT_UUID` with the same UUID.
This does not delete source documents or PostgreSQL artifacts. Other tenants'
application graph nodes are outside the deletion target.

Real integration tests require a disposable Neo4j instance configured through
`OPENWIKIRAG_TEST_NEO4J_URI` and `OPENWIKIRAG_TEST_NEO4J_PASSWORD`.
Managed transaction retries follow the [official async driver contract](https://neo4j.com/docs/api/python-driver/current/async_api.html).

### Graph-aware search (Slice 7.3)

Set `OPENWIKIRAG_GRAPH_ENABLED=true` in API and worker configuration after
provisioning Neo4j. The worker projects canonical graph artifacts before success;
search accepts `graph_hops: 1` or `2`. Responses preserve ordinary `hits`, expose
`graph` evidence explanations, and merge unique canonical passages in `evidence`.
Graph expansion currently requires no optional document/language/etc. filters;
unsupported combinations are rejected, not silently widened. Disabled graph
configuration rejects explicit graph requests. Bounds:10seeds,20visited nodes,
10neighbors/node,20graph passages,10s deadline. Current-version and succeeded-job
checks precede each graph source read; stale paths do not extend the frontier.

### Phase 8.1: conservative local-model answers

The Ollama adapter selects exact quotations and returns explicit insufficient evidence when unsupported. Its model digest is checked before/after inference, not atomically pinned. The 6000-byte prompt/schema cap is not a tokenizer measurement. Real local qwen2.5:7b supported/unsupported smoke tests passed; arbitrary paraphrase grounding and production answer quality are not claimed.

References: [Ollama chat](https://docs.ollama.com/api/chat), [model digest listing](https://docs.ollama.com/api/tags).

### Phase8.2: durable answer stages

Nine LangGraph stages are inspectable and PostgreSQL-checkpointed. Operator setup: `uv run python -m openwikirag.checkpoint_cli --grant-role openwikirag_app` using the migration database URL. Requests use the application role and never run checkpoint DDL; SDK tables live in `ow_checkpoints`. Do not expose raw checkpoint storage to clients. External LangSmith tracing is explicitly disabled for answer execution. Source text in historical checkpoints requires the Phase9 retention path.

A fresh real PostgreSQL connection resumed a failed generation stage under a non-superuser role. Source invalidation blocks answer publication. Raw workflow calls must be serialized by the owning service (8.3); they are not a public concurrency-safe API. [LangGraph persistence](https://docs.langchain.com/oss/python/langgraph/persistence).
### Phase 8.3: protected answer API

`POST /api/v1/answers` creates a pending run. `POST /api/v1/answers/{id}/execute` executes/resumes it; `/stream` emits stage progress then a validated final answer. GET and trace are owner-only. Answers are checksum-protected and canonical citations are revalidated on every read. SSE intentionally omits raw model tokens. A PostgreSQL advisory lock prevents simultaneous writers; disconnects leave recoverable state.

Configure `OPENWIKIRAG_ANSWER_MODEL`, its expected digest and local base URL, then use the checkpoint setup CLI. Socket-level SSE load/disconnect behavior and broad content-table RLS are not claimed.
### Phase 9.1: private durable conversations

Conversation routes create/list/load tenant-and-user-private histories. Even a same-tenant admin cannot read another user's conversation. Answer requests may include `conversation_id`; the normalized question and final validated answer append atomically at immutable sequence numbers. Reads cap at 200 messages, verify checksums and revalidate assistant citations. PostgreSQL composite foreign keys enforce conversation/tenant/user lineage. History is context—not canonical enterprise knowledge.
