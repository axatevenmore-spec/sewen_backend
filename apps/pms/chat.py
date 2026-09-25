"""
Team-wise messenger inside a PMS project (PMS -> Project -> Messenger).

Three kinds of conversation hang off a project: one ``Project`` chat, one
``Team`` chat per PMS department on the project's stages, and ``Direct`` chats
between two project members.

Nothing here keeps a second roster. A team is the department a stage belongs
to, and its members are whoever the stage and its tasks are assigned to right
now, plus the project manager -- so re-staffing a stage changes the team chat
with no sync step. The rest reuses what PMS already has: ``core.File`` for
attachments (the store the design proofs use), ``notify`` for mentions and new
messages, ``record_audit`` for the project activity trail.

Changes are pushed over Socket.IO as ``chat:activity`` (ids only, see
``apps.core.realtime``); the client then reads ``messages/?since=<cursor>``, the
same call it polls with when the socket is down. The cursor lags "now" by a few
seconds so a message whose transaction commits late is still picked up; clients
de-duplicate by id.
"""
import uuid
from datetime import timedelta

from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from apps.core.audit import notify, record_audit
from apps.core.exceptions import NotFound, PermissionDenied, ValidationFailed
from apps.core.permissions import has_permission
from apps.core.realtime import emit, project_room, user_room
from apps.core.serializers import ISODateTimeField

from . import services
from .models import Conversation, ConversationMember, Message, MessageAttachment, Task

#: Anyone holding this can already staff any team, so seeing every team's chat
#: grants nothing new. It is the "admin" gate the PMS catalogue already has.
OVERSIGHT_PERMISSION = "assign_stage"
PAGE_SIZE = 50
MAX_PAGE_SIZE = 100
SEARCH_LIMIT = 50
NOTIFY_SNIPPET_CHARS = 140
#: How far behind "now" the polling cursor sits (see module docstring).
CURSOR_LAG = timedelta(seconds=5)
#: The in-app notification ``entity_type`` for chat notifications.
ENTITY_TYPE = "PmsConversation"
KIND_ORDER = {"Project": 0, "Team": 1, "Direct": 2}
iso_time = ISODateTimeField().to_representation


def _is_live_user(user):
    return (
        user is not None
        and user.deleted_at is None
        and user.is_active
        and user.status not in ("Deleted", "Inactive")
    )


def _parse_uuid(value, field):
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        raise ValidationFailed("Invalid id.", field_errors={field: ["Invalid id."]})


def _person(user, role, teams=()):
    return {
        "id": str(user.id),
        "name": user.name,
        "avatar": user.avatar_url,
        "role": role,
        "teams": sorted(teams),
    }


def team_label(department):
    return f"{department.name} Team"


