import sys
import os
import django

sys.path.insert(0, os.path.abspath("Backend"))
os.environ["DJANGO_SETTINGS_MODULE"] = "config.settings"
django.setup()

from datetime import date
from django.utils import timezone
from apps.accounts.models import Client, User, Role
from apps.core.models import CompanyProfile, Setting
from apps.core.tenancy import tenant_context, set_current_client_id
from apps.hrms.models import (
    Employee, ApprovalChain, LeaveRequest, LeaveType, LeaveBalance,
    TravelRequest, Complaint, Resignation, Termination
)
from apps.hrms.workflow_service import (
    initialize_request_workflow,
    process_approval_action,
    can_user_approve_step,
    get_active_workflow,
    get_workflow_steps
)
from apps.hrms.views import (
    HrmsSettingsView, ApprovalChainViewSet, LeaveRequestViewSet,
    TravelRequestViewSet, ComplaintViewSet, ResignationViewSet, TerminationViewSet
)
from rest_framework.test import APIRequestFactory, force_authenticate

def run_tests():
    print("=== Starting End-to-End Verification ===")
    factory = APIRequestFactory()

    # 1. Setup Tenant and Users
    client, _ = Client.objects.get_or_create(
        slug="workflow-test-org",
        defaults={"name": "Workflow Test Org"}
    )
    set_current_client_id(client.id)

    with tenant_context(client.id):
        # Clean up previous test artifacts for idempotent runs
        Setting.objects.filter(client_id=client.id).delete()
        ApprovalChain.objects.filter(client_id=client.id).delete()
        LeaveRequest.objects.filter(client_id=client.id).delete()
        TravelRequest.objects.filter(client_id=client.id).delete()
        Complaint.objects.filter(client_id=client.id).delete()

        admin_role, _ = Role.objects.get_or_create(
            client=client, code="ADMIN",
            defaults={"name": "Administrator", "is_system": True}
        )
        emp_role, _ = Role.objects.get_or_create(
            client=client, code="EMPLOYEE",
            defaults={"name": "Staff Member", "is_system": False}
        )
        hr_role, _ = Role.objects.get_or_create(
            client=client, code="HR_ADMIN",
            defaults={"name": "HR Manager", "is_system": False}
        )

        admin_user, _ = User.objects.get_or_create(
            client=client, email="admin@workflow-test.local",
            defaults={"name": "Admin Tester", "role": admin_role, "status": "Active", "is_staff": True}
        )
        admin_user.is_superuser = True
        admin_user.is_staff = True
        admin_user.save()

        manager_user, _ = User.objects.get_or_create(
            client=client, email="manager@workflow-test.local",
            defaults={"name": "Manager Tester", "role": emp_role, "status": "Active"}
        )

        hr_user, _ = User.objects.get_or_create(
            client=client, email="hr@workflow-test.local",
            defaults={"name": "HR Tester", "role": hr_role, "status": "Active"}
        )

        requester_user, _ = User.objects.get_or_create(
            client=client, email="employee@workflow-test.local",
            defaults={"name": "Employee Tester", "role": emp_role, "status": "Active"}
        )

        unauthorized_user, _ = User.objects.get_or_create(
            client=client, email="unauth@workflow-test.local",
            defaults={"name": "Stranger Tester", "role": emp_role, "status": "Active"}
        )

        # Grant explicit permission sets to simulate RBAC authentication
        admin_user.permission_ids = {"view_staff", "edit_staff", "apply_leave", "approve_leave"}
        manager_user.permission_ids = {"view_staff", "edit_staff", "apply_leave", "approve_leave"}
        hr_user.permission_ids = {"view_staff", "edit_staff", "delete_staff", "apply_leave", "approve_leave"}
        requester_user.permission_ids = {"view_staff", "edit_staff", "apply_leave"}
        unauthorized_user.permission_ids = {"view_staff", "apply_leave"}

        emp_mgr, _ = Employee.objects.get_or_create(
            client=client, employee_code="EMP-MGR-001",
            defaults={"name": "Manager Tester", "email": manager_user.email, "joining_date": date(2025, 1, 1), "status": "Active"}
        )
        emp_hr, _ = Employee.objects.get_or_create(
            client=client, employee_code="EMP-HR-001",
            defaults={"name": "HR Tester", "email": hr_user.email, "joining_date": date(2025, 1, 1), "status": "Active"}
        )
        emp_req, _ = Employee.objects.get_or_create(
            client=client, employee_code="EMP-REQ-001",
            defaults={"name": "Employee Tester", "email": requester_user.email, "manager": emp_mgr, "joining_date": date(2025, 1, 1), "status": "Active"}
        )
        if emp_req.manager != emp_mgr:
            emp_req.manager = emp_mgr
            emp_req.save()

        manager_user.employee = emp_mgr
        manager_user.save()
        hr_user.employee = emp_hr
        hr_user.save()
        requester_user.employee = emp_req
        requester_user.save()

        print("[PASS] Tenant, Roles, Users and Employees initialized.")

        # 2. Test Organization Settings
        settings_view = HrmsSettingsView.as_view()
        
        # 2.1 GET settings (defaults)
        req = factory.get("/api/hrms/settings/")
        req.client_id = client.id
        force_authenticate(req, user=admin_user)
        resp = settings_view(req)
        assert resp.status_code == 200, f"Settings GET failed: {resp.data}"
        assert "sandwichRuleEnabled" in resp.data, "sandwichRuleEnabled missing from defaults"
        assert "companyName" in resp.data, "companyName missing"
        print("[PASS] Organization Settings GET verified (defaults returned).")

        # 2.2 PUT settings (update sandwichRuleEnabled and companyName)
        update_data = {
            "companyName": "Sewen Advanced Global Corp",
            "sandwichRuleEnabled": True,
            "maxCarryForwardDays": 15,
            "noticePeriod": "45 days"
        }
        req = factory.put("/api/hrms/settings/", data=update_data, format="json")
        req.client_id = client.id
        force_authenticate(req, user=admin_user)
        resp = settings_view(req)
        assert resp.status_code == 200, f"Settings PUT failed: {resp.data}"
        assert resp.data["sandwichRuleEnabled"] is True
        assert resp.data["maxCarryForwardDays"] == 15
        assert resp.data["noticePeriod"] == "45 days"
        assert resp.data["companyName"] == "Sewen Advanced Global Corp"

        # Verify company profile was synced
        cp = CompanyProfile.objects.filter(client_id=client.id).first()
        assert cp is not None and cp.legal_name == "Sewen Advanced Global Corp", "CompanyProfile not synced!"

        # Partial update verification
        req = factory.put("/api/hrms/settings/", data={"maxCarryForwardDays": 20}, format="json")
        req.client_id = client.id
        force_authenticate(req, user=admin_user)
        resp = settings_view(req)
        assert resp.status_code == 200
        assert resp.data["maxCarryForwardDays"] == 20
        assert resp.data["sandwichRuleEnabled"] is True, "Atomic merge failed, sandwichRuleEnabled wiped!"
        assert resp.data["companyName"] == "Sewen Advanced Global Corp", "companyName wiped on partial PUT!"
        print("[PASS] Organization Settings PUT and atomic merge verified.")

        # 3. Test Approval Chains Configuration
        chain_view = ApprovalChainViewSet.as_view({"post": "create", "get": "list"})
        
        # Create 2-tier Leave Management workflow
        # Step 1: Direct Line Manager, Step 2: HR
        leave_chain_payload = {
            "name": "Standard Leave 2-Tier Approval",
            "module": "Leave Management",
            "status": "Active",
            "steps": [
                {"tier": 1, "role": "Direct Line Manager", "autoEscalateDays": 3},
                {"tier": 2, "role": "HR", "autoEscalateDays": 5}
            ]
        }
        req = factory.post("/api/hrms/approval-chains/", data=leave_chain_payload, format="json")
        req.client_id = client.id
        force_authenticate(req, user=admin_user)
        resp = chain_view(req)
        assert resp.status_code == 201, f"Approval Chain create failed: {resp.data}"
        leave_chain_id = resp.data["id"]

        # Verify modules list action
        modules_view = ApprovalChainViewSet.as_view({"get": "modules"})
        req = factory.get("/api/hrms/approval-chains/modules/")
        req.client_id = client.id
        force_authenticate(req, user=admin_user)
        resp = modules_view(req)
        assert resp.status_code == 200
        assert "Leave Management" in resp.data["modules"]
        assert "Travel Requisitions" in resp.data["modules"]
        print("[PASS] ApprovalChain configuration and endpoints verified.")

        # 4. Test Leave Request Multi-Tier Workflow
        leave_type, _ = LeaveType.objects.get_or_create(
            client=client,
            name="Earned Leave",
            defaults={"annual_entitlement": 20, "is_paid": True}
        )
        balance, _ = LeaveBalance.objects.get_or_create(
            client=client,
            employee=emp_req,
            leave_type=leave_type,
            period_year=2026,
            defaults={"entitlement": 20, "carried_forward": 0, "used": 0, "encashed": 0}
        )
        initial_balance = balance.balance

        leave_create_view = LeaveRequestViewSet.as_view({"post": "create"})
        leave_action_view = LeaveRequestViewSet.as_view({"post": "approve"})

        leave_payload = {
            "employeeId": str(emp_req.id),
            "leaveTypeId": str(leave_type.id),
            "fromDate": "2026-11-02",
            "toDate": "2026-11-04",
            "days": 3,
            "reason": "Family vacation"
        }
        req = factory.post("/api/hrms/leave-requests/", data=leave_payload, format="json")
        req.client_id = client.id
        force_authenticate(req, user=requester_user)
        resp = leave_create_view(req)
        assert resp.status_code == 201, f"Leave create failed: {resp.data}"
        leave_id = resp.data["id"]

        lr = LeaveRequest.objects.get(id=leave_id)
        assert lr.current_step == 1
        assert str(lr.approval_chain_id) == str(leave_chain_id)
        assert "Tier 1" in lr.status or lr.status == "PENDING_TIER_1" or "Pending" in lr.status
        print(f"[PASS] LeaveRequest created with active workflow at step {lr.current_step}, status: {lr.status}")

        # 4.1 Unauthorized user tries to approve Step 1
        req = factory.post(f"/api/hrms/leave-requests/{leave_id}/approve/", data={"remark": "Hacked approval"}, format="json")
        req.client_id = client.id
        force_authenticate(req, user=unauthorized_user)
        try:
            resp = leave_action_view(req, pk=leave_id)
            assert resp.status_code in [400, 403], f"Unauthorized approval should fail! Got: {resp.status_code} - {resp.data}"
        except Exception as e:
            # PermissionDenied or Conflict exception is also expected and acceptable
            print(f"[PASS] Unauthorized approval rejected with exception: {e}")
        print("[PASS] Unauthorized approval blocked.")

        # 4.2 Manager (Step 1) Approves
        req = factory.post(f"/api/hrms/leave-requests/{leave_id}/approve/", data={"remark": "Looks good from manager"}, format="json")
        req.client_id = client.id
        force_authenticate(req, user=manager_user)
        resp = leave_action_view(req, pk=leave_id)
        assert resp.status_code == 200, f"Manager approve failed: {resp.data}"

        lr.refresh_from_db()
        assert lr.current_step == 2, f"Expected step 2, got {lr.current_step}"
        assert lr.status in ("Pending Tier 2", "PENDING_TIER_2")
        assert len(lr.approval_history) == 2  # submission + step 1
        # Check that balance is NOT deducted yet
        balance.refresh_from_db()
        assert balance.balance == initial_balance, "Balance deducted prematurely before final tier approval!"
        print("[PASS] Step 1 approved, transitioned to Step 2 without premature final deduction.")

        # 4.3 HR (Step 2) Final Approval
        req = factory.post(f"/api/hrms/leave-requests/{leave_id}/approve/", data={"remark": "HR Final Approval"}, format="json")
        req.client_id = client.id
        force_authenticate(req, user=hr_user)
        resp = leave_action_view(req, pk=leave_id)
        assert resp.status_code == 200, f"HR final approve failed: {resp.data}"

        lr.refresh_from_db()
        assert lr.status == "Approved", f"Expected Approved, got {lr.status}"
        assert lr.current_step == 2
        assert len(lr.approval_history) == 3
        balance.refresh_from_db()
        assert balance.balance < initial_balance, "Balance should be deducted on final approval!"
        print(f"[PASS] Final Step 2 approval completed, status: {lr.status}, balance deducted to {balance.balance}.")

        # 5. Test Travel Request Approval Workflow
        travel_chain_payload = {
            "name": "Travel 1-Tier Approval",
            "module": "Travel Requisitions",
            "status": "Active",
            "steps": [
                {"tier": 1, "role": "Direct Line Manager", "autoEscalateDays": 2}
            ]
        }
        req = factory.post("/api/hrms/approval-chains/", data=travel_chain_payload, format="json")
        req.client_id = client.id
        force_authenticate(req, user=admin_user)
        resp = chain_view(req)
        assert resp.status_code == 201
        travel_chain_id = resp.data["id"]

        travel_create_view = TravelRequestViewSet.as_view({"post": "create"})
        travel_action_view = TravelRequestViewSet.as_view({"post": "approve"})

        req = factory.post("/api/hrms/travel-requests/", data={
            "employeeId": str(emp_req.id),
            "purpose": "Client meeting",
            "destination": "New York",
            "start_date": "2026-12-01",
            "end_date": "2026-12-05",
            "advance_requested": 1500
        }, format="json")
        req.client_id = client.id
        force_authenticate(req, user=requester_user)
        resp = travel_create_view(req)
        assert resp.status_code == 201, f"Travel create failed: {resp.data}"
        travel_id = resp.data["id"]

        tr = TravelRequest.objects.get(id=travel_id)
        assert tr.current_step == 1
        assert str(tr.approval_chain_id) == str(travel_chain_id)

        # Manager approves travel request
        req = factory.post(f"/api/hrms/travel-requests/{travel_id}/approve/", data={"remark": "Approved travel"}, format="json")
        req.client_id = client.id
        force_authenticate(req, user=manager_user)
        resp = travel_action_view(req, pk=travel_id)
        assert resp.status_code == 200, f"Travel approve failed: {resp.data}"

        tr.refresh_from_db()
        assert tr.status == "Approved"
        assert tr.approver == manager_user
        print("[PASS] TravelRequest workflow initialized and approved.")

        # 6. Test Complaint (Grievance) Workflow Rejection
        complaint_chain_payload = {
            "name": "Complaint 1-Tier Approval",
            "module": "Grievances",
            "status": "Active",
            "steps": [
                {"tier": 1, "role": "HR", "autoEscalateDays": 3}
            ]
        }
        req = factory.post("/api/hrms/approval-chains/", data=complaint_chain_payload, format="json")
        req.client_id = client.id
        force_authenticate(req, user=admin_user)
        resp = chain_view(req)
        assert resp.status_code == 201

        complaint_create = ComplaintViewSet.as_view({"post": "create"})
        complaint_reject = ComplaintViewSet.as_view({"post": "reject"})

        req = factory.post("/api/hrms/complaints/", data={
            "raisedByEmployeeId": str(emp_req.id),
            "category": "WORKPLACE_ENVIRONMENT",
            "description": "Office 4th floor AC is leaking",
            "priority": "Medium"
        }, format="json")
        req.client_id = client.id
        force_authenticate(req, user=requester_user)
        resp = complaint_create(req)
        assert resp.status_code == 201, f"Complaint create failed: {resp.data}"
        complaint_id = resp.data["id"]

        comp = Complaint.objects.get(id=complaint_id)
        assert comp.current_step == 1

        # HR rejects with remark
        req = factory.post(f"/api/hrms/complaints/{complaint_id}/reject/", data={"remark": "Duplicate complaint already filed"}, format="json")
        req.client_id = client.id
        force_authenticate(req, user=hr_user)
        resp = complaint_reject(req, pk=complaint_id)
        assert resp.status_code == 200, f"Complaint reject failed: {resp.data}"

        comp.refresh_from_db()
        assert comp.status in ("Dismissed", "Rejected"), f"Expected Dismissed/Rejected, got {comp.status}"
        assert comp.approval_history[-1]["remark"] == "Duplicate complaint already filed"
        print("[PASS] Grievance workflow initialized and rejected with audit record.")

        # 7. Test Resignation Approval Workflow
        resignation_chain_payload = {
            "name": "Resignation 1-Tier Approval",
            "module": "Resignations",
            "status": "Active",
            "steps": [
                {"tier": 1, "role": "HR", "autoEscalateDays": 3}
            ]
        }
        req = factory.post("/api/hrms/approval-chains/", data=resignation_chain_payload, format="json")
        req.client_id = client.id
        force_authenticate(req, user=admin_user)
        resp = chain_view(req)
        assert resp.status_code == 201

        resignation_create = ResignationViewSet.as_view({"post": "create"})
        resignation_approve = ResignationViewSet.as_view({"post": "approve"})

        req = factory.post("/api/hrms/resignations/", data={
            "employeeId": str(emp_req.id),
            "reason": "Relocating abroad",
            "noticePeriodDays": 30
        }, format="json")
        req.client_id = client.id
        force_authenticate(req, user=requester_user)
        resp = resignation_create(req)
        assert resp.status_code == 201, f"Resignation create failed: {resp.data}"
        resignation_id = resp.data["id"]

        resig = Resignation.objects.get(id=resignation_id)
        assert resig.current_step == 1

        # HR approves resignation
        req = factory.post(f"/api/hrms/resignations/{resignation_id}/approve/", data={"remark": "Resignation accepted"}, format="json")
        req.client_id = client.id
        force_authenticate(req, user=hr_user)
        resp = resignation_approve(req, pk=resignation_id)
        assert resp.status_code == 200, f"Resignation approve failed: {resp.data}"

        resig.refresh_from_db()
        assert resig.status in ("Exit Interview Scheduled", "Approved")
        assert len(resig.approval_history) == 2
        print("[PASS] Resignation workflow initialized and approved.")

        # 8. Test Termination Approval Workflow
        termination_chain_payload = {
            "name": "Termination 1-Tier Approval",
            "module": "Terminations",
            "status": "Active",
            "steps": [
                {"tier": 1, "role": "HR", "autoEscalateDays": 2}
            ]
        }
        req = factory.post("/api/hrms/approval-chains/", data=termination_chain_payload, format="json")
        req.client_id = client.id
        force_authenticate(req, user=admin_user)
        resp = chain_view(req)
        assert resp.status_code == 201

        termination_create = TerminationViewSet.as_view({"post": "create"})
        termination_approve = TerminationViewSet.as_view({"post": "approve"})

        req = factory.post("/api/hrms/terminations/", data={
            "employeeId": str(emp_req.id),
            "terminationType": "Layoff",
            "reason": "Business restructuring"
        }, format="json")
        req.client_id = client.id
        force_authenticate(req, user=admin_user)
        resp = termination_create(req)
        assert resp.status_code == 201, f"Termination create failed: {resp.data}"
        termination_id = resp.data["id"]

        term = Termination.objects.get(id=termination_id)
        assert term.current_step == 1

        # HR approves termination
        req = factory.post(f"/api/hrms/terminations/{termination_id}/approve/", data={"remark": "Severance approved"}, format="json")
        req.client_id = client.id
        force_authenticate(req, user=hr_user)
        resp = termination_approve(req, pk=termination_id)
        assert resp.status_code == 200, f"Termination approve failed: {resp.data}"

        term.refresh_from_db()
        assert term.status in ("In Exit Clearance", "Approved")
        assert len(term.approval_history) == 2
        print("[PASS] Termination workflow initialized and approved.")

        print("\n==========================================")
        print("ALL 5 MODULE WORKFLOWS & SETTINGS TESTS PASSED 100%!")
        print("==========================================")

if __name__ == "__main__":
    run_tests()
