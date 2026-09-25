"""
Real-time push over Socket.IO (python-socketio, mounted on the ASGI app in
``config/asgi.py``; ``daphne`` makes ``manage.py runserver`` serve it).

Events carry ids, never content. A client that hears "project X changed" or
"conversation Y has a new message" re-reads through the ordinary REST endpoint,
which applies the permission checks. So a socket room is only ever trusted with
the fact that something changed, and REST stays the single source of truth.

Rooms
-----
``user:<id>``               every socket of one user
``tenant:<client>:pms``     users of one tenant who hold ``view_pms``
``pms:project:<id>``        sockets currently showing that project (``pms:watch``)

Server -> client
----------------
``notification:new``     ``{}``                                       user room
``pms:project_changed``  ``{projectId, action, actorId}``             tenant PMS room
``chat:activity``        ``{projectId, conversationId, messageId, change}``
                         project room, or both user rooms for a direct chat
``chat:read``            ``{projectId, conversationId}``              own user room
``presence:changed``     ``{userId, online}``                         tenant PMS room

Client -> server (acknowledged)
-------------------------------
``pms:watch`` / ``pms:unwatch``  ``{projectId}`` -> ``{ok}``
``presence:query``               ``{userIds}``  -> ``{online: [...]}``

Scaling out: presence is kept in this process. Behind several workers, set
``SOCKETIO_MESSAGE_QUEUE`` (a Redis URL, and install ``redis``) so emits reach
sockets held by other workers; presence then reflects one worker only.
"""
import logging

import socketio
from asgiref.sync import async_to_sync, sync_to_async
from django.conf import settings
from django.db import close_old_connections, transaction

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------
def _origin_allowed(origin, environ=None):
    """Configured CORS origins, plus the page's own origin (a same-host deploy,
    or the Vite dev proxy, which forwards the browser's Origin unchanged)."""
    if not origin:
        return True  # non-browser clients send no Origin
    if origin in getattr(settings, "CORS_ALLOWED_ORIGINS", []):
        return True
    environ = environ or {}
    host = environ.get("HTTP_X_FORWARDED_HOST") or environ.get("HTTP_HOST")
    return bool(host) and origin.split("://", 1)[-1] == host.split(",")[0].strip()


def _client_manager():
    url = getattr(settings, "SOCKETIO_MESSAGE_QUEUE", "")
    return socketio.AsyncRedisManager(url) if url else None


sio = socketio.AsyncServer(
    async_mode="asgi",
    cors_allowed_origins=_origin_allowed,
    client_manager=_client_manager(),
    logger=False,
    engineio_logger=False,
)


def user_room(user_id):
    return f"user:{user_id}"


def tenant_pms_room(client_id):
    return f"tenant:{client_id}:pms"


def project_room(project_id):
    return f"pms:project:{project_id}"


# ---------------------------------------------------------------------------
# Emitting from Django code
# ---------------------------------------------------------------------------
def emit(event, data, *, room):
    """Push ``event`` to ``room`` once the current transaction commits.

    After commit, so nobody is told to re-read a row they cannot see yet.
    Failures are logged, never raised: a push is a hint, and a write must not
    fail because a socket could not be told about it.
    """

    def send():
        try:
            async_to_sync(sio.emit)(event, data, room=room)
        except Exception:  # pragma: no cover - transport trouble
            logger.warning("realtime emit %s to %s failed", event, room, exc_info=True)

    transaction.on_commit(send)


def announce_project_change(project, action, actor=None):
    emit(
        "pms:project_changed",
        {
            "projectId": str(project.id),
            "action": action,
            "actorId": str(actor.id) if getattr(actor, "is_authenticated", False) else None,
        },
        room=tenant_pms_room(project.client_id),
    )


# ---------------------------------------------------------------------------
# Connection handling
# ---------------------------------------------------------------------------
#: (client id, user id) -> socket ids. One process's view (see module docstring).
_online = {}


async def _db(fn, *args):
    """Run ORM code for a socket handler, with the connection hygiene Django's
    request cycle would otherwise provide."""

    def run():
        close_old_connections()
        try:
            return fn(*args)
        finally:
            close_old_connections()

    return await sync_to_async(run, thread_sensitive=True)()


def authenticate_token(token):
    """The socket's identity, from the same JWT the REST API accepts.

    Returns ``None`` for a missing, expired or foreign token -- the handshake is
    then refused and the client falls back to polling.
    """
    from apps.accounts.authentication import CLIENT_CLAIM, TenantJWTAuthentication
    from apps.core.permissions import has_permission

    if not token:
        return None
    auth = TenantJWTAuthentication()
    try:
        validated = auth.get_validated_token(token)
        user = auth.get_user(validated)
    except Exception:
        return None
    if str(validated.get(CLIENT_CLAIM) or user.client_id) != str(user.client_id):
        return None
    if not user.is_active or user.status != "Active":
        return None
    return {
        "user_id": str(user.id),
        "client_id": str(user.client_id),
        "can_view_pms": has_permission(user, "view_pms"),
    }


def _project_in_tenant(project_id, client_id):
    import uuid

    from apps.core.tenancy import tenant_context
    from apps.pms.models import Project

    try:
        uuid.UUID(str(project_id))
    except (TypeError, ValueError):
        return False
    with tenant_context(client_id):
        return Project.objects.filter(
            pk=project_id, client_id=client_id, deleted_at__isnull=True
        ).exists()


@sio.event
async def connect(sid, environ, auth):
    token = (auth or {}).get("token") if isinstance(auth, dict) else None
    identity = await _db(authenticate_token, token)
    if identity is None:
        raise socketio.exceptions.ConnectionRefusedError("unauthorized")

    await sio.save_session(sid, identity)
    await sio.enter_room(sid, user_room(identity["user_id"]))
    if identity["can_view_pms"]:
        await sio.enter_room(sid, tenant_pms_room(identity["client_id"]))

    key = (identity["client_id"], identity["user_id"])
    first = key not in _online
    _online.setdefault(key, set()).add(sid)
    if first:
        await sio.emit(
            "presence:changed",
            {"userId": identity["user_id"], "online": True},
            room=tenant_pms_room(identity["client_id"]),
            skip_sid=sid,
        )


@sio.event
async def disconnect(sid, *args):
    try:
        identity = await sio.get_session(sid)
    except KeyError:
        return
    key = (identity["client_id"], identity["user_id"])
    sockets = _online.get(key)
    if sockets is None:
        return
    sockets.discard(sid)
    if not sockets:
        _online.pop(key, None)
        await sio.emit(
            "presence:changed",
            {"userId": identity["user_id"], "online": False},
            room=tenant_pms_room(identity["client_id"]),
        )


@sio.on("pms:watch")
async def pms_watch(sid, data):
    identity = await sio.get_session(sid)
    project_id = str((data or {}).get("projectId") or "")
    if not identity.get("can_view_pms"):
        return {"ok": False}
    if not await _db(_project_in_tenant, project_id, identity["client_id"]):
        return {"ok": False}
    await sio.enter_room(sid, project_room(project_id))
    return {"ok": True}


@sio.on("pms:unwatch")
async def pms_unwatch(sid, data):
    project_id = str((data or {}).get("projectId") or "")
    await sio.leave_room(sid, project_room(project_id))
    return {"ok": True}


@sio.on("presence:query")
async def presence_query(sid, data):
    identity = await sio.get_session(sid)
    wanted = {str(user_id) for user_id in (data or {}).get("userIds") or []}
    online = [
        user_id
        for (client_id, user_id) in list(_online)
        if client_id == identity["client_id"] and user_id in wanted
    ]
    return {"online": online}
