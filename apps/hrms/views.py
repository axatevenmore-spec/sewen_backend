"""HRMS endpoints (api.md §11)."""
from datetime import datetime, timedelta

from django.db import transaction
from django.db.models import Count, Q
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core.audit import notify, record_audit
from apps.core.exceptions import (
    BusinessRuleViolation,
    Codes,
    Conflict,
    NotFound,
    PermissionDenied,
    ValidationFailed,
)
from apps.core.money import ZERO, D, round2
from apps.core.numbering import allocate_number
from apps.core.pagination import envelope
from apps.core.permissions import HasModulePermission, has_permission
from apps.core.printing import PdfNotAvailable, print_payload
from apps.core.viewsets import ReadOnlyTenantViewSet, TenantModelViewSet

from . import services, user_link
from .models import (
    Appraisal,
    AppraisalCycle,
    AppraisalHistory,
    ApprovalChain,
    Application,
    Asset,
    AssetAssignment,
    AssetCategory,
    AssetRequest,
    Attendance,
    AttendancePunch,
    AttendanceRegularization,
    CalendarEvent,
    Candidate,
    Complaint,
    CompOff,
    Department,
    Designation,
    Employee,
    EMPLOYMENT_TYPES,
    Goal,
    Holiday,
    HrDocument,
    Interview,
    Job,
    Kpi,
    LeaveBalance,
    LeaveEncashment,
    LeaveRequest,
    LeaveType,
    Location,
    Offer,
    OnboardingTask,
    PayrollRun,
    Payslip,
    PerformanceIndicator,
    Policy,
    PolicyAcknowledgement,
    PolicyCategory,
    PolicyVersion,
    Resignation,
    SalaryAdvance,
    SalaryStructure,
    ScreeningQuestion,
    ScreeningAnswer,
    Team,
    Termination,
    Trainer,
    Training,
    TrainingParticipant,
    TRAINING_STAGES,
    WorkingDay,
    WorkSchedule,
    EmployeeTransfer,
    EmployeePromotion,
    EmployeeWarning,
    EmployeeAward,
    TravelRequest,
    Announcement,
    MeetingRoom,
    CompanyMeeting,
)
from .serializers import (
    AppraisalCycleSerializer,
    AppraisalSerializer,
    ApprovalChainSerializer,
    ApplicationSerializer,
    AssetCategorySerializer,
    AssetRequestSerializer,
    AssetSerializer,
    AttendancePunchSerializer,
    AttendanceSerializer,
    BulkAttendanceSerializer,
    CalendarEventSerializer,
    CandidateSerializer,
    ComplaintSerializer,
    CompOffSerializer,
    DepartmentSerializer,
    DesignationSerializer,
    EmployeeSerializer,
    GoalSerializer,
    HolidaySerializer,
    WorkScheduleSerializer,
    HrDocumentSerializer,
    InterviewSerializer,
    JobSerializer,
    KpiSerializer,
    LeaveBalanceSerializer,
    LeaveEncashmentSerializer,
    LeaveRequestSerializer,
    LeaveTypeSerializer,
    LocationSerializer,
    OfferSerializer,
    OnboardingTaskSerializer,
    PayrollRunSerializer,
    PayslipSerializer,
    PerformanceIndicatorSerializer,
    PolicyAcknowledgementSerializer,
    PolicyCategorySerializer,
    PolicySerializer,
    ProcessPayrollSerializer,
    RegularizationSerializer,
    ResignationSerializer,
    SalaryAdvanceSerializer,
    SalaryStructureSerializer,
    ScreeningQuestionSerializer,
    ScreeningAnswerSerializer,
    TeamSerializer,
    TerminationSerializer,
    TrainerSerializer,
    TrainingParticipantSerializer,
    TrainingSerializer,
    WorkingDaySerializer,
    EmployeeTransferSerializer,
    EmployeePromotionSerializer,
    EmployeeWarningSerializer,
    EmployeeAwardSerializer,
    TravelRequestSerializer,
    AnnouncementSerializer,
    MeetingRoomSerializer,
    CompanyMeetingSerializer,
)


#: Leave and pay screens serve both the employee (own rows) and the approver
#: (team rows, via OwnEmployeeScopeMixin). Requiring only the self-service id
#: locked HR Manager -- who approves but does not apply -- out of the queue.
LEAVE_READERS = ("apply_leave", "approve_leave")
PAY_READERS = ("view_own_payslip", "generate_payroll", "approve_payroll")
#: Company-wide pay -- every payslip, the salary templates. HR admin, super
#: admin and any executive role granted one of these.
PAY_MANAGERS = ("generate_payroll", "approve_payroll", "edit_salary_structure")

def _as_date(value):
    """``YYYY-MM-DD`` (or a date) to a date; anything else to None."""
    if not value:
        return None
    if hasattr(value, "year"):
        return value
    try:
        return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def _as_moment(value, day):
    """An ISO datetime, or ``HH:MM`` on ``day``, as an aware datetime."""
    if not value or str(value).strip() in ("—", "-", "null", "undefined", ""):
        return None
    from django.utils.dateparse import parse_datetime

    text = str(value).strip()
    moment = parse_datetime(text) if "T" in text or " " in text else None
    if moment is None:
        try:
            moment = datetime.combine(day, datetime.strptime(text[:5], "%H:%M").time())
        except ValueError:
            return None
    return timezone.make_aware(moment) if timezone.is_naive(moment) else moment


def reporting_chain_ids(client_id, manager_id):
    """Ids of every employee below ``manager_id`` (direct and indirect reports)."""
    found, frontier = set(), {manager_id}
    while frontier:
        below = set(
            Employee.objects.filter(
                client_id=client_id, manager_id__in=frontier, deleted_at__isnull=True
            ).values_list("id", flat=True)
        ) - found - {manager_id}
        found |= below
        frontier = below
    return found


class OwnEmployeeScopeMixin:
    """Record-level scope for self-service HR data.

    The permission map decides whether a screen can be used at all; this
    decides whose rows it shows. Holders of any of ``team_scope_permissions``
    see the tenant; everyone else sees -- and may write -- only rows for the
    employee linked to their own user. ``PayslipViewSet`` applies the same
    rule by hand.
    """

    team_scope_permissions = ()
    #: When True, a reporting manager without the team permission may also
    #: *read* the rows of everyone below them in the reporting chain. Writes
    #: stay limited to their own rows.
    manager_read_scope = False
    #: A field naming an employee who may also *read* the row -- the colleague
    #: a leave hands work over to sees it in "Assigned to me".
    delegate_read_field = None

    def has_team_scope(self):
        return has_permission(self.request.user, tuple(self.team_scope_permissions))

    def get_queryset(self):
        queryset = super().get_queryset()
        if self.has_team_scope():
            return queryset
        employee_id = getattr(self.request.user, "employee_id", None)
        if not employee_id and hasattr(self, "_resolve_employee"):
            resolved = self._resolve_employee(self.request)
            if resolved:
                employee_id = resolved.id
        if not employee_id:
            return queryset.none()
        if self.request.method not in ("GET", "HEAD", "OPTIONS"):
            return queryset.filter(employee_id=employee_id)
        visible = [employee_id]
        if self.manager_read_scope:
            visible += reporting_chain_ids(self.request.client_id, employee_id)
        scope = Q(employee_id__in=visible)
        if self.delegate_read_field:
            scope |= Q(**{self.delegate_read_field: employee_id})
        return queryset.filter(scope)

    def check_own_employee(self, employee):
        if self.has_team_scope():
            return
        resolved = None
        if hasattr(self, "_resolve_employee"):
            resolved = self._resolve_employee(self.request)
        own = resolved.pk if resolved else getattr(self.request.user, "employee_id", None)
        if employee is None or own is None or employee.pk != own:
            raise PermissionDenied(
                "You can only do this for your own employee record.",
                code=self.team_scope_permissions[0] if self.team_scope_permissions else "PERMISSION_DENIED",
            )

    def _resolve_employee(self, request, employee_id=None):
        client_id = getattr(request, "client_id", None) or getattr(request.user, "client_id", None)
        if employee_id:
            try:
                emp = Employee.objects.filter(
                    pk=employee_id, client_id=client_id, deleted_at__isnull=True
                ).first()
                if emp:
                    return emp
            except (ValueError, TypeError, Exception):
                pass
            return Employee.objects.filter(
                employee_code=employee_id, client_id=client_id, deleted_at__isnull=True
            ).first()

        if getattr(request.user, "employee", None):
            return request.user.employee

        if getattr(request.user, "email", None):
            emp = Employee.objects.filter(
                client_id=client_id, email__iexact=request.user.email, deleted_at__isnull=True
            ).first()
            if emp:
                if not getattr(request.user, "employee_id", None) and hasattr(request.user, "save"):
                    request.user.employee = emp
                    request.user.save(update_fields=["employee"])
                return emp

        if getattr(request.user, "name", None):
            emp = Employee.objects.filter(
                client_id=client_id, name__iexact=request.user.name, deleted_at__isnull=True
            ).first()
            if emp:
                if not getattr(request.user, "employee_id", None) and hasattr(request.user, "save"):
                    request.user.employee = emp
                    request.user.save(update_fields=["employee"])
                return emp

        if getattr(request.user, "is_authenticated", False) and (getattr(request.user, "client", None) or client_id):
            try:
                from apps.hrms.user_link import create_employee_for
                emp = create_employee_for(request.user)
                if hasattr(request.user, "save"):
                    request.user.employee = emp
                    request.user.save(update_fields=["employee"])
                return emp
            except Exception:
                pass

        return None

# ---------------------------------------------------------------------------
# Organisation (api.md §11.1)
# ---------------------------------------------------------------------------
class EmployeeViewSet(TenantModelViewSet):
    queryset = Employee.objects.select_related(
        "designation", "department", "manager", "location", "salary_structure"
    )
    serializer_class = EmployeeSerializer
    audit_entity_type = "Employee"
    audit_label_field = "employee_code"
    status_field = "status"
    search_fields = ["name", "employee_code", "email", "phone"]
    ordering = ["name"]
    default_date_field = "joining_date"
    filter_map = {
        "department": "department__name",
        "departmentId": "department_id",
        "designation": "designation__name",
        "location": "location__name",
        "managerId": "manager_id",
        "employmentType": "employment_type",
    }
    permission_map = {
        "read": ["view_staff"],
        "write": ["edit_staff"],
        "create": ["create_staff"],
        # Anyone who applies for leave picks a colleague to hand work over to.
        "colleagues": [LEAVE_READERS],
        # Every signed-in employee may read their own record.
        "me": [],
        "confirm_probation": ["edit_staff", "create_staff"],
        "extend_probation": ["edit_staff", "create_staff"],
        "evaluate_lifecycle": ["edit_staff", "create_staff"],
    }

    @action(detail=True, methods=["post"], url_path="confirm-probation")
    def confirm_probation(self, request, pk=None):
        employee = self.get_object()
        from . import lifecycle_service
        from datetime import date
        eff_date = request.data.get("effectiveDate") or request.data.get("confirmationDate")
        if isinstance(eff_date, str):
            try:
                eff_date = date.fromisoformat(eff_date)
            except ValueError:
                eff_date = None
        remarks = request.data.get("remarks") or request.data.get("reason")
        updated = lifecycle_service.confirm_employee_probation(
            employee, user=request.user, effective_date=eff_date, remarks=remarks
        )
        return Response(self.get_serializer(updated).data)

    @action(detail=True, methods=["post"], url_path="extend-probation")
    def extend_probation(self, request, pk=None):
        employee = self.get_object()
        from . import lifecycle_service
        from datetime import date
        ext_days = request.data.get("extensionDays") or request.data.get("days")
        new_end = request.data.get("newEndDate")
        if isinstance(new_end, str):
            try:
                new_end = date.fromisoformat(new_end)
            except ValueError:
                new_end = None
        reason = request.data.get("reason") or request.data.get("remarks")
        updated = lifecycle_service.extend_employee_probation(
            employee, user=request.user, extension_days=ext_days, new_end_date=new_end, reason=reason
        )
        return Response(self.get_serializer(updated).data)

    @action(detail=False, methods=["post"], url_path="evaluate-lifecycle")
    def evaluate_lifecycle(self, request):
        from . import lifecycle_service
        res = lifecycle_service.evaluate_employee_lifecycle(client_id=request.client_id)
        return Response(res)


    @action(detail=False, methods=["get"])
    def me(self, request):
        """``GET /hrms/employees/me/`` -- the caller's own employee record.

        Self-service: no permission beyond being signed in, and only ever the
        record linked to this login, so it cannot be pointed at anyone else.
        """
        employee_id = getattr(request.user, "employee_id", None)
        employee = (
            self.get_queryset().filter(pk=employee_id).first()
            if employee_id else None
        )
        if employee is None:
            raise NotFound(
                "Your login is not linked to an employee record. Ask HR to link it.",
                code="NO_EMPLOYEE",
            )
        return Response(self.get_serializer(employee).data)

    @action(detail=False, methods=["get"])
    def colleagues(self, request):
        """``GET /hrms/employees/colleagues/`` -- names only, for the delegate picker.

        Self-service cannot read the staff directory (`view_staff`), so this
        hands out just enough to choose who covers your work: no contact,
        pay or personal fields.
        """
        rows = (
            Employee.objects.filter(client_id=request.client_id, deleted_at__isnull=True)
            .exclude(status__in=["Resigned", "Terminated"])
            .select_related("department", "designation")
            .order_by("name")
        )
        return Response(envelope([
            {
                "id": str(e.id),
                "name": e.name,
                "department": e.department.name if e.department_id else "",
                "designation": e.designation.name if e.designation_id else "",
            }
            for e in rows
        ]))

    def get_aggregates(self, queryset):
        return queryset.aggregate(
            total=Count("id"),
            active=Count("id", filter=Q(status="Active")),
            onLeave=Count("id", filter=Q(status="On Leave")),
            probation=Count("id", filter=Q(status="Probation")),
            exited=Count("id", filter=Q(status__in=["Resigned", "Terminated"])),
        )

    def get_queryset(self):
        # The linked Administration login, for the directory's "Login" column
        # (users.employee_id is the only pointer, so it is read as a join).
        from django.db.models import OuterRef, Subquery

        from apps.accounts.models import User

        logins = User.objects.filter(
            employee=OuterRef("pk"), deleted_at__isnull=True
        ).exclude(status="Deleted").order_by("created_at")
        qs = super().get_queryset().annotate(
            linked_user_id=Subquery(logins.values("id")[:1]),
            linked_user_email=Subquery(logins.values("email")[:1]),
            linked_user_status=Subquery(logins.values("status")[:1]),
            linked_user_role=Subquery(logins.values("role__name")[:1]),
        )

        status_vals = []
        include_exited = None
        if hasattr(self, "request") and self.request and hasattr(self.request, "query_params"):
            status_vals = [s.strip() for s in self.request.query_params.getlist("status") if s.strip()]
            include_exited = self.request.query_params.get("include_exited")

        if any(s.lower() == "all" for s in status_vals):
            self.status_field = None
            return qs

        if not status_vals and str(include_exited).lower() not in ("true", "1", "yes"):
            # Exclude exited employees from active employee lists by default while preserving historical access
            qs = qs.exclude(status__in=["Resigned", "Terminated"])

        return qs

    @transaction.atomic
    def perform_create(self, serializer):
        client_id = self.get_client_id()
        email = (serializer.validated_data.get("email") or "").strip().lower()
        if email:
            existing_emp = Employee.objects.filter(
                client_id=client_id, email__iexact=email, deleted_at__isnull=True
            ).first()
            if existing_emp:
                raise Conflict(
                    f"An employee with email '{email}' already exists ({existing_emp.employee_code} - {existing_emp.name}).",
                    code=Codes.ALREADY_EXISTS,
                    payload={"existingEmployeeId": str(existing_emp.id), "employeeCode": existing_emp.employee_code},
                )

        serializer.validated_data["employee_code"] = allocate_number(
            self.request.user.client, "EMP"
        )
        employee = super().perform_create(serializer)

        # api.md §11.1 -- Connect employee with Administration user account
        if employee.email:
            from apps.accounts.invites import send_invite_on_commit
            from apps.accounts.models import User, Role
            client_id = self.get_client_id()
            normalized_email = employee.email.strip().lower()

            existing_user = User.objects.filter(
                client_id=client_id, email=normalized_email,
                deleted_at__isnull=True,
            ).first()

            if existing_user:
                if not user_link.linked_employee(existing_user):
                    existing_user.employee = employee
                    existing_user.save(update_fields=["employee", "updated_at"])
                    user_link.reconcile(existing_user, employee)
                    self.write_audit(
                        "link_user", employee,
                        description=f"Linked user account {existing_user.email} to employee {employee.employee_code}",
                    )
            elif self.request.data.get("createUserAccount", True):
                # No default password -- without one the login is unusable
                # until the employee activates it from the invite email.
                emp_password = (
                    self.request.data.get("password")
                    or self.request.data.get("userPassword")
                    or None
                )
                role_id = self.request.data.get("roleId")
                emp_role = None
                if role_id:
                    emp_role = Role.objects.filter(client_id=client_id, pk=role_id).first()
                if not emp_role:
                    emp_role = Role.objects.filter(client_id=client_id, code__in=["EM", "EMP"]).first()

                user = User.objects.create_user(
                    email=normalized_email,
                    password=emp_password,
                    client_id=client_id,
                    name=employee.name,
                    phone=employee.phone,
                    employee=employee,
                    department=employee.department.name if employee.department_id else None,
                    role=emp_role,
                    status="Active",
                )
                # Location, joining date, photo and manager follow the HR record.
                user_link.reconcile(user, employee)
                send_invite_on_commit(user)
                self.write_audit(
                    "provision_user", employee,
                    description=f"User account created for {user.email}",
                )
        return employee

    @transaction.atomic
    def perform_update(self, serializer):
        before = user_link.capture(serializer.instance, user_link.EMPLOYEE_FIELDS)
        employee = super().perform_update(serializer)
        changed = user_link.changed_fields(employee, before, user_link.EMPLOYEE_FIELDS)
        if changed:
            user_link.push_employee_to_user(employee, changed)
        return employee

    @transaction.atomic
    def perform_destroy(self, instance):
        super().perform_destroy(instance)
        user_link.unlink_employee(instance)

    @action(detail=True, methods=["post"])
    @transaction.atomic
    def terminate(self, request, pk=None):
        employee = self.get_object()
        if employee.status == "Terminated":
            raise Conflict("This employee is already terminated.", code=Codes.ALREADY_DONE)

        reason = request.data.get("reason")
        last_working_day = request.data.get("lastWorkingDay") or timezone.localdate()

        employee.status = "Terminated"
        employee.termination_reason = reason
        employee.last_working_day = last_working_day
        employee.save(
            update_fields=["status", "termination_reason", "last_working_day", "updated_at"]
        )

        Termination.objects.create(
            client_id=request.client_id,
            employee=employee,
            reason=reason,
            last_working_day=last_working_day,
            status="Completed",
            created_by=request.user,
        )
        # A terminated employee must not keep an active login.
        from apps.accounts.models import User, UserSession

        users = User.objects.filter(employee=employee, deleted_at__isnull=True)
        users.update(status="Inactive", is_active=False)
        UserSession.objects.filter(user__in=users, revoked_at__isnull=True).update(
            revoked_at=timezone.now(), revoked_reason="employee_terminated"
        )

        self.write_audit("terminate", employee, description=reason)
        return Response(self.get_serializer(employee).data)

    @action(detail=True, methods=["get"])
    def documents(self, request, pk=None):
        employee = self.get_object()
        rows = HrDocument.objects.filter(
            client_id=request.client_id, employee=employee, deleted_at__isnull=True
        )
        return Response(envelope(HrDocumentSerializer(rows, many=True).data))

    @action(detail=True, methods=["get"])
    def assets(self, request, pk=None):
        employee = self.get_object()
        rows = Asset.objects.filter(
            client_id=request.client_id, assigned_employee=employee, deleted_at__isnull=True
        )
        return Response(envelope(AssetSerializer(rows, many=True).data))


