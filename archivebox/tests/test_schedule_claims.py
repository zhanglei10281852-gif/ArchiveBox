#!/usr/bin/env python3
"""Durable per-slot execution claims for CrawlSchedule.

Covers the shared dispatch rules used by both `schedule --run-all` and the
background daemon poll: unique materialization per cron slot, lease mutex,
crash/failure takeover, restart idempotency and disable/delete/reschedule
guards. Everything here runs against a real initialized SQLite archive with
real CLI subprocesses / real DB rows (no mocks or monkeypatches).
"""

from datetime import timedelta

import pytest
from django.utils import timezone

from archivebox.crawls.schedule_util import (
    current_slot_for_schedule,
    next_run_for_schedule,
    validate_schedule,
)
from archivebox.tests.test_orm_helpers import use_archivebox_db

from .conftest import cli_env, run_archivebox_cmd

pytestmark = pytest.mark.django_db(transaction=True)


# ---------------------------------------------------------------------------
# Pure cron slot computation
# ---------------------------------------------------------------------------


def test_current_slot_for_schedule_boundaries():
    from datetime import datetime, timezone as dt_timezone

    utc = dt_timezone.utc
    daily = validate_schedule("daily")
    hourly = validate_schedule("hourly")

    assert current_slot_for_schedule(daily, datetime(2026, 11, 3, 0, 0, tzinfo=utc)) == datetime(2026, 11, 3, 0, 0, tzinfo=utc)
    assert current_slot_for_schedule(daily, datetime(2026, 11, 3, 0, 0, 1, tzinfo=utc)) == datetime(2026, 11, 3, 0, 0, tzinfo=utc)
    assert current_slot_for_schedule(daily, datetime(2026, 11, 2, 23, 59, 59, tzinfo=utc)) == datetime(2026, 11, 2, 0, 0, tzinfo=utc)

    assert current_slot_for_schedule(hourly, datetime(2026, 11, 3, 15, 30, tzinfo=utc)) == datetime(2026, 11, 3, 15, 0, tzinfo=utc)
    assert current_slot_for_schedule(hourly, datetime(2026, 11, 3, 15, 0, tzinfo=utc)) == datetime(2026, 11, 3, 15, 0, tzinfo=utc)

    fives = validate_schedule("*/5 * * * *")
    assert current_slot_for_schedule(fives, datetime(2026, 11, 3, 15, 7, tzinfo=utc)) == datetime(2026, 11, 3, 15, 5, tzinfo=utc)
    assert current_slot_for_schedule(fives, datetime(2026, 11, 3, 15, 10, tzinfo=utc)) == datetime(2026, 11, 3, 15, 10, tzinfo=utc)

    slot = current_slot_for_schedule(daily, datetime(2026, 11, 3, 12, 0, tzinfo=utc))
    assert next_run_for_schedule(daily, slot) == datetime(2026, 11, 4, 0, 0, tzinfo=utc)

    with pytest.raises(ValueError):
        current_slot_for_schedule("not a cron", datetime(2026, 11, 3, 12, 0, tzinfo=utc))


# ---------------------------------------------------------------------------
# Model-level fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def archivebox_db(initialized_archive):
    with use_archivebox_db(initialized_archive):
        yield initialized_archive


def _make_schedule(*, kind="crawl", schedule_expr="daily", url="https://example.com/scheduled-crawl", due=False):
    from archivebox.base_models.models import get_or_create_system_user_pk
    from archivebox.crawls.models import Crawl, CrawlSchedule

    user_id = get_or_create_system_user_pk()
    is_update = kind == "update"
    template = Crawl.objects.create(
        urls="" if is_update else url,
        max_depth=0,
        created_by_id=user_id,
        status=Crawl.StatusChoices.SEALED,
        retry_at=None,
        config={"SCHEDULE_KIND": "update"} if is_update else {},
    )
    schedule = CrawlSchedule.objects.create(
        template=template,
        schedule=schedule_expr,
        is_enabled=True,
        config={"SCHEDULE_KIND": "update"} if is_update else {},
        created_by_id=user_id,
    )
    if due:
        past = timezone.now() - timedelta(days=2)
        Crawl.objects.filter(pk=template.pk).update(created_at=past, modified_at=past)
        if is_update:
            CrawlSchedule.objects.filter(pk=schedule.pk).update(created_at=past, modified_at=past)
    schedule.refresh_from_db()
    template.refresh_from_db()
    return schedule


