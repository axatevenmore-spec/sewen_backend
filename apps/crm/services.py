"""
CRM automation (api.md §9.3).

``leadStageAutomation.js`` and ``taskCompletionService.js`` move here in full.
The client stops generating follow-up tasks; it reads ``createdTasks`` from the
stage-change response and toasts them (api-integration.md §9.3).

Two things the frontend hardcodes and this module reads from data instead:

  - ``CRM_TEAM_MEMBERS`` -- the role -> user roster, now ``users.crm_roles``,
    exposed at ``GET /crm/team-roster/``
  - the stage list -- now ``crm_stages``, tenant-configurable
"""
from datetime import timedelta

from django.db import transaction
from django.db.models import Count, Q
from django.utils import timezone

from apps.core.audit import notify, record_audit
from apps.core.exceptions import ValidationFailed
from apps.core.numbering import allocate_number

from .models import AUTOMATION_SOURCE, Deal, DealActivity, Lead, Stage, StageTask, Task

#: api.md §9.3 -- the fallback roles and offsets when the target stage has no
#: matching template. The server prefers a template from the lead's current
#: stage; these are what it falls back to.
NEXT_ACTION_FALLBACKS = {
    "call-again": {
        "title": "Follow-up Call",
        "role": "Tele Caller Executive",
        "due_in_days": 1,
    },
    "schedule-demo": {
        "title": "Schedule Demo",
        "role": "Area Sales Manager",
        "due_in_days": 2,
    },
    "send-quotation": {
        "title": "Send Quotation",
        "role": "BDE",
        "due_in_days": 1,
    },
}

#: api.md §9.3 -- priority nudges the computed due date forward.
PRIORITY_ADJUSTMENT = {"Urgent": -2, "High": -1, "Medium": 0, "Low": 1}


# ---------------------------------------------------------------------------
# Assignee resolution
# ---------------------------------------------------------------------------
def team_roster(client_id):
    """``GET /crm/team-roster/`` -- role -> users, from ``users.crm_roles``.

    Each person also carries ``isProjectManager`` (system role code ``PM``),
    which the PMS "Project Manager" dropdowns filter on. Users whose system
    role is ``PM`` always appear, even with empty ``crm_roles``.
    All active internal staff members appear under their assigned CRM roles or
    their system role/designation if ``crm_roles`` is empty.
    """
    from apps.accounts.models import User

    roster = {}
    users = (
        User.objects.filter(
            client_id=client_id, deleted_at__isnull=True, status="Active"
        )
        .exclude(role__code="CU")
        .exclude(party__isnull=False)
        .select_related("role", "employee__designation", "employee__department")
    )

    for user in users:
        role_code = user.role.code if user.role_id and user.role else None
        designation = (
            user.employee.designation.name
            if (user.employee_id and user.employee and user.employee.designation)
            else (user.role.name if user.role else "")
        )
        department = (
            user.employee.department.name
            if (user.employee_id and user.employee and user.employee.department)
            else (user.department or "")
        )
        person = {
            "id": str(user.id),
            "name": user.name,
            "email": user.email,
            "department": department,
            "designation": designation,
            "isProjectManager": role_code == "PM",
        }

        assigned_roles = list(user.crm_roles or [])
        if role_code == "PM" and "Project Manager" not in assigned_roles:
            assigned_roles.append("Project Manager")

        if not assigned_roles:
            fallback_role = user.role.name if user.role else (designation or "Staff")
            assigned_roles.append(fallback_role)

        for role in assigned_roles:
            roster.setdefault(role, []).append(dict(person))

    return roster


def resolve_assignee(client_id, role=None, department=None):
    """Role + department, round-robin on open-task count (db.md §9.3).

    The round-robin keeps one keen tele-caller from collecting every generated
    task; ``ix_crm_tasks_assignee`` is what keeps the count cheap.
    """
    from apps.accounts.models import User

    queryset = User.objects.filter(
        client_id=client_id, deleted_at__isnull=True, status="Active"
    )
    if role:
        queryset = queryset.filter(crm_roles__contains=[role])
    if department:
        queryset = queryset.filter(department__iexact=department)

    candidates = list(queryset.only("id"))
    if not candidates and department:
        # Fall back to role alone before giving up -- a mis-typed department
        # should not silently leave the task unassigned.
        candidates = list(
            User.objects.filter(
                client_id=client_id,
                deleted_at__isnull=True,
                status="Active",
                crm_roles__contains=[role] if role else [],
            ).only("id")
        )
    if not candidates:
        return None

    open_counts = dict(
        Task.objects.filter(
            client_id=client_id,
            assignee__in=candidates,
            status__in=["Open", "In Progress", "Waiting"],
            deleted_at__isnull=True,
        )
        .values_list("assignee_id")
        .annotate(count=Count("id"))
    )
    return min(candidates, key=lambda user: open_counts.get(user.id, 0))


