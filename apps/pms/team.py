"""
A project's team -- everyone working on it, for its Team tab and messenger.

Most of the team is derived: the project manager, whoever a stage is assigned
to, whoever a task is assigned to. Everyone else the PM adds to the team list
(``ProjectMember``), optionally into one of the project's teams (a stage
department). ``chat.ChatScope`` reads the same sources, so being on this list
is exactly what puts someone in the project's chats.

    GET    /pms/projects/{id}/team/              the team (+ who can be added)
    POST   /pms/projects/{id}/team/              { userId, departmentId? }
    DELETE /pms/projects/{id}/team/{memberId}/   take an added member off
    GET    /pms/my-chats/                        projects I can chat on
"""
from django.db import IntegrityError, transaction
from django.db.models import Q

from apps.core.audit import notify, record_audit
from apps.core.exceptions import NotFound, PermissionDenied, ValidationFailed
from apps.core.permissions import has_permission

from . import chat
from .models import Project, ProjectMember, ProjectStage, Task

#: Who may staff a project besides its own PM (the catalogue's ids).
MANAGE_PERMISSIONS = ("assign_members", "assign_stage")


def can_manage(user, project):
    return bool(
        getattr(user, "is_superuser", False)
        or project.project_manager_id == user.id
        or has_permission(user, MANAGE_PERMISSIONS)
    )


def can_view(user, project, scope=None):
    scope = scope or chat.ChatScope(project, user)
    return scope.is_participant or has_permission(user, "view_pms")


def _live(user):
    return chat._is_live_user(user) and not getattr(user, "is_customer", False)


def team_payload(project, user):
    """Everyone on the project, why they are on it, and (for a manager) who
    else could be added."""
    stages = list(
        project.stages.filter(deleted_at__isnull=True)
        .select_related("department", "assigned_user")
        .order_by("sequence")
    )
    teams = {}
    for stage in stages:
        if stage.department_id and stage.department.deleted_at is None:
            teams.setdefault(stage.department_id, stage.department)

    people = {}

    def entry(person):
        return people.setdefault(
            person.id,
            {
                "userId": str(person.id),
                "name": person.name,
                "email": person.email,
                "avatar": person.avatar_url,
                "roles": [],
                "teams": set(),
                "memberId": None,
                "departmentId": None,
            },
        )

    if project.project_manager_id and chat._is_live_user(project.project_manager):
        entry(project.project_manager)["roles"].append("Project Manager")
    for stage in stages:
        if chat._is_live_user(stage.assigned_user):
            row = entry(stage.assigned_user)
            row["roles"].append(f"Stage: {stage.name}")
            if stage.department_id in teams:
                row["teams"].add(teams[stage.department_id].name)
    tasks = Task.objects.filter(
        stage__in=stages, deleted_at__isnull=True, assigned_user__isnull=False
    ).select_related("assigned_user", "stage")
    for task in tasks:
        if chat._is_live_user(task.assigned_user):
            row = entry(task.assigned_user)
            if "Task assignee" not in row["roles"]:
                row["roles"].append("Task assignee")
            if task.stage.department_id in teams:
                row["teams"].add(teams[task.stage.department_id].name)
    members = ProjectMember.objects.filter(
        project=project, deleted_at__isnull=True
    ).select_related("user", "department")
    for member in members:
        if not chat._is_live_user(member.user):
            continue
        row = entry(member.user)
        row["roles"].append("Team member")
        row["memberId"] = str(member.id)
        if member.department_id in teams:
            row["departmentId"] = str(member.department_id)
            row["teams"].add(teams[member.department_id].name)

    rows = sorted(
        people.values(),
        key=lambda r: ("Project Manager" not in r["roles"], r["name"].lower()),
    )
    for row in rows:
        row["teams"] = sorted(row["teams"])

    manage = can_manage(user, project)
    payload = {
        "members": rows,
        "teams": [{"id": str(dept_id), "name": dept.name} for dept_id, dept in teams.items()],
        "canManage": manage,
    }
    if manage:
        from apps.accounts.models import User

        on_team = set(people)
        candidates = (
            User.objects.filter(
                client_id=project.client_id, deleted_at__isnull=True, is_active=True, status="Active"
            )
            .select_related("role", "employee__department", "employee__designation")
            .order_by("name")
        )
        payload["candidates"] = [
            {
                "userId": str(u.id),
                "name": u.name,
                "email": u.email,
                "department": (
                    u.employee.department.name
                    if u.employee_id and u.employee.department_id else ""
                ),
                "designation": (
                    u.employee.designation.name
                    if u.employee_id and u.employee.designation_id else ""
                ),
            }
            for u in candidates
            if u.id not in on_team and _live(u)
        ]
    return payload


