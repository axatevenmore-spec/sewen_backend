"""The company hierarchy the Org Chart draws (``GET /hrms/org-chart/``).

    Organization
     -> Department                 (hrms_departments)
       -> Department head          (departments.head_employee_id)
         -> Employees              (employees.department_id)
           -> Reporting lines      (employees.manager_id)

Built only from stored relationships -- nothing is inferred from names or
designations. The rules that turn them into one tree:

* Everyone active appears exactly once. People who have left (Resigned /
  Terminated) and deleted rows are not on the chart.
* An employee's *home* department is the one they head, else the one they
  belong to, else "Unassigned Department". Someone heading several
  departments sits in their own department when it is one of them, else the
  first by name; the others show them as head without a second node.
* Inside a department, an employee hangs under their reporting manager when
  that manager is in the same department. Cross-department reporting lines
  are kept on the card (``manager``) but not drawn, so a node never appears
  twice.
* Everyone else in the department -- no manager, or a manager elsewhere --
  hangs under the department head (``relation: "department"``), or at the
  top of the department when there is no head.
* Stored data that breaks the rules is not trusted: a reporting cycle is cut
  at one link, a head who is not an active employee counts as "not
  assigned". Each is listed in ``issues`` so HR can fix the record.
"""
from django.db.models import Count, Q

EXITED = ("Resigned", "Terminated")
UNASSIGNED_ID = "unassigned"
UNASSIGNED_NAME = "Unassigned Department"


def build(client_id):
    from .models import Department, Employee

    employees = list(
        Employee.objects.filter(client_id=client_id, deleted_at__isnull=True)
        .exclude(status__in=EXITED)
        .select_related("designation", "department", "location")
        .order_by("name", "employee_code")
    )
    by_id = {e.id: e for e in employees}
    departments = list(
        Department.objects.filter(client_id=client_id, deleted_at__isnull=True)
        .annotate(team_count=Count("teams", filter=Q(teams__deleted_at__isnull=True), distinct=True))
        .order_by("name")
    )
    issues = []

    manager_of = _reporting_lines(employees, by_id, issues)

    # -- heads ------------------------------------------------------------
    head_of = {}  # department id -> employee (valid heads only)
    for dept in departments:
        if dept.head_employee_id is None:
            continue
        head = by_id.get(dept.head_employee_id)
        if head is None:
            issues.append({
                "type": "invalid_head",
                "departmentId": str(dept.id),
                "message": f"{dept.name}: the recorded head is not an active employee.",
            })
            continue
        head_of[dept.id] = head

    # -- home department of every employee --------------------------------
    dept_ids = {d.id for d in departments}
    headed_by = {}
    for dept in departments:
        head = head_of.get(dept.id)
        if head is not None:
            headed_by.setdefault(head.id, []).append(dept.id)
    home = {}
    for e in employees:
        headed = headed_by.get(e.id, [])
        if headed:
            home[e.id] = e.department_id if e.department_id in headed else headed[0]
        elif e.department_id in dept_ids:
            home[e.id] = e.department_id
        else:
            home[e.id] = None  # Unassigned Department

    # -- trees ------------------------------------------------------------
    members = {}
    for e in employees:
        members.setdefault(home[e.id], []).append(e)

    def node(e, relation):
        manager = by_id.get(manager_of.get(e.id))
        return {
            "id": str(e.id),
            "employeeCode": e.employee_code,
            "name": e.name,
            "designation": e.designation.name if e.designation_id else None,
            "department": e.department.name if e.department_id else None,
            "location": e.location.name if e.location_id else None,
            "email": e.email,
            "phone": e.phone,
            "status": e.status,
            "avatar": e.avatar_url,
            "managerId": str(manager.id) if manager else None,
            "manager": manager.name if manager else None,
            "relation": relation,
            "reports": [],
        }

    def department_tree(dept_id, people):
        placed = {e.id for e in people}
        head = head_of.get(dept_id) if dept_id is not None else None
        head_here = head is not None and home.get(head.id) == dept_id

        nodes = {}
        roots = []
        for e in people:
            in_dept_manager = manager_of.get(e.id) if manager_of.get(e.id) in placed else None
            nodes[e.id] = node(e, "reports" if in_dept_manager else "department")
        for e in people:
            in_dept_manager = manager_of.get(e.id) if manager_of.get(e.id) in placed else None
            if in_dept_manager:
                nodes[in_dept_manager]["reports"].append(nodes[e.id])
            else:
                roots.append(nodes[e.id])

        if head_here:
            # The head's own reporting chain inside the department stays on
            # top; every other top-level person sits under the head.
            head_root = _root_of(head.id, manager_of, placed)
            top = [n for n in roots if n["id"] == str(head_root)]
            if head_root == head.id:
                nodes[head.id]["relation"] = "head"
            for n in roots:
                if n["id"] != str(head_root):
                    nodes[head.id]["reports"].append(n)
            roots = top
        for n in nodes.values():
            n["reports"].sort(key=lambda r: (r["relation"] == "department", r["name"].lower()))
        return roots

    result = []
    for dept in departments:
        people = members.get(dept.id, [])
        head = head_of.get(dept.id)
        result.append({
            "id": str(dept.id),
            "name": dept.name,
            "code": dept.code,
            "status": dept.status,
            "head": _head_summary(head),
            "headStatus": "assigned" if head else "missing",
            # The head node lives in another department they also lead.
            "headElsewhere": bool(head and home.get(head.id) != dept.id),
            "employeeCount": len(people),
            "teamCount": dept.team_count,
            "nodes": department_tree(dept.id, people),
        })
    unassigned = members.get(None, [])
    if unassigned:
        result.append({
            "id": UNASSIGNED_ID,
            "name": UNASSIGNED_NAME,
            "code": None,
            "status": "Active",
            "head": None,
            "headStatus": "missing",
            "headElsewhere": False,
            "employeeCount": len(unassigned),
            "teamCount": 0,
            "nodes": department_tree(None, unassigned),
        })

    return {
        "organization": {
            "employeeCount": len(employees),
            "departmentCount": len(departments),
        },
        "departments": result,
        "issues": issues,
    }


