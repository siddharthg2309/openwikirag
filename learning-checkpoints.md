# OpenWikiRAG — Pending Learning Checkpoints

Updated: 2026-09-04. Learning status: unanswered unless explicitly recorded below.

## Agreement and how to use this notebook

The learner explicitly authorized sequential implementation through Phase 9 without pausing for Q&A. This changes the learning gate, not the engineering verification gates. Each slice still gets a bounded objective, design, tests, decision, flow, and sequential commit on `main`. No unanswered checkpoint is proof of independent understanding or resume readiness.

Slices 1.1 and 2.1 were answered and corrected in `prepnotes.md`; they are excluded. Slice 1.2 was partially answered, but its remaining questions are included. Historical explanations below describe the slice when introduced; later slices may activate formerly deferred integrations. Current implementation status belongs in `implementation.md`, and quantitative evidence in `metrics.md`.

For each entry: read the objective, trace the flow in code, answer the five questions without AI narration, then compare against the design and tests. Add your answers below the entry; do not delete unresolved questions. New slices through Phase 9 are appended as they are verified.

## Slice 1.2 — PostgreSQL identity and membership persistence

Status: partially answered; remaining concepts deferred.

Objective: persist tenants, users, and tenant memberships so future authentication and authorization use canonical identity data.

Explanation: PostgreSQL is already the project’s system of record. Relational constraints prevent impossible membership states, while a repository keeps application services independent of SQLAlchemy query details. Async sessions fit the API’s I/O model without making the domain layer database-aware.

Input/output: An application service or test provides an `AsyncSession` to `IdentityRepository`. Create operations receive a tenant name, normalized email/provider subject, or tenant/user UUIDs plus a typed `Role`; they return flushed SQLAlchemy models. Membership lookup receives user and tenant UUIDs and returns a typed `Role` or `None`.

```text
application service / test
  -> create_database_engine(database_url)
  -> create_session_factory(engine)
  -> open AsyncSession
  -> IdentityRepository.create_tenant/create_user/add_membership
  -> normalize email and pre-check known uniqueness conflicts
  -> add SQLAlchemy model to session
  -> flush to enforce database constraints
  -> caller commits the transaction
  -> get_role_for_tenant(user_id, tenant_id)
  -> select membership role and convert to Role
```

Failure branches: invalid database configuration, missing tenant/user, duplicate membership, foreign-key violation, closed/failed session, and cross-tenant membership lookup.

Trade-off: This introduces migration and connection-pool concerns. SQLite may be used only for isolated repository tests; PostgreSQL behavior, especially Row-Level Security, must be verified separately. JWT/OIDC, password hashing, and RLS are intentionally deferred to later slices.

References: `implementation.md` Slice 1.2, `decisions.md` D-003, `flow.md` F-003.

Quiz topics from the original checkpoint: learner partially answered and explicitly overrode the remaining checkpoint. Source-of-truth correction and the SQLite/PostgreSQL verification boundary were recorded in `prepnotes.md`; async session lifetime, uniqueness constraints, transaction failure behavior, and why RLS is a later slice remain deferred.

1. Concept: what problem does “PostgreSQL identity and membership persistence” solve, and why is its boundary necessary?
2. Execution: trace the entry point through its functions and data stores to the output above. Who owns the transaction or state?
3. Failure: choose one failure branch above; explain what is persisted, what is returned, and whether retry is safe.
4. Trade-off: defend the choice above against one rejected alternative. When would you change it?
5. Interview: explain this slice in two sentences, naming its proof and one limitation. What can you honestly claim today?

Learner answers: pending.

## Slice 6.5 — Canonical evidence resolution

Status: unanswered; pause overridden through Phase 9.
Objective: obtain trustworthy source text for reranking and answers.
Explanation: a search hit is a pointer, not evidence truth. The resolver joins
manifest, normalized artifact, version, and document rows inside the tenant,
checks source/pipeline/current-version lineage, reads the database-owned object
key, verifies checksum and schema/counts, then finds and validates the chunk.
Verified objects are cached only inside one request; authorization is rechecked.
Missing/stale evidence is distinct from corruption and dependency outages.
Trade-off: this reads whole bounded manifests; a chunk-row index may be needed
at scale. Byte limits protect parsing after storage reads, not network allocation.
References: D-045, F-044, evidence.py. 11 focused canonical-evidence tests passed. Full Qdrant-enabled suite: 314 passed, 3 external-service skips. Ruff, mypy (99 files), lock and diff checks passed.

1. Why is a Qdrant payload insufficient proof of source text?
2. Trace a hit through the four-table ownership check and object verification.
3. What happens if the document becomes historical after its object is cached?
4. Compare manifest reads with storing indexed canonical chunk rows.
5. Explain this boundary in two interview sentences, including what remains unverified.

Learner answers: pending.

## Slice 6.4 — Version-safe evidence deduplication

Status: unanswered; pause overridden through Phase 9.

Objective: stop duplicate index projections from consuming evidence slots.
Explanation: point ids identify vector configurations, while tenant/version/chunk
identifies evidence. `deduplicate_evidence` revalidates RRF input, groups that
identity, checks source fields, and keeps the first (best-ranked) candidate.
Its RRF score and contributions do not change. Lower-ranked copies remain in
an explanation group. Distinct versions, siblings and parent passages survive.
No database or object state changes. Conflicting source metadata fails closed.
Trade-off: this avoids unsafe merging but does not remove semantic overlap or
choose the current document version. References: D-044, F-043, deduplication.py.

1. Why are point identity and evidence identity different?
2. Trace a three-point input where two points describe the same chunk.
3. Why reject a different content checksum instead of silently keeping the winner?
4. Why preserve different versions and siblings rather than one hit per document?
5. Give a two-sentence interview explanation and state what the tests do not prove.

Learner answers: pending.

## Slice 1.3 — JWT authentication and current identity

Status: unanswered; pause explicitly overridden.

Objective: validate a signed access token and combine its authenticated subject with a database-backed tenant membership to create the principal consumed by centralized authorization.

Explanation: Signature verification establishes that the token was issued by the configured signer and that it is currently valid. It does not prove current membership or role state. Looking up membership keeps authorization tied to the canonical identity store and makes role changes visible without trusting arbitrary client input.

Input/output: Input: an optional HTTP `Authorization: Bearer <JWT>` header. The JWT must contain `sub`, `tenant_id`, `iss`, `aud`, `iat`, and `exp`. Output: a `MeResponse` containing the internal user id, explicit tenant id, and current database-backed role, or a stable HTTP authentication/authorization error.

```text
HTTP GET /api/v1/me
  -> FastAPI Depends(get_current_principal)
  -> get_session() opens one AsyncSession for the request
  -> HTTPBearer extracts the bearer token
  -> JWTAuthenticator.verify() validates HS256/signature/issuer/audience/time/claims
  -> IdentityRepository.get_identity_context(subject, tenant_id)
  -> join user + membership + tenant
  -> reject missing membership or inactive user/tenant
  -> construct Principal from database role
  -> me() returns MeResponse
  -> dependency closes the AsyncSession
```

Failure branches: absent bearer credentials, invalid signature/claims, expired token, unknown subject, missing tenant membership, inactive user, inactive tenant, and database lookup failure.

Trade-off: Every protected request pays for a membership lookup unless a carefully scoped cache is introduced later. The current HS256 verifier is a local development seam, not proof of production OIDC integration. The learner's session-lifetime, uniqueness, transaction-failure, and RLS Q&A concepts are deferred rather than treated as understood.

References: `implementation.md` Slice 1.3, `decisions.md` D-004, `flow.md` F-004.

Quiz topics from the original checkpoint: learner explicitly overrode the Q&A gate to complete Phase 1. JWT verification versus authorization, the exact request flow, stale/malicious role claims, failure paths, and the database-lookup trade-off remain deferred learning topics.

1. Concept: what problem does “JWT authentication and current identity” solve, and why is its boundary necessary?
2. Execution: trace the entry point through its functions and data stores to the output above. Who owns the transaction or state?
3. Failure: choose one failure branch above; explain what is persisted, what is returned, and whether retry is safe.
4. Trade-off: defend the choice above against one rejected alternative. When would you change it?
5. Interview: explain this slice in two sentences, naming its proof and one limitation. What can you honestly claim today?

Learner answers: pending.

## Slice 1.4 — Local authentication lifecycle and audit events

Status: unanswered; pause explicitly overridden.

Objective: provide safe local development registration/login and refresh rotation while recording security-relevant operations.

Explanation: The override honors the learner's stated sequencing goal while preserving an honest evidence trail. It changes the collaboration gate, not the security requirements or the definition of verified behavior.

Input/output: Registration receives a tenant name, email, and password. The password grant receives email, password, and explicit tenant id. The refresh grant receives an opaque refresh token. Outputs are a registration identity or a short-lived access token plus opaque refresh token.

```text
register/token request
  -> LocalAuthService
  -> normalize/load identity
  -> Argon2id hash or verify password
  -> resolve active tenant membership
  -> issue access JWT
  -> generate opaque refresh secret
  -> store only refresh SHA-256 hash
  -> write audit event
  -> commit one transaction
```

Failure branches: Identify one invalid-input, authorization, dependency, or transaction failure in the referenced flow.

Trade-off: The implementation effort is larger than one normal learning slice, and the learner must revisit the deferred concepts before claiming independent understanding. Phase 1 remains incomplete until real PostgreSQL/RLS evidence exists.

References: `implementation.md` Slice 1.4, `decisions.md` D-005, `flow.md` F-005.

Quiz topics from the original checkpoint: provide safe local development registration/login and refresh rotation while recording security-relevant operations.

1. Concept: what problem does “Local authentication lifecycle and audit events” solve, and why is its boundary necessary?
2. Execution: trace the entry point through its functions and data stores to the output above. Who owns the transaction or state?
3. Failure: choose one failure branch above; explain what is persisted, what is returned, and whether retry is safe.
4. Trade-off: defend the choice above against one rejected alternative. When would you change it?
5. Interview: explain this slice in two sentences, naming its proof and one limitation. What can you honestly claim today?

Learner answers: pending.

## Slice 1.5 — Migration-managed PostgreSQL schema and RLS

Status: unanswered; pause explicitly overridden.

Objective: make identity/audit schema reproducible and enforce tenant filters in the database for applicable tables.

Explanation: The override honors the learner's stated sequencing goal while preserving an honest evidence trail. It changes the collaboration gate, not the security requirements or the definition of verified behavior.

```text
Alembic migration/admin role
  -> create tenants/users/memberships/refresh_tokens/audit_events
  -> enable and FORCE RLS on memberships and audit_events
  -> API transaction begins
  -> set_config('app.tenant_id', tenant_id, true)
  -> membership/audit queries see only that tenant
  -> transaction ends and local setting disappears
```

Failure branches: Identify one invalid-input, authorization, dependency, or transaction failure in the referenced flow.

Trade-off: The implementation effort is larger than one normal learning slice, and the learner must revisit the deferred concepts before claiming independent understanding. Phase 1 remains incomplete until real PostgreSQL/RLS evidence exists.

References: `implementation.md` Slice 1.5, `decisions.md` D-005, `flow.md` F-006.

Quiz topics from the original checkpoint: make identity/audit schema reproducible and enforce tenant filters in the database for applicable tables.

1. Concept: what problem does “Migration-managed PostgreSQL schema and RLS” solve, and why is its boundary necessary?
2. Execution: trace the entry point through its functions and data stores to the output above. Who owns the transaction or state?
3. Failure: choose one failure branch above; explain what is persisted, what is returned, and whether retry is safe.
4. Trade-off: defend the choice above against one rejected alternative. When would you change it?
5. Interview: explain this slice in two sentences, naming its proof and one limitation. What can you honestly claim today?

Learner answers: pending.

## Slice 1.6 — Phase security gate and handoff

Status: unanswered; pause explicitly overridden.

Objective: prove Phase 1 behavior at the API and database boundaries and prepare the Phase 2 contract.

Explanation: The override honors the learner's stated sequencing goal while preserving an honest evidence trail. It changes the collaboration gate, not the security requirements or the definition of verified behavior.

```text
local auth/OIDC subject seam
  -> access JWT validation
  -> current user + tenant membership
  -> centralized RBAC decision
  -> transaction-local PostgreSQL RLS
  -> audit event for protected success path
```

Failure branches: Identify one invalid-input, authorization, dependency, or transaction failure in the referenced flow.

Trade-off: The implementation effort is larger than one normal learning slice, and the learner must revisit the deferred concepts before claiming independent understanding. Phase 1 remains incomplete until real PostgreSQL/RLS evidence exists.

References: `implementation.md` Slice 1.6, `decisions.md` D-005, `flow.md` F-007.

Quiz topics from the original checkpoint: prove Phase 1 behavior at the API and database boundaries and prepare the Phase 2 contract.

1. Concept: what problem does “Phase security gate and handoff” solve, and why is its boundary necessary?
2. Execution: trace the entry point through its functions and data stores to the output above. Who owns the transaction or state?
3. Failure: choose one failure branch above; explain what is persisted, what is returned, and whether retry is safe.
4. Trade-off: defend the choice above against one rejected alternative. When would you change it?
5. Interview: explain this slice in two sentences, naming its proof and one limitation. What can you honestly claim today?

Learner answers: pending.

## Slice 2.2 — Transactional outbox and Redis Streams publisher

Status: unanswered; pause explicitly overridden.

Objective: make the database-to-queue handoff durable so a committed document intake cannot silently lose its ingestion event when Redis is unavailable.

Explanation: The outbox makes the database commit the source of truth for whether work exists. Redis becomes a rebuildable delivery projection: if it is unavailable, the outbox row remains available for retry; if acknowledgement bookkeeping fails after `XADD`, duplicate delivery is detectable instead of silently losing the event. Keeping Redis behind a small port makes application tests deterministic and preserves the option to replace the transport.

Input/output: The upload path receives the already-authenticated document intake request. It writes a `document.ingestion.requested` outbox event in the same database transaction as the document, version, and pending job. A publisher invocation receives a database session, a `StreamPublisher`, a stream name, and a positive batch limit. It returns successfully published event ids and Redis stream message ids; failed events remain pending with an attempt count and stable error code.

```text
DocumentUploadService.upload
  -> store raw object
  -> flush document/version/pending ingestion_job
  -> OutboxRepository.create_event
  -> record success audit
  -> commit PostgreSQL transaction

OutboxPublisherService.publish_pending(limit)
  -> list unpublished outbox rows ordered by occurred_at/id
  -> capture event ids and end the read transaction
  -> reload each event
  -> StreamPublisher.publish
       -> RedisStreamPublisher.xadd(stream, event_id, event_type,
          tenant_id, aggregate_id, serialized payload)
  -> mark published_at
  -> commit database state
  -> return stream message ids
```

Failure branches: upload transaction rollback, Redis unavailable, malformed event payload, repeated publisher execution, and successful Redis write followed by database commit failure.

Trade-off: The publisher needs a poll/retry loop and outbox retention/cleanup policy. A crash after Redis accepts an event but before `published_at` is committed can produce a duplicate stream message; event ids are included so the future consumer can deduplicate. This slice does not claim consumer acknowledgements, leases, retry backoff, dead letters, or real worker recovery.

References: `implementation.md` Slice 2.2, `decisions.md` D-010, `flow.md` F-009.

Quiz topics from the original checkpoint: overridden by learner; the outbox/source-of-truth, publish/mark ordering, at-least-once duplicate path, and consumer deduplication concepts remain deferred and are not marked understood.

1. Concept: what problem does “Transactional outbox and Redis Streams publisher” solve, and why is its boundary necessary?
2. Execution: trace the entry point through its functions and data stores to the output above. Who owns the transaction or state?
3. Failure: choose one failure branch above; explain what is persisted, what is returned, and whether retry is safe.
4. Trade-off: defend the choice above against one rejected alternative. When would you change it?
5. Interview: explain this slice in two sentences, naming its proof and one limitation. What can you honestly claim today?

