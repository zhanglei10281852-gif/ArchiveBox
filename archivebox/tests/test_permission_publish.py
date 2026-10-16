"""Versioned, recoverable permission publications for Crawls and Snapshots.

A Crawl visibility change is a published intent: every child Snapshot row, the
static JSON/HTML metadata on disk, and previously issued replay authorizations
must converge at the same comparable version. Tightening fails closed before
propagation finishes; relaxation only opens after the affected artifacts are
rewritten. Interrupted batches resume from a durable cursor instead of leaving
a permanently mixed-permission collection.
"""

import json

import pytest
from django.test import override_settings

from archivebox.core.permissions import (
    PERMISSIONS_PRIVATE,
    PERMISSIONS_PUBLIC,
    PERMISSIONS_UNLISTED,
    can_view_snapshot,
    direct_snapshots_queryset,
    effective_snapshot_permissions,
    public_snapshots_queryset,
    snapshot_replay_epoch,
)

from archivebox.tests.conftest import WEB_TEST_HOST

pytestmark = pytest.mark.django_db


class _AnonymousRequest:
    """Minimal request object for the pure permission predicates."""

    user = None

    def __init__(self):
        from django.contrib.auth.models import AnonymousUser

        self.user = AnonymousUser()


def _crawl(permissions=PERMISSIONS_PUBLIC, *, user=None, urls="https://publish.example/"):
    from archivebox.crawls.models import Crawl

    return Crawl.objects.create(
        urls=urls,
        created_by=user,
        config={"PERMISSIONS": permissions},
    )


def _snapshot(crawl, url=None, *, status=None, permissions=None):
    from archivebox.core.models import Snapshot

    snapshot = Snapshot(
        url=url or f"https://publish.example/{crawl.snapshot_set.count() + 1}",
        crawl=crawl,
        status=status or Snapshot.StatusChoices.SEALED,
    )
    if permissions is not None:
        snapshot.config = {"PERMISSIONS": permissions}
    snapshot.save()
    snapshot.refresh_from_db()
    return snapshot


def _write_static_manifests(snapshot):
    snapshot.output_dir.mkdir(parents=True, exist_ok=True)
    snapshot.write_json_details()
    snapshot.write_html_details()


def _read_manifest_permissions(snapshot):
    data = json.loads((snapshot.output_dir / "index.json").read_text(encoding="utf-8"))
    return data["permissions"], int(data["permissions_version"])


def _mint_replay_credential(snapshot, admin):
    """Mint exactly the same replay session + signed cookie as the admin handoff."""
    from django.contrib.auth import HASH_SESSION_KEY, SESSION_KEY
    from django.contrib.sessions.backends.db import SessionStore
    from django.core import signing
    from archivebox.core.views import REPLAY_AUTH_SALT

    epoch = snapshot_replay_epoch(snapshot)
    # A real, unexpired admin session row: the replay credential only points
    # at it, exactly like SnapshotReplayAuthView does.
    admin_session = SessionStore()
    admin_session[SESSION_KEY] = str(admin.pk)
    admin_session[HASH_SESSION_KEY] = admin.get_session_auth_hash()
    admin_session.create()

    replay_session = SessionStore()
    replay_session["replay"] = {
        "snapshot_id": str(snapshot.id),
        "user_id": str(admin.pk),
        "session_key": admin_session.session_key,
        "auth_hash": admin.get_session_auth_hash(),
        "perm_epoch": epoch,
    }
    replay_session.create()
    payload = {
        "snapshot_id": str(snapshot.id),
        "replay_session": replay_session.session_key,
        "perm_epoch": epoch,
    }
    return signing.dumps(payload, salt=REPLAY_AUTH_SALT)


def _replay_cookie_accepted(snapshot, admin, *, value: str | None = None) -> bool:
    from archivebox.core import views as core_views

    # Default mints a *current* credential; pass a previously minted value to
    # verify whether an outstanding grant survives a later publication.
    value = value if value is not None else _mint_replay_credential(snapshot, admin)

    class _CookieRequest(_AnonymousRequest):
        COOKIES = {}

    request = _CookieRequest()
    request.COOKIES = {core_views._replay_cookie_name(snapshot): value}
    return core_views._has_replay_cookie(request, snapshot)


