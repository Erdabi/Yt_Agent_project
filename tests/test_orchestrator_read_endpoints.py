"""Regression check for the orchestrator's pre-existing read surface —
confirms adding POST /channels didn't change how the sibling endpoints in
the same app/router behave.
"""

from fastapi.testclient import TestClient


def test_health(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["database"] is True


def test_list_channels(client: TestClient) -> None:
    response = client.get("/channels")
    assert response.status_code == 200
    assert isinstance(response.json(), list)


def test_list_projects(client: TestClient) -> None:
    response = client.get("/projects")
    assert response.status_code == 200
    assert isinstance(response.json(), list)


def test_list_jobs(client: TestClient) -> None:
    response = client.get("/jobs")
    assert response.status_code == 200
    assert isinstance(response.json(), list)