Learner answers: pending.

## Slice 2.3 — Consumer groups and recoverable ingestion jobs

Status: unanswered; pause explicitly overridden.

Objective: consume ingestion events with Redis consumer-group semantics and make job execution recoverable after worker crashes, transient failures, and permanent failures.

Explanation: PostgreSQL owns the durable state transition and lease, while Redis owns delivery and pending-message ownership. Commit-before-ack means a worker crash after the database commit produces a harmless redelivery; the terminal-state check avoids repeating the handler. Leaving retryable messages pending allows Redis reclaim to recover them after a crash, while `available_at` prevents processing before backoff expires. Dead-lettering makes permanent failures visible instead of retrying indefinitely.

Input/output: The worker setup creates a Redis consumer group for the ingestion stream. A normal poll reads new messages for one consumer; a reclaim poll transfers stale pending messages after the configured lease interval. Each message contains the outbox event id, event type, tenant id, job aggregate id, and serialized payload. The consumer returns a count of messages observed; durable outcomes are stored on `ingestion_jobs` and Redis messages are acknowledged only after the appropriate outcome is persisted.

```text
consumer setup
  -> Redis XGROUP CREATE (idempotent BUSYGROUP handling)

consume_once / reclaim_once
  -> XREADGROUP for new messages or XAUTOCLAIM for stale pending messages
  -> parse and validate event UUIDs, type, aggregate id, and JSON payload
  -> JobRepository.claim(job_id, tenant_id)
       -> row lock where supported
       -> reject missing/active/not-due/terminal jobs
       -> reject exhausted attempts into dead-letter state
       -> otherwise increment attempts and set running lease
  -> invoke injected ingestion handler
  -> success: mark succeeded -> commit -> XACK
  -> retryable failure: mark retryable + bounded backoff -> commit -> leave pending
  -> permanent failure: mark dead_letter -> publish diagnostic DLQ copy -> commit -> XACK
```

Failure branches: missing/malformed job payload, active lease, expired lease, retryable handler error, permanent handler error, Redis read/ack/DLQ failure, database commit interruption, and duplicate terminal delivery.

Trade-off: The worker must periodically reclaim stale pending messages and must distinguish retryable from permanent errors. Redis consumer groups can redeliver messages, and the same event may be observed more than once; the database job state is the deduplication gate. This slice does not yet make extraction side effects idempotent, expose job progress over HTTP, or supervise a production worker loop.

References: `implementation.md` Slice 2.3, `decisions.md` D-011, `flow.md` F-010.

Quiz topics from the original checkpoint: pending; explain consumer groups, leases, commit-before-ack, retryable versus permanent failure, exponential backoff, dead letters, and terminal-state duplicate suppression.

1. Concept: what problem does “Consumer groups and recoverable ingestion jobs” solve, and why is its boundary necessary?
2. Execution: trace the entry point through its functions and data stores to the output above. Who owns the transaction or state?
3. Failure: choose one failure branch above; explain what is persisted, what is returned, and whether retry is safe.
4. Trade-off: defend the choice above against one rejected alternative. When would you change it?
5. Interview: explain this slice in two sentences, naming its proof and one limitation. What can you honestly claim today?

Learner answers: pending.

## Slice 2.4 — Tenant-scoped ingestion-job progress visibility

Status: unanswered; pause explicitly overridden.

Objective: expose durable ingestion-job state through an authenticated API so clients can safely poll progress after an upload returns `202`.

Explanation: The compound tenant predicate prevents a caller from using a job UUID to probe another tenant. PostgreSQL is the source of truth for status, attempts, leases, timestamps, and errors; reading Redis would expose transport state and could disagree with a committed database transition. A coarse progress mapping is honest for this slice because extraction steps do not yet report percentages.

Input/output: The caller supplies a UUID path parameter and a bearer token. The endpoint returns a `JobProgressResponse` containing the canonical job id, document-version id, lifecycle status, current step, coarse progress percentage, attempts, timestamps, lease information, and safe error metadata. It returns `404` for a missing job or a job outside the principal's tenant scope.

```text
HTTP GET /api/v1/jobs/{job_id}
  -> get_current_principal()
       -> verify bearer token and current membership
       -> set PostgreSQL tenant context when using PostgreSQL
  -> IngestionJobProgressService.get()
       -> JobRepository.get_by_id(job_id, principal.tenant_id)
       -> AuthorizationService.require(READ_DOCUMENTS, resource tenant)
       -> _job_progress(IngestionJob)
  -> AuditRepository.record(job.read, success)
  -> session.commit()
  -> JobProgressResponse
```

Failure branches: missing/invalid bearer token, inactive identity, missing job, cross-tenant job id, unsupported future status, database lookup failure, and audit-commit failure.

Trade-off: Clients can poll a durable state, but polling is less immediate than push notifications and the percentage is lifecycle-level rather than step-level. A successful read adds an audit write and commit to the request. Worker wiring, SSE, and detailed extraction progress remain future slices.

References: `implementation.md` Slice 2.4, `decisions.md` D-012, `flow.md` F-011.

Quiz topics from the original checkpoint: after verification, explain why PostgreSQL is queried instead of Redis, how tenant-safe 404 behavior works, what the progress percentage means, the audit/commit failure path, and the endpoint’s interview explanation.

1. Concept: what problem does “Tenant-scoped ingestion-job progress visibility” solve, and why is its boundary necessary?
2. Execution: trace the entry point through its functions and data stores to the output above. Who owns the transaction or state?
3. Failure: choose one failure branch above; explain what is persisted, what is returned, and whether retry is safe.
4. Trade-off: defend the choice above against one rejected alternative. When would you change it?
5. Interview: explain this slice in two sentences, naming its proof and one limitation. What can you honestly claim today?

Learner answers: pending.

## Slice 2.5 — Runnable worker process wiring

Status: unanswered; pause explicitly overridden.

Objective: compose the transactional outbox publisher and recoverable ingestion consumer into a runnable worker process with bounded polling, error recovery, finite smoke mode, and graceful shutdown.

Explanation: The loop centralizes lifecycle and resource ownership without moving business rules out of the application services. The ordering minimizes queue latency for new uploads while giving crashed work priority. A finite mode makes local smoke checks deterministic. The deferred handler prevents a runnable scaffold from claiming that extraction succeeded.

Input/output: The process reads typed settings and optional `--once` mode. In loop mode it accepts SIGINT/SIGTERM through an asyncio stop event. Each cycle returns counts for outbox events published, stale messages reclaimed, and new messages consumed; the CLI emits structured lifecycle logs and exits only after resource cleanup.

```text
worker CLI
  -> parse --once
  -> load settings + configure logging
  -> create PostgreSQL engine/session factory
  -> create RedisStreamPublisher
  -> construct OutboxPublisherService
  -> construct IngestionConsumerService + DeferredIngestionHandler
  -> ensure Redis consumer group
  -> WorkerLoop.run_once()
       -> publish_pending(outbox_batch_size)
       -> reclaim_once()
       -> consume_once()
  -> if loop mode: idle/error wait and repeat
  -> stop/cancel
  -> close Redis, session context, and database engine
```

Failure branches: PostgreSQL/Redis startup failure, outbox or consumer dependency error, failed database rollback, cancellation during a cycle, signal-triggered shutdown, idle polling, and accidental success before extraction exists.

Trade-off: One process handles both relay and ingestion, which is simple for the modular-monolith phase but does not independently scale job types. The worker uses polling and a shared session, so production supervision, per-stage concurrency, health metrics, and connection recycling remain future hardening work.

References: `implementation.md` Slice 2.5, `decisions.md` D-013, `flow.md` F-012.

Quiz topics from the original checkpoint: after verification, explain composition-root ownership, cycle ordering, idle/error backoff, graceful shutdown, why the deferred handler fails instead of succeeding, and the interview explanation.

1. Concept: what problem does “Runnable worker process wiring” solve, and why is its boundary necessary?
2. Execution: trace the entry point through its functions and data stores to the output above. Who owns the transaction or state?
3. Failure: choose one failure branch above; explain what is persisted, what is returned, and whether retry is safe.
4. Trade-off: defend the choice above against one rejected alternative. When would you change it?
5. Interview: explain this slice in two sentences, naming its proof and one limitation. What can you honestly claim today?

Learner answers: pending.

## Slice 3.1 — Deterministic text and Markdown extraction contract

Status: unanswered; pause explicitly overridden.

Objective: define one deterministic, provenance-preserving normalized-document contract and implement it for UTF-8 plain text and Markdown before adding complex parsers.

Explanation: Newline normalization removes platform-specific variation without changing words, punctuation, case, or Unicode semantics. Including parser version and spans in the checksum detects meaningful parser-output changes and makes later artifact persistence/idempotency explicit. Building deterministic formats first proves the output boundary before complex PDF/DOCX/OCR dependencies are added.

Input/output: An application service will eventually provide the validated document version's `source_type` and raw bytes. This slice accepts `text` and `markdown` only. It returns an immutable `NormalizedDocument` with parser identity, canonical text, contiguous provenance spans, deterministic JSON bytes, and a SHA-256 checksum.

```text
source_type + raw bytes
  -> ExtractorRegistry selects one DocumentExtractor
  -> UTF-8-SIG decode
  -> normalize CRLF/CR line endings to LF
  -> create one source/normalized offset span per line
  -> for Markdown: update section path from ATX heading lines
  -> validate contiguous full-text span coverage
  -> canonical JSON (stable key order + compact separators)
  -> SHA-256 checksum over parser identity, text, and provenance
```

Failure branches: unknown source type, duplicate extractor registration, invalid UTF-8, blank content after canonicalization, invalid/noncontiguous spans, and parser-version mismatch assumptions.

Trade-off: This slice supports two deterministic source types only and records character offsets in decoded text rather than byte offsets. It intentionally defers PDF page extraction, DOCX structure, quality classification, OCR confidence, persistence, and worker integration. Later parsers must honor the same span coverage invariant and increment their parser version for material changes.

References: `implementation.md` Slice 3.1, `decisions.md` D-014, `flow.md` F-013.

Quiz topics from the original checkpoint: explain why canonicalization is limited, how normalized/source offsets differ, why the parser version is included in the checksum, how Markdown section paths are produced, and the interview explanation.

1. Concept: what problem does “Deterministic text and Markdown extraction contract” solve, and why is its boundary necessary?
2. Execution: trace the entry point through its functions and data stores to the output above. Who owns the transaction or state?
3. Failure: choose one failure branch above; explain what is persisted, what is returned, and whether retry is safe.
4. Trade-off: defend the choice above against one rejected alternative. When would you change it?
5. Interview: explain this slice in two sentences, naming its proof and one limitation. What can you honestly claim today?

Learner answers: pending.

## Slice 3.2 — Canonical normalized-artifact persistence

Status: unanswered; pause explicitly overridden.

Objective: store the canonical JSON emitted by the text/Markdown extraction contract outside PostgreSQL while committing tenant-scoped immutable artifact metadata in PostgreSQL.

Explanation: Object storage is suited to potentially large text/provenance artifacts, while PostgreSQL provides the transactional identity, foreign keys, uniqueness, and tenant filter required for traceability. A deterministic object key based on tenant, document version, parser identity, and checksum makes identical outputs addressable and prevents cross-tenant collisions. The immutability conflict prevents parser changes from silently rewriting historical artifacts.

Input/output: A future ingestion handler supplies the authenticated job's `tenant_id` and a `document_version_id`. This slice does not activate that handler; it verifies the underlying service directly. The output is a `PersistedNormalizedArtifact` that contains immutable metadata and whether an existing equivalent artifact was reused. Raw bytes and canonical JSON never enter a PostgreSQL text/blob column.

```text
tenant id + document-version id
  -> NormalizedArtifactRepository.get_document_version(tenant predicate)
  -> ObjectStorage.get(raw source key)
  -> ExtractorRegistry.extract(source type, raw bytes)
  -> NormalizedDocument.canonical_bytes() + SHA-256
  -> deterministic tenant/version/parser/checksum artifact key
  -> repository lookup by version + parser name + parser version
  -> [existing equal] construct result -> rollback read transaction -> reuse
  -> [none] ObjectStorage.put(canonical JSON)
           -> repository.create(metadata) -> PostgreSQL commit -> new result
```

Failure branches: missing/foreign document version, raw-object read failure, unsupported extraction type, invalid extraction output, normalized-object write failure, metadata conflict, metadata commit failure, cleanup failure, and rerun idempotency.

Trade-off: The object write and metadata commit remain a two-resource operation, so the service attempts cleanup on a failed metadata write. Generic persistence errors report whether cleanup failed; a uniqueness-conflict cleanup remains best effort and is not yet operator-observable. The unique constraint and post-conflict winner lookup provide an implemented recovery branch, but concurrency stress testing and reconciliation remain future hardening. PDF/DOCX/OCR, job transitions, and production S3/MinIO behavior are out of scope.

References: `implementation.md` Slice 3.2, `decisions.md` D-015, `flow.md` F-014.

Quiz topics from the original checkpoint: explain why canonical JSON lives outside PostgreSQL, what makes reruns idempotent, why checksum/parser conflicts are rejected, how object/DB failure handling works, and the interview explanation.

1. Concept: what problem does “Canonical normalized-artifact persistence” solve, and why is its boundary necessary?
2. Execution: trace the entry point through its functions and data stores to the output above. Who owns the transaction or state?
3. Failure: choose one failure branch above; explain what is persisted, what is returned, and whether retry is safe.
4. Trade-off: defend the choice above against one rejected alternative. When would you change it?
5. Interview: explain this slice in two sentences, naming its proof and one limitation. What can you honestly claim today?

Learner answers: pending.

## Slice 3.3 — Worker activation for deterministic artifacts

Status: unanswered; pause explicitly overridden.

Objective: replace the worker's intentional deferred handler with a real, tenant-safe handler that persists normalized text/Markdown artifacts and lets the existing consumer complete the job lifecycle.

Explanation: The stream envelope is transport input and cannot be the authority for choosing a source version. The claimed job has already been filtered by tenant, locked, and associated with one immutable source version. Artifact persistence commits before the consumer marks the job succeeded. A crash in between produces a duplicate delivery, which is safe because the artifact service reuses identical immutable output.

Input/output: The outbox publisher emits a committed `document.ingestion.requested` event. Redis delivers it to a consumer group. The consumer validates the envelope and claims its `IngestionJob` using the event tenant/job ID. The handler receives that claimed database object plus the payload; it returns no business result. Its successful side effect is one durable normalized artifact, after which the consumer records job success and acknowledges Redis.

```text
outbox event -> Redis Streams consumer group
  -> parse event envelope
  -> JobRepository.claim(job id, tenant id, lease)
  -> NormalizedArtifactIngestionHandler.handle(claimed job, payload)
  -> job.tenant_id + job.document_version_id only
  -> NormalizedArtifactService.persist()
  -> raw object -> extractor -> canonical artifact object -> artifact metadata commit
  -> refresh claimed job after handler transaction
  -> JobRepository.mark_succeeded() -> PostgreSQL commit -> Redis XACK
```

Failure branches: malformed stream payload remains rejected before a claim; foreign/missing source version, unsupported type, invalid encoding, artifact conflict, raw/output storage outage, metadata persistence failure, duplicate delivery after artifact commit, and worker startup/composition failure.

Trade-off: One input document can be persisted successfully while its job is still running until the following job-state commit; duplicate execution is therefore expected and handled by immutable artifact reuse. Current job progress remains coarse. Only local object storage and text/Markdown are activated; external storage, complex parsers, OCR, and concurrency stress testing remain later work.

References: `implementation.md` Slice 3.3, `decisions.md` D-016, `flow.md` F-015.