def _reload(schedule):
    from archivebox.crawls.models import CrawlSchedule

    return CrawlSchedule.with_run_state(CrawlSchedule.objects.select_related("template")).get(pk=schedule.pk)


def _expire_run(run, *, seconds_ago=1):
    from archivebox.crawls.models import CrawlScheduleRun

    past = timezone.now() - timedelta(seconds=seconds_ago)
    CrawlScheduleRun.objects.filter(pk=run.pk).update(claim_until=past)


# ---------------------------------------------------------------------------
# AC-2 / AC-7: shared claim rules, lease mutex, due detection, restart anchor
# ---------------------------------------------------------------------------


def test_poll_path_skips_fresh_schedule_and_dispatches_once_due(archivebox_db):
    from archivebox.crawls.models import Crawl, CrawlScheduleRun

    schedule = _make_schedule(due=False)
    assert schedule.is_due() is False
    assert schedule.due_slot() is None

    outcome = schedule.dispatch_due(force=False)
    assert outcome.state == "skipped"
    assert CrawlScheduleRun.objects.filter(schedule=schedule).count() == 0

    past = timezone.now() - timedelta(days=2)
    Crawl.objects.filter(pk=schedule.template_id).update(created_at=past, modified_at=past)
    due_schedule = _reload(schedule)
    assert due_schedule.is_due() is True

    outcome = due_schedule.dispatch_due(force=False)
    assert outcome.state == "dispatched"
    assert outcome.crawl is not None
    assert outcome.run.status == CrawlScheduleRun.StatusChoices.SUCCEEDED
    assert outcome.crawl.schedule_run_id == outcome.run.pk

    # A second tick, even after reloading state as a restarted process would,
    # must not dispatch the same slot again.
    assert _reload(schedule).is_due() is False
    second = _reload(schedule).dispatch_due(force=False)
    assert second.state == "skipped"
    assert Crawl.objects.filter(schedule_run__schedule=schedule).count() == 1
    assert CrawlScheduleRun.objects.filter(schedule=schedule, status="succeeded").count() == 1


def test_force_run_materializes_current_slot_before_first_period(archivebox_db):
    from archivebox.crawls.models import Crawl, CrawlScheduleRun

    schedule = _make_schedule(due=False)
    now = timezone.now()
    outcome = schedule.dispatch_due(now, force=True)
    assert outcome.state == "dispatched"
    assert outcome.run.slot_at == current_slot_for_schedule(schedule.schedule, now)

    reloaded = _reload(schedule)
    assert reloaded.is_due() is False
    assert reloaded.next_run_at == next_run_for_schedule(schedule.schedule, outcome.run.slot_at)

    # The forced slot is claimed for good: a second force is a skip.
    assert reloaded.dispatch_due(force=True).state == "skipped"
    assert Crawl.objects.filter(schedule_run__schedule=schedule).count() == 1
    assert CrawlScheduleRun.objects.filter(schedule=schedule).count() == 1


def test_active_lease_blocks_second_claim(archivebox_db):
    from archivebox.crawls.models import CrawlScheduleRun

    schedule = _make_schedule(due=True)
    now = timezone.now()
    slot = schedule.current_slot(now)

    first = CrawlScheduleRun.claim_slot(schedule, slot, now=now)
    assert first is not None
    run, _revision = first
    assert run.attempts == 1
    assert run.claim_token
    assert run.claimed_by
    assert run.claim_until > now

    second = CrawlScheduleRun.claim_slot(schedule, slot, now=now)
    assert second is None
    assert CrawlScheduleRun.objects.filter(schedule=schedule, slot_at=slot).count() == 1


