"""Organization: departments, designations, locations and the org chart, as
the Organization section of the sidebar reads and writes them."""
from django.test import TestCase
from rest_framework.test import APIClient

from apps.accounts.authentication import build_tokens
from apps.accounts.models import Client, Role, User
from apps.accounts.permission_catalogue import seed_roles, sync_permissions
from apps.hrms.models import Employee, Job

API = "/api/v1/hrms"


class OrganizationTests(TestCase):
    def setUp(self):
        sync_permissions()
        self.tenant = Client.objects.create(slug="org", name="Org")
        seed_roles(self.tenant)
        admin = User.objects.create_user(
            email="admin@org.test", password="pass-12345", client=self.tenant, name="Admin",
            role=Role.objects.get(client=self.tenant, code="AD"),
        )
        self.api = APIClient()
        self.api.credentials(HTTP_AUTHORIZATION=f"Bearer {build_tokens(admin)['access']}")

    def post(self, path, body):
        resp = self.api.post(f"{API}{path}", body, format="json")
        self.assertEqual(resp.status_code, 201, resp.content)
        return resp.json()

    def employee(self, name, **extra):
        return Employee.objects.create(
            client=self.tenant, employee_code=f"E-{name}", name=name,
            joining_date="2026-01-01", **extra,
        )

    def test_department_counts_head_budget_and_edit(self):
        head = self.employee("Priya", avatar_url="https://img/p.png")
        dept = self.post("/departments/", {
            "name": "Engineering", "headEmployeeId": str(head.id), "budget": "350000",
        })
        head.department_id = dept["id"]
        head.save()
        Job.objects.create(client=self.tenant, title="Dev", department_id=dept["id"], openings=3)
        Job.objects.create(client=self.tenant, title="Old", department_id=dept["id"], status="Closed")

        row = self.api.get(f"{API}/departments/{dept['id']}/").json()
        self.assertEqual(
            (row["head"], row["headAvatar"], row["budget"], row["employees"], row["openRoles"]),
            ("Priya", "https://img/p.png", 350000, 1, 3),
        )
        resp = self.api.patch(f"{API}/departments/{dept['id']}/", {"status": "Inactive"}, format="json")
        self.assertEqual(resp.json()["status"], "Inactive")

    def test_duplicate_names_are_a_field_error(self):
        self.post("/departments/", {"name": "Sales"})
        resp = self.api.post(f"{API}/departments/", {"name": "sales"}, format="json")
        self.assertEqual(resp.status_code, 400, resp.content)

    def test_in_use_units_cannot_be_deleted(self):
        dept = self.post("/departments/", {"name": "Ops"})
        desig = self.post("/designations/", {"name": "Lead", "level": 5, "departmentId": dept["id"]})
        loc = self.post("/locations/", {"name": "Pune", "type": "Factory", "address": "MIDC, Pune"})
        self.employee("Ravi", department_id=dept["id"], designation_id=desig["id"], location_id=loc["id"])

        for path in (f"/departments/{dept['id']}/", f"/designations/{desig['id']}/", f"/locations/{loc['id']}/"):
            resp = self.api.delete(f"{API}{path}")
            self.assertEqual(resp.status_code, 409, (path, resp.content))

        Employee.objects.all().delete()
        self.assertEqual(self.api.delete(f"{API}/designations/{desig['id']}/").status_code, 204)
        self.assertEqual(self.api.delete(f"{API}/departments/{dept['id']}/").status_code, 204)
        self.assertEqual(self.api.delete(f"{API}/locations/{loc['id']}/").status_code, 204)

    def test_designation_and_location_shapes(self):
        dept = self.post("/departments/", {"name": "Design"})
        desig = self.post("/designations/", {"name": "Designer", "level": 4, "departmentId": dept["id"]})
        self.assertEqual((desig["level"], desig["department"], desig["employees"]), (4, "Design", 0))
        resp = self.api.post(f"{API}/designations/", {"name": "X", "level": 9}, format="json")
        self.assertEqual(resp.status_code, 400)

        loc = self.post("/locations/", {"name": "HQ", "type": "Headquarters", "address": "1 Main Rd", "timezone": "IST"})
        self.assertEqual((loc["type"], loc["address"], loc["employees"]), ("Headquarters", "1 Main Rd", 0))
        resp = self.api.patch(f"{API}/locations/{loc['id']}/", {"address": "2 New Rd"}, format="json")
        self.assertEqual(resp.json()["address"], "2 New Rd")

    def test_org_chart_tree(self):
        boss = self.employee("Boss", email="boss@org.test")
        self.employee("Ann", manager=boss)
        self.employee("Gone", manager=boss, status="Resigned")
        tree = self.api.get(f"{API}/org-chart/").json()["tree"]
        self.assertEqual(len(tree), 1)
        self.assertEqual((tree[0]["name"], tree[0]["email"]), ("Boss", "boss@org.test"))
        self.assertEqual([(r["name"], r["manager"]) for r in tree[0]["reports"]], [("Ann", "Boss")])


