"""
The RBAC permission catalogue (api.md Appendix B).

``GET /admin/permissions/`` returns this grouped exactly as the UI renders it:
modules -> groups -> permissions. The seed lives here rather than in the
frontend's ``DEFAULT_MODULE_PERMISSIONS`` constant, which api-integration.md
§9.4 keeps only as an offline-dev fallback.

The block after "Gaps to fill" in Appendix B is included: Appendix B shipped
with no ids for Sales/Purchase/Inventory document actions or for PMS stage
handoff and approval, and api.md asks for them in the same structure.
"""

#: (module, group, [(permission_id, label), ...])
CATALOGUE = [
    (
        "CRM",
        "CRM Dashboard",
        [
            ("show_crm_dashboard", "Show CRM dashboard"),
            ("show_hrm_dashboard", "Show HRM dashboard"),
            ("show_account_dashboard", "Show account dashboard"),
            ("show_templates_menu", "Show templates menu"),
        ],
    ),
    (
        "CRM",
        "Lead Management",
        [
            ("create_lead", "Create lead"),
            ("view_lead", "View lead"),
            ("edit_lead", "Edit lead"),
            ("delete_lead", "Delete lead"),
            ("move_lead", "Move lead between stages"),
        ],
    ),
    (
        "CRM",
        "Pipeline & Stage",
        [
            ("manage_pipeline", "Manage pipeline"),
            ("create_pipeline", "Create pipeline"),
            ("edit_pipeline", "Edit pipeline"),
            ("delete_pipeline", "Delete pipeline"),
        ],
    ),
    (
        "CRM",
        "Tasks",
        [
            ("view_task", "View task"),
            ("create_task", "Create task"),
            ("edit_task", "Edit task"),
            ("delete_task", "Delete task"),
            ("assign_task", "Assign task"),
            ("manage_task_allocation", "Manage task allocation"),
        ],
    ),
    (
        "Staff",
        "Staff & User Access",
        [
            ("view_staff", "View staff"),
            ("create_staff", "Create staff"),
            ("edit_staff", "Edit staff"),
            ("delete_staff", "Delete staff"),
            ("manage_roles", "Manage roles"),
            ("reset_staff_password", "Reset staff password"),
        ],
    ),
    (
        "Project",
        "Projects & Milestones",
        [
            ("view_projects", "View projects"),
            ("create_project", "Create project"),
            ("edit_project", "Edit project"),
            ("delete_project", "Delete project"),
            ("manage_milestones", "Manage milestones"),
            ("assign_members", "Assign members"),
        ],
    ),
    (
        "HRM",
        "Attendance & Leave",
        [
            ("mark_attendance", "Mark attendance"),
            ("view_team_attendance", "View team attendance"),
            ("apply_leave", "Apply for leave"),
            ("approve_leave", "Approve leave"),
            ("regularize_attendance", "Regularize attendance"),
        ],
    ),
    (
        "HRM",
        "Payroll & Compensation",
        [
            ("view_own_payslip", "View own payslip"),
            ("generate_payroll", "Generate payroll"),
            ("edit_salary_structure", "Edit salary structure"),
            ("approve_payroll", "Approve payroll"),
        ],
    ),
    (
        "Account",
        "General Ledger & Accounts",
        [
            ("view_bank_accounts", "View bank accounts"),
            ("manage_journal_entries", "Manage journal entries"),
            ("view_ledger", "View ledger"),
            ("view_financial_reports", "View financial reports"),
            ("reconcile_bank", "Reconcile bank"),
            ("manage_bank_accounts", "Manage bank accounts"),
            ("close_financial_period", "Close financial period"),
            ("manage_chart_of_accounts", "Manage chart of accounts"),
            ("manage_taxes", "Manage taxes and tax rates"),
        ],
    ),
    (
        "POS",
        "Point of Sale & Invoicing",
        [
            ("create_pos_invoice", "Create POS invoice"),
            ("view_pos_orders", "View POS orders"),
            ("apply_discounts", "Apply discounts"),
            ("process_returns", "Process returns"),
            ("print_receipts", "Print receipts"),
        ],
    ),
    (
        "Menu Access",
        "Navigation Visibility",
        [
            ("menu_crm", "CRM menu"),
            ("menu_sales", "Sales menu"),
            ("menu_purchase", "Purchase menu"),
            ("menu_inventory", "Inventory menu"),
            ("menu_accounts", "Accounts menu"),
            ("menu_hrms", "HRMS menu"),
            ("menu_admin", "Administration menu"),
            ("menu_pms", "PMS menu"),
            ("menu_organization", "Organization menu"),
            ("menu_masters", "Masters menu"),
        ],
    ),
    (
        "Other Modules",
        "System Utilities & Tools",
        [
            ("export_excel", "Export to Excel"),
            ("view_audit_logs", "View audit logs"),
            ("system_backup", "System backup"),
            ("manage_company_profile", "Manage company profile"),
        ],
    ),
    # ---- api.md Appendix B, "Gaps to fill" --------------------------------
    (
        "Sales",
        "Sales Documents",
        [
            ("view_sales", "View sales"),
            ("create_quotation", "Create quotation"),
            ("create_sales_order", "Create sales order"),
            ("create_invoice", "Create invoice"),
            ("finalize_invoice", "Finalize invoice"),
            ("cancel_invoice", "Cancel invoice"),
            ("record_payment_in", "Record payment in"),
            ("override_credit_limit", "Override credit limit"),
            ("create_credit_note", "Create credit note"),
        ],
    ),
    (
        "Purchase",
        "Purchase Documents",
        [
            ("view_purchase", "View purchase"),
            ("create_purchase_order", "Create purchase order"),
            ("receive_goods", "Receive goods"),
            ("approve_qc", "Approve QC"),
            ("create_bill", "Create bill"),
            ("record_payment_out", "Record payment out"),
            ("cancel_purchase_document", "Cancel purchase document"),
            ("create_payment", "Create payment / advance"),
            ("edit_purchase", "Edit purchase / ASN documents"),
            ("create_debit_note", "Create debit note"),
        ],
    ),
    (
        "Inventory",
        "Stock Operations",
        [
            ("view_inventory", "View inventory"),
            ("adjust_stock", "Adjust stock"),
            ("create_transfer", "Create transfer"),
            ("approve_zone_request", "Approve zone request"),
            ("perform_audit", "Perform audit"),
            ("edit_inventory", "Edit inventory, demo & rework units"),
        ],
    ),
    (
        "Masters",
        "Master Records",
        [
            ("view_masters", "View master records"),
            ("manage_items", "Manage items and products"),
            ("manage_parties", "Manage customers and vendors"),
            ("manage_warehouses", "Manage warehouses and locations"),
            ("manage_price_lists", "Manage price lists"),
        ],
    ),
    (
        "PMS",
        "Project Management",
        [
            ("view_pms", "View PMS"),
            ("create_pms_project", "Create PMS project"),
            ("assign_stage", "Assign stage"),
            ("handoff_stage", "Handoff stage"),
            ("approve_document", "Approve document"),
            ("share_client_proof", "Share client proof"),
            ("log_delay", "Log delay"),
            ("complete_project", "Complete project"),
        ],
    ),
    # ---- Referenced by the Deals and Contracts views (and api.md §3.1's
    # example payload) but missing from Appendix B. Appended last so no
    # existing sort_order moves. ---------------------------------------------
    (
        "CRM",
        "Deals & Contracts",
        [
            ("manage_deals", "Create, edit and delete deals and contracts"),
        ],
    ),
    # ---- Client portal reorder (apps/sales/portal.py). Appended last for the
    # same reason. ------------------------------------------------------------
    (
        "Sales",
        "Client Portal",
        [
            ("place_portal_order", "Reorder purchased items from the client portal"),
        ],
    ),
]