Quiz topics from the original checkpoint: explain why the claimed job—not the stream payload—is canonical, why artifact and job completion have separate commits, which errors are permanent versus retryable, how duplicate delivery recovers after a crash, and the interview explanation.

1. Concept: what problem does “Worker activation for deterministic artifacts” solve, and why is its boundary necessary?
2. Execution: trace the entry point through its functions and data stores to the output above. Who owns the transaction or state?
3. Failure: choose one failure branch above; explain what is persisted, what is returned, and whether retry is safe.
4. Trade-off: defend the choice above against one rejected alternative. When would you change it?
5. Interview: explain this slice in two sentences, naming its proof and one limitation. What can you honestly claim today?

Learner answers: pending.

## Slice 3.4 — Deterministic digital-PDF extraction with page provenance

Status: unanswered; pause explicitly overridden.

Objective: make text-bearing PDFs a supported worker source type by extracting deterministic page text and retaining page-number provenance in the normalized artifact contract.

Explanation: `pypdf` provides `PdfReader` and per-page `extract_text()` without a native system runtime. Page boundaries are sufficient provenance for the first digital PDF path. The official documentation also makes clear that text extraction may be empty for image-only scans and can have significant memory cost, so OCR and resource limits remain explicit later concerns rather than hidden claims.

Input/output: The existing worker receives a claimed `IngestionJob` whose document version has `source_type="pdf"`. `NormalizedArtifactService` reads the immutable raw object bytes and sends them to the registry. The registry selects `PdfExtractor`, which returns one `NormalizedDocument` containing canonical text and page-numbered `SourceSpan` records. The service serializes that document to object storage and commits only its immutable metadata row in PostgreSQL; the consumer then completes the job and acknowledges Redis.

```text
claimed IngestionJob (tenant_id, document_version_id)
  -> NormalizedArtifactService.persist()
  -> raw PDF object read
  -> ExtractorRegistry.extract(source_type="pdf", data)
  -> PdfExtractor.extract()
  -> PdfReader(BytesIO(data), strict=True)
  -> each page.extract_text()
  -> per-page CRLF/CR -> LF canonicalization
  -> page-local spans rebased into one contiguous normalized document
  -> canonical JSON/checksum/object key
  -> immutable artifact metadata commit
  -> job succeeded commit
  -> Redis XACK
```

Failure branches: malformed PDF structure, encrypted PDF, page-level parse failure, no extracted text, unbounded parser work noted for future guardrails, worker permanent failure mapping, and duplicate artifact reuse.

Trade-off: This handles digital PDFs only; scans or text-empty pages fail for later quality classification/OCR. Page-local extracted-text offsets are logical parser coordinates, not PDF byte offsets or rendered glyph coordinates. The dependency is intentionally isolated behind the existing extractor interface.

References: `implementation.md` Slice 3.4, `decisions.md` D-017, `flow.md` F-016.

Quiz topics from the original checkpoint: explain why PDF source offsets are page-local extracted-text positions, why PDF parsing is permanent versus retryable, why digital extraction cannot serve a scanned PDF, how page-boundary normalization stays deterministic, and the interview explanation.

1. Concept: what problem does “Deterministic digital-PDF extraction with page provenance” solve, and why is its boundary necessary?
2. Execution: trace the entry point through its functions and data stores to the output above. Who owns the transaction or state?
3. Failure: choose one failure branch above; explain what is persisted, what is returned, and whether retry is safe.
4. Trade-off: defend the choice above against one rejected alternative. When would you change it?
5. Interview: explain this slice in two sentences, naming its proof and one limitation. What can you honestly claim today?

Learner answers: pending.

## Slice 3.5 — Deterministic PDF text-quality classification

Status: unanswered; pause explicitly overridden.

Objective: attach an explainable quality signal to PDF normalized artifacts so later OCR work can distinguish complete digital text from partially or entirely unextractable pages.

Explanation: Page coverage is a small, explainable signal that directly reflects the failure mode this slice can observe. It avoids untestable claims about semantic quality, font quality, OCR confidence, or layout. Keeping it in the artifact payload makes the future OCR decision reproducible and gives later stages the original assessment alongside the extracted text.

Input/output: The existing PDF extraction flow collects one normalized text observation for each source page. `PdfTextQualityClassifier` receives those ordered page observations and returns `PdfTextQualityAssessment`. A digital PDF with text on every page is `sufficient`; one with text on only some pages is `partial`; one with no text is `empty`. The assessment is embedded in a PDF normalized artifact when a usable text result exists. An empty PDF still raises the typed no-text error because OCR is not implemented in this slice.

```text
PdfExtractor.extract()
  -> PdfReader pages
  -> extract/canonicalize each page
  -> preserve all page observations, including empty pages
  -> PdfTextQualityClassifier.classify()
  -> validate counts/status/reason codes
  -> if usable text: spans + quality assessment -> NormalizedDocument
  -> if no usable text: NoTextExtractedError -> no artifact
```

Failure branches: inconsistent page/count inputs, zero-page or empty PDFs, mixed pages, deterministic checksum changes from quality metadata, and future OCR handoff without falsely claiming OCR completion.

Trade-off: The classifier detects missing text coverage, not semantic correctness or garbage extraction. `needs_ocr=true` is a handoff signal, not proof that OCR will improve the document. The PDF parser version changes so the new quality field receives a new immutable artifact identity; no PostgreSQL migration is needed because the field lives in the object artifact.

References: `implementation.md` Slice 3.5, `decisions.md` D-018, `flow.md` F-017.

Quiz topics from the original checkpoint: explain the sufficient/partial/empty states, why the quality signal is metadata rather than OCR, how partial text remains usable, which invariants prevent misleading counts, and the interview explanation.

1. Concept: what problem does “Deterministic PDF text-quality classification” solve, and why is its boundary necessary?
2. Execution: trace the entry point through its functions and data stores to the output above. Who owns the transaction or state?
3. Failure: choose one failure branch above; explain what is persisted, what is returned, and whether retry is safe.
4. Trade-off: defend the choice above against one rejected alternative. When would you change it?
5. Interview: explain this slice in two sentences, naming its proof and one limitation. What can you honestly claim today?

Learner answers: pending.

## Slice 3.6 — Replaceable OCR fallback boundary

Status: unanswered; pause explicitly overridden.

Objective: define a bounded, testable OCR fallback contract for scanned or partially scanned PDF pages without coupling extraction to one native OCR runtime.

Explanation: Ports make the orchestration deterministic and unit-testable without requiring native OCR tools or hiding provider behavior behind a Python wrapper. The CLI adapters isolate native CPU work and make executable/version checks explicit. Sequential bounded processing avoids unbounded CPU work in the worker and keeps the first implementation easy to reason about; later concurrency can be introduced only with measured resource limits.

Input/output: This slice is called by a future PDF activation stage with the original PDF bytes and the 1-based pages selected for OCR by a quality/router policy. It returns ordered `OcrPageResult` values containing page number, OCR text, and `source="ocr"`. It does not write a normalized artifact, update a job, or acknowledge a stream message.

```text
future PDF OCR stage
  -> PdfOcrFallback.extract(pdf_data, page_numbers)
  -> validate non-empty bytes, sorted unique positive pages, and max_pages
  -> for each selected page, in order
       -> PdfPageRenderer.render(page, dpi, per-page timeout)
       -> OcrEngine.recognize(PNG, language, PSM, per-page timeout)
       -> append OcrPageResult(page, text)
  -> return tuple of page results
```

Failure branches: empty PDF input, invalid page selection/options, page render failure, missing native binary, per-page timeout, non-zero OCR exit, empty OCR text, and bounded work exhaustion. Automatic routing remains a later worker slice.

Trade-off: The first adapter path requires Poppler and Tesseract binaries and is not portable without runtime setup. The OCR result is page-level text only; it does not yet include confidence, glyph coordinates, layout, or source offsets, and it is not worker-active. Each page receives its own timeout, while the maximum page count bounds total provider calls. A later activation slice must define how OCR text is merged with existing digital spans, how empty OCR pages affect quality, and how retryable versus permanent provider failures update jobs.

References: `implementation.md` Slice 3.6, `decisions.md` D-019, `flow.md` F-018.

Quiz topics from the original checkpoint: explain why OCR is behind ports, the exact per-page flow, why shell execution and timeouts matter, how provider failures differ from content failures, and the interview explanation.

1. Concept: what problem does “Replaceable OCR fallback boundary” solve, and why is its boundary necessary?
2. Execution: trace the entry point through its functions and data stores to the output above. Who owns the transaction or state?
3. Failure: choose one failure branch above; explain what is persisted, what is returned, and whether retry is safe.
4. Trade-off: defend the choice above against one rejected alternative. When would you change it?
5. Interview: explain this slice in two sentences, naming its proof and one limitation. What can you honestly claim today?

Learner answers: pending.

## Slice 3.7 — Activate OCR fallback in normalized PDF artifacts

Status: unanswered; pause explicitly overridden.

Objective: route scanned or partially scanned PDF pages through the OCR boundary and persist one immutable normalized artifact that preserves both digital and OCR page provenance.

Explanation: Routing only pages without usable digital text avoids duplicate content and unnecessary CPU work. Page-numbered OCR spans preserve citation location while their `kind` and OCR metadata make their weaker source semantics explicit. Changing parser identity makes the merged artifact immutable and reproducible; the original digital artifact remains available when OCR is disabled. An opt-in setting prevents a missing native dependency from changing the default worker behavior and allows deployment readiness to be proven separately.

Input/output: The worker's normalized-artifact handler reads the canonical PDF bytes and invokes the configured extractor. The extractor always parses the PDF's digital text layer first. When OCR is enabled and one or more pages contain no usable normalized digital text, it passes only those ordered page numbers and the original PDF bytes to `PdfOcrFallback`. It returns one `NormalizedDocument`; a successfully merged result has OCR metadata, a `pypdf-ocr` parser name, and an immutable parser/config-derived version.

```text
claimed PDF job
  -> normalized-artifact handler / configured ExtractorRegistry
  -> PdfExtractor parses every page with pypdf
  -> collect page text and missing page numbers
  -> if fallback configured and pages are missing:
       -> PdfOcrFallback renders/recognizes the missing pages
       -> validate exact ordered page coverage
       -> canonicalize OCR text
  -> merge digital and OCR page records in page order
  -> emit page-numbered spans (digital kind or OCR kind)
  -> recompute PDF quality and record OCR metadata
  -> canonical JSON/checksum -> object storage -> PostgreSQL metadata
  -> commit job success -> acknowledge Redis
```

Failure branches: OCR provider unavailable or timed out, renderer/provider non-zero exit, invalid fallback page results, all pages still empty, duplicate delivery after an OCR artifact commit, configuration disabled, and worker acknowledgement ordering. Native runtime failure must not be reported as successful extraction.

Trade-off: The merged artifact has two provenance coordinate systems: digital spans use page-local parser text offsets, while OCR spans use page-local generated OCR text offsets. This is explicit but must be carried into later citation/UI contracts. OCR configuration changes generate a new parser identity, which preserves history but increases artifact storage. A successful OCR provider that returns no text leaves the page empty; if every page remains empty the existing typed no-text failure prevents an empty artifact. Native Tesseract execution, confidence, layout, concurrency, and cloud-provider failover remain unverified or deferred.

References: `implementation.md` Slice 3.7, `decisions.md` D-020, `flow.md` F-019.

Quiz topics from the original checkpoint: explain the routing predicate, how digital and OCR provenance differ, why parser identity changes, how retry/dead-letter classification works, and the interview explanation.

1. Concept: what problem does “Activate OCR fallback in normalized PDF artifacts” solve, and why is its boundary necessary?
2. Execution: trace the entry point through its functions and data stores to the output above. Who owns the transaction or state?
3. Failure: choose one failure branch above; explain what is persisted, what is returned, and whether retry is safe.
4. Trade-off: defend the choice above against one rejected alternative. When would you change it?
5. Interview: explain this slice in two sentences, naming its proof and one limitation. What can you honestly claim today?

Learner answers: pending.

## Slice 3.8 — Deterministic DOCX extraction and worker activation

Status: unanswered; pause explicitly overridden.

Objective: process the already accepted DOCX upload type into normalized, section-aware text instead of dead-lettering it as unsupported.

Explanation: The OOXML package is a ZIP container, so checking declared uncompressed sizes before XML parsing provides a simple memory/CPU boundary against oversized or zip-bomb-like input. Parsing only the main document part keeps the contract focused and deterministic while still handling the common paragraph/run structure. Logical character offsets are useful to downstream chunking and citation even though they are not XML byte offsets. A parser identity makes the immutable artifact reproducible and lets a future richer parser coexist without changing the meaning of existing checksums.

Input/output: The worker receives a committed DOCX document-ingestion message, loads the immutable source bytes from object storage, and selects `DocxExtractor` by `source_type="docx"`. The extractor returns one `NormalizedDocument` with canonical text, paragraph-level contiguous spans, heading-derived `section_path`, `kind`, and the `docx-xml` / `ooxml-xml-v1` parser identity. The existing handler then writes the canonical artifact to object storage, persists only artifact metadata in PostgreSQL, marks the job successful, and allows Redis acknowledgement after commit.

```text
claimed DOCX job
  -> normalized-artifact handler / active ExtractorRegistry
  -> DocxExtractor opens source bytes as an OOXML ZIP
  -> sum member file_size values and reject an oversized archive
  -> require [Content_Types].xml and word/document.xml
  -> read only word/document.xml within the same size bound
  -> parse w:body and traverse w:p in document order
  -> skip w:del; emit w:t, w:tab, w:br/w:cr as logical text
  -> infer Heading1..Heading9 and update section paths
  -> skip blank paragraphs and build contiguous logical offsets
  -> canonical JSON/checksum -> object storage -> PostgreSQL metadata
  -> commit job success -> acknowledge Redis
```

Failure branches: invalid ZIP/XML, missing required OOXML part, zip-bomb-sized uncompressed archive, no meaningful text, unsupported rich-content semantics, duplicate delivery after artifact persistence, and worker permanent failure mapping. DOCX parsing failures are permanent content errors.

Trade-off: The parser is intentionally not a full Word layout engine. Headers/footers, footnotes, comments, drawings/images, table-layout reconstruction, page numbers, and nuanced tracked-change policies remain deferred; deleted runs are ignored. Paragraphs in tables are included in document order, but table geometry is not represented. Source offsets refer to the logical DOCX text sequence produced by this parser, not compressed bytes or XML character positions. A malformed or semantically empty DOCX is a permanent content failure; storage/database failures and duplicate artifact delivery continue to use the existing ingestion guarantees.

References: `implementation.md` Slice 3.8, `decisions.md` D-021, `flow.md` F-020.

Quiz topics from the original checkpoint: explain why ZIP/XML parsing is bounded, how heading paths and offsets are produced, what DOCX semantics are intentionally omitted, how worker failure classification works, and the interview explanation.

1. Concept: what problem does “Deterministic DOCX extraction and worker activation” solve, and why is its boundary necessary?
2. Execution: trace the entry point through its functions and data stores to the output above. Who owns the transaction or state?
3. Failure: choose one failure branch above; explain what is persisted, what is returned, and whether retry is safe.
4. Trade-off: defend the choice above against one rejected alternative. When would you change it?
5. Interview: explain this slice in two sentences, naming its proof and one limitation. What can you honestly claim today?

Learner answers: pending.

## Slice 4.1 — Deterministic metadata extraction contract

Status: unanswered; pause explicitly overridden.

Objective: derive explainable, versioned document metadata from a normalized artifact before introducing LLM-generated WikiRAG content.

Explanation: Deterministic metadata is cheap, reproducible, and explainable, and it gives a later LLM pipeline a stable input/output boundary. Conservative language and date rules avoid presenting guesses as facts. Reusing existing spans keeps metadata traceable to the source version without adding a second coordinate system. Pydantic validation makes the structured contract explicit before any model output or persistence is introduced.