class ChatScope:
    """What one user may see in one project's messenger, read once per request.

    - **Oversight** (superuser, the project's PM, or ``assign_stage``): Project
      chat and every team chat.
    - **Team member** (assigned a stage, or a task on a stage, of that team's
      department): Project chat and that team's chat.
    - **Direct** chats: only the two people in them.
    """

    def __init__(self, project, user):
        self.project = project
        self.user = user
        #: department id -> Department, in stage-sequence order.
        self.departments = {}
        #: department id -> {user id: User}
        self.rosters = {}

        stages = (
            project.stages.filter(deleted_at__isnull=True, department__isnull=False)
            .select_related("department", "assigned_user")
            .order_by("sequence")
        )
        stage_department = {}
        for stage in stages:
            if stage.department.deleted_at is not None:
                continue
            stage_department[stage.id] = stage.department_id
            self.departments.setdefault(stage.department_id, stage.department)
            roster = self.rosters.setdefault(stage.department_id, {})
            if _is_live_user(stage.assigned_user):
                roster.setdefault(stage.assigned_user_id, stage.assigned_user)

        tasks = Task.objects.filter(
            stage_id__in=list(stage_department),
            deleted_at__isnull=True,
            assigned_user__isnull=False,
        ).select_related("assigned_user")
        for task in tasks:
            if _is_live_user(task.assigned_user):
                self.rosters[stage_department[task.stage_id]].setdefault(
                    task.assigned_user_id, task.assigned_user
                )

        manager = project.project_manager if project.project_manager_id else None
        self.manager = manager if _is_live_user(manager) else None
        self.is_oversight = bool(
            getattr(user, "is_superuser", False)
            or project.project_manager_id == user.id
            or has_permission(user, OVERSIGHT_PERMISSION)
        )
        self.my_teams = {dept for dept, roster in self.rosters.items() if user.id in roster}
        self.is_participant = self.is_oversight or bool(self.my_teams)

    # -- who is in what -----------------------------------------------------
    def project_members(self):
        """Everyone on the project: the PM and every team's roster."""
        teams_of = {}
        people = {}
        for dept_id, roster in self.rosters.items():
            for user_id, person in roster.items():
                people[user_id] = person
                teams_of.setdefault(user_id, set()).add(self.departments[dept_id].name)
        rows = []
        if self.manager is not None:
            rows.append(_person(self.manager, "Project Manager", teams_of.get(self.manager.id, ())))
        for user_id, person in sorted(people.items(), key=lambda item: item[1].name.lower()):
            if self.manager is not None and user_id == self.manager.id:
                continue
            rows.append(_person(person, "Member", teams_of.get(user_id, ())))
        return rows

    def members_of(self, conversation, direct_users=None):
        if conversation.kind == "Project":
            return self.project_members()
        if conversation.kind == "Team":
            roster = self.rosters.get(conversation.department_id, {})
            rows = []
            if self.manager is not None:
                rows.append(_person(self.manager, "Project Manager"))
            for user_id, person in sorted(roster.items(), key=lambda item: item[1].name.lower()):
                if self.manager is not None and user_id == self.manager.id:
                    continue
                rows.append(_person(person, "Member"))
            return rows
        users = direct_users if direct_users is not None else _direct_users([conversation])
        return [
            _person(users[user_id], "Member")
            for user_id in _direct_ids(conversation)
            if user_id in users
        ]

    def can_access(self, conversation):
        if conversation.kind == "Project":
            return self.is_participant
        if conversation.kind == "Team":
            return self.is_oversight or conversation.department_id in self.my_teams
        return str(self.user.id) in _direct_ids(conversation, as_str=True)


def _direct_ids(conversation, as_str=False):
    parts = [part for part in (conversation.direct_key or "").split(":") if part]
    return parts if as_str else [uuid.UUID(part) for part in parts]


def _direct_users(conversations):
    from apps.accounts.models import User

    ids = {user_id for conv in conversations if conv.kind == "Direct" for user_id in _direct_ids(conv)}
    if not ids:
        return {}
    return {user.id: user for user in User.objects.filter(pk__in=ids)}


# ---------------------------------------------------------------------------
# Real-time announcements
# ---------------------------------------------------------------------------
def announce(conversation, change, message_id=None):
    """``chat:activity`` to whoever should re-read this conversation.

    Project and Team chats go to the project's room (sockets showing the
    project); a client that cannot see the conversation ignores the id. Direct
    chats go only to their two members.
    """
    payload = {
        "projectId": str(conversation.project_id),
        "conversationId": str(conversation.id),
        "messageId": str(message_id) if message_id else None,
        "change": change,
    }
    if conversation.kind == "Direct":
        for user_id in _direct_ids(conversation, as_str=True):
            emit("chat:activity", payload, room=user_room(user_id))
    else:
        emit("chat:activity", payload, room=project_room(conversation.project_id))


# ---------------------------------------------------------------------------
# Conversations
# ---------------------------------------------------------------------------
def _get_or_create(project, kind, **lookup):
    """Race-safe: the partial unique constraints make the loser re-read."""
    existing = Conversation.objects.filter(
        project=project, kind=kind, deleted_at__isnull=True, **lookup
    )
    row = existing.first()
    if row is not None:
        return row, False
    try:
        with transaction.atomic():
            return (
                Conversation.objects.create(
                    client_id=project.client_id, project=project, kind=kind, **lookup
                ),
                True,
            )
    except IntegrityError:
        return existing.first(), False


def ensure_conversations(project):
    """The Project chat, plus one Team chat per department on a live stage.

    Idempotent -- called when stages are added or assigned, and again on every
    messenger read, so a team that appears by any route gets its chat.
    """
    _get_or_create(project, "Project")
    department_ids = set(
        project.stages.filter(
            deleted_at__isnull=True,
            department__isnull=False,
            department__deleted_at__isnull=True,
        )
        .order_by()
        .values_list("department_id", flat=True)
    )
    have = set(
        Conversation.objects.filter(
            project=project, kind="Team", deleted_at__isnull=True
        ).values_list("department_id", flat=True)
    )
    for department_id in department_ids - have:
        _get_or_create(project, "Team", department_id=department_id)


