from __future__ import annotations

from django.db.models import Case, IntegerField, QuerySet, Value, When
from django.db.models.functions import Greatest
from django.http import HttpRequest

PERMISSIONS_PUBLIC = "public"
PERMISSIONS_UNLISTED = "unlisted"
PERMISSIONS_PRIVATE = "private"
PERMISSIONS_CHOICES = (
    (PERMISSIONS_PUBLIC, "Public"),
    (PERMISSIONS_UNLISTED, "Unlisted"),
    (PERMISSIONS_PRIVATE, "Private"),
)
PERMISSIONS_VALUES = {value for value, _label in PERMISSIONS_CHOICES}
PERMISSIONS_META = {
    PERMISSIONS_PUBLIC: ("👥", "Public", "#047857", "#d1fae5"),
    PERMISSIONS_UNLISTED: ("🔗", "Unlisted", "#1d4ed8", "#dbeafe"),
    PERMISSIONS_PRIVATE: ("🔒", "Private", "#991b1b", "#fee2e2"),
}

# Higher rank = less visible. The *effective* permission of an inherited
# snapshot whose row lags behind its crawl's intent is the stricter of the
# two, so an in-flight tightening fails closed and an in-flight relaxation
# stays closed until the row catches up.
PERMISSIONS_RANK = {
    PERMISSIONS_PUBLIC: 0,
    PERMISSIONS_UNLISTED: 1,
    PERMISSIONS_PRIVATE: 2,
}
RANK_PERMISSIONS = {rank: permission for permission, rank in PERMISSIONS_RANK.items()}


def normalize_permissions(permissions: object, *, default: str = PERMISSIONS_PRIVATE) -> str:
    permissions = str(permissions or "").strip().lower()
    if permissions not in PERMISSIONS_VALUES:
        return default
    return permissions


def stricter_permissions(left: str, right: str) -> str:
    return RANK_PERMISSIONS[max(PERMISSIONS_RANK[left], PERMISSIONS_RANK[right])]


def is_admin_user(request: HttpRequest) -> bool:
    user = request.user
    return bool(user.is_authenticated and user.is_active and user.is_staff)


def effective_snapshot_permissions(snapshot) -> str:
    """Effective anonymous visibility for a snapshot.

    An inherited snapshot is as visible as the *stricter* of its own row and
    its crawl's current intent: a lagging row fails closed immediately during
    a tightening, and a relaxation cannot expose it before the row catches up.
    Explicit per-snapshot overrides follow their own stream and ignore the
    parent intent entirely.
    """
    row_permissions = normalize_permissions(snapshot.permissions)
    if getattr(snapshot, "permissions_override", False):
        return row_permissions

    crawl = _snapshot_crawl(snapshot)
    if crawl is None:
        return row_permissions
    return stricter_permissions(row_permissions, normalize_permissions(getattr(crawl, "permissions", None)))


def _snapshot_crawl(snapshot):
    # select_related("crawl") populates the instance cache; otherwise this
    # performs one lazy row read. This is object-level request gating and
    # must never be called inside a row loop (querysets use the SQL
    # expression instead).
    cached = snapshot.__dict__.get("crawl")
    if cached is not None:
        return cached
    try:
        return snapshot.crawl
    except Exception:
        return None


def snapshot_replay_epoch(snapshot) -> int:
    """Comparable epoch embedded in replay grants/cookies.

    Every new intent rotates the epoch, which invalidates previously issued
    replay authorizations without enumerating sessions. Overrides rotate on
    their own version; inherited snapshots rotate with the crawl intent even
    while their row is still converging.
    """
    row_version = int(getattr(snapshot, "permissions_version", 1) or 1)
    if getattr(snapshot, "permissions_override", False):
        return row_version
    crawl = _snapshot_crawl(snapshot)
    parent_version = int(getattr(crawl, "permissions_version", 1) or 1) if crawl is not None else 1
    return max(row_version, parent_version)


def get_snapshot_permissions(snapshot) -> str:
    # Backwards-compatible alias used by models/admin/templates.
    return effective_snapshot_permissions(snapshot)


def can_view_snapshot(request: HttpRequest, snapshot) -> bool:
    permissions = effective_snapshot_permissions(snapshot)
    unlisted_ok = permissions == PERMISSIONS_UNLISTED and not getattr(snapshot, "matched_by_prefix", False)
    return permissions == PERMISSIONS_PUBLIC or unlisted_ok or is_admin_user(request)


def _persona_ids_for_permissions(allowed_permissions: set[str]) -> list[str]:
    from archivebox.personas.models import Persona

    return [str(persona_id) for persona_id in Persona.objects.filter(permissions__in=allowed_permissions).values_list("id", flat=True)]


def filter_personas_by_permissions(queryset: QuerySet, allowed_permissions: set[str]) -> QuerySet:
    return queryset.filter(id__in=_persona_ids_for_permissions(allowed_permissions))


def effective_permission_rank_expr(prefix: str = ""):
    """SQL expression of the effective permission rank.

    Mirrors effective_snapshot_permissions(): explicit overrides use the row
    value; inherited rows take the stricter of the row and the crawl intent.
    """
    row_field = f"{prefix}permissions"
    row_rank = Case(
        When(**{row_field: PERMISSIONS_PRIVATE}, then=Value(2)),
        When(**{row_field: PERMISSIONS_UNLISTED}, then=Value(1)),
        default=Value(0),
        output_field=IntegerField(),
    )
    crawl_rank = Case(
        When(**{f"{prefix}crawl__permissions": PERMISSIONS_PRIVATE}, then=Value(2)),
        When(**{f"{prefix}crawl__permissions": PERMISSIONS_UNLISTED}, then=Value(1)),
        default=Value(0),
        output_field=IntegerField(),
    )
    return Case(
        When(**{f"{prefix}permissions_override": True}, then=row_rank),
        default=Greatest(row_rank, crawl_rank),
        output_field=IntegerField(),
    )


def filter_snapshots_by_permissions(queryset: QuerySet, *, direct: bool = False, allowed_permissions: set[str] | None = None) -> QuerySet:
    allowed_permissions = allowed_permissions or ({PERMISSIONS_PUBLIC, PERMISSIONS_UNLISTED} if direct else {PERMISSIONS_PUBLIC})
    allowed_ranks = {PERMISSIONS_RANK[permission] for permission in allowed_permissions}
    # The join to crawls is the same parent relation every row already has.
    return queryset.annotate(_effective_permission_rank=effective_permission_rank_expr()).filter(
        _effective_permission_rank__in=allowed_ranks
    )


def public_snapshots_queryset(queryset: QuerySet) -> QuerySet:
    return filter_snapshots_by_permissions(queryset, direct=False)


def direct_snapshots_queryset(request: HttpRequest, queryset: QuerySet) -> QuerySet:
    return queryset if is_admin_user(request) else filter_snapshots_by_permissions(queryset, direct=True)
