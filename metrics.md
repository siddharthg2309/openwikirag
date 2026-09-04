# OpenWikiRAG Metrics and Resume Evidence Ledger

This is the single source of truth for quantitative claims about OpenWikiRAG.
Update it after every verified implementation slice and before editing the
resume. A number may appear in a resume bullet only when this file records the
value, workload, date, command, and evidence location that produced it.

## Claim discipline

Use these labels consistently:

| Status | Meaning | Resume use |
| --- | --- | --- |
| `planned` | The metric or benchmark is designed but not run. | Never claim it. |
| `implemented` | Code exists, but the behavior or measurement is incomplete. | Do not present as proven. |
| `verified` | A repeatable test or experiment produced the value. | May be used with its workload and scope. |
| `resume-ready` | Verified, reproducible, explainable by the learner, and not misleading about scale. | Safe to use. |

Never invent throughput, latency, accuracy, scale, cost, uptime, or percentage
improvements. Passing tests are evidence of correctness for the tested cases;
they are not evidence of production scale or universal security.

## Current verified snapshot

Last updated: 2026-09-03
Current verified slice: Phase 5, Slice 5.9 — Durable worker vector projection.

| Area | Metric | Current value | Status | Evidence |
| --- | --- | ---: | --- | --- |
| Automated tests | Local test suite | 253 passed, 5 skipped | verified | `uv run pytest` |
| Automated tests | Qdrant-enabled full suite | 255 passed, 3 skipped | verified | `OPENWIKIRAG_TEST_QDRANT_URL=http://127.0.0.1:6333 uv run pytest` |
| Automated tests | PostgreSQL + Redis-enabled suite | 49 passed | verified | `OPENWIKIRAG_TEST_POSTGRES_URL=... OPENWIKIRAG_TEST_REDIS_URL=... uv run pytest` |
| Automated tests | Focused document-upload tests | 11 passed | verified | `uv run pytest apps/api/tests/test_documents.py` |
| Automated tests | Focused outbox/publisher tests | 5 passed | verified | `uv run pytest apps/api/tests/test_outbox.py` |
| Automated tests | Focused ingestion lifecycle tests | 6 passed | verified | `uv run pytest apps/api/tests/test_ingestion.py` |
| Automated tests | Focused job-progress API tests | 8 passed | verified | `uv run pytest apps/api/tests/test_jobs.py` |
| Automated tests | Focused worker lifecycle tests | 4 passed | verified | `uv run pytest apps/worker/tests/test_worker.py` |
| Automated tests | Focused extraction/provenance/quality/OCR-merge/DOCX tests | 19 passed | verified | `uv run pytest apps/worker/tests/test_extraction.py` |
| Automated tests | Focused deterministic metadata tests | 6 passed | verified | `uv run pytest apps/worker/tests/test_metadata.py` |
| Automated tests | Focused WikiRAG page-skeleton tests | 5 passed | verified | `uv run pytest apps/worker/tests/test_wiki.py` |
| Automated tests | Focused WikiRAG generation-boundary tests | 8 passed | verified | `uv run pytest apps/worker/tests/test_wiki_generation.py` |
| Automated tests | Focused WikiRAG generation-persistence tests | 9 passed | verified | `uv run pytest apps/worker/tests/test_wiki_generation_artifacts.py` |
| Automated tests | Focused WikiRAG page-read API tests | 7 passed | verified | `uv run pytest apps/api/tests/test_wiki_pages.py -k 'read or object or malformed or corrupted or storage'` |
| Automated tests | Focused WikiRAG review/RBAC tests | 27 passed | verified | `uv run pytest apps/api/tests/test_authorization.py apps/api/tests/test_wiki_pages.py` |
| Automated tests | Focused WikiRAG page-listing tests | 4 passed | verified | `uv run pytest apps/api/tests/test_wiki_pages.py -k 'list'` |
| Automated tests | Focused OCR-boundary tests | 19 passed | verified | `uv run pytest apps/worker/tests/test_ocr.py` |
| Automated tests | Focused normalized-artifact persistence tests | 8 passed | verified | `uv run pytest apps/worker/tests/test_normalized_artifacts.py` |
| Automated tests | Focused artifact-activation regression tests | 32 passed | verified | `uv run pytest apps/api/tests/test_ingestion.py apps/worker/tests/test_worker.py apps/worker/tests/test_normalized_artifacts.py` |
| Automated tests | Focused WikiRAG worker-pipeline tests | 5 passed | verified | `uv run pytest apps/api/tests/test_ingestion.py -k 'wiki_pipeline or wiki_output or wiki_provider or wiki_regeneration'` |
| Automated tests | Focused WikiRAG regeneration API tests | 4 passed | verified | `uv run pytest apps/api/tests/test_wiki_pages.py -k 'regeneration'` |
| Automated tests | Focused WikiRAG regeneration worker tests | 3 passed | verified | `uv run pytest apps/api/tests/test_ingestion.py -k 'wiki_regeneration or regeneration_event'` |
| Automated tests | Focused hierarchical chunking tests | 6 passed | verified | `uv run pytest apps/worker/tests/test_chunking.py -q` |
| Automated tests | Focused chunk-manifest persistence tests | 11 passed | verified | `uv run pytest apps/worker/tests/test_chunk_artifacts.py -q` |
| Automated tests | Focused dense-embedding contract tests | 8 passed | verified | `uv run pytest apps/worker/tests/test_embeddings.py -q` |
| Automated tests | Focused sparse-embedding contract tests | 9 passed | verified | `uv run pytest apps/worker/tests/test_sparse.py -q` |
| Automated tests | Focused embedding-batch orchestration tests | 8 passed | verified | `uv run pytest apps/worker/tests/test_embedding_batch.py -q` |
| Automated tests | Focused model-cache lifecycle tests | 10 passed | verified | `uv run pytest apps/worker/tests/test_model_cache.py -q` |
| Automated tests | Focused vector-index projection tests | 9 passed | verified | `uv run pytest apps/worker/tests/test_vector_index.py -q` |
| Automated tests | Focused live Qdrant adapter tests | 7 passed against Qdrant 1.14.1 | verified | `OPENWIKIRAG_TEST_QDRANT_URL=http://127.0.0.1:6333 uv run pytest apps/worker/tests/test_qdrant.py -q` |
| Automated tests | Focused vector-ingestion and durable-consumer tests | 28 passed against Qdrant 1.14.1 | verified | `OPENWIKIRAG_TEST_QDRANT_URL=http://127.0.0.1:6333 uv run pytest apps/worker/tests/test_vector_ingestion.py apps/api/tests/test_ingestion.py -q` |
| Integration | Redis Streams adapter and consumer groups | 2 real integration tests passed against Redis 7 | verified | `OPENWIKIRAG_TEST_REDIS_URL=... uv run pytest apps/api/tests/test_redis_integration.py` |
| Static quality | Ruff lint | 0 reported issues | verified | `uv run ruff check .` |
| Static quality | Mypy | 0 issues across 91 source files | verified | `uv run mypy` |
| Database | PostgreSQL integration engine | PostgreSQL 16 | verified | Fresh test instance and migration run |
| Database | Alembic schema head | `0008_scope_page_checksum` | verified | PostgreSQL 16 `uv run alembic upgrade head` |
| Database | Chunk-manifest migration | `0009_chunk_manifests` present in source; PostgreSQL application unverified | implemented | `migrations/versions/0009_chunk_manifests.py` |
| Database | Migration drift | Check blocked by unavailable configured PostgreSQL role | unverified | `uv run alembic check` |
| Database | WikiRAG metadata migrations | 2 immutable artifact tables plus 1 tenant-scoped constraint migration applied and drift-checked | verified | PostgreSQL 16 `uv run alembic upgrade head` and `uv run alembic check` |
| Security | Domain roles | 4 (`viewer`, `editor`, `admin`, `operator`) | verified | `src/openwikirag/security/authorization.py` and tests |
| Security | Domain permissions | 7 typed permissions | verified | `src/openwikirag/security/authorization.py` and tests |
| Upload | Supported document categories | 4 (PDF, DOCX, Markdown, UTF-8 text) | verified | `validate_upload()` and upload tests |
| Upload | Default maximum payload | 25 MiB / 26,214,400 bytes | implemented | `Settings.max_upload_bytes`; boundary tests use smaller limits |
| Authentication | Access-token TTL | 900 seconds / 15 minutes | implemented | Typed configuration and auth tests |
| Authentication | Refresh-token TTL | 30 days | implemented | Typed configuration and refresh tests |
| Deployment | Compose dependency services declared | 5 (PostgreSQL, Redis, Neo4j, Qdrant, MinIO) | implemented | `docker-compose.yml`; not all integrations are verified |
| Ingestion | Durable outbox events per committed upload | 1 event per upload transaction | verified | Upload test asserts event payload and rollback behavior |
| Ingestion | Event delivery model | At-least-once with stable event id | verified | Outbox publisher tests cover Redis failure and commit interruption |
| Ingestion | Default job lease | 60 seconds | implemented | `Settings.job_lease_seconds`; reclaim tests use a shorter lease |
| Ingestion | Default retry backoff | 5 seconds base, 300 seconds cap | implemented | `Settings` and lifecycle tests |
| Ingestion | Default maximum attempts | 3 attempts | implemented | `IngestionJob.max_attempts` and exhaustion test |
| Ingestion | Job-progress lifecycle states exposed | 5 (`pending`, `running`, `retryable`, `succeeded`, `dead_letter`) | verified | `GET /api/v1/jobs/{job_id}` tests |
| Security | Job progress cross-tenant negative cases | 2 (`foreign job`, `missing job`) | verified | `apps/api/tests/test_jobs.py` |
| Worker | Real finite-cycle smoke test | 1 successful `--once` run against PostgreSQL 16 + Redis 7 | verified | `uv run python -m apps.worker.app.main --once` with isolated dependency URLs |
| Worker | Real Markdown artifact-processing smoke test | 1 Markdown job: published 1, reclaimed 0, consumed 1, succeeded 1 | verified | Fresh PostgreSQL 16 + Redis 7 Slice 3.3 experiment |
| Worker | Real PDF artifact-processing smoke test | 1 two-page PDF job: published 1, reclaimed 0, consumed 1, succeeded 1 | verified | Fresh PostgreSQL 16 + Redis 7 Slice 3.4 experiment |
| Worker | Real PDF quality-classification smoke test | 1 three-page mixed PDF job: published 1, reclaimed 0, consumed 1, succeeded 1; quality `partial`, OCR handoff `true` | verified | Fresh PostgreSQL 16 + Redis 7 Slice 3.5 experiment |
| Extraction | Deterministic source types | 4 (UTF-8 text, Markdown, text-bearing digital PDF, DOCX) | verified | `DEFAULT_EXTRACTOR_REGISTRY` and extraction tests |
| Extraction | Provenance coverage invariant | 100% of normalized characters covered by contiguous spans | verified | `NormalizedDocument` validation and extraction tests |
| Extraction | PDF page provenance | 2 page-numbered spans over a two-page digital-PDF fixture | verified | `apps/worker/tests/test_extraction.py` and Slice 3.4 smoke check |
| Extraction | PDF quality states | 3 deterministic states (`sufficient`, `partial`, `empty`) with `needs_ocr` handoff | verified | `PdfTextQualityClassifier` tests and Slice 3.5 smoke check |
| Extraction | Mixed-PDF quality signal | 2 of 3 pages text-bearing; partial artifact retains page spans `[1, 3]` and sets `needs_ocr=true` | verified | Slice 3.5 worker smoke check |
| Extraction | Durable artifact identity | 1 immutable metadata row per document-version/parser identity | verified | Database unique constraint and persistence tests |
| Extraction | Active worker source types | 4 (UTF-8 text, Markdown, text-bearing digital PDF, DOCX) | verified | Concrete worker handler, composition tests, and artifact-consumer tests |
| Extraction | DOCX parser contract | 1 bounded standard-library OOXML parser with paragraph, heading, tab, break, and deleted-text semantics | verified | `apps/worker/tests/test_extraction.py`; no external parser dependency |
| Metadata | Deterministic metadata fields | 5 (`title`, `language`, `headings`, `dates`, `authors`) | verified | `DeterministicMetadataExtractor` and focused metadata tests |
| Metadata | Evidence-backed metadata collections | 3 (`headings`, `dates`, `authors`) plus inferred-title evidence | verified | Metadata models and normalized-range assertions |
| Metadata | Conservative language labels | 2 (`en`, `und`) | verified | Transparent marker heuristic; broad language detection is deferred |
| Metadata | Supported date formats | 2 (ISO and month-name dates) | verified | Valid-date normalization tests; ambiguous numeric dates are omitted |
| WikiRAG | Typed page-skeleton models | 4 (`WikiPage`, `WikiSection`, `WikiDefinition`, `WikiReference`) | verified | `src/openwikirag/application/wiki.py` and focused tests |
| WikiRAG | Deterministic section identity | 1 SHA-256-derived stable id per source heading | verified | `WikiPageBuilder` repeat-build and section tests |
| WikiRAG | Structured generation contract | 1 replaceable provider port; 1 immutable request; 1 immutable result; 3 generated field types | verified | `src/openwikirag/application/wiki_generation.py` and 8 focused tests |
| WikiRAG | Generation reproducibility identities | 6 captured identities (page, source, metadata, prompt, config, provider) | verified | `WikiGenerationResult` canonical contract and deterministic test |
| WikiRAG | Generation error classes | 3 typed classes (input, provider, invalid output) | verified | Boundary failure tests; future retry/dead-letter mapping remains deferred |
| WikiRAG | Persisted generation artifact | 1 canonical JSON object plus 1 PostgreSQL metadata row per generation identity | verified | `WikiGenerationArtifactService` and 9 focused persistence tests |
| WikiRAG | Self-contained page artifact | 1 canonical composite JSON object plus 1 PostgreSQL metadata row per generation artifact | verified | `WikiPageArtifactService`, migrations `0007`/`0008`, and 9 focused page-artifact tests |
| WikiRAG | Composite page layers | 2 immutable layers (`WikiPage` skeleton + `WikiGenerationResult`) | verified | `WikiGeneratedPage` checksum/lineage contract and focused tests |
| WikiRAG | Page read API | 1 authenticated tenant-scoped read endpoint with checksum/schema verification and success auditing | verified | `GET /api/v1/wiki/pages/{artifact_id}` and 7 focused API tests |
| WikiRAG | Review state machine | 3 states with 4 allowed directed transitions plus idempotent same-state retries | verified | `POST /api/v1/wiki/pages/{artifact_id}/review`, compare-and-set repository update, and focused tests |
| WikiRAG | Page listing API | 1 authenticated metadata-only listing endpoint with bounded pagination, status filtering, stable ordering, and success auditing | verified | `GET /api/v1/wiki/pages` and 4 focused listing tests |
| WikiRAG | Regeneration request API | 1 authenticated asynchronous regeneration endpoint using 1 durable job, 1 transactional-outbox event, and 1 success audit per request | verified | `POST /api/v1/wiki/pages/{artifact_id}/regenerate` and 4 focused API tests |
| WikiRAG | Regenerated artifact versioning | 1 new configuration-identified generation/page pair per changed regeneration configuration; prior page bytes/review metadata preserved; duplicate delivery reuses artifacts | verified | `WikiRegenerationHandler` and 3 focused worker tests |
| Chunking | Hierarchical chunk contract | 1 frozen chunk schema with 2 chunk kinds, 5 explicit budget/overlap controls, heading-aware parent grouping, exact offsets/page provenance, and stable SHA-256 ids | verified | `HierarchicalChunker` and 6 focused tests |
| Chunking | Immutable manifest persistence | 1 canonical JSON manifest plus 1 tenant/version/source-scoped PostgreSQL metadata row per chunking configuration; byte-verified reuse and compensating cleanup | verified | `ChunkManifestService`, migration `0009_chunk_manifests`, and 11 focused persistence tests; PostgreSQL migration execution unverified |
| Embeddings | Dense embedding contract | 1 provider port, 1 frozen request, 1 frozen result schema, 4 reproducibility identities (input/provider/model/config), exact configured vector dimensions, finite values, and cosine unit-norm validation | verified | `DeterministicHashEmbeddingProvider` and 8 focused tests; semantic quality and external provider execution deferred |
| Embeddings | Sparse lexical contract | 1 provider port, 1 frozen request, 1 frozen result schema, bounded hashed indices, positive sublinear term-frequency weights, sorted unique geometry, and exact input/provider/model/configuration identity | verified | `DeterministicHashSparseEmbeddingProvider` and 9 focused tests; corpus IDF, fusion, semantic quality, and Qdrant projection deferred |
| Embeddings | Bounded batch orchestration | 1 generic provider port, 1 frozen limit configuration, 2 boundedness controls (batch size/concurrency), streaming input consumption, stable output ordering, indexed typed failures, and cancellation cleanup | verified | `EmbeddingBatcher` and 8 focused tests; provider-native batching, retries, cache, and Qdrant projection deferred |
| Embeddings | In-process model/provider cache | 1 frozen cache key with 4 identity dimensions, 1 frozen capacity configuration, bounded LRU storage, single-flight loading, failed-load suppression, eviction/shutdown cleanup, and cancellation isolation | verified | `EmbeddingModelCache` and 10 focused tests; result caching, distributed cache, TTL/invalidation, and Qdrant projection deferred |
| Vector indexing | Tenant-scoped hybrid projection contract | 1 provider-neutral index port, 1 immutable point schema, 1 collection-geometry identity, 2 named vectors (dense/sparse), 1 explicit tenant filter, and create/reuse/conflict upsert semantics | verified | `VectorPointRequest`, `VectorPoint`, `TenantVectorFilter`, `InMemoryVectorIndex`, and 9 focused tests |
| Vector indexing | Live Qdrant projection schema | 1 pinned client (`qdrant-client` 1.14.3), 1 named dense cosine vector, 1 named sparse vector, 10 typed payload indexes, and deterministic UUID point ids | verified | `QdrantVectorIndex`, Qdrant 1.14.1 integration, and 7 focused real-service tests |
| Vector indexing | Live Qdrant tenant-safe point lifecycle | Create/reuse/immutable-conflict behavior, validated vector/payload round-trip, and 1 foreign-tenant negative read path | verified | Qdrant-enabled full suite: 255 passed, 3 skipped |
| Vector indexing | Durable worker projection | 1 immutable chunk manifest plus 1 Qdrant point per parent/child chunk; bounded dense/sparse batches; replay reports created/reused counts and completes before job success/Redis ack | verified | 7 focused service tests, 21 consumer tests, and 255 Qdrant-enabled full-suite tests |
| WikiRAG | Page generation status | `draft` skeleton; artifact metadata transitions through `needs_review`/`approved`; content remains immutable | verified | Page-builder, persistence, review service, and API tests; no real LLM is configured |
| Extraction | OCR fallback contract | 2 replaceable ports, 2 native CLI adapters, 1 bounded sequential orchestrator | verified | `apps/worker/tests/test_ocr.py`; native runtime is not claimed active |
| Extraction | OCR request bounds | 50 pages maximum and 30 seconds per page by default | implemented | `OcrOptions`; no production workload benchmark yet |
| Extraction | OCR-enriched merge proof | 1 mixed three-page fixture; only the missing page was OCR-routed and recovered | verified | Fake OCR extraction and consumer tests |
| Worker | OCR-enabled consumer path | 1 mixed PDF job succeeded with fake OCR; acknowledged after artifact/job commit | verified | `apps/api/tests/test_ingestion.py` |
| Worker | DOCX artifact-processing consumer path | 1 SQLite-backed DOCX job succeeded with artifact commit before acknowledgement | verified | `apps/api/tests/test_ingestion.py` |
| Worker | Deterministic WikiRAG pipeline activation | 1 Markdown job produced 1 normalized artifact, 1 generation artifact, and 1 self-contained page artifact; job succeeded before acknowledgement | verified | `uv run pytest apps/api/tests/test_ingestion.py -k 'wiki_pipeline'` |
| Worker | WikiRAG artifact replay reuse | 1 simulated post-artifact crash replay reused exactly 1 normalized, 1 generation, and 1 page artifact per identity | verified | `uv run pytest apps/api/tests/test_ingestion.py -k 'reuses_artifacts'` |
| Worker | WikiRAG failure classification | 2 focused cases: invalid provider output dead-lettered; provider exception remained retryable and unacknowledged | verified | `uv run pytest apps/api/tests/test_ingestion.py -k 'wiki_output or wiki_provider'` |
| Worker | WikiRAG regeneration routing | 1 same-tenant regeneration job produced a new draft page identity; 2 invalid/mismatched event cases dead-lettered before page creation | verified | `uv run pytest apps/api/tests/test_ingestion.py -k 'wiki_regeneration or regeneration_event'` |
| Integration | Native OCR runtime availability | Poppler render verified; Tesseract unavailable on verification host | unverified | Real generated-PDF Poppler check; `command -v tesseract` absent |

