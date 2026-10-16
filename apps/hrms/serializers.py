"""HRMS serializers (api.md §11)."""
from django.utils import timezone
from rest_framework import serializers

from apps.core.exceptions import ValidationFailed
from apps.core.serializers import (
    BaseModelSerializer,
    BaseSerializer,
    MoneyField,
    TenantPrimaryKeyRelatedField,
)
from apps.accounts.models import User

from . import services
from .models import (
    Appraisal,
    AppraisalCycle,
    AppraisalHistory,
    AppraisalKpiScore,
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
    PayslipComponent,
    PerformanceIndicator,
    Policy,
    PolicyAcknowledgement,
    PolicyCategory,
    PolicyVersion,
    Resignation,
    ResignationChecklistItem,
    SalaryAdvance,
    SalaryStructure,
    ScreeningAnswer,
    ScreeningQuestion,
    Team,
    Termination,
    Trainer,
    Training,
    TrainingParticipant,
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


# ---------------------------------------------------------------------------
# Organisation (api.md §11.1)
# ---------------------------------------------------------------------------
class UniqueNameMixin:
    """Names are unique per tenant (a DB constraint); say so as a field error
    rather than letting the insert fail."""

    def validate_name(self, value):
        value = (value or "").strip()
        if not value:
            raise serializers.ValidationError("A name is required.")
        request = self.context.get("request")
        client_id = getattr(request, "client_id", None)
        taken = self.Meta.model.objects.filter(
            client_id=client_id, name__iexact=value, deleted_at__isnull=True
        )
        if self.instance is not None:
            taken = taken.exclude(pk=self.instance.pk)
        if client_id and taken.exists():
            raise serializers.ValidationError(f"'{value}' already exists.")
        return value


class DepartmentSerializer(UniqueNameMixin, BaseModelSerializer):
    head = serializers.CharField(source="head_employee.name", read_only=True)
    headAvatar = serializers.CharField(source="head_employee.avatar_url", read_only=True)
    headEmployeeId = TenantPrimaryKeyRelatedField(
        source="head_employee", model="hrms.Employee", required=False, allow_null=True
    )
    parentId = TenantPrimaryKeyRelatedField(
        source="parent", model="hrms.Department", required=False, allow_null=True
    )
    budget = serializers.DecimalField(
        max_digits=18, decimal_places=2, required=False, allow_null=True
    )
    #: Headcount, teams and open positions, annotated by the viewset.
    employees = serializers.SerializerMethodField()
    teams = serializers.SerializerMethodField()
    openRoles = serializers.SerializerMethodField()

    class Meta:
        model = Department
        fields = [
            "id", "name", "code", "head", "headAvatar", "headEmployeeId", "parent",
            "parentId", "status", "description", "budget", "employees", "teams",
            "openRoles", "created_at", "updated_at",
        ]
        read_only_fields = ["parent", "created_at", "updated_at"]

    def get_employees(self, department):
        return getattr(department, "employee_count", 0) or 0

    def get_teams(self, department):
        return getattr(department, "team_count", 0) or 0

    def get_openRoles(self, department):
        return getattr(department, "open_roles", 0) or 0

    def validate_headEmployeeId(self, head):
        # The Org Chart hangs the department's people under this person.
        if head is not None and head.status in ("Resigned", "Terminated"):
            raise serializers.ValidationError(f"{head.name} has left and cannot head a department.")
        return head

    def validate_parentId(self, parent):
        if parent is not None and self.instance is not None and parent.pk == self.instance.pk:
            raise serializers.ValidationError("A department cannot sit under itself.")
        return parent


class DesignationSerializer(UniqueNameMixin, BaseModelSerializer):
    department = serializers.CharField(source="department.name", read_only=True)
    departmentId = TenantPrimaryKeyRelatedField(
        source="department", model="hrms.Department", required=False, allow_null=True
    )
    level = serializers.IntegerField(required=False, allow_null=True, min_value=1, max_value=7)
    employees = serializers.SerializerMethodField()

    class Meta:
        model = Designation
        fields = [
            "id", "name", "level", "department", "departmentId", "employees",
            "created_at", "updated_at",
        ]
        read_only_fields = ["created_at", "updated_at"]

    def get_employees(self, designation):
        return getattr(designation, "employee_count", 0) or 0


class LocationAddressField(serializers.Field):
    """The screens edit an address as one line; older rows hold a dict of
    parts. Read as text either way, stored as ``{"line": ...}``."""

    def to_representation(self, value):
        if isinstance(value, dict):
            if value.get("line"):
                return value["line"]
            parts = [value.get(k) for k in ("line1", "line2", "city", "state", "pincode", "country")]
            return ", ".join(str(p) for p in parts if p)
        return value or ""

    def to_internal_value(self, data):
        if isinstance(data, dict):
            return data
        text = str(data or "").strip()
        return {"line": text} if text else {}


class LocationSerializer(UniqueNameMixin, BaseModelSerializer):
    type = serializers.ChoiceField(
        source="location_type", choices=Location.TYPES, required=False
    )
    address = LocationAddressField(required=False)
    employees = serializers.SerializerMethodField()

    class Meta:
        model = Location
        fields = [
            "id", "name", "type", "address", "timezone", "employees",
            "created_at", "updated_at",
        ]
        read_only_fields = ["created_at", "updated_at"]

    def get_employees(self, location):
        return getattr(location, "employee_count", 0) or 0


class EmployeeSerializer(BaseModelSerializer):
    employeeCode = serializers.CharField(source="employee_code", read_only=True)
    designation = serializers.CharField(source="designation.name", read_only=True)
    designationId = TenantPrimaryKeyRelatedField(
        source="designation", model="hrms.Designation", required=False, allow_null=True
    )
    department = serializers.CharField(source="department.name", read_only=True)
    departmentId = TenantPrimaryKeyRelatedField(
        source="department", model="hrms.Department", required=False, allow_null=True
    )
    manager = serializers.CharField(source="manager.name", read_only=True)
    managerId = TenantPrimaryKeyRelatedField(
        source="manager", model="hrms.Employee", required=False, allow_null=True
    )
    location = serializers.CharField(source="location.name", read_only=True)
    locationId = TenantPrimaryKeyRelatedField(
        source="location", model="hrms.Location", required=False, allow_null=True
    )
    joining = serializers.DateField(source="joining_date", required=False, allow_null=True)
    employmentType = serializers.CharField(
        source="employment_type", required=False, allow_null=True
    )
    salaryStructureId = TenantPrimaryKeyRelatedField(
        source="salary_structure", model="hrms.SalaryStructure",
        required=False, allow_null=True,
    )
    workSchedule = serializers.CharField(source="work_schedule.name", read_only=True)
    workScheduleId = TenantPrimaryKeyRelatedField(
        source="work_schedule", model="hrms.WorkSchedule",
        required=False, allow_null=True,
    )
    avatar = serializers.CharField(source="avatar_url", required=False, allow_null=True)
    #: The Administration login backed by this record (read-only; the link is
    #: made from either side's create, or from the user's "Linked employee").
    login = serializers.SerializerMethodField()

    class Meta:
        model = Employee
        fields = [
            "id", "employeeCode", "name", "email", "phone", "avatar", "login",
            "designation", "designationId", "department", "departmentId",
            "manager", "managerId", "location", "locationId", "joining",
            "employmentType", "shift", "workSchedule", "workScheduleId",
            "salaryStructureId", "standard_salary",
            "status", "date_of_birth", "gender", "blood_group", "personal_email",
            "emergency_contact", "address", "bank_account_number", "ifsc_code",
            "pan", "uan", "aadhaar_last4", "last_working_day",
            "termination_reason", "created_at", "updated_at",
        ]
        read_only_fields = ["employeeCode", "created_at", "updated_at"]

    def get_login(self, employee):
        if hasattr(employee, "linked_user_id"):
            if not employee.linked_user_id:
                return None
            return {
                "id": str(employee.linked_user_id),
                "email": employee.linked_user_email,
                "status": employee.linked_user_status,
                "role": employee.linked_user_role,
            }
        # Rows not read through the viewset's annotated queryset (a create).
        from .user_link import linked_user

        user = linked_user(employee)
        if user is None:
            return None
        return {
            "id": str(user.id),
            "email": user.email,
            "status": user.status,
            "role": user.role.name if user.role_id else None,
        }

    #: Pay on the employee record. Only payroll managers see colleagues' pay;
    #: everyone still sees their own (`/hrms/employees/me/`).
    PAY_FIELDS = ("standard_salary", "salaryStructureId")

    def to_representation(self, instance):
        data = super().to_representation(instance)
        request = self.context.get("request")
        user = getattr(request, "user", None)
        if user is None or getattr(user, "employee_id", None) == instance.pk:
            return data
        from apps.core.permissions import has_permission

        if not has_permission(user, ("generate_payroll", "approve_payroll", "edit_salary_structure")):
            for field in self.PAY_FIELDS:
                data.pop(field, None)
        return data

    def to_internal_value(self, data):
        if isinstance(data, dict):
            data = data.copy()
            if "joining" not in data and "joiningDate" in data:
                data["joining"] = data["joiningDate"]
            elif "joining" not in data and "doj" in data:
                data["joining"] = data["doj"]
        return super().to_internal_value(data)

    def validate(self, attrs):
        # Setting someone's pay is a payroll action, not a staff-record edit.
        pay_keys = {"standard_salary", "salary_structure"} & set(attrs)
        user = getattr(self.context.get("request"), "user", None)
        if pay_keys and user is not None:
            from apps.core.exceptions import PermissionDenied
            from apps.core.permissions import has_permission

            if not has_permission(user, ("generate_payroll", "approve_payroll", "edit_salary_structure")):
                raise PermissionDenied(
                    "Only payroll managers can change salary details.", code="generate_payroll"
                )

        if not attrs.get("joining_date") and not self.instance:
            attrs["joining_date"] = timezone.now().date()

        # Resolve department / designation / location if names were passed instead of IDs
        request = self.context.get("request")
        client_id = getattr(request, "client_id", None) or (
            getattr(getattr(request, "user", None), "client_id", None)
        )
        if client_id and hasattr(self, "initial_data") and isinstance(self.initial_data, dict):
            if not attrs.get("department") and self.initial_data.get("department"):
                d_name = str(self.initial_data["department"]).strip()
                if d_name and d_name != "All":
                    dept = Department.objects.filter(
                        client_id=client_id, name__iexact=d_name, deleted_at__isnull=True
                    ).first()
                    if not dept:
                        dept = Department.objects.create(client_id=client_id, name=d_name)
                    attrs["department"] = dept

            if not attrs.get("designation") and self.initial_data.get("designation"):
                des_name = str(self.initial_data["designation"]).strip()
                if des_name:
                    desig = Designation.objects.filter(
                        client_id=client_id, name__iexact=des_name, deleted_at__isnull=True
                    ).first()
                    if not desig:
                        desig = Designation.objects.create(client_id=client_id, name=des_name)
                    attrs["designation"] = desig

            if not attrs.get("location") and self.initial_data.get("location"):
                loc_name = str(self.initial_data["location"]).strip()
                if loc_name:
                    loc = Location.objects.filter(
                        client_id=client_id, name__iexact=loc_name, deleted_at__isnull=True
                    ).first()
                    if not loc:
                        loc = Location.objects.create(client_id=client_id, name=loc_name)
                    attrs["location"] = loc

        manager = attrs.get("manager")
        if manager is not None and manager.status in ("Resigned", "Terminated"):
            raise ValidationFailed(
                f"{manager.name} has left and cannot be a reporting manager.",
                field_errors={"managerId": ["Not an active employee."]},
            )
        if manager is not None and self.instance is not None:
            services.assert_no_manager_cycle(self.instance, manager.id)
        return attrs


# ---------------------------------------------------------------------------
# Attendance (api.md §11.2)
# ---------------------------------------------------------------------------
class AttendancePunchSerializer(BaseModelSerializer):
    employeeId = TenantPrimaryKeyRelatedField(source="employee", model="hrms.Employee", read_only=True)
    employeeName = serializers.CharField(source="employee.name", read_only=True)
    employeeCode = serializers.CharField(source="employee.employee_code", read_only=True)
    punchType = serializers.CharField(source="punch_type")
    punchTime = serializers.DateTimeField(source="punch_time")
    timeDisplay = serializers.SerializerMethodField()
    date = serializers.DateField(source="work_date", read_only=True)

    class Meta:
        model = AttendancePunch
        fields = [
            "id", "employeeId", "employeeName", "employeeCode", "date",
            "punchType", "punchTime", "timeDisplay", "source", "remark", "created_at"
        ]
        read_only_fields = ["id", "employeeId", "date", "created_at"]

    def get_timeDisplay(self, obj):
        local_dt = timezone.localtime(obj.punch_time) if timezone.is_aware(obj.punch_time) else obj.punch_time
        return local_dt.strftime("%I:%M %p")


class AttendanceSerializer(BaseModelSerializer):
    employeeId = TenantPrimaryKeyRelatedField(source="employee", model="hrms.Employee")
    employeeName = serializers.CharField(source="employee.name", read_only=True)
    name = serializers.CharField(source="employee.name", read_only=True)
    employeeCode = serializers.CharField(source="employee.employee_code", read_only=True)
    empId = serializers.CharField(source="employee.employee_code", read_only=True)
    department = serializers.CharField(source="employee.department.name", read_only=True)
    dept = serializers.CharField(source="employee.department.name", read_only=True)
    shift = serializers.CharField(source="employee.shift", read_only=True)
    date = serializers.DateField(source="work_date")
    checkIn = serializers.DateTimeField(source="check_in", required=False, allow_null=True)
    checkOut = serializers.DateTimeField(source="check_out", required=False, allow_null=True)
    firstPunch = serializers.DateTimeField(source="first_punch", required=False, allow_null=True)
    lastPunch = serializers.DateTimeField(source="last_punch", required=False, allow_null=True)
    workingHours = serializers.DecimalField(source="working_hours", max_digits=6, decimal_places=2, required=False, allow_null=True)
    lateMinutes = serializers.IntegerField(source="late_minutes", required=False)
    earlyLeavingMinutes = serializers.IntegerField(source="early_leaving_minutes", required=False)
    overtimeHours = serializers.DecimalField(source="overtime_hours", max_digits=6, decimal_places=2, required=False)
    punches = serializers.SerializerMethodField()
    formattedWorkingHours = serializers.SerializerMethodField()
    lateDisplay = serializers.SerializerMethodField()
    earlyDisplay = serializers.SerializerMethodField()
    isEarlyOut = serializers.SerializerMethodField()
    overtimeDisplay = serializers.SerializerMethodField()

    class Meta:
        model = Attendance
        fields = [
            "id", "employeeId", "employeeName", "name", "employeeCode", "empId",
            "department", "dept", "shift",
            "date", "checkIn", "checkOut", "firstPunch", "lastPunch",
            "hours", "workingHours", "lateMinutes", "earlyLeavingMinutes", "overtimeHours",
            "status", "source", "remark", "leave_request",
            "punches", "formattedWorkingHours", "lateDisplay", "earlyDisplay", "isEarlyOut", "overtimeDisplay",
            "created_at", "updated_at",
        ]
        # `hours` and the Late / Half Day verdict come from the flexibility
        # policy, applied server-side (api.md §11.2).
        read_only_fields = [
            "hours", "workingHours", "firstPunch", "lastPunch", "lateMinutes",
            "earlyLeavingMinutes", "overtimeHours", "punches", "formattedWorkingHours",
            "lateDisplay", "earlyDisplay", "isEarlyOut", "overtimeDisplay",
            "source", "leave_request", "created_at", "updated_at"
        ]

    def get_punches(self, obj):
        qs = obj.punches.all().order_by("punch_time")
        return [
            {
                "id": str(p.id),
                "punchType": p.punch_type,
                "punchTime": p.punch_time.isoformat(),
                "timeDisplay": (
                    timezone.localtime(p.punch_time)
                    if timezone.is_aware(p.punch_time)
                    else p.punch_time
                ).strftime("%I:%M %p"),
                "source": p.source,
                "remark": p.remark,
            }
            for p in qs
        ]

    def get_formattedWorkingHours(self, obj):
        hours = float(obj.working_hours or obj.hours or 0)
        if obj.work_date == timezone.localdate() and obj.check_out is None:
            last_p = obj.punches.order_by("-punch_time").first()
            if last_p and last_p.punch_type == "IN":
                active_secs = max(0, (timezone.now() - last_p.punch_time).total_seconds())
                total_minutes = int(hours * 60 + (active_secs // 60))
                h = total_minutes // 60
                m = total_minutes % 60
                return f"{h:02d}h {m:02d}m"
        total_minutes = int(hours * 60)
        h = total_minutes // 60
        m = total_minutes % 60
        return f"{h:02d}h {m:02d}m"

    def get_lateDisplay(self, obj):
        if obj.late_minutes and obj.late_minutes > 0:
            return f"{obj.late_minutes} min late"
        return "On time"

    def get_overtimeDisplay(self, obj):
        if obj.overtime_hours and float(obj.overtime_hours) > 0:
            total_minutes = int(float(obj.overtime_hours) * 60)
            h = total_minutes // 60
            m = total_minutes % 60
            return f"+{h:02d}h {m:02d}m"
        return "0h 00m"

    def get_earlyDisplay(self, obj):
        mins = getattr(obj, "early_leaving_minutes", 0) or 0
        if mins > 0:
            return f"{mins} min early"
        return "-"

    def get_isEarlyOut(self, obj):
        mins = getattr(obj, "early_leaving_minutes", 0) or 0
        if mins > 0:
            return True
        if obj.check_out is not None and obj.working_hours is not None and obj.working_hours < 8:
            return True
        return False

    def to_internal_value(self, data):
        data = data.copy() if hasattr(data, "copy") else dict(data)
        work_date = data.get("date") or data.get("work_date")
        if work_date:
            for time_field in ("checkIn", "check_in"):
                val = data.get(time_field)
                if val and isinstance(val, str) and len(val) <= 8 and ":" in val:
                    data[time_field] = f"{work_date}T{val}:00" if len(val) == 5 else f"{work_date}T{val}"
            for time_field in ("checkOut", "check_out"):
                val = data.get(time_field)
                if val and isinstance(val, str) and len(val) <= 8 and ":" in val:
                    data[time_field] = f"{work_date}T{val}:00" if len(val) == 5 else f"{work_date}T{val}"

        emp_id = data.get("employeeId") or data.get("employee_id")
        if emp_id and isinstance(emp_id, str):
            import uuid
            try:
                uuid.UUID(emp_id)
            except ValueError:
                from apps.hrms.models import Employee
                request = self.context.get("request")
                client_id = getattr(request, "client_id", None) or (request.user.client_id if request and hasattr(request, "user") else None)
                if client_id:
                    emp = Employee.objects.filter(
                        employee_code=emp_id, client_id=client_id, deleted_at__isnull=True
                    ).first()
                    if emp:
                        data["employeeId"] = str(emp.id)
        return super().to_internal_value(data)



class BulkAttendanceSerializer(BaseSerializer):
    date = serializers.DateField(required=False)
    records = serializers.ListField(child=serializers.DictField(), required=False, default=list)
    ids = serializers.ListField(child=serializers.CharField(), required=False, default=list)
    status = serializers.CharField(required=False, allow_null=True)


class RegularizationSerializer(BaseModelSerializer):
    employeeId = TenantPrimaryKeyRelatedField(
        source="employee", model="hrms.Employee", required=False, allow_null=True
    )
    employeeName = serializers.CharField(source="employee.name", read_only=True)
    date = serializers.DateField(source="work_date")
    requestedCheckIn = serializers.DateTimeField(
        source="requested_check_in", required=False, allow_null=True
    )
    requestedCheckOut = serializers.DateTimeField(
        source="requested_check_out", required=False, allow_null=True
    )
    requestedStatus = serializers.CharField(
        source="requested_status", required=False, allow_null=True
    )

    class Meta:
        model = AttendanceRegularization
        fields = [
            "id", "employeeId", "employeeName", "date", "requestedCheckIn",
            "requestedCheckOut", "requestedStatus", "reason", "status",
            "approver", "decided_at", "remark", "created_at",
        ]
        read_only_fields = ["status", "approver", "decided_at", "created_at"]

    def to_internal_value(self, data):
        data = data.copy() if hasattr(data, "copy") else dict(data)
        work_date = data.get("date") or data.get("work_date")
        if work_date:
            for time_field in ("requestedCheckIn", "requested_check_in"):
                val = data.get(time_field)
                if val and isinstance(val, str) and len(val) <= 8 and ":" in val:
                    data[time_field] = f"{work_date}T{val}:00" if len(val) == 5 else f"{work_date}T{val}"
            for time_field in ("requestedCheckOut", "requested_check_out"):
                val = data.get(time_field)
                if val and isinstance(val, str) and len(val) <= 8 and ":" in val:
                    data[time_field] = f"{work_date}T{val}:00" if len(val) == 5 else f"{work_date}T{val}"

        emp_id = data.get("employeeId") or data.get("employee_id")
        if emp_id and isinstance(emp_id, str):
            import uuid
            try:
                uuid.UUID(emp_id)
            except ValueError:
                from apps.hrms.models import Employee
                request = self.context.get("request")
                client_id = getattr(request, "client_id", None) or (request.user.client_id if request and hasattr(request, "user") else None)
                if client_id:
                    emp = Employee.objects.filter(
                        employee_code=emp_id, client_id=client_id, deleted_at__isnull=True
                    ).first()
                    if emp:
                        data["employeeId"] = str(emp.id)
        return super().to_internal_value(data)


# ---------------------------------------------------------------------------
# Leave (api.md §11.3)
# ---------------------------------------------------------------------------
class LeaveTypeSerializer(BaseModelSerializer):
    class Meta:
        model = LeaveType
        fields = [
            "id", "name", "code", "annual_entitlement", "accrual",
            "carry_forward_cap", "is_encashable", "is_paid", "enforce_sandwich_rule",
            "created_at",
        ]


class LeaveRequestSerializer(BaseModelSerializer):
    employeeId = TenantPrimaryKeyRelatedField(
        source="employee", model="hrms.Employee", required=False, allow_null=True
    )
    employeeName = serializers.CharField(source="employee.name", read_only=True)
    employeeCode = serializers.CharField(source="employee.employee_code", read_only=True)
    type = serializers.CharField(source="leave_type.name", read_only=True)
    leaveTypeId = TenantPrimaryKeyRelatedField(
        source="leave_type", model="hrms.LeaveType"
    )
    fromDate = serializers.DateField(source="from_date")
    toDate = serializers.DateField(source="to_date")
    delegateId = TenantPrimaryKeyRelatedField(
        source="delegate_employee", model="hrms.Employee",
        required=False, allow_null=True,
    )
    # Who covers the work, by name — the handover tables show it.
    delegate = serializers.CharField(
        source="delegate_employee.name", read_only=True, default=""
    )
    pmsTask = serializers.SerializerMethodField()
    crmTask = serializers.SerializerMethodField()
    currentStep = serializers.IntegerField(source="current_step", read_only=True)
    approvalChainId = serializers.CharField(source="approval_chain_id", read_only=True)
    approvalHistory = serializers.JSONField(source="approval_history", read_only=True)
    approverName = serializers.CharField(source="approver.name", read_only=True, default="")
    nextApproverRole = serializers.SerializerMethodField()

    class Meta:
        model = LeaveRequest
        fields = [
            "id", "employeeId", "employeeName", "employeeCode", "type",
            "leaveTypeId", "fromDate", "toDate", "days", "reason", "delegateId", "delegate",
            "delegate_confirmed_at", "status", "approver", "approverName", "decided_at",
            "remark", "currentStep", "approvalChainId", "approvalHistory", "nextApproverRole",
            "created_at", "updated_at", "pmsTask", "crmTask",
        ]
        read_only_fields = [
            "status", "approver", "approverName", "decided_at", "delegate_confirmed_at",
            "currentStep", "approvalChainId", "approvalHistory", "nextApproverRole",
            "created_at", "updated_at", "pmsTask", "crmTask",
        ]

    def get_nextApproverRole(self, obj):
        if not obj.approval_chain or not isinstance(obj.approval_chain.steps, list):
            return "Direct Line Manager" if obj.status in ("Pending Review", "Delegate Confirmed") else None
        steps = obj.approval_chain.steps
        step_num = obj.current_step or 1
        for s in steps:
            if (s.get("tier") or s.get("level")) == step_num:
                return s.get("role")
        return steps[-1].get("role") if steps else None

    def get_pmsTask(self, obj):
        try:
            from apps.pms.models import DelegatedTask
            task = DelegatedTask.objects.filter(
                client_id=obj.client_id,
                due_date=obj.to_date,
                title__contains=obj.employee.name if obj.employee else "",
                deleted_at__isnull=True,
            ).first()
            if task:
                return {
                    "id": str(task.id),
                    "taskNumber": task.task_number,
                    "title": task.title,
                    "status": task.status,
                    "priority": task.priority,
                    "dueDate": str(task.due_date) if task.due_date else None,
                }
        except Exception:
            pass
        return None

    def get_crmTask(self, obj):
        try:
            from apps.crm.models import Task as CrmTask
            task = CrmTask.objects.filter(
                client_id=obj.client_id,
                due_date=obj.to_date,
                title__contains=obj.employee.name if obj.employee else "",
                deleted_at__isnull=True,
            ).first()
            if task:
                return {
                    "id": str(task.id),
                    "taskNumber": task.task_number or f"CRM-{str(task.id)[:8]}",
                    "title": task.title,
                    "status": task.status,
                    "priority": task.priority,
                    "dueDate": str(task.due_date) if task.due_date else None,
                }
        except Exception:
            pass
        return None

    def validate(self, attrs):
        start = attrs.get("from_date") or getattr(self.instance, "from_date", None)
        end = attrs.get("to_date") or getattr(self.instance, "to_date", None)
        if start and end and end < start:
            raise serializers.ValidationError({"toDate": ["Must be on or after the start date."]})
        return attrs


class LeaveBalanceSerializer(BaseModelSerializer):
    employeeName = serializers.CharField(source="employee.name", read_only=True)
    employeeCode = serializers.CharField(source="employee.employee_code", read_only=True)
    leaveType = serializers.CharField(source="leave_type.name", read_only=True)
    # GeneratedField (computed by Postgres) -- DRF falls back to ModelField
    # for it, which drf-spectacular cannot map (DecimalField() with no args).
    # Declared explicitly so schema generation sees a real DecimalField.
    balance = serializers.DecimalField(
        max_digits=6, decimal_places=2, coerce_to_string=False, read_only=True
    )

    class Meta:
        model = LeaveBalance
        fields = [
            "id", "employee", "employeeName", "employeeCode", "leave_type",
            "leaveType", "period_year", "entitlement", "carried_forward",
            "used", "encashed", "balance",
        ]
        read_only_fields = ["balance", "used"]


class CompOffSerializer(BaseModelSerializer):
    employeeId = TenantPrimaryKeyRelatedField(source="employee", model="hrms.Employee")
    workedDate = serializers.DateField(source="worked_date")
    workType = serializers.CharField(source="work_type", required=False, allow_null=True)
    expiryDate = serializers.DateField(source="expiry_date", required=False, allow_null=True)

    class Meta:
        model = CompOff
        fields = [
            "id", "employeeId", "workedDate", "workType", "days", "expiryDate",
            "used", "created_at",
        ]


class LeaveEncashmentSerializer(BaseModelSerializer):
    employeeId = TenantPrimaryKeyRelatedField(source="employee", model="hrms.Employee")
    leaveTypeId = TenantPrimaryKeyRelatedField(source="leave_type", model="hrms.LeaveType")
    perDayRate = MoneyField(source="per_day_rate")
    # GeneratedField (days * per_day_rate) -- see LeaveBalanceSerializer.balance.
    amount = MoneyField(read_only=True)

    class Meta:
        model = LeaveEncashment
        fields = [
            "id", "employeeId", "leaveTypeId", "days", "perDayRate", "amount",
            "status", "created_at",
        ]
        read_only_fields = ["amount"]


# ---------------------------------------------------------------------------
# Payroll (api.md §11.4)
# ---------------------------------------------------------------------------
class SalaryStructureSerializer(BaseModelSerializer):
    # The API name for the model's ``overtime_monthly_cap_hours``.
    max_overtime_hours_month = serializers.DecimalField(
        source="overtime_monthly_cap_hours", max_digits=6, decimal_places=2, required=False
    )

    class Meta:
        model = SalaryStructure
        fields = [
            "id", "name", "basic_pct", "hra_pct", "components",
            "max_overtime_hours_month", "overtime_rate_multiplier", "is_active",
            "created_at", "updated_at",
        ]



class PayslipComponentSerializer(BaseModelSerializer):
    class Meta:
        model = PayslipComponent
        fields = ["id", "name", "kind", "amount"]


class PayslipSerializer(BaseModelSerializer):
    """The payroll row api.md §11.4 documents."""

    empId = serializers.CharField(source="employee.employee_code", read_only=True)
    employeeId = serializers.CharField(source="employee_id", read_only=True)
    name = serializers.CharField(source="employee.name", read_only=True)
    role = serializers.CharField(source="employee.designation.name", read_only=True)
    department = serializers.CharField(source="employee.department.name", read_only=True)
    month = serializers.DateField(source="period_month", read_only=True)
    standardSalary = MoneyField(source="standard_salary", read_only=True)
    earnedSalary = MoneyField(source="earned_salary", read_only=True)
    additionalEarnings = MoneyField(source="additional_earnings", required=False)
    advance = MoneyField(source="advance_recovery", required=False)
    paidLeaves = serializers.DecimalField(
        source="paid_leaves", max_digits=6, decimal_places=2,
        coerce_to_string=False, read_only=True,
    )
    attendedDays = serializers.DecimalField(
        source="attended_days", max_digits=6, decimal_places=2,
        coerce_to_string=False, read_only=True,
    )
    totalDays = serializers.DecimalField(
        source="total_days", max_digits=6, decimal_places=2,
        coerce_to_string=False, read_only=True,
    )
    paidAmount = MoneyField(source="paid_amount", read_only=True)
    paymentDate = serializers.DateField(source="payment_date", read_only=True)
    bank = serializers.CharField(source="bank_account.name", read_only=True)
    components = PayslipComponentSerializer(many=True, read_only=True)
    netPayable = MoneyField(source="net_payable", read_only=True)

    class Meta:
        model = Payslip
        fields = [
            "id", "empId", "employeeId", "name", "role", "department", "month",
            "standardSalary", "earnedSalary", "additionalEarnings", "deductions",
            "advance", "paidLeaves", "basic", "hra", "allowances", "status",
            "paidAmount", "paymentDate", "bank", "attendedDays", "totalDays",
            "netPayable", "earned_salary_overridden", "components",
            "created_at", "updated_at",
        ]
        read_only_fields = ["status", "earned_salary_overridden", "created_at", "updated_at"]


class PayrollRunSerializer(BaseModelSerializer):
    payslipCount = serializers.SerializerMethodField()

    class Meta:
        model = PayrollRun
        fields = [
            "id", "period_month", "status", "processed_by", "processed_at",
            "approved_by", "approved_at", "payslipCount", "created_at",
        ]

    def get_payslipCount(self, run):
        return run.payslips.filter(deleted_at__isnull=True).count()


class ProcessPayrollSerializer(BaseSerializer):
    month = serializers.DateField()
    employeeIds = serializers.ListField(
        child=serializers.CharField(), required=False, default=list
    )


class SalaryAdvanceSerializer(BaseModelSerializer):
    employeeId = TenantPrimaryKeyRelatedField(source="employee", model="hrms.Employee")
    employeeName = serializers.CharField(source="employee.name", read_only=True)
    issuedOn = serializers.DateField(source="issued_on")
    recoveredAmount = MoneyField(source="recovered_amount", read_only=True)
    outstanding = serializers.SerializerMethodField()

    class Meta:
        model = SalaryAdvance
        fields = [
            "id", "employeeId", "employeeName", "amount", "issuedOn",
            "installments", "recoveredAmount", "outstanding", "status",
            "notes", "created_at",
        ]
        read_only_fields = ["status", "created_at"]

    def get_outstanding(self, advance):
        return advance.outstanding


# ---------------------------------------------------------------------------
# Recruitment (api.md §11.5)
# ---------------------------------------------------------------------------
class JobSerializer(BaseModelSerializer):
    department = serializers.CharField(source="department.name", read_only=True)
    departmentId = TenantPrimaryKeyRelatedField(
        source="department", model="hrms.Department", required=False, allow_null=True
    )
    designationId = TenantPrimaryKeyRelatedField(
        source="designation", model="hrms.Designation", required=False, allow_null=True
    )
    locationId = TenantPrimaryKeyRelatedField(
        source="location", model="hrms.Location", required=False, allow_null=True
    )
    applicantCount = serializers.SerializerMethodField()

    class Meta:
        model = Job
        fields = [
            "id", "title", "department", "departmentId", "designationId",
            "locationId", "openings", "experience_range", "salary_range",
            "employment_type", "description", "status", "is_published",
            "published_at", "slug", "applicantCount", "created_at", "updated_at",
        ]
        read_only_fields = ["published_at", "slug", "created_at", "updated_at"]

    def get_applicantCount(self, job):
        return getattr(job, "applicant_count", None)


class CandidateSerializer(BaseModelSerializer):
    resumeFileId = TenantPrimaryKeyRelatedField(
        source="resume_file", model="core.File", required=False, allow_null=True
    )

    class Meta:
        model = Candidate
        fields = [
            "id", "name", "email", "phone", "resumeFileId", "source",
            "current_ctc", "expected_ctc", "notice_period_days", "stage",
            "rating", "employee", "notes", "created_at", "updated_at",
        ]
        read_only_fields = ["employee", "created_at", "updated_at"]


class ApplicationSerializer(BaseModelSerializer):
    candidateId = TenantPrimaryKeyRelatedField(source="candidate", model="hrms.Candidate")
    candidateName = serializers.CharField(source="candidate.name", read_only=True)
    jobId = TenantPrimaryKeyRelatedField(source="job", model="hrms.Job")
    jobTitle = serializers.CharField(source="job.title", read_only=True)

    class Meta:
        model = Application
        fields = [
            "id", "candidateId", "candidateName", "jobId", "jobTitle",
            "applied_at", "stage", "rejection_reason",
        ]


class InterviewSerializer(BaseModelSerializer):
    applicationId = TenantPrimaryKeyRelatedField(
        source="application", model="hrms.Application"
    )
    candidateName = serializers.CharField(
        source="application.candidate.name", read_only=True
    )
    scheduledAt = serializers.DateTimeField(source="scheduled_at")
    panelUserIds = serializers.JSONField(source="panel_user_ids", required=False)

    class Meta:
        model = Interview
        fields = [
            "id", "applicationId", "candidateName", "round", "scheduledAt",
            "mode", "panelUserIds", "status", "rating", "notes",
            "recommendation", "created_at",
        ]


class OfferSerializer(BaseModelSerializer):
    applicationId = TenantPrimaryKeyRelatedField(
        source="application", model="hrms.Application", required=False, allow_null=True
    )
    candidateId = TenantPrimaryKeyRelatedField(
        source="candidate", model="hrms.Candidate", required=False, allow_null=True
    )
    candidateName = serializers.CharField(
        source="candidate_name", required=False, allow_blank=True
    )
    email = serializers.EmailField(required=False, allow_blank=True)
    position = serializers.CharField(required=False, allow_blank=True)
    department = serializers.CharField(required=False, allow_blank=True)
    dept = serializers.CharField(source="department", required=False, allow_blank=True)
    location = serializers.CharField(required=False, allow_blank=True)
    jobType = serializers.CharField(source="job_type", required=False, allow_blank=True)
    workMode = serializers.CharField(source="work_mode", required=False, allow_blank=True)
    salary = serializers.CharField(required=False, allow_blank=True)
    offeredCtc = MoneyField(source="offered_ctc", required=False, allow_null=True)
    joiningDate = serializers.DateField(
        source="joining_date", required=False, allow_null=True
    )
    expiryDate = serializers.DateField(
        source="expiry_date", required=False, allow_null=True
    )
    reportingManager = serializers.CharField(
        source="reporting_manager", required=False, allow_blank=True
    )
    probationPeriod = serializers.CharField(
        source="probation_period", required=False, allow_blank=True
    )
    letterContent = serializers.JSONField(
        source="letter_content", required=False, allow_null=True
    )
    approvalChainId = serializers.CharField(source="approval_chain_id", read_only=True)
    currentStep = serializers.IntegerField(source="current_step", read_only=True)
    approvalHistory = serializers.JSONField(source="approval_history", read_only=True)
    approverName = serializers.CharField(source="approver.name", read_only=True, default="")
    decidedAt = serializers.DateTimeField(source="decided_at", read_only=True)
    sentDate = serializers.SerializerMethodField()

    class Meta:
        model = Offer
        fields = [
            "id", "applicationId", "candidateId", "candidateName", "email", "position",
            "department", "dept", "location", "jobType", "workMode", "salary",
            "offeredCtc", "joiningDate", "expiryDate", "reportingManager",
            "probationPeriod", "letterContent", "sentDate", "status",
            "approvalChainId", "currentStep", "approvalHistory", "approverName", "decidedAt",
            "letter_file", "sent_at", "responded_at", "notes", "created_at",
        ]
        read_only_fields = [
            "sent_at", "responded_at", "created_at",
            "approvalChainId", "currentStep", "approvalHistory", "approverName", "decidedAt"
        ]

    def validate(self, data):
        data = super().validate(data)
        joining = data.get("joining_date")
        expiry = data.get("expiry_date")
        if joining and expiry and expiry > joining:
            raise serializers.ValidationError({
                "expiryDate": "Offer expiry date cannot be later than joining date."
            })
        return data

    def get_sentDate(self, obj):
        dt = obj.sent_at or obj.created_at
        return dt.date().isoformat() if dt else ""

    def to_internal_value(self, data):
        data = data.copy() if hasattr(data, "copy") else dict(data)
        if "dept" in data and "department" not in data:
            data["department"] = data["dept"]
        if "jobType" in data and "job_type" not in data:
            data["job_type"] = data["jobType"]
        if "workMode" in data and "work_mode" not in data:
            data["work_mode"] = data["workMode"]
        if "expiryDate" in data and "expiry_date" not in data:
            data["expiry_date"] = data["expiryDate"]
        if "reportingManager" in data and "reporting_manager" not in data:
            data["reporting_manager"] = data["reportingManager"]
        if "probationPeriod" in data and "probation_period" not in data:
            data["probation_period"] = data["probationPeriod"]
        if "letterContent" in data and "letter_content" not in data:
            data["letter_content"] = data["letterContent"]

        salary_raw = data.get("salary")
        if salary_raw is not None and not data.get("offered_ctc") and not data.get("offeredCtc"):
            import re
            nums = re.findall(r"[\d]+(?:[.,]\d+)*", str(salary_raw))
            if nums:
                clean_num = nums[0].replace(",", "")
                try:
                    data["offeredCtc"] = float(clean_num)
                    data["offered_ctc"] = float(clean_num)
                except ValueError:
                    pass

        return super().to_internal_value(data)

    def create(self, validated_data):
        validated_data.pop("salary", None)
        return super().create(validated_data)

    def update(self, instance, validated_data):
        validated_data.pop("salary", None)
        return super().update(instance, validated_data)

    def to_representation(self, instance):
        ret = super().to_representation(instance)
        cand_name = instance.candidate_name
        if not cand_name:
            if instance.candidate:
                cand_name = instance.candidate.name
            elif instance.application and instance.application.candidate:
                cand_name = instance.application.candidate.name
        ret["candidateName"] = cand_name or "Candidate"
        if not ret.get("email") and instance.candidate and instance.candidate.email:
            ret["email"] = instance.candidate.email
        if not ret.get("salary"):
            if instance.offered_ctc:
                ret["salary"] = f"${int(instance.offered_ctc):,} / year"
            elif instance.letter_content and isinstance(instance.letter_content, dict) and instance.letter_content.get("salary"):
                ret["salary"] = instance.letter_content.get("salary")
            else:
                ret["salary"] = "Competitive"
        ret["dept"] = instance.department or ""
        ret["jobType"] = instance.job_type or "Full-time"
        ret["workMode"] = instance.work_mode or "Hybrid"
        return ret


class OnboardingTaskSerializer(BaseModelSerializer):
    candidateId = TenantPrimaryKeyRelatedField(source="candidate", model="hrms.Candidate")

    class Meta:
        model = OnboardingTask
        fields = ["id", "candidateId", "title", "owner", "due_date", "completed_at"]


class ScreeningQuestionSerializer(BaseModelSerializer):
    jobId = TenantPrimaryKeyRelatedField(
        source="job", model="hrms.Job", required=False, allow_null=True
    )
    jobTitle = serializers.CharField(source="job.title", read_only=True)

    class Meta:
        model = ScreeningQuestion
        fields = ["id", "jobId", "jobTitle", "question", "type", "options", "is_active", "sort_order", "created_at"]


class ScreeningAnswerSerializer(BaseModelSerializer):
    applicationId = TenantPrimaryKeyRelatedField(source="application", model="hrms.Application")
    questionId = TenantPrimaryKeyRelatedField(source="question", model="hrms.ScreeningQuestion")
    questionText = serializers.CharField(source="question.question", read_only=True)

    class Meta:
        model = ScreeningAnswer
        fields = ["id", "applicationId", "questionId", "questionText", "answer", "created_at"]



# ---------------------------------------------------------------------------
# Performance (api.md §11.6)
# ---------------------------------------------------------------------------
class AppraisalCycleSerializer(BaseModelSerializer):
    class Meta:
        model = AppraisalCycle
        fields = [
            "id", "name", "period_start", "period_end", "status",
            "participants_scope", "created_at",
        ]


class PerformanceIndicatorSerializer(BaseModelSerializer):
    departmentId = TenantPrimaryKeyRelatedField(
        source="department", model="hrms.Department", required=False, allow_null=True
    )

    class Meta:
        model = PerformanceIndicator
        fields = ["id", "name", "category", "weight", "departmentId", "created_at"]


class KpiSerializer(BaseModelSerializer):
    indicatorId = TenantPrimaryKeyRelatedField(
        source="indicator", model="hrms.PerformanceIndicator",
        required=False, allow_null=True,
    )

    class Meta:
        model = Kpi
        fields = ["id", "indicatorId", "name", "target", "unit", "applies_to", "created_at"]


class AppraisalKpiScoreSerializer(BaseModelSerializer):
    kpiId = TenantPrimaryKeyRelatedField(source="kpi", model="hrms.Kpi")
    kpiName = serializers.CharField(source="kpi.name", read_only=True)

    class Meta:
        model = AppraisalKpiScore
        fields = ["id", "kpiId", "kpiName", "target", "achieved", "score", "weight"]


class AppraisalHistorySerializer(BaseModelSerializer):
    actorName = serializers.CharField(source="actor.name", read_only=True)

    class Meta:
        model = AppraisalHistory
        fields = [
            "id", "from_stage", "to_stage", "actor", "actorName", "action",
            "comment", "created_at",
        ]


class AppraisalSerializer(BaseModelSerializer):
    employeeId = TenantPrimaryKeyRelatedField(source="employee", model="hrms.Employee")
    employeeName = serializers.CharField(source="employee.name", read_only=True)
    managerId = TenantPrimaryKeyRelatedField(
        source="manager", model="hrms.Employee", required=False, allow_null=True
    )
    cycleId = TenantPrimaryKeyRelatedField(source="cycle", model="hrms.AppraisalCycle")
    kpiScores = AppraisalKpiScoreSerializer(source="kpi_scores", many=True, read_only=True)
    history = AppraisalHistorySerializer(many=True, read_only=True)

    class Meta:
        model = Appraisal
        fields = [
            "id", "cycleId", "employeeId", "employeeName", "managerId", "stage",
            "status", "self_rating", "manager_rating", "final_rating",
            "strengths", "areas_for_improvement", "development_feedback",
            "hr_comments", "self_comments", "kpiScores", "history",
            "created_at", "updated_at",
        ]
        read_only_fields = ["stage", "status", "created_at", "updated_at"]


class GoalSerializer(BaseModelSerializer):
    employeeId = TenantPrimaryKeyRelatedField(source="employee", model="hrms.Employee")

    class Meta:
        model = Goal
        fields = [
            "id", "employeeId", "cycle", "title", "description", "target_date",
            "progress_pct", "status", "created_at",
        ]


# ---------------------------------------------------------------------------
# Training (api.md §11.7)
# ---------------------------------------------------------------------------
class TrainerSerializer(BaseModelSerializer):
    class Meta:
        model = Trainer
        fields = [
            "id", "name", "kind", "employee", "organisation", "expertise",
            "rate", "contact", "created_at",
        ]


class TrainingParticipantSerializer(BaseModelSerializer):
    employeeId = TenantPrimaryKeyRelatedField(source="employee", model="hrms.Employee")
    employeeName = serializers.CharField(source="employee.name", read_only=True)

    class Meta:
        model = TrainingParticipant
        fields = [
            "id", "employeeId", "employeeName", "attendance_status", "rating",
            "feedback", "evaluated_at",
        ]


class TrainingSerializer(BaseModelSerializer):
    trainerId = TenantPrimaryKeyRelatedField(
        source="trainer", model="hrms.Trainer", required=False, allow_null=True
    )
    trainerName = serializers.CharField(source="trainer.name", read_only=True)
    departmentId = TenantPrimaryKeyRelatedField(
        source="department", model="hrms.Department", required=False, allow_null=True
    )
    participants = TrainingParticipantSerializer(many=True, read_only=True)

    class Meta:
        model = Training
        fields = [
            "id", "title", "description", "type", "trainerId", "trainerName",
            "departmentId", "start_date", "end_date", "venue", "cost", "stage",
            "participants", "created_at", "updated_at",
        ]


# ---------------------------------------------------------------------------
# Assets, documents, policies, calendar, HR admin (api.md §11.8)
# ---------------------------------------------------------------------------
class AssetCategorySerializer(BaseModelSerializer):
    class Meta:
        model = AssetCategory
        fields = ["id", "name", "depreciation_pct", "default_warranty_months"]


class AssetAssignmentSerializer(BaseModelSerializer):
    employeeName = serializers.CharField(source="employee.name", read_only=True)

    class Meta:
        model = AssetAssignment
        fields = [
            "id", "employee", "employeeName", "assigned_at", "assigned_by",
            "returned_at", "returned_condition", "notes",
        ]


class AssetSerializer(BaseModelSerializer):
    assetCode = serializers.CharField(source="asset_code", read_only=True)
    category = serializers.CharField(source="category.name", read_only=True)
    categoryId = TenantPrimaryKeyRelatedField(
        source="category", model="hrms.AssetCategory", required=False, allow_null=True
    )
    assignedTo = serializers.CharField(source="assigned_employee.name", read_only=True)
    employeeId = TenantPrimaryKeyRelatedField(
        source="assigned_employee", model="hrms.Employee", required=False, allow_null=True
    )
    dept = serializers.CharField(
        source="assigned_employee.department.name", read_only=True
    )
    serialNumber = serializers.CharField(
        source="serial_number", required=False, allow_null=True, allow_blank=True
    )
    purchaseDate = serializers.DateField(
        source="purchase_date", required=False, allow_null=True
    )
    purchaseCost = MoneyField(source="purchase_cost", required=False, allow_null=True)
    warrantyExpiry = serializers.DateField(
        source="warranty_expiry", required=False, allow_null=True
    )
    history = AssetAssignmentSerializer(many=True, read_only=True)

    class Meta:
        model = Asset
        fields = [
            "id", "assetCode", "name", "category", "categoryId", "serialNumber",
            "assignedTo", "employeeId", "dept", "status", "condition",
            "purchaseDate", "purchaseCost", "warrantyExpiry", "location",
            "notes", "history", "created_at", "updated_at",
        ]
        read_only_fields = ["assetCode", "created_at", "updated_at"]


class AssetRequestSerializer(BaseModelSerializer):
    employeeId = TenantPrimaryKeyRelatedField(
        source="employee", model="hrms.Employee", required=False, allow_null=True
    )
    employeeName = serializers.CharField(source="employee.name", read_only=True)
    categoryId = TenantPrimaryKeyRelatedField(
        source="category", model="hrms.AssetCategory", required=False, allow_null=True
    )
    categoryName = serializers.CharField(source="category.name", read_only=True)
    fulfilledAssetId = TenantPrimaryKeyRelatedField(
        source="fulfilled_asset", model="hrms.Asset", required=False, allow_null=True
    )
    fulfilledAssetName = serializers.CharField(source="fulfilled_asset.name", read_only=True)

    class Meta:
        model = AssetRequest
        fields = [
            "id", "employeeId", "employeeName", "categoryId", "categoryName", "justification",
            "status", "approver", "fulfilled_asset", "fulfilledAssetId", "fulfilledAssetName", "created_at",
        ]
        read_only_fields = ["status", "approver", "fulfilled_asset", "created_at"]


class HrDocumentSerializer(BaseModelSerializer):
    employeeId = TenantPrimaryKeyRelatedField(
        source="employee", model="hrms.Employee", required=False, allow_null=True
    )
    employee = serializers.CharField(source="employee.name", read_only=True)
    employeeCode = serializers.CharField(source="employee.employee_code", read_only=True)
    fileId = TenantPrimaryKeyRelatedField(
        source="file", model="core.File", required=False, allow_null=True
    )
    fileSize = serializers.IntegerField(source="file.file_size", read_only=True)
    fileType = serializers.CharField(source="file.content_type", read_only=True)
    fileName = serializers.CharField(required=False, allow_blank=True)
    fileData = serializers.CharField(required=False, allow_blank=True, write_only=True)
    expiry = serializers.DateField(source="valid_until", required=False, allow_null=True)
    #: DERIVED from ``valid_until`` at read time (api.md §11.8).
    status = serializers.SerializerMethodField()
    uploadedBy = serializers.CharField(source="created_by.name", read_only=True)
    updatedOn = serializers.DateTimeField(source="updated_at", read_only=True)
    uploadDate = serializers.SerializerMethodField()

    class Meta:
        model = HrDocument
        fields = [
            "id", "title", "category", "employee", "employeeId", "employeeCode", "version",
            "expiry", "status", "fileId", "fileSize", "fileType", "fileName", "fileData", "uploadedBy",
            "updatedOn", "uploadDate", "tags", "description", "valid_from", "is_confidential",
            "created_at",
        ]

    def to_internal_value(self, data):
        data = data.copy() if hasattr(data, "copy") else dict(data)
        emp_id = data.get("employeeId")
        if emp_id in ("ALL", "All Staff", "all", "", None) or (
            isinstance(emp_id, str) and not (len(emp_id) == 36 and "-" in emp_id)
        ):
            data["employeeId"] = None

        if "fileName" in data and data["fileName"]:
            data["fileName"] = str(data["fileName"]).strip()

        v_raw = data.get("version")
        if isinstance(v_raw, str):
            v_clean = v_raw.lower().replace("v", "").split(".")[0].strip()
            data["version"] = int(v_clean) if v_clean.isdigit() else 1
        elif not v_raw:
            data["version"] = 1

        exp_raw = data.get("expiry")
        if exp_raw in ("—", "-", "Permanent", "permanent", "N/A", "n/a", "", None):
            data["expiry"] = None

        return super().to_internal_value(data)

    def create(self, validated_data):
        file_data = validated_data.pop("fileData", None)
        file_name = validated_data.pop("fileName", None)
        instance = super().create(validated_data)
        if file_data and not instance.file_id:
            self._attach_file(instance, file_data, file_name=file_name)
        return instance

    def update(self, instance, validated_data):
        file_data = validated_data.pop("fileData", None)
        file_name = validated_data.pop("fileName", None)
        instance = super().update(instance, validated_data)
        if file_data:
            self._attach_file(instance, file_data, file_name=file_name)
        return instance

    def _attach_file(self, instance, file_data, file_name=None):
        import base64
        import logging
        from django.conf import settings
        from apps.core.files import build_storage_key, write_bytes
        from apps.core.models import File
        from rest_framework.exceptions import ValidationError

        try:
            content_type = "application/pdf"
            if "," in file_data:
                header, base64_str = file_data.split(",", 1)
                if ";base64" in header and "data:" in header:
                    content_type = header.replace("data:", "").replace(";base64", "")
            else:
                base64_str = file_data
            raw_bytes = base64.b64decode(base64_str)

            # Validate PDF format
            if not raw_bytes.startswith(b"%PDF-") and "pdf" not in content_type.lower():
                raise ValidationError({"fileData": "Only PDF format documents are allowed."})

            # Validate file size (15 MB maximum)
            max_size_bytes = getattr(settings, "FILE_MAX_BYTES", 25 * 1024 * 1024)
            if len(raw_bytes) > max_size_bytes:
                limit_mb = max_size_bytes // (1024 * 1024)
                raise ValidationError({"fileData": f"File size exceeds the {limit_mb} MB limit."})

            client_id = str(instance.client_id)
            title = instance.title or "document"

            # Retain original filename if provided
            if file_name and str(file_name).strip():
                clean_name = str(file_name).strip()
                filename = clean_name if clean_name.lower().endswith(".pdf") else f"{clean_name}.pdf"
            else:
                clean_title = str(title).strip()
                filename = clean_title if clean_title.lower().endswith(".pdf") else f"{clean_title}.pdf"

            storage_key = build_storage_key(client_id, "hrms_documents", filename)
            write_bytes(storage_key, raw_bytes)
            f_obj = File.objects.create(
                client=instance.client,
                file_name=filename,
                file_size=len(raw_bytes),
                content_type="application/pdf",
                storage_key=storage_key,
                scope="hrms_documents",
                status="Committed",
                created_by=getattr(instance, "created_by", None),
            )
            instance.file = f_obj
            instance.save(update_fields=["file"])
        except ValidationError:
            raise
        except Exception as err:
            logging.getLogger(__name__).warning("Failed to attach fileData: %s", err)

    def to_representation(self, instance):
        from apps.core.files import public_url

        ret = super().to_representation(instance)
        if not instance.employee:
            ret["employee"] = "All Staff"
            ret["employeeId"] = "ALL"
            ret["employeeCode"] = None
        else:
            ret["employeeCode"] = instance.employee.employee_code
        if instance.file:
            try:
                ret["fileUrl"] = public_url(instance.file, self.context.get("request"))
            except Exception:
                pass
            ret["fileName"] = instance.file.file_name
            ret["fileSize"] = instance.file.file_size
            ret["fileType"] = "PDF"
        else:
            ret["fileName"] = f"{instance.title}.pdf"
            ret["fileSize"] = 0
            ret["fileType"] = "PDF"
        ret["uploadDate"] = instance.created_at.date().isoformat() if instance.created_at else None
        return ret

    def get_uploadDate(self, instance):
        return instance.created_at.date().isoformat() if instance.created_at else None

    def get_status(self, document):
        return services.document_status(document)


class PolicyVersionSerializer(BaseModelSerializer):
    changedByName = serializers.CharField(source="changed_by.name", read_only=True)

    class Meta:
        model = PolicyVersion
        fields = ["id", "version", "change_note", "changedByName", "created_at"]


class PolicySerializer(BaseModelSerializer):
    category = serializers.CharField(source="category.name", read_only=True)
    categoryId = TenantPrimaryKeyRelatedField(
        source="category", model="hrms.PolicyCategory", required=False, allow_null=True
    )
    ownerDept = serializers.CharField(source="owner_department.name", read_only=True)
    ownerDepartmentId = TenantPrimaryKeyRelatedField(
        source="owner_department", model="hrms.Department",
        required=False, allow_null=True,
    )
    applicableTo = serializers.CharField(
        source="applicable_to", required=False, allow_null=True, allow_blank=True
    )
    effectiveDate = serializers.DateField(
        source="effective_date", required=False, allow_null=True
    )
    reviewDate = serializers.DateField(
        source="review_date", required=False, allow_null=True
    )
    approvalRequired = serializers.BooleanField(source="approval_required", required=False)
    ackRequired = serializers.BooleanField(source="ack_required", required=False)
    versionHistory = PolicyVersionSerializer(
        source="version_history", many=True, read_only=True
    )

    class Meta:
        model = Policy
        fields = [
            "id", "name", "category", "categoryId", "ownerDept",
            "ownerDepartmentId", "applicableTo", "version", "effectiveDate",
            "reviewDate", "approvalRequired", "ackRequired", "ack_window_days",
            "status", "summary", "body", "file", "versionHistory",
            "approved_by", "approved_at", "created_at", "updated_at",
        ]
        read_only_fields = [
            "version", "status", "approved_by", "approved_at", "created_at", "updated_at",
        ]


class PolicyAcknowledgementSerializer(BaseModelSerializer):
    employeeName = serializers.CharField(source="employee.name", read_only=True)
    employeeCode = serializers.CharField(source="employee.employee_code", read_only=True)
    status = serializers.SerializerMethodField()

    class Meta:
        model = PolicyAcknowledgement
        fields = [
            "id", "employee", "employeeName", "employeeCode", "policy_version",
            "acknowledged_at", "status",
        ]

    def get_status(self, acknowledgement):
        policy = self.context.get("policy") or acknowledgement.policy
        return services.acknowledgement_status(acknowledgement, policy)


class PolicyCategorySerializer(BaseModelSerializer):
    class Meta:
        model = PolicyCategory
        fields = ["id", "name", "description"]


class CalendarEventSerializer(BaseModelSerializer):
    startDate = serializers.DateTimeField(source="starts_at")
    endDate = serializers.DateTimeField(source="ends_at", required=False, allow_null=True)
    date = serializers.SerializerMethodField()
    dept = serializers.CharField(source="department.name", read_only=True)
    departmentId = TenantPrimaryKeyRelatedField(
        source="department", model="hrms.Department", required=False, allow_null=True
    )
    virtualLink = serializers.CharField(
        source="virtual_link", required=False, allow_null=True, allow_blank=True
    )
    #: ``time`` is a display string the server derives (api.md §11.8).
    time = serializers.SerializerMethodField()

    class Meta:
        model = CalendarEvent
        fields = [
            "id", "title", "date", "startDate", "endDate", "type", "time",
            "location", "dept", "departmentId", "organizer", "description",
            "virtualLink", "all_day", "source_type", "source_id", "created_at",
        ]
        read_only_fields = ["source_type", "source_id", "created_at"]

    def get_date(self, event):
        return event.starts_at.date() if event.starts_at else None

    def get_time(self, event):
        return services.calendar_time_label(event)


class HolidaySerializer(BaseModelSerializer):
    locationId = TenantPrimaryKeyRelatedField(
        source="location", model="hrms.Location", required=False, allow_null=True
    )
    locationName = serializers.CharField(source="location.name", read_only=True)
    departmentId = TenantPrimaryKeyRelatedField(
        source="department", model="hrms.Department", required=False, allow_null=True
    )
    departmentName = serializers.CharField(source="department.name", read_only=True)
    holidayType = serializers.CharField(source="holiday_type", required=False, default="National Gazetted")
    isOptional = serializers.BooleanField(source="is_optional", required=False, default=False)
    year = serializers.SerializerMethodField(read_only=True)
    day = serializers.SerializerMethodField(read_only=True)

    class Meta:
        model = Holiday
        fields = [
            "id", "date", "name", "locationId", "locationName",
            "departmentId", "departmentName", "holidayType",
            "isOptional", "is_optional", "status", "description",
            "year", "day", "created_at"
        ]

    def get_year(self, obj):
        return obj.date.year if obj.date else None

    def get_day(self, obj):
        return obj.date.strftime("%A") if obj.date else None

    def validate(self, attrs):
        request = self.context.get("request")
        client_id = getattr(request, "client_id", None)
        date = attrs.get("date") or (self.instance.date if self.instance else None)
        location = attrs.get("location") if "location" in attrs else (self.instance.location if self.instance else None)
        department = attrs.get("department") if "department" in attrs else (self.instance.department if self.instance else None)

        if client_id and date:
            qs = Holiday.objects.filter(client_id=client_id, date=date, deleted_at__isnull=True)
            if location:
                qs = qs.filter(location=location)
            else:
                qs = qs.filter(location__isnull=True)
            if department:
                qs = qs.filter(department=department)
            else:
                qs = qs.filter(department__isnull=True)
            if self.instance:
                qs = qs.exclude(pk=self.instance.pk)
            if qs.exists():
                raise serializers.ValidationError({"date": f"A holiday on {date} already exists for this scope."})
        return attrs


class WorkScheduleSerializer(BaseModelSerializer):
    departmentId = TenantPrimaryKeyRelatedField(
        source="department", model="hrms.Department", required=False, allow_null=True
    )
    departmentName = serializers.CharField(source="department.name", read_only=True)
    teamId = TenantPrimaryKeyRelatedField(
        source="team", model="hrms.Team", required=False, allow_null=True
    )
    teamName = serializers.CharField(source="team.name", read_only=True)
    locationId = TenantPrimaryKeyRelatedField(
        source="location", model="hrms.Location", required=False, allow_null=True
    )
    locationName = serializers.CharField(source="location.name", read_only=True)
    shiftType = serializers.CharField(source="shift_type", required=False, default="Regular")
    shiftStart = serializers.TimeField(source="shift_start", required=False)
    shiftEnd = serializers.TimeField(source="shift_end", required=False)
    breakDuration = serializers.IntegerField(source="break_duration_minutes", required=False, default=60)
    workingHours = serializers.DecimalField(source="working_hours", max_digits=4, decimal_places=2, required=False, default=8.0)
    workingDays = serializers.ListField(source="working_days", child=serializers.IntegerField(min_value=0, max_value=6), required=False)
    weeklyOffs = serializers.ListField(source="weekly_offs", child=serializers.IntegerField(min_value=0, max_value=6), required=False)
    isDefault = serializers.BooleanField(source="is_default", required=False, default=False)
    effectiveFrom = serializers.DateField(source="effective_from", required=False, allow_null=True)
    effectiveTo = serializers.DateField(source="effective_to", required=False, allow_null=True)
    assignedCount = serializers.SerializerMethodField(read_only=True)

    class Meta:
        model = WorkSchedule
        fields = [
            "id", "name", "code", "shiftType", "shift_type",
            "shiftStart", "shift_start", "shiftEnd", "shift_end",
            "breakDuration", "break_duration_minutes",
            "workingHours", "working_hours",
            "workingDays", "working_days",
            "weeklyOffs", "weekly_offs",
            "departmentId", "departmentName",
            "teamId", "teamName",
            "locationId", "locationName",
            "isDefault", "is_default",
            "status", "effectiveFrom", "effective_from",
            "effectiveTo", "effective_to",
            "description", "assignedCount",
            "created_at", "updated_at",
        ]

    def get_assignedCount(self, obj):
        emp_ids = set(obj.employees.filter(status__in=["Active", "Probation"], deleted_at__isnull=True).values_list("id", flat=True))
        if obj.team_id:
            emp_ids.update(obj.team.members.filter(status__in=["Active", "Probation"], deleted_at__isnull=True).values_list("id", flat=True))
            if obj.team.lead_id:
                emp_ids.add(obj.team.lead_id)
        if obj.department_id:
            emp_ids.update(Employee.objects.filter(department_id=obj.department_id, status__in=["Active", "Probation"], deleted_at__isnull=True).values_list("id", flat=True))
        return len(emp_ids)

    def validate(self, attrs):
        shift_start = attrs.get("shift_start") or (self.instance.shift_start if self.instance else None)
        shift_end = attrs.get("shift_end") or (self.instance.shift_end if self.instance else None)
        shift_type = attrs.get("shift_type") or (self.instance.shift_type if self.instance else "Regular")
        eff_from = attrs.get("effective_from") or (self.instance.effective_from if self.instance else None)
        eff_to = attrs.get("effective_to") or (self.instance.effective_to if self.instance else None)

        if eff_from and eff_to and eff_to < eff_from:
            raise serializers.ValidationError({"effectiveTo": "Effective To cannot be earlier than Effective From."})

        if shift_start and shift_end and shift_start == shift_end and shift_type != "Flexible":
            raise serializers.ValidationError({"shiftEnd": "Shift start time and end time cannot be identical."})

        w_days = set(attrs.get("working_days") or (self.instance.working_days if self.instance else []))
        w_offs = set(attrs.get("weekly_offs") or (self.instance.weekly_offs if self.instance else []))
        overlap = w_days.intersection(w_offs)
        if overlap:
            raise serializers.ValidationError({"weeklyOffs": f"Days {list(overlap)} cannot be both a working day and a weekly off."})

        return attrs

    def create(self, validated_data):
        if validated_data.get("is_default"):
            request = self.context.get("request")
            client_id = getattr(request, "client_id", None)
            if client_id:
                WorkSchedule.objects.filter(client_id=client_id, is_default=True).update(is_default=False)
        return super().create(validated_data)

    def update(self, instance, validated_data):
        if validated_data.get("is_default"):
            request = self.context.get("request")
            client_id = getattr(request, "client_id", None)
            if client_id:
                WorkSchedule.objects.filter(client_id=client_id, is_default=True).exclude(pk=instance.pk).update(is_default=False)
        return super().update(instance, validated_data)


class WorkingDaySerializer(BaseModelSerializer):
    class Meta:
        model = WorkingDay
        fields = ["id", "weekday", "is_working", "shift_start", "shift_end", "location"]


class TeamSerializer(BaseModelSerializer):
    code = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    dept = serializers.CharField(source="department.name", read_only=True)
    department = serializers.CharField(source="department.name", read_only=True)
    departmentId = TenantPrimaryKeyRelatedField(
        source="department", model="hrms.Department", required=False, allow_null=True
    )
    lead = serializers.CharField(source="lead_name", required=False, allow_blank=True)
    leadId = TenantPrimaryKeyRelatedField(
        source="lead", model="hrms.Employee", required=False, allow_null=True
    )
    leadName = serializers.CharField(source="lead_name", read_only=True)
    approver = serializers.CharField(source="approver_name", required=False, allow_blank=True)
    approverId = TenantPrimaryKeyRelatedField(
        source="approver", model="hrms.Employee", required=False, allow_null=True
    )
    approverName = serializers.CharField(source="approver_name", read_only=True)
    headcount = serializers.IntegerField(required=False, allow_null=True)
    memberIds = serializers.ListField(child=serializers.CharField(), required=False, write_only=True)
    members = serializers.SerializerMethodField(read_only=True)
    memberCount = serializers.SerializerMethodField(read_only=True)
    membersDetail = serializers.SerializerMethodField(read_only=True)

    class Meta:
        model = Team
        fields = [
            "id", "code", "name", "department", "dept", "departmentId", "lead", "leadId", "leadName",
            "approver", "approverId", "approverName", "headcount", "members", "memberCount",
            "memberIds", "membersDetail", "description", "status", "created_at",
        ]

    def get_memberCount(self, team):
        active_members = team.members.filter(deleted_at__isnull=True).exclude(status__in=["Terminated", "Resigned"])
        count = active_members.count()
        if count > 0:
            return count
        return team.headcount or 0

    def get_members(self, team):
        return self.get_memberCount(team)

    def get_membersDetail(self, team):
        active_members = team.members.filter(deleted_at__isnull=True).exclude(status__in=["Terminated", "Resigned"])
        return [
            {
                "id": str(m.id),
                "name": m.name,
                "employeeCode": m.employee_code,
                "department": m.department.name if m.department else "",
                "designation": m.designation.name if m.designation else "",
                "status": m.status,
            }
            for m in active_members
        ]

    def to_internal_value(self, data):
        data = data.copy() if hasattr(data, "copy") else dict(data)
        request = self.context.get("request")
        client_id = getattr(request, "client_id", None) or getattr(getattr(request, "user", None), "client_id", None)

        # Team Code validation
        code_val = data.get("code") or data.get("teamCode")
        if code_val and client_id:
            from .models import Team
            code_clean = str(code_val).strip()
            qs = Team.objects.filter(client_id=client_id, code__iexact=code_clean)
            if self.instance:
                qs = qs.exclude(pk=self.instance.pk)
            if qs.exists():
                raise ValidationFailed(
                    f"Team ID / Code '{code_clean}' is already assigned to another team.",
                    field_errors={"code": ["Team ID must be unique."]}
                )
            data["code"] = code_clean

        # Department resolution
        dept_val = data.get("dept") or data.get("department")
        if dept_val and not data.get("departmentId") and client_id:
            from .models import Department
            dept_obj = None
            if isinstance(dept_val, str) and not ("-" in dept_val and len(dept_val) == 36):
                dept_obj = Department.objects.filter(client_id=client_id, name__iexact=dept_val.strip(), deleted_at__isnull=True).first()
                if not dept_obj:
                    dept_obj = Department.objects.create(client_id=client_id, name=dept_val.strip())
            elif isinstance(dept_val, str) and "-" in dept_val and len(dept_val) == 36:
                dept_obj = Department.objects.filter(client_id=client_id, id=dept_val, deleted_at__isnull=True).first()
            if dept_obj:
                data["departmentId"] = str(dept_obj.id)

        # Lead resolution & eligibility
        lead_val = data.get("lead") or data.get("leadId")
        if lead_val:
            from .models import Employee
            lead_obj = None
            if isinstance(lead_val, str) and "-" in lead_val and len(lead_val) == 36:
                lead_obj = Employee.objects.filter(id=lead_val).first()
            elif isinstance(lead_val, str) and client_id:
                lead_obj = Employee.objects.filter(client_id=client_id, name__iexact=lead_val.strip(), deleted_at__isnull=True).first()
            if lead_obj:
                if lead_obj.status in ("Terminated", "Resigned"):
                    raise ValidationFailed(
                        f"Cannot assign {lead_obj.name} as Team Lead: employee is {lead_obj.status.lower()}.",
                        field_errors={"lead": ["Team leader must be an active employee."]}
                    )
                data["leadId"] = str(lead_obj.id)
                data["lead"] = lead_obj.name
            else:
                data["lead"] = str(lead_val).strip()

        # Approver resolution & eligibility
        appr_val = data.get("approver") or data.get("approverId")
        if appr_val:
            from .models import Employee
            appr_obj = None
            if isinstance(appr_val, str) and "-" in appr_val and len(appr_val) == 36:
                appr_obj = Employee.objects.filter(id=appr_val).first()
            elif isinstance(appr_val, str) and client_id:
                appr_obj = Employee.objects.filter(client_id=client_id, name__iexact=appr_val.strip(), deleted_at__isnull=True).first()
            if appr_obj:
                if appr_obj.status in ("Terminated", "Resigned"):
                    raise ValidationFailed(
                        f"Cannot assign {appr_obj.name} as Default Approver: employee is {appr_obj.status.lower()}.",
                        field_errors={"approver": ["Approver must be an active employee."]}
                    )
                data["approverId"] = str(appr_obj.id)
                data["approver"] = appr_obj.name
            else:
                data["approver"] = str(appr_val).strip()

        # Members / Headcount
        raw_members = data.get("memberIds") if "memberIds" in data else data.get("members")
        if isinstance(raw_members, list):
            valid_ids = []
            for m in raw_members:
                m_str = str(m).strip()
                if not m_str:
                    continue
                valid_ids.append(m_str)
            data["memberIds"] = valid_ids
            data["headcount"] = len(valid_ids)
        elif isinstance(raw_members, (int, float)):
            data["headcount"] = int(raw_members)
            data["memberIds"] = []
        elif isinstance(raw_members, str) and raw_members.isdigit():
            data["headcount"] = int(raw_members)
            data["memberIds"] = []

        return super().to_internal_value(data)

    def create(self, validated_data):
        raw_member_ids = validated_data.pop("memberIds", None)
        request = self.context.get("request")
        client = getattr(request, "client", None) or getattr(getattr(request, "user", None), "client", None)
        if not validated_data.get("code") and client:
            from apps.core.numbering import allocate_number
            try:
                validated_data["code"] = allocate_number(client, "TM")
            except Exception:
                pass

        instance = super().create(validated_data)
        if raw_member_ids is not None:
            from .models import Employee
            client_id = instance.client_id
            resolved_employees = []
            for item in raw_member_ids:
                if "-" in item and len(item) == 36:
                    emp = Employee.objects.filter(client_id=client_id, id=item, deleted_at__isnull=True).first()
                else:
                    emp = Employee.objects.filter(client_id=client_id, employee_code__iexact=item, deleted_at__isnull=True).first()
                    if not emp:
                        emp = Employee.objects.filter(client_id=client_id, name__iexact=item, deleted_at__isnull=True).first()
                if emp and emp.status not in ("Terminated", "Resigned"):
                    resolved_employees.append(emp)
            instance.members.set(resolved_employees)
            instance.headcount = len(resolved_employees)
            instance.save(update_fields=["headcount"])
        return instance

    def update(self, instance, validated_data):
        raw_member_ids = validated_data.pop("memberIds", None)
        instance = super().update(instance, validated_data)
        if raw_member_ids is not None:
            from .models import Employee
            client_id = instance.client_id
            resolved_employees = []
            for item in raw_member_ids:
                if "-" in item and len(item) == 36:
                    emp = Employee.objects.filter(client_id=client_id, id=item, deleted_at__isnull=True).first()
                else:
                    emp = Employee.objects.filter(client_id=client_id, employee_code__iexact=item, deleted_at__isnull=True).first()
                    if not emp:
                        emp = Employee.objects.filter(client_id=client_id, name__iexact=item, deleted_at__isnull=True).first()
                if emp and emp.status not in ("Terminated", "Resigned"):
                    resolved_employees.append(emp)
            instance.members.set(resolved_employees)
            instance.headcount = len(resolved_employees)
            instance.save(update_fields=["headcount"])
        return instance

    def to_representation(self, instance):
        ret = super().to_representation(instance)
        lead_display = instance.lead_name or (instance.lead.name if instance.lead else "")
        appr_display = instance.approver_name or (instance.approver.name if instance.approver else "")
        active_members = instance.members.filter(deleted_at__isnull=True).exclude(status__in=["Terminated", "Resigned"])
        actual_count = active_members.count()
        count = actual_count if actual_count > 0 else (instance.headcount or 0)

        team_code = instance.code or getattr(instance, "legacy_id", None) or f"T-{str(instance.id)[:4].upper()}"
        ret["code"] = team_code
        ret["teamCode"] = team_code
        ret["lead"] = lead_display
        ret["leadName"] = lead_display
        ret["approver"] = appr_display
        ret["approverName"] = appr_display
        ret["members"] = count
        ret["memberCount"] = count
        ret["headcount"] = count
        ret["dept"] = instance.department.name if instance.department else ""
        ret["departmentId"] = str(instance.department.id) if instance.department else None
        ret["memberIds"] = [str(m.id) for m in active_members]
        ret["membersDetail"] = [
            {
                "id": str(m.id),
                "name": m.name,
                "employeeCode": m.employee_code,
                "department": m.department.name if m.department else "",
                "designation": m.designation.name if m.designation else "",
                "status": m.status,
            }
            for m in active_members
        ]
        return ret


class HrmsSettingsSerializer(serializers.Serializer):
    companyName = serializers.CharField(required=False, allow_blank=True, max_length=255)
    workWeek = serializers.CharField(required=False, allow_blank=True, max_length=100)
    timezone = serializers.CharField(required=False, allow_blank=True, max_length=100)
    fiscalYearStart = serializers.CharField(required=False, allow_blank=True, max_length=50)
    probationPeriod = serializers.CharField(required=False, allow_blank=True, max_length=100)
    noticePeriod = serializers.CharField(required=False, allow_blank=True, max_length=100)
    allowEmployeeDelegation = serializers.BooleanField(required=False, default=True)
    requireTwoFactorForAdmin = serializers.BooleanField(required=False, default=True)
    sandwichRuleEnabled = serializers.BooleanField(required=False, default=True)
    maxCarryForwardDays = serializers.IntegerField(required=False, min_value=0, max_value=365, default=15)


class ApprovalChainSerializer(BaseModelSerializer):
    module = serializers.CharField(source="applies_to", required=False, allow_blank=True)
    tier1 = serializers.CharField(required=False, allow_blank=True)
    tier2 = serializers.CharField(required=False, allow_blank=True)
    tier3 = serializers.CharField(required=False, allow_blank=True)
    autoEscalateDays = serializers.IntegerField(required=False, allow_null=True)

    class Meta:
        model = ApprovalChain
        fields = [
            "id", "name", "applies_to", "module", "tier1", "tier2", "tier3",
            "autoEscalateDays", "steps", "status", "created_at"
        ]
        extra_kwargs = {
            "name": {"required": False},
            "applies_to": {"required": False},
        }

    def to_internal_value(self, data):
        data = data.copy() if hasattr(data, "copy") else dict(data)
        from .workflow_service import normalize_module_name

        raw_module = data.get("module") or data.get("applies_to") or "General"
        canonical = normalize_module_name(raw_module)
        data["applies_to"] = canonical
        if not data.get("name"):
            data["name"] = f"{canonical} Approval Chain"

        if data.get("steps") and isinstance(data["steps"], list):
            steps = []
            for idx, s in enumerate(data["steps"]):
                if isinstance(s, dict) and s.get("role"):
                    tier = s.get("tier") or s.get("level") or (idx + 1)
                    auto_esc = s.get("autoEscalateDays") or data.get("autoEscalateDays") or 3
                    steps.append({
                        "tier": int(tier),
                        "role": str(s["role"]).strip(),
                        "autoEscalateDays": int(auto_esc),
                    })
            if steps:
                data["steps"] = sorted(steps, key=lambda x: x["tier"])

        elif data.get("tier1") or data.get("tier2"):
            steps = []
            if data.get("tier1"):
                steps.append({
                    "tier": 1,
                    "role": data["tier1"],
                    "autoEscalateDays": int(data.get("autoEscalateDays") or 3),
                })
            if data.get("tier2"):
                steps.append({"tier": 2, "role": data["tier2"]})
            if data.get("tier3") and data["tier3"] not in ("—", "-"):
                steps.append({"tier": 3, "role": data["tier3"]})
            data["steps"] = steps

        return super().to_internal_value(data)

    def validate(self, attrs):
        steps = attrs.get("steps") or (self.instance.steps if self.instance else [])
        if not steps:
            raise ValidationFailed({"steps": ["At least one approval step or Tier 1 approver is required."]})
        return attrs

    def create(self, validated_data):
        for k in ["tier1", "tier2", "tier3", "autoEscalateDays"]:
            validated_data.pop(k, None)
        return super().create(validated_data)

    def update(self, instance, validated_data):
        for k in ["tier1", "tier2", "tier3", "autoEscalateDays"]:
            validated_data.pop(k, None)
        return super().update(instance, validated_data)

    def to_representation(self, instance):
        ret = super().to_representation(instance)
        steps = instance.steps if isinstance(instance.steps, list) else []
        t1 = next((s.get("role") for s in steps if s.get("tier") == 1 or s.get("level") == 1), "")
        if not t1 and steps:
            t1 = steps[0].get("role", "")
        t2 = next((s.get("role") for s in steps if s.get("tier") == 2 or s.get("level") == 2), "")
        if not t2 and len(steps) > 1:
            t2 = steps[1].get("role", "")
        t3 = next((s.get("role") for s in steps if s.get("tier") == 3 or s.get("level") == 3), "—")
        if (not t3 or t3 in ("—", "-")) and len(steps) > 2:
            t3 = steps[2].get("role", "—")
        esc = next((s.get("autoEscalateDays") for s in steps if s.get("autoEscalateDays")), 3)

        ret["module"] = instance.applies_to or instance.name
        ret["tier1"] = t1 or "Direct Line Manager"
        ret["tier2"] = t2 or "Department Head"
        ret["tier3"] = t3 or "—"
        ret["autoEscalateDays"] = esc
        return ret


class TerminationSerializer(BaseModelSerializer):
    employeeId = TenantPrimaryKeyRelatedField(source="employee", model="hrms.Employee", required=False, allow_null=True)
    employeeName = serializers.CharField(source="employee.name", read_only=True)
    employeeCode = serializers.CharField(source="employee.employee_code", read_only=True)
    employee = serializers.CharField(source="employee.name", read_only=True)
    dept = serializers.CharField(source="employee.department.name", read_only=True)
    role = serializers.CharField(source="employee.designation.name", read_only=True)
    terminationType = serializers.CharField(source="termination_type", required=False, allow_blank=True)
    noticeDate = serializers.DateField(source="notice_date", required=False, allow_null=True)
    exitDate = serializers.DateField(source="last_working_day", required=False, allow_null=True)
    lastWorkingDay = serializers.DateField(
        source="last_working_day", required=False, allow_null=True
    )
    severance = serializers.CharField(required=False, allow_blank=True)
    letterContent = serializers.JSONField(source="letter_content", required=False, allow_null=True)
    currentStep = serializers.IntegerField(source="current_step", read_only=True)
    approvalChainId = serializers.CharField(source="approval_chain_id", read_only=True)
    approvalHistory = serializers.JSONField(source="approval_history", read_only=True)
    approverName = serializers.CharField(source="approver.name", read_only=True, default="")
    decidedAt = serializers.DateTimeField(source="decided_at", read_only=True)
    remark = serializers.CharField(required=False, allow_blank=True, allow_null=True)

    checklist = serializers.SerializerMethodField()

    class Meta:
        model = Termination
        fields = [
            "id", "employeeId", "employeeName", "employeeCode", "employee", "dept", "role",
            "terminationType", "noticeDate", "exitDate", "lastWorkingDay", "severance",
            "reason", "letter_file", "letterContent", "settlement_amount", "status",
            "currentStep", "approvalChainId", "approvalHistory", "approverName", "decidedAt", "remark",
            "checklist", "created_at",
        ]

    def get_checklist(self, obj):
        items = obj.checklist.all()
        return [
            {
                "id": str(i.id),
                "title": i.title,
                "department": i.department or "General",
                "notes": i.notes or "",
                "isCompleted": i.is_completed,
                "completedAt": i.completed_at.isoformat() if i.completed_at else None,
                "ownerName": i.owner.name if i.owner else "",
            }
            for i in items
        ]

    def to_internal_value(self, data):
        data = data.copy() if hasattr(data, "copy") else dict(data)
        request = self.context.get("request")
        client_id = getattr(request, "client_id", None) or getattr(getattr(request, "user", None), "client_id", None)
        if not data.get("employeeId") and data.get("employee") and client_id:
            from .models import Employee
            emp_val = str(data["employee"]).strip()
            emp = Employee.objects.filter(client_id=client_id, name__iexact=emp_val, deleted_at__isnull=True).first()
            if not emp:
                emp = Employee.objects.filter(client_id=client_id, employee_code__iexact=emp_val, deleted_at__isnull=True).first()
            if emp:
                data["employeeId"] = str(emp.id)

        if "exitDate" in data and "lastWorkingDay" not in data:
            data["lastWorkingDay"] = data["exitDate"]
        if "terminationType" in data and "termination_type" not in data:
            data["termination_type"] = data["terminationType"]
        if "noticeDate" in data and "notice_date" not in data:
            data["notice_date"] = data["noticeDate"]
        if "letterContent" in data and "letter_content" not in data:
            data["letter_content"] = data["letterContent"]
        return super().to_internal_value(data)

    def to_representation(self, instance):
        ret = super().to_representation(instance)
        ret["employee"] = instance.employee.name if instance.employee else ""
        ret["employeeCode"] = instance.employee.employee_code if instance.employee else ""
        ret["dept"] = instance.employee.department.name if instance.employee and instance.employee.department else ""
        ret["role"] = instance.employee.designation.name if instance.employee and instance.employee.designation else ""
        ret["terminationType"] = instance.termination_type or "Performance / Restructuring"
        ret["exitDate"] = instance.last_working_day.isoformat() if instance.last_working_day else ""
        ret["noticeDate"] = instance.notice_date.isoformat() if instance.notice_date else ""
        ret["severance"] = instance.severance or "1 Month Gross Settlement"
        ret["status"] = instance.status or "In Exit Clearance"
        return ret


class ResignationChecklistItemSerializer(BaseModelSerializer):
    ownerName = serializers.CharField(source="owner.name", read_only=True, default="")
    isCompleted = serializers.BooleanField(source="is_completed", required=False)
    completedAt = serializers.DateTimeField(source="completed_at", read_only=True)

    class Meta:
        model = ResignationChecklistItem
        fields = ["id", "title", "department", "notes", "isCompleted", "completedAt", "owner", "ownerName"]


class ResignationSerializer(BaseModelSerializer):
    employeeId = TenantPrimaryKeyRelatedField(source="employee", model="hrms.Employee", required=False, allow_null=True)
    employeeName = serializers.CharField(source="employee.name", read_only=True)
    employeeCode = serializers.CharField(source="employee.employee_code", read_only=True)
    employee = serializers.CharField(source="employee.name", read_only=True)
    dept = serializers.CharField(source="employee.department.name", read_only=True)
    role = serializers.CharField(source="employee.designation.name", read_only=True)
    submittedOn = serializers.DateField(source="submitted_on", required=False, allow_null=True)
    submittedDate = serializers.DateField(source="submitted_on", required=False, allow_null=True)
    noticePeriodDays = serializers.IntegerField(source="notice_period_days", required=False)
    noticePeriod = serializers.CharField(source="notice_period", required=False, allow_blank=True)
    lastWorkingDay = serializers.DateField(
        source="last_working_day", required=False, allow_null=True
    )
    handoverTo = serializers.CharField(source="handover_name", required=False, allow_blank=True)
    handoverToId = TenantPrimaryKeyRelatedField(
        source="handover_to", model="hrms.Employee", required=False, allow_null=True
    )
    checklist = ResignationChecklistItemSerializer(many=True, read_only=True)
    currentStep = serializers.IntegerField(source="current_step", read_only=True)
    approvalChainId = serializers.CharField(source="approval_chain_id", read_only=True)
    approvalHistory = serializers.JSONField(source="approval_history", read_only=True)
    approverName = serializers.CharField(source="approver.name", read_only=True, default="")
    decidedAt = serializers.DateTimeField(source="decided_at", read_only=True)
    remark = serializers.CharField(required=False, allow_blank=True, allow_null=True)

    class Meta:
        model = Resignation
        fields = [
            "id", "employeeId", "employeeName", "employeeCode", "employee", "dept", "role",
            "submittedOn", "submittedDate", "noticePeriodDays", "noticePeriod",
            "lastWorkingDay", "handoverTo", "handoverToId", "exit_interview_at",
            "reason", "status", "checklist", "currentStep", "approvalChainId", "approvalHistory",
            "approverName", "decidedAt", "remark", "created_at",
        ]

    def to_internal_value(self, data):
        data = data.copy() if hasattr(data, "copy") else dict(data)
        request = self.context.get("request")
        client_id = getattr(request, "client_id", None) or getattr(getattr(request, "user", None), "client_id", None)
        if not data.get("employeeId") and data.get("employee") and client_id:
            from .models import Employee
            emp_val = str(data["employee"]).strip()
            emp = Employee.objects.filter(client_id=client_id, name__iexact=emp_val, deleted_at__isnull=True).first()
            if not emp:
                emp = Employee.objects.filter(client_id=client_id, employee_code__iexact=emp_val, deleted_at__isnull=True).first()
            if emp:
                data["employeeId"] = str(emp.id)

        if "submittedDate" in data and "submittedOn" not in data:
            data["submittedOn"] = data["submittedDate"]
        if not data.get("submittedOn") and not data.get("submitted_on"):
            today_str = timezone.now().date().isoformat()
            data["submittedOn"] = today_str
            data["submitted_on"] = today_str

        if data.get("noticePeriod"):
            import re
            m = re.search(r"\d+", str(data["noticePeriod"]))
            if m:
                data["notice_period_days"] = int(m.group(0))
            data["notice_period"] = str(data["noticePeriod"])

        # Handover resolution
        handover_val = data.get("handoverTo") or data.get("handoverToId")
        if handover_val:
            from .models import Employee
            if isinstance(handover_val, str) and "-" in handover_val and len(handover_val) == 36:
                data["handoverToId"] = handover_val
                hand_obj = Employee.objects.filter(id=handover_val).first()
                if hand_obj:
                    data["handoverTo"] = hand_obj.name
            elif isinstance(handover_val, str) and client_id:
                hand_obj = Employee.objects.filter(client_id=client_id, name__iexact=handover_val.strip(), deleted_at__isnull=True).first()
                if hand_obj:
                    data["handoverToId"] = str(hand_obj.id)
                    data["handoverTo"] = hand_obj.name
                else:
                    data["handoverTo"] = handover_val.strip()

        return super().to_internal_value(data)

    def to_representation(self, instance):
        ret = super().to_representation(instance)
        ret["employee"] = instance.employee.name if instance.employee else ""
        ret["employeeCode"] = instance.employee.employee_code if instance.employee else ""
        ret["dept"] = instance.employee.department.name if instance.employee and instance.employee.department else ""
        ret["role"] = instance.employee.designation.name if instance.employee and instance.employee.designation else ""
        ret["submittedDate"] = instance.submitted_on.isoformat() if instance.submitted_on else ""
        ret["lastWorkingDay"] = instance.last_working_day.isoformat() if instance.last_working_day else ""
        ret["noticePeriod"] = instance.notice_period or f"{instance.notice_period_days} Days"
        ret["handoverTo"] = instance.handover_name or (instance.handover_to.name if instance.handover_to else "Pending Assignment")
        ret["status"] = instance.status or "Pending Manager Review"
        return ret


class ComplaintSerializer(BaseModelSerializer):
    """``is_anonymous`` is honoured by nulling the raiser in every serialiser --
    including exports (db.md §11.8)."""

    raisedBy = serializers.SerializerMethodField()
    raisedByEmployeeId = TenantPrimaryKeyRelatedField(
        source="raised_by_employee", model="hrms.Employee",
        required=False, allow_null=True, write_only=True,
    )
    againstEmployeeId = TenantPrimaryKeyRelatedField(
        source="against_employee", model="hrms.Employee",
        required=False, allow_null=True,
    )
    againstName = serializers.SerializerMethodField()
    complainant = serializers.SerializerMethodField()
    against = serializers.SerializerMethodField()
    summary = serializers.CharField(source="description", required=False, allow_blank=True)
    priority = serializers.CharField(required=False, allow_blank=True)
    assignedInvestigator = serializers.CharField(source="assigned_investigator_name", required=False, allow_blank=True)
    filedOn = serializers.DateField(source="filed_on", required=False, allow_null=True)
    isAnonymous = serializers.BooleanField(source="is_anonymous", required=False)
    currentStep = serializers.IntegerField(source="current_step", read_only=True)
    approvalChainId = serializers.CharField(source="approval_chain_id", read_only=True)
    approvalHistory = serializers.JSONField(source="approval_history", read_only=True)

    class Meta:
        model = Complaint
        fields = [
            "id", "raisedBy", "raisedByEmployeeId", "complainant", "againstEmployeeId",
            "againstName", "against", "category", "description", "summary", "priority",
            "isAnonymous", "status", "assigned_to", "assignedInvestigator", "filedOn",
            "resolution", "resolved_at", "comments", "currentStep", "approvalChainId", "approvalHistory",
            "created_at",
        ]
        read_only_fields = ["resolved_at", "currentStep", "approvalChainId", "approvalHistory", "created_at"]

    def get_raisedBy(self, obj):
        if obj.is_anonymous:
            return "Anonymous"
        return obj.raised_by_employee.name if obj.raised_by_employee else "Anonymous"

    def get_complainant(self, obj):
        return self.get_raisedBy(obj)

    def get_againstName(self, obj):
        return obj.against_employee.name if obj.against_employee else "Management / General"

    def get_against(self, obj):
        return self.get_againstName(obj)

    def to_internal_value(self, data):
        data = data.copy() if hasattr(data, "copy") else dict(data)
        request = self.context.get("request")
        client_id = getattr(request, "client_id", None) or getattr(getattr(request, "user", None), "client_id", None)
        from .models import Employee

        # Complainant
        comp_val = data.get("complainant") or data.get("raisedBy")
        if comp_val and not data.get("raisedByEmployeeId") and str(comp_val).lower() != "anonymous" and client_id:
            emp = Employee.objects.filter(client_id=client_id, name__iexact=str(comp_val).strip(), deleted_at__isnull=True).first()
            if emp:
                data["raisedByEmployeeId"] = str(emp.id)

        # Against
        against_val = data.get("against") or data.get("againstName")
        if against_val and not data.get("againstEmployeeId") and client_id:
            emp = Employee.objects.filter(client_id=client_id, name__iexact=str(against_val).strip(), deleted_at__isnull=True).first()
            if emp:
                data["againstEmployeeId"] = str(emp.id)

        # Investigator
        inv_val = data.get("assignedInvestigator")
        if inv_val and not data.get("assigned_investigator_name"):
            data["assigned_investigator_name"] = str(inv_val).strip()

        if "summary" in data and "description" not in data:
            data["description"] = data["summary"]
        if "filedOn" in data and "filed_on" not in data:
            data["filed_on"] = data["filedOn"]
        if not data.get("filed_on") and not data.get("filedOn"):
            data["filed_on"] = timezone.now().date().isoformat()

        return super().to_internal_value(data)

    def to_representation(self, instance):
        import datetime as dt
        if instance.filed_on and isinstance(instance.filed_on, dt.datetime):
            instance.filed_on = instance.filed_on.date()
        ret = super().to_representation(instance)
        ret["complainant"] = self.get_complainant(instance)
        ret["raisedBy"] = ret["complainant"]
        ret["against"] = self.get_against(instance)
        ret["againstName"] = ret["against"]
        ret["summary"] = instance.description or ""
        ret["assignedInvestigator"] = instance.assigned_investigator_name or (
            (instance.assigned_to.name or instance.assigned_to.email)
            if instance.assigned_to
            else "Ethics Committee"
        )
        ret["filedOn"] = instance.filed_on.isoformat() if instance.filed_on else (instance.created_at.date().isoformat() if instance.created_at else "")
        ret["status"] = instance.status or "Open"
        return ret


# ---------------------------------------------------------------------------
# Upgradation Scope: Transfers, Promotions, Warnings, Awards, Travel, Announcements
# ---------------------------------------------------------------------------
class EmployeeTransferSerializer(BaseModelSerializer):
    employeeId = TenantPrimaryKeyRelatedField(source="employee", model="hrms.Employee")
    employeeName = serializers.CharField(source="employee.name", read_only=True)
    employeeCode = serializers.CharField(source="employee.employee_code", read_only=True)
    fromDepartmentId = TenantPrimaryKeyRelatedField(source="from_department", model="hrms.Department", required=False, allow_null=True)
    fromDepartmentName = serializers.CharField(source="from_department.name", read_only=True)
    toDepartmentId = TenantPrimaryKeyRelatedField(source="to_department", model="hrms.Department", required=False, allow_null=True)
    toDepartmentName = serializers.CharField(source="to_department.name", read_only=True)
    fromLocationId = TenantPrimaryKeyRelatedField(source="from_location", model="hrms.Location", required=False, allow_null=True)
    fromLocationName = serializers.CharField(source="from_location.name", read_only=True)
    toLocationId = TenantPrimaryKeyRelatedField(source="to_location", model="hrms.Location", required=False, allow_null=True)
    toLocationName = serializers.CharField(source="to_location.name", read_only=True)

    class Meta:
        model = EmployeeTransfer
        fields = [
            "id", "transfer_number", "employeeId", "employeeName", "employeeCode",
            "fromDepartmentId", "fromDepartmentName", "toDepartmentId", "toDepartmentName",
            "fromLocationId", "fromLocationName", "toLocationId", "toLocationName",
            "effective_date", "reason", "status", "created_at", "updated_at",
        ]
        read_only_fields = ["transfer_number", "created_at", "updated_at"]

    def to_internal_value(self, data):
        data = data.copy() if hasattr(data, "copy") else dict(data)
        client_id = self.context.get("client_id")
        if "employee" in data and "employeeId" not in data:
            data["employeeId"] = data["employee"]

        to_dept = data.get("toDepartmentId") or data.get("to_department")
        if to_dept:
            from apps.hrms.models import Department
            dept_obj = Department.objects.filter(client_id=client_id, id=to_dept).first() if isinstance(to_dept, str) and len(to_dept) == 36 and "-" in to_dept else None
            if not dept_obj and isinstance(to_dept, str):
                dept_obj = Department.objects.filter(client_id=client_id, name__iexact=to_dept.strip()).first()
                if not dept_obj:
                    dept_obj = Department.objects.create(client_id=client_id, name=to_dept.strip())
            if dept_obj:
                data["toDepartmentId"] = str(dept_obj.id)

        from_dept = data.get("fromDepartmentId") or data.get("from_department")
        if from_dept:
            from apps.hrms.models import Department
            dept_obj = Department.objects.filter(client_id=client_id, id=from_dept).first() if isinstance(from_dept, str) and len(from_dept) == 36 and "-" in from_dept else None
            if not dept_obj and isinstance(from_dept, str):
                dept_obj = Department.objects.filter(client_id=client_id, name__iexact=from_dept.strip()).first()
            if dept_obj:
                data["fromDepartmentId"] = str(dept_obj.id)

        return super().to_internal_value(data)

    def to_representation(self, instance):
        ret = super().to_representation(instance)
        ret["from_department"] = instance.from_department.name if instance.from_department else ""
        ret["to_department"] = instance.to_department.name if instance.to_department else ""
        return ret


class EmployeePromotionSerializer(BaseModelSerializer):
    employeeId = TenantPrimaryKeyRelatedField(source="employee", model="hrms.Employee")
    employeeName = serializers.CharField(source="employee.name", read_only=True)
    employeeCode = serializers.CharField(source="employee.employee_code", read_only=True)
    fromDesignationId = TenantPrimaryKeyRelatedField(source="from_designation", model="hrms.Designation", required=False, allow_null=True)
    fromDesignationTitle = serializers.CharField(source="from_designation.title", read_only=True)
    toDesignationId = TenantPrimaryKeyRelatedField(source="to_designation", model="hrms.Designation", required=False, allow_null=True)
    toDesignationTitle = serializers.CharField(source="to_designation.title", read_only=True)

    class Meta:
        model = EmployeePromotion
        fields = [
            "id", "promotion_number", "employeeId", "employeeName", "employeeCode",
            "fromDesignationId", "fromDesignationTitle", "toDesignationId", "toDesignationTitle",
            "previous_salary", "new_salary", "effective_date", "justification", "status",
            "created_at", "updated_at",
        ]
        read_only_fields = ["promotion_number", "created_at", "updated_at"]

    def to_internal_value(self, data):
        data = data.copy() if hasattr(data, "copy") else dict(data)
        client_id = self.context.get("client_id")
        if "employee" in data and "employeeId" not in data:
            data["employeeId"] = data["employee"]
        if "notes" in data and "justification" not in data:
            data["justification"] = data["notes"]

        to_desig = data.get("toDesignationId") or data.get("new_designation") or data.get("to_designation")
        if to_desig:
            from apps.hrms.models import Designation
            desig_obj = Designation.objects.filter(client_id=client_id, id=to_desig).first() if isinstance(to_desig, str) and len(to_desig) == 36 and "-" in to_desig else None
            if not desig_obj and isinstance(to_desig, str):
                desig_obj = Designation.objects.filter(client_id=client_id, title__iexact=to_desig.strip()).first()
                if not desig_obj:
                    desig_obj = Designation.objects.create(client_id=client_id, title=to_desig.strip())
            if desig_obj:
                data["toDesignationId"] = str(desig_obj.id)

        from_desig = data.get("fromDesignationId") or data.get("previous_designation") or data.get("from_designation")
        if from_desig:
            from apps.hrms.models import Designation
            desig_obj = Designation.objects.filter(client_id=client_id, id=from_desig).first() if isinstance(from_desig, str) and len(from_desig) == 36 and "-" in from_desig else None
            if not desig_obj and isinstance(from_desig, str):
                desig_obj = Designation.objects.filter(client_id=client_id, title__iexact=from_desig.strip()).first()
            if desig_obj:
                data["fromDesignationId"] = str(desig_obj.id)

        return super().to_internal_value(data)

    def to_representation(self, instance):
        ret = super().to_representation(instance)
        ret["previous_designation"] = instance.from_designation.title if instance.from_designation else ""
        ret["new_designation"] = instance.to_designation.title if instance.to_designation else ""
        ret["notes"] = instance.justification or ""
        return ret


class EmployeeWarningSerializer(BaseModelSerializer):
    employeeId = TenantPrimaryKeyRelatedField(source="employee", model="hrms.Employee")
    employeeName = serializers.CharField(source="employee.name", read_only=True)
    employeeCode = serializers.CharField(source="employee.employee_code", read_only=True)

    class Meta:
        model = EmployeeWarning
        fields = [
            "id", "warning_number", "employeeId", "employeeName", "employeeCode",
            "issue_date", "severity", "incident_date", "subject", "description",
            "corrective_action", "status", "employee_explanation", "created_at", "updated_at",
        ]
        read_only_fields = ["warning_number", "created_at", "updated_at"]

    def to_internal_value(self, data):
        data = data.copy() if hasattr(data, "copy") else dict(data)
        if "employee" in data and "employeeId" not in data:
            data["employeeId"] = data["employee"]
        if "title" in data and "subject" not in data:
            data["subject"] = data["title"]
        if "explanation" in data and "description" not in data:
            data["description"] = data["explanation"]
        if "warning_date" in data and "issue_date" not in data:
            data["issue_date"] = data["warning_date"]

        sev = data.get("severity") or data.get("warning_level")
        if sev:
            sev_map = {
                "low": "Verbal",
                "medium": "First Written",
                "severe": "Final Written",
                "verbal": "Verbal",
                "first written": "First Written",
                "final written": "Final Written",
                "suspension": "Suspension",
            }
            data["severity"] = sev_map.get(str(sev).lower(), "First Written")

        return super().to_internal_value(data)

    def to_representation(self, instance):
        ret = super().to_representation(instance)
        ret["title"] = instance.subject
        ret["explanation"] = instance.description
        ret["warning_date"] = str(instance.issue_date) if instance.issue_date else ""
        ret["warning_level"] = instance.severity
        return ret


class EmployeeAwardSerializer(BaseModelSerializer):
    employeeId = TenantPrimaryKeyRelatedField(source="employee", model="hrms.Employee")
    employeeName = serializers.CharField(source="employee.name", read_only=True)
    employeeCode = serializers.CharField(source="employee.employee_code", read_only=True)

    class Meta:
        model = EmployeeAward
        fields = [
            "id", "award_number", "employeeId", "employeeName", "employeeCode",
            "award_name", "category", "award_date", "gift_amount", "citation",
            "badge_icon", "created_at",
        ]
        read_only_fields = ["award_number", "created_at"]

    def to_internal_value(self, data):
        data = data.copy() if hasattr(data, "copy") else dict(data)
        if "employee" in data and "employeeId" not in data:
            data["employeeId"] = data["employee"]
        if "cash_reward" in data and "gift_amount" not in data:
            data["gift_amount"] = data["cash_reward"]
        if "presentation_date" in data and "award_date" not in data:
            data["award_date"] = data["presentation_date"]

        return super().to_internal_value(data)

    def to_representation(self, instance):
        ret = super().to_representation(instance)
        ret["cash_reward"] = instance.gift_amount
        ret["presentation_date"] = str(instance.award_date) if instance.award_date else ""
        return ret


class TravelRequestSerializer(BaseModelSerializer):
    employeeId = TenantPrimaryKeyRelatedField(source="employee", model="hrms.Employee")
    employeeName = serializers.CharField(source="employee.name", read_only=True)
    employeeCode = serializers.CharField(source="employee.employee_code", read_only=True)
    currentStep = serializers.IntegerField(source="current_step", read_only=True)
    approvalChainId = serializers.CharField(source="approval_chain_id", read_only=True)
    approvalHistory = serializers.JSONField(source="approval_history", read_only=True)
    approverName = serializers.CharField(source="approver.name", read_only=True, default="")
    decidedAt = serializers.DateTimeField(source="decided_at", read_only=True)
    remark = serializers.CharField(required=False, allow_blank=True, allow_null=True)

    class Meta:
        model = TravelRequest
        fields = [
            "id", "travel_number", "employeeId", "employeeName", "employeeCode",
            "purpose", "destination", "start_date", "end_date", "advance_requested",
            "advance_disbursed", "actual_expenses", "settlement_notes", "status",
            "currentStep", "approvalChainId", "approvalHistory", "approverName",
            "decidedAt", "remark", "created_at", "updated_at",
        ]
        read_only_fields = [
            "travel_number", "currentStep", "approvalChainId", "approvalHistory",
            "approverName", "decidedAt", "created_at", "updated_at"
        ]

    def validate(self, data):
        data = super().validate(data)
        start = data.get("start_date")
        end = data.get("end_date")
        if start and end and end < start:
            raise serializers.ValidationError({
                "end_date": "End date cannot be earlier than start date."
            })

        emp = data.get("employee")
        if emp:
            if emp.status in ("Terminated", "Resigned"):
                from apps.core.exceptions import Conflict
                raise Conflict(
                    f"Cannot create travel requisition for {emp.name}: employee is {emp.status.lower()}.",
                    code="EMPLOYEE_EXITED",
                )
            if emp.last_working_day and start and start > emp.last_working_day:
                from apps.core.exceptions import Conflict
                raise Conflict(
                    f"Travel request starts after employee exit date ({emp.last_working_day}).",
                    code="EMPLOYEE_EXITED",
                )
        return data


class AnnouncementSerializer(BaseModelSerializer):
    targetDepartmentId = TenantPrimaryKeyRelatedField(source="target_department", model="hrms.Department", required=False, allow_null=True)
    targetDepartmentName = serializers.CharField(source="target_department.name", read_only=True)

    class Meta:
        model = Announcement
        fields = [
            "id", "title", "content", "priority", "targetDepartmentId",
            "targetDepartmentName", "is_pinned", "publish_date", "expiry_date",
            "author_name", "status", "created_at", "updated_at",
        ]
        read_only_fields = ["created_at", "updated_at"]

    def validate(self, data):
        data = super().validate(data)
        pub = data.get("publish_date")
        exp = data.get("expiry_date")
        if pub and exp and exp < pub:
            raise serializers.ValidationError({
                "expiry_date": "Announcement expiry date cannot be prior to publish date."
            })
        return data

    def to_internal_value(self, data):
        data = data.copy() if hasattr(data, "copy") else dict(data)
        p = data.get("priority")
        if p and isinstance(p, str):
            p_clean = p.strip().lower()
            if p_clean in ("urgent", "high", "medium", "low"):
                data["priority"] = p_clean.capitalize()
            elif p_clean == "normal":
                data["priority"] = "Medium"
        return super().to_internal_value(data)


class MeetingRoomSerializer(BaseModelSerializer):
    class Meta:
        model = MeetingRoom
        fields = [
            "id", "name", "code", "capacity", "location", "amenities", "is_active", "created_at", "updated_at",
        ]
        read_only_fields = ["created_at", "updated_at"]


class CompanyMeetingSerializer(BaseModelSerializer):
    roomId = TenantPrimaryKeyRelatedField(
        source="room", model="hrms.MeetingRoom", required=False, allow_null=True
    )
    roomName = serializers.CharField(source="room.name", read_only=True, allow_null=True)
    roomCode = serializers.CharField(source="room.code", read_only=True, allow_null=True)
    hostUserId = TenantPrimaryKeyRelatedField(
        source="host_user", model="accounts.User", required=False, allow_null=True
    )
    hostUserName = serializers.CharField(source="host_user.get_full_name", read_only=True)
    attendeeIds = serializers.PrimaryKeyRelatedField(
        source="attendees", many=True, read_only=False, queryset=User.objects.all(), required=False
    )
    attendeesDetail = serializers.SerializerMethodField(read_only=True)

    class Meta:
        model = CompanyMeeting
        fields = [
            "id", "title", "description", "roomId", "roomName", "roomCode", "meeting_type",
            "start_time", "end_time", "video_provider", "join_url",
            "hostUserId", "hostUserName", "attendeeIds", "attendeesDetail",
            "external_attendees", "agenda", "minutes_of_meeting", "action_items",
            "status", "created_at", "updated_at",
        ]
        read_only_fields = ["created_at", "updated_at"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from apps.accounts.models import User
        if "attendeeIds" in self.fields:
            self.fields["attendeeIds"].queryset = User.objects.all()

    def get_attendeesDetail(self, obj):
        return [
            {"id": str(u.id), "name": u.get_full_name() or u.username, "email": u.email}
            for u in obj.attendees.all()
        ]