class OrgHierarchyTests(TestCase):
    """``GET /hrms/org-chart/`` -> ``departments``: Organization -> Department
    -> Head -> reporting lines, built only from stored relationships."""

    def setUp(self):
        sync_permissions()
        self.tenant = Client.objects.create(slug="tree", name="Tree")
        seed_roles(self.tenant)
        admin = User.objects.create_user(
            email="admin@tree.test", password="pass-12345", client=self.tenant, name="Admin",
            role=Role.objects.get(client=self.tenant, code="AD"),
        )
        self.api = APIClient()
        self.api.credentials(HTTP_AUTHORIZATION=f"Bearer {build_tokens(admin)['access']}")
        self.seq = 0

    def dept(self, name, head=None):
        from apps.hrms.models import Department

        return Department.objects.create(client=self.tenant, name=name, head_employee=head)

    def emp(self, name, dept=None, manager=None, **extra):
        self.seq += 1
        return Employee.objects.create(
            client=self.tenant, employee_code=f"EMP-{self.seq:04d}", name=name,
            joining_date="2026-01-01", department=dept, manager=manager, **extra,
        )

    def chart(self):
        resp = self.api.get(f"{API}/org-chart/")
        self.assertEqual(resp.status_code, 200, resp.content)
        return resp.json()

    @staticmethod
    def walk(nodes):
        for n in nodes:
            yield n
            yield from OrgHierarchyTests.walk(n["reports"])

    @staticmethod
    def shape(nodes):
        return [(n["name"], OrgHierarchyTests.shape(n["reports"])) for n in nodes]

    def by_name(self, body):
        return {d["name"]: d for d in body["departments"]}

    def test_department_head_employees_and_reporting_lines(self):
        sales, hr, purchase = self.dept("Sales"), self.dept("HR"), self.dept("Purchase")
        neha = self.emp("Neha Shah", sales)
        rahul = self.emp("Rahul Verma", sales, manager=neha)
        self.emp("Anil Junior", sales, manager=rahul)
        self.emp("Sales Executive", sales)  # no manager -> under the head
        divya = self.emp("Divya Nair", hr)
        self.emp("HR Staff", hr)
        self.emp("Purchase Officer", purchase)
        sales.head_employee = neha
        sales.save()
        hr.head_employee = divya
        hr.save()

        depts = self.by_name(self.chart())
        self.assertEqual(self.shape(depts["Sales"]["nodes"]), [
            ("Neha Shah", [("Rahul Verma", [("Anil Junior", [])]), ("Sales Executive", [])]),
        ])
        self.assertEqual(depts["Sales"]["nodes"][0]["relation"], "head")
        self.assertEqual(depts["Sales"]["head"]["name"], "Neha Shah")
        self.assertEqual(self.shape(depts["HR"]["nodes"]), [("Divya Nair", [("HR Staff", [])])])
        # No head: the department says so and its people sit at its top.
        self.assertEqual(depts["Purchase"]["headStatus"], "missing")
        self.assertEqual(self.shape(depts["Purchase"]["nodes"]), [("Purchase Officer", [])])
        self.assertEqual(
            (depts["Sales"]["employeeCount"], depts["HR"]["employeeCount"], depts["Purchase"]["employeeCount"]),
            (4, 2, 1),
        )

    def test_unassigned_left_and_invalid_head(self):
        ops = self.dept("Ops")
        gone = self.emp("Gone Head", ops, status="Terminated")
        ops.head_employee = gone
        ops.save()
        self.emp("Floater")  # no department
        self.emp("Ops Worker", ops)

        body = self.chart()
        depts = self.by_name(body)
        self.assertEqual(self.shape(depts["Unassigned Department"]["nodes"]), [("Floater", [])])
        self.assertEqual((depts["Ops"]["headStatus"], depts["Ops"]["head"]), ("missing", None))
        self.assertEqual(self.shape(depts["Ops"]["nodes"]), [("Ops Worker", [])])
        self.assertIn("invalid_head", [i["type"] for i in body["issues"]])
        self.assertEqual(body["organization"]["employeeCount"], 2)

    def test_cycles_cross_department_managers_and_heads_elsewhere(self):
        mgmt, sales, accounts = self.dept("Management"), self.dept("Sales"), self.dept("Accounts")
        neha = self.emp("Neha", mgmt)  # heads Sales, belongs to Management
        sales.head_employee = neha
        sales.save()
        seller = self.emp("Seller", sales)
        a = self.emp("A", accounts)
        b = self.emp("B", accounts, manager=a)
        Employee.objects.filter(pk=a.pk).update(manager=b)  # A <-> B loop in stored data
        self.emp("Auditor", accounts, manager=neha)  # manager in another department

        body = self.chart()
        depts = self.by_name(body)
        self.assertEqual(self.shape(depts["Sales"]["nodes"]), [("Neha", [("Seller", [])])])
        self.assertEqual(depts["Management"]["employeeCount"], 0)
        accounts_nodes = list(self.walk(depts["Accounts"]["nodes"]))
        self.assertEqual(sorted(n["name"] for n in accounts_nodes), ["A", "Auditor", "B"])
        auditor = next(n for n in accounts_nodes if n["name"] == "Auditor")
        self.assertEqual(auditor["manager"], "Neha")
        self.assertIn("reporting_cycle", [i["type"] for i in body["issues"]])
        self.assertEqual(depts["Sales"]["nodes"][0]["reports"][0]["id"], str(seller.id))

    def test_every_active_employee_once_at_scale(self):
        import random

        rng = random.Random(7)
        departments = [self.dept(f"Dept {i:02d}") for i in range(20)]
        people = []
        for i in range(600):
            dept = rng.choice(departments + [None])
            same = [p for p in people[-80:] if p.department_id == (dept.id if dept else None)]
            manager = rng.choice(same) if same and rng.random() < 0.8 else None
            people.append(self.emp(f"Person {i:03d}", dept, manager=manager))
        for dept in departments[:15]:
            staff = [p for p in people if p.department_id == dept.id]
            if staff:
                dept.head_employee = staff[0]
                dept.save()

        body = self.chart()
        ids = [n["id"] for d in body["departments"] for n in self.walk(d["nodes"])]
        self.assertEqual(len(ids), len(set(ids)))  # no duplicates
        self.assertEqual(len(ids), 600)            # no orphans
        self.assertEqual(body["organization"]["employeeCount"], 600)
        self.assertEqual(sum(d["employeeCount"] for d in body["departments"]), 600)

    def test_heads_and_managers_must_be_active_employees(self):
        gone = self.emp("Gone", status="Resigned")
        resp = self.api.post(f"{API}/departments/", {"name": "X", "headEmployeeId": str(gone.id)}, format="json")
        self.assertEqual(resp.status_code, 400, resp.content)
        worker = self.emp("Worker")
        resp = self.api.patch(f"{API}/employees/{worker.id}/", {"managerId": str(gone.id)}, format="json")
        self.assertEqual(resp.status_code, 400, resp.content)
        resp = self.api.patch(f"{API}/employees/{worker.id}/", {"managerId": str(worker.id)}, format="json")
        self.assertEqual(resp.status_code, 400, resp.content)