The current suite count includes the PostgreSQL integration test only when its
environment variable is supplied. The normal local command skips that test.
The existing Starlette/httpx deprecation warning is not counted as a failure.

## Numbers that are not measured yet

These are intentionally blank until the corresponding subsystem exists and a
repeatable benchmark is run:

| Category | Metric to measure | Current value | Why it matters |
| --- | --- | ---: | --- |
| Ingestion | Upload API p50/p95 latency | — | Proves request-path responsiveness. |
| Ingestion | Documents processed per minute | — | Proves worker throughput under a stated corpus. |
| Ingestion | Queue wait p50/p95 | — | Separates delivery delay from processing time. |
| Ingestion | Retryable-failure recovery rate | — | Proves recoverability after dependency/worker failure. |
| Ingestion | Duplicate-delivery deduplication rate | — | Proves idempotent job handling. |
| Extraction | Extraction success rate | — | Measures parser behavior over fixtures. |
| Extraction | OCR fallback rate and p95 duration | — | Shows when OCR is needed and its cost. |
| Retrieval | Recall@5 / Recall@10 | — | Measures whether relevant evidence is found. |
| Retrieval | MRR and nDCG@10 | — | Measures ranking quality, not just existence. |
| Retrieval | Dense vs. sparse vs. hybrid delta | — | Supports an ablation-based design claim. |
| Retrieval | Reranker p50/p95 latency | — | Quantifies precision/latency trade-off. |
| Graph | Graph-expansion recall delta | — | Proves whether graph context adds value. |
| Answers | Citation validity rate | — | Every citation must resolve to authorized evidence. |
| Answers | Citation coverage rate | — | Required claims must have supporting evidence. |
| Answers | Unsupported-question refusal accuracy | — | Measures safe insufficient-evidence behavior. |
| Answers | End-to-end p50/p95 latency | — | Must be broken down by retrieval, graph, rerank, model, and persistence. |
| Answers | Model tokens and cost per answer | — | Makes provider cost explainable. |
| Security | Cross-tenant negative-test count | — | Expand as documents, jobs, vectors, graph, and answers arrive. |
| Reliability | Worker-crash recovery time | — | Measures lease reclaim and replay behavior. |
| Reliability | Dead-letter rate | — | Shows permanent failure volume under a stated workload. |
| Performance | Sustained concurrent search/answer capacity | — | Must include hardware, corpus, model, and dependency versions. |

