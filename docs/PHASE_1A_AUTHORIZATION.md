# Phase 1A: Authorization Core

## Goal

Establish the security policy that every later application use case will use. This slice intentionally does not implement login, token issuance, or database persistence yet. It proves the policy independently of FastAPI and PostgreSQL.

## Policy

OpenWikiRAG uses tenant-scoped RBAC:

| Role | Permissions |
| --- | --- |
| Viewer | Read documents |
| Editor | Read, write, and reindex documents |
| Admin | All document permissions, member management, and audit-log access |
| Operator | Platform-level permissions; cross-tenant access is explicitly treated as privileged |

The policy evaluator applies checks in this order:

1. The principal must be active.
2. A tenant-owned resource must belong to the principal's tenant, except for an operator.
3. The principal's role must contain the requested permission.

This order gives fail-closed behavior: an inactive user cannot bypass access checks, and a valid permission in tenant A cannot be used to read tenant B.

## Why this is separate from the API

Authorization is business policy, not HTTP plumbing. Keeping it in a small domain service means:

- API handlers do not duplicate role logic;
- workers can apply the same policy to background jobs;
- unit tests run without external services;
- a future OIDC provider or policy engine can replace authentication without rewriting authorization use cases.

## Important production follow-up

The current `Principal` is now populated by the protected `/api/v1/me` path: the API validates a signed local-development access token, loads the current membership from PostgreSQL, and ignores token role claims. Role claims should not be trusted forever: membership changes must take effect through token expiry, revocation, or a current membership lookup. Local password registration/login, rotating refresh tokens, audit events, Alembic migrations, and PostgreSQL RLS are documented in `implementation.md`, `decisions.md`, and `flow.md`.

## Phase 1 completion status

The authorization core and identity boundary are implemented and verified with local tests plus a PostgreSQL 16 integration test running under a non-superuser role. Production OIDC/JWKS, Redis-backed rate limiting, and document-table policies remain intentionally deferred to later phases.

## Verification

```bash
uv run pytest apps/api/tests/test_authorization.py
uv run ruff check .
uv run mypy
```