def _live_employees(prefix="employees"):
    return Q(**{f"{prefix}__deleted_at__isnull": True})


def _refuse_if_staffed(label, count, what="employee"):
    """An org unit still in use is not deleted out from under its people."""
    if count:
        raise Conflict(
            f"{count} {what}{'s' if count != 1 else ''} still use{'s' if count == 1 else ''} this {label}.",
            code=Codes.IN_USE,
            detail=f"Move them to another {label} first.",
        )


class HrmsDepartmentViewSet(TenantModelViewSet):
    queryset = Department.objects.select_related("head_employee", "parent")
    serializer_class = DepartmentSerializer
    audit_entity_type = "HrmsDepartment"
    audit_label_field = "name"
    status_field = "status"
    search_fields = ["name", "code"]
    ordering = ["name"]
    permission_map = {"read": ["view_staff"], "write": ["edit_staff"]}

    def get_queryset(self):
        from django.db.models import IntegerField, OuterRef, Subquery, Sum
        from django.db.models.functions import Coalesce

        open_jobs = (
            Job.objects.filter(department=OuterRef("pk"), status="Open", deleted_at__isnull=True)
            .values("department")
            .annotate(total=Sum("openings"))
            .values("total")
        )
        return super().get_queryset().annotate(
            employee_count=Count("employees", filter=_live_employees(), distinct=True),
            team_count=Count("teams", filter=Q(teams__deleted_at__isnull=True), distinct=True),
            open_roles=Coalesce(Subquery(open_jobs, output_field=IntegerField()), 0),
        )

    def check_delete_allowed(self, department):
        _refuse_if_staffed(
            "department", department.employees.filter(deleted_at__isnull=True).count()
        )
        _refuse_if_staffed(
            "department",
            department.designations.filter(deleted_at__isnull=True).count(),
            what="designation",
        )


class DesignationViewSet(TenantModelViewSet):
    queryset = Designation.objects.select_related("department")
    serializer_class = DesignationSerializer
    audit_entity_type = "Designation"
    audit_label_field = "name"
    status_field = None
    search_fields = ["name"]
    ordering = ["name"]
    filter_map = {"departmentId": "department_id"}
    permission_map = {"read": ["view_staff"], "write": ["edit_staff"]}

    def get_queryset(self):
        return super().get_queryset().annotate(
            employee_count=Count("employees", filter=_live_employees(), distinct=True),
        )

    def check_delete_allowed(self, designation):
        _refuse_if_staffed(
            "designation", designation.employees.filter(deleted_at__isnull=True).count()
        )


class HrmsLocationViewSet(TenantModelViewSet):
    queryset = Location.objects.all()
    serializer_class = LocationSerializer
    audit_entity_type = "HrmsLocation"
    audit_label_field = "name"
    status_field = None
    search_fields = ["name", "timezone"]
    ordering = ["name"]
    filter_map = {"type": "location_type"}
    permission_map = {"read": ["view_staff"], "write": ["edit_staff"]}

    def get_queryset(self):
        return super().get_queryset().annotate(
            employee_count=Count("employees", filter=_live_employees(), distinct=True),
        )

    def check_delete_allowed(self, location):
        _refuse_if_staffed(
            "location", location.employees.filter(deleted_at__isnull=True).count()
        )


class OrgChartView(APIView):
    permission_classes = [HasModulePermission]
    required_permissions = ["view_staff"]

    def get(self, request):
        from . import org_structure

        # ``tree`` is the plain reporting-line walk; the rest is the
        # Department -> Head -> reporting hierarchy the Org Chart draws.
        return Response({
            "tree": services.org_chart(request.client_id),
            **org_structure.build(request.client_id),
        })


class HrmsDashboardView(APIView):
    permission_classes = [HasModulePermission]
    required_permissions = ["show_hrm_dashboard"]

    def get(self, request):
        client_id = request.client_id
        today = timezone.localdate()
        employees = Employee.objects.filter(client_id=client_id, deleted_at__isnull=True)

        attendance_today = Attendance.objects.filter(
            client_id=client_id, work_date=today, deleted_at__isnull=True
        ).values("status").annotate(count=Count("id"))

        upcoming = CalendarEvent.objects.filter(
            client_id=client_id,
            deleted_at__isnull=True,
            starts_at__gte=timezone.now(),
            starts_at__lte=timezone.now() + timedelta(days=30),
        ).order_by("starts_at")[:10]

        exits = employees.filter(
            status__in=["Resigned", "Terminated"],
            last_working_day__gte=today - timedelta(days=365),
        ).count()
        headcount = employees.filter(status__in=["Active", "On Leave", "Probation"]).count()

        return Response(
            {
                "headcount": headcount,
                "newJoinersThisMonth": employees.filter(
                    joining_date__year=today.year, joining_date__month=today.month
                ).count(),
                "attritionRatePct": round(exits / headcount * 100, 1) if headcount else 0,
                "onLeaveToday": Attendance.objects.filter(
                    client_id=client_id, work_date=today, status="On Leave",
                    deleted_at__isnull=True,
                ).count(),
                "attendanceToday": {
                    row["status"]: row["count"] for row in attendance_today
                },
                "pendingLeaveRequests": LeaveRequest.objects.filter(
                    client_id=client_id,
                    status__in=["Pending Review", "Delegate Confirmed"],
                    deleted_at__isnull=True,
                ).count(),
                "openPositions": Job.objects.filter(
                    client_id=client_id, status="Open", deleted_at__isnull=True
                ).count(),
                "upcomingEvents": CalendarEventSerializer(upcoming, many=True).data,
            }
        )


# ---------------------------------------------------------------------------
# Attendance (api.md §11.2)
# ---------------------------------------------------------------------------
class AttendanceViewSet(OwnEmployeeScopeMixin, TenantModelViewSet):
    queryset = Attendance.objects.select_related("employee", "employee__department")
    serializer_class = AttendanceSerializer
    audit_entity_type = "Attendance"
    status_field = "status"
    default_date_field = "work_date"
    allowed_date_fields = ("work_date",)
    ordering = ["-work_date"]
    search_fields = ["employee__name", "employee__employee_code"]
    filter_map = {
        "employeeId": "employee_id",
        "department": "employee__department__name",
        "date": "work_date",
    }
    permission_map = {
        # Everyone who marks attendance reads their own rows (the mixin scopes
        # them); `view_team_attendance` reads the whole register.
        "read": [("view_team_attendance", "mark_attendance")],
        # Either permission writes; the mixin keeps a `mark_attendance`-only
        # employee to their own rows. Other corrections go through
        # regularization.
        "write": [("mark_attendance", "view_team_attendance")],
        # Marking a whole day for many people is a team action.
        "bulk": ["mark_attendance", "view_team_attendance"],
        # Tenant-wide figures and the edit trail.
        "summary": ["view_team_attendance"],
        "audit": ["view_team_attendance"],
        "today": [],
        "punch": [],
        "correct_punch": ["regularize_attendance", "mark_attendance"],
        "punches": [],
        "punch_timeline": [],
    }
    team_scope_permissions = ("view_team_attendance",)

    def _own_or_team(self, employee):
        """Someone else's attendance needs the team permission."""
        if employee is not None and not self.has_team_scope():
            self.check_own_employee(employee)

    def get_aggregates(self, queryset):
        return queryset.aggregate(
            total=Count("id"),
            present=Count("id", filter=Q(status="Present")),
            absent=Count("id", filter=Q(status="Absent")),
            late=Count("id", filter=Q(status="Late")),
            onLeave=Count("id", filter=Q(status="On Leave")),
            wfh=Count("id", filter=Q(status="WFH")),
        )

    def perform_create(self, serializer):
        data = serializer.validated_data
        self.check_own_employee(data.get("employee"))
        row = services.mark_attendance(
            client=self.request.user.client,
            employee=data["employee"],
            work_date=data["work_date"],
            check_in=data.get("check_in"),
            check_out=data.get("check_out"),
            status=data.get("status"),
            remark=data.get("remark"),
            source="manual",
            user=self.request.user,
        )
        serializer.instance = row
        self._created_instance = row
        self._concurrency_instance = row
        return row

    def perform_update(self, serializer):
        data = serializer.validated_data
        instance = serializer.instance
        self.check_own_employee(data.get("employee", instance.employee))
        row = services.mark_attendance(
            client=self.request.user.client,
            employee=data.get("employee", instance.employee),
            work_date=data.get("work_date", instance.work_date),
            check_in=data.get("check_in", instance.check_in),
            check_out=data.get("check_out", instance.check_out),
            status=data.get("status", instance.status),
            remark=data.get("remark", instance.remark),
            source="manual",
            user=self.request.user,
        )
        serializer.instance = row
        self._concurrency_instance = row
        return row

    @action(detail=False, methods=["post"])
    @transaction.atomic
    def bulk(self, request):
        """``{ date, records: [] }`` or ``{ ids: [], status }`` (api.md §11.2)."""
        serializer = BulkAttendanceSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        if data.get("ids") and data.get("status"):
            updated = Attendance.objects.filter(
                client_id=request.client_id, pk__in=data["ids"], deleted_at__isnull=True
            ).update(status=data["status"], source="bulk")
            return Response({"updated": updated})

        work_date = data.get("date") or timezone.localdate()
        written = 0
        for record in data.get("records", []):
            employee_id = record.get("employeeId") or record.get("employee_id") or record.get("id")
            employee = None
            try:
                employee = Employee.objects.filter(
                    pk=employee_id, client_id=request.client_id, deleted_at__isnull=True
                ).first()
            except (ValueError, TypeError, Exception):
                employee = None
            if employee is None and employee_id:
                employee = Employee.objects.filter(
                    employee_code=employee_id, client_id=request.client_id, deleted_at__isnull=True
                ).first()
            if employee is None:
                continue
            day = _as_date(record.get("date")) or work_date
            services.mark_attendance(
                client=request.user.client,
                employee=employee,
                work_date=day,
                check_in=_as_moment(record.get("checkIn"), day),
                check_out=_as_moment(record.get("checkOut"), day),
                status=record.get("status"),
                remark=record.get("remark"),
                source="bulk",
                user=request.user,
            )
            written += 1
        return Response({"written": written, "date": work_date})

    @action(detail=False, methods=["get"])
    def summary(self, request):
        month = request.query_params.get("month")
        parsed = None
        if month:
            try:
                parsed = datetime.strptime(month[:7], "%Y-%m").date()
            except ValueError:
                raise ValidationFailed(
                    "Month must be YYYY-MM.", field_errors={"month": ["Expected YYYY-MM."]}
                )
        rows = services.attendance_summary(
            request.client_id,
            month=parsed,
            employee_id=request.query_params.get("employeeId"),
            department_id=request.query_params.get("departmentId"),
        )
        return Response(envelope(rows))

    @action(detail=False, methods=["get"], url_path=r"individual/(?P<employee_id>[^/.]+)")
    def individual(self, request, employee_id=None):
        if not self.has_team_scope():
            target = self._resolve_employee(request, employee_id) if employee_id else None
            if target is None:
                raise PermissionDenied(
                    "You can only view your own attendance.", code="view_team_attendance"
                )
            self.check_own_employee(target)
        target = self._resolve_employee(request, employee_id) if employee_id else None
        target_id = target.id if target else employee_id
        rows = Attendance.objects.filter(
            client_id=request.client_id, employee_id=target_id, deleted_at__isnull=True
        ).order_by("work_date")
        date_from = request.query_params.get("date_from")
        date_to = request.query_params.get("date_to")
        if date_from:
            rows = rows.filter(work_date__gte=date_from)
        if date_to:
            rows = rows.filter(work_date__lte=date_to)
        return Response(envelope(AttendanceSerializer(rows, many=True).data))

    @action(detail=False, methods=["get"])
    def audit(self, request):
        """``GET /hrms/attendance/audit/`` -- the manual-edit trail (db.md §11.2)."""
        from apps.core.models import AuditLog
        from apps.core.serializers_platform import AuditLogSerializer

        rows = AuditLog.objects.filter(
            client_id=request.client_id,
            entity_type="Attendance",
            action__in=["attendance_edit", "attendance_mark"],
        )[:200]
        return Response(envelope(AuditLogSerializer(rows, many=True).data))

    def _resolve_employee(self, request, employee_id=None):
        if employee_id:
            try:
                emp = Employee.objects.filter(
                    pk=employee_id, client_id=request.client_id, deleted_at__isnull=True
                ).first()
                if emp:
                    return emp
            except (ValueError, TypeError, Exception):
                pass
            return Employee.objects.filter(
                employee_code=employee_id, client_id=request.client_id, deleted_at__isnull=True
            ).first()

        # Check request.user.employee
        if getattr(request.user, "employee", None):
            return request.user.employee

        # Fallback to match user email with employee email
        if getattr(request.user, "email", None):
            emp = Employee.objects.filter(
                client_id=request.client_id, email__iexact=request.user.email, deleted_at__isnull=True
            ).first()
            if emp:
                if not getattr(request.user, "employee_id", None) and hasattr(request.user, "save"):
                    request.user.employee = emp
                    request.user.save(update_fields=["employee"])
                return emp

        # Fallback to match user name with employee name
        if getattr(request.user, "name", None):
            emp = Employee.objects.filter(
                client_id=request.client_id, name__iexact=request.user.name, deleted_at__isnull=True
            ).first()
            if emp:
                if not getattr(request.user, "employee_id", None) and hasattr(request.user, "save"):
                    request.user.employee = emp
                    request.user.save(update_fields=["employee"])
                return emp

        return None

    @action(detail=False, methods=["get"])
    def today(self, request):
        """``GET /hrms/attendance/today/`` -- punch state for current employee today."""
        employee_id = request.query_params.get("employeeId")
        employee = self._resolve_employee(request, employee_id)
        if employee_id:
            self._own_or_team(employee)
        if not employee:
            return Response({
                "has_employee": False,
                "status": "Not Punched In",
                "is_punched_in": False,
                "punches": [],
                "formatted_working_time": "00h 00m",
                "working_seconds": 0,
                "working_hours": 0.0,
                "late_minutes": 0,
                "overtime_hours": 0.0,
                "first_punch": None,
                "last_punch": None,
                "date": timezone.localdate().isoformat(),
            })
        status_data = services.get_today_punch_status(request.client_id, employee)
        return Response(status_data)

    @action(detail=False, methods=["post"])
    def punch(self, request):
        """``POST /hrms/attendance/punch/`` -- record Punch In or Punch Out."""
        punch_type = (
            request.data.get("punch_type")
            or request.data.get("punchType")
            or request.data.get("type")
        )
        if not punch_type:
            raise ValidationFailed(
                "Punch type ('IN' or 'OUT') is required.",
                field_errors={"punch_type": ["Required."]},
            )

        employee = self._resolve_employee(
            request, request.data.get("employeeId") or request.data.get("employee_id")
        )
        if not employee:
            raise ValidationFailed(
                "No employee profile linked to current user account.",
                code="NO_EMPLOYEE",
            )

        # Self-service scope: non-managers can only punch for their own employee
        if not self.has_team_scope():
            self.check_own_employee(employee)

        remark = request.data.get("remark") or request.data.get("notes")
        source = request.data.get("source") or "web"
        early_reason = (
            request.data.get("earlyReason")
            or request.data.get("early_reason")
            or request.data.get("reason")
        )
        request_regularization = bool(
            request.data.get("requestRegularization")
            or request.data.get("request_regularization")
            or request.data.get("createRegularization")
        )

        punch_obj, status_data = services.record_punch(
            client=request.user.client,
            employee=employee,
            punch_type=punch_type,
            source=source,
            remark=remark,
            early_reason=early_reason,
            request_regularization=request_regularization,
            user=request.user,
        )
        return Response(
            status_data,
            status=status.HTTP_201_CREATED if punch_obj else status.HTTP_200_OK,
        )

    @action(detail=False, methods=["post"], url_path="correct-punch")
    def correct_punch(self, request):
        """``POST /hrms/attendance/correct-punch/`` -- HR/Admin manual punch correction."""
        if not has_permission(request.user, ("regularize_attendance", "mark_attendance")):
            raise PermissionDenied("You do not have permission to correct punches.")

        employee_id = request.data.get("employeeId") or request.data.get("employee_id")
        employee = self._resolve_employee(request, employee_id)
        if not employee:
            raise ValidationFailed(
                "Employee not found.", field_errors={"employeeId": ["Invalid employee."]}
            )

        punch_type = request.data.get("punchType") or request.data.get("punch_type")
        work_date_str = request.data.get("date") or request.data.get("work_date")
        punch_time_str = request.data.get("punchTime") or request.data.get("punch_time")
        reason = request.data.get("reason") or "Manual correction"

        if not work_date_str or not punch_time_str or not punch_type:
            raise ValidationFailed("Date, punch time, and punch type are required.")

        try:
            work_date = datetime.strptime(str(work_date_str)[:10], "%Y-%m-%d").date()
        except ValueError:
            raise ValidationFailed("Invalid date format. Expected YYYY-MM-DD.")

        if "T" in str(punch_time_str):
            from django.utils.dateparse import parse_datetime
            dt = parse_datetime(punch_time_str)
            if dt and not timezone.is_aware(dt):
                dt = timezone.make_aware(dt)
        else:
            time_part = datetime.strptime(str(punch_time_str)[:5], "%H:%M").time()
            dt = timezone.make_aware(datetime.combine(work_date, time_part))

        punch_obj, status_data = services.correct_punch(
            client=request.user.client,
            employee=employee,
            work_date=work_date,
            punch_type=punch_type,
            punch_time=dt,
            reason=reason,
            user=request.user,
        )
        return Response(status_data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["get"])
    def punches(self, request, pk=None):
        """``GET /hrms/attendance/<id>/punches/`` -- punch timeline for attendance row."""
        att = self.get_object()
        punches_qs = att.punches.all().order_by("punch_time")
        return Response(AttendancePunchSerializer(punches_qs, many=True).data)

    @action(detail=False, methods=["get"], url_path="punch-timeline")
    def punch_timeline(self, request):
        """``GET /hrms/attendance/punch-timeline/?employeeId=...&date=...``."""
        employee_id = request.query_params.get("employeeId")
        date_str = request.query_params.get("date") or request.query_params.get("work_date")
        employee = self._resolve_employee(request, employee_id)
        if employee_id:
            self._own_or_team(employee)
        if not employee:
            return Response([])

        if date_str:
            try:
                work_date = datetime.strptime(str(date_str)[:10], "%Y-%m-%d").date()
            except ValueError:
                work_date = timezone.localdate()
        else:
            work_date = timezone.localdate()

        punches = AttendancePunch.objects.filter(
            client_id=request.client_id,
            employee=employee,
            work_date=work_date,
            deleted_at__isnull=True,
        ).order_by("punch_time")
        return Response(AttendancePunchSerializer(punches, many=True).data)


