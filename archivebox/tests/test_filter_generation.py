"""URL filter generation: durable rule versions, convergence, and late writes.

Covers the contract that every effective URL_ALLOWLIST/URL_DENYLIST edit bumps a
persistently comparable generation, queued/paused/started Snapshots a new rule
rejects converge through the durable deleting queue, and late ArchiveResult or
discovery writes cannot resurrect converged rows.
"""

import pytest
from datetime import timedelta
from django.utils import timezone

from archivebox.core.models import ArchiveResult, Snapshot
from archivebox.crawls.models import Crawl

pytestmark = pytest.mark.django_db(transaction=True)


def _open_snapshot(crawl, url, *, status):
    snapshot = Snapshot.objects.create(crawl=crawl, url=url, status=status, retry_at=timezone.now())
    snapshot.output_dir.mkdir(parents=True, exist_ok=True)
    (snapshot.output_dir / "captured.txt").write_text(f"capture for {url}")
    return snapshot


def test_filter_version_bumps_only_on_effective_change(admin_user):
    crawl = Crawl.objects.create(urls="https://example.com/", created_by=admin_user)
    start_allow, start_deny = crawl.normalized_url_filters()
    assert crawl.url_filter_version == 0

    assert crawl.apply_url_filter_change(start_allow, start_deny)["changed"] is False
    assert crawl.url_filter_version == 0

    result = crawl.apply_url_filter_change([*start_allow, "example.com"], start_deny)
    assert result["changed"] is True
    assert crawl.url_filter_version == 1

    # Whitespace/blank-line differences normalize away: no second generation.
    result = crawl.apply_url_filter_change("example.com\n\n", "\n".join(start_deny) + "\n")
    assert result["changed"] is False
    assert crawl.url_filter_version == 1

    result = crawl.exclude_domain("https://other.test/some/path")
    assert result["changed"] is True
    assert result["created"] is True
    assert result["domain"] == "other.test"
    assert crawl.url_filter_version == 2
    assert "other.test" in crawl.get_url_denylist(use_effective_config=False)

    result = crawl.exclude_domain("other.test")
    assert result["changed"] is False
    assert result["created"] is False
    assert crawl.url_filter_version == 2


def test_effective_change_converges_open_snapshots_and_preserves_sealed_history(admin_user):
    crawl = Crawl.objects.create(
        urls="https://blocked.example/a\nhttps://keep.example/b",
        max_depth=1,
        created_by=admin_user,
    )
    queued = _open_snapshot(crawl, "https://blocked.example/a", status=Snapshot.StatusChoices.QUEUED)
    paused = _open_snapshot(crawl, "https://blocked.example/paused", status=Snapshot.StatusChoices.PAUSED)
    started = _open_snapshot(crawl, "https://blocked.example/running", status=Snapshot.StatusChoices.STARTED)
    ArchiveResult.objects.create(
        snapshot=started,
        plugin="wget",
        hook_name="on_Snapshot__06_wget",
        status=ArchiveResult.StatusChoices.SUCCEEDED,
        output_size=12,
    )
    allowed = _open_snapshot(crawl, "https://keep.example/b", status=Snapshot.StatusChoices.QUEUED)
    sealed = Snapshot.objects.create(
        crawl=crawl,
        url="https://blocked.example/finished",
        status=Snapshot.StatusChoices.SEALED,
        retry_at=None,
    )
    sealed.output_dir.mkdir(parents=True, exist_ok=True)
    sealed_file = sealed.output_dir / "done.txt"
    sealed_file.write_text("historical output stays")

    result = crawl.apply_url_filter_change("keep.example", "")

    assert crawl.url_filter_version == 1
    assert result["changed"] is True
    assert result["removed_urls"] == 1
    assert "https://blocked.example/a" not in crawl.urls
    assert "https://keep.example/b" in crawl.urls
    assert result["accepted_snapshots"] == 1
    assert result["cancelled_snapshots"] == 1
    assert result["stopped_snapshots"] == 2
    assert result["deleted_snapshots"] == 3
    assert result["pending_delete_snapshots"] == 3

    for rejected in (queued, paused, started):
        rejected.refresh_from_db()
        assert rejected.status == Snapshot.DELETING_STATE
        assert rejected.retry_at.year == 9999
        assert rejected.output_dir.exists()

    allowed.refresh_from_db()
    assert allowed.status == Snapshot.StatusChoices.QUEUED
    assert allowed.url_filter_version == 0

    sealed.refresh_from_db()
    assert sealed.status == Snapshot.StatusChoices.SEALED
    assert sealed_file.read_text() == "historical output stays"

    # Nothing rejected remains schedulable.
    due = Snapshot.objects.filter(
        crawl__status__in=Crawl.RUNNABLE_STATES,
        status__in=Snapshot.RUNNABLE_STATES,
        retry_at__lte=timezone.now(),
    )
    assert {s.pk for s in due} == {allowed.pk}

    # The durable queue performs the real process cancellation, file removal,
    # ArchiveResult cascade, and hard delete; sealed output is untouched.
    Snapshot.delete_requested()
    assert not Snapshot.objects.filter(pk__in=[queued.pk, paused.pk, started.pk]).exists()
    assert not ArchiveResult.objects.filter(snapshot_id=started.pk).exists()
    assert not queued.output_dir.exists()
    assert Snapshot.objects.filter(pk=sealed.pk).exists()
    assert sealed_file.read_text() == "historical output stays"

    # Re-running reconciliation after cleanup is an idempotent no-op.
    again = crawl.apply_url_filter_change("keep.example", "")
    assert again["changed"] is False
    assert again["pending_delete_snapshots"] == 0
    assert crawl.url_filter_version == 1