Input/output: This pure application step receives a validated `NormalizedDocument` and optional source metadata (`title_hint` and `filename`). It returns a frozen Pydantic `DocumentMetadata` value containing title, language, source type, heading records, normalized dates, and author records. Evidence fields point to normalized character ranges and reuse the matching normalized span's section path and page number when available.

```text
NormalizedDocument + optional title hint/filename
  -> validate non-empty normalized contract
  -> convert heading spans into typed heading evidence
  -> choose title: hint -> first heading -> first non-empty line -> filename stem
  -> classify language as conservative en or und
  -> scan valid ISO and month-name dates; normalize to YYYY-MM-DD
  -> scan labeled author lines and split usable names
  -> map every inferred range to normalized text/span context
  -> Pydantic validation -> canonical JSON/checksum
```

Failure branches: empty normalized content, invalid evidence offsets, unsupported/ambiguous date forms, missing title candidates, and author lines with no usable names. The extractor must fail closed for invalid normalized inputs and omit uncertain metadata rather than inventing it.

Trade-off: The baseline supports English/unknown classification and two date formats; other languages, locale-specific dates, author semantics, and richer document metadata remain unrecognized rather than guessed. The result is an in-memory contract in this slice, so no database row or worker state changes yet. The metadata checksum versions this output, but page persistence, LLM prompt/model identity, human review, and regeneration remain later decisions.

References: `implementation.md` Slice 4.1, `decisions.md` D-022, `flow.md` F-021.

Quiz topics from the original checkpoint: explain why deterministic metadata precedes LLM generation, title precedence, language/date limitations, how evidence offsets are mapped, the omission-versus-invention trade-off, and a two-sentence interview explanation.

1. Concept: what problem does “Deterministic metadata extraction contract” solve, and why is its boundary necessary?
2. Execution: trace the entry point through its functions and data stores to the output above. Who owns the transaction or state?
3. Failure: choose one failure branch above; explain what is persisted, what is returned, and whether retry is safe.
4. Trade-off: defend the choice above against one rejected alternative. When would you change it?
5. Interview: explain this slice in two sentences, naming its proof and one limitation. What can you honestly claim today?

Learner answers: pending.

## Slice 4.2 — Deterministic WikiRAG page skeleton

Status: unanswered; pause explicitly overridden.

Objective: turn normalized content plus deterministic metadata into a typed, versioned WikiRAG page skeleton that later structured generation can safely enrich.

Explanation: The page is a derived projection, never a replacement for the normalized source artifact. Carrying source and metadata checksums prevents mixing fields from different document versions, while deterministic ids make repeated builds and future upserts idempotent. Frozen Pydantic models reject malformed structure before persistence or model enrichment and keep the later LLM boundary explicit.

Input/output: This pure derived step receives a `NormalizedDocument` and its matching `DocumentMetadata`. It returns a frozen `WikiPage` containing source type, language, title, source/metadata checksums, ordered `WikiSection` records with deterministic ids and heading evidence, typed but empty definitions and references, and `review_status="draft"`.

```text
NormalizedDocument + matching DocumentMetadata
  -> validate source-type and evidence-range consistency
  -> copy title/language/source identity and checksums
  -> derive one stable section id per metadata heading
  -> build ordered sections with heading/page/section evidence
  -> initialize empty definition/reference collections and draft status
  -> Pydantic validation -> canonical JSON/checksum
```

Failure branches: metadata from a different source type, evidence outside normalized text, empty/invalid section paths, invalid definition/reference evidence, and malformed future model payloads. Model payload handling is deferred; this slice must not silently accept inconsistent deterministic inputs.

Trade-off: The current page contains structure and deterministic metadata, not semantic summary or model-generated definitions. Section ids change when the source content or heading coordinates change, which is intentional for immutable versioning. The schema does not yet persist review history, prompt/model/config hashes, or generated-field evidence beyond the reusable range type; those are future LLM/persistence concerns. A draft page with no headings is valid because some normalized sources have no heading structure.

References: `implementation.md` Slice 4.2, `decisions.md` D-023, `flow.md` F-022.

Quiz topics from the original checkpoint: explain why the page is a derived artifact, how section ids remain stable, how provenance is represented for future generated fields, what consistency checks prevent cross-version mixing, and a two-sentence interview explanation.

1. Concept: what problem does “Deterministic WikiRAG page skeleton” solve, and why is its boundary necessary?
2. Execution: trace the entry point through its functions and data stores to the output above. Who owns the transaction or state?
3. Failure: choose one failure branch above; explain what is persisted, what is returned, and whether retry is safe.
4. Trade-off: defend the choice above against one rejected alternative. When would you change it?
5. Interview: explain this slice in two sentences, naming its proof and one limitation. What can you honestly claim today?

Learner answers: pending.

## Slice 4.3 — Provider-neutral structured WikiRAG generation boundary

Status: unanswered; pause explicitly overridden.

Objective: create a safe, provider-independent boundary for enriching a deterministic WikiRAG page with structured summaries, definitions, and references without allowing model output to replace source-derived identity.

Explanation: The port makes provider choice replaceable and keeps the application contract explainable without credentials or network calls. JSON encoding gives the document a structural data boundary even when its content contains commands, role requests, or prompt-like delimiters. Strict validation plus source-span checking prevents malformed or fabricated citations from crossing into later persistence. Separate provider and output errors let a later worker classify dependency failures as retryable while treating invalid model content as a permanent generation failure.

Input/output: An application caller supplies a matching `NormalizedDocument`, deterministic `WikiPage`, a generation configuration checksum, and a `WikiGenerationProvider`. The provider receives a frozen request containing the page skeleton and normalized text. The boundary returns a frozen `WikiGenerationResult` containing validated generated content and the page, source artifact, configuration, and provider identities.

```text
caller
  -> validate NormalizedDocument/WikiPage source identity
  -> build frozen WikiGenerationRequest
  -> serialize prompt rules + page skeleton + document_data JSON
  -> provider.generate(request)
  -> parse mapping or JSON text
  -> validate frozen summary/definition/reference schema
  -> validate each evidence range, text, section path, and page context
  -> emit frozen WikiGenerationResult with immutable identities
```

Failure branches: mismatched page/source artifact, empty provider identity, provider exception, invalid JSON, unknown output fields, missing evidence, evidence outside normalized text, evidence text mismatch, and evidence context mismatch. These failures must not mutate source or page state.

Trade-off: This slice proves a boundary with fake providers, not real model quality, latency, token cost, or injection resistance against a deployed model. The contract requires generated evidence to fit one normalized span, so a claim whose support crosses parser spans must be split or omitted. JSON parsing is strict and does not attempt automatic repair. The result is still in memory; retry policy, persistence, review status transitions, and partial failure handling remain future slices.

References: `implementation.md` Slice 4.3, `decisions.md` D-024, `flow.md` F-023.

Quiz topics from the original checkpoint: explain the prompt data boundary, provider-versus-output error distinction, strict schema role, evidence-context validation, page/source/config identity capture, and a two-sentence interview explanation.

1. Concept: what problem does “Provider-neutral structured WikiRAG generation boundary” solve, and why is its boundary necessary?
2. Execution: trace the entry point through its functions and data stores to the output above. Who owns the transaction or state?
3. Failure: choose one failure branch above; explain what is persisted, what is returned, and whether retry is safe.
4. Trade-off: defend the choice above against one rejected alternative. When would you change it?
5. Interview: explain this slice in two sentences, naming its proof and one limitation. What can you honestly claim today?

Learner answers: pending.

## Slice 4.4 — Immutable WikiRAG generation artifact persistence

Status: unanswered; pause explicitly overridden.

Objective: persist a validated `WikiGenerationResult` as an immutable, tenant-scoped derived artifact while keeping large JSON outside PostgreSQL and preserving source-artifact lineage.

Explanation: This preserves PostgreSQL as the source of truth for ownership and artifact metadata while object storage holds the large canonical payload. Verifying the normalized artifact checksum prevents a caller from attaching a valid result to a different source version. Commit-after-object-write plus cleanup handles the cross-system failure window, and identity conflicts prevent silent model output replacement. Explicit tenant predicates provide the same application boundary used by existing document-artifact persistence until document-table RLS is implemented.

Input/output: The caller supplies a tenant id, document-version id, normalized-artifact id, and an already validated `WikiGenerationResult`. The service returns a typed persisted-artifact identity with a `reused` flag. The canonical JSON payload is stored in object storage; PostgreSQL stores only ownership, lineage, checksums, provider/configuration identity, review status, and the object key.

```text
caller
  -> tenant-scoped normalized-artifact lookup
  -> verify result.source_artifact_checksum
  -> derive object key from tenant/version/page/result identity
  -> lookup immutable generation identity
  -> if existing: verify object bytes and return reused or conflict
  -> write canonical result JSON to object storage
  -> create metadata row
  -> commit PostgreSQL transaction
  -> return persisted identity
```

Failure branches: missing or foreign normalized artifact, source checksum mismatch, object read/write failure, database commit failure, cleanup failure, concurrent identical insert, concurrent conflicting insert, and corrupted existing object. No failure may overwrite an immutable artifact.

Trade-off: The database stores metadata and ownership, not the generated JSON itself; reading a page later requires a metadata lookup followed by object storage. Compensating deletion is best-effort, so cleanup failure is surfaced for operator repair. The service is idempotent for the tested object adapter but does not claim distributed transactions or production RLS. Worker state, review history, and live-provider retry policy remain future slices.

References: `implementation.md` Slice 4.4, `decisions.md` D-025, `flow.md` F-024.

Quiz topics from the original checkpoint: explain why JSON is outside PostgreSQL, how source lineage is verified, how idempotent reuse differs from overwrite, the object/database failure ordering, tenant-boundary enforcement, and a two-sentence interview explanation.

1. Concept: what problem does “Immutable WikiRAG generation artifact persistence” solve, and why is its boundary necessary?
2. Execution: trace the entry point through its functions and data stores to the output above. Who owns the transaction or state?
3. Failure: choose one failure branch above; explain what is persisted, what is returned, and whether retry is safe.
4. Trade-off: defend the choice above against one rejected alternative. When would you change it?
5. Interview: explain this slice in two sentences, naming its proof and one limitation. What can you honestly claim today?

Learner answers: pending.

## Slice 4.5 — Persist a self-contained WikiRAG page artifact

Status: unanswered; pause explicitly overridden.

Objective: package the deterministic `WikiPage` skeleton together with its validated `WikiGenerationResult` so one persisted page artifact can later be read, reviewed, and rendered without reconstructing the skeleton from another transient pipeline step.

Explanation: The page artifact becomes independently readable and reviewable while the generation result remains a lower-level, model-owned immutable artifact. A separate table makes the boundary explicit, preserves backward compatibility for result objects, and lets later projections rebuild page views from a versioned composite payload. Redundant tenant and parent ids in the new row make authorization predicates explicit and efficient at the repository boundary.

Input/output: The caller supplies a tenant id, document-version id, normalized-artifact id, the previously persisted generation-artifact id, a deterministic `WikiPage`, and the matching validated `WikiGenerationResult`. The service returns a typed page-artifact identity with the wrapper content checksum, object key, review status, and whether an existing immutable artifact was reused.

```text
caller
  -> validate WikiPage + WikiGenerationResult wrapper checksums
  -> tenant-scoped generation-artifact lookup
  -> verify generation result/page/source/metadata lineage
  -> derive canonical {page, generation} JSON and checksum
  -> lookup page-artifact identity
  -> if existing: verify object bytes and return reused or conflict
  -> write composite JSON to object storage
  -> create tenant/version/source-linked page metadata row
  -> commit PostgreSQL transaction
  -> return persisted identity
```

Failure branches: invalid page/result pairing, missing or foreign generation artifact, generation checksum or lineage mismatch, object read/write failure, database failure, cleanup failure, concurrent identical insert, concurrent conflicting insert, and corrupted existing object. No existing generation or page artifact may be overwritten.

Trade-off: There are now two immutable derived layers: model-owned generation JSON and a self-contained page package. This costs one additional metadata row and object write, but it keeps each checksum's meaning precise and enables future review without a live provider call. The new metadata table does not yet have document-table RLS, API reads, review transitions, or worker orchestration; those remain explicit later slices. Cross-system cleanup is still best effort.

References: `implementation.md` Slice 4.5, `decisions.md` D-026, `flow.md` F-025.

Quiz topics from the original checkpoint: explain why the page artifact is separate from the generation result, the exact wrapper checksum/lineage checks, the object-first transaction flow, the difference between reuse and overwrite, and a two-sentence interview explanation.

1. Concept: what problem does “Persist a self-contained WikiRAG page artifact” solve, and why is its boundary necessary?
2. Execution: trace the entry point through its functions and data stores to the output above. Who owns the transaction or state?
3. Failure: choose one failure branch above; explain what is persisted, what is returned, and whether retry is safe.
4. Trade-off: defend the choice above against one rejected alternative. When would you change it?
5. Interview: explain this slice in two sentences, naming its proof and one limitation. What can you honestly claim today?

Learner answers: pending.

## Slice 4.6 — Expose a tenant-scoped WikiRAG page read path

Status: unanswered; pause explicitly overridden.

Objective: make a persisted self-contained WikiRAG page artifact readable by an authenticated tenant member without exposing another tenant's metadata or unverified object contents.

Explanation: The database remains the tenant-scoped ownership/index boundary and object storage remains the large-payload boundary. Requiring permission in the application service keeps role policy centralized, while the tenant predicate prevents a foreign id from becoming an oracle. Hashing before schema use prevents corrupted bytes from reaching the response, and post-validation audit records describe data that was actually served.

Input/output: An authenticated tenant member supplies a page-artifact UUID and bearer access token. The route returns page-artifact metadata plus the validated complete `WikiPage` skeleton and `WikiGenerationResult`. The response never exposes the storage key as an authorization input; it is loaded from the tenant-scoped metadata row.

```text
HTTP request
  -> bearer JWT verification + current membership resolution
  -> require READ_DOCUMENTS
  -> tenant-scoped page-artifact id lookup
  -> 404 if missing/foreign
  -> object read by database-owned key
  -> SHA-256 verification against metadata
  -> JSON parse + WikiGeneratedPage schema/lineage validation
  -> response mapping
  -> success audit event + PostgreSQL commit
  -> HTTP 200
```

Failure branches: missing/foreign artifact, absent bearer token, inactive or unauthorized principal, object read failure, checksum mismatch, malformed JSON, schema/lineage mismatch, and audit/metadata commit failure. The endpoint must never return unverified or cross-tenant page data.

Trade-off: The read path performs one database lookup and one object read for each page, which is intentionally simple and explainable before caching is introduced. The response exposes the complete deterministic and generated layers but does not change review status. Object-store outages are visible as retryable API failures, while integrity failures require operator investigation. Document table RLS, production object storage, listing, review transitions, and regeneration remain deferred.

References: `implementation.md` Slice 4.6, `decisions.md` D-027, `flow.md` F-026.

Quiz topics from the original checkpoint: explain the exact auth-to-database-to-object flow, why `404` is used for both foreign and missing ids, how object integrity is verified, why audit happens after validation, and a two-sentence interview explanation.

1. Concept: what problem does “Expose a tenant-scoped WikiRAG page read path” solve, and why is its boundary necessary?
2. Execution: trace the entry point through its functions and data stores to the output above. Who owns the transaction or state?
3. Failure: choose one failure branch above; explain what is persisted, what is returned, and whether retry is safe.
4. Trade-off: defend the choice above against one rejected alternative. When would you change it?
5. Interview: explain this slice in two sentences, naming its proof and one limitation. What can you honestly claim today?

Learner answers: pending.