def visible_conversations(scope):
    rows = Conversation.objects.filter(
        project=scope.project, deleted_at__isnull=True
    ).select_related("department")
    visible = []
    for conversation in rows:
        if not scope.can_access(conversation):
            continue
        # A team taken off every stage keeps its chat only while it has history.
        if (
            conversation.kind == "Team"
            and conversation.department_id not in scope.departments
            and conversation.last_message_at is None
        ):
            continue
        visible.append(conversation)
    return visible


def get_conversation(scope, conversation_id):
    """404 rather than 403 for a chat the caller cannot see, so a guessed id
    does not confirm the chat exists."""
    conversation = (
        Conversation.objects.filter(
            project=scope.project,
            pk=_parse_uuid(conversation_id, "conversationId"),
            deleted_at__isnull=True,
        )
        .select_related("department")
        .first()
    )
    if conversation is None or not scope.can_access(conversation):
        raise NotFound("That conversation no longer exists.")
    return conversation


def conversation_title(conversation, viewer_id, direct_users):
    if conversation.kind == "Project":
        return "Project Chat"
    if conversation.kind == "Team":
        return team_label(conversation.department)
    others = [
        direct_users[user_id].name
        for user_id in _direct_ids(conversation)
        if user_id != viewer_id and user_id in direct_users
    ]
    return others[0] if others else "Direct message"


def _unread_count(conversation, user, last_read_at):
    queryset = conversation.messages.filter(deleted_at__isnull=True).exclude(sender_id=user.id)
    if last_read_at is not None:
        queryset = queryset.filter(created_at__gt=last_read_at)
    return queryset.count()


def _last_message(conversation):
    message = (
        conversation.messages.filter(deleted_at__isnull=True)
        .prefetch_related("attachments")
        .order_by("-created_at")
        .first()
    )
    if message is None:
        return None
    names = [a.file_name for a in message.attachments.all() if a.deleted_at is None]
    text = message.text or ", ".join(names)
    return {
        "id": str(message.id),
        "text": text[:NOTIFY_SNIPPET_CHARS],
        "senderId": str(message.sender_id) if message.sender_id else None,
        "senderName": message.sender_name,
        "createdAt": iso_time(message.created_at),
        "hasAttachments": bool(names),
    }


def list_conversations(scope):
    """``(rows, aggregates)`` for ``GET /pms/projects/{id}/conversations/``."""
    ensure_conversations(scope.project)
    conversations = visible_conversations(scope)
    direct_users = _direct_users(conversations)
    read_at = dict(
        ConversationMember.objects.filter(
            conversation__in=conversations, user=scope.user
        ).values_list("conversation_id", "last_read_at")
    )
    department_order = {dept_id: index for index, dept_id in enumerate(scope.departments)}

    rows = []
    for conversation in conversations:
        members = scope.members_of(conversation, direct_users)
        department = conversation.department
        rows.append(
            {
                "id": str(conversation.id),
                "kind": conversation.kind,
                "title": conversation_title(conversation, scope.user.id, direct_users),
                "departmentId": str(conversation.department_id) if department else None,
                "departmentName": department.name if department else None,
                "color": department.color if department else None,
                "isActive": conversation.kind != "Team"
                or conversation.department_id in scope.departments,
                "isMyTeam": conversation.department_id in scope.my_teams,
                "members": members,
                "memberCount": len(members),
                "unreadCount": _unread_count(
                    conversation, scope.user, read_at.get(conversation.id)
                ),
                "lastMessage": _last_message(conversation),
                "lastMessageAt": iso_time(conversation.last_message_at),
                "_sort": (
                    KIND_ORDER[conversation.kind],
                    department_order.get(conversation.department_id, len(department_order)),
                    -(conversation.last_message_at.timestamp())
                    if conversation.last_message_at
                    else 0,
                ),
            }
        )
    rows.sort(key=lambda row: row.pop("_sort"))

    aggregates = {
        "totalUnread": sum(row["unreadCount"] for row in rows),
        "isParticipant": scope.is_participant,
        "canSeeAllTeams": scope.is_oversight,
        "myTeamIds": [str(dept_id) for dept_id in scope.my_teams],
        "projectMembers": scope.project_members(),
    }
    return rows, aggregates