def test_disabled_and_deleted_schedule_cannot_be_claimed(archivebox_db):
    from archivebox.crawls.models import CrawlSchedule, CrawlScheduleRun

    schedule = _make_schedule(due=True)
    slot = schedule.current_slot()
    CrawlSchedule.objects.filter(pk=schedule.pk).update(is_enabled=False)

    assert CrawlScheduleRun.claim_slot(_reload(schedule), slot, now=timezone.now()) is None

    CrawlSchedule.objects.filter(pk=schedule.pk).update(is_enabled=True)
    assert CrawlScheduleRun.claim_slot(_reload(schedule), slot, now=timezone.now()) is not None

    CrawlSchedule.objects.filter(pk=schedule.pk).delete()
    assert CrawlScheduleRun.objects.filter(schedule_id=schedule.pk).count() == 0


# ---------------------------------------------------------------------------
# AC-5: dead-owner takeover (with and without an orphaned materialized Crawl)
# ---------------------------------------------------------------------------


def test_expired_claim_is_taken_over_and_materializes_single_crawl(archivebox_db):
    from archivebox.crawls.models import Crawl, CrawlScheduleRun

    schedule = _make_schedule(due=True)
    run, _revision = CrawlScheduleRun.claim_slot(schedule, schedule.current_slot(), now=timezone.now())
    run_id = run.pk
    _expire_run(run)

    outcome = _reload(schedule).dispatch_due(force=True)
    assert outcome.state == "dispatched"
    assert outcome.run.pk == run_id
    assert outcome.run.attempts == 2
    assert outcome.run.status == CrawlScheduleRun.StatusChoices.SUCCEEDED
    assert outcome.crawl is not None
    assert Crawl.objects.filter(schedule_run_id=run_id).count() == 1
    assert CrawlScheduleRun.objects.filter(schedule=schedule).count() == 1


def test_takeover_adopts_orphaned_crawl_without_duplicating_it(archivebox_db):
    from archivebox.crawls.models import Crawl, CrawlScheduleRun

    schedule = _make_schedule(due=True)
    run, _revision = CrawlScheduleRun.claim_slot(schedule, schedule.current_slot(), now=timezone.now())

    # Simulate a process that materialized the Crawl and died before it could
    # confirm the slot run.
    orphan = Crawl.objects.create(
        urls=schedule.template.urls,
        max_depth=0,
        created_by=schedule.template.created_by,
        schedule=schedule,
        schedule_run=run,
        status=Crawl.StatusChoices.QUEUED,
        retry_at=timezone.now(),
    )
    _expire_run(run)

    outcome = _reload(schedule).dispatch_due(force=True)
    assert outcome.state == "dispatched"
    assert outcome.crawl.pk == orphan.pk
    assert outcome.run.pk == run.pk
    assert outcome.run.attempts == 2
    assert outcome.run.status == CrawlScheduleRun.StatusChoices.SUCCEEDED
    assert Crawl.objects.filter(schedule_run_id=run.pk).count() == 1


# ---------------------------------------------------------------------------
# AC-8: disable / reschedule while a claim is held
# ---------------------------------------------------------------------------


def test_stale_owner_cannot_commit_after_disable_then_reenable_takes_over(archivebox_db):
    from archivebox.crawls.models import Crawl, CrawlSchedule, CrawlScheduleRun

    schedule = _make_schedule(due=True)
    run, revision = CrawlScheduleRun.claim_slot(schedule, schedule.current_slot(), now=timezone.now())

    # Admin disables the plan between claim and materialization.
    CrawlSchedule.objects.filter(pk=schedule.pk).update(is_enabled=False, modified_at=timezone.now())
    outcome = schedule._materialize_crawl_slot(run, revision)
    assert outcome.state == "skipped"
    assert not Crawl.objects.filter(schedule_run_id=run.pk).exists()
    run.refresh_from_db()
    assert run.status == CrawlScheduleRun.StatusChoices.FAILED
    assert run.claim_until <= timezone.now()

    # Re-enabling while the slot is still due lets a new owner take the
    # failed row over immediately (its lease is already expired).
    CrawlSchedule.objects.filter(pk=schedule.pk).update(is_enabled=True)
    outcome = _reload(schedule).dispatch_due(force=False)
    assert outcome.state == "dispatched"
    assert outcome.run.pk == run.pk
    assert outcome.run.attempts == 2
    assert Crawl.objects.filter(schedule_run_id=run.pk).count() == 1