## Slice 4.7 — Add tenant-scoped WikiRAG review-status transitions

Status: unanswered; pause explicitly overridden.

Objective: let an authorized reviewer move a persisted page through a small, explicit review state machine without mutating its immutable content object.

Explanation: Review state is mutable workflow metadata, while the page/generation object is immutable derived content. Separating these concerns permits review without rewriting or re-hashing the package. A dedicated permission makes the policy explicit instead of silently equating document editing with page approval, and compare-and-set preserves the last committed reviewer decision under races.

Input/output: An authenticated editor or admin supplies a page-artifact UUID and a JSON body containing one target status: `draft`, `needs_review`, or `approved`. The route returns the artifact id, tenant id, previous status, current status, and whether the request changed state. It never accepts an object key and never rewrites the page package.

```text
HTTP request + bearer JWT
  -> verify token and current tenant membership
  -> require REVIEW_WIKI_PAGES
  -> tenant-scoped artifact lookup
  -> 404 if missing/foreign
  -> validate target status and allowed transition
  -> conditional update where tenant + id + expected current status match
  -> 409 if transition is invalid or stale
  -> record success audit with from/to/changed metadata
  -> commit status and audit together
  -> HTTP 200
```

Failure branches: absent/invalid auth, inactive or unauthorized principal, missing/foreign artifact, unknown target status, invalid state transition, stale concurrent transition, database/audit commit failure, and an artifact row containing an unsupported legacy status.

Trade-off: The page artifact row is mutable only in its review metadata; its object bytes, content checksum, page checksum, and generation lineage remain immutable. The API adds one database read and one conditional update before the audit commit. The three-state workflow is intentionally small and does not represent review comments, assignment, or a separate rejected state. A failed compare-and-set returns a conflict for the client to reload rather than guessing a new state.

References: `implementation.md` Slice 4.7, `decisions.md` D-028, `flow.md` F-027.

Quiz topics from the original checkpoint: explain immutable content versus mutable workflow state, the allowed transitions, compare-and-set concurrency, authorization/audit ordering, and a two-sentence interview explanation.

1. Concept: what problem does “Add tenant-scoped WikiRAG review-status transitions” solve, and why is its boundary necessary?
2. Execution: trace the entry point through its functions and data stores to the output above. Who owns the transaction or state?
3. Failure: choose one failure branch above; explain what is persisted, what is returned, and whether retry is safe.
4. Trade-off: defend the choice above against one rejected alternative. When would you change it?
5. Interview: explain this slice in two sentences, naming its proof and one limitation. What can you honestly claim today?

Learner answers: pending.

## Slice 4.8 — Add tenant-scoped WikiRAG page listing

Status: unanswered; pause explicitly overridden.

Objective: let authenticated tenant members discover persisted WikiRAG page artifacts with bounded pagination and optional review-status filtering without reading large object-storage payloads.

Explanation: Database metadata is the index needed for reviewer discovery, while the object payload is large and unnecessary until a selected page is opened. Server-side tenant/status predicates make the boundary explicit, deterministic ordering keeps offset pagination stable for a fixed dataset, and a bounded window prevents an accidental unbounded query. Fetching `limit + 1` avoids a separate count query while still telling the client whether another page exists.

Input/output: An authenticated tenant member supplies optional `review_status`, `limit`, and `offset` query parameters. The route returns a bounded list of page-artifact metadata summaries, the requested window, and `has_more`; it does not return the composite page JSON or storage object key.

```text
HTTP request + bearer JWT
  -> verify token and current tenant membership
  -> require READ_DOCUMENTS
  -> validate status/limit/offset query parameters
  -> tenant + optional status repository query
  -> deterministic created_at/id ordering
  -> fetch limit + 1 rows and derive has_more
  -> map metadata summaries
  -> record success audit with filter/window/count
  -> commit audit
  -> HTTP 200
```

Failure branches: absent/invalid auth, inactive or unauthorized principal, invalid status/limit/offset, database query failure, unsupported persisted review status, and audit/commit failure. No list response may expose another tenant's artifact or object key.

Trade-off: The list is a metadata index and does not prove object integrity; the selected page read still performs checksum/schema validation. Offset pagination is easy to consume but can shift when new rows arrive, so cursor pagination may be introduced if measured scale requires it. Each successful list adds an audit row, which improves access visibility but creates write load for high-volume polling; aggregation or sampling can be considered with observability data.

References: `implementation.md` Slice 4.8, `decisions.md` D-029, `flow.md` F-028.

Quiz topics from the original checkpoint: explain why listing returns metadata instead of object payloads, how tenant filtering and deterministic pagination work, the empty/filter behavior, the audit trade-off, and a two-sentence interview explanation.

1. Concept: what problem does “Add tenant-scoped WikiRAG page listing” solve, and why is its boundary necessary?
2. Execution: trace the entry point through its functions and data stores to the output above. Who owns the transaction or state?
3. Failure: choose one failure branch above; explain what is persisted, what is returned, and whether retry is safe.
4. Trade-off: defend the choice above against one rejected alternative. When would you change it?
5. Interview: explain this slice in two sentences, naming its proof and one limitation. What can you honestly claim today?

Learner answers: pending.

## Slice 4.9 — Activate the deterministic WikiRAG artifact pipeline

Status: unanswered; pause explicitly overridden.

Objective: connect the existing durable ingestion worker to the deterministic metadata, WikiRAG page, structured-generation, generation-artifact, and self-contained page-artifact stages so one accepted document can reach a readable page package asynchronously.

Explanation: This composes already proven boundaries into one explainable pipeline while keeping provider selection behind a port. The deterministic provider makes the worker path locally reproducible and exercises provenance validation, artifact identity, and idempotency without making a false claim about model quality or external credentials. Stage labels make the existing progress API useful for diagnosis, and the consumer remains the owner of terminal job state and Redis acknowledgement.

Input/output: The consumer validates the stream envelope, claims the tenant-scoped `IngestionJob`, and passes the claimed job plus the untrusted payload to the WikiRAG handler. The handler ignores payload-selected resource ids and uses the job's tenant and document-version ids. Success leaves one normalized artifact, one generation artifact, and one self-contained page artifact, then the consumer marks the job `succeeded`, commits, and acknowledges Redis.

```text
Redis event
  -> parse and tenant-scoped lease claim
  -> current_step=normalize
  -> NormalizedArtifactService.persist
  -> current_step=metadata
  -> DeterministicMetadataExtractor.extract
  -> WikiPageBuilder.build
  -> current_step=generate
  -> StructuredWikiGenerator + deterministic baseline provider
  -> WikiGenerationArtifactService.persist
  -> current_step=page_artifact
  -> WikiPageArtifactService.persist
  -> JobRepository.mark_succeeded
  -> PostgreSQL commit
  -> Redis acknowledgement
```

Failure branches: tenant/version mismatch, malformed or unsupported source, normalized artifact conflict/storage/commit failure, invalid metadata/page inputs, provider failure, invalid provider output, generation/page artifact conflict or storage/commit failure, duplicate delivery, and acknowledgement occurring before durable artifact completion.

Trade-off: The pipeline has independent commits: normalized metadata, generation metadata, and page metadata may remain after a later permanent failure, but all are immutable and safely reusable on a retry. A provider is invoked again after a crash before its artifact commit; deterministic identity makes the local baseline safe, while live providers will need an explicit idempotency strategy. The baseline summary is intentionally minimal and is not evidence of semantic LLM quality. Carrying the normalized document in the service result increases in-process memory for one job, bounded by the existing document upload and worker limits, and avoids a second parse in the same attempt.

References: `implementation.md` Slice 4.9, `decisions.md` D-030, `flow.md` F-029.

Quiz topics from the original checkpoint: explain why the handler is a pipeline composition boundary, how normalized data reaches metadata/page/generation persistence, why the baseline provider is not a live LLM, how each failure class affects Redis acknowledgement and job state, the commit boundaries and duplicate-reuse behavior, and a two-sentence interview explanation.

1. Concept: what problem does “Activate the deterministic WikiRAG artifact pipeline” solve, and why is its boundary necessary?
2. Execution: trace the entry point through its functions and data stores to the output above. Who owns the transaction or state?
3. Failure: choose one failure branch above; explain what is persisted, what is returned, and whether retry is safe.
4. Trade-off: defend the choice above against one rejected alternative. When would you change it?
5. Interview: explain this slice in two sentences, naming its proof and one limitation. What can you honestly claim today?

Learner answers: pending.

## Slice 4.10 — Activate authorized WikiRAG regeneration

Status: unanswered; pause explicitly overridden.

Objective: let an authorized reviewer request a fresh WikiRAG page version from the same immutable document version while preserving every existing page artifact and review decision.

Explanation: This preserves the current asynchronous reliability boundary and gives the reviewer a durable job id to poll. Reusing the existing permission and outbox avoids a second policy system and a dual-write between HTTP and Redis. The configuration hash is already validated and persisted as generation identity, so no new table or migration is needed for this bounded versioned run.

```text
HTTP request + bearer JWT
  -> verify token and current tenant membership
  -> require REVIEW_WIKI_PAGES
  -> tenant-filtered source-page lookup
  -> create pending wiki_regeneration job
  -> create wiki.page.regeneration.requested outbox event
  -> record success audit with job/version/reason metadata
  -> PostgreSQL commit
  -> HTTP 202 with durable job id
```

Failure branches: absent/invalid auth, insufficient role, foreign/missing source page, malformed event/payload, event/job-type mismatch, source-page lineage mismatch, normalized/provider/storage/database failure, immutable artifact conflict, duplicate delivery, and audit/outbox commit failure. No successful response or acknowledgement may precede the durable state it represents.

Trade-off: The request endpoint returns before generation completes and clients must poll the job progress endpoint. Repeated HTTP requests create separate jobs, but the immutable artifact identity makes the resulting storage/database writes reusable; request-key deduplication is deferred. If the server keeps the same regeneration hash, a repeated run reuses the same artifact rather than making an artificial version. A live provider must later define model/config selection and nondeterministic-output idempotency before production use.

References: `implementation.md` Slice 4.10, `decisions.md` D-031, `flow.md` F-030.

Quiz topics from the original checkpoint: explain why regeneration is asynchronous; how authorization, tenant filtering, outbox, Redis, and worker routing connect; why a new configuration identity creates a new immutable generation while the old page remains; how malformed/foreign requests fail; the idempotency and commit trade-offs; and a two-sentence interview explanation.

1. Concept: what problem does “Activate authorized WikiRAG regeneration” solve, and why is its boundary necessary?
2. Execution: trace the entry point through its functions and data stores to the output above. Who owns the transaction or state?
3. Failure: choose one failure branch above; explain what is persisted, what is returned, and whether retry is safe.
4. Trade-off: defend the choice above against one rejected alternative. When would you change it?
5. Interview: explain this slice in two sentences, naming its proof and one limitation. What can you honestly claim today?

Learner answers: pending.

## Slice 5.1 — Add deterministic hierarchical chunking

Status: unanswered; pause explicitly overridden.

Objective: convert one normalized document into stable parent/child retrieval units that preserve heading scope, source offsets, page provenance, and bounded token/character sizes before any embedding or vector-index side effect.

Explanation: This keeps chunk boundaries testable and independent of a database, embedding provider, or Qdrant SDK. Parent context supports later retrieval expansion; child windows keep candidate text bounded. Deriving ids from source identity, kind, range, path, parent identity, and content makes duplicate writes safe in future persistence slices and preserves citation offsets.

```text
NormalizedDocument
  -> deterministic whitespace token spans
  -> top-level heading section ranges and optional preamble
  -> non-overlapping bounded parent windows
  -> bounded child windows with controlled adjacent overlap
  -> exact source text/range, checksum, section path, page range, stable id
  -> ordered immutable Chunk tuple
```

Failure branches: non-normalized input, empty/unprovenanced content, invalid or contradictory budgets, token overlap that prevents progress, and a token longer than the configured character budget. No partial chunk result should be returned for invalid input.

Trade-off: Whitespace-token counts are a transparent approximation and must not be used as a model-token budget claim. Parent/child representations intentionally repeat context across levels, while child overlap is bounded and only occurs between adjacent windows. The first sectioning boundary is top-level heading aware; deeper headings remain in text and section provenance, with richer recursive parent trees deferred until retrieval behavior is measured.

References: `implementation.md` Slice 5.1, `decisions.md` D-032, `flow.md` F-031.

Quiz topics from the original checkpoint: explain why chunks are derived projections, how parent/child ranges and overlap are generated, how offsets/page provenance are preserved, what token-count approximation means, the failure/termination safeguards, and a two-sentence interview explanation.

1. Concept: what problem does “Add deterministic hierarchical chunking” solve, and why is its boundary necessary?
2. Execution: trace the entry point through its functions and data stores to the output above. Who owns the transaction or state?
3. Failure: choose one failure branch above; explain what is persisted, what is returned, and whether retry is safe.
4. Trade-off: defend the choice above against one rejected alternative. When would you change it?
5. Interview: explain this slice in two sentences, naming its proof and one limitation. What can you honestly claim today?

Learner answers: pending.

## Slice 5.2 — Persist immutable chunk manifests

Status: unanswered; pause explicitly overridden.

Objective: persist one deterministic chunk result as a rebuildable canonical manifest linked to its normalized artifact, without making a vector index the source of truth.

Explanation: This preserves PostgreSQL as the canonical metadata authority while keeping large derived text outside the relational row. A future embedding worker can rebuild vectors from the manifest for any model/configuration, and the stable identity prevents duplicate manifests under at-least-once delivery.

```text
ChunkingResult + ChunkingConfig
  -> build and validate ChunkManifest
  -> tenant/version/normalized-artifact lookup
  -> source checksum lineage check
  -> canonical manifest JSON and checksums
  -> existing identity lookup
      -> verify object bytes and reuse
      -> or write object storage first
          -> stage metadata row
          -> commit PostgreSQL metadata
          -> return persisted identity
```

Failure branches: non-chunk result, empty or structurally inconsistent result, tenant/version/artifact mismatch, immutable identity conflict, object read/write/delete failure, database commit failure, and concurrent conflicting insert.

Trade-off: The manifest is an immutable snapshot, so a new chunk schema or configuration creates a new artifact rather than mutating old bytes. Object storage and PostgreSQL still do not share a transaction; cleanup is best effort and later reconciliation remains necessary. Object reads are required for integrity verification on reuse, trading extra I/O for duplicate safety. PostgreSQL document-table RLS and vector payload filtering remain separate controls.

References: `implementation.md` Slice 5.2, `decisions.md` D-033, `flow.md` F-032.

Quiz topics from the original checkpoint: explain why the manifest is canonical while vectors are projections, how object-first/metadata-commit ordering works, how identity and checksums make retries safe, what cleanup cannot guarantee, and a two-sentence interview explanation.

1. Concept: what problem does “Persist immutable chunk manifests” solve, and why is its boundary necessary?
2. Execution: trace the entry point through its functions and data stores to the output above. Who owns the transaction or state?
3. Failure: choose one failure branch above; explain what is persisted, what is returned, and whether retry is safe.
4. Trade-off: defend the choice above against one rejected alternative. When would you change it?
5. Interview: explain this slice in two sentences, naming its proof and one limitation. What can you honestly claim today?

Learner answers: pending.

## Slice 5.3 — Define a deterministic dense-embedding contract

Status: unanswered; pause explicitly overridden.

Objective: establish a provider-neutral, reproducible dense-embedding boundary that records input/config/model identity and validates vector dimensions before any index write.

Explanation: This gives future model adapters one application-owned port and makes vector identity queryable before Qdrant persistence. A deterministic provider keeps unit tests offline and exposes model-independent invariants such as dimension, finite values, and normalization.

