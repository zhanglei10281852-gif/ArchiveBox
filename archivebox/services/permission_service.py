"""Durable, recoverable permission publications.

Every Crawl/Snapshot visibility change is a publication with a monotonically
increasing version that is shared by the parent intent, child Snapshot rows,
static JSON/HTML metadata, and replay authorizations:

* The intent row commits first. Tightening therefore fails closed on every
  online surface before any child is rewritten, and previously minted replay
  cookies become inert via epoch comparison.
* Child rows converge in bounded, compare-and-swapped batches keyed by a
  durable cursor, so a killed orchestrator or a partial file rewrite resumes
  from an explicit position instead of leaving permanently mixed permissions.
* Relaxation inverts the order per child: static JSON/HTML metadata is
  rewritten first, and only then does the row flip open, so nothing is
  publicly reachable before its artifacts match the new intent.
* Explicit single-snapshot overrides are versioned on their own stream and
  are never touched (or rolled back) by parent publications, even late ones.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import TYPE_CHECKING, Iterable

from django.db.models import F
from django.utils import timezone

from archivebox.core.permissions import (
    PERMISSIONS_RANK,
    PERMISSIONS_VALUES,
    normalize_permissions,
)

if TYPE_CHECKING:
    from archivebox.core.models import PermissionPublish, Snapshot

# Child row convergence in the request/CLI path stays DB-only and bounded;
# filesystem rewrites always belong to the orchestrator.
INLINE_ROW_BUDGET = 200
ORCHESTRATOR_BATCH_SIZE = 100

# Scope keys accepted by inline advancement.
_CRAWL_SCOPE = "crawl"
_SNAPSHOT_SCOPE = "snapshot"


def _models():
    from archivebox.core.models import PermissionPublish, Snapshot
    from archivebox.crawls.models import Crawl

    return Crawl, Snapshot, PermissionPublish


def _is_tightening(old_permissions: str, new_permissions: str) -> bool:
    return PERMISSIONS_RANK[new_permissions] > PERMISSIONS_RANK[old_permissions]


def _system_user_pk():
    from archivebox.base_models.models import get_or_create_system_user_pk

    return get_or_create_system_user_pk()


def _set_config_permissions(config: dict | None, permissions: str) -> dict:
    config = dict(config or {})
    config["PERMISSIONS"] = permissions
    return config


# ============================================================================
# Publication requests
# ============================================================================


def publish_crawl_permissions(crawl, new_permissions: str, *, user=None):
    """Admin/API entry point: compare-and-swap a crawl's PERMISSIONS intent.

    The crawl row (config + version) is updated here, not by the caller, so
    intent bump and durable publication always happen as one operation.
    Returns the publication, or ``None`` when the crawl already carries the
    requested permission.
    """
    Crawl, _Snapshot, PermissionPublish = _models()

    new_permissions = normalize_permissions(new_permissions)
    if new_permissions not in PERMISSIONS_VALUES:
        raise ValueError(f"Invalid permissions: {new_permissions}")

    row = Crawl.objects.filter(pk=crawl.pk).values("permissions", "permissions_version", "config").first()
    if row is None:
        return None
    current = normalize_permissions(row["permissions"])
    if current == new_permissions:
        return None

    version = int(row["permissions_version"] or 1) + 1
    updated = Crawl.objects.filter(pk=crawl.pk, permissions_version=row["permissions_version"]).update(
        config=_set_config_permissions(row["config"], new_permissions),
        permissions_version=version,
        modified_at=timezone.now(),
    )
    if updated != 1:
        # A concurrent request already moved the intent; that publication owns
        # convergence.
        return None

    crawl.permissions_version = version
    crawl.config = _set_config_permissions(row["config"], new_permissions)

    return _create_crawl_publication(
        crawl_id=crawl.pk,
        old_permissions=current,
        new_permissions=new_permissions,
        version=version,
        user=user,
    )


def handle_committed_crawl_permission_change(
    *,
    crawl_id,
    old_permissions: str | None,
    old_permissions_version: int,
    new_permissions: str | None,
):
    """Post-commit hook for Crawl.save() callers that edited config directly.

    Only the version column is compare-and-swapped here (the config value is
    already committed). A concurrent :func:`publish_crawl_permissions` that
    advanced the version first wins and this becomes a no-op.
    """
    Crawl, _Snapshot, PermissionPublish = _models()

    new_permissions = normalize_permissions(new_permissions)
    old_version = int(old_permissions_version or 1)
    row = Crawl.objects.filter(pk=crawl_id).values("permissions", "permissions_version").first()
    if row is None:
        return None
    if normalize_permissions(row["permissions"]) != new_permissions:
        return None
    if int(row["permissions_version"] or 1) != old_version:
        return None

    version = old_version + 1
    updated = Crawl.objects.filter(pk=crawl_id, permissions_version=old_version).update(
        permissions_version=version,
        modified_at=timezone.now(),
    )
    if updated != 1:
        return None
    return _create_crawl_publication(
        crawl_id=crawl_id,
        old_permissions=normalize_permissions(old_permissions),
        new_permissions=new_permissions,
        version=version,
    )


def _create_crawl_publication(*, crawl_id, old_permissions: str, new_permissions: str, version: int, user=None):
    Crawl, Snapshot, PermissionPublish = _models()

    existing = PermissionPublish.objects.filter(
        scope_type=_CRAWL_SCOPE,
        crawl_id=crawl_id,
        version=version,
    ).first()
    if existing is not None:
        return existing

    snapshot_count = Snapshot.objects.filter(crawl_id=crawl_id, permissions_override=False).count()
    publish = PermissionPublish.objects.create(
        scope_type=_CRAWL_SCOPE,
        crawl_id=crawl_id,
        old_permissions=old_permissions,
        new_permissions=new_permissions,
        version=version,
        status=PermissionPublish.StatusChoices.PENDING,
        phase=PermissionPublish.PhaseChoices.ROWS,
        snapshot_count=snapshot_count,
        created_by=user if user is not None and getattr(user, "pk", None) else _system_user_pk(),
    )
    # Tightening converges the DB gate in the request path up to a small,
    # DB-only budget so ordinary crawls are uniformly closed immediately.
    if _is_tightening(old_permissions, new_permissions):
        _advance_publication(publish, max_snapshots=INLINE_ROW_BUDGET, db_only=True)
    return publish


def publish_snapshot_permissions(snapshots: Iterable["Snapshot"], new_permissions: str, *, user=None):
    """Request explicit single-snapshot visibility changes.

    Each snapshot becomes its own publication on its own version stream;
    completing one marks the snapshot as an override so later parent jobs can
    never change it back.
    """
    _Crawl, Snapshot, PermissionPublish = _models()

    new_permissions = normalize_permissions(new_permissions)
    if new_permissions not in PERMISSIONS_VALUES:
        raise ValueError(f"Invalid permissions: {new_permissions}")

    publications: list[PermissionPublish] = []
    for snapshot in snapshots:
        current_row = (
            Snapshot.objects.filter(pk=snapshot.pk)
            .select_related("crawl")
            .values(
                "permissions",
                "permissions_version",
                "permissions_override",
                "crawl__permissions",
                "crawl__permissions_version",
            )
            .first()
        )
        if current_row is None:
            continue
        # Compute effective visibility with the exact same rule as the
        # request gates (an inherited row takes the stricter crawl intent).
        row_permissions = normalize_permissions(current_row["permissions"])
        if current_row["permissions_override"]:
            current = row_permissions
        else:
            from archivebox.core.permissions import stricter_permissions

            current = stricter_permissions(row_permissions, normalize_permissions(current_row["crawl__permissions"]))
        if current == new_permissions and not (
            current_row["permissions_override"] and normalize_permissions(current_row["crawl__permissions"]) == new_permissions
        ):
            continue

        version = int(current_row["permissions_version"] or 1) + 1
        existing = PermissionPublish.objects.filter(
            scope_type=_SNAPSHOT_SCOPE,
            snapshot_id=snapshot.pk,
            version=version,
        ).first()
        if existing is not None:
            publications.append(existing)
            continue

        publish = PermissionPublish.objects.create(
            scope_type=_SNAPSHOT_SCOPE,
            snapshot_id=snapshot.pk,
            old_permissions=current,
            new_permissions=new_permissions,
            version=version,
            status=PermissionPublish.StatusChoices.PENDING,
            phase=PermissionPublish.PhaseChoices.ROWS,
            snapshot_count=1,
            created_by=user if user is not None and getattr(user, "pk", None) else _system_user_pk(),
        )
        # Tightening flips the row immediately: the online gate and replay
        # epoch close before any static-file work is scheduled.
        if _is_tightening(current, new_permissions):
            _flip_snapshot_row(
                snapshot.pk,
                new_permissions,
                version,
                expect_version=current_row["permissions_version"],
                override=True,
            )
        publications.append(publish)
    return publications


# ============================================================================
# Row convergence
# ============================================================================


def _flip_snapshot_row(snapshot_id, permissions: str, version: int, *, expect_version: int, override: bool | None = None) -> bool:
    """Compare-and-swap one snapshot row to a newer intent version.

    The ``permissions_version < version`` guard means a stale worker finishing
    an old publication can never write an old value over a newer row.
    """
    _Crawl, Snapshot, _PermissionPublish = _models()

    snapshot = Snapshot.objects.filter(pk=snapshot_id).only("config", "permissions_version").first()
    if snapshot is None:
        return False
    values = {
        "config": _set_config_permissions(snapshot.config, permissions),
        "modified_at": timezone.now(),
    }
    queryset = Snapshot.objects.filter(pk=snapshot_id, permissions_version=expect_version)
    if override is None:
        values["permissions_version"] = version
        queryset = queryset.filter(permissions_version__lt=version)
    else:
        values["permissions_version"] = version
        values["permissions_override"] = override
    updated = queryset.update(**values)
    return updated == 1


def _converge_crawl_rows(publish, *, max_snapshots: int) -> int:
    """Tightening phase 1: move inherited child rows to the new intent."""
    _Crawl, Snapshot, _PermissionPublish = _models()

    stale_ids = list(
        Snapshot.objects.filter(
            crawl_id=publish.crawl_id,
            permissions_override=False,
            permissions_version__lt=publish.version,
        )
        .order_by("id")
        .values_list("id", flat=True)[:max_snapshots]
    )
    if not stale_ids:
        return 0
    # Re-check immediately before writing: an explicit override landing in
    # the same instant must never be reverted by the parent publication.
    still_inherited = set(
        Snapshot.objects.filter(
            pk__in=stale_ids,
            permissions_override=False,
            permissions_version__lt=publish.version,
        ).values_list("id", flat=True)
    )
    snapshots = [
        snapshot
        for snapshot in Snapshot.objects.filter(pk__in=stale_ids).only("id", "config", "permissions_version")
        if snapshot.id in still_inherited
    ]
    if not snapshots:
        return 0
    now = timezone.now()
    for snapshot in snapshots:
        snapshot.config = _set_config_permissions(snapshot.config, publish.new_permissions)
        snapshot.permissions_version = publish.version
        snapshot.modified_at = now
    Snapshot.objects.filter(pk__in=stale_ids, permissions_override=False, permissions_version__lt=publish.version).bulk_update(
        snapshots,
        ["config", "permissions_version", "modified_at"],
        batch_size=len(snapshots),
    )
    return len(snapshots)


# ============================================================================
# Static metadata convergence
# ============================================================================


class StaticManifestWriteError(OSError):
    """A static detail manifest could not be rewritten.

    ``atomic_write`` reports failed/unsupported writes as SystemExit by
    design; normalize that (and ordinary OSErrors from a stale or
    unreachable mount) into one recorded, retryable publication failure
    instead of letting the orchestrator die mid-convergence.
    """


def _rewrite_snapshot_static(snapshot, *, target_permissions: str | None = None, target_version: int | None = None) -> None:
    """Rewrite only the static detail manifests that already exist on disk.

    Never creates new artifacts for snapshots that were never exported, and
    never touches archived payload bytes. During relaxation the row is still
    closed, so the manifests are rendered from the in-memory *target* intent
    first; the row only opens after this returns.
    """
    output_dir = Path(snapshot.output_dir)
    if not output_dir.is_dir():
        return
    projected = False
    if target_permissions is not None and target_permissions != snapshot.permissions:
        snapshot.config = _set_config_permissions(snapshot.config, target_permissions)
        snapshot.__dict__["permissions"] = target_permissions
        if target_version is not None:
            snapshot.permissions_version = int(target_version)
        projected = True
    try:
        if (output_dir / "index.json").exists():
            snapshot.write_json_details()
        if (output_dir / "index.html").exists():
            snapshot.write_html_details()
        if (output_dir / "index.jsonl").exists():
            snapshot.write_index_jsonl()
    except (OSError, SystemExit) as err:
        raise StaticManifestWriteError(f"{type(err).__name__}: {err}") from err
    finally:
        if projected:
            # Keep the caller's DB-state expectations intact; the row flip is
            # the caller's responsibility.
            snapshot.refresh_from_db()


def _remaining_inherited_ids(publish, *, after_id=None, static: bool = False):
    _Crawl, Snapshot, _PermissionPublish = _models()

    queryset = Snapshot.objects.filter(crawl_id=publish.crawl_id, permissions_override=False).order_by("id")
    if static:
        # Static convergence covers every inherited snapshot at the target
        # version; rows still lagging are handled by the rows phase first.
        queryset = queryset.filter(permissions_version=publish.version)
    else:
        queryset = queryset.filter(permissions_version__lt=publish.version)
    if after_id:
        queryset = queryset.filter(id__gt=after_id)
    return queryset


def _rewrite_collection_indexes() -> None:
    """Regenerate collection-level static exports when they already exist.

    Portable exports are owner-held artifacts that list every row; this only
    refreshes files the operator already generated (never creates new ones),
    and it reuses the exact CLI export path so the regenerated files stay
    byte-faithful to a fresh ``archivebox list --html/--json`` run.
    """
    from archivebox.config import CONSTANTS
    from archivebox.core.models import Snapshot
    from archivebox.misc.system import atomic_write

    data_dir = Path(CONSTANTS.DATA_DIR)
    json_path = data_dir / CONSTANTS.JSON_INDEX_FILENAME
    jsonl_path = data_dir / CONSTANTS.JSONL_INDEX_FILENAME
    html_path = data_dir / CONSTANTS.HTML_INDEX_FILENAME

    if not (json_path.exists() or jsonl_path.exists() or html_path.exists()):
        return

    queryset = Snapshot.objects.all().order_by("-bookmarked_at", "-created_at").prefetch_related("tags")
    if json_path.exists():
        atomic_write(str(json_path), queryset.to_json(with_headers=True))
    if html_path.exists():
        # to_html(with_headers=True) also refreshes the JSONL manifest and
        # every per-snapshot detail page, exactly like a normal static export.
        atomic_write(str(html_path), queryset.to_html(with_headers=True))
    elif jsonl_path.exists():
        from archivebox.misc.util import to_json

        manifest = "".join(
            f"{to_json(snapshot.to_dict(extended=True, static_export=True), indent=None, sort_keys=True)}\n"
            for snapshot in queryset.iterator(chunk_size=500)
        )
        atomic_write(str(jsonl_path), manifest)


# ============================================================================
# Publication advancement
# ============================================================================


def _claim(publish) -> bool:
    _Crawl, _Snapshot, PermissionPublish = _models()

    now = timezone.now()
    updated = (
        PermissionPublish.objects.filter(pk=publish.pk)
        .filter(
            # Claim only non-running rows so two orchestrators cannot fan out the
            # same batch concurrently.
            status__in=(PermissionPublish.StatusChoices.PENDING, PermissionPublish.StatusChoices.FAILED),
        )
        .update(status=PermissionPublish.StatusChoices.RUNNING, started_at=now, modified_at=now)
    )
    if updated:
        publish.status = PermissionPublish.StatusChoices.RUNNING
        publish.started_at = now
    return updated == 1


def _release(publish, *, status: str, error: str = "", phase: str | None = None) -> None:
    _Crawl, _Snapshot, PermissionPublish = _models()

    values = {"status": status, "last_error": error[:5000], "modified_at": timezone.now()}
    if phase is not None:
        values["phase"] = phase
    if status == PermissionPublish.StatusChoices.COMPLETED:
        values["completed_at"] = timezone.now()
    PermissionPublish.objects.filter(pk=publish.pk).update(**values)
    for key, value in values.items():
        setattr(publish, key, value)


def _advance_snapshot_publication(publish, *, max_snapshots: int, db_only: bool) -> bool:
    _Crawl, Snapshot, PermissionPublish = _models()

    snapshot = Snapshot.objects.select_related("crawl").filter(pk=publish.snapshot_id).first()
    if snapshot is None:
        _release(publish, status=PermissionPublish.StatusChoices.COMPLETED, phase=PermissionPublish.PhaseChoices.COMPLETED)
        return True

    # A newer explicit edit or an out-of-band state matching the target ends
    # this publication's ownership.
    if int(snapshot.permissions_version or 1) > publish.version and snapshot.permissions == publish.new_permissions:
        _release(publish, status=PermissionPublish.StatusChoices.SUPERSEDED)
        return True

    if publish.tightens:
        if snapshot.permissions_version < publish.version or snapshot.permissions != publish.new_permissions:
            _flip_snapshot_row(
                snapshot.pk,
                publish.new_permissions,
                publish.version,
                expect_version=snapshot.permissions_version,
                override=True,
            )
        if db_only:
            return False
    else:
        if db_only:
            return False
        if snapshot.permissions_version < publish.version:
            # Relaxation: metadata must match the new intent before the row
            # opens. Override state is resolved at completion against the
            # current crawl intent.
            _rewrite_snapshot_static(
                snapshot,
                target_permissions=publish.new_permissions,
                target_version=publish.version,
            )
            _flip_snapshot_row(
                snapshot.pk,
                publish.new_permissions,
                publish.version,
                expect_version=snapshot.permissions_version,
                override=True,
            )

    snapshot.refresh_from_db()
    if snapshot.permissions_version < publish.version:
        return False

    _finish_snapshot_publication(publish, snapshot)
    return publish.status == PermissionPublish.StatusChoices.COMPLETED


def _finish_snapshot_publication(publish, snapshot) -> None:
    _Crawl, Snapshot, PermissionPublish = _models()

    # Choosing the crawl's current value clears the override and rejoins the
    # inherited stream; anything else stays an explicit override.
    crawl = snapshot.crawl
    if crawl is not None and normalize_permissions(crawl.permissions) == publish.new_permissions:
        Snapshot.objects.filter(pk=snapshot.pk).update(
            permissions_override=False,
            permissions_version=crawl.permissions_version,
            modified_at=timezone.now(),
        )
    _release(publish, status=PermissionPublish.StatusChoices.COMPLETED, phase=PermissionPublish.PhaseChoices.COMPLETED)


def _advance_crawl_publication(publish, *, max_snapshots: int, db_only: bool) -> bool:
    """Advance one crawl publication by a bounded batch.

    Returns True when the publication reached a terminal state.
    """
    Crawl, Snapshot, PermissionPublish = _models()

    crawl = Crawl.objects.filter(pk=publish.crawl_id).first()
    if crawl is None:
        _release(publish, status=PermissionPublish.StatusChoices.COMPLETED, phase=PermissionPublish.PhaseChoices.COMPLETED)
        return True
    if int(crawl.permissions_version or 1) > publish.version:
        # A newer intent publication owns convergence now.
        _release(publish, status=PermissionPublish.StatusChoices.SUPERSEDED)
        return True

    if publish.tightens and publish.phase == PermissionPublish.PhaseChoices.ROWS:
        moved = _converge_crawl_rows(publish, max_snapshots=max_snapshots)
        if moved:
            publish.refresh_from_db(fields=["processed_count"])
            PermissionPublish.objects.filter(pk=publish.pk).update(
                processed_count=F("processed_count") + moved,
                modified_at=timezone.now(),
            )
            publish.processed_count = int(publish.processed_count or 0) + moved
        if _remaining_inherited_ids(publish).exists():
            return False
        if db_only:
            return False
        # The static phase walks every inherited snapshot from the start; the
        # rows-phase cursor does not carry over between phases.
        PermissionPublish.objects.filter(pk=publish.pk).update(
            phase=PermissionPublish.PhaseChoices.STATIC,
            cursor=None,
            modified_at=timezone.now(),
        )
        publish.phase = PermissionPublish.PhaseChoices.STATIC
        publish.cursor = None

    if db_only:
        return False

    # Per-child static convergence. Relaxation rewrites first and only then
    # opens the row; tightening rewrites the already-closed converged rows.
    budget = max_snapshots
    cursor = publish.cursor
    progressed = False
    while budget > 0:
        snapshot = (
            _remaining_inherited_ids(
                publish,
                after_id=cursor,
                static=publish.tightens,
            )
            .select_related("crawl")
            .first()
        )
        if snapshot is None:
            break
        try:
            if publish.tightens:
                _rewrite_snapshot_static(snapshot)
            else:
                _rewrite_snapshot_static(
                    snapshot,
                    target_permissions=publish.new_permissions,
                    target_version=publish.version,
                )
                _flip_snapshot_row(
                    snapshot.pk,
                    publish.new_permissions,
                    publish.version,
                    expect_version=snapshot.permissions_version,
                    override=False,
                )
        except OSError as err:
            # Real filesystem failure (stale mount, replaced path, bad
            # permissions): keep the durable cursor before this snapshot so
            # the exact failing item is retried first on resume.
            _release(
                publish,
                status=PermissionPublish.StatusChoices.PENDING,
                error=f"{type(err).__name__}: {err} while rewriting {snapshot.output_dir}",
            )
            return False
        cursor = str(snapshot.id)
        progressed = True
        budget -= 1

    walked_count = Snapshot.objects.filter(crawl_id=publish.crawl_id, permissions_override=False, id__lte=cursor).count() if cursor else 0
    # Progress is monotonic: rows already converged in the first phase must
    # not make the counter appear to move backwards during static walk.
    processed_count = max(int(publish.processed_count or 0), walked_count)
    PermissionPublish.objects.filter(pk=publish.pk).update(
        cursor=cursor,
        processed_count=processed_count,
        modified_at=timezone.now(),
    )
    publish.cursor = cursor
    publish.processed_count = processed_count

    if not publish.tightens and _remaining_inherited_ids(publish).exists():
        _release(publish, status=PermissionPublish.StatusChoices.PENDING)
        return progressed
    if publish.tightens and _remaining_inherited_ids(publish, static=True, after_id=cursor).exists():
        _release(publish, status=PermissionPublish.StatusChoices.PENDING)
        return progressed

    # All children converged; refresh the portable collection indexes once.
    try:
        _rewrite_collection_indexes()
    except (OSError, SystemExit) as err:
        _release(
            publish,
            status=PermissionPublish.StatusChoices.PENDING,
            error=f"{type(err).__name__}: {err} while rewriting collection indexes",
        )
        return False

    _release(publish, status=PermissionPublish.StatusChoices.COMPLETED, phase=PermissionPublish.PhaseChoices.COMPLETED)
    return True


def _advance_publication(publish, *, max_snapshots: int = ORCHESTRATOR_BATCH_SIZE, db_only: bool = False) -> bool:
    _Crawl, _Snapshot, PermissionPublish = _models()

    publish = PermissionPublish.objects.get(pk=publish.pk)
    if publish.status == PermissionPublish.StatusChoices.COMPLETED:
        return True
    if publish.status == PermissionPublish.StatusChoices.SUPERSEDED:
        return True
    if not _claim(publish):
        return False
    try:
        if publish.scope_type == _CRAWL_SCOPE:
            finished = _advance_crawl_publication(publish, max_snapshots=max_snapshots, db_only=db_only)
        else:
            finished = _advance_snapshot_publication(publish, max_snapshots=max_snapshots, db_only=db_only)
    except Exception as err:
        # Record and retry: the durable cursor makes the next pass resume at
        # the exact snapshot instead of restarting the whole publication.
        _release(publish, status=PermissionPublish.StatusChoices.PENDING, error=f"{type(err).__name__}: {err}")
        return False
    if not finished and publish.status == PermissionPublish.StatusChoices.RUNNING:
        _release(publish, status=PermissionPublish.StatusChoices.PENDING)
    return finished


def advance_pending_publishes(
    *,
    publish_id=None,
    max_snapshots: int = ORCHESTRATOR_BATCH_SIZE,
    time_budget: float | None = None,
    db_only: bool = False,
) -> dict:
    """Advance pending publications, oldest intent first (parent ordering).

    ``db_only`` converges child rows without touching archive storage and is
    safe to run from the request path; the orchestrator passes
    ``db_only=False`` to rewrite static metadata.
    """
    _Crawl, _Snapshot, PermissionPublish = _models()

    queryset = (
        PermissionPublish.objects.filter(
            status__in=(
                PermissionPublish.StatusChoices.PENDING,
                PermissionPublish.StatusChoices.RUNNING,
                PermissionPublish.StatusChoices.FAILED,
            )
        )
        .select_related("crawl", "snapshot")
        .order_by("created_at", "id")
    )
    if publish_id is not None:
        queryset = queryset.filter(pk=publish_id)

    started = time.monotonic()
    advanced = 0
    completed = 0
    for publish in queryset:
        if time_budget is not None and time.monotonic() - started >= time_budget:
            break
        was_pending = publish.status in (PermissionPublish.StatusChoices.PENDING, PermissionPublish.StatusChoices.FAILED)
        if _advance_publication(publish, max_snapshots=max_snapshots, db_only=db_only):
            completed += 1
        if was_pending:
            advanced += 1
    return {"advanced": advanced, "completed": completed}


def reset_interrupted_publishes() -> int:
    """Reopen publications left RUNNING by a killed orchestrator process."""
    _Crawl, _Snapshot, PermissionPublish = _models()

    return PermissionPublish.objects.filter(status=PermissionPublish.StatusChoices.RUNNING).update(
        status=PermissionPublish.StatusChoices.PENDING,
        modified_at=timezone.now(),
    )