def compute_due_date(offset_days, priority="Medium", from_date=None):
    """api.md §9.3 step 3 -- ``dueIn`` offset, adjusted by priority."""
    base = from_date or timezone.localdate()
    days = int(offset_days or 0) + PRIORITY_ADJUSTMENT.get(priority, 0)
    return base + timedelta(days=max(days, 0))


# ---------------------------------------------------------------------------
# Stage automation (api.md §9.3)
# ---------------------------------------------------------------------------
#: Fields a linked stage task takes from its master task. Order, Required,
#: Auto-create and Repeats stay per stage (Lead Stage Tasks owns those).
MASTER_TO_STAGE_TASK = {
    "title": "title",
    "description": "description",
    "role": "assignee_role",
    "department": "department",
    "duration_days": "offset_days",
    "priority": "priority",
}


@transaction.atomic
def sync_master_task_stages(master, *, user=None):
    """Keep one linked ``StageTask`` per stage the master task is used in.

    The Tasks Master's "Used in stages" is what decides where a task type is
    generated; the automation only reads stage tasks, so this is the bridge.
    Linked rows follow the master's name, role, due days, priority and form;
    unticking a stage retires its linked row. A deleted or inactive master
    retires all of them.
    """
    stage_ids = set() if (master.deleted_at or not master.is_active) else {
        stage.id for stage in master.stages.filter(deleted_at__isnull=True)
    }
    linked = {
        row.stage_id: row
        for row in StageTask.objects.filter(master_task=master, deleted_at__isnull=True)
    }
    values = {target: getattr(master, source) for source, target in MASTER_TO_STAGE_TASK.items()}
    actor = user if getattr(user, "is_authenticated", False) else None

    for stage_id in stage_ids - set(linked):
        last = (
            StageTask.objects.filter(stage_id=stage_id, deleted_at__isnull=True)
            .order_by("-sort_order")
            .values_list("sort_order", flat=True)
            .first()
        )
        StageTask.objects.create(
            client_id=master.client_id,
            stage_id=stage_id,
            master_task=master,
            sort_order=(last or 0) + 1,
            created_by=actor,
            **values,
        )
    for stage_id, row in linked.items():
        if stage_id not in stage_ids:
            row.soft_delete(actor)
            continue
        changed = [field for field, value in values.items() if getattr(row, field) != value]
        if changed:
            for field in changed:
                setattr(row, field, values[field])
            row.save(update_fields=changed + ["updated_at"])


@transaction.atomic
def run_stage_automation(lead, stage, *, user=None, due_at=None):
    """Create the target stage's auto-create tasks and notify the assignees.

    Returns the created tasks so ``PATCH /crm/leads/{id}/`` can return
    ``{ lead, createdTasks: [] }`` for the UI to toast.

    ``due_at`` is the date and time the Complete Task form scheduled the move
    for: when it is given, every task it creates is due that day instead of
    being offset from today.
    """
    templates = StageTask.objects.filter(
        client_id=lead.client_id, stage=stage, auto_create=True, deleted_at__isnull=True
    ).select_related("form", "master_task__form").order_by("sort_order")

    created = []
    for template in templates:
        # A template creates its task once per lead by default, so moving back
        # and forth between stages does not pile up duplicates. "Max repeats"
        # raises that cap; the older yes/no ``repeats`` means no cap.
        already = Task.objects.filter(
            client_id=lead.client_id, lead=lead, stage_task=template, deleted_at__isnull=True
        ).count()
        if template.max_repeats is not None:
            if already >= max(template.max_repeats, 1):
                continue
        elif already and not template.repeats:
            continue

        # A blank role or department on the stage row falls back to the master.
        master = template.master_task if template.master_task_id else None
        role = template.assignee_role or (master.role if master else None)
        department = template.department or (master.department if master else None)
        assignee = resolve_assignee(lead.client_id, role, department)
        extra = {"stage": stage.name}
        if due_at is not None:
            extra["dueAt"] = due_at.strftime("%Y-%m-%dT%H:%M")
            extra["dueTime"] = due_at.strftime("%H:%M")
        task = Task.objects.create(
            client_id=lead.client_id,
            task_number=allocate_number(lead.client, "TSK"),
            lead=lead,
            stage_task=template,
            title=template.title,
            description=template.description or (master.description if master else None),
            assignee=assignee,
            assignee_role=role,
            department=department,
            # The lead task editor opens this form (the frontend reads extra).
            extra=extra,
            due_date=(
                due_at.date() if due_at is not None
                else compute_due_date(template.offset_days, template.priority)
            ),
            priority=template.priority,
            status="Open",
            source=AUTOMATION_SOURCE,
            created_by=user if getattr(user, "is_authenticated", False) else None,
        )
        created.append(task)

        if assignee is not None:
            notify(
                client=lead.client_id,
                recipients=[assignee],
                type="crm.task_assigned",
                category="crm",
                title=f"New task: {task.title}",
                body=f"{lead.name} moved to {stage.name}.",
                entity_type="CrmTask",
                entity_id=task.id,
                actor=user,
            )
    return created