Input/output: An embedding caller creates an immutable `EmbeddingRequest` containing exact text, its SHA-256 checksum, and validated `EmbeddingConfig`. The provider returns a frozen `DenseEmbedding` containing a finite vector, dimension, cosine metric, input/configuration checksums, schema version, and provider/model identity.

```text
EmbeddingRequest
  -> validate exact text checksum and configuration
  -> deterministic feature-hash tokens
  -> accumulate signed values into configured dimensions
  -> L2-normalize vector for cosine distance
  -> validate DenseEmbedding invariants
  -> return canonical result
```

Failure branches: wrong request type, empty text, invalid checksum/configuration, non-finite or wrong-dimension provider output, zero-norm output, and unsupported distance metric.

Trade-off: The local vector is cheap, deterministic, and dependency-free but is not a meaningful semantic embedding. Cosine-normalized vectors make distance behavior explicit, while a later provider may use another metric and must create a new model/configuration identity. Batch limits, provider retries, model caching, sparse representations, and index namespaces remain later decisions.

References: `implementation.md` Slice 5.3, `decisions.md` D-034, `flow.md` F-033.

Quiz topics from the original checkpoint: explain dense embeddings versus lexical signals, the request/provider/result flow, why dimension and normalization matter, why the local provider is not semantic proof, and a two-sentence interview explanation.

1. Concept: what problem does “Define a deterministic dense-embedding contract” solve, and why is its boundary necessary?
2. Execution: trace the entry point through its functions and data stores to the output above. Who owns the transaction or state?
3. Failure: choose one failure branch above; explain what is persisted, what is returned, and whether retry is safe.
4. Trade-off: defend the choice above against one rejected alternative. When would you change it?
5. Interview: explain this slice in two sentences, naming its proof and one limitation. What can you honestly claim today?

Learner answers: pending.

## Slice 5.4 — Define a deterministic sparse/lexical contract

Status: unanswered; pause explicitly overridden.

Objective: establish a provider-neutral sparse representation for exact-term retrieval that records input/configuration/model identity and validates sorted sparse-vector geometry before any index write.

Explanation: The contract preserves exact-term signals independently of dense-model behavior, keeps the first baseline offline, and gives future sparse providers and Qdrant adapters a validated shape. Hashing avoids a mutable shared vocabulary in this slice, while the bounded index space makes collision and rebuild trade-offs explicit.

Input/output: An embedding caller creates an immutable `SparseEmbeddingRequest` containing exact text, its SHA-256 checksum, and validated `SparseEmbeddingConfig`. The provider returns a frozen `SparseEmbedding` containing sorted unique indices, aligned positive weights, index-space geometry, dot-product metric, input/configuration checksums, schema version, and provider/model identity.

```text
SparseEmbeddingRequest
  -> validate exact text checksum and configuration
  -> Unicode case-fold and tokenize lexical terms
  -> hash terms into bounded indices and aggregate term frequency
  -> compute positive sublinear weights
  -> sort unique indices and align values
  -> validate SparseEmbedding invariants
  -> return canonical result
```

Failure branches: wrong request type, empty text, invalid checksum/configuration, zero terms, duplicate/unsorted/out-of-range indices, non-finite/non-positive weights, mismatched arrays, and provider-output identity mismatch.

Trade-off: Feature hashing is deterministic and stateless but can collide terms; a larger index space reduces collision probability without eliminating it. Local term-frequency weights are not IDF and do not prove ranking quality. Dot-product sparse scores complement cosine-normalized dense scores but must be calibrated or fused in a later retrieval slice. Batch limits, persistence, and Qdrant integration remain deferred.

References: `implementation.md` Slice 5.4, `decisions.md` D-035, `flow.md` F-034.

Quiz topics from the original checkpoint: explain why sparse retrieval preserves exact terms, how hashing and term weights work, why sparse indices must be sorted/unique, how dense and sparse outputs complement each other, and a two-sentence interview explanation.

1. Concept: what problem does “Define a deterministic sparse/lexical contract” solve, and why is its boundary necessary?
2. Execution: trace the entry point through its functions and data stores to the output above. Who owns the transaction or state?
3. Failure: choose one failure branch above; explain what is persisted, what is returned, and whether retry is safe.
4. Trade-off: defend the choice above against one rejected alternative. When would you change it?
5. Interview: explain this slice in two sentences, naming its proof and one limitation. What can you honestly claim today?

Learner answers: pending.

## Slice 5.5 — Add bounded batch embedding orchestration

Status: unanswered; pause explicitly overridden.

Objective: execute dense or sparse provider requests in bounded batches with a configurable concurrency ceiling, deterministic result ordering, cancellation safety, and request-indexed typed failures before any model cache or index write.

Explanation: Batch boundaries limit memory and make provider pressure visible, while the concurrency ceiling protects a downstream model service. Sequential batches avoid launching the entire corpus at once, and index-bearing errors make a failed chunk diagnosable. A validator at the orchestration boundary prevents a provider adapter from bypassing the existing dense/sparse identity checks.

Input/output: An embedding caller supplies an ordered iterable of dense or sparse requests, an async provider with `embed(request)`, a frozen batch configuration, and an application-owned result validator. The batcher returns a complete tuple of validated results in the same order as the requests, or raises an indexed typed error without returning partial results.

```text
ordered requests
  -> validate batch limits and obtain an input iterator
  -> consume one bounded sequential batch
  -> acquire semaphore for each provider call
  -> run active batch with bounded concurrency
  -> validate each provider result
  -> restore original order
  -> start next bounded batch or return complete tuple
```

Failure branches: invalid batch/concurrency limits, provider exception, malformed provider output, validator exception, active-batch cancellation, and caller cancellation. No partial result tuple is returned on failure.

Trade-off: Sequential batches may leave provider capacity unused between batches, and fail-fast cancellation cannot undo work already accepted by a remote provider. The batcher returns no partial tuple, so callers must retry or persist their own completed work at a later boundary. Provider retries, rate limiting, cache hits, persistence, and Qdrant projection remain deferred.

References: `implementation.md` Slice 5.5, `decisions.md` D-036, `flow.md` F-035.

Quiz topics from the original checkpoint: explain why batching controls memory and provider pressure, how the concurrency ceiling is enforced, why output ordering is restored, what happens to sibling tasks after a failure or cancellation, and a two-sentence interview explanation.

1. Concept: what problem does “Add bounded batch embedding orchestration” solve, and why is its boundary necessary?
2. Execution: trace the entry point through its functions and data stores to the output above. Who owns the transaction or state?
3. Failure: choose one failure branch above; explain what is persisted, what is returned, and whether retry is safe.
4. Trade-off: defend the choice above against one rejected alternative. When would you change it?
5. Interview: explain this slice in two sentences, naming its proof and one limitation. What can you honestly claim today?

Learner answers: pending.

## Slice 5.6 — Add an in-process model/provider cache

Status: unanswered; pause explicitly overridden.

Objective: avoid duplicate model/provider loading within one worker process while bounding memory with an LRU cache, sharing concurrent same-key loads, and cleaning up evicted or shutdown resources safely.

Explanation: The key makes model/configuration identity explicit and prevents a dense model from being reused for a sparse representation or an incompatible dimension. Single-flight avoids a thundering herd during cold start, while LRU bounds the number of loaded instances in each worker process. A closer gives native or GPU-backed adapters a lifecycle boundary without requiring the cache to know provider-specific details.

Input/output: An embedding composition root supplies a validated `ModelCacheKey` and an async loader/closer port. A cache hit returns the existing model instance. A miss returns the result of one shared loader task to all concurrent callers and stores it under the exact identity key; no embedding text or tenant id enters the key.

```text
ModelCacheKey
  -> validate cache state and key identity
  -> return LRU hit, or join/create one in-flight load
  -> await loader through cancellation shield
  -> reject failed/empty load, or insert loaded model
  -> evict least-recently-used entry when over capacity
  -> close evicted model and return loaded instance
```

Failure branches: malformed key, invalid capacity, loader failure, loader returning no model, evicted-model cleanup failure, shutdown cleanup failure, use after close, and caller cancellation. Cache failures must not silently return a different model identity.

Trade-off: The cache is local to one worker process, so each process may load its own copy and a restart loses warm state. LRU eviction may reload cold models, and a remote provider may continue work after local cancellation. Cleanup failures need operational visibility, while TTL, invalidation, retries, provider-native model management, persistence, and Qdrant remain later concerns.

References: `implementation.md` Slice 5.6, `decisions.md` D-037, `flow.md` F-036.

Quiz topics from the original checkpoint: explain why this is a model cache rather than an embedding-result cache, how the cache key prevents incompatible reuse, how single-flight loading works, what LRU eviction and cleanup do, and a two-sentence interview explanation.

1. Concept: what problem does “Add an in-process model/provider cache” solve, and why is its boundary necessary?
2. Execution: trace the entry point through its functions and data stores to the output above. Who owns the transaction or state?
3. Failure: choose one failure branch above; explain what is persisted, what is returned, and whether retry is safe.
4. Trade-off: defend the choice above against one rejected alternative. When would you change it?
5. Interview: explain this slice in two sentences, naming its proof and one limitation. What can you honestly claim today?

Learner answers: pending.

## Slice 5.7 — Define a tenant-scoped Qdrant projection contract

Status: unanswered; pause explicitly overridden.

Objective: define and verify a provider-neutral vector-index projection boundary that can represent one chunk with named dense and sparse vectors, preserve exact tenant/document provenance, and make repeated upserts deterministic before adding a live Qdrant adapter.

Explanation: Keeping the port and schemas in the application layer prevents domain code from depending on a vendor SDK and makes the projection testable without a network service. Explicit payload provenance makes retrieved evidence traceable back to the immutable document version and chunk manifest. Deterministic identity plus byte comparison makes at-least-once retries safe while still detecting a provider/configuration conflict. Requiring both named vectors preserves the hybrid retrieval contract; later specialized collections can define a separate schema.

Input/output: An application composition root supplies a validated chunk, its tenant/document version lineage, extracted metadata, the collection geometry, and one matching dense plus sparse embedding result. The projection boundary returns an immutable point with deterministic id, named vector values, provenance payload, and canonical bytes. The index port returns `created` or `reused`, or rejects a conflicting immutable point.

```text
chunk + document lineage + metadata + dense/sparse results
  -> validate collection configuration and tenant/document/chunk identity
  -> validate dense dimension, sparse index-space, and input checksums
  -> build provenance-only payload (no raw text)
  -> derive deterministic point id from projection identity
  -> serialize named dense/sparse vectors and payload
  -> VectorIndex.upsert(point)
  -> create new point, reuse identical bytes, or reject immutable conflict
```

Failure branches: invalid collection geometry/name, missing or mismatched chunk lineage, dense input checksum mismatch, sparse input checksum mismatch, wrong dense dimension, wrong sparse index space, malformed point identity/payload, conflicting immutable point bytes, use of an invalid index input, and foreign-tenant access.

Trade-off: The point payload is deliberately metadata-only, so a later retrieval path must resolve canonical chunk text from artifacts or a database-backed manifest. A model/configuration change produces a new projection identity and may require a new collection or rebuild policy. The in-memory adapter proves contract semantics only; Qdrant network behavior, payload indexes, durability, retry policy, and search performance remain unverified.

References: `implementation.md` Slice 5.7, `decisions.md` D-038, `flow.md` F-037.

Quiz topics from the original checkpoint: explain why Qdrant is a projection rather than the source of truth, how named dense/sparse vectors and payload provenance are validated, how deterministic point identity enables idempotent upserts, how tenant filtering prevents leakage, and a two-sentence interview explanation.

1. Concept: what problem does “Define a tenant-scoped Qdrant projection contract” solve, and why is its boundary necessary?
2. Execution: trace the entry point through its functions and data stores to the output above. Who owns the transaction or state?
3. Failure: choose one failure branch above; explain what is persisted, what is returned, and whether retry is safe.
4. Trade-off: defend the choice above against one rejected alternative. When would you change it?
5. Interview: explain this slice in two sentences, naming its proof and one limitation. What can you honestly claim today?

Learner answers: pending.

## Slice 5.8 — Activate the live Qdrant projection adapter

Status: unanswered; pause explicitly overridden.

Objective: connect the verified provider-neutral hybrid point contract to a real Qdrant collection with Qdrant-compatible point ids, named dense/sparse vectors, tenant/document payload indexes, schema validation, and tenant-scoped create/reuse/read behavior.

Explanation: The official Qdrant async client supports the same async application shape, named dense/sparse collection configuration, and payload-index API needed here. Provisioning indexes before ingesting points also gives filtered retrieval the intended index structures. An infrastructure adapter proves the real dependency boundary while preserving a provider-neutral application port and local fake tests.

Input/output: The worker/application composition root supplies Qdrant connection settings, the server-owned collection geometry, and a validated `VectorPoint`. Schema provisioning creates or validates one Qdrant collection and payload indexes. `upsert()` writes or reuses a point; `get()` returns a validated application point only when the requested tenant matches.

```text
settings + VectorCollectionConfig
  -> create AsyncQdrantClient at infrastructure boundary
  -> collection_exists / get_collection
  -> create or validate named dense/sparse schema
  -> create required tenant/document/provenance payload indexes
  -> VectorPoint -> PointStruct(UUID id, named vectors, metadata payload)
  -> retrieve existing checksum under per-adapter lock
  -> create, reuse, or reject immutable conflict
  -> retrieve -> reconstruct validated VectorPoint -> tenant-scoped result
```

Failure branches: missing/invalid Qdrant configuration, unavailable service, collection schema mismatch, payload-index provisioning failure, malformed point mapping, missing point, foreign tenant, and immutable checksum conflict. Cross-process races remain explicitly unverified until a canonical persistence arbiter is added.

Trade-off: This slice adds a production dependency and a real local-service test. Existing collections with incompatible geometry require an explicit rebuild or new collection name. Read-before-write adds a network round trip and does not solve multi-process races; stronger arbitration remains a later persistence slice. Qdrant search, fusion, retrieval quality, batch ingestion, deletion, reindexing, and performance measurements remain out of scope.

References: `implementation.md` Slice 5.8, `decisions.md` D-039, `flow.md` F-038.

Quiz topics from the original checkpoint: explain why Qdrant point ids must be UUID/uint-compatible, how collection provisioning and payload indexes are made idempotent, how the adapter maps application points to named Qdrant vectors, what the read-before-write immutability limitation is, and a two-sentence interview explanation.

1. Concept: what problem does “Activate the live Qdrant projection adapter” solve, and why is its boundary necessary?
2. Execution: trace the entry point through its functions and data stores to the output above. Who owns the transaction or state?
3. Failure: choose one failure branch above; explain what is persisted, what is returned, and whether retry is safe.
4. Trade-off: defend the choice above against one rejected alternative. When would you change it?
5. Interview: explain this slice in two sentences, naming its proof and one limitation. What can you honestly claim today?

Learner answers: pending.

## Slice 5.9 — Activate chunk and vector projection in the durable worker

Status: unanswered; pause explicitly overridden.

Objective: extend the claimed ingestion workflow from immutable WikiRAG output through deterministic chunk-manifest persistence, bounded dense/sparse embedding, and tenant-scoped vector upserts before the job can succeed and Redis can be acknowledged.

Explanation: PostgreSQL metadata and immutable object artifacts remain the source of truth, so they must exist before a disposable projection is written. Reusing the already verified batching and provider-neutral ports keeps concurrency bounded and allows later model or vector-store replacement. Deterministic identities make replay safe without treating Qdrant as canonical state.

Input/output: A claimed PostgreSQL ingestion job provides canonical tenant and document- version identity. Normalization returns the source document id and pipeline version alongside the validated normalized artifact. The vector-ingestion service receives that lineage, deterministic metadata, the normalized document, and a provider-neutral vector index. It returns the persisted manifest id, total point count, and created/reused counts; it does not return partial success.