## Measurement record template

Copy this block for each benchmark or meaningful quantitative experiment.

```md
### <metric name>

- Date:
- Status: planned | implemented | verified | resume-ready
- Code version / commit:
- Environment: OS, CPU/GPU, RAM, Python, dependency versions
- Corpus or workload: tenant count, document count, bytes, chunks, query count, concurrency
- Warm-up and repetitions:
- Metric definition:
- Result: p50 / p95 / p99 / rate / count / score
- Baseline:
- Change under test:
- Command:
- Raw output or report:
- Interpretation:
- Caveats:
```

For latency, record at least p50 and p95, the number of repetitions, and the
workload. For retrieval, record the dataset version, relevant IDs, k value, and
whether the result is dense-only, sparse-only, hybrid, reranked, or graph-aware.
For cost, record model/provider, token counts, currency, and pricing date.

## Benchmark plan by project phase

### Phase 1 — Identity, tenants, and RBAC

Already verified:

- 4 roles and 6 typed permissions.
- 35 local tests and 36 PostgreSQL-enabled tests.
- PostgreSQL 16 migration/RLS integration under a non-superuser runtime role.
- Access-token validation, refresh rotation, replay rejection, audit writes, and
  negative tenant/membership paths.

Do not claim production authentication scale. Production OIDC/JWKS and Redis
rate limiting remain deferred.