@transaction.atomic
def change_lead_stage(lead, target_stage, *, user=None, due_at=None):
    """The stage change and its automation, in one transaction."""
    previous = lead.stage
    if previous_id_equals(previous, target_stage):
        return lead, []

    lead.stage = target_stage
    if target_stage.is_won or target_stage.is_lost:
        pass  # closing a lead is a separate, explicit action
    lead.save(update_fields=["stage", "updated_at"])

    record_audit(
        client=lead.client_id,
        actor=user,
        action="stage_change",
        entity_type="CrmLead",
        entity_id=lead.id,
        entity_label=lead.lead_number,
        description=f"Stage changed to {target_stage.name}",
        from_value=previous.name if previous else None,
        to_value=target_stage.name,
    )

    created = run_stage_automation(lead, target_stage, user=user, due_at=due_at)
    return lead, created


def previous_id_equals(previous, target):
    return previous is not None and str(previous.id) == str(target.id)


def next_stage_after(client_id, stage):
    """The stage a lead moves on to. Never the Lost stage: losing a lead is its
    own action (Mark as Lost), not the next step of the pipeline."""
    return (
        Stage.objects.filter(
            client_id=client_id,
            deleted_at__isnull=True,
            is_active=True,
            is_lost=False,
            sequence__gt=stage.sequence,
        )
        .order_by("sequence")
        .first()
    )


def resolve_target_stage(lead, target_stage):
    """The stage a ``move-next-stage`` lands on.

    The Complete Task form's "Move to stage" picker sends the stage the lead is
    really in, which may be further along than the next one (it skipped ahead) or
    somewhere else entirely (it was lost). Left unset, the lead simply goes to
    the stage after its current one.
    """
    if target_stage is None:
        return next_stage_after(lead.client_id, lead.stage)

    if str(target_stage.client_id) != str(lead.client_id):
        raise ValidationFailed(
            "That stage belongs to another company.", field_errors={"targetStageId": ["Unknown stage."]}
        )
    if not target_stage.is_active:
        raise ValidationFailed(
            f"{target_stage.name} is not an active stage.",
            field_errors={"targetStageId": ["That stage is not active."]},
        )
    if previous_id_equals(lead.stage, target_stage):
        raise ValidationFailed(
            f"The lead is already in {target_stage.name}.",
            field_errors={"targetStageId": ["Pick a different stage."]},
        )
    return target_stage


# ---------------------------------------------------------------------------
# Task completion (api.md §9.3)
# ---------------------------------------------------------------------------
#: A task still to be worked: what blocks a stage move and what "finished with
#: this lead" closes.
OPEN_TASK_STATUSES = ["Open", "In Progress", "Waiting"]


def open_required_tasks(lead, *, exclude=None):
    """The lead's open tasks from a *required* template of its current stage.

    A lead may not move on while one of these is still open (the Complete Task
    form's "Ready to move on").
    """
    if lead is None or lead.stage_id is None:
        return Task.objects.none()
    rows = Task.objects.filter(
        client_id=lead.client_id,
        lead=lead,
        status__in=OPEN_TASK_STATUSES,
        stage_task__stage_id=lead.stage_id,
        stage_task__required=True,
        deleted_at__isnull=True,
    )
    if exclude is not None:
        rows = rows.exclude(pk=exclude.pk)
    return rows