@transaction.atomic
def start_direct(scope, other_user_id):
    """Get or create the direct chat between the caller and one project member."""
    if not scope.is_participant:
        raise PermissionDenied(
            "Only people working on this project can message its members.",
            code="PMS_CHAT_NOT_PARTICIPANT",
        )
    other_id = _parse_uuid(other_user_id, "userId")
    if other_id == scope.user.id:
        raise ValidationFailed(
            "You cannot start a conversation with yourself.",
            field_errors={"userId": ["Pick someone else."]},
        )
    member_ids = {uuid.UUID(row["id"]) for row in scope.project_members()}
    if other_id not in member_ids:
        raise ValidationFailed(
            "That person is not on this project.",
            field_errors={"userId": ["Not a project member."]},
        )

    key = ":".join(sorted([str(scope.user.id), str(other_id)]))
    conversation, created = _get_or_create(scope.project, "Direct", direct_key=key)
    if created:
        for user_id in (scope.user.id, other_id):
            ConversationMember.objects.get_or_create(
                conversation=conversation,
                user_id=user_id,
                defaults={"client_id": scope.project.client_id},
            )
        announce(conversation, "conversation")
    return conversation, created


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------
def message_queryset():
    return Message.objects.select_related(
        "sender", "stage", "reply_to"
    ).prefetch_related("attachments__file", "reply_to__attachments")


def list_messages(conversation, *, before=None, since=None, limit=None):
    """A page of messages, oldest first, soft-deleted rows included as
    tombstones.

    - default: the newest page; ``before=<messageId>`` pages back from there.
    - ``since=<cursor>``: everything created, edited or deleted after the
      cursor, for polling.
    """
    try:
        limit = min(max(int(limit or PAGE_SIZE), 1), MAX_PAGE_SIZE)
    except (TypeError, ValueError):
        limit = PAGE_SIZE
    queryset = message_queryset().filter(conversation=conversation)
    cursor = timezone.now() - CURSOR_LAG

    if since:
        from rest_framework.fields import DateTimeField

        try:
            since_at = DateTimeField().to_internal_value(since)
        except Exception:
            raise ValidationFailed(
                "since must be an ISO timestamp.",
                field_errors={"since": ["Expected an ISO-8601 timestamp."]},
            )
        rows = list(queryset.filter(updated_at__gt=since_at).order_by("created_at")[:MAX_PAGE_SIZE])
        return rows, {"hasMore": False, "cursor": iso_time(cursor)}

    if before:
        pivot = conversation.messages.filter(pk=_parse_uuid(before, "before")).first()
        if pivot is None:
            raise NotFound("That message no longer exists.")
        queryset = queryset.filter(created_at__lt=pivot.created_at)

    rows = list(queryset.order_by("-created_at")[: limit + 1])
    has_more = len(rows) > limit
    return list(reversed(rows[:limit])), {"hasMore": has_more, "cursor": iso_time(cursor)}


def _clean_mentions(scope, conversation, text, mentions):
    """Keep a mention only if it names someone in this conversation's
    audience and its ``@label`` is still in the text.

    Team mentions (``@QC Team``) are for the Project chat only: a team chat's
    audience is one team, so tagging another team there would notify people
    who cannot open it.
    """
    lowered = (text or "").lower()
    audience = {row["id"]: row for row in scope.members_of(conversation)}
    cleaned, seen = [], set()
    for mention in mentions or []:
        kind, raw_id = mention.get("type"), str(mention.get("id") or "")
        if (kind, raw_id) in seen:
            continue
        if kind == "user" and raw_id in audience:
            label = audience[raw_id]["name"]
        elif kind == "team" and conversation.kind == "Project":
            department = next(
                (d for d_id, d in scope.departments.items() if str(d_id) == raw_id), None
            )
            if department is None:
                continue
            label = team_label(department)
        else:
            continue
        if f"@{label}".lower() not in lowered:
            continue
        seen.add((kind, raw_id))
        cleaned.append({"type": kind, "id": raw_id, "name": label})
    return cleaned


def _mention_recipients(scope, mentions):
    recipients = set()
    for mention in mentions:
        if mention["type"] == "user":
            recipients.add(uuid.UUID(mention["id"]))
        else:
            for dept_id in scope.departments:
                if str(dept_id) == mention["id"]:
                    recipients |= set(scope.rosters.get(dept_id, {}))
    return recipients


def _attachments_for(scope, attachment_ids):
    from apps.core.models import File

    if not attachment_ids:
        return []
    ids = [_parse_uuid(value, "attachmentIds") for value in attachment_ids]
    files = {
        row.id: row
        for row in File.objects.filter(
            client_id=scope.project.client_id,
            pk__in=ids,
            status="committed",
            uploaded_by=scope.user,
        )
    }
    missing = [str(file_id) for file_id in ids if file_id not in files]
    if missing:
        raise ValidationFailed(
            "One of the attachments is not available.",
            field_errors={"attachmentIds": [f"Unknown or unfinished upload: {missing[0]}."]},
        )
    # Keep the caller's order, drop repeats.
    return [files[file_id] for file_id in dict.fromkeys(ids)]