def test_new_crawl_stamps_child_rows_with_the_current_intent_version(admin_user):
    crawl = _crawl(PERMISSIONS_PUBLIC, user=admin_user)
    snapshot = _snapshot(crawl)

    assert crawl.permissions_version == 1
    assert snapshot.permissions_version == 1
    assert snapshot.permissions_override is False
    assert effective_snapshot_permissions(snapshot) == PERMISSIONS_PUBLIC


def test_tightening_closes_anonymous_access_while_child_rows_are_stale(admin_user):
    from archivebox.services.permission_service import publish_crawl_permissions

    crawl = _crawl(PERMISSIONS_PUBLIC, user=admin_user)
    inherited = _snapshot(crawl, "https://publish.example/a")
    _snapshot(crawl, "https://publish.example/b")
    # An explicitly overridden child follows its own value, not the crawl intent.
    override = _snapshot(crawl, "https://publish.example/secret", permissions=PERMISSIONS_UNLISTED)

    old_cookie_snapshot = _snapshot(crawl, "https://publish.example/cookied")
    credential = _mint_replay_credential(old_cookie_snapshot, admin_user)

    publish = publish_crawl_permissions(crawl, PERMISSIONS_PRIVATE, user=admin_user)
    assert publish is not None
    assert publish.version == 2

    # Simulate a batch the propagation has not reached yet: its row still says
    # public at the old version. The surface must fail closed anyway.
    stale = type(inherited).objects.get(pk=inherited.pk)
    stale.config = {**stale.config, "PERMISSIONS": PERMISSIONS_PUBLIC}
    stale.permissions_version = 1
    type(inherited).objects.filter(pk=stale.pk).update(
        config=stale.config,
        permissions_version=1,
    )
    stale.refresh_from_db()

    anon = _AnonymousRequest()
    assert effective_snapshot_permissions(stale) == PERMISSIONS_PRIVATE
    assert can_view_snapshot(anon, stale) is False
    assert not public_snapshots_queryset(type(stale).objects.all()).filter(pk=stale.pk).exists()
    assert not direct_snapshots_queryset(anon, type(stale).objects.all()).filter(pk=stale.pk).exists()

    response = (
        __import__("django.test", fromlist=["Client"])
        .Client()
        .get(
            f"/snapshot/{stale.id}/",
            HTTP_HOST=WEB_TEST_HOST,
        )
    )
    assert response.status_code == 302
    assert "/admin/core/snapshot/replay-auth/" in response["Location"]

    # The cookie handed out before the change is bound to the old epoch and is
    # now inert, even though the admin session it references still exists.
    from archivebox.core import views as core_views

    class _CookieRequest(_AnonymousRequest):
        COOKIES = {}

    request = _CookieRequest()
    request.COOKIES = {core_views._replay_cookie_name(stale): credential}
    assert core_views._has_replay_cookie(request, stale) is False

    # Explicit overrides are not dragged along by a parent publication.
    override.refresh_from_db()
    assert effective_snapshot_permissions(override) == PERMISSIONS_UNLISTED
    assert override.permissions_override is True
    # Full (unguessable) unlisted links keep working for anonymous visitors.
    full_link_request = _AnonymousRequest()
    override.matched_by_prefix = False
    assert can_view_snapshot(full_link_request, override) is True

    # Admin access is preserved on every surface.
    class _AdminRequest:
        user = admin_user

    assert can_view_snapshot(_AdminRequest(), stale) is True
    assert can_view_snapshot(_AdminRequest(), override) is True


