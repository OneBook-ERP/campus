"""M17-01 desk navigation (Frappe 16.53 model): the campus Dock lists every area in order, each area has one standard
Sidebar (sections by job) and a Workspace page that agree, every link resolves, every campus DocType, report and desk Page has
exactly one home, the Dock follows the user's roles and a pack switched off takes its area away."""

import json
from collections import Counter

import frappe
from frappe.boot import get_bootinfo

from campus.campus_platform.api import clear_pack_cache
from campus.campus_platform.module_packs import PACK_WORKSPACES
from campus.campus_platform.pack_visibility import hidden_workspaces
from campus.setup.permission_matrix import TEST_ONLY_DOCTYPES
from campus.tests.factories import make_company, make_user
from campus.tests.utils import CampusTestCase

AREAS = [
	"Student Services", "Admissions", "Academics", "Examinations", "Fees and Finance", "IQAC", "Research", "Hostel",
	"Transport", "Labs", "Placement", "Faculty", "Academic Governance", "Communication", "Approvals", "Administration",
]  # fmt: skip
COLLEGE = "Campus Nav Test College"  # autonomous: the COE pack (Examinations) is open to it
COE_OFF_COLLEGE = "Campus Nav COE Off College"  # autonomous too, but switches the COE pack off
RESEARCH_USER = "campus-nav-research@example.com"
AREA_PACK_USER = "campus-nav-area-packs@example.com"
AREA_PACKS = {
	"HOSTEL": ("Hostel", "Warden"),
	"TRANSPORT": ("Transport", "Warden"),
	"LABS": ("Labs", "Lab In-charge"),
	"PLACEMENT": ("Placement", "Placement Officer"),
}
LAST_SECTIONS = ["Reports", "Setup"]
USERS = {
	"accounts": ("campus-nav-accounts@example.com", ["Accounts User"]),
	"accounts_manager": ("campus-nav-accounts-manager@example.com", ["Accounts Manager"]),
	"coe": ("campus-nav-coe@example.com", ["COE"]),
	"instructor": ("campus-nav-instructor@example.com", ["Instructor"]),
	"officer": ("campus-nav-officer@example.com", ["Admission Officer"]),
	"principal": ("campus-nav-principal@example.com", ["Principal"]),
}


def campus_modules():
	return frappe.get_all("Module Def", {"app_name": "campus"}, pluck="name")


def campus_sidebars():
	names = frappe.get_all("Sidebar", {"module": ["in", campus_modules()], "standard": 1}, pluck="name")
	return [frappe.get_doc("Sidebar", n) for n in names]


def campus_workspaces():
	names = frappe.get_all("Workspace", {"module": ["in", campus_modules()], "public": 1}, pluck="name")
	return [frappe.get_doc("Workspace", n) for n in names]


def sidebar_of(area):
	"""The area's sidebar is the one whose first item (Home) opens the area's workspace."""
	return next(
		s for s in campus_sidebars() if s.items[0].link_type == "Workspace" and s.items[0].link_to == area
	)


def link_items(sidebar):
	return [i for i in sidebar.items if i.type == "Link"]


def sections(sidebar):
	"""Section label -> its links, in order."""
	out = {}
	for item in sidebar.items[1:]:
		if item.type == "Section Break":
			out[item.label] = []
		else:
			out[list(out)[-1]].append(item)
	return out


def dock_areas(bootinfo):
	"""The campus Dock entries a user gets, resolved the way the desk does (Sidebar.dock_entry): a Workspace row needs
	the page in the user's workspaces and a sidebar holding it in module_sidebars."""
	pages = {p["name"] for p in bootinfo.workspaces["pages"]}
	sidebars = bootinfo.module_sidebars
	areas = set()
	for row in (bootinfo.get("dock") or {}).get("campus", []):
		if row.get("hidden") or row["link_to"] not in pages:
			continue
		if any(row["link_to"] in s.get("workspaces", []) for s in sidebars.values()):
			areas.add(row["title"])
	return areas


