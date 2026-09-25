"""
RBAC (api.md Appendix B, db.md §2.2).

Effective permissions = role grants union user grants minus user denies. They
are computed at login, cached on the session and returned by ``/auth/me/``.

api-integration.md §6 is explicit that client-side checks are UX only:
"A hidden button must still 403 if called." These classes are that 403.
api.md §1.5 also requires the permission id in the error ``code``, so the UI
can disable the action for the rest of the session without string-matching.
"""
from rest_framework.permissions import BasePermission

from .exceptions import NotAuthenticated, PermissionDenied


class IsAuthenticatedInTenant(BasePermission):
    """Authenticated, active, and bound to a tenant.

    Raises rather than returning False so the body carries a code; DRF's own
    403-for-unauthenticated behaviour would break the client, which clears its
    token on 401 only (api.md §1.2).
    """

    def has_permission(self, request, view):
        user = getattr(request, "user", None)
        if user is None or not getattr(user, "is_authenticated", False):
            raise NotAuthenticated()
        if getattr(request, "client_id", None) is None:
            raise NotAuthenticated("Your account is not attached to a tenant.")
        if getattr(user, "status", "Active") not in ("Active",):
            raise PermissionDenied(
                "This account is not active.", code="ACCOUNT_INACTIVE"
            )
        return True


class AllowPublic(BasePermission):
    """Unauthenticated endpoints: public quotation, proof approval, careers, forms."""

    def has_permission(self, request, view):
        return True


class HasModulePermission(IsAuthenticatedInTenant):
    """Checks the permission ids a view declares.

    A view declares either a flat requirement::

        required_permissions = ["view_sales"]

    or one per action, which is what most document viewsets need::

        permission_map = {
            "list": ["view_sales"],
            "create": ["create_invoice"],
            "finalize": ["finalize_invoice"],
            "cancel": ["cancel_invoice"],
        }

    An entry that is a tuple means "any one of these" -- for shared records
    such as parties, which Sales and Purchase both maintain::

        permission_map = {"write": [("create_invoice", "create_bill")]}

    A missing entry means the action is open to any authenticated tenant user
    who holds a role; that is deliberate, because api.md Appendix B has no ids
    for several read-only screens and inventing them would break the seeded
    roles. A user with neither a role nor a per-user grant gets nothing here.
    """

    def has_permission(self, request, view):
        super().has_permission(request, view)

        user = request.user
        if getattr(user, "is_superuser", False):
            return True

        granted = granted_permissions(user)
        if not user.role_id and not granted:
            raise PermissionDenied(
                "Your account has no role. Ask an administrator to assign one.",
                code="NO_ROLE",
            )

        required = self._required_for(view, getattr(view, "action", None), request.method)
        missing = missing_permissions(granted, required)
        if missing:
            raise PermissionDenied(
                "You don't have permission to do that.",
                # api.md §1.5 -- the permission id goes in `code`; for an
                # any-of entry that is its first option.
                code=missing[0][0],
                detail=f"Requires: {', '.join(' or '.join(m) for m in missing)}.",
            )
        return True

    @staticmethod
    def _required_for(view, action, method):
        permission_map = getattr(view, "permission_map", None) or {}
        if action and action in permission_map:
            return permission_map[action]
        if method in permission_map:
            return permission_map[method]
        # `write` is a convenience bucket for "any non-safe method".
        if method not in ("GET", "HEAD", "OPTIONS") and "write" in permission_map:
            return permission_map["write"]
        if method in ("GET", "HEAD", "OPTIONS") and "read" in permission_map:
            return permission_map["read"]
        return getattr(view, "required_permissions", None) or []


def granted_permissions(user):
    """The caller's effective permission ids, read once per request.

    Authentication stamps ``permission_ids`` from the database on every
    request; the fallback covers users that did not come through it (tests,
    management commands).
    """
    granted = getattr(user, "permission_ids", None)
    if granted is None:
        granted = user.effective_permissions()
        user.permission_ids = granted
    return granted


def missing_permissions(granted, required):
    """Required entries the grant does not satisfy, each as a tuple of options.

    A tuple entry is satisfied by any one of its ids.
    """
    missing = []
    for entry in required or []:
        options = tuple(entry) if isinstance(entry, (tuple, list)) else (entry,)
        if not any(option in granted for option in options):
            missing.append(options)
    return missing


def require_permission(user, permission_id, message=None):
    """Imperative gate for code paths that are not view-level.

    Used by override flows -- ``overrideCreditLimit`` (api.md §4.1) and PMS
    ``force`` completion (api.md §10.3) -- where the requirement depends on the
    request body, not the route. ``permission_id`` may be a tuple (any of).
    """
    if not has_permission(user, permission_id):
        options = permission_id if isinstance(permission_id, (tuple, list)) else (permission_id,)
        raise PermissionDenied(
            message or "You don't have permission to do that.", code=options[0]
        )
    return True


def has_permission(user, permission_id):
    if getattr(user, "is_superuser", False):
        return True
    return not missing_permissions(granted_permissions(user), [permission_id])