def test_tightening_propagation_resumes_from_cursor_after_a_real_file_failure(admin_user):
    from archivebox.core.models import Snapshot
    from archivebox.services.permission_service import advance_pending_publishes, publish_crawl_permissions

    crawl = _crawl(PERMISSIONS_PUBLIC, user=admin_user)
    snapshots = [_snapshot(crawl, f"https://publish.example/{i}") for i in range(4)]
    for snapshot in snapshots:
        _write_static_manifests(snapshot)
        assert _read_manifest_permissions(snapshot) == (PERMISSIONS_PUBLIC, 1)

    # A real filesystem obstruction: index.html is a directory, so rewriting
    # the snapshot's HTML detail manifest raises instead of silently skipping.
    blocked = snapshots[1]
    blocked_html = blocked.output_dir / "index.html"
    blocked_html.unlink()
    blocked_html.mkdir()

    publish = publish_crawl_permissions(crawl, PERMISSIONS_PRIVATE, user=admin_user)
    first = advance_pending_publishes(publish_id=publish.id, max_snapshots=2)
    blocked.refresh_from_db()
    # Operator clears the obstruction and restores the stale export file; the
    # publication then rewrites it in place (it never creates new exports).
    blocked_html.rmdir()
    blocked_html.write_text("stale legacy export", encoding="utf-8")

    # The run that hit the obstruction stays durable and resumable.
    publish.refresh_from_db()
    if publish.status == publish.StatusChoices.PENDING:
        assert publish.last_error
        assert publish.processed_count >= 1
        first = {**first, "completed": 0}

    while True:
        result = advance_pending_publishes(publish_id=publish.id, max_snapshots=2)
        publish.refresh_from_db()
        if publish.status == publish.StatusChoices.COMPLETED:
            break
        if result.get("advanced", 0) == 0 and publish.status == publish.StatusChoices.PENDING:
            pytest.fail(f"permission publish stalled at cursor={publish.cursor}: {publish.last_error}")

    for snapshot in snapshots:
        snapshot.refresh_from_db()
        assert snapshot.permissions == PERMISSIONS_PRIVATE
        assert snapshot.permissions_version == publish.version
        assert _read_manifest_permissions(snapshot) == (PERMISSIONS_PRIVATE, publish.version)
        html = (snapshot.output_dir / "index.html").read_text(encoding="utf-8")
        assert f'name="archivebox:permissions-version" content="{publish.version}"' in html
        assert 'name="archivebox:permissions" content="private"' in html
    assert (
        Snapshot.objects.filter(crawl=crawl, permissions_override=False)
        .exclude(
            permissions_version=publish.version,
        )
        .count()
        == 0
    )


def test_relaxation_does_not_open_a_snapshot_until_its_artifacts_are_rewritten(admin_user):
    from archivebox.services.permission_service import advance_pending_publishes, publish_crawl_permissions

    crawl = _crawl(PERMISSIONS_PRIVATE, user=admin_user)
    first = _snapshot(crawl, "https://publish.example/a")
    second = _snapshot(crawl, "https://publish.example/b")
    _write_static_manifests(first)
    _write_static_manifests(second)

    publish = publish_crawl_permissions(crawl, PERMISSIONS_PUBLIC, user=admin_user)
    assert publish is not None

    advance_pending_publishes(publish_id=publish.id, max_snapshots=1)
    first.refresh_from_db()
    second.refresh_from_db()
    # The processed snapshot is public with rewritten, versioned metadata...
    if first.permissions_version == publish.version:
        assert first.permissions == PERMISSIONS_PUBLIC
        assert _read_manifest_permissions(first) == (PERMISSIONS_PUBLIC, publish.version)
        # ...but the snapshot the batch has not reached stays fully closed.
        assert effective_snapshot_permissions(second) == PERMISSIONS_PRIVATE
        assert not public_snapshots_queryset(type(second).objects.all()).filter(pk=second.pk).exists()
        assert _read_manifest_permissions(second) == (PERMISSIONS_PRIVATE, 1)
    else:
        # Batch boundary may land between snapshots; nothing may be open yet.
        assert effective_snapshot_permissions(first) == PERMISSIONS_PRIVATE
        assert effective_snapshot_permissions(second) == PERMISSIONS_PRIVATE

    while True:
        advance_pending_publishes(publish_id=publish.id, max_snapshots=2)
        publish.refresh_from_db()
        if publish.status == publish.StatusChoices.COMPLETED:
            break

    for snapshot in (first, second):
        snapshot.refresh_from_db()
        assert effective_snapshot_permissions(snapshot) == PERMISSIONS_PUBLIC
        assert _read_manifest_permissions(snapshot) == (PERMISSIONS_PUBLIC, publish.version)
    anon = _AnonymousRequest()
    assert can_view_snapshot(anon, first) and can_view_snapshot(anon, second)


def test_snapshots_created_mid_publication_inherit_the_current_version(admin_user):
    from archivebox.core.models import Snapshot
    from archivebox.services.permission_service import advance_pending_publishes, publish_crawl_permissions

    crawl = _crawl(PERMISSIONS_PRIVATE, user=admin_user)
    _snapshot(crawl, "https://publish.example/old")
    publish = publish_crawl_permissions(crawl, PERMISSIONS_PUBLIC, user=admin_user)

    # A runner creating a Snapshot while the parent publication is still in
    # flight must inherit the *current* intent, not the stale on-disk version.
    concurrent = _snapshot(crawl, "https://publish.example/new")
    assert concurrent.permissions == PERMISSIONS_PUBLIC
    assert concurrent.permissions_version == publish.version
    assert public_snapshots_queryset(Snapshot.objects.all()).filter(pk=concurrent.pk).exists()

    advance_pending_publishes(publish_id=publish.id)
    publish.refresh_from_db()
    assert publish.status == publish.StatusChoices.COMPLETED
    concurrent.refresh_from_db()
    assert concurrent.permissions_version == publish.version