### Phase 2 — Upload and durable job lifecycle

Record after the outbox and Redis worker slices:

- upload p50/p95 at 1 MiB, 10 MiB, and the configured maximum payload;
- documents/minute and bytes/minute at stated worker concurrency;
- queue wait and processing p50/p95;
- worker crash -> lease reclaim time;
- duplicate delivery -> number of canonical versions before/after replay;
- retryable failure -> eventual success rate and attempt count;
- dead-letter count and operator-visible failure rate;
- storage/database failure cleanup rate.

### Phase 3 — Extraction, normalization, and OCR

Use a fixed fixture set containing PDF, Markdown, text, DOCX, malformed,
encrypted, empty, huge, and image-only files. Record:

- extraction success/failure counts by file type;
- normalized artifact checksum determinism across repeated runs;
- page/section/character provenance coverage;
- OCR fallback percentage;
- OCR confidence distribution and p50/p95 processing duration;
- parser memory and CPU under the fixture workload.

### Phases 4–5 — WikiRAG artifacts, chunks, and embeddings

Record:

- metadata/schema validation success rate;
- provenance coverage for summaries, definitions, sections, and references;
- chunks per document and token/character distribution;
- duplicate chunk rate after rerun;
- embedding throughput and batch size;
- vector-index build duration and storage size;
- model/pipeline version attached to every artifact.