class RegularizationViewSet(OwnEmployeeScopeMixin, TenantModelViewSet):
    queryset = AttendanceRegularization.objects.select_related("employee")
    serializer_class = RegularizationSerializer
    audit_entity_type = "AttendanceRegularization"
    status_field = "status"
    default_date_field = "work_date"
    ordering = ["-work_date"]
    filter_map = {"employeeId": "employee_id"}
    team_scope_permissions = ("view_team_attendance",)
    permission_map = {"read": [], "create": [], "write": ["regularize_attendance"]}

    def perform_create(self, serializer):
        employee = serializer.validated_data.get("employee")
        if not employee:
            resolved = self._resolve_employee(self.request)
            if resolved:
                serializer.validated_data["employee"] = resolved
                employee = resolved
        if not employee:
            raise ValidationFailed(
                "No employee profile found or linked to this user account.",
                code="NO_EMPLOYEE",
            )
        self.check_own_employee(employee)
        return super().perform_create(serializer)

    @transaction.atomic
    def perform_update(self, serializer):
        """``{ approved, remark }`` -- an approval rewrites the attendance row."""
        approved = self.request.data.get("approved")
        instance = serializer.instance
        row = super().perform_update(serializer)

        if approved is not None:
            row.status = "Approved" if approved else "Rejected"
            row.approver = self.request.user
            row.decided_at = timezone.now()
            row.remark = self.request.data.get("remark", row.remark)
            row.save(update_fields=["status", "approver", "decided_at", "remark", "updated_at"])

            if approved:
                services.mark_attendance(
                    client=self.request.user.client,
                    employee=row.employee,
                    work_date=row.work_date,
                    check_in=row.requested_check_in,
                    check_out=row.requested_check_out,
                    status=row.requested_status,
                    remark=f"Regularized: {row.reason}",
                    source="regularization",
                    user=self.request.user,
                )
        return row


class FlexibilityPolicyView(APIView):
    """``GET/PUT /hrms/attendance/flexibility/``."""

    permission_classes = [HasModulePermission]
    permission_map = {"read": ["view_team_attendance"], "write": ["mark_attendance"]}

    def get(self, request):
        return Response(services.flexibility_policy(request.client_id))

    def put(self, request):
        from apps.core.models import Setting

        policy = services.flexibility_policy(request.client_id)
        policy.update(request.data if isinstance(request.data, dict) else {})
        Setting.objects.update_or_create(
            client_id=request.client_id,
            key="attendance_flexibility",
            defaults={"value": policy, "updated_by": request.user},
        )
        return Response(policy)


# ---------------------------------------------------------------------------
# Leave (api.md §11.3)
# ---------------------------------------------------------------------------
class LeaveTypeViewSet(TenantModelViewSet):
    queryset = LeaveType.objects.all()
    serializer_class = LeaveTypeSerializer
    audit_entity_type = "LeaveType"
    audit_label_field = "name"
    status_field = None
    ordering = ["name"]
    permission_map = {"read": [LEAVE_READERS], "write": ["approve_leave"]}


class LeaveRequestViewSet(OwnEmployeeScopeMixin, TenantModelViewSet):
    queryset = LeaveRequest.objects.select_related(
        "employee", "leave_type", "delegate_employee"
    )
    serializer_class = LeaveRequestSerializer
    audit_entity_type = "LeaveRequest"
    status_field = "status"
    default_date_field = "from_date"
    ordering = ["-from_date"]
    search_fields = ["employee__name", "reason"]
    filter_map = {"employeeId": "employee_id", "type": "leave_type__name", "leaveTypeId": "leave_type_id"}
    team_scope_permissions = ("approve_leave",)
    manager_read_scope = True
    delegate_read_field = "delegate_employee_id"
    permission_map = {"read": [LEAVE_READERS], "create": ["apply_leave"], "write": ["approve_leave"]}

    def get_aggregates(self, queryset):
        return queryset.aggregate(
            total=Count("id"),
            pending=Count("id", filter=Q(status__in=["Pending Review", "Delegate Confirmed"])),
            approved=Count("id", filter=Q(status="Approved")),
            rejected=Count("id", filter=Q(status="Rejected")),
        )

    def perform_create(self, serializer):
        employee = serializer.validated_data.get("employee")
        if not employee:
            resolved = self._resolve_employee(self.request)
            if resolved:
                serializer.validated_data["employee"] = resolved
                employee = resolved
        self.check_own_employee(employee)

        from apps.hrms.models import LeaveRequest, Resignation, Termination
        from apps.core.exceptions import Conflict, Codes

        if employee:
            if employee.status in ("Terminated", "Resigned"):
                raise Conflict(
                    f"Cannot apply for leave for {employee.name}: employee is {employee.status.lower()}.",
                    code="EMPLOYEE_EXITED",
                )
            exit_rec = (
                Resignation.objects.filter(
                    client_id=self.request.client_id, employee=employee, deleted_at__isnull=True
                ).exclude(status="Cancelled").order_by("-created_at").first() or
                Termination.objects.filter(
                    client_id=self.request.client_id, employee=employee, deleted_at__isnull=True
                ).exclude(status="Cancelled").order_by("-created_at").first()
            )
            if exit_rec and exit_rec.last_working_day:
                from_d = serializer.validated_data.get("from_date")
                to_d = serializer.validated_data.get("to_date")
                if (from_d and from_d > exit_rec.last_working_day) or (to_d and to_d > exit_rec.last_working_day):
                    raise Conflict(
                        f"Leave cannot be requested beyond effective exit date ({exit_rec.last_working_day}).",
                        code="EMPLOYEE_EXITED",
                    )

        from_date = serializer.validated_data.get("from_date")
        to_date = serializer.validated_data.get("to_date")
        if employee and from_date and to_date:
            conflict = (
                LeaveRequest.objects.filter(
                    client_id=self.request.client_id,
                    employee=employee,
                    deleted_at__isnull=True,
                    status__in=["Pending Review", "Delegate Confirmed", "Approved"],
                    from_date__lte=to_date,
                    to_date__gte=from_date,
                )
                .order_by("-created_at")
                .first()
            )
            if conflict:
                raise Conflict(
                    f"This overlaps an existing leave request ({conflict.from_date} to {conflict.to_date}).",
                    code=getattr(Codes, "OVERLAPPING_LEAVE", "OVERLAPPING_LEAVE"),
                )

        if not serializer.validated_data.get("days") and from_date and to_date:
            serializer.validated_data["days"] = max(1, (to_date - from_date).days + 1)

        request_row = super().perform_create(serializer)
        from . import workflow_service
        workflow_service.initialize_request_workflow(request_row, "Leave Management", self.request.user)
        return request_row

    @transaction.atomic
    def perform_update(self, serializer):
        """``{ approved, remark }`` or ``{ status }`` (api.md §11.3)."""
        approved = self.request.data.get("approved")
        target_status = self.request.data.get("status")
        instance = serializer.instance

        delegate_id = (
            self.request.data.get("delegateId")
            or self.request.data.get("delegate_employee")
            or self.request.data.get("delegate_employee_id")
        )
        if not delegate_id and self.request.data.get("delegate"):
            from apps.hrms.models import Employee
            delegate_name = self.request.data.get("delegate")
            emp = Employee.objects.filter(
                client_id=self.request.client_id,
                name__iexact=str(delegate_name).strip(),
                deleted_at__isnull=True,
            ).first()
            if emp:
                delegate_id = emp.id

        if delegate_id:
            instance.delegate_employee_id = delegate_id
            instance.save(update_fields=["delegate_employee_id"])

        if approved is True or target_status == "Approved":
            from . import workflow_service
            row, _ = workflow_service.process_approval_action(
                instance, "Leave Management", self.request.user, "approve",
                remark=self.request.data.get("remark"),
            )
            self._concurrency_instance = row
            return row

        if approved is False or target_status == "Rejected":
            from . import workflow_service
            row, _ = workflow_service.process_approval_action(
                instance, "Leave Management", self.request.user, "reject",
                remark=self.request.data.get("remark"),
            )
            self._concurrency_instance = row
            return row

        return super().perform_update(serializer)

    @action(detail=True, methods=["post"])
    def approve(self, request, pk=None):
        instance = self.get_object()
        from . import workflow_service
        updated, _ = workflow_service.process_approval_action(
            instance, "Leave Management", request.user, "approve",
            remark=request.data.get("remark"),
        )
        return Response(self.get_serializer(updated).data)

    @action(detail=True, methods=["post"])
    def reject(self, request, pk=None):
        instance = self.get_object()
        from . import workflow_service
        updated, _ = workflow_service.process_approval_action(
            instance, "Leave Management", request.user, "reject",
            remark=request.data.get("remark"),
        )
        return Response(self.get_serializer(updated).data)

    @action(detail=True, methods=["post"])
    def resubmit(self, request, pk=None):
        instance = self.get_object()
        from . import workflow_service
        updated, _ = workflow_service.process_approval_action(
            instance, "Leave Management", request.user, "resubmit",
            remark=request.data.get("remark"),
        )
        return Response(self.get_serializer(updated).data)

    @action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):
        row = services.cancel_leave(
            self.get_object(), user=request.user, reason=request.data.get("reason")
        )
        self.write_audit("cancel", row, description=request.data.get("reason"))
        return Response(self.get_serializer(row).data)

    @action(detail=True, methods=["post"], url_path="confirm-delegate")
    def confirm_delegate(self, request, pk=None):
        row = self.get_object()
        row.delegate_confirmed_at = timezone.now()
        row.status = "Delegate Confirmed"
        row.save(update_fields=["delegate_confirmed_at", "status", "updated_at"])
        return Response(self.get_serializer(row).data)

    @action(detail=False, methods=["post"], url_path="calculate-days")
    def calculate_days(self, request):
        """Calculates days preview with sandwich-leave detection (Spec §2.5.2)."""
        import datetime
        from datetime import timedelta
        from .models import Holiday, LeaveType

        from_date_str = request.data.get("fromDate") or request.data.get("from_date")
        to_date_str = request.data.get("toDate") or request.data.get("to_date")
        leave_type_id = request.data.get("leaveTypeId") or request.data.get("leave_type_id")

        if not from_date_str or not to_date_str:
            return Response({"error": "fromDate and toDate are required"}, status=400)

        start = datetime.date.fromisoformat(str(from_date_str)[:10])
        end = datetime.date.fromisoformat(str(to_date_str)[:10])
        if end < start:
            return Response({"error": "toDate must be on or after fromDate"}, status=400)

        leave_type = LeaveType.objects.filter(id=leave_type_id, client_id=request.client_id).first() if leave_type_id else None
        enforce_sandwich = bool(leave_type and leave_type.enforce_sandwich_rule)

        from apps.core.models import Setting
        hrms_settings = Setting.objects.filter(client_id=request.client_id, key="hrms").first()
        org_sandwich = (hrms_settings.value.get("sandwichRuleEnabled", True) if hrms_settings and isinstance(hrms_settings.value, dict) else True)
        enforce_sandwich = bool(enforce_sandwich and org_sandwich)

        employee_id = request.data.get("employeeId") or request.data.get("employee_id")
        employee = None
        if employee_id:
            from .models import Employee
            employee = Employee.objects.filter(client_id=request.client_id, pk=employee_id, deleted_at__isnull=True).first()
        elif getattr(request.user, "employee_id", None):
            from .models import Employee
            employee = Employee.objects.filter(client_id=request.client_id, pk=request.user.employee_id, deleted_at__isnull=True).first()

        schedule = services.get_employee_schedule(request.client_id, employee)

        total_calendar_days = (end - start).days + 1
        day_breakdown = []
        working_days = 0
        sandwich_days = 0

        curr = start
        while curr <= end:
            is_weekend = services.is_employee_weekly_off(request.client_id, curr, employee, schedule=schedule)
            is_holiday = services.is_employee_holiday(request.client_id, curr, employee)
            is_off = is_weekend or is_holiday
            if not is_off:
                working_days += 1
                counted = True
            else:
                counted = enforce_sandwich
                if enforce_sandwich:
                    sandwich_days += 1

            day_breakdown.append({
                "date": curr.isoformat(),
                "day_of_week": curr.strftime("%A"),
                "is_weekend": is_weekend,
                "is_holiday": is_holiday,
                "is_counted": counted
            })
            curr += timedelta(days=1)

        counted_days = total_calendar_days if enforce_sandwich else working_days

        return Response({
            "enforce_sandwich_rule": enforce_sandwich,
            "total_calendar_days": total_calendar_days,
            "working_days": working_days,
            "sandwich_penalty_days": sandwich_days if enforce_sandwich else 0,
            "chargeable_days": counted_days,
            "day_breakdown": day_breakdown
        })



class LeaveBalanceViewSet(OwnEmployeeScopeMixin, ReadOnlyTenantViewSet):
    queryset = LeaveBalance.objects.select_related("employee", "leave_type")
    serializer_class = LeaveBalanceSerializer
    status_field = None
    filter_map = {"employeeId": "employee_id", "leaveTypeId": "leave_type_id", "year": "period_year"}
    ordering = ["employee__name"]
    team_scope_permissions = ("approve_leave",)
    manager_read_scope = True
    permission_map = {"read": [LEAVE_READERS]}


class CompOffViewSet(OwnEmployeeScopeMixin, TenantModelViewSet):
    queryset = CompOff.objects.select_related("employee")
    serializer_class = CompOffSerializer
    audit_entity_type = "CompOff"
    status_field = None
    default_date_field = "worked_date"
    ordering = ["-worked_date"]
    filter_map = {"employeeId": "employee_id", "used": "used"}
    team_scope_permissions = ("approve_leave",)
    manager_read_scope = True
    permission_map = {"read": [LEAVE_READERS], "write": ["approve_leave"]}


class LeaveEncashmentViewSet(OwnEmployeeScopeMixin, TenantModelViewSet):
    queryset = LeaveEncashment.objects.select_related("employee", "leave_type")
    serializer_class = LeaveEncashmentSerializer
    audit_entity_type = "LeaveEncashment"
    status_field = "status"
    ordering = ["-created_at"]
    filter_map = {"employeeId": "employee_id"}
    team_scope_permissions = ("approve_leave",)
    manager_read_scope = True
    permission_map = {"read": [LEAVE_READERS], "write": ["approve_leave"]}


# ---------------------------------------------------------------------------
# Payroll (api.md §11.4)
# ---------------------------------------------------------------------------
class PayslipViewSet(TenantModelViewSet):
    queryset = Payslip.objects.select_related(
        "employee", "employee__department", "employee__designation", "bank_account"
    ).prefetch_related("components")
    serializer_class = PayslipSerializer
    audit_entity_type = "Payslip"
    status_field = "status"
    default_date_field = "period_month"
    ordering = ["-period_month", "employee__name"]
    search_fields = ["employee__name", "employee__employee_code"]
    filter_map = {
        "employeeId": "employee_id",
        "department": "employee__department__name",
        "month": "period_month",
    }
    permission_map = {
        "read": [PAY_READERS],
        "write": ["generate_payroll"],
        "approve": ["approve_payroll"],
        "mark_paid": ["approve_payroll"],
        # Tenant-wide totals.
        "summary": [("generate_payroll", "approve_payroll")],
    }
    http_method_names = ["get", "patch", "post", "head", "options"]

    def get_queryset(self):
        queryset = super().get_queryset()
        # Without the team-wide permission, a user sees only their own payslips.
        if not has_permission(self.request.user, "generate_payroll") and not has_permission(
            self.request.user, "approve_payroll"
        ):
            employee_id = getattr(self.request.user, "employee_id", None)
            queryset = queryset.filter(employee_id=employee_id) if employee_id else queryset.none()
        return queryset

    def get_aggregates(self, queryset):
        user = self.request.user
        if has_permission(user, ("generate_payroll", "approve_payroll")):
            return services.payroll_summary(self.get_client_id())
        # Totals of the caller's own payslips, not the company payroll.
        return services.payroll_summary(self.get_client_id(), queryset=queryset)

    def perform_update(self, serializer):
        """Adjust earnings / deductions before approval (api.md §11.4)."""
        payslip = super().perform_update(serializer)
        override = self.request.data.get("earnedSalary")
        services.recompute_payslip(
            payslip,
            earned_salary_override=override if override is not None else None,
            user=self.request.user,
        )
        payslip.refresh_from_db()
        return payslip

    @action(detail=True, methods=["post"])
    def approve(self, request, pk=None):
        payslip = self.get_object()
        if payslip.status == "Paid":
            raise Conflict("This payslip is already paid.", code=Codes.ALREADY_DONE)
        payslip.status = "Approved"
        payslip.save(update_fields=["status", "updated_at"])
        self.write_audit("approve", payslip, description="Payslip approved")
        return Response(self.get_serializer(payslip).data)

    @action(detail=True, methods=["post"], url_path="mark-paid")
    def mark_paid(self, request, pk=None):
        from apps.accounting.models import BankAccount

        bank_account = None
        bank_account_id = request.data.get("bankAccountId")
        if bank_account_id:
            bank_account = BankAccount.objects.filter(
                pk=bank_account_id, client_id=request.client_id, deleted_at__isnull=True
            ).first()

        payslip = services.mark_payslip_paid(
            self.get_object(),
            payment_date=request.data.get("paymentDate"),
            bank_account=bank_account,
            user=request.user,
        )
        self.write_audit("mark_paid", payslip, description="Payslip marked paid")
        return Response(self.get_serializer(payslip).data)

    @action(detail=True, methods=["get"])
    def payslip(self, request, pk=None):
        row = self.get_object()
        return Response(
            print_payload(row, self.get_serializer_class(), request=request, title="Payslip")
        )

    @action(detail=False, methods=["post"])
    def process(self, request):
        """``POST /hrms/payroll/process/`` -- generate the run."""
        from apps.core.permissions import require_permission

        require_permission(request.user, "generate_payroll")

        serializer = ProcessPayrollSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        result = services.process_payroll(
            client=request.user.client,
            period_month=serializer.validated_data["month"],
            employee_ids=serializer.validated_data.get("employeeIds") or None,
            user=request.user,
        )
        return Response(
            {
                "run": PayrollRunSerializer(result["run"]).data,
                "payslips": PayslipSerializer(result["payslips"], many=True).data,
            },
            status=status.HTTP_201_CREATED,
        )

    @action(detail=False, methods=["get"])
    def summary(self, request):
        month = request.query_params.get("month")
        parsed = None
        if month:
            try:
                parsed = datetime.strptime(month[:7], "%Y-%m").date()
            except ValueError:
                raise ValidationFailed(
                    "Month must be YYYY-MM.", field_errors={"month": ["Expected YYYY-MM."]}
                )
        return Response(services.payroll_summary(request.client_id, parsed))