def test_convergence_survives_restart_and_retries_after_file_failure(admin_user, monkeypatch):
    crawl = Crawl.objects.create(urls="https://blocked.example/a", created_by=admin_user)
    snapshot = _open_snapshot(crawl, "https://blocked.example/a", status=Snapshot.StatusChoices.QUEUED)
    output_dir = snapshot.output_dir

    crawl.apply_url_filter_change("keep.example", "")
    snapshot.refresh_from_db()
    assert snapshot.status == Snapshot.DELETING_STATE

    # Simulate the runner's cleanup pass failing on storage (stale mount, open
    # handle): the row must remain a retryable "deleting" marker, not disappear.
    from archivebox.core import models as core_models

    def raise_oserror(paths):
        raise OSError("storage temporarily unavailable")

    monkeypatch.setattr(core_models.Snapshot, "delete_output_paths", staticmethod(raise_oserror))
    Snapshot.delete_requested()
    snapshot.refresh_from_db()
    assert snapshot.status == Snapshot.DELETING_STATE
    assert output_dir.exists()
    assert Snapshot.objects.filter(pk=snapshot.pk, status=Snapshot.DELETING_STATE).exists()

    # A later sweep (equivalent to after a service restart) finishes cleanup.
    monkeypatch.undo()
    Snapshot.delete_requested()
    assert not Snapshot.objects.filter(pk=snapshot.pk).exists()
    assert not output_dir.exists()


def test_add_url_rechecks_current_generation_with_stale_instance(admin_user):
    crawl = Crawl.objects.create(urls="https://example.com/", created_by=admin_user)
    stale = Crawl.objects.get(pk=crawl.pk)

    Crawl.objects.get(pk=crawl.pk).apply_url_filter_change("", "forbidden.example")

    assert stale.add_url({"url": "https://forbidden.example/link", "depth": 0}) is False
    assert "forbidden.example/link" not in Crawl.objects.get(pk=crawl.pk).urls

    assert stale.add_url({"url": "https://allowed.example/ok", "depth": 0}) is True
    assert "https://allowed.example/ok" in Crawl.objects.get(pk=crawl.pk).urls


def test_seed_and_discovered_snapshots_stamp_current_generation(admin_user):
    crawl = Crawl.objects.create(urls="https://root.example/", max_depth=1, created_by=admin_user)
    [parent] = crawl.create_snapshots_from_urls()
    assert parent.url_filter_version == 0

    crawl.apply_url_filter_change("", "cdn.example.com")
    assert Crawl.objects.get(pk=crawl.pk).url_filter_version == 1

    children = crawl.create_discovered_snapshots(
        parent,
        [
            {"url": "https://cdn.example.com/asset.js"},
            {"url": "https://root.example/child"},
        ],
        depth=1,
    )
    assert [child.url for child in children] == ["https://root.example/child"]
    assert children[0].url_filter_version == 1
    # The blocked discovery never reached the queue under any spelling.
    assert not Snapshot.objects.filter(crawl=crawl, url__contains="cdn.example.com").exists()

    # New seed lines appended after the edit are filtered and stamped at v1.
    crawl.urls = crawl.urls + "\nhttps://cdn.example.com/blocked-seed\nhttps://root.example/seed2"
    crawl.save(update_fields=["urls", "modified_at"])
    seeds = crawl.create_snapshots_from_urls()
    assert [seed.url for seed in seeds] == ["https://root.example/seed2"]
    assert seeds[0].url_filter_version == 1