def desk_boot(email):
	"""Boot as `email`. The workspace list is cached per request, so the previous user's must go."""
	frappe.set_user(email)
	frappe.cache.hdel("bootinfo", email)
	if hasattr(frappe.local, "request_cache"):
		frappe.local.request_cache.clear()
	return get_bootinfo()


def make_desk_user(email, roles, college):
	"""A user with only `roles` who sees only `college`."""
	frappe.get_doc(
		{
			"doctype": "User",
			"email": email,
			"first_name": "nav",
			"send_welcome_email": 0,
			"roles": [{"role": r} for r in roles],
		}
	).insert()
	frappe.get_doc(
		{"doctype": "User Permission", "user": email, "allow": "Company", "for_value": college}
	).insert()


def exam_shells(bootinfo):
	return {shell for shell, s in bootinfo.module_sidebars.items() if s["module"] == "Examinations"}


class TestNavigation(CampusTestCase):
	def test_dock_lists_every_area_in_order(self):
		dock = frappe.get_doc("Dock", "campus")
		self.assertTrue(dock.standard)
		self.assertEqual([i.title for i in dock.items], AREAS)
		self.assertEqual({w.name for w in campus_workspaces()}, set(AREAS))
		for item in dock.items:
			self.assertEqual((item.link_type, item.link_to), ("Workspace", item.title))
			self.assertTrue(item.icon, item.title)
			self.assertFalse(item.hidden, item.title)
			self.assertEqual(sidebar_of(item.title).header_icon, item.icon, item.title)

	def test_each_area_has_one_sidebar_in_its_workspace_module(self):
		sidebars = campus_sidebars()
		self.assertEqual(len(sidebars), len(AREAS))
		self.assertEqual(sorted(s.items[0].link_to for s in sidebars), sorted(AREAS))
		for workspace in campus_workspaces():
			self.assertEqual(sidebar_of(workspace.name).module, workspace.module, workspace.name)
		self.assertEqual(Counter(s.module for s in sidebars).most_common(1)[0][1], 1)  # one shell per module

	def test_approvals_has_its_own_module(self):
		self.assertEqual(frappe.db.get_value("Workspace", "Approvals", "module"), "Campus Approvals")
		self.assertEqual(sidebar_of("Approvals").module, "Campus Approvals")
		self.assertEqual(sidebar_of("Administration").module, "Campus Platform")

	def test_communication_is_labelled_communication(self):
		bootinfo = desk_boot("Administrator")
		self.assertEqual(bootinfo.module_sidebars[sidebar_of("Communication").name]["label"], "Communication")

	def test_no_area_is_open_to_every_desk_user(self):
		for workspace in campus_workspaces():
			self.assertTrue(workspace.roles, f"workspace {workspace.name} has no roles")
			# the retiring icon grid: upgraded sites keep its rows, a fresh site in Apps mode has none
			if not frappe.db.exists("Desktop Icon", workspace.name):
				continue
			icon = frappe.get_doc("Desktop Icon", workspace.name)
			self.assertEqual({r.role for r in icon.roles}, {r.role for r in workspace.roles}, workspace.name)

	def test_students_and_guardians_have_no_desk_access(self):
		for role in ("Student", "Guardian"):
			self.assertEqual(frappe.db.get_value("Role", role, "desk_access"), 0, role)

	def test_sidebar_sections_are_by_job_with_reports_and_setup_last(self):
		for sidebar in campus_sidebars():
			home = sidebar.items[0]
			self.assertEqual((home.type, home.label, home.icon), ("Link", "Home", "house"), sidebar.name)
			by_section = sections(sidebar)
			self.assertGreaterEqual(len(by_section), 2, sidebar.name)
			labels = list(by_section)
			tail = [s for s in LAST_SECTIONS if s in labels]
			self.assertEqual(labels[len(labels) - len(tail) :], tail, sidebar.name)
			self.assertTrue({"Masters", "Transactions", "Settings"}.isdisjoint(labels), sidebar.name)
			for label, links in by_section.items():
				self.assertTrue(links, f"{sidebar.name}: empty section {label}")
				self.assertTrue(all(i.child for i in links), f"{sidebar.name}: {label}")
				if label != "Reports":
					self.assertFalse(
						[i.label for i in links if i.link_type == "Report"], f"{sidebar.name}: {label}"
					)
			for item in sidebar.items:
				if item.type == "Section Break":
					self.assertTrue(item.icon, f"{sidebar.name}: {item.label}")

	def test_every_link_target_exists(self):
		for sidebar in campus_sidebars():
			for item in link_items(sidebar):
				if item.link_type == "URL":
					self.assertTrue(item.url.startswith("/"), item.label)
				else:
					self.assertTrue(
						frappe.db.exists(item.link_type, item.link_to),
						f"{sidebar.name}: {item.link_type} {item.link_to}",
					)
		for workspace in campus_workspaces():
			for link in workspace.links:
				if link.type == "Link":
					self.assertTrue(
						frappe.db.exists(link.link_type, link.link_to), f"{workspace.name}: {link.link_to}"
					)
			for shortcut in workspace.shortcuts:
				if shortcut.type != "URL":
					self.assertTrue(
						frappe.db.exists(shortcut.type, shortcut.link_to),
						f"{workspace.name}: {shortcut.link_to}",
					)

	def test_every_campus_doctype_has_exactly_one_home_area(self):
		homes = Counter(  # a filtered link is a saved view of its DocType, not a second home
			i.link_to
			for s in campus_sidebars()
			for i in link_items(s)
			if i.link_type == "DocType" and not i.filters
		)
		doctypes = frappe.get_all(
			"DocType",
			{
				"module": ["in", campus_modules()],
				"istable": 0,
				"custom": 0,
				"name": ["not in", list(TEST_ONLY_DOCTYPES)],
			},
			pluck="name",
		)
		self.assertTrue(doctypes)
		self.assertEqual({d: homes.get(d, 0) for d in doctypes if homes.get(d, 0) != 1}, {})
		self.assertEqual(homes["UAT Feedback"], 1)
		self.assertIn("UAT Feedback", [i.link_to for i in link_items(sidebar_of("Administration"))])

	def test_every_campus_desk_page_has_exactly_one_home_area(self):
		homes = Counter(i.link_to for s in campus_sidebars() for i in link_items(s) if i.link_type == "Page")
		pages = frappe.get_all("Page", {"module": ["in", campus_modules()]}, pluck="name")
		self.assertIn("student-360", pages)
		self.assertEqual({p: homes.get(p, 0) for p in pages if homes.get(p, 0) != 1}, {})
		self.assertIn("student-360", [i.link_to for i in link_items(sidebar_of("Student Services"))])

	def test_every_campus_report_has_exactly_one_home_area(self):
		homes = Counter(
			i.link_to for s in campus_sidebars() for i in link_items(s) if i.link_type == "Report"
		)
		reports = frappe.get_all("Report", {"module": ["in", campus_modules()]}, pluck="name")
		self.assertTrue(reports)
		self.assertEqual({r: homes.get(r, 0) for r in reports if homes.get(r, 0) != 1}, {})

	def test_workspace_page_mirrors_the_sidebar(self):
		for workspace in campus_workspaces():
			sidebar = sidebar_of(workspace.name)
			in_sidebar = {
				(i.link_type, i.link_to)
				for i in link_items(sidebar)
				if i.link_type in ("DocType", "Report", "Page")
			}
			on_page = {(l.link_type, l.link_to) for l in workspace.links if l.type == "Link"}
			self.assertEqual(on_page, in_sidebar, workspace.name)
			self.assertEqual(
				[l.label for l in workspace.links if l.type == "Card Break"],
				list(sections(sidebar)),
				workspace.name,
			)
			self.assertEqual(
				[b["data"]["card_name"] for b in json.loads(workspace.content) if b["type"] == "card"],
				list(sections(sidebar)),
			)
			shortcuts = [s.label for s in workspace.shortcuts]
			self.assertTrue(4 <= len(shortcuts) <= 6, workspace.name)
			self.assertEqual(
				[
					b["data"]["shortcut_name"]
					for b in json.loads(workspace.content)
					if b["type"] == "shortcut"
				],
				shortcuts,
			)

	def test_workspace_doctype_slug_collisions_use_explicit_list_view(self):
		"""Verify workspace shortcuts whose target DocType matches the workspace name set doc_view='List'."""
		self.assertIn("/assets/campus/js/navigation.js", frappe.get_hooks("app_include_js", app_name="campus"))
		collisions = {w.name for w in campus_workspaces() if frappe.db.exists("DocType", w.name)}
		self.assertIn("Hostel", collisions)
		for workspace in campus_workspaces():
			for shortcut in workspace.shortcuts:
				if shortcut.type == "DocType" and shortcut.link_to == workspace.name:
					self.assertEqual(shortcut.doc_view, "List", f"{workspace.name}: {shortcut.label}")

	def test_module_packs_hide_their_area(self):
		for code, area in (
			("COE", "Examinations"), ("GOVERNANCE", "Academic Governance"), ("IQAC", "IQAC"), ("RESEARCH", "Research"),
			*((code, area) for code, (area, _role) in AREA_PACKS.items()),
		):  # fmt: skip
			self.assertIn(
				area, frappe.get_all("Module Pack Workspace", {"parent": code}, pluck="workspace"), code
			)
		self.assertEqual(set(PACK_WORKSPACES), {"COE", "GOVERNANCE", "IQAC", "RESEARCH", *AREA_PACKS})

	def test_hostel_labs_and_placement_go_with_their_packs(self):
		make_desk_user(AREA_PACK_USER, [role for _area, role in AREA_PACKS.values()], COLLEGE)
		areas = {area for area, _role in AREA_PACKS.values()}
		self.assertTrue(areas <= dock_areas(desk_boot(AREA_PACK_USER)))
		frappe.set_user("Administrator")
		profile = frappe.get_doc("Institution Profile", COLLEGE)
		for code in AREA_PACKS:
			profile.append("packs", {"pack": code, "enabled": 0})
		profile.save()
		clear_pack_cache()
		bootinfo = desk_boot(AREA_PACK_USER)
		self.assertTrue(areas.isdisjoint(dock_areas(bootinfo)), dock_areas(bootinfo))
		self.assertTrue(areas.isdisjoint(bootinfo.module_sidebars), list(bootinfo.module_sidebars))

	def test_research_area_hidden_when_its_pack_is_off(self):
		make_user(RESEARCH_USER, [COLLEGE])  # sees only this college, so the college's switch decides
		self.assertNotIn("Research", hidden_workspaces(RESEARCH_USER))
		profile = frappe.get_doc("Institution Profile", COLLEGE)
		profile.append("packs", {"pack": "RESEARCH", "enabled": 0})
		profile.save()
		clear_pack_cache()
		self.assertIn("Research", hidden_workspaces(RESEARCH_USER))
		bootinfo = desk_boot(RESEARCH_USER)
		self.assertNotIn("Research", [r["link_to"] for r in bootinfo.dock.get("campus", [])])
		self.assertNotIn("Research", bootinfo.module_sidebars)

	def test_pack_off_hides_the_area_sidebar_from_users_without_the_area_role(self):
		"""Frappe keeps the Examinations sidebar (without Home) for an Accounts Manager, who reads some exam DocType
		but lacks the workspace role. The pack switch must still take it away, routing included."""
		profile = frappe.get_doc("Institution Profile", COE_OFF_COLLEGE)
		profile.append("packs", {"pack": "COE", "enabled": 0})
		profile.save()
		on_user, off_user = "campus-nav-am-coe-on@example.com", "campus-nav-am-coe-off@example.com"
		for email, college in ((on_user, COLLEGE), (off_user, COE_OFF_COLLEGE)):
			make_desk_user(email, ["Accounts Manager"], college)

		self.assertIn("Examinations", exam_shells(desk_boot(on_user)))  # the pack is on for this college

		bootinfo = desk_boot(off_user)
		self.assertEqual(exam_shells(bootinfo), set())
		self.assertNotIn("Examinations", bootinfo.entity_module.values())
		for by_name in bootinfo.canonical_shell.values():
			self.assertNotIn("Examinations", by_name.values())

	def test_no_dues_offices_reach_the_clearances_they_sign(self):
		"""campus_finance.no_dues: the Warden signs off from Hostel, the Lab In-charge from Labs, the Librarian from
		Fees and Finance, where it sees only the clearances."""
		for email, role, area in (
			("campus-nav-warden@example.com", "Warden", "Hostel"), ("campus-nav-lab@example.com", "Lab In-charge", "Labs"),
			("campus-nav-librarian@example.com", "Librarian", "Fees and Finance"),
		):  # fmt: skip
			make_desk_user(email, [role], COLLEGE)
			bootinfo = desk_boot(email)
			frappe.set_user("Administrator")
			self.assertIn(area, dock_areas(bootinfo), role)
			links = [
				i["link_to"]
				for i in bootinfo.module_sidebars[area]["items"]
				if i["type"] == "Link" and i["link_type"] == "DocType"
			]
			self.assertIn("No Dues Clearance", links, role)
			if role == "Librarian":
				self.assertEqual(links, ["No Dues Clearance"])

	# role mapping
	def desk_areas(self, key):
		bootinfo = desk_boot(USERS[key][0])
		return dock_areas(bootinfo), bootinfo

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		for college, abbr in ((COLLEGE, "CNTC"), (COE_OFF_COLLEGE, "CNCO")):
			make_company(college, abbr)  # a pack shows only to users of an institution it is enabled for
			if not frappe.db.exists("Institution Profile", college):
				frappe.get_doc(
					{"doctype": "Institution Profile", "company": college, "institution_status": "Autonomous"}
				).insert()

	def setUp(self):
		frappe.db.savepoint("navigation_test")
		clear_pack_cache()
		self.addCleanup(clear_pack_cache)
		for email, roles in USERS.values():
			if not frappe.db.exists("User", email):
				frappe.get_doc(
					{
						"doctype": "User",
						"email": email,
						"first_name": "nav",
						"send_welcome_email": 0,
						"roles": [{"role": r} for r in roles],
					}
				).insert()

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.rollback(save_point="navigation_test")

	def assert_dock_follows_workspace_roles(self, key, areas):
		roles = set(USERS[key][1])
		allowed = {w.name for w in campus_workspaces() if roles & {r.role for r in w.roles}}
		self.assertEqual(areas, allowed - hidden_workspaces(USERS[key][0]), key)

	def test_accounts_user_sees_finance_but_not_examinations(self):
		areas, bootinfo = self.desk_areas("accounts")
		self.assertIn("Fees and Finance", areas)
		self.assertTrue({"Examinations", "Admissions", "IQAC", "Administration"}.isdisjoint(areas), areas)
		self.assertNotIn("Examinations", [p["name"] for p in bootinfo.workspaces["pages"]])
		self.assert_dock_follows_workspace_roles("accounts", areas)

	def test_coe_sees_examinations_only_of_the_campus_areas_it_is_for(self):
		areas, bootinfo = self.desk_areas("coe")
		self.assertIn("Examinations", areas)
		self.assertTrue({"Fees and Finance", "Admissions", "IQAC"}.isdisjoint(areas), areas)
		sidebar = bootinfo.module_sidebars["Examinations"]
		self.assertIn("Student Result", [i["link_to"] for i in sidebar["items"]])
		self.assert_dock_follows_workspace_roles("coe", areas)

	def test_admission_officer_sees_admissions(self):
		areas, _ = self.desk_areas("officer")
		self.assertIn("Admissions", areas)
		self.assertTrue({"Examinations", "Fees and Finance", "IQAC"}.isdisjoint(areas), areas)
		self.assert_dock_follows_workspace_roles("officer", areas)

	def test_instructor_accounts_manager_and_principal_get_only_their_dock_entries(self):
		for key in ("instructor", "accounts_manager", "principal"):
			areas, _ = self.desk_areas(key)
			self.assertIn("Approvals", areas, key)  # every staff role reaches the approval inbox
			self.assert_dock_follows_workspace_roles(key, areas)