def _reporting_lines(employees, by_id, issues):
    """employee id -> manager id, keeping only links to active employees and
    cutting any reporting cycle the stored data contains."""
    manager_of = {}
    for e in employees:
        if e.manager_id and e.manager_id != e.id and e.manager_id in by_id:
            manager_of[e.id] = e.manager_id
        elif e.manager_id == e.id:
            issues.append({
                "type": "self_manager", "employeeId": str(e.id),
                "message": f"{e.name} is recorded as their own manager.",
            })

    done = set()
    for e in employees:
        path, seen = [], set()
        current = e.id
        while current in manager_of and current not in done:
            if current in seen:
                cycle = path[path.index(current):]
                # Cut deterministically: the member with the lowest code
                # keeps no manager on the chart.
                cut = min(cycle, key=lambda i: (by_id[i].employee_code or "", str(i)))
                manager_of.pop(cut, None)
                issues.append({
                    "type": "reporting_cycle", "employeeId": str(cut),
                    "message": "Reporting cycle: " + " -> ".join(by_id[i].name for i in cycle)
                    + f". {by_id[cut].name}'s manager is ignored on the chart.",
                })
                break
            seen.add(current)
            path.append(current)
            current = manager_of[current]
        done.update(path)
    return manager_of


def _root_of(employee_id, manager_of, placed):
    current = employee_id
    while manager_of.get(current) in placed:
        current = manager_of[current]
    return current


def _head_summary(head):
    if head is None:
        return None
    return {
        "id": str(head.id),
        "employeeCode": head.employee_code,
        "name": head.name,
        "designation": head.designation.name if head.designation_id else None,
        "avatar": head.avatar_url,
    }