def test_runner_claim_gate_converges_rejected_snapshot_without_running_it(admin_user):
    from archivebox.services.runner import run_due_snapshot

    crawl = Crawl.objects.create(
        urls="https://keep.example/a",
        status=Crawl.StatusChoices.STARTED,
        created_by=admin_user,
    )
    crawl.apply_url_filter_change("", "blocked.example")

    # Simulate the admission race: a runner that selected the Snapshot under
    # the old rule persists it after the generation bump, so the row exists
    # with a stale stamp but no reconciliation has seen it yet.
    snapshot = Snapshot.objects.create(
        crawl=crawl,
        url="https://blocked.example/a",
        status=Snapshot.StatusChoices.QUEUED,
        retry_at=timezone.now(),
        url_filter_version=0,
    )
    output_dir = snapshot.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "partial.txt").write_text("captured before the rule changed")

    # The real claim path converges the row before any hook/crawl runtime runs.
    handled = run_due_snapshot(snapshot, lock_seconds=60, runtime_config=None)
    assert handled is True
    snapshot.refresh_from_db()
    assert snapshot.status == Snapshot.DELETING_STATE
    # The claim never ran hooks or removed files; the cleanup queue owns that.
    assert (output_dir / "partial.txt").exists()

    # The same gate leaves a stale-stamped row the current rule still admits.
    admitted = Snapshot.objects.create(
        crawl=crawl,
        url="https://keep.example/a",
        status=Snapshot.StatusChoices.QUEUED,
        retry_at=timezone.now() + timedelta(seconds=60),
        url_filter_version=0,
    )
    assert admitted.reconcile_url_filter_generation() is None
    admitted.refresh_from_db()
    assert admitted.url_filter_version == crawl.url_filter_version
    assert admitted.status == Snapshot.StatusChoices.QUEUED

    # Relaxing the rule adopts the current generation for queued legacy rows
    # without restoring anything already cleaned.
    other = Crawl.objects.create(urls="https://open.example/", status=Crawl.StatusChoices.STARTED, created_by=admin_user)
    waiting = _open_snapshot(other, "https://open.example/", status=Snapshot.StatusChoices.QUEUED)
    other.apply_url_filter_change("", "blocked.example")
    other.apply_url_filter_change("", "")
    assert other.url_filter_version == 2
    assert waiting.accepted_under_current_url_filters() is True
    waiting.refresh_from_db()
    assert waiting.url_filter_version == 2
    assert waiting.status == Snapshot.StatusChoices.QUEUED
    assert waiting.reconcile_url_filter_generation() is None


def test_late_archive_results_and_discoveries_cannot_resurrect_deleting_snapshot(admin_user):
    from abx_dl.events import ArchiveResultEvent
    from archivebox.services.archive_result_service import _save_archiveresult_event_to_db
    from archivebox.services.snapshot_service import finalize_completed_snapshot, project_discovered_snapshots

    crawl = Crawl.objects.create(
        urls="https://blocked.example/a\nhttps://parent.example/",
        max_depth=1,
        status=Crawl.StatusChoices.STARTED,
        created_by=admin_user,
    )
    snapshot = _open_snapshot(crawl, "https://blocked.example/a", status=Snapshot.StatusChoices.STARTED)
    parent = _open_snapshot(crawl, "https://parent.example/", status=Snapshot.StatusChoices.STARTED)
    # Parser output from an already-captured old page mentioning the domain.
    parser_dir = parent.output_dir / "parse_html_urls"
    parser_dir.mkdir(parents=True, exist_ok=True)
    (parser_dir / "urls.jsonl").write_text(jsonl_for("https://blocked.example/from-old-page"))

    result = crawl.apply_url_filter_change("", "blocked.example")
    assert result["cancelled_snapshots"] == 1
    snapshot.refresh_from_db()
    assert snapshot.status == Snapshot.DELETING_STATE

    owned_retry_at = snapshot.retry_at
    finalize_completed_snapshot(
        str(snapshot.id),
        owned_retry_at=owned_retry_at,
        was_sealed=False,
    )
    snapshot.refresh_from_db()
    assert snapshot.status == Snapshot.DELETING_STATE
    assert snapshot.downloaded_at is None
    assert not ArchiveResult.objects.filter(snapshot=snapshot).exists()

    # The late hook result event for the converged row is dropped wholesale.
    event = ArchiveResultEvent(
        snapshot_id=str(snapshot.id),
        plugin="wget",
        hook_name="on_Snapshot__06_wget.py",
        status="succeeded",
        output_str="late payload",
    )
    _save_archiveresult_event_to_db(event, None)
    assert not ArchiveResult.objects.filter(snapshot=snapshot).exists()
    snapshot.refresh_from_db()
    assert snapshot.status == Snapshot.DELETING_STATE

    # Late parser discoveries do not queue the blocked domain from either page.
    assert project_discovered_snapshots(str(parent.id)) == []
    assert not Snapshot.objects.filter(url="https://blocked.example/from-old-page").exists()
    assert project_discovered_snapshots(str(snapshot.id)) == []

    # Redo/plugin scheduling cannot pull the row back into work.
    assert snapshot.schedule_plugin_run(["wget"]) is False
    snapshot.refresh_from_db()
    assert snapshot.status == Snapshot.DELETING_STATE