@transaction.atomic
def complete_task(
    task, *, outcome=None, next_action=None, note=None, user=None,
    follow_up_at=None, close_open_tasks=False, target_stage=None,
):
    """``POST /crm/tasks/{id}/complete/``.

    The ``nextAction`` table from api.md §9.3, with the server preferring a
    matching template from the lead's current stage over the hardcoded
    fallbacks.

    The Complete Task form adds three optional inputs:

    - ``follow_up_at`` with ``call-again``: the next attempt of *this* task,
      scheduled for that date and time, instead of a generic follow-up call.
    - ``close_open_tasks`` with ``finish``: every other open task on the lead
      is closed too. The lead stays in its stage.
    - ``target_stage`` with ``move-next-stage``: the stage the lead is moved to
      instead of the next one, for a lead that skipped ahead or was lost. The
      tasks the stage creates are due on ``follow_up_at`` when it is given.

    ``move-next-stage`` is refused while a required task of the current stage
    is still open.
    """
    if task.status == "Completed":
        from apps.core.exceptions import Conflict, Codes

        raise Conflict("This task is already completed.", code=Codes.ALREADY_DONE)

    if next_action == "move-next-stage" and task.lead_id:
        blocking = list(open_required_tasks(task.lead, exclude=task).values_list("title", flat=True))
        if blocking:
            from apps.core.exceptions import BusinessRuleViolation

            names = ", ".join(dict.fromkeys(blocking))
            raise BusinessRuleViolation(
                f"Complete these tasks before moving the lead on: {names}.",
                code="REQUIRED_TASKS_OPEN",
            )

    task.status = "Completed"
    task.outcome = outcome
    task.next_action = next_action
    task.completion_note = note
    task.completed_at = timezone.now()
    task.completed_by = user if getattr(user, "is_authenticated", False) else None
    task.save()

    follow_up = None
    stage_changed = False
    created_tasks = []
    moved_to = None

    if next_action == "move-next-stage" and task.lead_id:
        lead = task.lead
        target = resolve_target_stage(lead, target_stage)
        if target is not None:
            # Advancing runs the target stage's own automation, per api.md §9.3.
            lead, created_tasks = change_lead_stage(
                lead, target, user=user, due_at=follow_up_at
            )
            stage_changed = True
            moved_to = target

    elif next_action == "call-again" and follow_up_at and task.lead_id:
        follow_up = _create_next_attempt(task, follow_up_at, user=user)
        created_tasks = [follow_up]
        task.follow_up_task = follow_up
        task.save(update_fields=["follow_up_task", "updated_at"])

    elif next_action in NEXT_ACTION_FALLBACKS and task.lead_id:
        follow_up = _create_follow_up(task, next_action, user=user)
        if follow_up is not None:
            created_tasks = [follow_up]
            task.follow_up_task = follow_up
            task.save(update_fields=["follow_up_task", "updated_at"])

    elif next_action == "finish" and close_open_tasks and task.lead_id:
        closed_at = timezone.now()
        Task.objects.filter(
            client_id=task.client_id,
            lead_id=task.lead_id,
            status__in=OPEN_TASK_STATUSES,
            deleted_at__isnull=True,
        ).exclude(pk=task.pk).update(
            status="Cancelled",
            completion_note="Closed: finished with this lead.",
            completed_at=closed_at,
            completed_by=task.completed_by,
            updated_at=closed_at,
        )

    record_audit(
        client=task.client_id,
        actor=user,
        action="task_completed",
        entity_type="CrmTask",
        entity_id=task.id,
        entity_label=task.title,
        description=f"Task completed ({outcome or 'no outcome'})",
        comments=note,
    )

    return {
        "task": task,
        "followUpTask": follow_up,
        "createdTasks": created_tasks,
        "stageChanged": stage_changed,
        "movedToStage": moved_to.name if moved_to is not None else None,
    }


def _create_next_attempt(task, follow_up_at, *, user=None):
    """The same task again, as attempt N+1, due at ``follow_up_at``."""
    if timezone.is_aware(follow_up_at):
        follow_up_at = timezone.localtime(follow_up_at)
    extra = dict(task.extra or {})
    # This attempt's progress and answers belong to the attempt just closed.
    for key in ("process", "customValues", "taskFormAnswers"):
        extra.pop(key, None)
    extra["attempt"] = int(extra.get("attempt") or 1) + 1
    extra["dueAt"] = follow_up_at.strftime("%Y-%m-%dT%H:%M")
    extra["dueTime"] = follow_up_at.strftime("%H:%M")

    follow_up = Task.objects.create(
        client_id=task.client_id,
        task_number=allocate_number(task.lead.client, "TSK"),
        lead=task.lead,
        deal=task.deal,
        stage_task=task.stage_task,
        title=task.title,
        description=task.description,
        assignee=task.assignee,
        assignee_role=task.assignee_role,
        department=task.department,
        due_date=follow_up_at.date(),
        priority=task.priority,
        status="Open",
        source=task.source,
        extra=extra,
        created_by=user if getattr(user, "is_authenticated", False) else None,
    )
    if follow_up.assignee_id:
        notify(
            client=task.client_id,
            recipients=[follow_up.assignee],
            type="crm.task_assigned",
            category="crm",
            title=f"New task: {follow_up.title} (attempt {extra['attempt']})",
            body=f"Follow-up for {task.lead.name}.",
            entity_type="CrmTask",
            entity_id=follow_up.id,
            actor=user,
        )
    return follow_up


def _create_follow_up(task, next_action, *, user=None):
    fallback = NEXT_ACTION_FALLBACKS[next_action]
    lead = task.lead

    # Prefer a matching template from the lead's current stage (api.md §9.3).
    template = (
        StageTask.objects.filter(
            client_id=lead.client_id,
            stage=lead.stage,
            title__iexact=fallback["title"],
            deleted_at__isnull=True,
        ).first()
        or StageTask.objects.filter(
            client_id=lead.client_id,
            stage=lead.stage,
            assignee_role=fallback["role"],
            deleted_at__isnull=True,
        ).first()
    )

    role = template.assignee_role if template else fallback["role"]
    department = template.department if template else None
    offset = template.offset_days if template else fallback["due_in_days"]
    priority = template.priority if template else "Medium"
    title = template.title if template else fallback["title"]

    assignee = resolve_assignee(lead.client_id, role, department)
    follow_up = Task.objects.create(
        client_id=lead.client_id,
        task_number=allocate_number(lead.client, "TSK"),
        lead=lead,
        stage_task=template,
        title=title,
        description=f"Follow-up from: {task.title}",
        assignee=assignee,
        assignee_role=role,
        department=department,
        due_date=compute_due_date(offset, priority),
        priority=priority,
        status="Open",
        source=AUTOMATION_SOURCE,
        created_by=user if getattr(user, "is_authenticated", False) else None,
    )

    if assignee is not None:
        notify(
            client=lead.client_id,
            recipients=[assignee],
            type="crm.task_assigned",
            category="crm",
            title=f"New task: {follow_up.title}",
            body=f"Follow-up for {lead.name}.",
            entity_type="CrmTask",
            entity_id=follow_up.id,
            actor=user,
        )
    return follow_up


