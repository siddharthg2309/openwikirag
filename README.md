# OpenWikiRAG

[![CI](https://github.com/siddharthg2309/openwikirag/actions/workflows/ci.yml/badge.svg)](https://github.com/siddharthg2309/openwikirag/actions/workflows/ci.yml)

OpenWikiRAG is a multi-tenant enterprise knowledge platform that converts
private documents into structured knowledge and citation-grounded answers.
It combines WikiRAG, hybrid retrieval, knowledge graphs, durable document
processing, and secure answer generation.

The current repository covers the non-voice knowledge and answer path. Voice
input/output is intentionally deferred, and production deployment guarantees
are not implied by the local reference deployment.

## Project overview

Users upload PDFs, DOCX files, Markdown, or text to a tenant-isolated API. The
platform validates the upload, stores the original file outside the database,
and processes it asynchronously. The worker extracts text and provenance,
builds an immutable WikiRAG page, creates hierarchical chunks, generates dense
and lexical search representations, and projects explicit relationships into a
knowledge graph.

For a question, OpenWikiRAG searches only authorized evidence, combines dense
and lexical retrieval, optionally expands graph context and reranks candidates,
then generates an answer whose citations are validated against the canonical
source before being returned.

## Architecture

```text
                                  DOCUMENT INGESTION

  User / tenant
       |
       v
  FastAPI API
  authentication | RBAC | tenant isolation | request correlation
       |
       +-----------------------> Object storage
       |                           raw files and immutable artifacts
       |
       +-----------------------> PostgreSQL
       |                           metadata, lineage, jobs, audits, state
       |                                      |
       |                                      v
       |                               Transactional outbox
       |                                      |
       |                                      v
       |                               Redis Streams
       |                                      |
       |                                      v
       |                               Durable worker
       |                                      |
       |       +----------------------+-------+--------+----------------+
       |       |                      |                |                |
       |       v                      v                v                v
       |  Extract + normalize   WikiRAG page    Hierarchical      Graph facts
       |  text + provenance     + metadata      chunks + vectors  + provenance
       |       |                      |                |                |
       |       +----------------------+----------------+----------------+
       |                                      |
       |                                      v
       |                         PostgreSQL canonical artifacts
       |                                      |
       |                         +------------+------------+
       |                         |                         |
       |                         v                         v
       |                      Qdrant                    Neo4j
       |                 dense + sparse index       graph projection
       |
       |
       |                              QUESTION ANSWERING
       |
       v
  User question
       |
       v
  Authenticated, tenant-scoped search
       |
       +---- dense retrieval
       +---- sparse / lexical retrieval
       +---- Reciprocal Rank Fusion
       +---- optional reranking and graph expansion
                                      |
                                      v
                         canonical source + citation validation
                                      |
                                      v
                         LangGraph answer workflow
                                      |
                         SSE progress + answer + audit
```

## End-to-end workflow

1. **Upload:** authenticate the user, verify tenant membership, validate file
   bytes and size, calculate a checksum, and create an immutable document
   version.
2. **Persist:** store raw bytes in object storage and commit PostgreSQL
   metadata, an ingestion job, and an outbox event in one transaction.
3. **Process:** Redis Streams delivers the job to a worker with leases,
   retries, reclaim handling, and dead-letter failure states.
4. **Understand:** extraction produces normalized text and source spans;
   WikiRAG creates structured page artifacts, metadata, chunks, embeddings, and
   explicit graph facts.
5. **Search:** the API applies tenant and document-version filters before
   Qdrant retrieval, combines dense and sparse rankings, and can add graph
   evidence or a cross-encoder reranker.
6. **Answer:** the workflow loads canonical evidence, validates citations,
   refuses unsupported questions, persists the result, and streams progress to
   the client.

## Start locally

```bash
./ops/start_project.sh
```

The launcher starts PostgreSQL, Redis, Qdrant, the migration gate, the API, and
the worker, then waits for API and worker readiness. Add `--all` when you also
need the optional Neo4j and MinIO containers. It preserves Compose volumes and
does not stop unrelated projects.

If a default host port is already in use, override that mapping for the
launcher, for example: `OPENWIKIRAG_REDIS_PORT=16379 ./ops/start_project.sh`.

## Key engineering properties

- **Tenant isolation:** JWT authentication, RBAC, membership checks, composite
  foreign keys, database RLS, and tenant-aware vector and graph queries.
- **Immutable lineage:** document versions, normalized artifacts, WikiRAG pages,
  chunks, vectors, and answers retain checksums and source identity.
- **Reliable processing:** transactional outbox, Redis Streams, at-least-once
  delivery, idempotent replay, leases, bounded retries, and dead-letter jobs.
- **Explainable retrieval:** source offsets, page ranges, provenance, per-leg
  scores, and Reciprocal Rank Fusion contributions are retained.
- **Grounded generation:** citations are revalidated against authorized,
  current canonical evidence before an answer is published.
- **Private conversations:** user-owned conversation history and explicit memory
  are isolated from enterprise evidence and support deletion and retention.
- **Recovery boundary:** PostgreSQL and immutable object bytes are backed up
  together; Qdrant and Neo4j are rebuildable projections rather than sources
  of truth.

## Technology stack

| Area | Technology | Purpose |
| --- | --- | --- |
| API | Python 3.13, FastAPI, Pydantic | Typed HTTP APIs, validation, authentication, and SSE |
| Database | PostgreSQL 16, SQLAlchemy 2, Alembic | Source of truth, transactions, constraints, RLS, and audit data |
| Authentication | JWT, refresh-token rotation, Argon2 | Identity and session security |
| Async processing | Redis 7 Streams | Outbox relay, durable jobs, retries, and recovery |
| Object storage | Filesystem-backed object-volume adapter | Raw uploads and canonical immutable artifacts in the current local deployment |
| Vector search | Qdrant 1.14.1 | Dense and sparse tenant-filtered retrieval |
| Dense embeddings | Deterministic hash baseline + optional Ollama `/api/embed` adapter | Server-owned model/digest and dimension checks; semantic quality requires a labeled evaluation |
| Knowledge graph | Neo4j 5.26 | Relationship projection and bounded graph expansion |
| Knowledge layer | WikiRAG artifacts and provenance contracts | Structured, reviewable document knowledge |
| WikiRAG generation | Deterministic baseline + optional Ollama JSON-schema adapter | Server-owned model/digest checks with citation-evidence validation |
| OCR runtime | Poppler `pdftoppm` and Tesseract 5.3 | Opt-in image-only PDF fallback with page-level provenance; English data is packaged in the application image |
| Answer workflow | LangGraph with PostgreSQL checkpoints | Durable and resumable answer execution |
| Local model | Ollama with `qwen2.5:7b` support | Local answer generation and model identity checks |
| Optional reranking | Sentence Transformers CrossEncoder | Pairwise candidate reranking |
| Quality | Pytest, Ruff, Mypy | Tests, linting, and strict type checking |
| Infrastructure | Docker Compose | Local PostgreSQL, Redis, Qdrant, API, and worker services; Neo4j/MinIO are optional boundaries |