def test_reschedule_rejects_old_slot_commit_and_advances_on_new_cron(archivebox_db):
    from archivebox.crawls.models import Crawl, CrawlSchedule, CrawlScheduleRun

    schedule = _make_schedule(schedule_expr="daily", due=True)
    old_slot = schedule.current_slot()
    old_run, old_revision = CrawlScheduleRun.claim_slot(schedule, old_slot, now=timezone.now())

    # Admin changes the cadence to hourly while the old owner is mid-claim.
    new_anchor = timezone.now() - timedelta(hours=2)
    CrawlSchedule.objects.filter(pk=schedule.pk).update(schedule="hourly", modified_at=new_anchor, created_at=new_anchor)
    schedule.refresh_from_db()

    outcome = schedule._materialize_crawl_slot(old_run, old_revision)
    assert outcome.state == "skipped"
    assert not Crawl.objects.filter(schedule_run_id=old_run.pk).exists()
    old_run.refresh_from_db()
    assert old_run.status == CrawlScheduleRun.StatusChoices.FAILED
    assert old_run.claim_until <= timezone.now()
    assert not hasattr(old_run, "crawl")

    # The next legal period is computed from the new cron expression and runs
    # exactly once as a new slot; the stale daily slot stays identifiable as
    # failed and never turns into a run under the new config.
    reloaded = _reload(schedule)
    new_slot = reloaded.current_slot()
    assert new_slot != old_slot
    assert reloaded.is_due() is True
    outcome = reloaded.dispatch_due(force=False)
    assert outcome.state == "dispatched"
    assert outcome.run.slot_at == new_slot
    assert outcome.crawl.schedule_run_id == outcome.run.pk

    assert CrawlScheduleRun.objects.filter(schedule=schedule, status="succeeded").count() == 1
    assert Crawl.objects.filter(schedule_run__schedule=schedule).count() == 1
    assert reloaded.next_run_at == next_run_for_schedule("hourly", new_slot)


def test_deleting_schedule_cascades_runs_but_keeps_derived_crawl(archivebox_db):
    from archivebox.crawls.models import Crawl, CrawlSchedule, CrawlScheduleRun

    schedule = _make_schedule(due=False)
    outcome = schedule.dispatch_due(force=True)
    crawl = outcome.crawl
    run_pk = outcome.run.pk

    CrawlSchedule.objects.get(pk=schedule.pk).delete()

    assert not CrawlScheduleRun.objects.filter(pk=run_pk).exists()
    crawl.refresh_from_db()
    assert crawl.schedule_id is None
    assert crawl.schedule_run_id is None
    assert Crawl.objects.filter(pk=crawl.pk).exists()


# ---------------------------------------------------------------------------
# AC-3 / AC-6: scheduled maintenance runs once; a genuine execution failure
# keeps the slot identifiable and safe to take over.
# ---------------------------------------------------------------------------