# ---------------------------------------------------------------------------
# Lead counters (api.md §9.1)
# ---------------------------------------------------------------------------
def annotate_lead_counters(client_id, leads):
    """The eight counters the list row renders, computed server-side.

    db.md §9.1 and §15 both say: paginate first, then count only the page's
    leads. That is what this does -- one query per counter family over the 25
    ids on screen, not a lateral subquery per row over the whole table.
    """
    leads = list(leads)
    if not leads:
        return leads
    lead_ids = [lead.id for lead in leads]

    from apps.sales.models import DeliveryChallan, Estimate, SalesInvoice

    from .models import LeadCall, LeadFile, LeadProduct, LeadSource

    def counts(model, field="lead_id", extra=None):
        queryset = model.objects.filter(
            client_id=client_id, deleted_at__isnull=True, **{f"{field}__in": lead_ids}
        )
        if extra:
            queryset = queryset.filter(**extra)
        return dict(
            queryset.values_list(field).annotate(count=Count("id"))
        )

    products = counts(LeadProduct)
    sources = counts(LeadSource)
    files = counts(LeadFile)
    calls = counts(LeadCall)
    open_tasks = counts(Task, extra={"status__in": ["Open", "In Progress", "Waiting"]})
    estimates = counts(Estimate, field="crm_lead_id")
    invoices = dict(
        SalesInvoice.objects.filter(
            client_id=client_id,
            deleted_at__isnull=True,
            sales_order__quotation__crm_lead_id__in=lead_ids,
        )
        .values_list("sales_order__quotation__crm_lead_id")
        .annotate(count=Count("id"))
    )
    challans = dict(
        DeliveryChallan.objects.filter(
            client_id=client_id,
            deleted_at__isnull=True,
            quotation__crm_lead_id__in=lead_ids,
        )
        .values_list("quotation__crm_lead_id")
        .annotate(count=Count("id"))
    )

    for lead in leads:
        lead.products_count = products.get(lead.id, 0)
        lead.sources_count = sources.get(lead.id, 0)
        lead.files_count = files.get(lead.id, 0)
        lead.calls_count = calls.get(lead.id, 0)
        lead.open_tasks_count = open_tasks.get(lead.id, 0)
        lead.estimates_count = estimates.get(lead.id, 0)
        lead.sales_invoices_count = invoices.get(lead.id, 0)
        lead.delivery_challans_count = challans.get(lead.id, 0)
    return leads


# ---------------------------------------------------------------------------
# Lead status <-> Deals pipeline
# ---------------------------------------------------------------------------
#: Deal stages that close a deal; a lead in a won / lost stage has its deals here.
CLOSED_DEAL_STAGES = ("Won", "Lost")


def is_crm_admin(user):
    """Admins may reopen a lost lead or deal."""
    from apps.core.permissions import has_permission

    from .access import ADMIN_PERMISSION

    return bool(getattr(user, "is_superuser", False) or has_permission(user, ADMIN_PERMISSION))


#: The look of a Lost / Won stage made for a workspace that has none.
CLOSING_STAGE_DEFAULTS = {
    True: {"name": "Lost", "color": "#b91c1c", "icon": "XCircle", "bg": "#fef2f2", "fg": "#991b1b"},
    False: {"name": "Won", "color": "#15803d", "icon": "Trophy", "bg": "#f0fdf4", "fg": "#166534"},
}


def closing_stage(client_id, *, lost):
    """The tenant's Lost (or Won) lead stage -- created at the end of the
    pipeline when the workspace has none, so Mark as Lost always has a home."""
    flag = {"is_lost": True} if lost else {"is_won": True}
    live = Stage.objects.filter(client_id=client_id, deleted_at__isnull=True)
    stage = live.filter(**flag).order_by("sequence").first()
    if stage is not None:
        return stage

    defaults = CLOSING_STAGE_DEFAULTS[lost]
    # A stage already called "Lost" / "Won" just gets its flag.
    stage = live.filter(name__iexact=defaults["name"]).first()
    if stage is not None:
        for field, value in flag.items():
            setattr(stage, field, value)
        stage.save(update_fields=[*flag, "updated_at"])
        return stage
    last = live.order_by("-sequence").values_list("sequence", flat=True).first() or 0
    return Stage.objects.create(
        client_id=client_id,
        sequence=last + 1,
        is_active=True,
        **flag,
        **defaults,
    )


