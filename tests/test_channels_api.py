"""Integration tests for POST/GET /channels
(services/orchestrator/app/api/channels.py).

Real Postgres, real FastAPI app — no mocked DB layer, matching this
project's stated integration-testing approach. Every channel created
here is deleted in a fixture teardown, since there is no automated
cleanup of channels anywhere else in the system and this table has no
unique constraint that would otherwise catch a leftover row.
"""

from collections.abc import Iterator
from uuid import UUID

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def created_channel_ids() -> Iterator[list[str]]:
    ids: list[str] = []
    yield ids
    if not ids:
        return
    from sqlalchemy import delete

    from libs.core.db import sync_session_scope
    from libs.models import Channel

    with sync_session_scope() as session:
        # A Core-level DELETE, not `session.delete(instance)`: the ORM
        # relationship from Channel to VideoIdea/Project has no
        # `passive_deletes=True`, so an ORM-level delete tries to null
        # out those children's (NOT NULL) channel_id columns itself
        # instead of trusting the FK's own `ON DELETE CASCADE` — a
        # pre-existing characteristic of those model relationships, not
        # something this test should work around by changing. A plain
        # DELETE statement bypasses that cascade resolution entirely and
        # lets Postgres's own ON DELETE CASCADE do the cleanup, exactly
        # as the schema is already designed to.
        session.execute(delete(Channel).where(Channel.id.in_(UUID(i) for i in ids)))


def test_create_channel_minimal(client: TestClient, created_channel_ids: list[str]) -> None:
    response = client.post("/channels", json={"name": "Test Channel"})
    assert response.status_code == 201, response.text
    body = response.json()
    created_channel_ids.append(body["id"])

    assert body["name"] == "Test Channel"
    assert body["niche"] is None
    assert body["is_active"] is True
    assert "id" in body
    assert "created_at" in body


def test_create_channel_with_all_fields(client: TestClient, created_channel_ids: list[str]) -> None:
    response = client.post(
        "/channels",
        json={
            "name": "Full Channel",
            "niche": "science education",
            "persona_config": {"tone": "curious", "default_privacy_status": "unlisted"},
            "youtube_channel_id": "UC1234567890",
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()
    created_channel_ids.append(body["id"])
    assert body["niche"] == "science education"

    # persona_config/youtube_channel_id aren't part of ChannelOut (same
    # response shape GET /channels already used before this endpoint
    # existed) — confirm they were nonetheless actually persisted.
    from libs.core.db import sync_session_scope
    from libs.models import Channel

    with sync_session_scope() as session:
        channel = session.get(Channel, UUID(body["id"]))
        assert channel is not None
        assert channel.persona_config == {"tone": "curious", "default_privacy_status": "unlisted"}
        assert channel.youtube_channel_id == "UC1234567890"


def test_create_channel_missing_name(client: TestClient) -> None:
    response = client.post("/channels", json={"niche": "science education"})
    assert response.status_code == 422


def test_create_channel_blank_name(client: TestClient) -> None:
    response = client.post("/channels", json={"name": ""})
    assert response.status_code == 422


def test_create_channel_name_too_long(client: TestClient) -> None:
    response = client.post("/channels", json={"name": "x" * 256})
    assert response.status_code == 422


def test_created_channel_is_immediately_usable_for_a_goal(
    client: TestClient, created_channel_ids: list[str], require_redis: None
) -> None:
    """The whole point of this endpoint: a channel created through the API
    (no direct DB access) must be immediately usable as `POST /goals`'s
    `channel_id` — the one thing every prior workflow required a
    manually-inserted row for. Needs Redis too, since submitting a goal
    dispatches the first pipeline stage over Celery.
    """
    create_response = client.post("/channels", json={"name": "Goal-Ready Channel"})
    assert create_response.status_code == 201
    channel_id = create_response.json()["id"]
    created_channel_ids.append(channel_id)

    goal_response = client.post(
        "/goals", json={"channel_id": channel_id, "goal": "a video about how engines work"}
    )
    assert goal_response.status_code == 202, goal_response.text
    assert "project_id" in goal_response.json()


def test_list_channels_includes_created(client: TestClient, created_channel_ids: list[str]) -> None:
    create_response = client.post("/channels", json={"name": "Listed Channel"})
    assert create_response.status_code == 201
    channel_id = create_response.json()["id"]
    created_channel_ids.append(channel_id)

    list_response = client.get("/channels")
    assert list_response.status_code == 200
    ids = {c["id"] for c in list_response.json()}
    assert channel_id in ids