def test_explicit_single_snapshot_override_survives_later_parent_jobs(admin_user):
    from archivebox.services.permission_service import (
        advance_pending_publishes,
        publish_crawl_permissions,
        publish_snapshot_permissions,
    )

    crawl = _crawl(PERMISSIONS_PUBLIC, user=admin_user)
    snapshot = _snapshot(crawl)
    cookie_before = _mint_replay_credential(snapshot, admin_user)
    assert _replay_cookie_accepted(snapshot, admin_user, value=cookie_before)

    override_publish = publish_snapshot_permissions([snapshot], PERMISSIONS_UNLISTED, user=admin_user)[0]
    advance_pending_publishes(publish_id=override_publish.id)
    snapshot.refresh_from_db()
    assert snapshot.permissions_override is True
    assert snapshot.permissions == PERMISSIONS_UNLISTED
    # The explicit edit rotated this snapshot's replay epoch on its own stream.
    assert not _replay_cookie_accepted(snapshot, admin_user, value=cookie_before)

    # A late crawl tightening job must not drag the override back to private.
    tighten = publish_crawl_permissions(crawl, PERMISSIONS_PRIVATE, user=admin_user)
    advance_pending_publishes(publish_id=tighten.id)
    snapshot.refresh_from_db()
    assert snapshot.permissions == PERMISSIONS_UNLISTED
    assert snapshot.permissions_override is True
    # A stale worker finishing the old v1 publication after the v2 intent can
    # never write the old value back: its CAS only matches rows below v2.
    assert effective_snapshot_permissions(snapshot) == PERMISSIONS_UNLISTED

    # And a subsequent relaxation leaves the explicit override untouched too.
    relax = publish_crawl_permissions(crawl, PERMISSIONS_PUBLIC, user=admin_user)
    advance_pending_publishes(publish_id=relax.id)
    snapshot.refresh_from_db()
    assert snapshot.permissions == PERMISSIONS_UNLISTED
    assert snapshot.permissions_override is True
    assert effective_snapshot_permissions(snapshot) == PERMISSIONS_UNLISTED


def test_single_snapshot_tightening_revokes_old_replay_grant_immediately(admin_user):
    from archivebox.services.permission_service import advance_pending_publishes, publish_snapshot_permissions

    crawl = _crawl(PERMISSIONS_PUBLIC, user=admin_user)
    snapshot = _snapshot(crawl)

    old_cookie = _mint_replay_credential(snapshot, admin_user)
    assert _replay_cookie_accepted(snapshot, admin_user, value=old_cookie)
    publish = publish_snapshot_permissions([snapshot], PERMISSIONS_PRIVATE, user=admin_user)[0]

    # Tightening flips the row immediately: the old-epoch cookie is inert
    # before any static-file work runs.
    snapshot.refresh_from_db()
    assert snapshot.permissions == PERMISSIONS_PRIVATE
    assert not _replay_cookie_accepted(snapshot, admin_user, value=old_cookie)

    advance_pending_publishes(publish_id=publish.id)
    publish.refresh_from_db()
    assert publish.status == publish.StatusChoices.COMPLETED