def reopen_target(lead):
    """Where a reopened lead goes: the stage it was closed from, else the first open one."""
    stage = lead.reopen_stage
    if stage is not None and stage.deleted_at is None and not (stage.is_lost or stage.is_won):
        return stage
    return (
        Stage.objects.filter(
            client_id=lead.client_id, deleted_at__isnull=True, is_active=True,
            is_lost=False, is_won=False,
        )
        .order_by("sequence")
        .first()
    )


def _actor(user):
    return user if getattr(user, "is_authenticated", False) else None


def lead_deals(lead):
    return Deal.objects.filter(lead=lead, deleted_at__isnull=True)


def open_lead_deal(lead, stage="Draft", *, user=None, reason=""):
    """The lead's deal in the pipeline, created in ``stage``."""
    deal = Deal.objects.create(
        client_id=lead.client_id,
        deal_number=allocate_number(lead.client, "DEAL"),
        title=f"{lead.company or lead.name} opportunity",
        lead=lead,
        party=lead.party,
        owner=lead.owner or _actor(user),
        stage=stage,
        value=lead.amount or 0,
        client_name=lead.company or lead.name,
        contact_phone=lead.phone,
        closed_at=timezone.now() if stage in CLOSED_DEAL_STAGES else None,
        lost_reason=lead.lost_reason if stage == "Lost" else None,
        created_by=_actor(user),
    )
    DealActivity.objects.create(
        deal=deal, type="created", actor=_actor(user),
        description=f"Created in {stage} from lead {lead.lead_number}{reason}",
    )
    return deal


def ensure_lead_deal(lead, *, user=None):
    """A new lead joins the Deals pipeline as a Draft deal (once)."""
    if lead.deleted_at is None and not lead_deals(lead).exists():
        return open_lead_deal(lead, "Draft", user=user, reason=" (new lead)")
    return None


def _move_lead_deals(lead, stage, *, user=None, create=False, only_from=None, reason=""):
    """Put the lead's deals in ``stage``; with ``create``, open one if it has none."""
    now = timezone.now()
    deals = list(lead_deals(lead))
    if not deals:
        if create:
            open_lead_deal(lead, stage, user=user, reason=reason)
        return

    for deal in deals:
        if deal.stage == stage or (only_from and deal.stage not in only_from):
            continue
        previous = deal.stage
        deal.stage = stage
        deal.closed_at = now if stage in CLOSED_DEAL_STAGES else None
        if stage == "Lost":
            deal.lost_reason = lead.lost_reason or deal.lost_reason
        else:
            deal.lost_reason = None
        deal.save(update_fields=["stage", "closed_at", "lost_reason", "updated_at"])
        DealActivity.objects.create(
            deal=deal, type="stage_change", actor=_actor(user),
            description=f"Stage moved from {previous} to {stage}{reason}",
        )


@transaction.atomic
def sync_deals_after_lead_move(lead, previous_stage, *, user=None):
    """A lead's stage moved: its deals follow.

    Lost / Won put the deals in Lost / Won (opening one when the lead has none,
    so a lost lead shows in the Deals pipeline's Lost column). Leaving Lost or
    Won reopens the deals that were closed.
    """
    target = lead.stage
    reason = f" (lead moved to {target.name})"
    if target.is_lost or target.is_won:
        if previous_stage is not None and not (previous_stage.is_lost or previous_stage.is_won):
            lead.reopen_stage = previous_stage
            lead.save(update_fields=["reopen_stage", "updated_at"])
        _move_lead_deals(
            lead, "Lost" if target.is_lost else "Won", user=user, create=True, reason=reason
        )
    elif previous_stage is not None and (previous_stage.is_lost or previous_stage.is_won):
        lead.reopen_stage = None
        lead.lost_reason = None
        lead.save(update_fields=["reopen_stage", "lost_reason", "updated_at"])
        _move_lead_deals(lead, "Open", user=user, only_from=CLOSED_DEAL_STAGES, reason=reason)


@transaction.atomic
def sync_lead_after_deal_move(deal, previous, *, user=None):
    """A deal's stage moved: its lead follows.

    Lost / Won close the lead into the Lost / Won lead stage; moving the deal
    out of Lost or Won reopens the lead to the stage it was closed from.
    Returns the tasks the lead's stage automation created.
    """
    lead = deal.lead
    if lead is None or lead.deleted_at is not None:
        return []
    current = lead.stage
    current_is_lost = bool(current and current.is_lost)
    current_is_won = bool(current and current.is_won)
    if deal.stage == "Lost" and not current_is_lost:
        target = closing_stage(lead.client_id, lost=True)
    elif deal.stage == "Won" and not current_is_won:
        target = closing_stage(lead.client_id, lost=False)
    elif (
        previous in CLOSED_DEAL_STAGES
        and deal.stage not in CLOSED_DEAL_STAGES
        and (current_is_lost or current_is_won)
    ):
        target = reopen_target(lead)
    else:
        return []
    if target is None:
        return []

    if target.is_lost or target.is_won:
        if not (current_is_lost or current_is_won):
            lead.reopen_stage = current
        if target.is_lost and deal.lost_reason_id:
            lead.lost_reason = deal.lost_reason
    else:
        lead.reopen_stage = None
        lead.lost_reason = None
    lead.stage = target
    lead.save(update_fields=["stage", "reopen_stage", "lost_reason", "updated_at"])
    record_audit(
        client=lead.client_id,
        actor=user,
        action="stage_change",
        entity_type="CrmLead",
        entity_id=lead.id,
        entity_label=lead.lead_number,
        description=f"Stage changed to {target.name} (deal {deal.deal_number} moved to {deal.stage})",
        from_value=current.name if current else "",
        to_value=target.name,
    )
    # The lead's other deals follow it too.
    _move_lead_deals(
        lead,
        deal.stage if deal.stage in CLOSED_DEAL_STAGES else "Open",
        user=user,
        only_from=None if deal.stage in CLOSED_DEAL_STAGES else CLOSED_DEAL_STAGES,
        reason=f" (deal {deal.deal_number} moved to {deal.stage})",
    )
    return run_stage_automation(lead, target, user=user)