```text
claimed job
  -> normalized artifact + document/version/pipeline lineage
  -> metadata + WikiRAG generation/page artifacts
  -> HierarchicalChunker.chunk(normalized document)
  -> ChunkManifestService.persist(...) -> object + PostgreSQL commit
  -> build dense requests -> bounded dense embedding batch
  -> build sparse requests -> bounded sparse embedding batch
  -> zip chunks + validated representations
  -> VectorPointRequest.build_point()
  -> VectorIndex.upsert(point) for every parent/child chunk
  -> vector-ingestion result
  -> consumer commits job succeeded
  -> Redis acknowledge
```

Failure branches: normalized lineage mismatch, empty/invalid chunking result, chunk-manifest object or metadata failure, dense/sparse provider failure, malformed provider output, collection-geometry mismatch, Qdrant schema/dependency failure, point conflict/corruption, partial vector completion followed by retry, and foreign-tenant source access.

Trade-off: Each ingestion now performs chunking, two bounded embedding passes, and one upsert per emitted parent/child chunk, increasing job duration. The deterministic hash providers prove orchestration rather than semantic quality. No canonical embedding rows, cross-process compare-and-set, bulk Qdrant write, deletion, or performance claim is introduced. Regeneration reuses source-identical vector points rather than creating model-identical duplicates.

References: `implementation.md` Slice 5.9, `decisions.md` D-040, `flow.md` F-039.

Quiz topics from the original checkpoint: explain why the manifest commits before Qdrant, how partial projection retries remain idempotent, why provider/dependency failures differ from invalid lineage/configuration, how job-success/Redis-ack ordering protects searchable completeness, and a two-sentence interview explanation.

1. Concept: what problem does “Activate chunk and vector projection in the durable worker” solve, and why is its boundary necessary?
2. Execution: trace the entry point through its functions and data stores to the output above. Who owns the transaction or state?
3. Failure: choose one failure branch above; explain what is persisted, what is returned, and whether retry is safe.
4. Trade-off: defend the choice above against one rejected alternative. When would you change it?
5. Interview: explain this slice in two sentences, naming its proof and one limitation. What can you honestly claim today?

Learner answers: pending.

## Slice 6.1 — Define tenant-scoped candidate retrieval contracts

Status: unanswered; pause explicitly overridden.

Objective: establish a provider-neutral, measurable boundary that normalizes and validates a tenant-owned search request, embeds the query with the configured dense/sparse providers, and returns independently ranked dense and sparse candidate lists before any fusion or reranking.

Explanation: Separating candidate generation from fusion preserves explainability and gives the next Qdrant slice a stable port. Stable tie-breaking makes tests and future evaluations replayable. Bounded filters and candidate counts limit accidental or abusive fan-out. Returning metadata rather than chunk text respects the current Qdrant projection; canonical text resolution is a later retrieval stage.

Input/output: The caller supplies a tenant id obtained from trusted identity context, raw query text, bounded filters, retrieval mode, and candidate limit. The service returns the normalized query/checksum and zero, one, or two independently ranked candidate lists. Each candidate contains validated provenance metadata, leg, score, and rank but no canonical chunk text.

```text
tenant id + raw query + filters + mode + limit
  -> NFKC normalization + whitespace collapse + validation
  -> deterministic UTF-8 query checksum
  -> dense mode: DenseEmbeddingProvider -> CandidateIndex.search_dense
  -> sparse mode: SparseEmbeddingProvider -> CandidateIndex.search_sparse
  -> hybrid mode: execute both paths and preserve separate results
  -> validate tenant/filter membership, leg, ranks, scores, and limits
  -> CandidateRetrievalResult(dense_candidates, sparse_candidates)
```

Failure branches: invalid tenant UUID, blank or oversized normalized query, NUL/control characters, unsupported mode, duplicate/too-many filter values, invalid candidate limit, provider exception, provider result mismatch, collection geometry mismatch, malformed candidate/result, index dependency failure, and foreign-tenant point.

Trade-off: Hybrid callers receive two lists and must use a later fusion service. The local adapter proves deterministic scoring/filter semantics, not production recall or latency. Query model/collection compatibility remains server configuration; changing an embedding model requires a new configured collection namespace or rebuild. HTTP authorization, Qdrant search, deduplication, fusion, reranking, text resolution, and evaluation remain deferred.

References: `implementation.md` Slice 6.1, `decisions.md` D-041, `flow.md` F-040.

Quiz topics from the original checkpoint: explain candidate generation versus fusion/reranking, why tenant scope is server-owned, how dense and sparse scores differ, why stable tie-breaking matters, how filter bounds prevent abuse, and a two-sentence interview explanation. The authorized sequential set overrides this pause before Slice 6.2; these concepts remain deferred rather than learner-verified.

1. Concept: what problem does “Define tenant-scoped candidate retrieval contracts” solve, and why is its boundary necessary?
2. Execution: trace the entry point through its functions and data stores to the output above. Who owns the transaction or state?
3. Failure: choose one failure branch above; explain what is persisted, what is returned, and whether retry is safe.
4. Trade-off: defend the choice above against one rejected alternative. When would you change it?
5. Interview: explain this slice in two sentences, naming its proof and one limitation. What can you honestly claim today?

Learner answers: pending.

## Slice 6.2 — Activate named-vector Qdrant candidate search

Status: unanswered; pause explicitly overridden.

Objective: implement the verified `CandidateIndex` boundary with live Qdrant named dense and sparse queries while enforcing tenant, optional metadata, and representation-identity filters inside the database request.

Explanation: Authorization and compatibility are data-selection constraints, not post-query cleanup. Pushing them into Qdrant prevents cross-tenant results from entering the candidate set and keeps incompatible model generations from consuming the top-k budget. The application port remains vendor-neutral while the adapter owns Qdrant-specific named vectors and filter models.

Input/output: The verified candidate service supplies trusted tenant scope, one validated dense or sparse query representation, immutable optional filters, and a bounded limit. The adapter returns a tuple of validated application candidates with Qdrant similarity scores and provenance payloads.

```text
tenant + query representation + filters + limit
  -> validate type, geometry, filter contract, and limit
  -> Qdrant Filter.must(
       tenant,
       leg provider/model/config identity,
       optional fields
     )
  -> AsyncQdrantClient.query_points(
       using="dense" + float vector
       OR using="sparse" + SparseVector,
       with_payload=true,
       with_vectors=false
     )
  -> validate payload and canonical UUID point id
  -> sort by descending score then point id
  -> contiguous SearchCandidate tuple
  -> CandidateRetrievalService revalidates tenant/filter/identity/result shape
```

Failure branches: non-UUID tenant, wrong query type or geometry, invalid filters/limit, missing collection, network/server error, malformed or absent payload, non-UUID point id, non-finite score, duplicate point, wrong tenant/filter/representation payload, and incompatible payload-index type.

Trade-off: The collection schema grows from 10 to 16 payload indexes, increasing indexing and storage cost. Dense and sparse modes each make one Qdrant round trip, while hybrid mode currently makes two sequential calls. Stable ordering applies to the returned window; equal-score points outside Qdrant's top-k boundary may still depend on engine selection. Fusion, text loading, reranking, HTTP access, quality evaluation, sharding, and performance remain deferred.

References: `implementation.md` Slice 6.2, `decisions.md` D-042, `flow.md` F-041.

Quiz topics from the original checkpoint: explain why tenant and representation identity are database filters, how UUID OR filters differ from keyword `MatchAny`, why payloads but not vectors return, where dependency versus integrity failures arise, and a two-sentence interview explanation. This pause was explicitly overridden for Slice 6.3.

1. Concept: what problem does “Activate named-vector Qdrant candidate search” solve, and why is its boundary necessary?
2. Execution: trace the entry point through its functions and data stores to the output above. Who owns the transaction or state?
3. Failure: choose one failure branch above; explain what is persisted, what is returned, and whether retry is safe.
4. Trade-off: defend the choice above against one rejected alternative. When would you change it?
5. Interview: explain this slice in two sentences, naming its proof and one limitation. What can you honestly claim today?

Learner answers: pending.

## Slice 6.3 — Fuse candidate legs with explainable RRF

Status: unanswered; pause explicitly overridden.

Objective: turn the independently ranked dense and sparse candidate lists into one deterministic, deduplicated ordering without comparing their provider-specific raw-score scales.

Explanation: RRF combines ordering evidence without assuming cosine and sparse-dot scores are calibrated. A configurable `k` controls how sharply early ranks dominate, while contribution records make every fused score auditable. Deduplication by immutable point id is exact and avoids prematurely collapsing different chunks or document versions.

Input/output: The caller supplies one validated `CandidateRetrievalResult`. The pure service returns query/tenant lineage, the configured `rrf_k` and result limit, and one contiguous fused candidate list with exact provenance and per-leg explanation.

```text
CandidateRetrievalResult(dense list, sparse list)
  -> reconstruct/revalidate complete input contract
  -> for dense candidates in rank order:
       contribution = 1 / (k + rank)
       create or update point-id accumulator
  -> repeat for sparse candidates
  -> same id requires exact payload equality
  -> sum each point's contributions
  -> sort by (-fused_score, best_source_rank, point_id)
  -> apply fused_limit
  -> assign contiguous fused ranks
  -> validate and return ReciprocalRankFusionResult
```

Failure branches: non-result input, bypass-constructed invalid candidate result, cross-leg same-id payload conflict, invalid RRF `k`, invalid fused limit, malformed contribution, nonfinite score, duplicate fused id, tenant mismatch, and noncontiguous result rank.

Trade-off: Rank magnitudes, not score margins, drive the baseline, so information about a very large score gap is intentionally discarded. Larger `k` flattens rank differences; smaller `k` emphasizes top positions. The service has no I/O and does not prove retrieval quality. Weighted fusion, parent/document collapsing, text loading, reranking, APIs, and evaluation remain deferred.

References: `implementation.md` Slice 6.3, `decisions.md` D-043, `flow.md` F-042.

Quiz topics from the original checkpoint: explain why rank fusion avoids raw-score comparability, calculate one candidate's RRF score by hand, describe cross-leg deduplication and payload-conflict behavior, explain the `k` trade-off and deterministic tie-break, and give a two-sentence interview explanation. This pause is active before the next slice.

1. Concept: what problem does “Fuse candidate legs with explainable RRF” solve, and why is its boundary necessary?
2. Execution: trace the entry point through its functions and data stores to the output above. Who owns the transaction or state?
3. Failure: choose one failure branch above; explain what is persisted, what is returned, and whether retry is safe.
4. Trade-off: defend the choice above against one rejected alternative. When would you change it?
5. Interview: explain this slice in two sentences, naming its proof and one limitation. What can you honestly claim today?

Learner answers: pending.

## Slice 6.6 — Bounded cross-encoder reranking

Status: unanswered; checkpoint overridden through Phase 9.
Objective: improve the ordering of retrieved canonical passages.
Explanation: a bi-encoder embeds query and document separately; a cross-encoder
reads a pair jointly and produces a relevance score. Candidate retrieval finds
a small set first because pair scoring across the whole corpus is expensive.
Flow: RerankingService validates tenant, text and candidate limits, then calls
the provider under a timeout; validated scores sort passages with stable ties.
Source references survive the reorder. Only one CPU inference runs per adapter.
A timed-out thread cannot be killed safely, so later calls fail busy until it ends.
Missing weights never silently turn reranking into a lexical heuristic.
Trade-off: extra precision potential costs latency and bounded candidate recall;
512-token model truncation can omit later passage details.
Evidence: 9 focused offline cases passed; full Qdrant-enabled suite: 323 passed, 4 skips. Ruff, mypy (102 files), lock and diff passed. A separate real CPU inference passed on two query/passage pairs with cross-encoder/ms-marco-MiniLM-L2-v2 pinned at 1b5cd67b15209f24824c50370e0397743aa9b787; relevant Paris evidence ranked above the banana distractor. This is a smoke test, not a relevance benchmark.

1. How does a cross-encoder differ from embedding similarity and RRF?
2. Trace canonical evidence through pair scoring and explain retained source ranks.
3. Why must a timed-out CPU inference retain its ownership slot?
4. How do candidate count and truncation trade recall for cost?
5. Give an interview explanation separating real model smoke proof from measured quality.

Learner answers: pending.

## Slice 6.7 — Authenticated canonical search

Status: unanswered; pause overridden through Phase 9.
Objective and design: Expose bounded search with no client-controlled tenant. Authorization precedes retrieval; only current sources with succeeded ingestion are resolved. Optional reranking is explicit and unavailable models fail, never fall back. Alternatives rejected: trust projection text or uploaded status. Trade-off: source checks add I/O and stale projections may produce fewer hits.
Execution and failures: POST /api/v1/search -> JWT/current membership -> SearchBody.to_request -> SearchService.search -> candidate retrieval -> RRF -> canonical dedup -> readiness/current/tenant joins -> checked object/chunk -> optional pair scoring -> SearchResult -> checksum-only success audit and commit. Request-owned Qdrant client closes in finally; no schema provisioning. 422 invalid controls, 401/403 identity, 503 dependency/integrity/model failure; absent/stale candidates are skipped. Audit failure rolls back and fails closed.
Files: application/search.py, evidence.py, repositories/chunk_artifacts.py; API search_routes.py/search_dependencies.py; core/config.py; apps/api/tests/test_search.py.
Evidence: 10 focused API/component tests passed; Qdrant-enabled full suite: 333 passed, 4 external/model skips. Ruff, mypy (106 files), diff passed. API source reads exercised SQLite/local objects/in-memory ranked candidates; live Qdrant legs covered by the existing full-suite integrations. New PostgreSQL source reads are not yet claimed.

1. Why is authentication insufficient without current membership and source authorization?
2. Trace search through all five retrieval stages and the audit commit.
3. What happens when indexing finishes but ingestion has not committed success?
4. Why skip stale evidence but fail on corrupted canonical bytes?
5. Explain the endpoint and its proof in an interview without claiming semantic quality.

Learner answers: pending.

### Slice 6.7 final dependency review evidence

The request-owned Qdrant client now disables the SDK's synchronous constructor
compatibility probe. A dependency-level test verifies this flag, no schema calls,
and client cleanup. Worker provisioning retains its compatibility behavior.
Final proof: 11 focused cases; 334 passed/4 skipped in the Qdrant-enabled suite;
Ruff, mypy (106 files), diff checks passed. This supersedes the earlier 333 count.

## Slice 6.8 — Reproducible retrieval ablation

Status: unanswered; pause overridden through Phase 9.
Objective and design: Measure three production baseline modes and optional real cross-encoder against authored qrels, with fixed candidate window/cutoff and dataset checksum. Reject invalid labels/ranking duplicates. Alternatives rejected: invented uplift or heuristic pretending to be reranking. Trade-off: this tiny development fixture cannot establish generalization or production performance.
Execution and failures: evaluation_cli -> validate EvaluationDataset -> normalize/chunk ten passages -> hash dense/sparse embeddings -> in-memory candidate adapter -> per-query dense/sparse/hybrid RRF -> optional pinned CPU pair scoring -> per-query Recall@3/MRR@3/nDCG@3 -> macro averages and JSON. No service mutations; malformed corpus, missing model or invalid scores fail explicitly.
Files: application/evaluation.py, evaluation_cli.py, evals/retrieval.json, evals/retrieval-results-2026-09-04.json, test_evaluation.py.
Evidence: 3 focused tests; 337 passing/4 skipped Qdrant-enabled full suite; Ruff/mypy (109 files)/diff passed. Actual authored corpus:10 documents/8 queries,k=3,window=10. Dense Recall/MRR/nDCG=0.375/0.3125/0.328866; sparse=1.0/1.0/0.997855; hybrid=0.875/0.8125/0.826721; real pinned reranker=0.9375/1.0/0.989665. Sparse outperformed hybrid and reranked recall/nDCG on this fixture. Default dense remains hash-based, not semantic. Reproduce via README CLI.

