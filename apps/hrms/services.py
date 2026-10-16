"""
HRMS services (api.md §11).

The three interlocking pieces db.md §11 calls out:

  - attendance derives ``hours`` and the Late / Half Day verdict from the
    ``attendance_flexibility`` policy
  - leave approval posts attendance **and** decrements the balance in one
    transaction
  - payroll ports ``salaryCalculations.js`` verbatim, and freezes the result on
    the payslip so a later attendance edit cannot silently restate a paid month
"""
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from django.db import transaction
from django.db.models import Count, Q, Sum
from django.utils import timezone

from apps.core.audit import notify, record_audit
from apps.core.exceptions import BusinessRuleViolation, Codes, Conflict, ValidationFailed
from apps.core.money import ZERO, D, round2
from apps.core.numbering import allocate_number

from .models import (
    Attendance,
    AttendancePunch,
    CalendarEvent,
    Employee,
    Holiday,
    LeaveBalance,
    LeaveRequest,
    Payslip,
    PayslipComponent,
    PayrollRun,
    SalaryAdvance,
    WorkingDay,
)

#: api.md §11.4 -- totalDays defaults to 24.
DEFAULT_TOTAL_DAYS = Decimal("24")

#: Defaults for the `attendance_flexibility` settings blob (db.md §2.8).
DEFAULT_FLEXIBILITY = {
    "graceMinutes": 15,
    "shiftStart": "09:30",
    "shiftEnd": "18:30",
    "fullDayHours": 8,
    "halfDayThresholdHours": 4,
    "coreHoursStart": "11:00",
    "coreHoursEnd": "16:00",
}


def flexibility_policy(client_id):
    from apps.core.models import Setting
    import re

    row = Setting.objects.filter(client_id=client_id, key="attendance_flexibility").first()
    policy = dict(DEFAULT_FLEXIBILITY)
    if row and isinstance(row.value, dict):
        raw = row.value
        policy.update(raw)

        # Normalize frontend keys if present
        if "gracePeriod" in raw and raw["gracePeriod"]:
            m = re.search(r"(\d+)", str(raw["gracePeriod"]))
            if m:
                policy["graceMinutes"] = int(m.group(1))

        if "halfDayThreshold" in raw and raw["halfDayThreshold"]:
            m = re.search(r"(\d+(\.\d+)?)", str(raw["halfDayThreshold"]))
            if m:
                policy["halfDayThresholdHours"] = float(m.group(1))

        if "minimumWorkingHours" in raw and raw["minimumWorkingHours"]:
            m = re.search(r"(\d+(\.\d+)?)", str(raw["minimumWorkingHours"]))
            if m:
                policy["fullDayHours"] = float(m.group(1))

        if "flexibleWorkingHours" in raw and raw["flexibleWorkingHours"]:
            times = re.findall(r"(\d{1,2}:\d{2})", str(raw["flexibleWorkingHours"]))
            if len(times) >= 1:
                policy["shiftStart"] = times[0]
            if len(times) >= 2:
                policy["shiftEnd"] = times[1]

        if "lateAfter" in raw and raw["lateAfter"]:
            policy["lateAfter"] = str(raw["lateAfter"])[:5]

        if "overtimeStartsAfter" in raw and raw["overtimeStartsAfter"]:
            m = re.search(r"(\d+(\.\d+)?)", str(raw["overtimeStartsAfter"]))
            if m:
                policy["overtimeStartsAfterHours"] = float(m.group(1))

    return policy


def _parse_time(value, fallback):
    try:
        hour, minute = str(value).split(":")[:2]
        return time(int(hour), int(minute))
    except (ValueError, AttributeError):
        return fallback


def get_employee_schedule(client_id, employee, work_date=None):
    """Resolve an employee's applicable WorkSchedule with precedence:
    1. Direct Employee.work_schedule
    2. Department.work_schedules (active)
    3. Team.work_schedules (active)
    4. Tenant default WorkSchedule (is_default=True, active)
    5. Fallback to None (caller falls back to flexibility_policy)
    """
    from apps.hrms.models import WorkSchedule, Employee

    if employee is None:
        return WorkSchedule.objects.filter(
            client_id=client_id, is_default=True, status="Active", deleted_at__isnull=True
        ).first()

    if isinstance(employee, (int, str)):
        employee = Employee.objects.filter(client_id=client_id, pk=employee, deleted_at__isnull=True).first()
        if not employee:
            return None

    def _is_active_on_date(sched):
        if not sched or sched.status != "Active":
            return False
        if work_date:
            if sched.effective_from and work_date < sched.effective_from:
                return False
            if sched.effective_to and work_date > sched.effective_to:
                return False
        return True

    # 1. Direct assignment
    if employee.work_schedule and _is_active_on_date(employee.work_schedule):
        return employee.work_schedule

    # 2. Department assignment
    if employee.department_id:
        dept_sched = WorkSchedule.objects.filter(
            client_id=client_id, department_id=employee.department_id, status="Active", deleted_at__isnull=True
        ).first()
        if _is_active_on_date(dept_sched):
            return dept_sched

    # 3. Team assignment
    team_ids = []
    if hasattr(employee, "teams"):
        team_ids.extend(list(employee.teams.values_list("id", flat=True)))
    if hasattr(employee, "led_teams"):
        team_ids.extend(list(employee.led_teams.values_list("id", flat=True)))
    if team_ids:
        team_sched = WorkSchedule.objects.filter(
            client_id=client_id, team_id__in=team_ids, status="Active", deleted_at__isnull=True
        ).first()
        if _is_active_on_date(team_sched):
            return team_sched

    # 4. Default schedule
    default_sched = WorkSchedule.objects.filter(
        client_id=client_id, is_default=True, status="Active", deleted_at__isnull=True
    ).first()
    if _is_active_on_date(default_sched):
        return default_sched

    return None


def is_employee_holiday(client_id, date, employee=None):
    """Check if date is a published holiday for tenant and employee scope."""
    from apps.hrms.models import Holiday, Employee
    qs = Holiday.objects.filter(
        client_id=client_id,
        date=date,
        status="Published",
        deleted_at__isnull=True,
    )
    if not qs.exists():
        return False

    if employee is None:
        return True

    if isinstance(employee, (int, str)):
        employee = Employee.objects.filter(client_id=client_id, pk=employee).first()

    # If any published holiday on this date is global (no location and no department)
    if qs.filter(location__isnull=True, department__isnull=True).exists():
        return True

    # Or matches employee's location or department
    if employee:
        if employee.location_id and qs.filter(location_id=employee.location_id).exists():
            return True
        if employee.department_id and qs.filter(department_id=employee.department_id).exists():
            return True

    return False


def is_employee_weekly_off(client_id, date, employee=None, schedule=None):
    """Check if date is a weekly off day for the employee's schedule."""
    if schedule is None:
        schedule = get_employee_schedule(client_id, employee, work_date=date)

    weekday = date.weekday()
    if schedule and schedule.weekly_offs is not None and len(schedule.weekly_offs) > 0:
        return weekday in schedule.weekly_offs

    if schedule and schedule.working_days is not None and len(schedule.working_days) > 0:
        return weekday not in schedule.working_days

    # Fallback to WorkingDay model config or standard Mon-Fri (5=Sat, 6=Sun)
    from apps.hrms.models import WorkingDay
    wd = WorkingDay.objects.filter(client_id=client_id, weekday=weekday, deleted_at__isnull=True).first()
    if wd is not None:
        return not wd.is_working

    return weekday in (5, 6)