#: A sales document's status -> where its lead's deal moves: an estimate or
#: quotation sent to the lead puts a Draft deal in Sent; one the lead accepted
#: (or a quotation turned into an order) wins the deal. A rejected/declined
#: estimate puts the deal in Open.
DOCUMENT_DEAL_STAGE = {
    "estimate": {
        "Draft": "Draft",
        "Sent": "Sent",
        "Accepted": "Won",
        "Declined": "Open",
        "Rejected": "Open",
        "Need Improvement": "Open",
    },
    "quotation": {
        "Draft": "Draft",
        "Sent": "Sent",
        "Viewed": "Sent",
        "Accepted": "Won",
        "Converted": "Won",
        "Declined": "Open",
        "Rejected": "Open",
    },
}


@transaction.atomic
def sync_deal_from_document(document, kind, *, user=None):
    """An estimate / quotation for a lead changed status: its deal follows.

    ``Sent`` only moves a deal that is still a Draft; ``Won`` moves any deal
    that is not closed. A Lost deal is left alone -- reopening one is an admin's
    call. A lead with no deal yet gets one.
    """
    target = DOCUMENT_DEAL_STAGE[kind].get(document.status)
    lead = getattr(document, "crm_lead", None)
    if target is None or lead is None or lead.deleted_at is not None:
        return
    actor = user or getattr(document, "created_by", None)
    number = getattr(document, f"{kind}_number", None)
    reason = f" ({kind} {number + ' ' if number else ''}{document.status.lower()})"

    deal = getattr(document, "crm_deal", None) if kind == "quotation" else None
    deals = [deal] if deal is not None and deal.deleted_at is None else list(lead_deals(lead))
    if not deals:
        deals = [open_lead_deal(lead, "Draft", user=actor, reason=reason)]

    for deal in deals:
        if deal.stage in CLOSED_DEAL_STAGES or deal.stage == target:
            continue
        if target == "Sent" and deal.stage != "Draft":
            continue
        previous = deal.stage
        deal.stage = target
        deal.closed_at = timezone.now() if target in CLOSED_DEAL_STAGES else None
        update_fields = ["stage", "closed_at", "updated_at"]
        doc_total = getattr(document, "total_amount", None) or getattr(document, "total", None)
        if doc_total is not None and (deal.value is None or deal.value == 0):
            deal.value = doc_total
            update_fields.append("value")
        deal.save(update_fields=update_fields)
        DealActivity.objects.create(
            deal=deal, type="stage_change", actor=_actor(actor),
            description=f"Stage moved from {previous} to {target}{reason}",
        )
        # A won deal wins its lead too.
        sync_lead_after_deal_move(deal, previous, user=actor)


#: How long an auto-generated contract runs from the day the estimate was accepted.
AUTO_CONTRACT_DAYS = 365


def _estimate_marker(estimate):
    return f"Generated automatically from estimate {estimate.estimate_number or estimate.id}"


