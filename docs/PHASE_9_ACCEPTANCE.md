# Phase 9 acceptance evidence

Date: 2026-09-05. Audit in progress; this is not a full-project completion claim.

| Requirement | Evidence | Status |
| --- | --- | --- |
| Conversation/message persistence | test_conversations.py real PostgreSQL fresh session and concurrent ordering | Verified |
| Private conversation ownership | API/service predicates and composite owner FK negative tests | Verified |
| Durable workflow state | test_workflow.py fresh PostgreSQL saver resumes generation | Verified |
| Bounded summary | latest six validated messages, 2,000 UTF-8 byte cap; multibyte PostgreSQL boundary test; provider-context assertions | Verified |
| Explicit memory and deletion | test_answer_runs.py withdrawal-before-inference and deletion-during-inference | Verified |
| Cross-store purge | test_retention.py actual PostgreSQL/LangGraph deadline and deletion | Verified |
| Scoped cache | test_score_cache.py key dimensions/Redis TTL; workflow reuse test | Verified |
| Conversation resumes after API restart | fresh-session persistence is proven; actual separate API-process restart experiment pending | Incomplete |

Latest regression: 384 passed, two optional model tests skipped; one known local-Qdrant warning. Strict typing passed for141 source files.

Pre-existing project limitations remain explicit: native OCR proof, live wiki-generation activation, semantic dense embeddings, free-text graph extraction and production deployment are not established by these Phase9 tests. Resume readiness still requires learner answers and measured production-quality evidence where claimed.