def jsonl_for(url: str) -> str:
    import json

    return json.dumps({"url": url}) + "\n"


def test_rule_relaxation_does_not_restore_cleaned_entries_but_admits_new_discoveries(admin_user):
    crawl = Crawl.objects.create(
        urls="https://cdn.example.com/asset.js",
        max_depth=1,
        created_by=admin_user,
    )
    parent = _open_snapshot(crawl, "https://example.com/", status=Snapshot.StatusChoices.QUEUED)
    blocked = _open_snapshot(crawl, "https://cdn.example.com/asset.js", status=Snapshot.StatusChoices.QUEUED)

    crawl.apply_url_filter_change("", "cdn.example.com")
    Snapshot.delete_requested()
    assert not Snapshot.objects.filter(pk=blocked.pk).exists()
    assert "cdn.example.com" not in crawl.urls

    # Relaxing only bumps the generation; no automatic restoration happens.
    result = crawl.apply_url_filter_change("", "")
    assert result["changed"] is True
    assert crawl.url_filter_version == 2
    assert not Snapshot.objects.filter(pk=blocked.pk).exists()
    assert "cdn.example.com" not in crawl.urls

    # Genuinely new discoveries after the relaxation are admitted as fresh rows
    # stamped at the current generation.
    created = crawl.create_discovered_snapshots(
        parent,
        [{"url": "https://cdn.example.com/new-asset.js"}],
        depth=1,
    )
    assert len(created) == 1
    assert created[0].url_filter_version == 2
    assert created[0].status == Snapshot.StatusChoices.QUEUED


def test_exclude_domain_response_counts_reflect_actual_outcomes(client, admin_user):
    from django.urls import reverse
    from archivebox.tests.conftest import ADMIN_TEST_HOST

    crawl = Crawl.objects.create(
        urls="https://cdn.example.com/a\nhttps://cdn.example.com/b\nhttps://keep.example/c",
        created_by=admin_user,
    )
    first = _open_snapshot(crawl, "https://cdn.example.com/a", status=Snapshot.StatusChoices.QUEUED)
    second = _open_snapshot(crawl, "https://cdn.example.com/b", status=Snapshot.StatusChoices.STARTED)
    third = _open_snapshot(crawl, "https://keep.example/c", status=Snapshot.StatusChoices.QUEUED)

    client.force_login(admin_user)
    response = client.post(
        reverse("admin:crawls_crawl_snapshot_exclude_domain", args=[crawl.pk, first.pk]),
        HTTP_HOST=ADMIN_TEST_HOST,
    )
    payload = response.json()
    assert response.status_code == 200
    assert payload["accepted_snapshots"] == 1
    assert payload["cancelled_snapshots"] == 1
    assert payload["stopped_snapshots"] == 1
    assert payload["pending_delete_snapshots"] == 2
    assert payload["removed_urls"] == 2

    assert Snapshot.objects.get(pk=second.pk).status == Snapshot.DELETING_STATE
    assert Snapshot.objects.get(pk=first.pk).status == Snapshot.DELETING_STATE
    assert Snapshot.objects.get(pk=third.pk).status == Snapshot.StatusChoices.QUEUED