class SalaryStructureViewSet(TenantModelViewSet):
    queryset = SalaryStructure.objects.all()
    serializer_class = SalaryStructureSerializer
    audit_entity_type = "SalaryStructure"
    audit_label_field = "name"
    status_field = None
    ordering = ["name"]
    # The pay templates are company data, not the employee's own slip.
    permission_map = {"read": [PAY_MANAGERS], "write": ["edit_salary_structure"]}


class SalaryAdvanceViewSet(OwnEmployeeScopeMixin, TenantModelViewSet):
    queryset = SalaryAdvance.objects.select_related("employee")
    serializer_class = SalaryAdvanceSerializer
    audit_entity_type = "SalaryAdvance"
    status_field = "status"
    default_date_field = "issued_on"
    ordering = ["-issued_on"]
    filter_map = {"employeeId": "employee_id"}
    # Same rule as payslips: team-wide pay needs a payroll permission.
    team_scope_permissions = ("generate_payroll", "approve_payroll")
    permission_map = {"read": [PAY_READERS], "write": ["generate_payroll"]}


# ---------------------------------------------------------------------------
# Recruitment (api.md §11.5)
# ---------------------------------------------------------------------------
class JobViewSet(TenantModelViewSet):
    queryset = Job.objects.select_related("department", "designation", "location")
    serializer_class = JobSerializer
    audit_entity_type = "Job"
    audit_label_field = "title"
    status_field = "status"
    search_fields = ["title", "description"]
    ordering = ["-created_at"]
    filter_map = {"departmentId": "department_id", "isPublished": "is_published", "status": "status"}
    permission_map = {"read": ["view_staff"], "write": ["create_staff"]}

    def get_queryset(self):
        return super().get_queryset().annotate(
            applicant_count=Count("applications", distinct=True)
        )

    def perform_create(self, serializer):
        from django.utils.text import slugify

        job = super().perform_create(serializer)
        if not job.slug:
            base = slugify(job.title)[:120] or "role"
            slug, suffix = base, 1
            while Job.objects.filter(
                client_id=self.get_client_id(), slug=slug, deleted_at__isnull=True
            ).exclude(pk=job.pk).exists():
                suffix += 1
                slug = f"{base}-{suffix}"
            job.slug = slug
            job.save(update_fields=["slug"])
        return job

    @action(detail=True, methods=["post"])
    def publish(self, request, pk=None):
        job = self.get_object()
        job.is_published = True
        job.published_at = timezone.now()
        job.save(update_fields=["is_published", "published_at", "updated_at"])
        return Response(self.get_serializer(job).data)


def hire_candidate_to_employee(candidate, user=None, client_id=None):
    """
    Unified transactional helper to convert candidate -> employee.
    Idempotent: prevents duplicate employee creation across retries and concurrent calls.
    Maps: name, email, phone, joining date, department, designation, location, employment type, salary, manager.
    """
    if candidate.employee_id:
        return candidate.employee

    client_id = client_id or candidate.client_id
    existing_emp = None
    if candidate.email:
        existing_emp = Employee.objects.filter(
            client_id=client_id, email__iexact=candidate.email.strip(), deleted_at__isnull=True
        ).first()
    if not existing_emp and candidate.phone:
        existing_emp = Employee.objects.filter(
            client_id=client_id, phone=candidate.phone.strip(), deleted_at__isnull=True
        ).first()

    if existing_emp:
        candidate.employee = existing_emp
        candidate.stage = "Hired"
        candidate.is_onboarded = True
        candidate.save(update_fields=["employee", "stage", "is_onboarded", "updated_at"])
        return existing_emp

    # Resolve latest offer
    offer = (
        Offer.objects.filter(
            Q(candidate=candidate) | Q(application__candidate=candidate),
            status="Accepted",
            deleted_at__isnull=True,
        )
        .order_by("-created_at")
        .first()
    )
    if not offer:
        offer = (
            Offer.objects.filter(
                Q(candidate=candidate) | Q(application__candidate=candidate),
                deleted_at__isnull=True,
            )
            .order_by("-created_at")
            .first()
        )

    # Resolve application and job
    application = (
        Application.objects.filter(candidate=candidate, deleted_at__isnull=True)
        .select_related("job", "job__department", "job__designation", "job__location")
        .order_by("-applied_at")
        .first()
    )

    # Resolve Department
    dept_obj = None
    if application and application.job and application.job.department:
        dept_obj = application.job.department
    elif offer and offer.department:
        dept_obj = Department.objects.filter(
            client_id=client_id, name__iexact=str(offer.department).strip(), deleted_at__isnull=True
        ).first()

    # Resolve Designation
    desig_obj = None
    if application and application.job and application.job.designation:
        desig_obj = application.job.designation
    elif offer and offer.position:
        desig_obj = Designation.objects.filter(
            client_id=client_id, name__iexact=str(offer.position).strip(), deleted_at__isnull=True
        ).first()

    # Resolve Location
    loc_obj = None
    if application and application.job and application.job.location:
        loc_obj = application.job.location
    elif offer and offer.location:
        loc_obj = Location.objects.filter(
            client_id=client_id, name__iexact=str(offer.location).strip(), deleted_at__isnull=True
        ).first()

    # Resolve Reporting Manager
    mgr_obj = None
    if offer and offer.reporting_manager:
        mgr_name = str(offer.reporting_manager).strip()
        mgr_obj = Employee.objects.filter(
            client_id=client_id, deleted_at__isnull=True
        ).filter(Q(name__iexact=mgr_name) | Q(employee_code__iexact=mgr_name)).first()
    hiring_mgr = getattr(application.job, "hiring_manager", None) if application and application.job else None
    if not mgr_obj and hiring_mgr:
        hm_name = str(hiring_mgr).strip()
        mgr_obj = Employee.objects.filter(
            client_id=client_id, deleted_at__isnull=True
        ).filter(Q(name__iexact=hm_name) | Q(employee_code__iexact=hm_name)).first()

    # Calculate salary
    salary = ZERO
    if offer and offer.offered_ctc:
        salary = offer.offered_ctc / 12
    elif application and application.job and application.job.salary_range:
        pass

    # Resolve employment type
    emp_type = None
    if offer and offer.job_type in dict(EMPLOYMENT_TYPES):
        emp_type = offer.job_type
    elif application and application.job and application.job.employment_type in dict(EMPLOYMENT_TYPES):
        emp_type = application.job.employment_type
    else:
        emp_type = "Full-time"

    client_obj = getattr(user, "client", None)
    if not client_obj:
        from apps.accounts.models import Client
        client_obj = Client.objects.filter(pk=client_id).first()

    from apps.core.models import Setting
    from .lifecycle_service import calculate_probation_end_date, parse_duration_to_days

    hrms_row = Setting.objects.filter(client_id=client_id, key="hrms").first()
    hrms_config = dict(DEFAULT_HRMS_SETTINGS)
    if hrms_row and isinstance(hrms_row.value, dict):
        hrms_config.update(hrms_row.value)

    probation_cfg = (offer.probation_period if offer and offer.probation_period else None) or hrms_config.get("probationPeriod", "90 Days (3 Months)")
    notice_cfg = hrms_config.get("noticePeriod", "60 Days (2 Months)")
    joining_dt = offer.joining_date if offer and offer.joining_date else timezone.localdate()
    prob_end_dt = calculate_probation_end_date(joining_dt, probation_cfg)
    notice_days = parse_duration_to_days(notice_cfg, default=60)

    employee = Employee.objects.create(
        client_id=client_id,
        employee_code=allocate_number(client_obj, "EMP"),
        name=candidate.name,
        email=candidate.email,
        phone=candidate.phone,
        joining_date=joining_dt,
        department=dept_obj,
        designation=desig_obj,
        location=loc_obj,
        manager=mgr_obj,
        employment_type=emp_type,
        standard_salary=salary,
        status="Probation",
        probation_start_date=joining_dt,
        probation_period=probation_cfg,
        probation_end_date=prob_end_dt,
        confirmation_status="On Probation",
        notice_period=notice_cfg,
        notice_period_days=notice_days,
        created_by=user,
    )
    candidate.employee = employee
    candidate.stage = "Hired"
    candidate.is_onboarded = True
    candidate.save(update_fields=["employee", "stage", "is_onboarded", "updated_at"])

    if application and application.stage != "Hired":
        application.stage = "Hired"
        application.save(update_fields=["stage", "updated_at"])

    if employee.email:
        from apps.accounts.models import User
        login = User.objects.filter(
            client_id=client_id, email=employee.email.strip().lower(),
            deleted_at__isnull=True,
        ).first()
        if login is not None and not user_link.linked_employee(login):
            login.employee = employee
            login.save(update_fields=["employee", "updated_at"])
            user_link.reconcile(login, employee)

    record_audit(
        client=client_id,
        actor=user,
        action="onboard",
        entity_type="Employee",
        entity_id=employee.id,
        entity_label=employee.employee_code,
        description=f"Hired from candidate {candidate.name}",
    )
    return employee


class CandidateViewSet(TenantModelViewSet):
    queryset = Candidate.objects.select_related("resume_file", "employee")
    serializer_class = CandidateSerializer
    audit_entity_type = "Candidate"
    audit_label_field = "name"
    status_field = "stage"
    search_fields = ["name", "email", "phone"]
    ordering = ["-created_at"]
    filter_map = {"stage": "stage", "source": "source", "isOnboarded": "is_onboarded"}
    permission_map = {"read": ["view_staff"], "write": ["create_staff"]}

    @action(detail=True, methods=["post"])
    @transaction.atomic
    def hire(self, request, pk=None):
        candidate = self.get_object()
        employee = hire_candidate_to_employee(candidate, user=request.user, client_id=request.client_id)
        return Response(EmployeeSerializer(employee).data, status=status.HTTP_201_CREATED)


class ApplicationViewSet(TenantModelViewSet):
    queryset = Application.objects.select_related("candidate", "job")
    serializer_class = ApplicationSerializer
    audit_entity_type = "Application"
    status_field = "stage"
    ordering = ["-applied_at"]
    filter_map = {"jobId": "job_id", "candidateId": "candidate_id"}
    permission_map = {"read": ["view_staff"], "write": ["create_staff"]}


class InterviewViewSet(TenantModelViewSet):
    queryset = Interview.objects.select_related("application", "application__candidate")
    serializer_class = InterviewSerializer
    audit_entity_type = "Interview"
    status_field = "status"
    ordering = ["scheduled_at"]
    filter_map = {"applicationId": "application_id"}
    permission_map = {"read": ["view_staff"], "write": ["create_staff"]}

    @action(detail=True, methods=["post"], url_path="generate-zoom-meeting")
    def generate_zoom_meeting(self, request, pk=None):
        """Generates or attaches a Zoom meeting link to the interview (Spec §2.5.5)."""
        import random
        interview = self.get_object()
        meeting_id = f"{random.randint(100, 999)} {random.randint(1000, 9999)} {random.randint(1000, 9999)}"
        passcode = "".join(random.choices("0123456789abcdef", k=6))
        zoom_link = f"https://zoom.us/j/{meeting_id.replace(' ', '')}?pwd={passcode}"

        interview.location = zoom_link
        note_text = f"Zoom Meeting ID: {meeting_id} | Passcode: {passcode} | Link: {zoom_link}"
        if interview.notes:
            interview.notes = f"{interview.notes}\n{note_text}"
        else:
            interview.notes = note_text
        interview.save(update_fields=["location", "notes", "updated_at"])

        return Response({
            "zoom_link": zoom_link,
            "meeting_id": meeting_id,
            "passcode": passcode,
            "interview": self.get_serializer(interview).data
        })



class OfferViewSet(TenantModelViewSet):
    queryset = Offer.objects.select_related("application", "application__candidate", "candidate", "approval_chain")
    serializer_class = OfferSerializer
    audit_entity_type = "Offer"
    status_field = "status"
    ordering = ["-created_at"]
    filter_map = {"applicationId": "application_id", "candidateId": "candidate_id", "status": "status"}
    permission_map = {"read": ["view_staff"], "write": ["create_staff", "edit_staff"]}

    def perform_create(self, serializer):
        initial_status = serializer.validated_data.get("status") or "Draft"
        if initial_status == "Sent":
            serializer.validated_data["sent_at"] = timezone.now()
        elif initial_status in ("Accepted", "Declined"):
            serializer.validated_data["responded_at"] = timezone.now()

        offer = super().perform_create(serializer)

        candidate = getattr(offer, "candidate", None) or (
            offer.application.candidate if offer.application else None
        )
        if candidate:
            target_stage = "Hired" if offer.status == "Accepted" else "Offer"
            if candidate.stage != target_stage:
                candidate.stage = target_stage
                candidate.save(update_fields=["stage", "updated_at"])
            if offer.application and offer.application.stage != target_stage:
                offer.application.stage = target_stage
                offer.application.save(update_fields=["stage", "updated_at"])

        from . import workflow_service
        workflow_service.initialize_request_workflow(offer, "Offer Letters", self.request.user)
        return offer

    def perform_update(self, serializer):
        previous = serializer.instance.status
        offer = super().perform_update(serializer)
        if previous != offer.status:
            update_fields = ["updated_at"]
            if offer.status == "Sent" and offer.sent_at is None:
                offer.sent_at = timezone.now()
                update_fields.append("sent_at")
            if offer.status in ("Accepted", "Declined") and offer.responded_at is None:
                offer.responded_at = timezone.now()
                update_fields.append("responded_at")
            offer.save(update_fields=update_fields)

            candidate = getattr(offer, "candidate", None) or (
                offer.application.candidate if offer.application else None
            )
            if candidate:
                if offer.status == "Accepted":
                    candidate.stage = "Hired"
                    candidate.save(update_fields=["stage", "updated_at"])
                    if offer.application:
                        offer.application.stage = "Hired"
                        offer.application.save(update_fields=["stage", "updated_at"])
                elif offer.status in ("Sent", "Pending"):
                    if candidate.stage not in ("Offer", "Hired"):
                        candidate.stage = "Offer"
                        candidate.save(update_fields=["stage", "updated_at"])
                elif offer.status in ("Declined", "Rejected", "Revoked"):
                    other_offers = Offer.objects.filter(
                        client_id=offer.client_id,
                        deleted_at__isnull=True,
                        status__in=["Sent", "Pending", "Accepted"],
                    ).filter(Q(candidate=candidate) | Q(application__candidate=candidate)).exclude(pk=offer.pk).exists()
                    if not other_offers:
                        candidate.stage = "Rejected"
                        candidate.save(update_fields=["stage", "updated_at"])
        return offer

    @action(detail=True, methods=["post"])
    def send_offer(self, request, pk=None):
        offer = self.get_object()
        offer.status = "Sent"
        offer.sent_at = timezone.now()
        offer.save(update_fields=["status", "sent_at", "updated_at"])
        candidate = getattr(offer, "candidate", None) or (
            offer.application.candidate if offer.application else None
        )
        if candidate and candidate.stage != "Offer":
            candidate.stage = "Offer"
            candidate.save(update_fields=["stage", "updated_at"])
        self.write_audit(
            action="send_offer",
            instance=offer,
            description=f"Offer letter sent to {offer.candidate_name or 'candidate'}.",
            after={"status": "Sent", "sent_at": offer.sent_at.isoformat()},
        )
        return Response(self.get_serializer(offer).data)

    @action(detail=True, methods=["post"])
    def accept_offer(self, request, pk=None):
        offer = self.get_object()
        offer.status = "Accepted"
        offer.responded_at = timezone.now()
        offer.save(update_fields=["status", "responded_at", "updated_at"])
        candidate = getattr(offer, "candidate", None) or (
            offer.application.candidate if offer.application else None
        )
        if candidate:
            candidate.stage = "Hired"
            candidate.save(update_fields=["stage", "updated_at"])
            if offer.application:
                offer.application.stage = "Hired"
                offer.application.save(update_fields=["stage", "updated_at"])
        self.write_audit(
            action="accept_offer",
            instance=offer,
            description=f"Offer accepted by {offer.candidate_name or 'candidate'}.",
            after={"status": "Accepted", "responded_at": offer.responded_at.isoformat()},
        )
        return Response(self.get_serializer(offer).data)

    @action(detail=True, methods=["post"])
    def approve(self, request, pk=None):
        instance = self.get_object()
        from . import workflow_service
        updated, is_final = workflow_service.process_approval_action(
            instance, "Offer Letters", request.user, "approve",
            remark=request.data.get("remark"),
            target_status="Sent" if is_final else None,
        )
        return Response(self.get_serializer(updated).data)

    @action(detail=True, methods=["post"])
    def reject(self, request, pk=None):
        instance = self.get_object()
        from . import workflow_service
        updated, is_final = workflow_service.process_approval_action(
            instance, "Offer Letters", request.user, "reject",
            remark=request.data.get("remark") or request.data.get("reason"),
            target_status="Rejected",
        )
        return Response(self.get_serializer(updated).data)

    @action(detail=True, methods=["get"])
    def letter(self, request, pk=None):
        offer = self.get_object()
        return Response(
            print_payload(
                offer, self.get_serializer_class(), request=request, title="Offer Letter"
            )
        )


