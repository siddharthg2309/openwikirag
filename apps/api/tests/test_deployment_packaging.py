import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def _compose_service(name: str) -> str:
    compose = (ROOT / "docker-compose.yml").read_text()
    match = re.search(
        rf"^  {re.escape(name)}:\n(?P<body>.*?)(?=^  [a-z][a-z0-9-]*:\n|\Z)",
        compose,
        re.MULTILINE | re.DOTALL,
    )
    assert match is not None, f"Compose service {name!r} is missing."
    return match.group("body")


def test_runtime_image_is_locked_and_non_root() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text()

    assert dockerfile.count("FROM python:3.13-slim-bookworm") == 2
    assert "FROM python:3.13-slim-bookworm AS builder" in dockerfile
    assert "FROM python:3.13-slim-bookworm AS runtime" in dockerfile
    assert "uv sync --locked --no-dev --no-editable" in dockerfile
    assert "COPY --from=builder" in dockerfile
    assert "USER openwikirag:openwikirag" in dockerfile


def test_compose_migration_gates_api_and_worker() -> None:
    migrate = _compose_service("migrate")
    api = _compose_service("api")
    worker = _compose_service("worker")

    assert "alembic upgrade head" in migrate
    assert "openwikirag.checkpoint_cli --grant-role openwikirag_app" in migrate
    for service in (api, worker):
        assert "condition: service_completed_successfully" in service
        assert "app-objects:/data" in service
        assert "read_only: true" in service
        assert "no-new-privileges:true" in service
    assert "healthcheck:\n      disable: true" in worker


def test_compose_uses_container_dns_and_explicit_process_commands() -> None:
    compose = (ROOT / "docker-compose.yml").read_text()
    api = _compose_service("api")
    worker = _compose_service("worker")

    assert "@postgres:5432/" in compose
    assert "OPENWIKIRAG_REDIS_URL: redis://redis:6379/0" in compose
    assert "OPENWIKIRAG_QDRANT_URL: http://qdrant:6333" in compose
    assert 'command: ["uvicorn", "apps.api.app.main:app", "--host", "0.0.0.0"' in api
    assert 'command: ["python", "-m", "apps.worker.app.main"]' in worker
    assert '"${OPENWIKIRAG_API_PORT:-8000}:8000"' in api
    assert "condition: service_healthy" in api
    assert "condition: service_healthy" in worker


def test_runtime_role_bootstrap_covers_existing_and_future_database_objects() -> None:
    init_script = (ROOT / "docker/postgres/init/001-create-app-role.sh").read_text()
    checkpoints = (ROOT / "src/openwikirag/infrastructure/checkpoints.py").read_text()

    for source in (init_script, checkpoints):
        assert "ON ALL TABLES IN SCHEMA public" in source
        assert "ON ALL SEQUENCES IN SCHEMA public" in source
        assert "ALTER DEFAULT PRIVILEGES IN SCHEMA public" in source


def test_clean_smoke_operator_isolates_and_cleans_one_prefixed_project() -> None:
    script = (ROOT / "ops/smoke_compose.sh").read_text()

    assert "openwikirag-smoke-" in script
    assert "validate_port" in script
    assert "wait_for_url" in script
    assert "/healthz" in script
    assert "worker_cycle_completed" in script
    assert "down --volumes --remove-orphans" in script
    assert "trap cleanup EXIT" in script
    assert "docker compose down" not in script