### Phases 6–8 — Retrieval and grounded answers

Use one versioned JSONL evaluation dataset and compare these ablations:

1. lexical-only;
2. dense-only;
3. dense + sparse with RRF;
4. hybrid + cross-encoder reranker;
5. hybrid + reranker + graph expansion.

For each system, record Recall@k, Precision@k, MRR, nDCG, graph recall,
candidate count, reranker latency, citation validity, citation coverage,
groundedness, refusal correctness, p50/p95 latency, and model cost. Never report
an improvement without naming the baseline, dataset, k, workload, and change.

### Phases 9–12 — Conversations, voice, observability, and deployment

Record:

- conversation authorization negative tests and retention/deletion outcomes;
- STT word/segment confidence, p50/p95 transcription latency, and failure rate;
- TTS p50/p95 latency and failure rate;
- trace coverage from API request through worker, retrieval, model, and answer;
- load-test throughput, error rate, and p95 under fixed concurrency;
- recovery-test duration for PostgreSQL/object storage restore and projection rebuild;
- CI duration and pass rate across a stated sample of runs.

## Resume-safe bullet bank

Use only bullets whose bracketed fields have been replaced with verified values
from this file. Until later benchmarks exist, use the implementation-focused
versions rather than pretending to have scale results.

### Current implementation-stage bullets