class OnboardingViewSet(TenantModelViewSet):
    queryset = OnboardingTask.objects.select_related("candidate", "owner")
    serializer_class = OnboardingTaskSerializer
    audit_entity_type = "OnboardingTask"
    status_field = None
    ordering = ["due_date"]
    filter_map = {"candidateId": "candidate_id"}
    permission_map = {"read": ["view_staff"], "write": ["create_staff"]}

    @action(
        detail=False, methods=["post"], url_path=r"(?P<candidate_id>[^/.]+)/complete"
    )
    @transaction.atomic
    def complete(self, request, candidate_id=None):
        """Convert candidate -> employee, keeping the hiring trail (db.md §11.5)."""
        candidate = Candidate.objects.filter(
            pk=candidate_id, client_id=request.client_id, deleted_at__isnull=True
        ).first()
        if candidate is None:
            raise NotFound("That candidate no longer exists.")
        if candidate.employee_id:
            return Response(
                EmployeeSerializer(candidate.employee).data, status=status.HTTP_200_OK
            )

        employee = hire_candidate_to_employee(candidate, user=request.user, client_id=request.client_id)
        return Response(
            EmployeeSerializer(employee).data, status=status.HTTP_201_CREATED
        )

    @action(
        detail=False,
        methods=["post"],
        url_path=r"(?P<candidate_id>[^/.]+)/verify-documents",
    )
    def verify_documents(self, request, candidate_id=None):
        candidate = Candidate.objects.filter(
            pk=candidate_id, client_id=request.client_id, deleted_at__isnull=True
        ).first()
        if candidate is None:
            raise NotFound("That candidate no longer exists.")
        candidate.documents_verified = True
        candidate.save(update_fields=["documents_verified", "updated_at"])
        OnboardingTask.objects.filter(
            client_id=request.client_id, candidate=candidate,
            title__icontains="document", completed_at__isnull=True,
        ).update(completed_at=timezone.now())
        return Response({"verified": True, "documentsVerified": True})


# Hidden: Screening Questions out of scope (Sweven spec) -- restore by uncommenting this block.
# class ScreeningQuestionViewSet(TenantModelViewSet):
#     queryset = ScreeningQuestion.objects.select_related("job")
#     serializer_class = ScreeningQuestionSerializer
#     audit_entity_type = "ScreeningQuestion"
#     status_field = None
#     ordering = ["sort_order"]
#     filter_map = {"jobId": "job_id", "isActive": "is_active"}
#     permission_map = {"read": ["view_staff"], "write": ["create_staff"]}


class RecruitmentFunnelView(APIView):
    permission_classes = [HasModulePermission]
    required_permissions = ["view_staff"]

    def get(self, request, section=None):
        client_id = request.client_id
        applications = Application.objects.filter(
            client_id=client_id, deleted_at__isnull=True
        )
        rows = applications.values("stage").annotate(count=Count("id"))
        funnel = {row["stage"]: row["count"] for row in rows}

        candidates = Candidate.objects.filter(
            client_id=client_id, deleted_at__isnull=True
        )
        cand_rows = candidates.values("stage").annotate(count=Count("id"))
        cand_funnel = {row["stage"]: row["count"] for row in cand_rows}

        # Combine or fallback if applications table has fewer/no records
        stages = ["Applied", "Screening", "Shortlisted", "Interview", "Offer", "Hired", "Rejected"]
        combined_funnel = {}
        for st in stages:
            combined_funnel[st] = max(funnel.get(st, 0), cand_funnel.get(st, 0))

        if section == "funnel":
            return Response(
                envelope(
                    [
                        {"stage": stage, "count": combined_funnel.get(stage, 0)}
                        for stage in stages
                    ]
                )
            )

        jobs = Job.objects.filter(client_id=client_id, deleted_at__isnull=True)
        total_apps = max(applications.count(), candidates.count())
        return Response(
            {
                "openJobs": jobs.filter(status="Open").count(),
                "totalApplications": total_apps,
                "interviewsScheduled": Interview.objects.filter(
                    client_id=client_id, status="Scheduled", deleted_at__isnull=True
                ).count(),
                "offersOut": Offer.objects.filter(
                    client_id=client_id, status="Sent", deleted_at__isnull=True
                ).count(),
                "hired": combined_funnel.get("Hired", 0),
                "funnel": combined_funnel,
            }
        )


# ---------------------------------------------------------------------------
# Performance (api.md §11.6)
# ---------------------------------------------------------------------------
class AppraisalCycleViewSet(TenantModelViewSet):
    queryset = AppraisalCycle.objects.all()
    serializer_class = AppraisalCycleSerializer
    audit_entity_type = "AppraisalCycle"
    audit_label_field = "name"
    status_field = "status"
    ordering = ["-period_start"]
    permission_map = {"read": ["view_staff"], "write": ["edit_staff"]}


class PerformanceIndicatorViewSet(TenantModelViewSet):
    queryset = PerformanceIndicator.objects.select_related("department")
    serializer_class = PerformanceIndicatorSerializer
    audit_entity_type = "PerformanceIndicator"
    audit_label_field = "name"
    status_field = None
    ordering = ["name"]
    permission_map = {"read": ["view_staff"], "write": ["edit_staff"]}


class KpiViewSet(TenantModelViewSet):
    queryset = Kpi.objects.select_related("indicator")
    serializer_class = KpiSerializer
    audit_entity_type = "Kpi"
    audit_label_field = "name"
    status_field = None
    ordering = ["name"]
    permission_map = {"read": ["view_staff"], "write": ["edit_staff"]}


class AppraisalViewSet(TenantModelViewSet):
    queryset = Appraisal.objects.select_related(
        "cycle", "employee", "manager"
    ).prefetch_related("kpi_scores__kpi", "history__actor")
    serializer_class = AppraisalSerializer
    audit_entity_type = "Appraisal"
    status_field = "status"
    ordering = ["-created_at"]
    filter_map = {"cycleId": "cycle_id", "employeeId": "employee_id", "stage": "stage"}
    permission_map = {"read": ["view_staff"], "write": ["edit_staff"]}

    def _transition(self, appraisal, *, to_stage, to_status, action_name, comment=None,
                    require=None):
        """Every transition appends to ``history[]`` with actor, stage, timestamp
        and comment (api.md §11.6)."""
        if require and not has_permission(self.request.user, require):
            raise PermissionDenied(
                "You don't have permission to do that.", code=require
            )

        from_stage = appraisal.stage
        appraisal.stage = to_stage
        appraisal.status = to_status
        appraisal.save(update_fields=["stage", "status", "updated_at"])

        AppraisalHistory.objects.create(
            client_id=appraisal.client_id,
            appraisal=appraisal,
            from_stage=from_stage,
            to_stage=to_stage,
            actor=self.request.user,
            action=action_name,
            comment=comment,
        )
        return appraisal

    @action(detail=True, methods=["post"], url_path="self-review")
    def self_review(self, request, pk=None):
        appraisal = self.get_object()
        appraisal.self_rating = request.data.get("rating", appraisal.self_rating)
        appraisal.self_comments = request.data.get("comments", appraisal.self_comments)
        appraisal.strengths = request.data.get("strengths", appraisal.strengths)
        appraisal.areas_for_improvement = request.data.get(
            "areasForImprovement", appraisal.areas_for_improvement
        )
        appraisal.save()
        self._transition(
            appraisal, to_stage="Manager Review", to_status="Submitted",
            action_name="self_review_submitted",
            comment=request.data.get("comments"),
        )
        return Response(self.get_serializer(appraisal).data)

    @action(detail=True, methods=["post"], url_path="manager-review")
    def manager_review(self, request, pk=None):
        appraisal = self.get_object()
        appraisal.manager_rating = request.data.get("rating", appraisal.manager_rating)
        appraisal.development_feedback = request.data.get(
            "developmentFeedback", appraisal.development_feedback
        )
        appraisal.save()

        from .models import AppraisalKpiScore

        for score in request.data.get("kpiScores") or []:
            kpi_id = score.get("kpiId")
            if not kpi_id:
                continue
            AppraisalKpiScore.objects.update_or_create(
                appraisal=appraisal,
                kpi_id=kpi_id,
                defaults={
                    "client_id": appraisal.client_id,
                    "target": score.get("target"),
                    "achieved": score.get("achieved"),
                    "score": score.get("score"),
                    "weight": score.get("weight"),
                },
            )

        self._transition(
            appraisal, to_stage="HR Review", to_status="In Progress",
            action_name="manager_review_submitted",
            comment=request.data.get("comments"),
        )
        return Response(self.get_serializer(appraisal).data)

    @action(detail=True, methods=["post"], url_path="return")
    def return_to_stage(self, request, pk=None):
        appraisal = self.get_object()
        target = request.data.get("returnToStage") or "Self Review"
        self._transition(
            appraisal, to_stage=target, to_status="Returned",
            action_name="returned", comment=request.data.get("reason"),
        )
        return Response(self.get_serializer(appraisal).data)

    @action(detail=True, methods=["post"])
    def approve(self, request, pk=None):
        appraisal = self.get_object()
        appraisal.hr_comments = request.data.get("hrComments", appraisal.hr_comments)
        appraisal.save(update_fields=["hr_comments", "updated_at"])
        self._transition(
            appraisal, to_stage="Finalization", to_status="Approved",
            action_name="hr_approved", comment=request.data.get("hrComments"),
            require="edit_staff",
        )
        return Response(self.get_serializer(appraisal).data)

    @action(detail=True, methods=["post"])
    @transaction.atomic
    def finalize(self, request, pk=None):
        """Close and sync the rating to the profile (api.md §11.6)."""
        appraisal = self.get_object()
        appraisal.final_rating = request.data.get(
            "finalRating", appraisal.manager_rating or appraisal.self_rating
        )
        appraisal.save(update_fields=["final_rating", "updated_at"])
        self._transition(
            appraisal, to_stage="Finalization", to_status="Completed",
            action_name="finalized", require="edit_staff",
        )
        return Response(self.get_serializer(appraisal).data)

    @action(detail=True, methods=["get"])
    def history(self, request, pk=None):
        from .serializers import AppraisalHistorySerializer

        appraisal = self.get_object()
        return Response(
            envelope(
                AppraisalHistorySerializer(appraisal.history.all(), many=True).data
            )
        )


class GoalViewSet(TenantModelViewSet):
    queryset = Goal.objects.select_related("employee", "cycle")
    serializer_class = GoalSerializer
    audit_entity_type = "Goal"
    audit_label_field = "title"
    status_field = "status"
    ordering = ["target_date"]
    filter_map = {"employeeId": "employee_id", "cycleId": "cycle_id"}
    permission_map = {"read": ["view_staff"], "write": ["edit_staff"]}


class PerformanceDashboardView(APIView):
    permission_classes = [HasModulePermission]
    required_permissions = ["view_staff"]

    def get(self, request):
        appraisals = Appraisal.objects.filter(
            client_id=request.client_id, deleted_at__isnull=True
        )
        by_stage = appraisals.values("stage").annotate(count=Count("id"))
        by_status = appraisals.values("status").annotate(count=Count("id"))
        return Response(
            {
                "total": appraisals.count(),
                "completed": appraisals.filter(status="Completed").count(),
                "byStage": {row["stage"]: row["count"] for row in by_stage},
                "byStatus": {row["status"]: row["count"] for row in by_status},
                "goalsOnTrack": Goal.objects.filter(
                    client_id=request.client_id, deleted_at__isnull=True,
                    progress_pct__gte=50,
                ).count(),
            }
        )


# ---------------------------------------------------------------------------
# Training (api.md §11.7)
# ---------------------------------------------------------------------------
class TrainerViewSet(TenantModelViewSet):
    queryset = Trainer.objects.select_related("employee")
    serializer_class = TrainerSerializer
    audit_entity_type = "Trainer"
    audit_label_field = "name"
    status_field = None
    ordering = ["name"]
    permission_map = {"read": ["view_staff"], "write": ["edit_staff"]}