# ---------------------------------------------------------------------------
# Attendance (api.md §11.2)
# ---------------------------------------------------------------------------
def derive_attendance(client_id, *, check_in, check_out, submitted_status=None, employee=None, work_date=None):
    """``hours`` plus the Late / Half Day determination, applied server-side.

    An explicitly-submitted non-working status (Absent, On Leave, Holiday,
    Week Off, WFH) is respected as-is; the policy only decides between Present,
    Late and Half Day, which is the judgement the frontend was making badly.
    """
    if submitted_status in ("Absent", "On Leave", "Holiday", "Week Off"):
        return submitted_status, None

    schedule = get_employee_schedule(client_id, employee, work_date=work_date)
    policy = flexibility_policy(client_id)

    if check_in is None:
        if submitted_status:
            return submitted_status, None
        if work_date:
            if is_employee_holiday(client_id, work_date, employee):
                return "Holiday", None
            if is_employee_weekly_off(client_id, work_date, employee, schedule=schedule):
                return "Week Off", None
        return "Absent", None

    hours = None
    if check_out is not None:
        delta = check_out - check_in
        hours = round(Decimal(delta.total_seconds()) / Decimal(3600), 2)
        if hours < 0:
            raise ValidationFailed(
                "Check-out cannot be before check-in.",
                field_errors={"checkOut": ["Must be after check-in."]},
            )

    if submitted_status in ("WFH", "On Leave", "Absent", "Half Day"):
        return submitted_status, hours

    shift_start = (schedule.shift_start if schedule else None) or _parse_time(policy.get("shiftStart"), time(9, 30))
    grace = int(policy.get("graceMinutes") or 0)
    half_day_threshold = Decimal(str(policy.get("halfDayThresholdHours") or 4))
    full_day_hours = Decimal(str(schedule.working_hours if schedule else (policy.get("fullDayHours") or 8)))

    if hours is not None:
        if hours < half_day_threshold:
            return "Half Day", hours
        if hours < full_day_hours:
            pass

    local_check_in = timezone.localtime(check_in) if timezone.is_aware(check_in) else check_in
    if policy.get("lateAfter"):
        late_cutoff = _parse_time(policy.get("lateAfter"), shift_start)
        latest_on_time = datetime.combine(local_check_in.date(), late_cutoff)
    else:
        latest_on_time = (
            datetime.combine(local_check_in.date(), shift_start) + timedelta(minutes=grace)
        )
    if local_check_in.replace(tzinfo=None) > latest_on_time:
        return "Late", hours

    return "Present", hours


@transaction.atomic
def mark_attendance(
    *, client, employee, work_date, check_in=None, check_out=None, status=None,
    remark=None, source="manual", user=None, leave_request=None,
):
    client_id = getattr(client, "id", client)
    emp = employee if hasattr(employee, "status") else Employee.objects.filter(pk=employee).first()
    if emp and emp.status in ("Terminated", "Resigned") and not leave_request:
        from apps.hrms.models import Resignation, Termination
        exit_rec = (
            Resignation.objects.filter(client_id=client_id, employee=emp, status__in=["Completed", "Relieved & Completed", "Approved", "Exit Clearance Completed"]).first() or
            Termination.objects.filter(client_id=client_id, employee=emp, status__in=["Completed", "Settled", "Approved", "In Exit Clearance"]).first()
        )
        last_day = getattr(exit_rec, "last_working_day", None) if exit_rec else None
        if not last_day or work_date > last_day:
            raise Conflict(
                f"Cannot mark attendance for {emp.name}: employee is {emp.status.lower()} (effective exit date: {last_day or 'prior to today'}).",
                code="EMPLOYEE_EXITED",
            )

    if source == "regularization" and status:
        derived_status = status
        hours = None
        if check_in and check_out:
            delta = check_out - check_in
            hours = round(Decimal(delta.total_seconds()) / Decimal(3600), 2)
    else:
        derived_status, hours = derive_attendance(
            client.id if hasattr(client, "id") else client,
            check_in=check_in,
            check_out=check_out,
            submitted_status=status,
            employee=employee,
            work_date=work_date,
        )

    existing = Attendance.objects.select_for_update().filter(
        client_id=client_id, employee=employee, work_date=work_date
    ).first()

    before = None
    if existing is not None:
        before = {
            "status": existing.status,
            "checkIn": existing.check_in,
            "checkOut": existing.check_out,
        }
        existing.check_in = check_in
        existing.check_out = check_out
        existing.hours = hours
        existing.status = derived_status
        if source == "regularization" and derived_status == "Present":
            existing.early_leaving_minutes = 0
        existing.remark = remark
        existing.source = source
        if leave_request is not None:
            existing.leave_request = leave_request
        existing.updated_by = user if getattr(user, "is_authenticated", False) else None
        existing.save()
        row = existing
    else:
        row = Attendance.objects.create(
            client_id=client_id,
            employee=employee,
            work_date=work_date,
            check_in=check_in,
            check_out=check_out,
            hours=hours,
            status=derived_status,
            early_leaving_minutes=0 if (source == "regularization" and derived_status == "Present") else 0,
            remark=remark,
            source=source,
            leave_request=leave_request,
            created_by=user if getattr(user, "is_authenticated", False) else None,
        )

    # db.md §11.2 -- manual edits write an audit row; that is what
    # GET /hrms/attendance/audit/ reads.
    if source in ("manual", "regularization") and getattr(user, "is_authenticated", False):
        record_audit(
            client=client_id,
            actor=user,
            action="attendance_edit" if existing is not None else "attendance_mark",
            entity_type="Attendance",
            entity_id=row.id,
            entity_label=f"{employee.employee_code} {work_date}",
            description=f"Attendance set to {derived_status}",
            before=before,
            after={"status": derived_status, "checkIn": check_in, "checkOut": check_out},
        )
    return row


def format_duration_hm(seconds):
    if not seconds or seconds < 0:
        return "00h 00m"
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    return f"{hours:02d}h {minutes:02d}m"


def format_time_ampm(dt):
    if not dt:
        return None
    local_dt = timezone.localtime(dt) if timezone.is_aware(dt) else dt
    return local_dt.strftime("%I:%M %p")


