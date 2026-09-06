"""Fail-closed production configuration contracts."""

from typing import Any

import pytest
from pydantic import ValidationError

from openwikirag.core.config import Settings


def _production_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "environment": "production",
        "auth_mode": "oidc",
        "oidc_issuer": "https://issuer.example",
        "oidc_audience": "openwikirag-api",
        "oidc_jwks_url": "https://issuer.example/.well-known/jwks.json",
        "object_storage_backend": "s3",
        "object_store_endpoint_url": "https://s3.example",
        "jwt_secret": "a-production-secret-that-is-not-a-placeholder",
        "database_url": "postgresql+asyncpg://app:database-secret@db.example/app",
        "migration_database_url": "postgresql+asyncpg://migration:migration-secret@db.example/app",
        "neo4j_password": "graph-production-secret",
    }
    values.update(overrides)
    return Settings(**values)


def test_production_requires_an_s3_compatible_object_backend() -> None:
    with pytest.raises(ValidationError, match="S3-compatible"):
        _production_settings(object_storage_backend="filesystem")


def test_production_endpoint_error_does_not_echo_the_rejected_url() -> None:
    endpoint = "http://private-storage.example:9000"
    with pytest.raises(ValidationError, match="HTTPS") as error:
        _production_settings(object_store_endpoint_url=endpoint)

    assert endpoint not in str(error.value)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("jwt_secret", "openwikirag-development-secret-change-me-now", "JWT secret"),
        (
            "database_url",
            "postgresql+asyncpg://app:openwikirag-dev-password@db.example/app",
            "database credentials",
        ),
        (
            "migration_database_url",
            "postgresql+asyncpg://migration:openwikirag-dev-password@db.example/app",
            "database credentials",
        ),
    ],
)
def test_production_rejects_placeholder_credentials_without_echoing_them(
    field: str, value: str, message: str
) -> None:
    with pytest.raises(ValidationError, match=message) as error:
        _production_settings(**{field: value})

    assert value not in str(error.value)


def test_active_graph_credentials_cannot_use_a_development_placeholder() -> None:
    placeholder = "openwikirag-dev-password"
    with pytest.raises(ValidationError, match="Neo4j credentials") as error:
        _production_settings(graph_enabled=True, neo4j_password=placeholder)

    assert placeholder not in str(error.value)


def test_valid_production_settings_allow_ambient_s3_identity() -> None:
    settings = _production_settings(graph_enabled=False)

    assert settings.auth_mode == "oidc"
    assert settings.object_storage_backend == "s3"
    assert settings.object_store_access_key_id == ""


def test_development_defaults_and_inactive_graph_secret_remain_usable() -> None:
    settings = Settings()
    production_without_graph = _production_settings(
        graph_enabled=False,
        neo4j_password="openwikirag-dev-password",
    )

    assert settings.auth_mode == "local"
    assert settings.object_storage_backend == "filesystem"
    assert production_without_graph.neo4j_password == "openwikirag-dev-password"