class TrainingViewSet(TenantModelViewSet):
    queryset = Training.objects.select_related("trainer", "department").prefetch_related(
        "participants__employee"
    )
    serializer_class = TrainingSerializer
    audit_entity_type = "Training"
    audit_label_field = "title"
    status_field = "stage"
    default_date_field = "start_date"
    ordering = ["-start_date"]
    search_fields = ["title", "description", "venue"]
    permission_map = {"read": ["view_staff"], "write": ["edit_staff"]}

    @action(detail=True, methods=["post"], url_path="assign-trainer")
    def assign_trainer(self, request, pk=None):
        training = self.get_object()
        trainer_id = request.data.get("trainerId")
        trainer = Trainer.objects.filter(
            pk=trainer_id, client_id=request.client_id, deleted_at__isnull=True
        ).first()
        if trainer is None:
            raise ValidationFailed(
                "Unknown trainer.", field_errors={"trainerId": ["Not found."]}
            )
        training.trainer = trainer
        training.type = request.data.get("trainerType") or trainer.kind
        if training.stage == "Requested":
            training.stage = "Trainer Assigned"
        training.save(update_fields=["trainer", "type", "stage", "updated_at"])
        return Response(self.get_serializer(training).data)

    @action(detail=True, methods=["post"])
    def participants(self, request, pk=None):
        training = self.get_object()
        employee_ids = request.data.get("employeeIds") or []
        rows = TrainingParticipant.objects.bulk_create(
            [
                TrainingParticipant(
                    client_id=request.client_id, training=training, employee_id=employee_id
                )
                for employee_id in employee_ids
            ],
            ignore_conflicts=True,
        )
        return Response({"enrolled": len(rows)}, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"])
    def evaluate(self, request, pk=None):
        training = self.get_object()
        participant = training.participants.filter(
            pk=request.data.get("participantId"), deleted_at__isnull=True
        ).first()
        if participant is None:
            raise NotFound("That participant is not on this training.")
        participant.rating = request.data.get("rating")
        participant.feedback = request.data.get("feedback")
        participant.evaluated_at = timezone.now()
        participant.save()

        if not training.participants.filter(evaluated_at__isnull=True).exists():
            training.stage = "Evaluated"
            training.save(update_fields=["stage", "updated_at"])
        return Response(TrainingParticipantSerializer(participant).data)

    @action(detail=False, methods=["get"])
    def funnel(self, request):
        rows = self.get_queryset().values("stage").annotate(count=Count("id"))
        counts = {row["stage"]: row["count"] for row in rows}
        stages = [stage for stage, _ in TRAINING_STAGES]
        return Response(
            envelope(
                [{"stage": stage, "count": counts.get(stage, 0)} for stage in stages]
            )
        )

    @action(detail=False, methods=["get"])
    def dashboard(self, request):
        from django.db.models import Q

        queryset = self.get_queryset()
        participant_count = TrainingParticipant.objects.filter(
            client_id=request.client_id, deleted_at__isnull=True
        ).aggregate(total=Count("id"))["total"] or 0
        aggregates = queryset.aggregate(
            total=Count("id"),
            ongoing=Count("id", filter=Q(stage="Ongoing")),
            completed=Count(
                "id", filter=Q(stage__in=["Completed", "Evaluated"])
            ),
        )
        return Response(
            {
                "total": aggregates["total"] or 0,
                "ongoing": aggregates["ongoing"] or 0,
                "completed": aggregates["completed"] or 0,
                "participants": participant_count,
            }
        )


# ---------------------------------------------------------------------------
# Assets, documents, policies, calendar, HR admin (api.md §11.8)
# ---------------------------------------------------------------------------
class AssetViewSet(TenantModelViewSet):
    queryset = Asset.objects.select_related(
        "category", "assigned_employee", "assigned_employee__department", "location"
    ).prefetch_related("history__employee")
    serializer_class = AssetSerializer
    audit_entity_type = "Asset"
    audit_label_field = "asset_code"
    status_field = "status"
    search_fields = ["name", "asset_code", "serial_number"]
    ordering = ["name"]
    filter_map = {"categoryId": "category_id", "employeeId": "assigned_employee_id"}
    permission_map = {"read": ["view_staff"], "write": ["edit_staff"]}

    def perform_create(self, serializer):
        if not serializer.validated_data.get("asset_code"):
            serializer.validated_data["asset_code"] = allocate_number(
                self.request.user.client, "ASSET"
            )
        return super().perform_create(serializer)

    @action(detail=True, methods=["post"])
    @transaction.atomic
    def assign(self, request, pk=None):
        asset = self.get_object()
        employee = Employee.objects.filter(
            pk=request.data.get("employeeId"), client_id=request.client_id,
            deleted_at__isnull=True,
        ).first()
        if employee is None:
            raise ValidationFailed(
                "Unknown employee.", field_errors={"employeeId": ["Not found."]}
            )
        if asset.history.filter(returned_at__isnull=True).exists():
            raise Conflict(
                "This asset is already assigned. Record a return first.",
                code=Codes.IN_USE,
            )

        AssetAssignment.objects.create(
            client_id=request.client_id,
            asset=asset,
            employee=employee,
            assigned_by=request.user,
            notes=request.data.get("notes"),
        )
        asset.assigned_employee = employee
        asset.status = "Assigned"
        asset.save(update_fields=["assigned_employee", "status", "updated_at"])
        self.write_audit("assign", asset, description=f"Assigned to {employee.name}")
        return Response(self.get_serializer(asset).data)

    @action(detail=True, methods=["post"], url_path="return")
    @transaction.atomic
    def return_asset(self, request, pk=None):
        asset = self.get_object()
        assignment = asset.history.filter(returned_at__isnull=True).first()
        if assignment is None:
            raise Conflict("This asset is not currently assigned.", code=Codes.BAD_TARGET)

        assignment.returned_at = timezone.now()
        assignment.returned_condition = request.data.get("condition")
        assignment.notes = request.data.get("notes", assignment.notes)
        assignment.save()

        asset.assigned_employee = None
        asset.condition = request.data.get("condition") or asset.condition
        asset.status = (
            "Under Maintenance"
            if request.data.get("condition") == "Needs Repair"
            else "Available"
        )
        asset.save(update_fields=["assigned_employee", "condition", "status", "updated_at"])
        self.write_audit("return", asset, description=request.data.get("notes"))
        return Response(self.get_serializer(asset).data)


class AssetCategoryViewSet(TenantModelViewSet):
    queryset = AssetCategory.objects.all()
    serializer_class = AssetCategorySerializer
    audit_entity_type = "AssetCategory"
    audit_label_field = "name"
    status_field = None
    ordering = ["name"]
    permission_map = {"read": [], "write": ["edit_staff"]}


class AssetRequestViewSet(OwnEmployeeScopeMixin, TenantModelViewSet):
    queryset = AssetRequest.objects.select_related("employee", "category", "fulfilled_asset")
    serializer_class = AssetRequestSerializer
    audit_entity_type = "AssetRequest"
    status_field = "status"
    ordering = ["-created_at"]
    filter_map = {"employeeId": "employee_id", "status": "status"}
    team_scope_permissions = ("edit_staff", "view_staff")
    permission_map = {"read": [], "create": [], "write": ["edit_staff"]}

    def perform_create(self, serializer):
        employee = serializer.validated_data.get("employee")
        if not employee:
            resolved = self._resolve_employee(self.request)
            if resolved:
                serializer.validated_data["employee"] = resolved
                employee = resolved
        self.check_own_employee(employee)
        return super().perform_create(serializer)

    @transaction.atomic
    def perform_update(self, serializer):
        approved = self.request.data.get("approved")
        status = self.request.data.get("status")
        instance = serializer.instance
        if approved is True:
            instance.status = "Approved"
            instance.approver = self.request.user
        elif approved is False:
            instance.status = "Rejected"
            instance.approver = self.request.user
        elif status in ("Pending", "Approved", "Fulfilled", "Rejected"):
            instance.status = status
            if status in ("Approved", "Rejected", "Fulfilled"):
                instance.approver = self.request.user

        fulfilled_asset_id = self.request.data.get("fulfilledAssetId") or self.request.data.get("fulfilled_asset")
        if fulfilled_asset_id:
            from apps.hrms.models import Asset
            asset = Asset.objects.filter(pk=fulfilled_asset_id, client_id=self.request.client_id).first()
            if asset:
                instance.fulfilled_asset = asset
                instance.status = "Fulfilled"

        instance.save()
        self.write_audit("update_status", instance, description=f"Asset request marked {instance.status}")
        return super().perform_update(serializer)


class HrDocumentViewSet(OwnEmployeeScopeMixin, TenantModelViewSet):
    queryset = HrDocument.objects.select_related("employee", "file", "created_by")
    serializer_class = HrDocumentSerializer
    audit_entity_type = "HrDocument"
    audit_label_field = "title"
    status_field = None
    search_fields = ["title", "description", "category"]
    ordering = ["-created_at"]
    filter_map = {"category": "category", "employeeId": "employee_id"}
    team_scope_permissions = ("view_staff", "edit_staff")
    permission_map = {
        "read": [],
        "create": [],
        "download": [],
        "write": [],
    }

    def get_queryset(self):
        queryset = TenantModelViewSet.get_queryset(self)
        if self.has_team_scope():
            return queryset
        employee_id = getattr(self.request.user, "employee_id", None)
        if not employee_id and hasattr(self, "_resolve_employee"):
            resolved = self._resolve_employee(self.request)
            if resolved:
                employee_id = resolved.id
        if not employee_id:
            return queryset.none()
        return queryset.filter(Q(employee_id=employee_id) | Q(employee__isnull=True))

    def perform_create(self, serializer):
        from rest_framework.exceptions import PermissionDenied, ValidationError

        # Requirement 4: Automatically derive employee identity from authenticated session;
        # do not allow employees to submit documents on behalf of another employee.
        if not self.has_team_scope():
            resolved = self._resolve_employee(self.request)
            if not resolved:
                raise PermissionDenied(
                    "You must be an active employee to upload personal documents.",
                    code="NO_EMPLOYEE_LINK",
                )
            serializer.validated_data["employee"] = resolved
        else:
            # HR Admin with team scope can assign to a specific employee or leave None for company-wide
            pass

        # Requirement 2 & 3: Require the employee to select a document category:
        # Identity, Contract, Financial, Compliance, Legal, Educational, General. Save category with document.
        category = serializer.validated_data.get("category")
        allowed_categories = {
            "identity": "Identity",
            "contract": "Contract",
            "financial": "Financial",
            "compliance": "Compliance",
            "legal": "Legal",
            "educational": "Educational",
            "general": "General",
        }
        if not category:
            serializer.validated_data["category"] = "General"
        else:
            norm_cat = str(category).strip().lower()
            if norm_cat in allowed_categories:
                serializer.validated_data["category"] = allowed_categories[norm_cat]
            elif self.has_team_scope():
                # HR Admins can retain legacy categories (e.g. Policy)
                pass
            else:
                raise ValidationError({
                    "category": f"Invalid document category '{category}'. Allowed categories: {', '.join(allowed_categories.values())}."
                })

        return super().perform_create(serializer)

    def filter_queryset(self, queryset):
        queryset = super().filter_queryset(queryset)
        # `status` is derived, so it is filtered after the query rather than in SQL.
        requested = self.request.query_params.getlist("status")
        if requested:
            today = timezone.localdate()
            if "Expired" in requested and len(requested) == 1:
                queryset = queryset.filter(valid_until__lt=today)
            elif "Valid" in requested and len(requested) == 1:
                queryset = queryset.filter(
                    Q(valid_until__isnull=True) | Q(valid_until__gte=today)
                )
        return queryset

    @action(detail=True, methods=["get"])
    def download(self, request, pk=None):
        from apps.core.files import public_url

        document = self.get_object()
        # Requirement 1, 2, 6: Employees can download only their own documents; protect download endpoints
        if not self.has_team_scope():
            if document.employee_id:
                self.check_own_employee(document.employee)
            elif document.is_confidential:
                raise PermissionDenied("You do not have permission to view or download this confidential document.")

        if document.file_id is None:
            raise NotFound("No file is attached to this document.")
        return Response({"downloadUrl": public_url(document.file, request)})

    def perform_update(self, serializer):
        # Requirement 2, 3: Only HR Admins with edit_staff or the document owner can update their own metadata
        instance = serializer.instance
        if not self.has_team_scope():
            if instance.employee_id:
                self.check_own_employee(instance.employee)
            else:
                raise PermissionDenied("Only authorized HR administrators can update company-wide documents.")
            # Disallow changing document ownership / employee association
            serializer.validated_data.pop("employee", None)
            serializer.validated_data.pop("employee_id", None)
        return super().perform_update(serializer)

    def perform_destroy(self, instance):
        # Requirement 2, 3: Only HR Admins with edit_staff or the document owner can delete their own documents
        if not self.has_team_scope():
            if instance.employee_id:
                self.check_own_employee(instance.employee)
            else:
                raise PermissionDenied("Only authorized HR administrators can delete company-wide documents.")
        return super().perform_destroy(instance)


class PolicyViewSet(TenantModelViewSet):
    queryset = Policy.objects.select_related("category", "owner_department").prefetch_related(
        "version_history"
    )
    serializer_class = PolicySerializer
    audit_entity_type = "Policy"
    audit_label_field = "name"
    status_field = "status"
    search_fields = ["name", "summary"]
    ordering = ["name"]
    filter_map = {"categoryId": "category_id"}
    permission_map = {"read": ["view_staff"], "write": ["edit_staff"]}

    @transaction.atomic
    def perform_update(self, serializer):
        """An edit bumps ``version`` and appends to ``versionHistory[]``."""
        instance = serializer.instance
        previous_body = instance.body
        policy = super().perform_update(serializer)

        if policy.body != previous_body:
            policy.version += 1
            policy.save(update_fields=["version", "updated_at"])
            PolicyVersion.objects.create(
                client_id=policy.client_id,
                policy=policy,
                version=policy.version,
                body=policy.body,
                file=policy.file,
                changed_by=self.request.user,
                change_note=self.request.data.get("changeNote"),
            )
        return policy

    @action(detail=True, methods=["post"], url_path="submit-for-approval")
    def submit_for_approval(self, request, pk=None):
        policy = self.get_object()
        policy.status = "Pending Approval"
        policy.save(update_fields=["status", "updated_at"])
        return Response(self.get_serializer(policy).data)

    @action(detail=True, methods=["post"])
    def approve(self, request, pk=None):
        policy = self.get_object()
        policy.status = "Active"
        policy.approved_by = request.user
        policy.approved_at = timezone.now()
        policy.save(update_fields=["status", "approved_by", "approved_at", "updated_at"])

        if policy.ack_required:
            employees = Employee.objects.filter(
                client_id=request.client_id, status="Active", deleted_at__isnull=True
            )
            PolicyAcknowledgement.objects.bulk_create(
                [
                    PolicyAcknowledgement(
                        client_id=request.client_id,
                        policy=policy,
                        policy_version=policy.version,
                        employee=employee,
                    )
                    for employee in employees
                ],
                ignore_conflicts=True,
            )
        self.write_audit("approve", policy, description="Policy approved")
        return Response(self.get_serializer(policy).data)

    @action(detail=True, methods=["post"])
    def archive(self, request, pk=None):
        policy = self.get_object()
        policy.status = "Archived"
        policy.save(update_fields=["status", "updated_at"])
        return Response(self.get_serializer(policy).data)

    @action(detail=True, methods=["get"])
    def acknowledgements(self, request, pk=None):
        policy = self.get_object()
        rows = policy.acknowledgements.filter(deleted_at__isnull=True).select_related(
            "employee"
        )
        return Response(
            envelope(
                PolicyAcknowledgementSerializer(
                    rows, many=True, context={"policy": policy}
                ).data
            )
        )

    @action(detail=True, methods=["post"])
    def acknowledge(self, request, pk=None):
        policy = self.get_object()
        employee_id = getattr(request.user, "employee_id", None)
        if employee_id is None:
            raise BusinessRuleViolation(
                "Your account is not linked to an employee record.",
                code="NO_EMPLOYEE_RECORD",
            )
        row, _ = PolicyAcknowledgement.objects.get_or_create(
            client_id=request.client_id,
            policy=policy,
            policy_version=policy.version,
            employee_id=employee_id,
        )
        row.acknowledged_at = timezone.now()
        row.save(update_fields=["acknowledged_at", "updated_at"])
        return Response(
            PolicyAcknowledgementSerializer(row, context={"policy": policy}).data
        )


class PolicyCategoryViewSet(TenantModelViewSet):
    queryset = PolicyCategory.objects.all()
    serializer_class = PolicyCategorySerializer
    audit_entity_type = "PolicyCategory"
    audit_label_field = "name"
    status_field = None
    ordering = ["name"]
    permission_map = {"read": ["view_staff"], "write": ["edit_staff"]}


class CalendarEventViewSet(TenantModelViewSet):
    queryset = CalendarEvent.objects.select_related("department")
    serializer_class = CalendarEventSerializer
    audit_entity_type = "CalendarEvent"
    audit_label_field = "title"
    status_field = None
    default_date_field = "starts_at"
    ordering = ["starts_at"]
    search_fields = ["title", "description", "location"]
    filter_map = {"type": "type", "dept": "department__name", "departmentId": "department_id"}
    permission_map = {"read": ["view_staff"], "write": ["edit_staff"]}

    def filter_queryset(self, queryset):
        queryset = super().filter_queryset(queryset)
        params = self.request.query_params
        if params.get("from"):
            queryset = queryset.filter(starts_at__gte=params["from"])
        if params.get("to"):
            queryset = queryset.filter(starts_at__lte=params["to"])
        return queryset

    def perform_destroy(self, instance):
        if instance.source_type == "LeaveRequest":
            raise Conflict(
                "Leave events are generated from the leave request.",
                code="GENERATED_EVENT",
                detail="Cancel the leave request instead.",
            )
        super().perform_destroy(instance)


class HolidayViewSet(TenantModelViewSet):
    queryset = Holiday.objects.select_related("location", "department")
    serializer_class = HolidaySerializer
    audit_entity_type = "Holiday"
    audit_label_field = "name"
    status_field = "status"
    default_date_field = "date"
    ordering = ["date"]
    search_fields = ["name", "description"]
    filterset_fields = ["status", "holiday_type", "location", "department"]
    permission_map = {"read": ["view_staff"], "write": ["edit_staff"]}

    def get_queryset(self):
        qs = super().get_queryset()
        year = self.request.query_params.get("year")
        if year:
            try:
                qs = qs.filter(date__year=int(year))
            except (ValueError, TypeError):
                pass
        dept = self.request.query_params.get("department") or self.request.query_params.get("departmentId")
        if dept:
            qs = qs.filter(department_id=dept)
        loc = self.request.query_params.get("location") or self.request.query_params.get("locationId")
        if loc:
            qs = qs.filter(location_id=loc)
        return qs

    @action(detail=True, methods=["post"])
    def publish(self, request, pk=None):
        instance = self.get_object()
        instance.status = "Published"
        instance.save(update_fields=["status", "updated_at"])
        return Response(self.get_serializer(instance).data)

    @action(detail=True, methods=["post"])
    def deactivate(self, request, pk=None):
        instance = self.get_object()
        instance.status = "Inactive"
        instance.save(update_fields=["status", "updated_at"])
        return Response(self.get_serializer(instance).data)


class WorkScheduleViewSet(TenantModelViewSet):
    queryset = WorkSchedule.objects.select_related("department", "team", "location").prefetch_related("employees")
    serializer_class = WorkScheduleSerializer
    audit_entity_type = "WorkSchedule"
    audit_label_field = "name"
    status_field = "status"
    search_fields = ["name", "code", "department__name", "team__name"]
    filterset_fields = ["status", "shift_type", "department", "is_default"]
    ordering = ["name"]
    permission_map = {"read": ["view_staff"], "write": ["edit_staff"]}

    def get_queryset(self):
        qs = super().get_queryset()
        dept = self.request.query_params.get("department") or self.request.query_params.get("departmentId")
        if dept:
            qs = qs.filter(department_id=dept)
        status_param = self.request.query_params.get("status")
        if status_param:
            qs = qs.filter(status=status_param)
        return qs

    @action(detail=True, methods=["post"])
    def activate(self, request, pk=None):
        instance = self.get_object()
        instance.status = "Active"
        instance.save(update_fields=["status", "updated_at"])
        return Response(self.get_serializer(instance).data)

    @action(detail=True, methods=["post"])
    def deactivate(self, request, pk=None):
        instance = self.get_object()
        instance.status = "Inactive"
        instance.save(update_fields=["status", "updated_at"])
        return Response(self.get_serializer(instance).data)


class WorkingDayView(APIView):
    """``GET/PUT /hrms/working-days/``.

    The endpoint retains its weekday-row contract for global availability while
    also acting as the persistence target for the HR Admin work-schedule
    configuration (`defaultWorkingDays`, `departmentWorkingDays`,
    `departmentWorkingHours`).  The configuration object is stored as a tenant
    setting so department-specific values survive refresh/re-login without a
    new table or endpoint.
    """

    permission_classes = [HasModulePermission]
    permission_map = {"read": ["view_staff"], "write": ["edit_staff"]}

    SETTING_KEY = "hrms_working_days"

    def _load_config(self, request):
        from apps.core.models import Setting

        row = Setting.objects.filter(
            client_id=request.client_id, key=self.SETTING_KEY
        ).first()
        return row.value if row and isinstance(row.value, dict) else None

    def get(self, request):
        config = self._load_config(request)
        if config is not None:
            return Response(config)
        rows = WorkingDay.objects.filter(
            client_id=request.client_id, deleted_at__isnull=True
        ).order_by("weekday")
        return Response(envelope(WorkingDaySerializer(rows, many=True).data))

    @transaction.atomic
    def put(self, request):
        data = request.data
        if isinstance(data, dict) and (
            "defaultWorkingDays" in data
            or "departmentWorkingDays" in data
            or "departmentWorkingHours" in data
        ):
            from apps.core.models import Setting

            normalised = {
                "defaultWorkingDays": data.get("defaultWorkingDays", 24),
                "departmentWorkingDays": data.get("departmentWorkingDays") or {},
                "departmentWorkingHours": data.get("departmentWorkingHours") or {},
            }
            row, _ = Setting.objects.update_or_create(
                client_id=request.client_id,
                key=self.SETTING_KEY,
                defaults={"value": normalised, "updated_by": request.user},
            )
            return Response(row.value)

        days = data.get("days") if isinstance(data, dict) else data
        if not isinstance(days, list):
            raise ValidationFailed(
                "Provide a list of weekday configurations or a work-schedule configuration.",
                field_errors={"days": ["Expected a list."]},
            )
        for row in days:
            WorkingDay.objects.update_or_create(
                client_id=request.client_id,
                weekday=row.get("weekday"),
                location_id=row.get("locationId"),
                defaults={
                    "is_working": row.get("isWorking", True),
                    "shift_start": row.get("shiftStart"),
                    "shift_end": row.get("shiftEnd"),
                },
            )
        return self.get(request)


class TeamViewSet(TenantModelViewSet):
    queryset = Team.objects.select_related("department", "lead", "approver").prefetch_related("members")
    serializer_class = TeamSerializer
    audit_entity_type = "Team"
    audit_label_field = "name"
    status_field = "status"
    search_fields = ["name", "code", "lead_name", "approver_name", "department__name", "lead__name", "approver__name"]
    filter_map = {"departmentId": "department_id", "status": "status"}
    ordering = ["name"]
    permission_map = {"read": ["view_staff"], "write": ["edit_staff"]}

    def perform_destroy(self, instance):
        # Safe dependency check and detachment
        instance.members.clear()
        super().perform_destroy(instance)

    @action(detail=True, methods=["post"], url_path="add-member")
    def add_member(self, request, pk=None):
        team = self.get_object()
        emp_id = request.data.get("employeeId") or request.data.get("employee_id")
        if not emp_id:
            raise ValidationFailed("Employee ID is required.", field_errors={"employeeId": ["Required."]})
        emp = Employee.objects.filter(client_id=request.client_id, id=emp_id, deleted_at__isnull=True).first()
        if not emp:
            raise NotFound("Employee not found.")
        if emp.status in ("Terminated", "Resigned"):
            raise ValidationFailed(f"Cannot assign {emp.name}: employee is {emp.status.lower()}.")
        team.members.add(emp)
        team.headcount = team.members.filter(deleted_at__isnull=True).exclude(status__in=["Terminated", "Resigned"]).count()
        team.save(update_fields=["headcount", "updated_at"])
        return Response(TeamSerializer(team, context={"request": request}).data)

    @action(detail=True, methods=["post"], url_path="remove-member")
    def remove_member(self, request, pk=None):
        team = self.get_object()
        emp_id = request.data.get("employeeId") or request.data.get("employee_id")
        if not emp_id:
            raise ValidationFailed("Employee ID is required.", field_errors={"employeeId": ["Required."]})
        emp = Employee.objects.filter(client_id=request.client_id, id=emp_id).first()
        if emp:
            team.members.remove(emp)
            team.headcount = team.members.filter(deleted_at__isnull=True).exclude(status__in=["Terminated", "Resigned"]).count()
            team.save(update_fields=["headcount", "updated_at"])
        return Response(TeamSerializer(team, context={"request": request}).data)


DEFAULT_HRMS_SETTINGS = {
    "companyName": "",
    "workWeek": "Monday - Friday (5 Days)",
    "timezone": "IST (UTC +05:30) — Asia/Kolkata",
    "fiscalYearStart": "April 01",
    "probationPeriod": "90 Days (3 Months)",
    "noticePeriod": "60 Days (2 Months)",
    "allowEmployeeDelegation": True,
    "requireTwoFactorForAdmin": True,
    "sandwichRuleEnabled": True,
    "maxCarryForwardDays": 15,
}


class ApprovalChainViewSet(TenantModelViewSet):
    queryset = ApprovalChain.objects.all()
    serializer_class = ApprovalChainSerializer
    audit_entity_type = "ApprovalChain"
    audit_label_field = "name"
    status_field = "status"
    ordering = ["name"]
    filter_map = {"appliesTo": "applies_to", "status": "status"}
    permission_map = {"read": ["view_staff"], "write": ["edit_staff"]}

    @action(detail=True, methods=["post"])
    def activate(self, request, pk=None):
        chain = self.get_object()
        chain.status = "Active"
        chain.save(update_fields=["status", "updated_at"])
        self.write_audit("activate", chain, description="Approval workflow activated")
        return Response(self.get_serializer(chain).data)

    @action(detail=True, methods=["post"])
    def deactivate(self, request, pk=None):
        chain = self.get_object()
        chain.status = "Inactive"
        chain.save(update_fields=["status", "updated_at"])
        self.write_audit("deactivate", chain, description="Approval workflow deactivated")
        return Response(self.get_serializer(chain).data)

    @action(detail=False, methods=["get"], url_path="modules")
    def modules(self, request):
        from .workflow_service import MODULE_CANONICAL
        unique_modules = sorted(list(set(MODULE_CANONICAL.values())))
        standard_roles = [
            "Direct Line Manager",
            "Department Head",
            "Team Leader",
            "HR Operations Lead",
            "HR Director",
            "Finance Lead",
            "Chief Financial Officer (CFO)",
            "Managing Director",
            "Operations Lead",
            "IT Stock Manager",
        ]
        return Response({
            "modules": unique_modules,
            "roles": standard_roles,
        })


class TerminationViewSet(TenantModelViewSet):
    queryset = Termination.objects.select_related("employee", "approval_chain")
    serializer_class = TerminationSerializer
    audit_entity_type = "Termination"
    status_field = "status"
    ordering = ["-created_at"]
    filter_map = {"employeeId": "employee_id", "status": "status"}
    permission_map = {"read": ["view_staff"], "write": ["delete_staff", "edit_staff"]}

    def perform_create(self, serializer):
        instance = super().perform_create(serializer)
        from . import workflow_service
        workflow_service.initialize_request_workflow(instance, "Terminations", self.request.user)
        return instance

    @action(detail=True, methods=["post"])
    def approve(self, request, pk=None):
        instance = self.get_object()
        from . import workflow_service
        updated, is_final = workflow_service.process_approval_action(
            instance, "Terminations", request.user, "approve",
            remark=request.data.get("remark"),
            target_status=request.data.get("status") or "In Exit Clearance",
        )
        if is_final:
            from . import lifecycle_service
            lifecycle_service.execute_employee_offboarding(
                updated, user=request.user, reason=request.data.get("remark"), force=False
            )
            updated.refresh_from_db()
        return Response(self.get_serializer(updated).data)

    @action(detail=True, methods=["post"])
    def reject(self, request, pk=None):
        instance = self.get_object()
        from . import workflow_service
        updated, is_final = workflow_service.process_approval_action(
            instance, "Terminations", request.user, "reject",
            remark=request.data.get("remark") or request.data.get("reason"),
        )
        return Response(self.get_serializer(updated).data)

    @action(detail=True, methods=["post"], url_path="execute-offboarding")
    def execute_offboarding(self, request, pk=None):
        instance = self.get_object()
        from . import lifecycle_service
        force = bool(request.data.get("force", False))
        updated, executed, message = lifecycle_service.execute_employee_offboarding(
            instance, user=request.user, reason=request.data.get("reason") or request.data.get("remark"), force=force
        )
        return Response({
            "success": executed,
            "message": message,
            "data": self.get_serializer(updated).data,
        })

    @action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):
        instance = self.get_object()
        from . import lifecycle_service
        updated = lifecycle_service.cancel_exit_process(
            instance, user=request.user, reason=request.data.get("reason") or request.data.get("remark")
        )
        return Response(self.get_serializer(updated).data)

    @action(detail=True, methods=["get", "post"], url_path="checklist")
    def checklist(self, request, pk=None):
        instance = self.get_object()
        from . import lifecycle_service
        from .models import ResignationChecklistItem
        if request.method == "POST":
            item_id = request.data.get("itemId") or request.data.get("id")
            if item_id:
                item = ResignationChecklistItem.objects.filter(
                    client_id=request.client_id, id=item_id, termination=instance
                ).first()
                if item:
                    is_completed = request.data.get("isCompleted")
                    if is_completed is None:
                        is_completed = not item.is_completed
                    item.is_completed = bool(is_completed)
                    item.completed_at = timezone.now() if item.is_completed else None
                    if request.data.get("notes"):
                        item.notes = str(request.data["notes"]).strip()
                    if request.user and getattr(request.user, "is_authenticated", False):
                        item.owner = request.user
                    item.save()
            else:
                title = request.data.get("title")
                dept = request.data.get("department", "HR")
                notes = request.data.get("notes", "")
                if title:
                    ResignationChecklistItem.objects.create(
                        client_id=request.client_id,
                        termination=instance,
                        title=str(title).strip(),
                        department=dept,
                        notes=notes,
                        is_completed=False,
                    )
        status_info = lifecycle_service.get_clearance_status(instance)
        items_data = [
            {
                "id": str(i.id),
                "title": i.title,
                "department": i.department or "General",
                "notes": i.notes or "",
                "isCompleted": i.is_completed,
                "completedAt": i.completed_at.isoformat() if i.completed_at else None,
                "ownerName": i.owner.name if i.owner else "",
            }
            for i in status_info["items"]
        ]
        return Response({
            "total": status_info["total"],
            "completed": status_info["completed"],
            "pending": status_info["pending"],
            "isCleared": status_info["is_cleared"],
            "checklist": items_data,
        })