@transaction.atomic
def contract_from_estimate(estimate, *, user=None):
    """The lead accepted an estimate: a Draft contract is generated from it.

    The contract is for the estimate's customer (the lead's, when the estimate
    was addressed to the lead itself -- created from the lead the way Convert
    does, since a contract needs a customer record), worth the estimate's
    total, linked to the lead's deal, and runs a year from today. One contract
    per estimate.
    """
    from apps.masters.models import Party

    from .models import Contract

    lead = estimate.crm_lead
    if lead is None or lead.deleted_at is not None:
        return None
    marker = _estimate_marker(estimate)
    if Contract.objects.filter(
        client_id=estimate.client_id, description__startswith=marker, deleted_at__isnull=True
    ).exists():
        return None

    actor = _actor(user or estimate.created_by)
    party = estimate.party or lead.party
    if party is None:
        party = Party.objects.create(
            client_id=lead.client_id,
            code=allocate_number(lead.client, "CUST"),
            type="Customer",
            name=estimate.party_name or lead.company or lead.name,
            phone=estimate.party_phone or lead.phone,
            email=estimate.party_email or lead.email,
            place_of_supply=estimate.place_of_supply or lead.state,
            created_by=actor,
        )
    if lead.party_id is None:
        lead.party = party
        lead.save(update_fields=["party", "updated_at"])

    deal = lead_deals(lead).order_by("created_at").first()
    today = timezone.localdate()
    customer_name = party.name
    contract = Contract.objects.create(
        client_id=lead.client_id,
        contract_number=allocate_number(lead.client, "CON", today),
        title=f"Contract - {customer_name}",
        party=party,
        deal=deal,
        contract_type="Other",
        value=estimate.total,
        start_date=today,
        end_date=today + timedelta(days=AUTO_CONTRACT_DAYS),
        status="Draft",
        customer_name=customer_name,
        description=f"{marker} accepted by {lead.name} on {today:%d-%m-%Y}.",
        created_by=actor,
    )
    if deal is not None:
        DealActivity.objects.create(
            deal=deal, type="contract", actor=actor,
            description=f"Contract {contract.contract_number} generated from the accepted estimate.",
        )
    record_audit(
        client=lead.client_id,
        actor=actor,
        action="contract_generated",
        entity_type="CrmLead",
        entity_id=lead.id,
        entity_label=lead.lead_number,
        description=f"Contract {contract.contract_number} generated from estimate {estimate.estimate_number or ''}".strip(),
    )
    return contract


@transaction.atomic
def mark_lead_lost(lead, *, lost_reason=None, user=None):
    """The Leads list's "Mark as Lost": the lead moves to the Lost stage."""
    target = closing_stage(lead.client_id, lost=True)
    if target is None:
        raise ValidationFailed("No Lost stage is configured. Add one in CRM System Setup.")
    if lead.stage_id == target.id:
        return lead
    previous = lead.stage
    lead.stage = target
    if lost_reason is not None:
        lead.lost_reason = lost_reason
    lead.save(update_fields=["stage", "lost_reason", "updated_at"])
    record_audit(
        client=lead.client_id,
        actor=user,
        action="stage_change",
        entity_type="CrmLead",
        entity_id=lead.id,
        entity_label=lead.lead_number,
        description="Marked as lost",
        from_value=previous.name if previous else None,
        to_value=target.name,
    )
    try:
        sync_deals_after_lead_move(lead, previous, user=user)
    except Exception:
        pass
    return lead


# ---------------------------------------------------------------------------
# Seed data
# ---------------------------------------------------------------------------
#: api.md §9.3 -- the shipped stage defaults, including the inactive `Future`.
DEFAULT_STAGES = [
    ("New Lead", 1, "#2563eb", "UserPlus", "#eff6ff", "#1d4ed8", False, False, True),
    ("Details Collected", 2, "#7c3aed", "ClipboardList", "#f5f3ff", "#6d28d9", False, False, True),
    ("Quotation Shared", 3, "#0891b2", "FileText", "#ecfeff", "#0e7490", False, False, True),
    ("Demo Pending", 4, "#c2410c", "Clock", "#fff7ed", "#9a3412", False, False, True),
    ("Demo Done", 5, "#4338ca", "CheckSquare", "#eef2ff", "#3730a3", False, False, True),
    ("Negotiation", 6, "#a16207", "Handshake", "#fefce8", "#854d0e", False, False, True),
    ("Won", 7, "#15803d", "Trophy", "#f0fdf4", "#166534", True, False, True),
    ("Lost", 8, "#b91c1c", "XCircle", "#fef2f2", "#991b1b", False, True, True),
    ("Future", 9, "#64748b", "CalendarClock", "#f8fafc", "#475569", False, False, False),
]

#: api.md §9.3 -- the deal catalogue is separate and independent.
DEFAULT_DEAL_STAGES = [
    ("Draft", 1, False, False),
    ("Sent", 2, False, False),
    ("Open", 3, False, False),
    ("Revised", 4, False, False),
    ("Declined", 5, False, True),
    ("Won", 6, True, False),
    ("Lost", 7, False, True),
]


def seed_crm_configuration(client):
    """Create the shipped stage catalogues for a new tenant. Idempotent."""
    from .models import DealStage, Source

    created = []
    for name, sequence, color, icon, bg, fg, is_won, is_lost, is_active in DEFAULT_STAGES:
        stage, was_created = Stage.objects.get_or_create(
            client=client,
            name=name,
            defaults={
                "sequence": sequence,
                "color": color,
                "icon": icon,
                "bg": bg,
                "fg": fg,
                "is_won": is_won,
                "is_lost": is_lost,
                "is_active": is_active,
            },
        )
        if was_created:
            created.append(stage)

    for name, sequence, is_won, is_lost in DEFAULT_DEAL_STAGES:
        DealStage.objects.get_or_create(
            client=client,
            name=name,
            defaults={"sequence": sequence, "is_won": is_won, "is_lost": is_lost},
        )

    for name in ("Website", "Referral", "Cold Call", "Exhibition", "Walk-in", "Campaign"):
        Source.objects.get_or_create(client=client, name=name)

    return created