@transaction.atomic
def post_message(scope, conversation, data):
    user = scope.user
    project = scope.project
    text = (data.get("text") or "").strip()

    reply_to = None
    if data.get("replyToId"):
        reply_to = conversation.messages.filter(
            pk=_parse_uuid(data["replyToId"], "replyToId"), deleted_at__isnull=True
        ).first()
        if reply_to is None:
            raise ValidationFailed(
                "The message you are replying to no longer exists.",
                field_errors={"replyToId": ["Not in this conversation."]},
            )

    stage = None
    if data.get("stageId"):
        stage = project.stages.filter(
            pk=_parse_uuid(data["stageId"], "stageId"), deleted_at__isnull=True
        ).first()
        if stage is None:
            raise ValidationFailed(
                "That stage is not part of this project.",
                field_errors={"stageId": ["Unknown stage."]},
            )

    files = _attachments_for(scope, data.get("attachmentIds"))
    mentions = _clean_mentions(scope, conversation, text, data.get("mentions"))

    message = Message.objects.create(
        client_id=project.client_id,
        conversation=conversation,
        project=project,
        stage=stage,
        sender=user,
        sender_name=user.name or user.email,
        text=text,
        reply_to=reply_to,
        mentions=mentions,
        created_by=user,
    )
    MessageAttachment.objects.bulk_create(
        [
            MessageAttachment(
                client_id=project.client_id,
                message=message,
                file=file_row,
                file_name=file_row.file_name,
                file_size=file_row.file_size,
                content_type=file_row.content_type,
                created_by=user,
            )
            for file_row in files
        ]
    )
    Conversation.objects.filter(pk=conversation.pk).update(
        last_message_at=message.created_at, updated_at=timezone.now()
    )
    conversation.last_message_at = message.created_at
    # The sender has read their own conversation up to this point.
    _touch_read(conversation, user, message.created_at)

    title = conversation_title(conversation, user.id, _direct_users([conversation]))
    if files:
        record_audit(
            client=project.client_id,
            actor=user,
            action="CHAT_FILE_SHARED",
            entity_type="PmsProject",
            entity_id=project.id,
            entity_label=project.code,
            description=f"Shared {', '.join(f.file_name for f in files)} in "
            f"{title if conversation.kind != 'Direct' else 'a direct message'}",
        )
    _notify_new_message(scope, conversation, message, mentions)
    announce(conversation, "created", message.id)
    return message_queryset().get(pk=message.pk)


def _own_message(conversation, user, message_id):
    message = conversation.messages.filter(
        pk=_parse_uuid(message_id, "messageId"), deleted_at__isnull=True
    ).first()
    if message is None:
        raise NotFound("That message no longer exists.")
    if message.sender_id != user.id:
        raise PermissionDenied(
            "You can only change your own messages.", code="PMS_CHAT_NOT_SENDER"
        )
    return message


@transaction.atomic
def edit_message(scope, conversation, message_id, data):
    message = _own_message(conversation, scope.user, message_id)
    text = (data.get("text") or "").strip()
    if not text and not message.attachments.filter(deleted_at__isnull=True).exists():
        raise ValidationFailed(
            "A message cannot be empty. Delete it instead.",
            field_errors={"text": ["Required."]},
        )
    message.text = text
    message.mentions = _clean_mentions(
        scope, conversation, text, data.get("mentions", message.mentions)
    )
    message.edited_at = timezone.now()
    message.updated_by = scope.user
    message.save(update_fields=["text", "mentions", "edited_at", "updated_by", "updated_at"])
    announce(conversation, "updated", message.id)
    return message_queryset().get(pk=message.pk)


@transaction.atomic
def delete_message(scope, conversation, message_id):
    message = _own_message(conversation, scope.user, message_id)
    message.soft_delete(scope.user)
    project = scope.project
    title = conversation_title(conversation, scope.user.id, _direct_users([conversation]))
    record_audit(
        client=project.client_id,
        actor=scope.user,
        action="CHAT_MESSAGE_DELETED",
        entity_type="PmsProject",
        entity_id=project.id,
        entity_label=project.code,
        description=f"Deleted a message in "
        f"{title if conversation.kind != 'Direct' else 'a direct message'}",
    )
    announce(conversation, "deleted", message.id)
    return message_queryset().get(pk=message.pk)