class ResignationViewSet(OwnEmployeeScopeMixin, TenantModelViewSet):
    queryset = Resignation.objects.select_related("employee", "approval_chain").prefetch_related("checklist")
    serializer_class = ResignationSerializer
    audit_entity_type = "Resignation"
    status_field = "status"
    default_date_field = "submitted_on"
    ordering = ["-submitted_on"]
    filter_map = {"employeeId": "employee_id", "status": "status"}
    team_scope_permissions = ("view_staff", "edit_staff")
    permission_map = {
        "read": [],
        "create": [],
        "cancel": [],
        "write": ["edit_staff"],
        "approve": ["edit_staff"],
        "reject": ["edit_staff"],
        "resubmit": ["edit_staff"],
        "execute_offboarding": ["edit_staff"],
        "checklist": ["edit_staff"],
        "waive_notice": ["edit_staff"],
        "shorten_notice": ["edit_staff"],
    }

    def perform_create(self, serializer):
        employee = serializer.validated_data.get("employee")
        if not employee:
            resolved = self._resolve_employee(self.request)
            if resolved:
                serializer.validated_data["employee"] = resolved
                employee = resolved
        self.check_own_employee(employee)

        from apps.core.exceptions import Conflict
        if employee:
            if employee.status in ("Terminated", "Resigned"):
                raise Conflict(
                    f"Cannot submit resignation for {employee.name}: employee is already {employee.status.lower()}.",
                    code="EMPLOYEE_EXITED",
                )
            active_resignation = Resignation.objects.filter(
                client_id=self.request.client_id,
                employee=employee,
                status__in=["Submitted", "Pending", "Pending Review", "Notice Period", "Exit Interview Scheduled", "Clearance In Progress", "Approved"],
                deleted_at__isnull=True,
            ).first()
            if active_resignation:
                raise Conflict(
                    "An active resignation process is already recorded for this employee.",
                    code="ALREADY_EXISTS",
                )

        instance = super().perform_create(serializer)
        from . import workflow_service
        workflow_service.initialize_request_workflow(instance, "Resignations", self.request.user)
        return instance

    @action(detail=True, methods=["post"])
    def approve(self, request, pk=None):
        instance = self.get_object()
        from . import workflow_service
        updated, is_final = workflow_service.process_approval_action(
            instance, "Resignations", request.user, "approve",
            remark=request.data.get("remark"),
            target_status=request.data.get("status") or "Exit Interview Scheduled",
        )
        if is_final:
            from . import lifecycle_service
            lifecycle_service.execute_employee_offboarding(
                updated, user=request.user, reason=request.data.get("remark"), force=False
            )
            updated.refresh_from_db()
        return Response(self.get_serializer(updated).data)

    @action(detail=True, methods=["post"])
    def reject(self, request, pk=None):
        instance = self.get_object()
        from . import workflow_service
        updated, is_final = workflow_service.process_approval_action(
            instance, "Resignations", request.user, "reject",
            remark=request.data.get("remark") or request.data.get("reason"),
        )
        return Response(self.get_serializer(updated).data)

    @action(detail=True, methods=["post"])
    def resubmit(self, request, pk=None):
        instance = self.get_object()
        from . import workflow_service
        updated, is_final = workflow_service.process_approval_action(
            instance, "Resignations", request.user, "resubmit",
            remark=request.data.get("remark"),
        )
        return Response(self.get_serializer(updated).data)

    @action(detail=True, methods=["post"], url_path="execute-offboarding")
    def execute_offboarding(self, request, pk=None):
        instance = self.get_object()
        from . import lifecycle_service
        force = bool(request.data.get("force", False))
        updated, executed, message = lifecycle_service.execute_employee_offboarding(
            instance, user=request.user, reason=request.data.get("reason") or request.data.get("remark"), force=force
        )
        return Response({
            "success": executed,
            "message": message,
            "data": self.get_serializer(updated).data,
        })

    @action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):
        instance = self.get_object()
        if not self.has_team_scope():
            self.check_own_employee(instance.employee)
        from . import lifecycle_service
        updated = lifecycle_service.cancel_exit_process(
            instance, user=request.user, reason=request.data.get("reason") or request.data.get("remark")
        )
        return Response(self.get_serializer(updated).data)

    @action(detail=True, methods=["get", "post"], url_path="checklist")
    def checklist(self, request, pk=None):
        instance = self.get_object()
        from . import lifecycle_service
        from .models import ResignationChecklistItem
        if request.method == "POST":
            item_id = request.data.get("itemId") or request.data.get("id")
            if item_id:
                item = ResignationChecklistItem.objects.filter(
                    client_id=request.client_id, id=item_id, resignation=instance
                ).first()
                if item:
                    is_completed = request.data.get("isCompleted")
                    if is_completed is None:
                        is_completed = not item.is_completed
                    item.is_completed = bool(is_completed)
                    item.completed_at = timezone.now() if item.is_completed else None
                    if request.data.get("notes"):
                        item.notes = str(request.data["notes"]).strip()
                    if request.user and getattr(request.user, "is_authenticated", False):
                        item.owner = request.user
                    item.save()
            else:
                title = request.data.get("title")
                dept = request.data.get("department", "HR")
                notes = request.data.get("notes", "")
                if title:
                    ResignationChecklistItem.objects.create(
                        client_id=request.client_id,
                        resignation=instance,
                        title=str(title).strip(),
                        department=dept,
                        notes=notes,
                        is_completed=False,
                    )
        status_info = lifecycle_service.get_clearance_status(instance)
        items_data = [
            {
                "id": str(i.id),
                "title": i.title,
                "department": i.department or "General",
                "notes": i.notes or "",
                "isCompleted": i.is_completed,
                "completedAt": i.completed_at.isoformat() if i.completed_at else None,
                "ownerName": i.owner.name if i.owner else "",
            }
            for i in status_info["items"]
        ]
        return Response({
            "total": status_info["total"],
            "completed": status_info["completed"],
            "pending": status_info["pending"],
            "isCleared": status_info["is_cleared"],
            "checklist": items_data,
        })

    @action(detail=True, methods=["post"], url_path="waive-notice")
    def waive_notice(self, request, pk=None):
        instance = self.get_object()
        from . import lifecycle_service
        from datetime import date
        eff_date = (
            request.data.get("effectiveLastDay")
            or request.data.get("effective_last_day")
            or request.data.get("lastWorkingDay")
            or request.data.get("last_working_day")
        )
        if isinstance(eff_date, str):
            try:
                eff_date = date.fromisoformat(eff_date)
            except ValueError:
                eff_date = None
        reason = (
            request.data.get("reason")
            or request.data.get("waiverReason")
            or request.data.get("waiver_reason")
            or request.data.get("remark")
        )
        updated = lifecycle_service.waive_resignation_notice(
            instance, user=request.user, effective_last_day=eff_date, reason=reason
        )
        return Response(self.get_serializer(updated).data)

    @action(detail=True, methods=["post"], url_path="shorten-notice")
    def shorten_notice(self, request, pk=None):
        instance = self.get_object()
        from . import lifecycle_service
        from datetime import date
        rev_date = (
            request.data.get("revisedLastWorkingDay")
            or request.data.get("revised_last_working_day")
            or request.data.get("lastWorkingDay")
            or request.data.get("last_working_day")
        )
        if isinstance(rev_date, str):
            try:
                rev_date = date.fromisoformat(rev_date)
            except ValueError:
                rev_date = None
        reason = request.data.get("reason") or request.data.get("remark")
        updated = lifecycle_service.shorten_resignation_notice(
            instance, user=request.user, revised_last_working_day=rev_date, reason=reason
        )
        return Response(self.get_serializer(updated).data)


class ComplaintViewSet(TenantModelViewSet):
    """HR-only. db.md §11.8 asks for restricted access rather than relying on
    endpoint checks alone; this is the endpoint half."""

    queryset = Complaint.objects.select_related(
        "raised_by_employee", "against_employee", "assigned_to", "approval_chain"
    )
    serializer_class = ComplaintSerializer
    audit_entity_type = "Complaint"
    status_field = "status"
    ordering = ["-created_at"]
    filter_map = {"category": "category", "priority": "priority", "status": "status"}
    permission_map = {"read": [], "create": [], "write": ["edit_staff"]}


    def get_queryset(self):
        qs = super().get_queryset()
        user = getattr(self.request, "user", None)
        if not user or not user.is_authenticated:
            return qs.none()
        from apps.core.permissions import has_permission
        if not has_permission(user, ("view_staff", "edit_staff")):
            emp_id = getattr(user, "employee_id", None)
            qs = qs.filter(Q(raised_by_employee_id=emp_id) | Q(assigned_to=user))
        return qs

    def perform_create(self, serializer):
        if not serializer.validated_data.get("raised_by_employee"):
            employee_id = getattr(self.request.user, "employee_id", None)
            if employee_id:
                serializer.validated_data["raised_by_employee_id"] = employee_id
        instance = super().perform_create(serializer)
        from . import workflow_service
        workflow_service.initialize_request_workflow(instance, "Grievances", self.request.user)
        return instance

    def perform_update(self, serializer):
        complaint = super().perform_update(serializer)
        if complaint.status in ("Resolved", "Closed") and complaint.resolved_at is None:
            complaint.resolved_at = timezone.now()
            complaint.save(update_fields=["resolved_at", "updated_at"])
        return complaint

    @action(detail=True, methods=["post"])
    def comment(self, request, pk=None):
        instance = self.get_object()
        comment_text = request.data.get("comment") or request.data.get("text")
        if not comment_text or not str(comment_text).strip():
            raise ValidationFailed("Comment text cannot be empty.")
        if not isinstance(instance.comments, list):
            instance.comments = []
        import uuid
        entry = {
            "id": str(uuid.uuid4()),
            "author": request.user.name or request.user.email,
            "text": str(comment_text).strip(),
            "created_at": timezone.now().isoformat(),
        }
        instance.comments.append(entry)
        instance.save(update_fields=["comments", "updated_at"])
        self.write_audit(
            action="comment",
            instance=instance,
            description=f"Added investigation comment on grievance {instance.id}.",
        )
        return Response(self.get_serializer(instance).data)

    @action(detail=True, methods=["post"])
    def resolve(self, request, pk=None):
        instance = self.get_object()
        resolution_text = request.data.get("resolution") or request.data.get("remark") or ""
        instance.resolution = resolution_text
        instance.status = "Resolved"
        instance.resolved_at = timezone.now()
        instance.save(update_fields=["resolution", "status", "resolved_at", "updated_at"])
        try:
            from apps.core.models import Notification
            from .user_link import linked_user
            recipients = []
            if instance.assigned_to:
                recipients.append(instance.assigned_to)
            if not instance.is_anonymous and instance.raised_by_employee:
                u = linked_user(instance.raised_by_employee)
                if u and u not in recipients:
                    recipients.append(u)
            for r in recipients:
                Notification.objects.create(
                    client=instance.client,
                    recipient=r,
                    actor=getattr(request, "user", None),
                    type="hrms.grievance_resolved",
                    category="hrms",
                    entity_type="Complaint",
                    entity_id=instance.id,
                    title=f"Grievance Resolved: {instance.category or 'General'}",
                    body=f"Grievance filed on {instance.filed_on} has been resolved: {resolution_text[:120]}",
                    channels=["in_app"],
                    payload={"type": "grievance", "complaintId": str(instance.id)},
                )
        except Exception:
            pass
        self.write_audit(
            action="resolve",
            instance=instance,
            description=f"Resolved grievance {instance.id}.",
            after={"status": "Resolved", "resolution": resolution_text},
        )
        return Response(self.get_serializer(instance).data)

    @action(detail=True, methods=["post"])
    def approve(self, request, pk=None):
        instance = self.get_object()
        from . import workflow_service
        updated, is_final = workflow_service.process_approval_action(
            instance, "Grievances", request.user, "approve",
            remark=request.data.get("remark"),
            target_status=request.data.get("status") or "Resolved",
        )
        return Response(self.get_serializer(updated).data)

    @action(detail=True, methods=["post"])
    def reject(self, request, pk=None):
        instance = self.get_object()
        from . import workflow_service
        updated, is_final = workflow_service.process_approval_action(
            instance, "Grievances", request.user, "reject",
            remark=request.data.get("remark"),
            target_status="Dismissed",
        )
        return Response(self.get_serializer(updated).data)


class HrmsSettingsView(APIView):
    """``GET/PUT /hrms/settings/`` -- the HRMS setup screen."""

    permission_classes = [HasModulePermission]
    permission_map = {"read": ["view_staff"], "write": ["edit_staff"]}

    def get(self, request):
        from apps.core.models import CompanyProfile, Setting

        row = Setting.objects.filter(client_id=request.client_id, key="hrms").first()
        data = dict(DEFAULT_HRMS_SETTINGS)
        if row and isinstance(row.value, dict):
            data.update(row.value)

        if not data.get("companyName"):
            cp = CompanyProfile.objects.filter(client_id=request.client_id).first()
            if cp and (cp.legal_name or cp.trade_name):
                data["companyName"] = cp.legal_name or cp.trade_name

        return Response(data)

    def put(self, request):
        from apps.core.models import CompanyProfile, Setting
        from .serializers import HrmsSettingsSerializer

        serializer = HrmsSettingsSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)

        row = Setting.objects.filter(client_id=request.client_id, key="hrms").first()
        merged = dict(DEFAULT_HRMS_SETTINGS)
        if row and isinstance(row.value, dict):
            merged.update(row.value)
        old_probation = merged.get("probationPeriod")
        merged.update(serializer.validated_data)
        new_probation = serializer.validated_data.get("probationPeriod")

        if new_probation and new_probation != old_probation:
            from . import lifecycle_service
            lifecycle_service.recalculate_probations_for_setting_change(
                request.client_id, new_probation, user=request.user
            )

        row, _ = Setting.objects.update_or_create(
            client_id=request.client_id,
            key="hrms",
            defaults={"value": merged, "updated_by": request.user},
        )

        c_name = str(merged.get("companyName") or "").strip()
        if c_name:
            try:
                CompanyProfile.objects.update_or_create(
                    client_id=request.client_id,
                    defaults={"legal_name": c_name, "trade_name": c_name},
                )
            except Exception:
                pass

        return Response(merged)