def iter_permissions():
    """Yield ``(id, module, group, label, sort_order)`` in catalogue order."""
    sort_order = 0
    for module, group, permissions in CATALOGUE:
        for permission_id, label in permissions:
            yield permission_id, module, group, label, sort_order
            sort_order += 10


def all_permission_ids():
    return [permission_id for permission_id, *_ in iter_permissions()]


def sync_permissions():
    """Upsert the catalogue. Idempotent -- safe to run on every deploy."""
    from .models import Permission

    existing = {row.id: row for row in Permission.objects.all()}
    to_create, to_update = [], []
    for permission_id, module, group, label, sort_order in iter_permissions():
        row = existing.get(permission_id)
        if row is None:
            to_create.append(
                Permission(
                    id=permission_id,
                    module=module,
                    group=group,
                    label=label,
                    sort_order=sort_order,
                )
            )
        elif (row.module, row.group, row.label, row.sort_order) != (
            module,
            group,
            label,
            sort_order,
        ):
            row.module, row.group, row.label, row.sort_order = module, group, label, sort_order
            to_update.append(row)

    if to_create:
        Permission.objects.bulk_create(to_create)
    if to_update:
        Permission.objects.bulk_update(to_update, ["module", "group", "label", "sort_order"])
    return len(to_create), len(to_update)