# ---------------------------------------------------------------------------
# Read state and notifications
# ---------------------------------------------------------------------------
def _touch_read(conversation, user, at):
    member, created = ConversationMember.objects.get_or_create(
        conversation=conversation,
        user=user,
        defaults={"client_id": conversation.client_id, "last_read_at": at},
    )
    if not created and (member.last_read_at is None or member.last_read_at < at):
        member.last_read_at = at
        member.save(update_fields=["last_read_at", "updated_at"])


def mark_read(scope, conversation):
    """Opening a conversation reads it, and clears the bell for it too."""
    from apps.core.models import Notification

    now = timezone.now()
    _touch_read(conversation, scope.user, now)
    Notification.objects.filter(
        client_id=scope.project.client_id,
        recipient=scope.user,
        entity_type=ENTITY_TYPE,
        entity_id=conversation.id,
        read_at__isnull=True,
    ).update(read_at=now)
    # The reader's other tabs clear their badges too.
    emit(
        "chat:read",
        {"projectId": str(conversation.project_id), "conversationId": str(conversation.id)},
        room=user_room(scope.user.id),
    )
    return now


def _notify_new_message(scope, conversation, message, mentions):
    """One notification per mention; for everyone else in the audience, at most
    one unread "new messages" notification per conversation, so a busy chat
    does not bury the bell."""
    from apps.core.models import Notification

    settings = services.get_settings(scope.project.client_id)
    if not (settings.notifications or {}).get("enabled", True):
        return

    sender = scope.user
    project = scope.project
    title = conversation_title(conversation, None, _direct_users([conversation]))
    snippet = (message.text or ", ".join(a.file_name for a in message.attachments.all()))[
        :NOTIFY_SNIPPET_CHARS
    ]
    payload = {
        "projectId": str(project.id),
        "projectCode": project.code,
        "conversationId": str(conversation.id),
        "messageId": str(message.id),
        "path": f"/pms/projects/{project.id}?tab=messenger&conversation={conversation.id}",
    }
    where = "a direct message" if conversation.kind == "Direct" else title

    mentioned = _mention_recipients(scope, mentions) - {sender.id}
    if mentioned:
        notify(
            client=project.client_id,
            recipients=list(mentioned),
            type="pms.chat_mention",
            category="pms",
            title=f"{message.sender_name} mentioned you in {where}",
            body=f"{project.code}: {snippet}",
            entity_type=ENTITY_TYPE,
            entity_id=conversation.id,
            actor=sender,
            payload=payload,
        )

    audience = {uuid.UUID(row["id"]) for row in scope.members_of(conversation)}
    others = audience - mentioned - {sender.id}
    if not others:
        return
    already = set(
        Notification.objects.filter(
            client_id=project.client_id,
            recipient_id__in=others,
            type="pms.chat_message",
            entity_id=conversation.id,
            read_at__isnull=True,
        ).values_list("recipient_id", flat=True)
    )
    recipients = others - already
    if not recipients:
        return
    notify(
        client=project.client_id,
        recipients=list(recipients),
        type="pms.chat_message",
        category="pms",
        title=(
            f"New message from {message.sender_name}"
            if conversation.kind == "Direct"
            else f"New messages in {title}"
        ),
        body=f"{project.code} · {message.sender_name}: {snippet}",
        entity_type=ENTITY_TYPE,
        entity_id=conversation.id,
        actor=sender,
        payload=payload,
    )


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------
def search_messages(scope, query, conversation_id=None):
    """Message text, sender name and attachment file name, across every
    conversation the caller can see in this project (or one of them)."""
    query = (query or "").strip()
    if len(query) < 2:
        return []
    conversations = visible_conversations(scope)
    if conversation_id:
        wanted = _parse_uuid(conversation_id, "conversationId")
        conversations = [c for c in conversations if c.id == wanted]
    ids = [c.id for c in conversations]
    if not ids:
        return []
    matches = (
        Message.objects.filter(conversation_id__in=ids, deleted_at__isnull=True)
        .filter(
            Q(text__icontains=query)
            | Q(sender_name__icontains=query)
            | Q(attachments__file_name__icontains=query, attachments__deleted_at__isnull=True)
        )
        .order_by("-created_at")
        .values_list("id", flat=True)
        .distinct()[:SEARCH_LIMIT]
    )
    match_ids = list(matches)
    return list(message_queryset().filter(pk__in=match_ids).order_by("-created_at"))