- Built a tenant-isolated enterprise knowledge platform foundation with FastAPI, PostgreSQL, SQLAlchemy, Alembic, centralized RBAC, JWT authentication, Argon2id password hashing, rotating refresh tokens, and PostgreSQL RLS; verified with **36 tests** including a PostgreSQL integration suite.

- Implemented a secure document-intake boundary supporting **4 document categories** with bounded multipart reads, content sniffing, SHA-256 checksums, filename sanitization, immutable version metadata, and pending ingestion-job registration; verified with **11 focused upload tests**.

- Separated raw document bytes from transactional metadata using a replaceable object-storage port and PostgreSQL canonical records; tested object-write failure, metadata rollback, orphan cleanup, viewer denial, and membershipless-tenant denial.

- Built a recoverable ingestion-job lifecycle with Redis consumer groups, lease-based stale-message reclaim, bounded exponential retry backoff, commit-before-ack success handling, terminal duplicate suppression, and dead-letter routing; verified with **6 lifecycle tests** and **2 real Redis 7 integration tests**.

- Added an authenticated tenant-scoped job-progress API backed by PostgreSQL canonical state, coarse lifecycle progress mapping, safe cross-tenant `404` behavior, and audited reads; verified with **8 focused API tests** and **1 PostgreSQL 16 integration test**.

- Wired a runnable asynchronous worker around the transactional outbox and Redis consumer-group services with ordered relay/reclaim/consume cycles, idle/error backoff, graceful signal shutdown, finite `--once` smoke mode, and deterministic artifact extraction for **4 source types**; verified with **4 worker tests** and fresh PostgreSQL 16 + Redis 7 Markdown/PDF smoke runs.

- Established a deterministic extraction contract for **4 source types** (UTF-8 text, Markdown, text-bearing digital PDF, and DOCX), preserving source/normalized character offsets, Markdown/DOCX heading paths, PDF page provenance, and parser-versioned SHA-256 artifacts; verified with **19 extraction/provenance/quality/OCR/DOCX tests**.

- Added page-provenanced digital-PDF extraction with `pypdf`, typed malformed/encrypted/textless failure handling, and immutable artifact integration; verified with **21 ingestion/worker/artifact regression tests** plus a real two-page PostgreSQL 16 + Redis 7 worker smoke.

- Added explainable PDF page-coverage classification with **3 quality states** and a deterministic `needs_ocr` handoff, preserving usable text from mixed PDFs while deferring OCR execution; verified with a real three-page PostgreSQL 16 + Redis 7 worker smoke.

- Added a bounded page-level OCR seam with replaceable renderer/engine ports, Poppler/Tesseract CLI adapters, temporary-file cleanup, structured no-shell execution, and typed timeout/provider failures; verified with **19 focused tests**. Native Tesseract execution and worker activation remain unverified.

- Activated opt-in PDF OCR for pages without usable digital text, preserving digital/OCR provenance and immutable `pypdf-ocr` artifact identity; verified with **6 new fake-provider extraction, consumer, and composition tests**. Native Tesseract execution remains unverified.

- Activated deterministic DOCX ingestion with a bounded standard-library OOXML parser, paragraph/run extraction, heading-derived section paths, tab/break preservation, deleted-text exclusion, and permanent malformed/oversized failure handling; verified with **4 focused parser tests**, **1 consumer persistence test**, and **111 local tests** overall. No external dependency or migration was added.

