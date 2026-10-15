"""`monitor`, `next_run_at` and `check_count` on the task read API.

The values come from the task row and nowhere else. These tests seed real
rows, read them back through the real app (real authentication seam, real
routes, in-memory database), and compare each API value with the column it
must come from. Nothing is mocked on the path under test.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.core.owner import CONFIGURED_OWNER_ID
from app.tasks.models import Task
from app.tasks.schemas import TaskMonitorRead, TaskRead
from app.tasks.states import TaskState

pytestmark = pytest.mark.anyio

OTHER = uuid.UUID("bbbbbbbb-0000-0000-0000-00000000bbbb")

#: Synthetic sentinels. The expected value and the path are never to appear.
EXPECTED_SENTINEL = "SENTINEL-EXPECTED-VALUE-7731"
PATH_SENTINEL = "external.content"

SPEC = {
    "condition": {
        "kind": "value", "path": PATH_SENTINEL, "operator": "ne",
        "expected": EXPECTED_SENTINEL,
    },
    "interval_seconds": 900,
}

#: Every TaskRead field before this change. Each must still be present.
PREVIOUS_FIELDS = {
    "id", "owner_id", "conversation_id", "origin", "objective", "state",
    "priority", "deadline", "current_step", "budget", "spent", "error_code",
    "result", "failure_count", "completed_at", "cancelled_at", "created_at",
    "updated_at", "step_count",
}
NEW_FIELDS = {"monitor", "next_run_at", "check_count"}


async def seed(session_factory, **fields) -> uuid.UUID:
    values = {
        "owner_id": CONFIGURED_OWNER_ID,
        "objective": "Watch Gmail for a reply from the dentist",
        "state": TaskState.QUEUED,
    }
    values.update(fields)
    async with session_factory() as session:
        task = Task(**values)
        session.add(task)
        await session.commit()
        return task.id


async def column(session_factory, task_id, name):
    async with session_factory() as session:
        return (await session.execute(
            select(getattr(Task, name)).where(Task.id == task_id)
        )).scalar_one()


def same_moment(api_value, stored) -> bool:
    """The API sends the stored time; SQLite stores it without an offset."""
    if api_value is None or stored is None:
        return api_value is None and stored is None
    parsed = datetime.fromisoformat(api_value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    if stored.tzinfo is None:
        stored = stored.replace(tzinfo=timezone.utc)
    return parsed == stored


async def read_everywhere(client, task_id):
    """The same task as the list, the detail and the activity report show it."""
    listed = (await client.get("/api/tasks", params={"limit": 50})).json()["tasks"]
    detail = (await client.get(f"/api/tasks/{task_id}")).json()
    activity = (await client.get("/api/tasks/activity")).json()
    grouped = [t for answer in activity.values() for t in answer["tasks"]]
    pick = lambda rows: next(t for t in rows if t["id"] == str(task_id))  # noqa: E731
    return {"list": pick(listed), "detail": detail, "activity": pick(grouped)}


# ============================================================================
# Present
# ============================================================================


async def test_a_monitor_is_reported_from_its_row_on_every_read_route(
    client, session_factory
) -> None:
    due = datetime(2026, 10, 14, 9, 30, tzinfo=timezone.utc)
    task_id = await seed(
        session_factory, state=TaskState.RUNNING, monitor=SPEC,
        next_run_at=due, check_count=3,
    )

    for route, body in (await read_everywhere(client, task_id)).items():
        assert body["monitor"] == {
            "kind": "value", "operator": "ne", "interval_seconds": 900,
        }, route
        assert body["check_count"] == 3, route
        assert same_moment(body["next_run_at"], due), (route, body["next_run_at"])
        assert body["next_run_at"] is not None


async def test_the_values_are_the_columns_not_a_recomputation(
    client, session_factory
) -> None:
    """Every value tracks the row: change the row, the API changes with it."""
    task_id = await seed(
        session_factory, monitor=SPEC, next_run_at=datetime(2026, 10, 14, 9, 0, tzinfo=timezone.utc),
        check_count=1,
    )
    first = (await client.get(f"/api/tasks/{task_id}")).json()
    assert first["check_count"] == 1

    later = datetime(2026, 10, 14, 9, 15, tzinfo=timezone.utc)
    async with session_factory() as session:
        row = (await session.execute(select(Task).where(Task.id == task_id))).scalar_one()
        row.check_count = 2
        row.next_run_at = later
        await session.commit()

    second = (await client.get(f"/api/tasks/{task_id}")).json()
    assert second["check_count"] == 2
    assert same_moment(second["next_run_at"], later)
    assert same_moment(second["next_run_at"], await column(session_factory, task_id, "next_run_at"))
    assert second["check_count"] == await column(session_factory, task_id, "check_count")


async def test_the_monitor_exposes_its_shape_and_never_the_condition_text(
    client, session_factory
) -> None:
    task_id = await seed(session_factory, monitor=SPEC)
    response = await client.get(f"/api/tasks/{task_id}")
    assert set(response.json()["monitor"]) == {"kind", "operator", "interval_seconds"}
    assert EXPECTED_SENTINEL not in response.text
    assert PATH_SENTINEL not in response.text
    listed = await client.get("/api/tasks")
    assert EXPECTED_SENTINEL not in listed.text
    assert PATH_SENTINEL not in listed.text
    assert EXPECTED_SENTINEL not in (await client.get("/api/tasks/activity")).text


# ============================================================================
# Absent
# ============================================================================


async def test_an_ordinary_task_reports_no_monitor_no_schedule_and_zero_checks(
    client, session_factory
) -> None:
    task_id = await seed(session_factory, objective="Draft a weekly review")
    for route, body in (await read_everywhere(client, task_id)).items():
        assert body["monitor"] is None, route
        assert body["next_run_at"] is None, route
        assert body["check_count"] == 0, route


async def test_a_scheduled_ordinary_task_has_a_schedule_but_is_not_a_monitor(
    client, session_factory
) -> None:
    due = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)
    task_id = await seed(session_factory, next_run_at=due)
    body = (await client.get(f"/api/tasks/{task_id}")).json()
    assert body["monitor"] is None
    assert same_moment(body["next_run_at"], due)
    assert body["check_count"] == 0


async def test_a_monitor_that_is_not_scheduled_has_a_null_schedule(
    client, session_factory
) -> None:
    """Finished and waiting monitors have no next run; null says so."""
    for state, extra in (
        (TaskState.PLANNED, {}),
        (TaskState.AWAITING_APPROVAL, {}),
        (TaskState.BLOCKED, {}),
        (TaskState.COMPLETED, {"completed_at": datetime.now(timezone.utc)}),
    ):
        task_id = await seed(session_factory, state=state, monitor=SPEC, next_run_at=None, **extra)
        body = (await client.get(f"/api/tasks/{task_id}")).json()
        assert body["next_run_at"] is None, state
        assert body["monitor"] is not None, state


async def test_an_unreadable_stored_spec_is_still_a_monitor_with_unknown_details(
    client, session_factory
) -> None:
    broken = await seed(session_factory, monitor={"condition": {"kind": "nonsense"}, "interval_seconds": 1})
    not_dict = await seed(session_factory, monitor=["not", "an", "object"])
    listed = await client.get("/api/tasks")
    assert listed.status_code == 200
    for task_id in (broken, not_dict):
        body = (await client.get(f"/api/tasks/{task_id}")).json()
        assert body["monitor"] == {"kind": None, "operator": None, "interval_seconds": None}
    assert "nonsense" not in listed.text


# ============================================================================
# Unit: the read model
# ============================================================================


def test_the_monitor_read_model() -> None:
    assert TaskMonitorRead.from_stored(None) is None
    read = TaskMonitorRead.from_stored(SPEC)
    assert (read.kind.value, read.operator.value, read.interval_seconds) == ("value", "ne", 900)
    assert set(TaskMonitorRead.model_fields) == {"kind", "operator", "interval_seconds"}
    blank = TaskMonitorRead.from_stored({"interval_seconds": "soon"})
    assert blank == TaskMonitorRead()


def test_task_read_gained_exactly_three_fields() -> None:
    fields = set(TaskRead.model_fields)
    assert PREVIOUS_FIELDS <= fields
    assert fields - PREVIOUS_FIELDS == NEW_FIELDS
    # Optional, with defaults, so any other producer of a TaskRead keeps working.
    assert TaskRead.model_fields["monitor"].default is None
    assert TaskRead.model_fields["next_run_at"].default is None
    assert TaskRead.model_fields["check_count"].default == 0


def test_openapi_keeps_the_chat_monitor_name_and_publishes_the_task_one_under_its_own() -> None:
    """Two schemas once shared a class name, which made FastAPI rename the
    existing chat one in the published document. They no longer collide."""
    from app.main import create_app

    schemas = create_app().openapi()["components"]["schemas"]

    # The existing chat-turn schema keeps the name it always had.
    assert set(schemas["MonitorRead"]["properties"]) == {"outcome", "task_id"}
    # The task one has its own, and says what it carries.
    assert set(schemas["TaskMonitorRead"]["properties"]) == {"kind", "operator", "interval_seconds"}
    # No module-prefixed disambiguation names were generated for either.
    assert not [name for name in schemas if name.startswith("app__") and "Monitor" in name]
    # Each is referenced from the place it belongs.
    task_monitor = schemas["TaskRead"]["properties"]["monitor"]["anyOf"][0]["$ref"]
    chat_monitor = schemas["ChatResponse"]["properties"]["monitor"]["anyOf"][0]["$ref"]
    assert task_monitor.endswith("/TaskMonitorRead")
    assert chat_monitor.endswith("/MonitorRead")


# ============================================================================
# Compatibility
# ============================================================================


async def test_every_previous_field_is_still_present_with_the_same_shape(
    client, session_factory
) -> None:
    task_id = await seed(session_factory, monitor=SPEC, check_count=2,
                         next_run_at=datetime.now(timezone.utc) + timedelta(minutes=5))
    for route, body in (await read_everywhere(client, task_id)).items():
        assert PREVIOUS_FIELDS <= set(body), (route, PREVIOUS_FIELDS - set(body))
        assert NEW_FIELDS <= set(body), route
        assert body["state"] == "queued"
        assert body["objective"] == "Watch Gmail for a reply from the dentist"
        assert body["owner_id"] == str(CONFIGURED_OWNER_ID)  # unchanged, pre-existing
    detail = (await client.get(f"/api/tasks/{task_id}")).json()
    assert {"steps", "events", "plan"} <= set(detail)


async def test_the_other_task_endpoints_are_unchanged(client, session_factory) -> None:
    task_id = await seed(session_factory, monitor=SPEC)
    assert (await client.get(f"/api/tasks/{task_id}/steps")).status_code == 200
    assert (await client.get(f"/api/tasks/{task_id}/events")).status_code == 200
    assert (await client.get(f"/api/tasks/{task_id}/plan")).status_code == 404  # no plan: as before
    filtered = (await client.get("/api/tasks", params=[("state", "queued"), ("limit", "5")])).json()
    assert filtered["total"] == 1
    assert (await client.get("/api/tasks", params={"limit": 0})).status_code == 422


async def test_cancel_still_confirms_and_reports_the_row(client, session_factory) -> None:
    task_id = await seed(
        session_factory, monitor=SPEC, check_count=2,
        next_run_at=datetime.now(timezone.utc) + timedelta(minutes=10),
    )
    response = await client.post(f"/api/tasks/{task_id}/cancel", json={})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["state"] == "cancelled"
    assert body["cancelled_at"] is not None
    # The response reports the row as it is after the cancel, not as before.
    assert same_moment(body["next_run_at"], await column(session_factory, task_id, "next_run_at"))
    assert body["check_count"] == 2
    assert body["monitor"]["interval_seconds"] == 900
    # Cancelling does not clear the schedule column (existing behaviour,
    # unchanged here), so the API reports it as stored: the state, not this
    # value, says nothing will run.
    assert await column(session_factory, task_id, "next_run_at") is not None
    assert body["next_run_at"] is not None
    # Idempotent, and a finished task is still refused with 409.
    assert (await client.post(f"/api/tasks/{task_id}/cancel", json={})).status_code == 200
    done = await seed(session_factory, state=TaskState.COMPLETED, monitor=SPEC,
                      completed_at=datetime.now(timezone.utc))
    assert (await client.post(f"/api/tasks/{done}/cancel", json={})).status_code == 409
    assert (await client.post(f"/api/tasks/{task_id}/cancel", json={"x": 1})).status_code == 422


# ============================================================================
# Authentication and ownership
# ============================================================================


async def test_the_fields_need_the_token(anon_client, session_factory) -> None:
    task_id = await seed(session_factory, monitor=SPEC, check_count=4)
    for path in ("/api/tasks", f"/api/tasks/{task_id}", "/api/tasks/activity"):
        response = await anon_client.get(path)
        assert response.status_code == 401, path
        assert "check_count" not in response.text
        assert "interval_seconds" not in response.text
    cancel = await anon_client.post(f"/api/tasks/{task_id}/cancel", json={})
    assert cancel.status_code == 401


async def test_another_owners_monitor_is_invisible_and_cannot_be_selected(
    client, session_factory
) -> None:
    mine = await seed(session_factory, monitor=SPEC, check_count=1)
    other_spec = {"condition": dict(SPEC["condition"]), "interval_seconds": 3600}
    theirs = await seed(session_factory, owner_id=OTHER, monitor=other_spec, check_count=9,
                        next_run_at=datetime(2031, 1, 1, tzinfo=timezone.utc))
    forged = {"owner_id": str(OTHER), "owner": str(OTHER)}
    headers = {"X-Owner-Id": str(OTHER)}

    listed = (await client.get("/api/tasks", params=forged, headers=headers))
    assert {t["id"] for t in listed.json()["tasks"]} == {str(mine)}
    assert "2031" not in listed.text
    assert (await client.get(f"/api/tasks/{theirs}", params=forged, headers=headers)).status_code == 404
    activity = (await client.get("/api/tasks/activity", params=forged, headers=headers)).text
    assert str(theirs) not in activity
    assert (await client.post(f"/api/tasks/{theirs}/cancel", json={}, headers=headers)).status_code == 404
    assert await column(session_factory, theirs, "state") is TaskState.QUEUED