1. What do Recall@k, MRR and nDCG each measure?
2. Trace corpus text through each ablation and explain why the candidate window is fixed.
3. Why are duplicate rankings or missing qrel documents invalid?
4. What does sparse beating hybrid tell us about adding a weak dense signal?
5. How would you explain these numbers without implying held-out enterprise accuracy?

Learner answers: pending.

## Slice 7.1 — Canonical evidence-backed graph artifacts

Status: unanswered; pause overridden through Phase 9.
Objective and design: Persist immutable graph JSON in PostgreSQL before deriving Neo4j. Normalize entity names with Unicode NFKC/casefold/space collapse and tenant-specific IDs; parse four explicit annotation predicates, not arbitrary NLP. Keep exact parent-chunk quotation and offsets. Alternative rejected: Neo4j as canonical truth, unsupported inferred facts. Trade-off: limited extraction recall, no synonym/entity-type disambiguation; artifacts cap 1,000 entities/facts.
Execution and failures: WikiIngestionHandler -> vector manifest -> explicit async job refresh (artifact replay may rollback/expire ORM state) -> KnowledgeArtifactService.build -> tenant/version/normalized joins -> checksum-verified manifest -> parent-chunk parser -> validated artifact -> KnowledgeRepository.put nested insert or immutable compare -> consumer success commit -> Redis ack. Repository never commits; graph errors rollback before retry mapping. Foreign sources fail before objects; corruption/conflicts fail closed.
Files: application/knowledge.py, wiki_ingestion.py, repositories/knowledge.py, models.py, migrations/versions/0010_knowledge_artifacts.py, test_knowledge.py, test_knowledge_postgres.py.
Evidence: 4 focused graph cases and 21 worker-consumer cases passed. PostgreSQL16 isolated instance: migrations0001–0010 applied; alembic check no upgrade drift (existing cyclic document/version FK warning). Live PostgreSQL non-superuser worker graph commit/current canonical search/replay passed. Combined PostgreSQL+Qdrant suite343 passed,3 skips; Ruff/mypy113 files/diff passed. No existing database volumes changed. Neo4j not yet exercised.

1. Why are canonical graph artifacts separate from Neo4j projections?
2. Trace one annotated relationship into entity IDs, a fact and its evidence offsets.
3. Why must an expired async ORM job be refreshed after manifest replay?
4. What does explicit annotation extraction gain and lose compared with an LLM extractor?
5. Explain safe graph rebuilding and the current verification boundary in an interview.

Learner answers: pending.

## Slice 7.2a — Tenant/version-scoped generation checksum

Status: unanswered; pause overridden through Phase 9.
Objective and design: Replace global generation-result checksum uniqueness with tenant/version/checksum uniqueness; preserve generation identity/object keys. Content equality cannot confer or block ownership. This supersedes the global-checksum portion of D-025. Alternative rejected: making test text unique would conceal the defect. Trade-off: downgrade now correctly refuses incompatible duplicates rather than deleting them.
Execution and failures: Identical bytes in separate tenant-owned versions -> same generated checksum -> separate scoped generation metadata and object keys -> successful worker graph artifacts/jobs. Alembic0011 replaces only the constraint; no rows removed. Existing repository reads remain tenant/version-scoped.
Files: models.py generation constraint, migrations/versions/0011_generation_checksum_scope.py, test_generation_checksum_scope.py; test_knowledge_postgres.py.
Evidence: New two-tenant worker regression failed before the fix (second job dead_letter), then passed. Focused regression plus repeated real PostgreSQL graph-worker integration:2 passed. PostgreSQL0011 migration and alembic check passed; Ruff/mypy118 files/diff passed. Current Neo4j slice remains pending its final regression.

1. Why is equal content checksum not equal ownership identity?
2. Trace identical bytes through two tenants to separate generation rows.
3. Why did the old global constraint dead-letter the second upload?
4. Why can a safe downgrade fail after introducing scoped duplicates?
5. Give a concise interview explanation of this discovered integration defect and its regression proof.

Learner answers: pending.

## Slice 7.2 — Neo4j projection and canonical rebuild

Status: unanswered; pause overridden through Phase 9.
Objective and design: Use Neo4j6.3 async driver with tenant/id composite uniqueness and artifact/entity/fact nodes; MERGE endpoint links in one bounded transaction, checksum conflicts abort. Parameters never interpolate user Cypher. Read caps1–50 and3s transaction timeout. Explicit tenant confirmation gates bounded projection deletion. Alternative direct unkeyed edges risks duplicates; trade-off: extra fact node hop, canonical verification still needed for stale projections.
Execution and failures: KnowledgeArtifact -> full schema validation -> managed write (10s timeout, bounded retry) -> artifact checksum compare -> entity/fact MERGE -> endpoint links -> commit. neighbors queries only tenant-scoped seed/fact refs. graph_cli pages50 canonical PostgreSQL rows, verifies payload/checksum/identity, replays. clear deletes at most500 matching application-label nodes per transaction for the confirmed tenant; no PostgreSQL data changes.
Files: application/graph_projection.py, infrastructure/neo4j.py, graph_cli.py, core/config.py, pyproject/uv.lock, test_graph_projection.py.
Evidence: 3 focused cases passed including2 real Neo4j5.26 integration cases: idempotence, exact adjacency, bounds, foreign denial, checksum conflict, confirmed clear, identical rebuild. Actual CLI replayed1 PostgreSQL artifact into real Neo4j. Combined PostgreSQL+Qdrant+Neo4j suite347 passed,3 skips; Ruff/mypy118 files/lock/diff passed. D-051 prerequisite resolved repeated-content ingestion defect. Canonical data was never deleted.

1. Why pair MERGE with uniqueness constraints and canonical IDs?
2. Trace an artifact through its atomic transaction and a one-hop read.
3. What happens if a retry supplies a different checksum for the same artifact?
4. Why must graph hits be checked against current canonical evidence later?
5. Describe the tested rebuild without claiming arbitrary graph scale or production availability.

Learner answers: pending.

## Slice 7.3 — Bounded canonical graph-aware retrieval

Status: unanswered; pause overridden through Phase 9.
Objective and design: Expand only entities mentioned in canonically verified seed passages. Bounded BFS:1–2hops,10seeds,20visited nodes,10neighbors/node,20unique graph passages,10s overall timeout; seed matching caps32,000 characters. Validate canonical fact/endpoints/current source/quote before extending a branch. Text-first source identity merge preserves unique passages. Optional graph_enabled wires worker/API; requests combining graph with optional filters are explicitly rejected until filter semantics are implemented, never widened.
Execution and failures: Search -> canonical text hits -> SourceEvidence contract -> verify current/succeeded source -> seed entities from same-manifest canonical artifacts -> tenant-only Neo4j adjacency -> validate graph artifact/checksum/endpoints -> source metadata/object/chunk/quote -> next BFS frontier -> deduped combined evidence. Stale branches stop; foreign/disconnected/corrupt refs fail closed. Worker stages graph artifact and optional projection before consumer commit/ack; projection outage rolls back graph row and remains retryable.
Files: source_evidence.py, graph_retrieval.py, search.py, repositories/knowledge.py, API search dependency/route, worker wiring/config, test_graph_retrieval.py and source fixture helper.
Evidence: 6 focused cases passed including live Neo4j across3 linked documents. One-hop versus two-hop distinction and connected passages missing initial text/dense-only result proven; stale intermediate cannot extend frontier; foreign/corrupt rejection, filter/hop controls and retryable graph outage/no ack proven. PostgreSQL+Qdrant+Neo4j full suite353 passed,3 skips; Ruff/mypy121 files/diff passed. General NLP extraction/graph ranking quality/scale remain unverified.

1. How does BFS branching grow, and which six bounds constrain this implementation?
2. Trace a projected edge through canonical artifact, current manifest and exact quote verification.
3. Why must a stale intermediate fact stop traversal rather than merely disappear from the answer?
4. Why reject graph plus filters until their semantics are implemented?
5. Explain the three-document proof and distinguish it from broad semantic-retrieval quality.

Learner answers: pending.

## Slice 8.1 — Bounded extractive generation and real Ollama provider

Status: unanswered; pause overridden through Phase 9.
Objective and design: An LLM selects exact source quotations using request-local E1–E8 labels. The server maps these to canonical identities and offsets and renders only supported quotes. Limit 8 passages, 6000 serialized UTF-8 prompt/schema bytes, two attempts and explicit insufficient-evidence output. This establishes lexical support, not arbitrary paraphrase entailment or relevance accuracy. Ollama uses an expected model digest checked before/after inference; mutable-tag ABA races are not covered. Alternative free-form prose was rejected until claim-level entailment is proven. HTTPx is a runtime dependency for bounded schema-validated local model calls.
Execution and failures: pack_context validates normalized query, unique tenant-owned source models and byte budget -> Ollama tags check -> JSON-schema chat -> post-check -> validate_draft matches exact visible quote -> server derives citation span and text. Empty evidence bypasses the model. Timeout/network/remote protocol/429/5xx get at most two attempts; malformed output, invented quotes and drift fail closed. Database existence/current-source checks are the next workflow slice, not part of this provider contract.
Files: application/answers.py; infrastructure/ollama.py; apps/worker/tests/test_answers.py; pyproject.toml; uv.lock.
Evidence: 9 focused tests passed including real qwen2.5:7b supported and unsupported questions (16.98s). PG/Qdrant/Neo4j-enabled suite: 361 passed, 4 skips, 2 warnings (19.54s); model smoke separately enabled. Ruff/mypy 124 files passed. Digest expected 845dbda0ea48ed749caafd9e6037047aa19acfcfd82e704d7ca97d631a0b697e; no model files or aliases changed. No quality/production latency claim.

1. Why is exact quotation support stronger than citation syntax, yet weaker than proving answer relevance?
2. Trace E1 from prompt passage to canonical document/version/chunk and offset.
3. Which failures retry, and why do invented citations not retry?
4. What does the 6000-byte budget guarantee, and what does it not measure?
5. Explain the mutable-tag provenance limitation and this slice honestly in an interview.

Learner answers: pending.

## Slice 8.2 — Durable typed LangGraph answer workflow

Status: unanswered; pause overridden through Phase 9.
Objective and design: Nine explicit stages reuse bounded canonical retrieval, RRF/deduplication, optional Neo4j expansion and common-source reranking, then context packing, generation and current-source validation. State is JSON-compatible and capped at 1MiB; heavy intermediate fields are cleared after use. PostgreSQL AsyncPostgresSaver has its own ow_checkpoints schema and operator-only setup/grant CLI. Tenant/user/run-derived keys and provider identity are checked on resume. Inherited external tracing is disabled. Raw workflow is trusted-service-only; API live membership checks/concurrency belong to8.3. Alternative custom persistence would duplicate an established library; in-memory storage alone cannot survive restart.
Execution and failures: authorize -> retrieve -> fuse -> resolve -> graph -> rerank -> context -> generate -> validate. Each completed stage is synchronously checkpointed. Transient generation failure preserves context and next=generate. A fresh connection resumes only generate/validate. Canonical sources are reread before model/reranker use and after generation; invalidated source blocks result. Result reads revalidate citations again. Empty evidence follows8.1 refusal path. State/private DB content requires retention handling in9; no public raw checkpoint API.
Files: application/workflow.py; application/reranking.py; infrastructure/checkpoints.py; checkpoint_cli.py; apps/worker/tests/test_workflow.py; pyproject.toml; uv.lock.
Evidence: 6 focused tests passed (3.94s), including fresh real PostgreSQL connection resume/read/delete under non-superuser openwikirag_rls_test, stale source denial, user/tenant/provider isolation, and graph-source rerank/nonfinite rejection. Service-enabled full suite 367 passed/4 skips/1 warning (24.82s). Ruff/mypy128 files, lock, diff, checkpoint CLI/grants and Alembic drift check passed. LangGraph1.2.11, postgres-checkpointer3.1.2, psycopg3.3.5. Independent reviewer did not complete inspection; no independent clean-review claim.

1. How does a checkpoint differ from an answer artifact or long-term memory?
2. Which nodes run after a timeout, and why can the earlier retrieval be reused?
3. Why must canonical source and current authorization be rechecked after restart?
4. What can repeat after a crash, and why is this not exactly-once model execution?
5. Explain the separate checkpoint schema, sensitive-state retention, and bounded-stage design in an interview.

Learner answers: pending.
## Slice 8.3 — Owner-private persisted answer runs and validated SSE

Status: unanswered; pause overridden through Phase 9.
Objective and design: preserve a run ID before failure, serialize execution, persist a supported answer, and expose only owner-safe progress/final data.
Execution and failures: create -> owner lookup -> advisory lock -> checkpoint workflow -> validate -> answer commit -> final event. Dependency errors are retryable; invalid/stale/unauthorized output is permanent; a disconnect retains state.
Evidence: 12 focused tests; final PG/Qdrant/Neo4j suite 373 passed/4 opt-in skips after review fixes; real Ollama passed in the prior full run; static/migration proof. Socket-level load is unverified.

1. Why persist a pending run before retrieval/model inference?
2. Trace timeout, checkpoint resume and final commit.
3. Which threats are handled by owner predicates, live membership checks and the advisory lock?
4. Why is progress plus a validated answer safer than raw token streaming?
5. Explain the two-store consistency window and recovery tradeoff.

Learner answers: pending.
## Slice 9.1 — Owner-private conversations and ordered messages

Status: unanswered; pause overridden through Phase 9.
Objective/design: persist private multi-turn history, preserve deterministic ordering under concurrency, and link answer runs without turning history into authoritative knowledge.
Flow/failures: owner check -> row lock -> capped sequence append/checksum -> commit. Assistant reads revalidate current citations. Foreign admins, corrupt lineage/checksum, stale citations and overflow fail closed.
Evidence: 8 focused tests; full suite 375 passed/4 skips; static/migration checks. Q&A pending.

1. Why is conversation ownership narrower than tenant RBAC?
2. Trace user and assistant appends across run creation, retry and completion.
3. How do row locking, unique sequence and composite owner FKs prevent different corruption classes?
4. Why must stored assistant citations be revalidated, and why is chat history not enterprise truth?
5. Explain the 200-message hard bound versus pagination/summarization alternatives.

Learner answers: pending.
## Slice 9.2 — Bounded context, explicit memory and retention (in progress)

Status: unanswered; quiz pause overridden through Phase 9. Implementation verification remains in progress.
Objective: preserve useful conversation context while making deletion enforceable across database rows and workflow checkpoints.
Design: the latest six messages produce a deterministic summary capped at 2,000 UTF-8 bytes. Explicit user memory is private context. Answer quotes still require canonical source evidence. Deletion hides records immediately and sets a seven-day purge deadline. Purge deletes workflow checkpoints before deleting their owning conversation transaction; a failure can be retried.
Evidence so far: PostgreSQL integration proved preservation before deadline and deletion of conversation, answer run, memory and actual LangGraph checkpoint after deadline. A separate regression proves deletion during inference blocks publication.

1. Why can conversation context clarify a question without becoming citation evidence?
2. Trace soft deletion through the seven-day deadline and checkpoint/database purge.
3. What happens if checkpoint deletion succeeds but the database transaction fails?
4. What information does a deterministic bounded summary lose?
5. How would you explain explicit memory consent and deletion guarantees in an interview?

Learner answers: pending.
### Slice 9.2 verification update

379 tests passed with PostgreSQL/Qdrant/Neo4j enabled, four opt-in skips;139 source files typed. Memory fingerprints prevent using withdrawn preferences. Purge clears saved context and checkpoints before removing memory tombstones. Explain why owner-wide invalidation is conservative, why a tombstone remains during partial cleanup, and why deployment must schedule the purge CLI. All answers remain pending.