def test_maintenance_failure_is_retriable_and_then_succeeds_once(archivebox_db):
    from django.db import connection

    from archivebox.crawls.models import CrawlScheduleRun

    schedule = _make_schedule(kind="update", due=True)

    # Produce a genuine database failure (not a mock): rename the snapshot
    # table away so run_scheduled_maintenance raises on its first query. This
    # mirrors a real write/query failure mid-dispatch. Rename it back as soon
    # as dispatch raises so the retry runs on a healthy database.
    with connection.cursor() as cursor:
        cursor.execute("ALTER TABLE core_snapshot RENAME TO core_snapshot_broken")
    try:
        with pytest.raises(Exception):
            schedule.dispatch_due(force=True)
    finally:
        with connection.cursor() as cursor:
            cursor.execute("ALTER TABLE core_snapshot_broken RENAME TO core_snapshot")

    failed = CrawlScheduleRun.objects.get(schedule=schedule)
    assert failed.status == CrawlScheduleRun.StatusChoices.FAILED
    assert "OperationalError" in failed.error
    assert failed.claim_until <= timezone.now()
    assert failed.finished_at is not None
    assert failed.attempts == 1

    # The next pass takes the same slot over and completes maintenance; a
    # third dispatch still cannot run the slot again.
    outcome = _reload(schedule).dispatch_due(force=True)
    assert outcome.state == "dispatched"
    assert outcome.run.pk == failed.pk
    assert outcome.run.attempts == 2
    assert outcome.run.status == CrawlScheduleRun.StatusChoices.SUCCEEDED

    assert _reload(schedule).dispatch_due(force=True).state == "skipped"
    assert CrawlScheduleRun.objects.filter(schedule=schedule).count() == 1


# ---------------------------------------------------------------------------
# AC-1 / AC-3 / AC-4: two real OS processes dispatching concurrently
# ---------------------------------------------------------------------------


@pytest.mark.timeout(300)
def test_two_concurrent_run_all_clis_materialize_one_crawl(initialized_archive):
    env = cli_env(disable_extractors=True)
    created = run_archivebox_cmd(
        ["schedule", "--every=daily", "--depth=0", "https://example.com/concurrent-slot"],
        cwd=initialized_archive,
        env=env,
        check=True,
    )
    assert created.returncode == 0

    first = run_archivebox_cmd(
        ["schedule", "--run-all"],
        cwd=initialized_archive,
        env=env,
        wait=False,
        start_new_session=True,
    )
    second = run_archivebox_cmd(
        ["schedule", "--run-all"],
        cwd=initialized_archive,
        env=env,
        wait=False,
        start_new_session=True,
    )
    first.communicate(timeout=180)
    second.communicate(timeout=180)
    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr

    with use_archivebox_db(initialized_archive):
        from archivebox.crawls.models import Crawl, CrawlScheduleRun

        succeeded = list(
            CrawlScheduleRun.objects.filter(status=CrawlScheduleRun.StatusChoices.SUCCEEDED).values_list("schedule_id", "slot_at")
        )
        assert len(succeeded) == 1
        assert CrawlScheduleRun.objects.count() == 1
        assert Crawl.objects.exclude(schedule_run=None).count() == 1

    # A sequential follow-up run-all has nothing left to dispatch.
    follow_up = run_archivebox_cmd(["schedule", "--run-all"], cwd=initialized_archive, env=env)
    assert follow_up.returncode == 0
    assert "Enqueued 0 scheduled crawl" in follow_up.stdout


@pytest.mark.timeout(300)
def test_two_concurrent_run_all_clis_run_maintenance_once(initialized_archive):
    env = cli_env(disable_extractors=True)
    created = run_archivebox_cmd(["schedule", "--every=day"], cwd=initialized_archive, env=env, check=True)
    assert created.returncode == 0

    first = run_archivebox_cmd(
        ["schedule", "--run-all"],
        cwd=initialized_archive,
        env=env,
        wait=False,
        start_new_session=True,
    )
    second = run_archivebox_cmd(
        ["schedule", "--run-all"],
        cwd=initialized_archive,
        env=env,
        wait=False,
        start_new_session=True,
    )
    first.communicate(timeout=180)
    second.communicate(timeout=180)
    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr

    with use_archivebox_db(initialized_archive):
        from archivebox.crawls.models import CrawlScheduleRun

        assert CrawlScheduleRun.objects.count() == 1
        run = CrawlScheduleRun.objects.get()
        assert run.status == CrawlScheduleRun.StatusChoices.SUCCEEDED
        assert run.attempts == 1