@override_settings(PUBLIC_INDEX=True)
def test_recovery_resumes_interrupted_publication_and_leaves_no_mixed_state(client, admin_user):
    from archivebox.core.models import Snapshot
    from archivebox.core.recovery_util import recover_orchestrator_state
    from archivebox.services.permission_service import (
        advance_pending_publishes,
        publish_crawl_permissions,
    )

    crawl = _crawl(PERMISSIONS_PUBLIC, user=admin_user)
    snapshots = [_snapshot(crawl, f"https://publish.example/r{i}") for i in range(3)]
    for snapshot in snapshots:
        _write_static_manifests(snapshot)

    publish = publish_crawl_permissions(crawl, PERMISSIONS_PRIVATE, user=admin_user)
    # Simulate a service killed mid-batch: one child converged, the publish is
    # left RUNNING with a durable cursor.
    first = snapshots[0]
    Snapshot.objects.filter(pk=first.pk).update(
        config={**first.config, "PERMISSIONS": PERMISSIONS_PRIVATE}, permissions_version=publish.version
    )
    type(publish).objects.filter(pk=publish.id).update(
        status=publish.StatusChoices.RUNNING,
        cursor=str(first.id),
        processed_count=1,
    )

    cleaned = recover_orchestrator_state()
    assert cleaned["permission_publishes_interrupted"] >= 1
    publish.refresh_from_db()
    assert publish.status == publish.StatusChoices.PENDING

    advance_pending_publishes()
    publish.refresh_from_db()
    assert publish.status == publish.StatusChoices.COMPLETED
    assert (
        not Snapshot.objects.filter(crawl=crawl, permissions_override=False)
        .exclude(
            permissions_version=publish.version,
        )
        .exists()
    )
    for snapshot in snapshots:
        assert _read_manifest_permissions(snapshot) == (PERMISSIONS_PRIVATE, publish.version)

    # The public list and direct links agree: no half-hidden collection.
    response = client.get("/public", HTTP_HOST=WEB_TEST_HOST)
    body = response.content.decode()
    assert all(str(snapshot.id) not in body for snapshot in snapshots)


def test_static_export_still_contains_private_rows_and_carries_versions(admin_user):
    from archivebox.core.models import Snapshot

    crawl = _crawl(PERMISSIONS_PRIVATE, user=admin_user)
    snapshot = _snapshot(crawl)
    record = snapshot.to_dict(extended=True, static_export=True)
    assert record["permissions"] == PERMISSIONS_PRIVATE
    assert int(record["permissions_version"]) >= 1
    assert "permissions_override" in snapshot.to_json()

    # Static export is a portable, owner-held artifact: it must keep exporting
    # every row regardless of visibility (no permission regression there).
    exported = Snapshot.objects.all().to_json(with_headers=False)
    assert str(snapshot.id) in exported
    html = Snapshot.objects.all().to_html(with_headers=False)
    assert snapshot.url in html