def grouped_catalogue():
    """The exact structure ``GET /admin/permissions/`` returns.

    modules -> groups -> permissions, read from the table so a tenant that has
    added a permission sees it without a frontend release.
    """
    from .models import Permission

    modules = []
    index = {}
    for row in Permission.objects.all().order_by("module", "sort_order", "id"):
        module = index.get(row.module)
        if module is None:
            module = {"module": row.module, "groups": []}
            index[row.module] = module
            modules.append(module)
        group = next((g for g in module["groups"] if g["group"] == row.group), None)
        if group is None:
            group = {"group": row.group, "permissions": []}
            module["groups"].append(group)
        group["permissions"].append(
            {"id": row.id, "label": row.label, "description": row.description}
        )
    return modules


#: Seed roles for a fresh tenant. ``"*"`` means every permission in the
#: catalogue -- the Administrator role.
DEFAULT_ROLES = [
    ("AD", "Administrator", "Full access to every module and system configuration.", ["*"]),
    (
        "GM",
        "General Manager",
        "Operational supervision and approval access across sales, projects, inventory, purchase, accounts, HR, and CRM.",
        [
            "menu_crm", "menu_sales", "menu_purchase", "menu_inventory", "menu_accounts", "menu_hrms", "menu_pms", "menu_organization", "menu_masters",
            "show_crm_dashboard", "show_hrm_dashboard", "show_account_dashboard", "show_templates_menu",
            "view_lead", "create_lead", "edit_lead", "move_lead", "manage_pipeline",
            "view_task", "create_task", "edit_task", "assign_task", "manage_task_allocation",
            "manage_deals",
            "view_sales", "create_quotation", "create_sales_order", "create_invoice", "finalize_invoice", "cancel_invoice", "record_payment_in", "override_credit_limit", "create_credit_note",
            "view_purchase", "create_purchase_order", "receive_goods", "approve_qc", "create_bill", "record_payment_out", "create_payment", "edit_purchase", "create_debit_note", "cancel_purchase_document",
            "view_inventory", "adjust_stock", "create_transfer", "approve_zone_request", "perform_audit", "edit_inventory",
            "view_projects", "create_project", "edit_project", "manage_milestones", "assign_members",
            "view_pms", "create_pms_project", "assign_stage", "handoff_stage", "approve_document", "share_client_proof", "log_delay", "complete_project",
            "view_bank_accounts", "manage_bank_accounts", "manage_journal_entries", "view_ledger", "view_financial_reports", "reconcile_bank", "close_financial_period", "manage_chart_of_accounts", "manage_taxes",
            "view_staff", "mark_attendance", "view_team_attendance", "apply_leave", "approve_leave", "regularize_attendance", "view_own_payslip", "approve_payroll",
            "view_masters", "manage_items", "manage_parties", "manage_warehouses", "manage_price_lists",
            "view_pos_orders", "create_pos_invoice", "apply_discounts", "process_returns", "print_receipts",
            "export_excel", "view_audit_logs",
        ],
    ),
    (
        "PM",
        "Project Manager",
        "Full access to all project related work: PMS projects, stages, milestones, tasks, team allocation, and customer tracking.",
        [
            "menu_pms", "menu_sales", "menu_masters",
            "view_pms", "create_pms_project", "assign_stage", "handoff_stage",
            "approve_document", "share_client_proof", "log_delay", "complete_project",
            "view_projects", "create_project", "edit_project", "delete_project",
            "manage_milestones", "assign_members",
            "view_task", "create_task", "edit_task", "delete_task", "assign_task", "manage_task_allocation",
            "view_sales", "view_masters", "manage_parties", "export_excel",
        ],
    ),
    (
        "EM",
        "Project Employee",
        "View and edit assigned project tasks, punch attendance, view own payslips, and apply for leave.",
        [
            "menu_pms", "view_pms", "view_projects", "view_task", "edit_task",
            "mark_attendance", "apply_leave", "view_own_payslip",
        ],
    ),
    (
        "TC",
        "Telecaller",
        "Access to view and manage CRM leads, tasks, calls, and export activity reports.",
        [
            "menu_crm", "show_crm_dashboard",
            "view_lead", "create_lead", "edit_lead", "move_lead", "manage_deals",
            "view_task", "create_task", "edit_task", "export_excel",
        ],
    ),
    (
        "SE",
        "Sales Person",
        "Full sales permissions: leads, pipeline, deals, quotations, sales orders, invoices, payments, and clients.",
        [
            "menu_sales", "menu_crm", "menu_inventory", "menu_masters",
            "show_crm_dashboard", "view_lead", "create_lead", "edit_lead", "delete_lead", "move_lead",
            "manage_pipeline", "create_pipeline", "edit_pipeline", "delete_pipeline", "manage_deals",
            "view_task", "create_task", "edit_task", "delete_task", "assign_task", "manage_task_allocation",
            "view_sales", "create_quotation", "create_sales_order", "create_invoice", "finalize_invoice", "cancel_invoice",
            "record_payment_in", "override_credit_limit", "create_credit_note",
            "view_masters", "manage_parties", "manage_price_lists", "view_inventory",
            "create_pos_invoice", "view_pos_orders", "apply_discounts", "process_returns", "print_receipts",
            "export_excel",
        ],
    ),
    (
        "SM",
        "Sales Manager",
        "Sales leadership: pipeline oversight, quotation approvals, customer credit, and order confirmation.",
        [
            "menu_sales", "menu_crm", "menu_inventory", "menu_masters",
            "show_crm_dashboard", "view_lead", "create_lead", "edit_lead", "delete_lead", "move_lead",
            "manage_pipeline", "create_pipeline", "edit_pipeline", "delete_pipeline", "manage_deals",
            "view_task", "create_task", "edit_task", "delete_task", "assign_task", "manage_task_allocation",
            "view_sales", "create_quotation", "create_sales_order", "create_invoice", "finalize_invoice", "cancel_invoice",
            "record_payment_in", "override_credit_limit", "create_credit_note",
            "view_masters", "manage_parties", "manage_price_lists", "view_inventory",
            "create_pos_invoice", "view_pos_orders", "apply_discounts", "process_returns", "print_receipts",
            "export_excel",
        ],
    ),
    (
        "HR",
        "HR Manager",
        "Full HR permissions: employee profiles, attendance, leave approvals, payroll, and organization.",
        [
            "menu_hrms", "menu_organization", "show_hrm_dashboard",
            "view_staff", "create_staff", "edit_staff", "delete_staff", "reset_staff_password",
            "mark_attendance", "view_team_attendance", "regularize_attendance",
            "apply_leave", "approve_leave",
            "view_own_payslip", "generate_payroll", "edit_salary_structure", "approve_payroll",
            "export_excel",
        ],
    ),
    (
        "CU",
        "Customer",
        "Full access to customer project tracking, stage progress, milestones, and client portal ordering.",
        [
            "menu_pms", "menu_sales",
            "view_projects", "view_pms", "view_sales", "place_portal_order",
        ],
    ),
    (
        "AC",
        "Accountant",
        "General ledger, bank accounts, journal entries, payments, invoicing, and financial statements.",
        [
            "menu_accounts", "menu_sales", "menu_purchase", "menu_masters",
            "show_account_dashboard", "view_bank_accounts", "manage_bank_accounts", "manage_journal_entries",
            "view_ledger", "view_financial_reports", "reconcile_bank", "close_financial_period",
            "manage_chart_of_accounts", "manage_taxes",
            "view_sales", "view_purchase", "record_payment_in", "record_payment_out", "finalize_invoice",
            "create_credit_note", "create_debit_note", "view_masters", "export_excel",
        ],
    ),
    (
        "PU",
        "Purchase Officer",
        "Procurement, purchase orders, goods receipt notes, steel weight tolerance, and QC approvals.",
        [
            "menu_purchase", "menu_inventory", "menu_masters",
            "view_purchase", "create_purchase_order", "create_bill", "receive_goods",
            "approve_qc", "record_payment_out", "create_payment", "edit_purchase",
            "create_debit_note", "cancel_purchase_document", "view_masters", "manage_parties",
            "view_inventory", "adjust_stock", "create_transfer",
        ],
    ),
    (
        "ST",
        "Store Keeper",
        "Stock movements, warehouse transfers, physical audits, raw material yard, and scrap management.",
        [
            "menu_inventory", "menu_masters",
            "view_inventory", "adjust_stock", "create_transfer",
            "approve_zone_request", "perform_audit", "receive_goods", "edit_inventory",
            "view_masters", "manage_items", "manage_warehouses",
        ],
    ),
]


def seed_roles(client, sync_existing=False):
    """Create or synchronize the default roles for a tenant. Idempotent."""
    from .models import Permission, Role, RolePermission

    client_id = getattr(client, "id", client)
    every_permission = list(Permission.objects.values_list("id", flat=True))
    synced_roles = []
    for code, name, description, permission_ids in DEFAULT_ROLES:
        role, created = Role.objects.get_or_create(
            client_id=client_id,
            code=code,
            defaults={"name": name, "description": description, "is_system": True},
        )
        wanted = every_permission if permission_ids == ["*"] else permission_ids
        valid_perms = [pid for pid in wanted if pid in set(every_permission)]

        if created or sync_existing:
            if not created:
                role.name = name
                role.description = description
                role.is_system = True
                role.save(update_fields=["name", "description", "is_system"])
                RolePermission.objects.filter(role=role).delete()

            RolePermission.objects.bulk_create(
                [
                    RolePermission(role=role, permission_id=permission_id)
                    for permission_id in valid_perms
                ],
                ignore_conflicts=True,
            )
        synced_roles.append(role)
    return synced_roles