- Established a deterministic metadata contract for **5 fields** with frozen Pydantic schemas, title precedence, conservative `en`/`und` classification, valid ISO/month-name date normalization, labeled author splitting, normalized-text evidence ranges, and canonical checksums; verified with **6 focused metadata tests** and **117 local tests**. LLM generation and metadata persistence remain later slices.

- Built a deterministic WikiRAG page skeleton with **4 typed Pydantic models**, source/metadata checksum propagation, stable heading-derived section ids, draft review status, and evidence-bearing contracts for future definitions/references; verified with **5 focused WikiRAG tests** and **122 local tests**.

- Added a provider-neutral structured WikiRAG generation boundary with **1 provider port**, **1 immutable request**, **1 immutable result**, **3 generated field types**, and **6 reproducibility identities** (page, source, metadata, prompt, config, provider); verified with **8 focused generation tests** and **130 local tests**. Real model invocation, retries, and human review remain deferred.

- Persisted validated WikiRAG generation as immutable canonical JSON outside PostgreSQL with **1 tenant-scoped metadata table**, **1 normalized-artifact lineage link**, idempotent byte verification, immutable conflict detection, and compensating cleanup; verified with **9 focused persistence tests**, **139 local tests**, migration `0006`, and **1 PostgreSQL integration test**. Document-table RLS, worker activation, and live-provider orchestration remain deferred.

- Packaged the deterministic WikiRAG skeleton and validated generated fields as **1 self-contained immutable page artifact** linked to its generation result, with four-way checksum lineage, tenant-scoped lookup, idempotent byte verification, immutable corruption rejection, and compensating cleanup; verified with **9 focused page-artifact tests**, **148 local tests**, migrations `0007`/`0008`, and **1 PostgreSQL integration test**. API reads, worker activation, review transitions, live-provider orchestration, and document-table RLS remain deferred.

- Exposed **1 authenticated tenant-scoped WikiRAG page-read endpoint** that verifies object checksums and composite schema before returning page/generation data, maps foreign and missing artifacts to indistinguishable `404`s, and audits successful reads; verified with **7 focused API tests**, **155 local tests**, and **1 PostgreSQL integration test**. Listing, review transitions, regeneration, worker activation, and document-table RLS remain deferred.

- Added a **3-state WikiRAG review workflow** with **1 dedicated RBAC permission**, **4 explicit transitions**, idempotent same-state retries, tenant-scoped compare-and-set concurrency protection, immutable object preservation, and atomic success auditing; verified with **9 review-focused tests**, **164 local tests**, and **1 PostgreSQL integration test**. Review history, comments, assignment, regeneration, worker activation, and document-table RLS remain deferred.

- Added **1 authenticated metadata-only WikiRAG page-listing endpoint** with **2 bounded pagination controls**, server-side review filtering, deterministic ordering, `has_more` continuation, tenant exclusion, and success auditing without object-storage reads; verified with **4 focused listing tests**, **168 local tests**, and **1 PostgreSQL integration test**. Cursor pagination, review history, comments, assignment, regeneration, worker activation, and document-table RLS remain deferred.

- Activated the deterministic WikiRAG worker pipeline across **5 ordered stages** (normalization, metadata/page construction, structured generation, generation-artifact persistence, and page-artifact persistence) with **3 immutable artifact outputs**, a replaceable provider port, named progress steps, and permanent-versus-retryable failure mapping; verified with **4 focused pipeline tests**, **172 local tests**, and duplicate replay reuse. Live LLM orchestration, provider-specific retries, regeneration, and document-table RLS remain deferred.

- Added **1 authenticated asynchronous WikiRAG regeneration endpoint** for editor/admin roles, backed by **1 durable job**, **1 transactional-outbox event**, and **1 success audit** per request; worker routing validates the source page's tenant/version lineage and creates a new configuration-identified draft artifact while preserving the prior page bytes and review status; verified with **4 API tests**, **3 worker tests**, **179 local tests**, and duplicate delivery reuse. Live provider/model selection, request-key deduplication, and PostgreSQL/Redis regeneration smoke remain deferred.

- Added a deterministic hierarchical chunking boundary with **2 chunk kinds** (parent and child), **5 explicit budget/overlap controls**, top-level heading grouping, bounded child overlap, exact normalized offsets, page-range provenance, and stable SHA-256 ids; verified with **6 focused chunking tests** and **185 local tests**. Model-specific tokenizers, chunk persistence, embeddings, Qdrant, retrieval, and reranking remain deferred.

- Persisted deterministic chunks as **1 immutable canonical JSON manifest** plus **1 tenant/version/source-scoped metadata row** with schema/config/content checksums and parent/child counts; repeated runs verify and reuse bytes, while metadata failures trigger compensating cleanup; verified with **11 focused persistence tests** and **196 local tests**. Migration `0009_chunk_manifests` is added, but PostgreSQL/Alembic execution remains unverified; embeddings and Qdrant remain deferred.

- Defined **1 provider-neutral dense-embedding port**, **1 immutable request**, and **1 immutable result schema** with exact input/configuration/model identity, fixed dimensions, finite cosine-normalized vectors, and deterministic canonical bytes; verified with **8 focused tests** and **204 local tests**. The local feature-hashing adapter proves reproducibility and shape only; live model quality, sparse vectors, batching, and Qdrant remain deferred.