# ---------------------------------------------------------------------------
# Upgradation Scope: Transfers, Promotions, Warnings, Awards, Travel, Announcements
# ---------------------------------------------------------------------------
class EmployeeTransferViewSet(TenantModelViewSet):
    queryset = EmployeeTransfer.objects.select_related("employee", "from_department", "to_department", "from_location", "to_location")
    serializer_class = EmployeeTransferSerializer
    audit_entity_type = "EmployeeTransfer"
    audit_label_field = "transfer_number"
    status_field = "status"
    search_fields = ["transfer_number", "employee__name", "reason"]
    ordering = ["-effective_date", "-created_at"]
    filter_map = {"employeeId": "employee_id", "status": "status"}
    permission_map = {"read": ["view_staff"], "write": ["edit_staff"]}

    @transaction.atomic
    def perform_create(self, serializer):
        serializer.validated_data["transfer_number"] = allocate_number(self.request.user.client, "TRF")
        transfer = super().perform_create(serializer)
        emp = transfer.employee
        if transfer.to_department:
            emp.department = transfer.to_department
        if transfer.to_location:
            emp.location = transfer.to_location
        emp.save(update_fields=["department", "location", "updated_at"])
        try:
            from . import user_link
            user_link.push_to_user(emp)
        except Exception:
            pass

        # Notify the employee
        try:
            from apps.core.models import Notification
            from apps.accounts.models import User
            from .user_link import linked_user
            user = linked_user(emp)
            if not user and emp.email:
                user = User.objects.filter(
                    client=transfer.client, email__iexact=emp.email.strip(), deleted_at__isnull=True
                ).exclude(status="Deleted").first()
            if user:
                dept_name = transfer.to_department.name if transfer.to_department else "New Department"
                Notification.objects.create(
                    client=transfer.client,
                    recipient=user,
                    actor=getattr(self.request, "user", None),
                    type="hrms.transfer",
                    category="hrms",
                    entity_type="EmployeeTransfer",
                    entity_id=transfer.id,
                    title="Department Transfer Notice",
                    body=f"You have been transferred to {dept_name} effective {transfer.effective_date}. {transfer.reason or ''}".strip(),
                    channels=["in_app"],
                    payload={"path": "/portal/employee", "type": "transfer", "transferId": str(transfer.id)},
                )
        except Exception:
            pass
        return transfer


class EmployeePromotionViewSet(TenantModelViewSet):
    queryset = EmployeePromotion.objects.select_related("employee", "from_designation", "to_designation")
    serializer_class = EmployeePromotionSerializer
    audit_entity_type = "EmployeePromotion"
    audit_label_field = "promotion_number"
    status_field = "status"
    search_fields = ["promotion_number", "employee__name", "justification"]
    ordering = ["-effective_date", "-created_at"]
    filter_map = {"employeeId": "employee_id", "status": "status"}
    permission_map = {"read": ["view_staff"], "write": ["edit_staff"]}

    @transaction.atomic
    def perform_create(self, serializer):
        serializer.validated_data["promotion_number"] = allocate_number(self.request.user.client, "PRM")
        promotion = super().perform_create(serializer)
        emp = promotion.employee
        if promotion.to_designation:
            emp.designation = promotion.to_designation
        if promotion.new_salary:
            emp.standard_salary = promotion.new_salary
        emp.save(update_fields=["designation", "standard_salary", "updated_at"])
        try:
            from . import user_link
            user_link.push_to_user(emp)
        except Exception:
            pass

        # Notify the employee
        try:
            from apps.core.models import Notification
            from apps.accounts.models import User
            from .user_link import linked_user
            user = linked_user(emp)
            if not user and emp.email:
                user = User.objects.filter(
                    client=promotion.client, email__iexact=emp.email.strip(), deleted_at__isnull=True
                ).exclude(status="Deleted").first()
            if user:
                desig_name = promotion.to_designation.title if promotion.to_designation else "New Role"
                salary_text = f" with revised CTC Rs {promotion.new_salary}" if promotion.new_salary else ""
                Notification.objects.create(
                    client=promotion.client,
                    recipient=user,
                    actor=getattr(self.request, "user", None),
                    type="hrms.promotion",
                    category="hrms",
                    entity_type="EmployeePromotion",
                    entity_id=promotion.id,
                    title=f"Congratulations on Your Promotion to {desig_name}!",
                    body=f"Your promotion to {desig_name} is effective {promotion.effective_date}{salary_text}.",
                    channels=["in_app"],
                    payload={"path": "/portal/employee", "type": "promotion", "promotionId": str(promotion.id)},
                )
        except Exception:
            pass
        return promotion


class EmployeeWarningViewSet(TenantModelViewSet):
    queryset = EmployeeWarning.objects.select_related("employee")
    serializer_class = EmployeeWarningSerializer
    audit_entity_type = "EmployeeWarning"
    audit_label_field = "warning_number"
    status_field = "status"
    search_fields = ["warning_number", "employee__name", "subject", "description"]
    ordering = ["-issue_date", "-created_at"]
    filter_map = {"employeeId": "employee_id", "status": "status", "severity": "severity"}
    permission_map = {"read": ["view_staff"], "write": ["edit_staff"]}

    @transaction.atomic
    def perform_create(self, serializer):
        serializer.validated_data["warning_number"] = allocate_number(self.request.user.client, "WRN")
        warning = super().perform_create(serializer)

        # Notify the employee
        try:
            from apps.core.models import Notification
            from apps.accounts.models import User
            from .user_link import linked_user
            emp = warning.employee
            user = linked_user(emp)
            if not user and emp.email:
                user = User.objects.filter(
                    client=warning.client, email__iexact=emp.email.strip(), deleted_at__isnull=True
                ).exclude(status="Deleted").first()
            if user:
                Notification.objects.create(
                    client=warning.client,
                    recipient=user,
                    actor=getattr(self.request, "user", None),
                    type="hrms.warning",
                    category="hrms",
                    entity_type="EmployeeWarning",
                    entity_id=warning.id,
                    title=f"Disciplinary Notice ({warning.severity}): {warning.subject}",
                    body=f"Notice issued on {warning.issue_date} regarding '{warning.subject}'. {warning.description or ''}".strip(),
                    channels=["in_app"],
                    payload={"path": "/portal/employee", "type": "warning", "warningId": str(warning.id)},
                )
        except Exception:
            pass
        return warning


class EmployeeAwardViewSet(TenantModelViewSet):
    queryset = EmployeeAward.objects.select_related("employee")
    serializer_class = EmployeeAwardSerializer
    audit_entity_type = "EmployeeAward"
    audit_label_field = "award_number"
    status_field = None
    search_fields = ["award_number", "employee__name", "award_name", "category"]
    ordering = ["-award_date", "-created_at"]
    filter_map = {"employeeId": "employee_id", "category": "category"}
    permission_map = {"read": ["view_staff"], "write": ["edit_staff"]}

    @transaction.atomic
    def perform_create(self, serializer):
        serializer.validated_data["award_number"] = allocate_number(self.request.user.client, "AWD")
        award = super().perform_create(serializer)

        # Notify the employee
        try:
            from apps.core.models import Notification
            from apps.accounts.models import User
            from .user_link import linked_user
            emp = award.employee
            user = linked_user(emp)
            if not user and emp.email:
                user = User.objects.filter(
                    client=award.client, email__iexact=emp.email.strip(), deleted_at__isnull=True
                ).exclude(status="Deleted").first()
            if user:
                Notification.objects.create(
                    client=award.client,
                    recipient=user,
                    actor=getattr(self.request, "user", None),
                    type="hrms.award",
                    category="hrms",
                    entity_type="EmployeeAward",
                    entity_id=award.id,
                    title=f"Award Received: {award.award_name}!",
                    body=f"Congratulations! You were presented the {award.award_name} award on {award.award_date}. {award.citation or ''}".strip(),
                    channels=["in_app"],
                    payload={"path": "/portal/employee", "type": "award", "awardId": str(award.id)},
                )
        except Exception:
            pass
        return award


class TravelRequestViewSet(TenantModelViewSet):
    queryset = TravelRequest.objects.select_related("employee", "approval_chain")
    serializer_class = TravelRequestSerializer
    audit_entity_type = "TravelRequest"
    audit_label_field = "travel_number"
    status_field = "status"
    search_fields = ["travel_number", "employee__name", "destination", "purpose"]
    ordering = ["-start_date", "-created_at"]
    filter_map = {"employeeId": "employee_id", "status": "status"}
    permission_map = {"read": ["view_staff"], "write": ["edit_staff"]}

    def perform_create(self, serializer):
        serializer.validated_data["travel_number"] = allocate_number(self.request.user.client, "TRV")
        instance = super().perform_create(serializer)
        from . import workflow_service
        workflow_service.initialize_request_workflow(instance, "Travel Requisitions", self.request.user)
        return instance

    @action(detail=True, methods=["post"])
    def approve(self, request, pk=None):
        instance = self.get_object()
        from . import workflow_service
        updated, is_final = workflow_service.process_approval_action(
            instance, "Travel Requisitions", request.user, "approve",
            remark=request.data.get("remark"),
            target_status="Approved",
        )
        return Response(self.get_serializer(updated).data)

    @action(detail=True, methods=["post"])
    def reject(self, request, pk=None):
        instance = self.get_object()
        from . import workflow_service
        updated, is_final = workflow_service.process_approval_action(
            instance, "Travel Requisitions", request.user, "reject",
            remark=request.data.get("remark") or request.data.get("reason"),
        )
        return Response(self.get_serializer(updated).data)

    @action(detail=True, methods=["post"])
    def resubmit(self, request, pk=None):
        instance = self.get_object()
        from . import workflow_service
        updated, is_final = workflow_service.process_approval_action(
            instance, "Travel Requisitions", request.user, "resubmit",
            remark=request.data.get("remark"),
        )
        return Response(self.get_serializer(updated).data)

    @action(detail=True, methods=["post"])
    def settle(self, request, pk=None):
        instance = self.get_object()
        if instance.status not in ("Approved", "Disbursed", "Settled"):
            raise Conflict(
                f"Cannot settle travel requisition {instance.travel_number} with status '{instance.status}'. It must be approved first.",
                code="NOT_APPROVED",
            )
        advance_disbursed = request.data.get("advance_disbursed")
        actual_expenses = request.data.get("actual_expenses")
        settlement_notes = request.data.get("settlement_notes")
        if advance_disbursed is not None:
            instance.advance_disbursed = advance_disbursed
        if actual_expenses is not None:
            instance.actual_expenses = actual_expenses
        if settlement_notes is not None:
            instance.settlement_notes = settlement_notes
        instance.status = "Settled"
        instance.save(update_fields=["advance_disbursed", "actual_expenses", "settlement_notes", "status", "updated_at"])
        self.write_audit(
            action="settle",
            instance=instance,
            description=f"Settled travel requisition {instance.travel_number}.",
            after={
                "status": "Settled",
                "advance_disbursed": str(instance.advance_disbursed),
                "actual_expenses": str(instance.actual_expenses),
            },
        )
        return Response(self.get_serializer(instance).data)

    def perform_update(self, serializer):
        target_status = self.request.data.get("status")
        instance = serializer.instance
        if target_status and str(target_status).lower() == "settled":
            if instance.status not in ("Approved", "Disbursed", "Settled"):
                raise Conflict(
                    f"Cannot settle travel requisition {instance.travel_number} with status '{instance.status}'. It must be approved first.",
                    code="NOT_APPROVED",
                )
        if target_status and str(target_status).lower() in ("approved", "rejected") and str(target_status).capitalize() != instance.status:
            from . import workflow_service
            act = "approve" if str(target_status).lower() == "approved" else "reject"
            updated, _ = workflow_service.process_approval_action(
                instance, "Travel Requisitions", self.request.user, act,
                remark=self.request.data.get("remark"),
            )
            return updated
        return super().perform_update(serializer)


class AnnouncementViewSet(TenantModelViewSet):
    queryset = Announcement.objects.select_related("target_department")
    serializer_class = AnnouncementSerializer
    audit_entity_type = "Announcement"
    audit_label_field = "title"
    status_field = "status"
    search_fields = ["title", "content", "author_name"]
    ordering = ["-is_pinned", "-publish_date", "-created_at"]
    filter_map = {"targetDepartmentId": "target_department_id", "priority": "priority", "status": "status"}
    permission_map = {"read": [], "write": ["edit_staff"]}


    def get_queryset(self):
        qs = super().get_queryset()
        user = getattr(self.request, "user", None)
        if not user or not user.is_authenticated:
            return qs.none()
        from apps.core.permissions import has_permission
        if not has_permission(user, ("view_staff", "edit_staff")):
            today = timezone.localdate()
            qs = qs.filter(
                status="Published",
                publish_date__lte=today,
            ).filter(
                Q(expiry_date__isnull=True) | Q(expiry_date__gte=today)
            )
            emp = getattr(user, "employee", None)
            if emp and emp.department_id:
                qs = qs.filter(Q(target_department__isnull=True) | Q(target_department_id=emp.department_id))
            else:
                qs = qs.filter(target_department__isnull=True)
        return qs

    def perform_create(self, serializer):
        if not serializer.validated_data.get("author_name"):
            user = getattr(self.request, "user", None)
            serializer.validated_data["author_name"] = user.name if user and user.name else "HR Department"
        return super().perform_create(serializer)

    @action(detail=True, methods=["post"])
    def publish(self, request, pk=None):
        announcement = self.get_object()
        announcement.status = "Published"
        if not announcement.publish_date:
            announcement.publish_date = timezone.localdate()
        announcement.save(update_fields=["status", "publish_date", "updated_at"])
        self.write_audit(
            action="publish",
            instance=announcement,
            description=f"Published announcement '{announcement.title}'.",
            after={"status": "Published"},
        )
        return Response(self.get_serializer(announcement).data)

    @action(detail=True, methods=["post"])
    def archive(self, request, pk=None):
        announcement = self.get_object()
        announcement.status = "Archived"
        announcement.save(update_fields=["status", "updated_at"])
        self.write_audit(
            action="archive",
            instance=announcement,
            description=f"Archived announcement '{announcement.title}'.",
            after={"status": "Archived"},
        )
        return Response(self.get_serializer(announcement).data)


class ScreeningQuestionViewSet(TenantModelViewSet):
    queryset = ScreeningQuestion.objects.select_related("job")
    serializer_class = ScreeningQuestionSerializer
    audit_entity_type = "ScreeningQuestion"
    status_field = None
    ordering = ["sort_order", "created_at"]
    filter_map = {"jobId": "job_id", "isActive": "is_active"}
    permission_map = {"read": ["view_staff"], "write": ["create_staff"]}


class ScreeningAnswerViewSet(TenantModelViewSet):
    queryset = ScreeningAnswer.objects.select_related("application", "question")
    serializer_class = ScreeningAnswerSerializer
    audit_entity_type = "ScreeningAnswer"
    status_field = None
    ordering = ["created_at"]
    filter_map = {"applicationId": "application_id", "questionId": "question_id"}
    permission_map = {"read": ["view_staff"], "write": ["create_staff"]}


class MeetingRoomViewSet(TenantModelViewSet):
    queryset = MeetingRoom.objects.all()
    serializer_class = MeetingRoomSerializer
    audit_entity_type = "MeetingRoom"
    audit_label_field = "name"
    status_field = None
    search_fields = ["name", "code", "location"]
    ordering = ["name"]
    filter_map = {"isActive": "is_active"}
    permission_map = {"read": ["view_staff"], "write": ["edit_staff"]}


class CompanyMeetingViewSet(TenantModelViewSet):
    queryset = CompanyMeeting.objects.select_related("room", "host_user").prefetch_related("attendees")
    serializer_class = CompanyMeetingSerializer
    audit_entity_type = "CompanyMeeting"
    audit_label_field = "title"
    status_field = "status"
    search_fields = ["title", "agenda", "description"]
    ordering = ["start_time"]
    filter_map = {"roomId": "room_id", "status": "status", "hostUserId": "host_user_id"}
    permission_map = {"read": ["view_staff"], "write": ["edit_staff"]}

    def perform_create(self, serializer):
        data = serializer.validated_data
        room = data.get("room")
        start_time = data.get("start_time")
        end_time = data.get("end_time")

        if room and start_time and end_time:
            conflicts = CompanyMeeting.objects.filter(
                client=self.request.user.client,
                room=room,
                status__in=["scheduled", "in_progress"],
                start_time__lt=end_time,
                end_time__gt=start_time,
            )
            if conflicts.exists():
                raise ValidationFailed("Selected conference room is already booked for this time slot.")

        if not data.get("host_user"):
            serializer.validated_data["host_user"] = self.request.user

        video_provider = data.get("video_provider")
        join_url = data.get("join_url")
        if not join_url and video_provider in ["zoom", "google_meet"]:
            import uuid
            code = uuid.uuid4().hex[:10]
            if video_provider == "google_meet":
                serializer.validated_data["join_url"] = f"https://meet.google.com/{code[:3]}-{code[3:7]}-{code[7:10]}"
            else:
                serializer.validated_data["join_url"] = f"https://zoom.us/j/{uuid.uuid4().int % 10000000000}"

        return super().perform_create(serializer)

    def perform_update(self, serializer):
        data = serializer.validated_data
        instance = serializer.instance
        room = data.get("room", instance.room)
        start_time = data.get("start_time", instance.start_time)
        end_time = data.get("end_time", instance.end_time)

        if room and start_time and end_time:
            conflicts = CompanyMeeting.objects.filter(
                client=self.request.user.client,
                room=room,
                status__in=["scheduled", "in_progress"],
                start_time__lt=end_time,
                end_time__gt=start_time,
            ).exclude(pk=instance.pk)
            if conflicts.exists():
                raise ValidationFailed("Selected conference room is already booked for this time slot.")

        return super().perform_update(serializer)

    @action(detail=True, methods=["post"], url_path="mom")
    def save_mom(self, request, pk=None):
        meeting = self.get_object()
        mom_text = request.data.get("minutes_of_meeting", "")
        action_items = request.data.get("action_items", [])
        status_val = request.data.get("status") or "completed"

        meeting.minutes_of_meeting = mom_text
        meeting.action_items = action_items
        meeting.status = status_val
        meeting.save(update_fields=["minutes_of_meeting", "action_items", "status", "updated_at"])

        self.write_audit("save_mom", meeting, description=f"Updated MoM and action items for meeting {meeting.title}.")
        return Response(self.get_serializer(meeting).data)


