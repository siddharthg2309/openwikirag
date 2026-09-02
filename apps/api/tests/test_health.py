from fastapi.testclient import TestClient

from apps.api.app.main import app

client = TestClient(app)


def test_healthz_reports_process_liveness() -> None:
    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_readyz_reports_configuration_readiness() -> None:
    response = client.get("/readyz")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ready",
        "checks": {"configuration": "ok"},
    }