def calculate_punch_metrics(client_id, employee, work_date, punches=None, now=None):
    """Calculate working hours, pairs, late minutes, overtime from punch records."""
    if punches is None:
        punches = list(
            AttendancePunch.objects.filter(
                client_id=client_id,
                employee=employee,
                work_date=work_date,
                deleted_at__isnull=True,
            ).order_by("punch_time")
        )

    if now is None:
        now = timezone.now()

    pairs = []
    pairs_display = []
    current_in = None
    total_completed_seconds = 0

    for p in punches:
        if p.punch_type == "IN":
            current_in = p
        elif p.punch_type == "OUT" and current_in:
            delta = (p.punch_time - current_in.punch_time).total_seconds()
            if delta > 0:
                total_completed_seconds += delta
                pairs.append((current_in, p, int(delta)))
                pairs_display.append({
                    "inTime": format_time_ampm(current_in.punch_time),
                    "outTime": format_time_ampm(p.punch_time),
                    "duration": format_duration_hm(delta),
                    "durationSeconds": int(delta),
                })
            current_in = None

    is_punched_in = bool(punches and punches[-1].punch_type == "IN")
    active_seconds = 0
    if is_punched_in and punches:
        active_seconds = max(0, int((now - punches[-1].punch_time).total_seconds()))

    total_working_seconds = int(total_completed_seconds + active_seconds)
    working_hours = round(Decimal(total_working_seconds) / Decimal(3600), 2)
    completed_hours = round(Decimal(total_completed_seconds) / Decimal(3600), 2)

    first_punch = punches[0] if punches else None
    last_punch = punches[-1] if punches else None
    first_in = next((p for p in punches if p.punch_type == "IN"), None)
    last_out = next((p for p in reversed(punches) if p.punch_type == "OUT"), None)

    schedule = get_employee_schedule(client_id, employee, work_date=work_date)
    policy = flexibility_policy(client_id)
    shift_start = (schedule.shift_start if schedule else None) or _parse_time(policy.get("shiftStart"), time(9, 30))
    shift_end = (schedule.shift_end if schedule else None) or _parse_time(policy.get("shiftEnd"), time(18, 30))
    grace = int(policy.get("graceMinutes") or 0)
    half_day_threshold = Decimal(str(policy.get("halfDayThresholdHours") or 4))
    full_day_hours = Decimal(str(schedule.working_hours if schedule else (policy.get("fullDayHours") or 8)))
    full_day_seconds = int(full_day_hours * 3600)

    late_minutes = 0
    if first_in:
        local_first_in = (
            timezone.localtime(first_in.punch_time)
            if timezone.is_aware(first_in.punch_time)
            else first_in.punch_time
        )
        shift_start_dt = datetime.combine(local_first_in.date(), shift_start)
        if policy.get("lateAfter"):
            late_cutoff = _parse_time(policy.get("lateAfter"), shift_start)
            grace_dt = datetime.combine(local_first_in.date(), late_cutoff)
        else:
            grace_dt = shift_start_dt + timedelta(minutes=grace)
        if local_first_in.replace(tzinfo=None) > grace_dt:
            diff = (local_first_in.replace(tzinfo=None) - shift_start_dt).total_seconds()
            late_minutes = max(0, int(diff // 60))

    early_leaving_minutes = 0
    if last_out:
        local_last_out = (
            timezone.localtime(last_out.punch_time)
            if timezone.is_aware(last_out.punch_time)
            else last_out.punch_time
        )
        shift_end_dt = datetime.combine(local_last_out.date(), shift_end)
        if local_last_out.replace(tzinfo=None) < shift_end_dt:
            diff = (shift_end_dt - local_last_out.replace(tzinfo=None)).total_seconds()
            early_leaving_minutes = max(0, int(diff // 60))

    is_early_out = bool(last_out and (early_leaving_minutes > 0 or working_hours < full_day_hours))

    overtime_threshold_hours = Decimal(str(policy.get("overtimeStartsAfterHours") or full_day_hours))
    overtime_threshold_seconds = int(overtime_threshold_hours * 3600)
    overtime_seconds = max(0, int(total_working_seconds - overtime_threshold_seconds))
    overtime_hours = round(Decimal(overtime_seconds) / Decimal(3600), 2)

    if not punches:
        if is_employee_holiday(client_id, work_date, employee):
            status = "Holiday"
        elif is_employee_weekly_off(client_id, work_date, employee, schedule=schedule):
            status = "Week Off"
        else:
            status = "Absent"
    elif is_punched_in:
        status = "Late" if late_minutes > 0 else "Present"
    elif working_hours == 0:
        status = "Absent"
    elif working_hours < half_day_threshold:
        status = "Half Day"
    elif late_minutes > 0:
        status = "Late"
    else:
        status = "Present"

    return {
        "punches": punches,
        "is_punched_in": is_punched_in,
        "total_working_seconds": total_working_seconds,
        "total_completed_seconds": int(total_completed_seconds),
        "formatted_working_time": format_duration_hm(total_working_seconds),
        "working_hours": working_hours,
        "completed_hours": completed_hours,
        "first_punch": first_punch,
        "last_punch": last_punch,
        "first_in": first_in,
        "last_out": last_out,
        "late_minutes": late_minutes,
        "early_leaving_minutes": early_leaving_minutes,
        "is_early_out": is_early_out,
        "shift_start": policy.get("shiftStart", "09:30"),
        "shift_end": policy.get("shiftEnd", "18:30"),
        "full_day_hours": float(full_day_hours),
        "half_day_threshold_hours": float(half_day_threshold),
        "overtime_seconds": overtime_seconds,
        "overtime_hours": overtime_hours,
        "status": status,
        "pairs_display": pairs_display,
    }


def get_today_punch_status(client, employee, work_date=None):
    """Retrieve punch status, punch timeline, working duration, and metrics."""
    client_id = getattr(client, "id", client)
    if work_date is None:
        work_date = timezone.localdate()

    punches = list(
        AttendancePunch.objects.filter(
            client_id=client_id,
            employee=employee,
            work_date=work_date,
            deleted_at__isnull=True,
        ).order_by("punch_time")
    )
    metrics = calculate_punch_metrics(client_id, employee, work_date, punches=punches)

    if metrics["is_punched_in"]:
        status_label = "Punched In"
    elif punches:
        status_label = "Punched Out"
    else:
        status_label = "Not Punched In"

    first_punch_time = format_time_ampm(metrics["first_punch"].punch_time) if metrics["first_punch"] else None
    last_punch_time = format_time_ampm(metrics["last_punch"].punch_time) if metrics["last_punch"] else None

    att_row = Attendance.objects.filter(
        client_id=client_id, employee=employee, work_date=work_date
    ).first()

    has_out_punch = any(p.punch_type == "OUT" for p in punches)
    day_completed = bool(has_out_punch and not metrics["is_punched_in"])
    can_punch_in = bool(not metrics["is_punched_in"] and not day_completed)
    can_punch_out = bool(metrics["is_punched_in"])

    from apps.hrms.models import AttendanceRegularization
    has_pending_regularization = AttendanceRegularization.objects.filter(
        client_id=client_id, employee=employee, work_date=work_date, status="Pending"
    ).exists()

    early_mins = att_row.early_leaving_minutes if att_row else metrics["early_leaving_minutes"]

    return {
        "has_employee": True,
        "employee": {
            "id": str(employee.id),
            "name": employee.name,
            "employeeCode": employee.employee_code,
            "department": getattr(employee.department, "name", "") if employee.department else "",
            "shift": employee.shift or "General",
        },
        "date": work_date.isoformat(),
        "status": status_label,
        "is_punched_in": metrics["is_punched_in"],
        "day_completed": day_completed,
        "can_punch_in": can_punch_in,
        "can_punch_out": can_punch_out,
        "is_early_out": metrics["is_early_out"],
        "early_leaving_minutes": early_mins,
        "early_minutes": early_mins,
        "shift_start": metrics["shift_start"],
        "shift_end": metrics["shift_end"],
        "full_day_hours": metrics["full_day_hours"],
        "half_day_threshold_hours": metrics["half_day_threshold_hours"],
        "has_pending_regularization": has_pending_regularization,
        "attendance_status": att_row.status if att_row else metrics["status"],
        "first_punch": first_punch_time,
        "last_punch": last_punch_time,
        "first_punch_iso": metrics["first_punch"].punch_time.isoformat() if metrics["first_punch"] else None,
        "last_punch_iso": metrics["last_punch"].punch_time.isoformat() if metrics["last_punch"] else None,
        "working_seconds": metrics["total_working_seconds"],
        "formatted_working_time": metrics["formatted_working_time"],
        "working_hours": float(metrics["working_hours"]),
        "late_minutes": metrics["late_minutes"],
        "overtime_seconds": metrics["overtime_seconds"],
        "overtime_hours": float(metrics["overtime_hours"]),
        "punches": [
            {
                "id": str(p.id),
                "punchType": p.punch_type,
                "punchTime": p.punch_time.isoformat(),
                "timeDisplay": format_time_ampm(p.punch_time),
                "source": p.source,
                "remark": p.remark,
            }
            for p in punches
        ],
        "pairs": metrics["pairs_display"],
    }


@transaction.atomic
def record_punch(
    *, client, employee, punch_type, punch_time=None, source="web", remark=None, user=None,
    early_reason=None, request_regularization=False
):
    """Record a Punch In or Punch Out event with validation, and update Attendance."""
    client_id = getattr(client, "id", client)
    punch_type = str(punch_type).upper().strip()
    if punch_type not in ("IN", "OUT"):
        raise ValidationFailed(
            f"Invalid punch type: {punch_type}. Must be 'IN' or 'OUT'.",
            field_errors={"punch_type": ["Must be IN or OUT."]},
        )

    if punch_time is None:
        punch_time = timezone.now()
    elif not timezone.is_aware(punch_time):
        punch_time = timezone.make_aware(punch_time)

    work_date = timezone.localdate(punch_time)

    # Lock existing punches for this employee today
    existing_punches = list(
        AttendancePunch.objects.select_for_update()
        .filter(client_id=client_id, employee=employee, work_date=work_date)
        .order_by("punch_time")
    )
    last_punch = existing_punches[-1] if existing_punches else None

    # Single punch per day rule: If already punched out today, employee cannot punch in again
    has_out_punch = any(p.punch_type == "OUT" for p in existing_punches)
    if punch_type == "IN" and has_out_punch:
        raise ValidationFailed(
            "You have already completed your punch in and punch out for today.",
            code="DAY_PUNCH_LIMIT_REACHED",
        )

    # Duplicate / validation checks
    if last_punch:
        time_diff = abs((punch_time - last_punch.punch_time).total_seconds())
        if last_punch.punch_type == punch_type:
            # If within 5 seconds of identical punch type, treat as duplicate click
            if time_diff < 5:
                return last_punch, get_today_punch_status(client_id, employee, work_date=work_date)
            if punch_type == "IN":
                raise ValidationFailed(
                    "You are already punched in. Please punch out first.",
                    code="ALREADY_PUNCHED_IN",
                )
            else:
                raise ValidationFailed(
                    "You are already punched out. Please punch in first.",
                    code="ALREADY_PUNCHED_OUT",
                )
    elif punch_type == "OUT":
        raise ValidationFailed(
            "Cannot punch out without punching in first.",
            code="CANNOT_PUNCH_OUT",
        )

    punch_remark = remark
    if early_reason:
        punch_remark = f"{remark} | Early: {early_reason}" if remark else f"Early Punch Out: {early_reason}"

    punch = AttendancePunch.objects.create(
        client_id=client_id,
        employee=employee,
        work_date=work_date,
        punch_type=punch_type,
        punch_time=punch_time,
        source=source,
        remark=punch_remark,
        created_by=user if getattr(user, "is_authenticated", False) else None,
    )

    all_punches = existing_punches + [punch]
    metrics = calculate_punch_metrics(
        client_id, employee, work_date, punches=all_punches, now=punch_time
    )

    first_in = metrics["first_in"].punch_time if metrics["first_in"] else None
    last_out = metrics["last_out"].punch_time if metrics["last_out"] else None
    first_punch_time = metrics["first_punch"].punch_time if metrics["first_punch"] else None
    last_punch_time = metrics["last_punch"].punch_time if metrics["last_punch"] else None

    check_in_time = first_in or first_punch_time
    check_out_time = None if metrics["is_punched_in"] else last_out

    # Upsert Attendance row
    att_row = Attendance.objects.select_for_update().filter(
        client_id=client_id, employee=employee, work_date=work_date
    ).first()

    status_to_use = metrics["status"]
    if att_row and att_row.status in ("On Leave", "Holiday", "Week Off"):
        status_to_use = att_row.status

    early_leaving_mins = metrics["early_leaving_minutes"] if punch_type == "OUT" else (att_row.early_leaving_minutes if att_row else 0)

    if att_row is not None:
        att_row.first_punch = first_punch_time
        att_row.last_punch = last_punch_time
        att_row.check_in = check_in_time
        att_row.check_out = check_out_time
        att_row.hours = metrics["working_hours"]
        att_row.working_hours = metrics["working_hours"]
        att_row.late_minutes = metrics["late_minutes"]
        att_row.early_leaving_minutes = early_leaving_mins
        att_row.overtime_hours = metrics["overtime_hours"]
        att_row.status = status_to_use
        att_row.source = "punch"
        if early_reason:
            att_row.remark = (
                f"{att_row.remark} | Early: {early_reason}"
                if att_row.remark and "Early:" not in att_row.remark
                else (att_row.remark or f"Early Punch Out: {early_reason}")
            )
        att_row.updated_by = user if getattr(user, "is_authenticated", False) else None
        att_row.save()
    else:
        att_row = Attendance.objects.create(
            client_id=client_id,
            employee=employee,
            work_date=work_date,
            first_punch=first_punch_time,
            last_punch=last_punch_time,
            check_in=check_in_time,
            check_out=check_out_time,
            hours=metrics["working_hours"],
            working_hours=metrics["working_hours"],
            late_minutes=metrics["late_minutes"],
            early_leaving_minutes=early_leaving_mins,
            overtime_hours=metrics["overtime_hours"],
            status=status_to_use,
            source="punch",
            remark=f"Early Punch Out: {early_reason}" if early_reason else None,
            created_by=user if getattr(user, "is_authenticated", False) else None,
        )

    punch.attendance = att_row
    punch.save(update_fields=["attendance"])

    # Connect with Regularization if requested or early punch out with reason
    if punch_type == "OUT" and (request_regularization or (early_reason and metrics["is_early_out"])):
        from apps.hrms.models import AttendanceRegularization
        AttendanceRegularization.objects.update_or_create(
            client_id=client_id,
            employee=employee,
            work_date=work_date,
            status="Pending",
            defaults={
                "requested_check_in": check_in_time,
                "requested_check_out": punch_time,
                "requested_status": "Present",
                "reason": early_reason or remark or f"Early punch out ({early_leaving_mins} min early)",
                "created_by": user if getattr(user, "is_authenticated", False) else None,
            },
        )

    return punch, get_today_punch_status(client_id, employee, work_date=work_date)


@transaction.atomic
def correct_punch(*, client, employee, work_date, punch_type, punch_time, reason, user=None):
    """HR/Admin correction for missing punches (with audit trail)."""
    client_id = getattr(client, "id", client)
    punch = AttendancePunch.objects.create(
        client_id=client_id,
        employee=employee,
        work_date=work_date,
        punch_type=punch_type,
        punch_time=punch_time,
        source="manual",
        remark=f"Correction: {reason}",
        created_by=user if getattr(user, "is_authenticated", False) else None,
    )

    all_punches = list(
        AttendancePunch.objects.select_for_update()
        .filter(client_id=client_id, employee=employee, work_date=work_date)
        .order_by("punch_time")
    )
    metrics = calculate_punch_metrics(client_id, employee, work_date, punches=all_punches)

    first_in = metrics["first_in"].punch_time if metrics["first_in"] else None
    last_out = metrics["last_out"].punch_time if metrics["last_out"] else None
    first_punch_time = metrics["first_punch"].punch_time if metrics["first_punch"] else None
    last_punch_time = metrics["last_punch"].punch_time if metrics["last_punch"] else None

    check_in_time = first_in or first_punch_time
    check_out_time = None if metrics["is_punched_in"] else last_out

    att_row = Attendance.objects.select_for_update().filter(
        client_id=client_id, employee=employee, work_date=work_date
    ).first()

    if att_row is not None:
        att_row.first_punch = first_punch_time
        att_row.last_punch = last_punch_time
        att_row.check_in = check_in_time
        att_row.check_out = check_out_time
        att_row.hours = metrics["working_hours"]
        att_row.working_hours = metrics["working_hours"]
        att_row.late_minutes = metrics["late_minutes"]
        att_row.overtime_hours = metrics["overtime_hours"]
        att_row.status = metrics["status"]
        att_row.source = "regularization"
        att_row.updated_by = user if getattr(user, "is_authenticated", False) else None
        att_row.save()
    else:
        att_row = Attendance.objects.create(
            client_id=client_id,
            employee=employee,
            work_date=work_date,
            first_punch=first_punch_time,
            last_punch=last_punch_time,
            check_in=check_in_time,
            check_out=check_out_time,
            hours=metrics["working_hours"],
            working_hours=metrics["working_hours"],
            late_minutes=metrics["late_minutes"],
            overtime_hours=metrics["overtime_hours"],
            status=metrics["status"],
            source="regularization",
            created_by=user if getattr(user, "is_authenticated", False) else None,
        )

    punch.attendance = att_row
    punch.save(update_fields=["attendance"])

    if getattr(user, "is_authenticated", False):
        record_audit(
            client=client_id,
            actor=user,
            action="attendance_correction",
            entity_type="AttendancePunch",
            entity_id=punch.id,
            entity_label=f"{employee.employee_code} {work_date} {punch_type}",
            description=f"Punch correction added: {punch_type} at {punch_time}. Reason: {reason}",
            after={"punch_type": punch_type, "punch_time": punch_time.isoformat(), "reason": reason},
        )

    return punch, get_today_punch_status(client_id, employee, work_date=work_date)


def working_days_in(client_id, start, end, location_id=None, department_id=None, employee=None):
    """Working dates in a range, honouring employee/department/tenant work-schedule and published holidays."""
    schedule = get_employee_schedule(client_id, employee) if employee else None
    if not schedule and department_id:
        from apps.hrms.models import WorkSchedule
        schedule = WorkSchedule.objects.filter(
            client_id=client_id, department_id=department_id, status="Active", deleted_at__isnull=True
        ).first()
    if not schedule:
        from apps.hrms.models import WorkSchedule
        schedule = WorkSchedule.objects.filter(
            client_id=client_id, is_default=True, status="Active", deleted_at__isnull=True
        ).first()

    weekly_offs = set(schedule.weekly_offs) if (schedule and schedule.weekly_offs) else None
    working_days_list = set(schedule.working_days) if (schedule and schedule.working_days) else None

    config = {}
    if weekly_offs is None and working_days_list is None:
        config = {
            row.weekday: row.is_working
            for row in WorkingDay.objects.filter(client_id=client_id, deleted_at__isnull=True)
        }

    holiday_qs = Holiday.objects.filter(
        client_id=client_id, deleted_at__isnull=True, status="Published", date__gte=start, date__lte=end
    )
    loc_id = location_id or (getattr(employee, "location_id", None) if employee else None)
    dep_id = department_id or (getattr(employee, "department_id", None) if employee else None)

    applicable_holidays = set()
    for h in holiday_qs:
        if h.location_id is None and h.department_id is None:
            applicable_holidays.add(h.date)
        elif loc_id and h.location_id == loc_id:
            applicable_holidays.add(h.date)
        elif dep_id and h.department_id == dep_id:
            applicable_holidays.add(h.date)

    days = []
    current = start
    while current <= end:
        weekday = current.weekday()
        if weekly_offs is not None:
            is_working = (weekday not in weekly_offs)
        elif working_days_list is not None:
            is_working = (weekday in working_days_list)
        else:
            is_working = config.get(weekday, weekday < 5)

        if is_working and current not in applicable_holidays:
            days.append(current)
        current += timedelta(days=1)
    return days


def attendance_summary(client_id, *, month=None, employee_id=None, department_id=None):
    """``GET /hrms/attendance/summary/`` -- monthly per-employee counts."""
    queryset = Attendance.objects.filter(client_id=client_id, deleted_at__isnull=True)
    if month:
        queryset = queryset.filter(work_date__year=month.year, work_date__month=month.month)
    if employee_id:
        queryset = queryset.filter(employee_id=employee_id)
    if department_id:
        queryset = queryset.filter(employee__department_id=department_id)

    rows = (
        queryset.values("employee_id", "employee__employee_code", "employee__name")
        .annotate(
            present=Count("id", filter=Q(status="Present")),
            absent=Count("id", filter=Q(status="Absent")),
            late=Count("id", filter=Q(status="Late")),
            halfDay=Count("id", filter=Q(status="Half Day")),
            wfh=Count("id", filter=Q(status="WFH")),
            onLeave=Count("id", filter=Q(status="On Leave")),
            holiday=Count("id", filter=Q(status="Holiday")),
            weekOff=Count("id", filter=Q(status="Week Off")),
            totalHours=Sum("hours"),
        )
        .order_by("employee__name")
    )
    return [
        {
            "employeeId": str(row["employee_id"]),
            "employeeCode": row["employee__employee_code"],
            "name": row["employee__name"],
            "present": row["present"],
            "absent": row["absent"],
            "late": row["late"],
            "halfDay": row["halfDay"],
            "wfh": row["wfh"],
            "onLeave": row["onLeave"],
            "holiday": row["holiday"],
            "weekOff": row["weekOff"],
            "totalHours": row["totalHours"] or 0,
            # Late and WFH days still count as attended for payroll.
            "attendedDays": row["present"] + row["late"] + row["wfh"]
            + Decimal("0.5") * row["halfDay"],
        }
        for row in rows
    ]


# ---------------------------------------------------------------------------
# Leave (api.md §11.3)
# ---------------------------------------------------------------------------
def assert_no_overlap(leave_request, statuses=("Pending Review", "Delegate Confirmed", "Approved")):
    """db.md §11.3 -- overlapping live requests for the same employee are rejected."""
    clash = LeaveRequest.objects.filter(
        client_id=leave_request.client_id,
        employee_id=leave_request.employee_id,
        status__in=statuses,
        from_date__lte=leave_request.to_date,
        to_date__gte=leave_request.from_date,
        deleted_at__isnull=True,
    ).exclude(pk=leave_request.pk)
    if clash.exists():
        existing = clash.first()
        raise BusinessRuleViolation(
            "This overlaps an existing leave request "
            f"({existing.from_date} to {existing.to_date}).",
            code="LEAVE_OVERLAP",
            payload={"conflictingRequestId": str(existing.id)},
        )


def leave_balance_for(client_id, employee_id, leave_type_id, year=None):
    year = year or timezone.localdate().year
    balance, _ = LeaveBalance.objects.get_or_create(
        client_id=client_id,
        employee_id=employee_id,
        leave_type_id=leave_type_id,
        period_year=year,
        defaults={"entitlement": ZERO, "carried_forward": ZERO, "used": ZERO},
    )
    return balance


@transaction.atomic
def approve_leave(leave_request, *, user=None, remark=None):
    """One transaction: flip to Approved, post attendance, decrement balance.

    api.md §11.3 requires all three together -- a leave that is approved but
    not posted to attendance would silently become an absence at payroll time.
    """
    leave_request = LeaveRequest.objects.select_for_update().get(pk=leave_request.pk)
    if leave_request.status == "Approved":
        raise Conflict("This leave is already approved.", code=Codes.ALREADY_DONE)
    if leave_request.status in ("Rejected", "Cancelled"):
        raise Conflict(
            f"This leave request is {leave_request.status.lower()}.",
            code=Codes.BAD_TARGET,
        )

    # When approving, only an already Approved leave request constitutes an overlap error.
    # Another unapproved / pending request should not block HR from approving this one.
    assert_no_overlap(leave_request, statuses=("Approved",))

    # Auto-cancel any other pending / unapproved requests that overlapped this now-approved range
    LeaveRequest.objects.filter(
        client_id=leave_request.client_id,
        employee_id=leave_request.employee_id,
        status__in=["Pending Review", "Delegate Confirmed"],
        from_date__lte=leave_request.to_date,
        to_date__gte=leave_request.from_date,
        deleted_at__isnull=True,
    ).exclude(pk=leave_request.pk).update(status="Cancelled", remark="Superseded by approved leave")

    balance = leave_balance_for(
        leave_request.client_id, leave_request.employee_id, leave_request.leave_type_id,
        leave_request.from_date.year,
    )
    available = D(balance.entitlement) + D(balance.carried_forward) - D(balance.used) - D(balance.encashed)
    if D(leave_request.days) > available:
        raise BusinessRuleViolation(
            f"{leave_request.employee.name} has {available} day(s) of "
            f"{leave_request.leave_type.name} left but requested {leave_request.days}.",
            code="INSUFFICIENT_LEAVE_BALANCE",
            payload={"available": str(available), "requested": str(leave_request.days)},
        )

    leave_request.status = "Approved"
    leave_request.approver = user if getattr(user, "is_authenticated", False) else None
    leave_request.decided_at = timezone.now()
    leave_request.remark = remark
    leave_request.save()

    # Post attendance for every working day in range.
    for day in working_days_in(
        leave_request.client_id, leave_request.from_date, leave_request.to_date
    ):
        mark_attendance(
            client=leave_request.client,
            employee=leave_request.employee,
            work_date=day,
            status="On Leave",
            source="leave",
            leave_request=leave_request,
            remark=leave_request.reason,
            user=user,
        )

    balance.used = D(balance.used) + D(leave_request.days)
    balance.save(update_fields=["used", "updated_at"])

    # api.md §11.8 -- leave-derived calendar events are generated, not entered.
    CalendarEvent.objects.create(
        client_id=leave_request.client_id,
        title=f"{leave_request.employee.name} on leave",
        type="Leave",
        starts_at=timezone.make_aware(
            datetime.combine(leave_request.from_date, time(0, 0))
        ),
        ends_at=timezone.make_aware(datetime.combine(leave_request.to_date, time(23, 59))),
        all_day=True,
        department=leave_request.employee.department,
        source_type="LeaveRequest",
        source_id=leave_request.id,
        created_by=user if getattr(user, "is_authenticated", False) else None,
    )

    # Connect delegate to PMS DelegatedTask & CRM Task
    if leave_request.delegate_employee_id:
        try:
            import logging
            from apps.pms.models import DelegatedTask
            from apps.accounts.models import User
            from apps.core.numbering import allocate_number

            delegate_user = User.objects.filter(
                client_id=leave_request.client_id,
                employee_id=leave_request.delegate_employee_id,
                deleted_at__isnull=True,
            ).first()
            if not delegate_user and leave_request.delegate_employee:
                delegate_user = User.objects.filter(
                    client_id=leave_request.client_id,
                    name__iexact=leave_request.delegate_employee.name,
                    deleted_at__isnull=True,
                ).first()

            creator_user = user if getattr(user, "is_authenticated", False) else None
            if not creator_user:
                creator_user = User.objects.filter(
                    client_id=leave_request.client_id,
                    employee_id=leave_request.employee_id,
                    deleted_at__isnull=True,
                ).first() or delegate_user

            if delegate_user and creator_user:
                existing_pms = DelegatedTask.objects.filter(
                    client_id=leave_request.client_id,
                    assigned_to=delegate_user,
                    due_date=leave_request.to_date,
                    title__contains=leave_request.employee.name,
                    deleted_at__isnull=True,
                ).first()
                if not existing_pms:
                    DelegatedTask.objects.create(
                        client_id=leave_request.client_id,
                        task_number=allocate_number(leave_request.client, "DEL"),
                        title=f"Leave Coverage: {leave_request.employee.name} ({leave_request.from_date} to {leave_request.to_date})",
                        description=f"Coverage handover delegated during leave: {leave_request.reason or 'General coverage'}",
                        delegated_by=creator_user,
                        assigned_to=delegate_user,
                        priority="High",
                        due_date=leave_request.to_date,
                        status="Pending",
                    )
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning("Failed to create PMS delegated task: %s", e)

        try:
            from apps.crm.models import Task as CrmTask, Lead, Deal
            first_lead = Lead.objects.filter(client_id=leave_request.client_id, deleted_at__isnull=True).first()
            first_deal = Deal.objects.filter(client_id=leave_request.client_id, deleted_at__isnull=True).first()
            parent_lead = first_lead
            parent_deal = first_deal if not parent_lead else None

            if delegate_user:
                existing_crm = CrmTask.objects.filter(
                    client_id=leave_request.client_id,
                    assignee=delegate_user,
                    due_date=leave_request.to_date,
                    title__contains=leave_request.employee.name,
                    deleted_at__isnull=True,
                ).first()
                if not existing_crm:
                    CrmTask.objects.create(
                        client_id=leave_request.client_id,
                        lead=parent_lead,
                        deal=parent_deal,
                        title=f"Leave Coverage: Support {leave_request.employee.name} Leads/Deals",
                        description=f"Coverage delegated from {leave_request.from_date} to {leave_request.to_date}. Handover: {leave_request.reason or 'General client support'}",
                        assignee=delegate_user,
                        due_date=leave_request.to_date,
                        priority="High",
                        status="Open",
                        source="Leave Delegation",
                    )
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning("Failed to create CRM coverage task: %s", e)

    # Notifications go to logins: the employee's linked user, if they have one.
    # (This used to pass the employee id, which is not a user -- the insert
    # failed its foreign key at commit, so approving any leave errored.)
    from apps.accounts.models import User

    recipient_ids = list(
        User.objects.filter(
            employee_id=leave_request.employee_id, deleted_at__isnull=True
        ).values_list("id", flat=True)
    )
    if recipient_ids and leave_request.employee.status == "Active" and D(leave_request.days) >= 1:
        notify(
            client=leave_request.client_id,
            recipients=recipient_ids,
            type="hrms.leave_approved",
            category="hrms",
            title="Your leave was approved",
            body=f"{leave_request.from_date} to {leave_request.to_date}.",
            entity_type="LeaveRequest",
            entity_id=leave_request.id,
            actor=user,
        )
    return leave_request


@transaction.atomic
def cancel_leave(leave_request, *, user=None, reason=None):
    """Withdraw a leave, removing exactly the rows it generated."""
    leave_request = LeaveRequest.objects.select_for_update().get(pk=leave_request.pk)
    if leave_request.status == "Cancelled":
        raise Conflict("This leave is already cancelled.", code=Codes.ALREADY_CANCELLED)

    was_approved = leave_request.status == "Approved"
    leave_request.status = "Cancelled"
    leave_request.remark = reason or leave_request.remark
    leave_request.save(update_fields=["status", "remark", "updated_at"])

    if was_approved:
        Attendance.objects.filter(
            client_id=leave_request.client_id, leave_request=leave_request
        ).update(deleted_at=timezone.now())

        balance = leave_balance_for(
            leave_request.client_id, leave_request.employee_id,
            leave_request.leave_type_id, leave_request.from_date.year,
        )
        balance.used = max(D(balance.used) - D(leave_request.days), ZERO)
        balance.save(update_fields=["used", "updated_at"])

        # Source-tagged events make this exact, rather than a fuzzy date match.
        CalendarEvent.objects.filter(
            client_id=leave_request.client_id,
            source_type="LeaveRequest",
            source_id=leave_request.id,
        ).update(deleted_at=timezone.now())

    return leave_request


# ---------------------------------------------------------------------------
# Payroll (api.md §11.4)
# ---------------------------------------------------------------------------
def compute_salary(
    *, standard_salary, total_days=None, attended_days=0, paid_leaves=0,
    additional_earnings=0, deductions=0, advance=0, earned_salary_override=None,
):
    """Ported verbatim from ``features/hrms/payroll/salaryCalculations.js``.

        perDaySalary        = standardSalary / totalDays
        absentDays          = totalDays - attendedDays - paidLeaves
        attendanceDeduction = perDaySalary * max(0, absentDays)
        earnedSalary        = standardSalary - attendanceDeduction
        remainingPayable    = earnedSalary + additionalEarnings - deductions - advance

    Approved paid leave does not cause a deduction. An explicit
    ``earnedSalary`` overrides the computed value -- api.md §11.4 asks for that
    escape hatch to be kept, and the caller logs that it was used.
    """
    standard_salary = D(standard_salary)
    total_days = D(total_days) if total_days else DEFAULT_TOTAL_DAYS
    if total_days <= ZERO:
        total_days = DEFAULT_TOTAL_DAYS
    attended_days = D(attended_days)
    paid_leaves = D(paid_leaves)

    per_day = standard_salary / total_days
    absent_days = total_days - attended_days - paid_leaves
    if absent_days < ZERO:
        absent_days = ZERO
    attendance_deduction = round2(per_day * absent_days)

    if earned_salary_override is not None:
        earned_salary = round2(earned_salary_override)
        overridden = True
    else:
        earned_salary = round2(standard_salary - attendance_deduction)
        overridden = False

    net_payable = round2(
        earned_salary + D(additional_earnings) - D(deductions) - D(advance)
    )

    return {
        "perDaySalary": round2(per_day),
        "absentDays": absent_days,
        "attendanceDeduction": attendance_deduction,
        "earnedSalary": earned_salary,
        "earnedSalaryOverridden": overridden,
        "netPayable": net_payable,
    }


def split_structure(structure, earned_salary):
    """Basic / HRA / allowances from the salary structure, if one is attached."""
    earned_salary = D(earned_salary)
    if structure is None:
        return {"basic": earned_salary, "hra": ZERO, "allowances": ZERO, "components": []}

    basic = round2(earned_salary * D(structure.basic_pct or 0) / Decimal("100"))
    hra = round2(earned_salary * D(structure.hra_pct or 0) / Decimal("100"))
    allowances = round2(earned_salary - basic - hra)

    components = []
    for component in structure.components or []:
        name = component.get("name")
        kind = component.get("type") or component.get("kind") or "earning"
        calc = component.get("calc") or "fixed"
        value = D(component.get("value"))
        amount = (
            round2(earned_salary * value / Decimal("100")) if calc == "percent" else round2(value)
        )
        if name:
            components.append({"name": name, "kind": kind, "amount": amount})

    return {"basic": basic, "hra": hra, "allowances": allowances, "components": components}


@transaction.atomic
def process_payroll(*, client, period_month, employee_ids=None, user=None):
    """``POST /hrms/payroll/process/`` -- generate the run."""
    period_month = period_month.replace(day=1)

    run, _ = PayrollRun.objects.get_or_create(
        client=client,
        period_month=period_month,
        defaults={
            "status": "In Progress",
            "processed_by": user if getattr(user, "is_authenticated", False) else None,
            "processed_at": timezone.now(),
            "created_by": user if getattr(user, "is_authenticated", False) else None,
        },
    )
    if run.status in ("Approved", "Paid"):
        raise Conflict(
            f"Payroll for {period_month:%B %Y} is already {run.status.lower()}.",
            code=Codes.ALREADY_DONE,
        )

    employees = Employee.objects.filter(
        client=client, deleted_at__isnull=True, status__in=["Active", "On Leave", "Probation"]
    ).select_related("salary_structure")
    if employee_ids:
        employees = employees.filter(pk__in=employee_ids)

    month_end = _month_end(period_month)
    working = working_days_in(client.id, period_month, month_end)
    total_days = Decimal(len(working)) or DEFAULT_TOTAL_DAYS

    payslips = []
    for employee in employees:
        rows = Attendance.objects.filter(
            client=client,
            employee=employee,
            work_date__gte=period_month,
            work_date__lte=month_end,
            deleted_at__isnull=True,
        ).values("status").annotate(count=Count("id"))
        counts = {row["status"]: row["count"] for row in rows}

        attended = (
            Decimal(counts.get("Present", 0))
            + Decimal(counts.get("Late", 0))
            + Decimal(counts.get("WFH", 0))
            + Decimal("0.5") * Decimal(counts.get("Half Day", 0))
        )
        paid_leaves = Decimal(counts.get("On Leave", 0))

        advance = _pending_advance_instalment(employee)
        result = compute_salary(
            standard_salary=employee.standard_salary,
            total_days=total_days,
            attended_days=attended,
            paid_leaves=paid_leaves,
            advance=advance,
        )
        split = split_structure(employee.salary_structure, result["earnedSalary"])

        # Overtime capping logic (Spec §2.5.3)
        ot_sum = Attendance.objects.filter(
            client=client,
            employee=employee,
            work_date__gte=period_month,
            work_date__lte=month_end,
            deleted_at__isnull=True,
        ).aggregate(tot=Sum("overtime_hours"))["tot"] or Decimal("0.00")

        structure = employee.salary_structure
        max_ot = structure.overtime_monthly_cap_hours if structure and structure.overtime_monthly_cap_hours else None
        capped_ot = min(ot_sum, Decimal(str(max_ot))) if max_ot is not None else ot_sum
        multiplier = Decimal(str(structure.overtime_rate_multiplier)) if structure and structure.overtime_rate_multiplier else Decimal("1.5")
        hourly_rate = (employee.standard_salary / (total_days * Decimal("8.0"))) if total_days else Decimal("0.00")
        ot_pay = round2(capped_ot * hourly_rate * multiplier)

        additional_earnings = ot_pay
        net_payable = round2(result["netPayable"] + additional_earnings)

        payslip, created = Payslip.objects.update_or_create(
            payroll_run=run,
            employee=employee,
            defaults={
                "client": client,
                "period_month": period_month,
                "standard_salary": round2(employee.standard_salary),
                "attended_days": attended,
                "paid_leaves": paid_leaves,
                "total_days": total_days,
                "earned_salary": result["earnedSalary"],
                "earned_salary_overridden": False,
                "basic": split["basic"],
                "hra": split["hra"],
                "allowances": split["allowances"],
                "additional_earnings": additional_earnings,
                "deductions": ZERO,
                "advance_recovery": advance,
                "net_payable": net_payable,
                "status": "In Progress",
            },
        )

        PayslipComponent.objects.filter(payslip=payslip).delete()
        components = list(split["components"] or [])
        if ot_pay > 0:
            comp_name = "Overtime Pay (Capped)" if (max_ot and ot_sum > capped_ot) else "Overtime Pay"
            components.append({"name": comp_name, "kind": "Earning", "amount": ot_pay})

        if components:
            PayslipComponent.objects.bulk_create(
                [
                    PayslipComponent(
                        client=client,
                        payslip=payslip,
                        name=component["name"],
                        kind=component["kind"],
                        amount=component["amount"],
                    )
                    for component in components
                ]
            )
        payslips.append(payslip)

    run.status = "Ready for Review"
    run.save(update_fields=["status", "updated_at"])
    return {"run": run, "payslips": payslips}


def _month_end(first_of_month):
    if first_of_month.month == 12:
        return date(first_of_month.year, 12, 31)
    return date(first_of_month.year, first_of_month.month + 1, 1) - timedelta(days=1)


def _pending_advance_instalment(employee):
    """An advance is recovered from the month it is applied to (api.md §11.4)."""
    advance = SalaryAdvance.objects.filter(
        employee=employee, status="Active", deleted_at__isnull=True
    ).first()
    if advance is None:
        return ZERO
    outstanding = D(advance.amount) - D(advance.recovered_amount)
    if outstanding <= ZERO:
        return ZERO
    instalment = round2(D(advance.amount) / Decimal(max(advance.installments, 1)))
    return min(instalment, round2(outstanding))


@transaction.atomic
def recompute_payslip(payslip, *, earned_salary_override=None, user=None):
    """``PATCH /hrms/payroll/{id}/`` -- adjust before approval."""
    if payslip.status in ("Approved", "Paid"):
        raise Conflict(
            "An approved payslip cannot be adjusted.", code=Codes.ALREADY_DONE
        )

    result = compute_salary(
        standard_salary=payslip.standard_salary,
        total_days=payslip.total_days,
        attended_days=payslip.attended_days,
        paid_leaves=payslip.paid_leaves,
        additional_earnings=payslip.additional_earnings,
        deductions=payslip.deductions,
        advance=payslip.advance_recovery,
        earned_salary_override=earned_salary_override,
    )
    payslip.earned_salary = result["earnedSalary"]
    payslip.earned_salary_overridden = result["earnedSalaryOverridden"]
    payslip.net_payable = result["netPayable"]
    payslip.save(
        update_fields=["earned_salary", "earned_salary_overridden", "net_payable", "updated_at"]
    )

    if result["earnedSalaryOverridden"]:
        # api.md §11.4 -- keep the escape hatch, and log it.
        record_audit(
            client=payslip.client_id,
            actor=user,
            action="payslip_override",
            entity_type="Payslip",
            entity_id=payslip.id,
            entity_label=f"{payslip.employee.employee_code} {payslip.period_month:%b %Y}",
            description=f"Earned salary manually set to {result['earnedSalary']}",
        )
    return payslip


@transaction.atomic
def mark_payslip_paid(payslip, *, payment_date=None, bank_account=None, user=None):
    """Posts the salary journal entry -- payroll is the one HRMS table that
    touches accounts (db.md §11.4)."""
    from apps.accounting import services as ledger

    if payslip.status == "Paid":
        raise Conflict("This payslip is already paid.", code=Codes.ALREADY_DONE)
    if payslip.status != "Approved":
        raise Conflict(
            "Approve the payslip before marking it paid.", code=Codes.NOT_FINALIZED
        )

    payslip.status = "Paid"
    payslip.paid_amount = payslip.net_payable
    payslip.payment_date = payment_date or timezone.localdate()
    payslip.bank_account = bank_account
    payslip.save(
        update_fields=["status", "paid_amount", "payment_date", "bank_account", "updated_at"]
    )

    entry = ledger.post_payroll(payslip, user=user)
    if entry is not None:
        payslip.journal_entry = entry
        payslip.save(update_fields=["journal_entry", "updated_at"])

    _recover_advance(payslip)
    return payslip


def _recover_advance(payslip):
    if D(payslip.advance_recovery) <= ZERO:
        return
    advance = SalaryAdvance.objects.select_for_update().filter(
        employee=payslip.employee, status="Active", deleted_at__isnull=True
    ).first()
    if advance is None:
        return
    advance.recovered_amount = round2(
        D(advance.recovered_amount) + D(payslip.advance_recovery)
    )
    if advance.recovered_amount >= D(advance.amount):
        advance.status = "Recovered"
    advance.save(update_fields=["recovered_amount", "status", "updated_at"])


def payroll_summary(client_id, period_month=None, queryset=None):
    """``GET /hrms/payroll/summary/`` -- gross, deductions, net, headcount.

    ``queryset`` narrows the totals to rows the caller may see.
    """
    if queryset is None:
        queryset = Payslip.objects.filter(client_id=client_id, deleted_at__isnull=True)
    if period_month:
        queryset = queryset.filter(period_month=period_month.replace(day=1))

    rows = queryset.aggregate(
        headcount=Count("id"),
        gross=Sum("earned_salary"),
        additional=Sum("additional_earnings"),
        deductions=Sum("deductions"),
        advances=Sum("advance_recovery"),
        net=Sum("net_payable"),
        paid=Sum("paid_amount"),
    )
    return {
        "headcount": rows["headcount"] or 0,
        "gross": round2(rows["gross"] or 0),
        "additionalEarnings": round2(rows["additional"] or 0),
        "deductions": round2(rows["deductions"] or 0),
        "advanceRecovery": round2(rows["advances"] or 0),
        "net": round2(rows["net"] or 0),
        "paid": round2(rows["paid"] or 0),
    }


# ---------------------------------------------------------------------------
# Derived read-time statuses (db.md §12)
# ---------------------------------------------------------------------------
def document_status(hr_document, today=None):
    """``Valid | Expiring Soon | Expired``, derived from ``valid_until``."""
    today = today or timezone.localdate()
    if hr_document.valid_until is None:
        return "Valid"
    if hr_document.valid_until < today:
        return "Expired"
    if (hr_document.valid_until - today).days <= (hr_document.expiring_soon_days or 30):
        return "Expiring Soon"
    return "Valid"


def acknowledgement_status(acknowledgement, policy, today=None):
    """``Pending | Acknowledged | Overdue`` -- overdue is derived from the
    effective date plus the ack window (api.md §11.8)."""
    if acknowledgement.acknowledged_at is not None:
        return "Acknowledged"
    today = today or timezone.localdate()
    if policy.effective_date is None:
        return "Pending"
    deadline = policy.effective_date + timedelta(days=policy.ack_window_days or 14)
    return "Overdue" if today > deadline else "Pending"


def calendar_time_label(event):
    """api.md §11.8 -- ``time`` is a display string the server derives.

    ``"10:00 AM - 11:30 AM"``, ``"Full Day"`` or ``"Multi-Day"``.
    """
    if event.ends_at and event.starts_at.date() != event.ends_at.date():
        return "Multi-Day"
    if event.all_day:
        return "Full Day"
    start = timezone.localtime(event.starts_at).strftime("%I:%M %p").lstrip("0")
    if event.ends_at is None:
        return start
    end = timezone.localtime(event.ends_at).strftime("%I:%M %p").lstrip("0")
    return f"{start} - {end}"


def org_chart(client_id):
    """``GET /hrms/org-chart/`` -- a recursive walk over ``manager_id``."""
    # People who have left are not on the chart.
    employees = list(
        Employee.objects.filter(client_id=client_id, deleted_at__isnull=True)
        .exclude(status__in=["Resigned", "Terminated"])
        .select_related("designation", "department", "location")
        .only(
            "id", "name", "employee_code", "manager_id", "avatar_url", "email",
            "phone", "status", "designation__name", "department__name", "location__name",
        )
    )
    nodes = {
        employee.id: {
            "id": str(employee.id),
            "name": employee.name,
            "employeeCode": employee.employee_code,
            "designation": employee.designation.name if employee.designation_id else None,
            "department": employee.department.name if employee.department_id else None,
            "location": employee.location.name if employee.location_id else None,
            "email": employee.email,
            "phone": employee.phone,
            "status": employee.status,
            "avatar": employee.avatar_url,
            "managerId": str(employee.manager_id) if employee.manager_id else None,
            "manager": None,
            "reports": [],
        }
        for employee in employees
    }

    roots = []
    for employee in employees:
        node = nodes[employee.id]
        parent = nodes.get(employee.manager_id) if employee.manager_id else None
        if parent is not None and parent is not node:
            node["manager"] = parent["name"]
            parent["reports"].append(node)
        else:
            roots.append(node)
    return roots


def assert_no_manager_cycle(employee, manager_id):
    """db.md §11.1 -- reject any manager reachable from the employee."""
    if manager_id is None:
        return
    if str(manager_id) == str(employee.id):
        raise ValidationFailed(
            "An employee cannot report to themselves.",
            field_errors={"managerId": ["Cannot be self."]},
        )

    seen = {employee.id}
    current = manager_id
    while current is not None:
        if current in seen:
            raise ValidationFailed(
                "That reporting line would create a loop.",
                field_errors={"managerId": ["Creates a reporting cycle."]},
            )
        seen.add(current)
        current = (
            Employee.objects.filter(pk=current).values_list("manager_id", flat=True).first()
        )