@pytest.mark.timeout(420)
@pytest.mark.django_db(transaction=True)
def test_crawl_retightening_closes_anonymous_links_and_old_replay_cookies_over_http(tmp_path, recursive_test_site):
    """End-to-end: private -> public -> private must close every surface.

    A replay cookie minted for the original private snapshot and the direct
    snapshot links that worked while the crawl was public both have to fail
    for anonymous visitors on the re-tightened crawl, while the logged-in
    admin keeps access.
    """
    import socket
    import time

    import requests
    from archivebox.tests.conftest import (
        cli_env,
        create_admin_and_token,
        get_free_port,
        init_archive,
        run_archivebox_cmd,
        start_archivebox_server,
        stop_server,
    )
    from archivebox.tests.test_ui_public_snapshot import (
        _create_private_snapshot_over_full_server,
        _login_admin_over_full_server,
    )

    # The test server binds 127.0.0.1; *.localhost must route there. Some CI
    # hosts wildcard .localhost at a non-loopback resolver, so normalize only
    # this client's name resolution (the requests still hit the real server).
    real_getaddrinfo = socket.getaddrinfo

    def _loopback_getaddrinfo(host, *args, **kwargs):
        if isinstance(host, str) and host.endswith(".archivebox.localhost"):
            return real_getaddrinfo("127.0.0.1", *args, **kwargs)
        return real_getaddrinfo(host, *args, **kwargs)

    socket.getaddrinfo = _loopback_getaddrinfo
    init_archive(tmp_path)
    create_admin_and_token(tmp_path)
    port = get_free_port()
    env = cli_env(
        port=port,
        server=True,
        SERVER_SECURITY_MODE="safe-subdomains-fullreplay",
        PUBLIC_ADD_VIEW="True",
        PUBLIC_INDEX="True",
    )

    try:
        start_archivebox_server(tmp_path, env=env, port=port)
        session, csrf_token = _login_admin_over_full_server(port)
        snapshot = _create_private_snapshot_over_full_server(
            tmp_path,
            session,
            port,
            csrf_token,
            recursive_test_site["root_url"],
        )
        snap_url = f"http://{snapshot['host']}/index.html"
        authorized = session.get(snap_url, timeout=10, allow_redirects=True)
        assert authorized.status_code == 200
        old_cookie = next(c.value for c in session.cookies if c.name.startswith("archivebox_replay_"))
        old_cookie_name = next(c.name for c in session.cookies if c.name.startswith("archivebox_replay_"))

        crawl_id = (
            run_archivebox_cmd(
                [
                    "manage",
                    "shell",
                    "-c",
                    f"from archivebox.core.models import Snapshot; print(Snapshot.objects.get(id={snapshot['id']!r}).crawl_id)",
                ],
                cwd=tmp_path,
                env=env,
                timeout=30,
            )
            .stdout.strip()
            .splitlines()[-1]
        )

        def _set_crawl_permissions(permissions: str) -> None:
            csrf = next(
                (cookie.value for cookie in session.cookies if cookie.name.startswith("archivebox_csrftoken_")),
                "",
            )
            response = session.post(
                f"http://admin.archivebox.localhost:{port}/admin/crawls/crawl/{crawl_id}/set-permissions/",
                headers={
                    "Referer": f"http://admin.archivebox.localhost:{port}/admin/",
                    "X-CSRFToken": csrf,
                },
                data={"permissions": permissions, "csrfmiddlewaretoken": csrf},
                timeout=10,
            )
            assert response.status_code == 200, response.text

        # Relax to public and let the runner finish the publication; the
        # snapshot becomes anonymously readable only after its artifacts are
        # rewritten.
        _set_crawl_permissions("public")
        anon = requests.Session()

        def _anon_opened(response) -> bool:
            if response is None:
                return False
            if response.status_code != 302:
                return response.status_code == 200
            # While still private the snap host redirects only to replay-auth;
            # an opened snapshot redirects to its web detail page.
            return "/admin/core/snapshot/replay-auth/" not in response.headers.get("Location", "")

        opened = None
        for _ in range(60):
            try:
                opened = anon.get(
                    snap_url,
                    timeout=10,
                    allow_redirects=False,
                    headers={"Connection": "close"},
                )
            except requests.exceptions.RequestException:
                # Transient keep-alive/scheduling hiccup on a loaded host.
                opened = None
                time.sleep(1)
                continue
            if _anon_opened(opened):
                break
            time.sleep(1)
        assert opened is not None and _anon_opened(opened), getattr(opened, "headers", None)

        # Re-tighten: direct anonymous links and the old replay grant die with
        # the new intent, even before the leftover redirect is followed.
        _set_crawl_permissions("private")

        def _get_anon():
            for attempt in range(3):
                try:
                    return anon.get(snap_url, timeout=10, allow_redirects=False, headers={"Connection": "close"})
                except requests.exceptions.RequestException:
                    # Daphne may drop or stall an idle connection under load.
                    if attempt == 2:
                        raise
                    time.sleep(0.5)

        for _ in range(20):
            closed = _get_anon()
            if closed.status_code in (302, 303) and "/admin/core/snapshot/replay-auth/" in closed.headers.get("Location", ""):
                break
            time.sleep(0.5)
        assert closed.status_code in (302, 303)
        assert "/admin/core/snapshot/replay-auth/" in closed.headers["Location"]

        stale = requests.Session()
        stale.cookies.set(old_cookie_name, old_cookie, domain=snapshot["host"].split(":", 1)[0], path="/")
        stale_response = stale.get(
            snap_url,
            timeout=10,
            allow_redirects=False,
            headers={"Connection": "close"},
        )
        assert stale_response.status_code in (302, 303)
        assert "/admin/core/snapshot/replay-auth/" in stale_response.headers["Location"]

        # Admin access is preserved through the same change.
        admin_response = session.get(snap_url, timeout=10, allow_redirects=True)
        assert admin_response.status_code == 200
    finally:
        socket.getaddrinfo = real_getaddrinfo
        stop_server(tmp_path)


def test_unlisted_direct_link_rules_are_preserved_through_effective_permissions(admin_user):
    crawl = _crawl(PERMISSIONS_UNLISTED, user=admin_user)
    snapshot = _snapshot(crawl)
    anon = _AnonymousRequest()

    snapshot.matched_by_prefix = False
    assert can_view_snapshot(anon, snapshot) is True
    snapshot.matched_by_prefix = True
    assert can_view_snapshot(anon, snapshot) is False
    assert not public_snapshots_queryset(type(snapshot).objects.all()).filter(pk=snapshot.pk).exists()
    assert direct_snapshots_queryset(anon, type(snapshot).objects.all()).filter(pk=snapshot.pk).exists()