- Defined **1 provider-neutral sparse-embedding port**, **1 immutable request**, and **1 immutable result schema** with Unicode case-folded lexical terms, bounded feature-hashed indices, positive sublinear term-frequency weights, sorted unique sparse geometry, and exact input/provider/model/configuration identity; verified with **9 focused tests** and **213 local tests**. The local hash adapter proves exact-term representation shape and replayability only; corpus IDF, collision/semantic-quality analysis, dense/sparse fusion, batching, persistence, and Qdrant remain deferred.

- Added **1 provider-neutral bounded batch orchestrator** with **2 explicit limits** (maximum batch size and in-flight concurrency), streaming request consumption, stable input-order results, indexed provider/output failures, fail-fast active-batch cancellation, later-batch suppression, and caller-cancellation cleanup; verified with **8 focused tests** and **221 local tests**. Provider-native batching, retries/rate limiting, model cache, persistence, Qdrant projection, and production throughput remain deferred.

- Added **1 bounded in-process model/provider cache** with **4 identity dimensions** (representation, provider, model, configuration), single-flight concurrent loading, LRU eviction, async resource cleanup, failed-load suppression, shutdown cancellation, and waiter-cancellation isolation; verified with **10 focused tests** and **231 local tests**. This is loaded-instance caching only; result caching, distributed cache, TTL/invalidation, retries, persistence, Qdrant projection, and cache performance measurements remain deferred.

- Defined **1 provider-neutral tenant-scoped hybrid vector-index port** with **1 immutable point schema**, **2 named vectors** (dense and sparse), **1 collection geometry identity**, **1 explicit tenant payload filter**, provenance-only payloads, deterministic point ids, and create/reuse/immutable-conflict upsert behavior; verified with **9 focused tests** and **240 local tests**.

- Activated **1 asynchronous live Qdrant projection adapter** using **qdrant-client 1.14.3** against Qdrant 1.14.1, with **1 named dense cosine vector**, **1 named sparse vector**, **10 typed payload indexes**, deterministic UUID point ids, schema conflict detection, validated round-trips, same-checksum reuse, immutable conflicts, and foreign-tenant isolation; verified with **7 focused adapter tests** and **255 Qdrant-enabled full-suite tests**. Live search, fusion, retrieval quality, and performance measurements remain deferred.

- Extended the durable ingestion worker through **4 immutable artifact layers** (normalized text, WikiRAG generation, page, and chunk manifest), bounded dense/sparse embedding, and **1 provenance-only Qdrant point per parent/child chunk** before job success and Redis acknowledgement; verified with **28 focused tests** and **255 Qdrant-enabled full-suite tests**, including partial-write replay and unacknowledged dependency failure. Semantic quality, search, fusion, reranking, and throughput remain unmeasured.

### Future measured bullets

- Designed and implemented an idempotent asynchronous ingestion pipeline with Redis Streams, transactional outbox events, leases, retries, and dead-letter handling across **[N] documents / [N] jobs**, achieving **[N] documents/minute** at **[concurrency]** with **[p95]** processing latency.

- Built WikiRAG pages, hierarchical chunks, dense/sparse Qdrant indexes, and graph-aware retrieval; improved **[Recall@k/MRR/nDCG]** from **[baseline]** to **[result]** on **[dataset size]** evaluation queries at **[p95 latency]**.

- Added citation validation and grounded-answer refusal behavior, achieving **[citation validity]%** citation validity and **[citation coverage]%** claim coverage across **[N]** labeled questions.

- Added tenant isolation across PostgreSQL, object storage, Qdrant, and Neo4j, with **[N]** cross-tenant negative tests and **[0]** unauthorized data exposures in the tested scenarios.

## ATS keyword inventory

Keep the truthful terms that match the implemented repository:

`Python`, `FastAPI`, `Pydantic`, `SQLAlchemy`, `PostgreSQL`, `Alembic`, `Row-Level Security`, `RBAC`, `JWT`, `Argon2id`, `REST API`, `asyncio`, `Docker Compose`, `Redis Streams`, `MinIO`, `Qdrant`, `Neo4j`, `WikiRAG`, `RAG`, `hybrid retrieval`, `dense retrieval`, `sparse retrieval`, `reranking`, `knowledge graph`, `citation grounding`, `idempotency`, `transactional outbox`, `dead-letter queue`, `OCR`, `OpenTelemetry`, `Prometheus`, `React`, `TypeScript`, `CI/CD`, `pytest`, `Ruff`, `mypy`.

Do not list a technology as production-integrated merely because it appears in
Compose or the blueprint. Mark it implemented, verified, or planned based on
the repository evidence.

## Update checklist

Before adding a number to the resume:

- [ ] The value is recorded in the current snapshot or a dated measurement record.
- [ ] The command and raw report are reproducible.
- [ ] The workload, hardware, versions, and dataset are named.
- [ ] A baseline is named when claiming improvement.
- [ ] The number describes the tested scope, not an unmeasured production claim.
- [ ] The learner can explain how the metric was calculated and what could make it misleading.
- [ ] The corresponding implementation and test evidence still exists.