@transaction.atomic
def add_member(project, actor, user_id, department_id=None):
    from apps.accounts.models import User

    user = User.objects.filter(
        pk=chat._parse_uuid(user_id, "userId"),
        client_id=project.client_id,
        deleted_at__isnull=True,
    ).first()
    if user is None or not _live(user):
        raise ValidationFailed("Pick an active employee.", field_errors={"userId": ["Unknown user."]})

    department = None
    if department_id:
        stage = (
            ProjectStage.objects.filter(
                project=project,
                department_id=chat._parse_uuid(department_id, "departmentId"),
                deleted_at__isnull=True,
            )
            .select_related("department")
            .first()
        )
        if stage is None:
            raise ValidationFailed(
                "That team has no stage on this project.",
                field_errors={"departmentId": ["Not a team on this project."]},
            )
        department = stage.department

    member = ProjectMember.objects.filter(
        project=project, user=user, deleted_at__isnull=True
    ).first()
    if member is not None:
        # Already on the list: this moves them to another team.
        member.department = department
        member.save(update_fields=["department", "updated_at"])
    else:
        try:
            with transaction.atomic():
                member = ProjectMember.objects.create(
                    client_id=project.client_id,
                    project=project,
                    user=user,
                    department=department,
                    added_by=actor,
                )
        except IntegrityError:
            raise ValidationFailed("That employee is already on the team.", code="ALREADY_MEMBER")

        notify(
            client=project.client_id,
            recipients=[user.id],
            type="pms.team_added",
            category="pms",
            title=f"You were added to {project.code}",
            body=(
                f"{project.name}: you can now chat with the project team"
                + (f" ({department.name} Team)." if department else ".")
            ),
            entity_type="PmsProject",
            entity_id=project.id,
            actor=actor,
        )

    record_audit(
        client=project.client_id,
        actor=actor,
        action="PROJECT_MEMBER_ADDED",
        entity_type="PmsProject",
        entity_id=project.id,
        entity_label=project.code,
        description=f"{user.name} on the team" + (f" ({department.name})" if department else ""),
    )
    chat.ensure_conversations(project)
    return member


@transaction.atomic
def remove_member(project, actor, member_id):
    member = (
        ProjectMember.objects.filter(
            project=project, pk=chat._parse_uuid(member_id, "memberId"), deleted_at__isnull=True
        )
        .select_related("user")
        .first()
    )
    if member is None:
        raise NotFound("That team member is no longer on the project.")
    member.soft_delete(user=actor)
    record_audit(
        client=project.client_id,
        actor=actor,
        action="PROJECT_MEMBER_REMOVED",
        entity_type="PmsProject",
        entity_id=project.id,
        entity_label=project.code,
        description=f"{member.user.name} off the team",
    )


def participant_project_ids(user):
    """Projects ``user`` works on: as PM, stage or task assignee, or member."""
    live = Q(deleted_at__isnull=True, client_id=user.client_id)
    ids = set(Project.objects.filter(live, project_manager=user).values_list("id", flat=True))
    ids |= set(
        ProjectStage.objects.filter(
            assigned_user=user, deleted_at__isnull=True, project__deleted_at__isnull=True
        ).values_list("project_id", flat=True)
    )
    ids |= set(
        Task.objects.filter(
            assigned_user=user, deleted_at__isnull=True, project__deleted_at__isnull=True
        ).values_list("project_id", flat=True)
    )
    ids |= set(
        ProjectMember.objects.filter(
            user=user, deleted_at__isnull=True, project__deleted_at__isnull=True
        ).values_list("project_id", flat=True)
    )
    return ids


def my_chats(user):
    """``GET /pms/my-chats/`` -- every project I can chat on, with what the
    messenger needs (stages for its stage tags) and my unread count."""
    projects = (
        Project.objects.filter(pk__in=participant_project_ids(user), deleted_at__isnull=True)
        .order_by("-updated_at")
    )
    rows = []
    for project in projects:
        scope = chat.ChatScope(project, user)
        if not scope.is_participant:
            continue
        _, aggregates = chat.list_conversations(scope)
        stages = project.stages.filter(deleted_at__isnull=True).select_related("department").order_by("sequence")
        rows.append(
            {
                "id": str(project.id),
                "code": project.code,
                "name": project.name,
                "customerName": project.customer_name,
                "status": project.status,
                "totalUnread": aggregates["totalUnread"],
                "stages": [
                    {
                        "id": str(stage.id),
                        "name": stage.name,
                        "sequence": stage.sequence,
                        "departmentId": str(stage.department_id) if stage.department_id else None,
                        "department": stage.department.name if stage.department_id else "",
                    }
                    for stage in stages
                ],
            }
        )
    return rows


def require_view(user, project, scope=None):
    if not can_view(user, project, scope):
        # 404, as for conversations: do not confirm the project to outsiders.
        raise NotFound("That project no longer exists.")


def require_manage(user, project):
    if not can_manage(user, project):
        raise PermissionDenied(
            "Only the project manager can change the project team.", code="assign_members"
        )
