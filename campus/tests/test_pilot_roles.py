"""PR-02 least privilege for the pilot teacher logins: faculty, HOD, class advisor and COE no longer hold the office role
(Academics User), a re-run of the kit takes it away from existing pilot users, and each role's UAT path still works
under the reduced roles. The demo teachers of campus.tests.demo hold Academics User; each test here takes it away.

COE: its flows (exam registrations, hall tickets, results, grade sheets) are proven with a COE-only login in
test_exam_registration, test_hall_tickets and test_results; this module checks its desk areas and reports.
"""

import json
from unittest import mock

import frappe
from frappe.desk import reportview
from frappe.desk.query_report import get_report_doc
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, add_to_date, now_datetime, today

from campus.admissions.setup import OFFICER_READ, OFFICER_RIGHTS
from campus.campus_platform.api import latest_request
from campus.campus_platform.approvals import approve, request_approval
from campus.faculty import attendance_corrections, portal, portal_mentoring
from campus.faculty.setup import GRANTED_RIGHTS_KEY, TEACHING_STAFF_RIGHTS, grant_teaching_staff_rights
from campus.setup import people
from campus.setup.demo_data import staff
from campus.setup.profile import PROFILE_DIR
from campus.student_services import corrections
from campus.tests import demo
from campus.tests.test_faculty_portal import COLLEAGUE, TEACHER, FacultyPortalFixture
from campus.tests.test_malpractice import INVIGILATOR, OTHER_INVIGILATOR, MalpracticeFixture
from campus.tests.test_mentoring import MENTOR_A, MentoringFixture
from campus.tests.test_navigation import desk_boot, dock_areas
from campus.tests.test_timetable import CSE, employee

OFFICE_ROLE = "Academics User"
SXCCE = json.loads((PROFILE_DIR / "sxcce.json").read_text())
KIT_ROLES = {user["key"]: user["roles"] for user in SXCCE["users"]}
TEACHING_KEYS = ("faculty", "hod", "class_advisor", "coe")
HOD = demo.email("hod_cse")
CORRECTION = "Attendance Correction Request"
BY_HAND = "Report Manager"  # a role the kit never gives
# The desk areas each pilot role's guide (docs/usage/roles) names, and office areas it must not see.
AREAS = {
	"faculty": ({"Academics", "Examinations", "Student Services", "Faculty", "Research", "Approvals"}, {"Communication", "Academic Governance"}),
	"hod": ({"Academic Governance", "Academics", "Communication", "Examinations", "Faculty", "Research", "Student Services", "Approvals"}, set()),
	"class_advisor": ({"Student Services", "Approvals"}, {"Communication", "Academic Governance"}),
	"coe": ({"Examinations", "Academics", "Student Services", "Approvals"}, {"Communication", "Academic Governance", "Faculty"}),
}
REPORTS = {
	"faculty": ("My Pending Approvals",),
	"hod": (
		"My Pending Approvals", "Workload Balance", "Timetable Clash Checker", "Attendance Shortage", "CIA Status by Offering",
		"Result Analysis", "Arrear List",
	),
	"class_advisor": ("My Pending Approvals", "Attendance Shortage", "Mentoring Activity"),
	"coe": ("My Pending Approvals", "Exam Eligibility Withheld", "CIA Status by Offering", "Result Analysis", "Rank List", "Arrear List"),
	"principal": ("My Pending Approvals", "Result Analysis", "Lab Equipment Register"),
	"accounts": ("Student Fee Ledger",),
	"admissions": (
		"Import Check", "Live Seat Matrix", "Pending Documents", "Admission No-Shows", "Admissions by Quota and Category",
		"Refund Register",
	),
}  # fmt: skip


def as_user(user, fn, *args, **kwargs):
	frappe.set_user(user)
	try:
		return fn(*args, **kwargs)
	finally:
		frappe.set_user("Administrator")


def drop_office_role(case, *users):
	"""Take Academics User from demo teachers for one test (the fixtures' savepoint rollback gives it back)."""
	for user in users:
		frappe.db.delete("Has Role", {"parent": user, "parenttype": "User", "role": OFFICE_ROLE})
		frappe.clear_cache(user=user)
		case.addCleanup(restore_office_role, user)


def restore_office_role(user):
	if not frappe.db.exists("Has Role", {"parent": user, "parenttype": "User", "role": OFFICE_ROLE}):
		frappe.get_doc({"doctype": "Has Role", "parent": user, "parenttype": "User", "parentfield": "roles", "role": OFFICE_ROLE}).db_insert()
	frappe.clear_cache(user=user)


def kit_login(key):
	"""A login holding exactly the pilot kit's role set of `key`."""
	email = f"campus-pilot-roles-{key.replace('_', '-')}@example.com"
	if not frappe.db.exists("User", email):
		frappe.get_doc(
			{"doctype": "User", "email": email, "first_name": key, "send_welcome_email": 0, "roles": [{"role": r} for r in KIT_ROLES[key]]}
		).insert()  # fmt: skip
	return email


class TestKitRoleSets(IntegrationTestCase):
	def test_teachers_hold_no_office_role_and_the_office_keeps_it(self):
		for key in TEACHING_KEYS:
			self.assertNotIn(OFFICE_ROLE, KIT_ROLES[key], key)
		self.assertEqual(KIT_ROLES["office"], [OFFICE_ROLE])
		self.assertEqual((staff.HOD_ROLES, staff.FACULTY_ROLES), (["HOD", "Instructor"], ["Instructor"]))
		self.assertEqual(set(KIT_ROLES["class_advisor"]), {"Class Advisor", "Mentor", "Instructor"})
		self.assertEqual(KIT_ROLES["coe"], ["COE"])

	def test_only_teaching_specs_lose_the_retired_office_role(self):
		for key in (*TEACHING_KEYS, "admissions"):
			self.assertEqual(people.retired_roles(KIT_ROLES[key]), {OFFICE_ROLE}, key)
		for key in ("office", "principal", "iqac", "accounts", "student"):
			self.assertEqual(people.retired_roles(KIT_ROLES[key]), set(), key)

	def test_a_sync_takes_away_only_the_retired_role(self):
		user = "campus-pilot-roles-sync@example.com"
		frappe.get_doc(
			{"doctype": "User", "email": user, "first_name": "sync", "send_welcome_email": 0,
			"roles": [{"role": r} for r in ("Instructor", OFFICE_ROLE, "Mentor", BY_HAND)]}
		).insert()  # fmt: skip
		self.assertEqual(people.sync_roles(user, ["Instructor", "Class Advisor"]), (["Class Advisor"], [OFFICE_ROLE]))
		held = set(frappe.get_roles(user))
		self.assertTrue({"Instructor", "Class Advisor", "Mentor", BY_HAND} <= held)  # Mentor: a kit role given by hand
		self.assertNotIn(OFFICE_ROLE, held)
		self.assertEqual(people.sync_roles(user, ["Instructor", "Class Advisor"]), ([], []))  # idempotent
		self.assertEqual(set(frappe.get_roles(user)), held)

	def test_a_sync_keeps_a_retired_role_an_active_position_gives(self):
		user = "campus-pilot-roles-position@example.com"
		frappe.get_doc(
			{"doctype": "User", "email": user, "first_name": "position", "send_welcome_email": 0, "roles": [{"role": r} for r in ("Instructor", OFFICE_ROLE)]}
		).insert()  # fmt: skip
		office_seat = [frappe._dict(name="seat", position="OFFICE", role=OFFICE_ROLE, role_granted=1)]
		with mock.patch("campus.setup.people.active_holdings", return_value=office_seat):
			self.assertEqual(people.sync_roles(user, ["Instructor"]), ([], []))
		self.assertIn(OFFICE_ROLE, frappe.get_roles(user))
		self.assertEqual(people.sync_roles(user, ["Instructor"]), ([], [OFFICE_ROLE]))  # the seat ended

	def test_a_teacher_has_no_desk_rights_on_attendance_marks_or_periods(self):
		user = kit_login("faculty")
		refused = {
			"Student Attendance": ("read", "write", "create", "submit"),
			"Assessment Result": ("write", "create", "submit"),
			"Assessment Plan": ("write", "create", "submit"),
			"Course Schedule": ("read", "write", "create"),
		}
		for doctype, ptypes in refused.items():
			for ptype in ptypes:
				self.assertFalse(frappe.has_permission(doctype, ptype, user=user), f"{doctype} {ptype}")
		self.assertTrue(frappe.has_permission("Course Schedule", "select", user=user))

	@mock.patch("campus.campus_platform.pack_visibility.hidden_workspaces", return_value=set())
	def test_each_role_sees_its_desk_areas_and_no_office_area(self, _packs):
		for key, (wanted, refused) in AREAS.items():
			user = kit_login(key)
			try:  # module packs are per college (test_navigation); here only the roles decide
				areas = dock_areas(desk_boot(user))
			finally:
				frappe.set_user("Administrator")
			self.assertTrue(wanted <= areas, f"{key}: missing {wanted - areas}")
			self.assertTrue(refused.isdisjoint(areas), f"{key}: sees {refused & areas}")
			self.assertTrue({"Fees and Finance", "Admissions"}.isdisjoint(areas), key)

	def test_each_role_opens_the_reports_of_its_uat_script(self):
		for key, reports in REPORTS.items():
			user = kit_login(key)
			for report in reports:
				self.assertEqual(as_user(user, get_report_doc, report).name, report, f"{key}: {report}")

	def test_accounts_does_not_open_the_unscoped_education_fee_collection_report(self):
		"""Education's Student Fee Collection is not company-scoped: Accounts uses the campus Student Fee Ledger."""
		user = kit_login("accounts")
		with self.assertRaises(frappe.PermissionError):
			as_user(user, get_report_doc, "Student Fee Collection")
		self.assertFalse(frappe.db.exists("Custom Role", {"report": "Student Fee Collection"}))

	def test_the_office_reads_the_instructor_records_it_allocates(self):
		user = kit_login("office")  # Faculty > Setup > Instructor; Course Allocation and Timetable name instructors
		self.assertTrue(frappe.has_permission("Instructor", "read", user=user))
		self.assertFalse(frappe.has_permission("Instructor", "write", user=user))

	def test_the_admission_officer_works_its_applicants_without_the_office_role(self):
		"""UAT admission-officer: convert, check and admit applicants, open the Student for Confirm Joining; no office,
		invoice or exam rights (Confirm Joining raises the invoice on the officer's behalf)."""
		user = kit_login("admissions")
		self.assertEqual(KIT_ROLES["admissions"], ["Admission Officer"])
		granted = (
			("Student Applicant", "create"), ("Student Applicant", "write"), ("Student", "read"), ("Program Enrollment", "read"),
			("Seat Matrix", "read"), ("Admission Quota", "read"), ("Program", "read"), ("Student Category", "read"), ("Lead", "write"),
		)  # fmt: skip
		for doctype, ptype in granted:
			self.assertTrue(frappe.has_permission(doctype, ptype, user=user), f"{doctype} {ptype}")
		refused = (
			("Student", "create"), ("Student", "write"), ("Program Enrollment", "create"), ("Sales Invoice", "write"),
			("Payment Entry", "write"), ("Student Result", "read"), ("Course Schedule", "read"),
			("Data Import", "write"), ("Data Import", "create"),
		)  # fmt: skip
		for doctype, ptype in refused:
			self.assertFalse(frappe.has_permission(doctype, ptype, user=user), f"{doctype} {ptype}")
		for doctype in (*OFFICER_READ, *OFFICER_RIGHTS, "Lead", "Data Import"):  # export: the Custom DocPerm default must be cleared
			self.assertEqual(
				frappe.db.get_value("Custom DocPerm", {"parent": doctype, "role": "Admission Officer", "permlevel": 0}, "export"), 0, doctype
			)  # (Desk User carries export on a few DocTypes without read, which opens nothing)

	@mock.patch("campus.campus_platform.pack_visibility.hidden_workspaces", return_value=set())
	def test_the_admission_officer_docks_admissions_and_the_student_list_only(self, _packs):
		user = kit_login("admissions")
		try:
			bootinfo = desk_boot(user)
		finally:
			frappe.set_user("Administrator")
		self.assertEqual(dock_areas(bootinfo), {"Admissions", "Student Services", "Approvals"})
		links = [i["link_to"] for i in bootinfo.module_sidebars["Student Services"]["items"] if i["type"] == "Link"]
		self.assertIn("Student", links)
		self.assertNotIn("Student Attendance", links)

	def test_no_teacher_creates_a_student_or_a_record_correction(self):
		for key in TEACHING_KEYS:
			user = kit_login(key)
			for doctype in ("Student", "Student Record Correction", "Student Applicant"):
				self.assertFalse(frappe.has_permission(doctype, "create", user=user), f"{key}: {doctype}")


class TestFacultyAndHodWithoutTheOfficeRole(FacultyPortalFixture):
	def setUp(self):
		super().setUp()
		drop_office_role(self, TEACHER, COLLEAGUE, HOD)
		self.own = self.period_of("faculty_1")
		self.others = self.period_of("faculty_2")
		# the teacher's own college: the desk form checks the teacher's company User Permission on the student
		self.students = frappe.get_all("Student", {"oc_company": demo.ENGINEERING}, pluck="name", limit=3, order_by="name")
		for period in (self.own, self.others):
			self.enrol(period.student_group, self.students)
		course = frappe.db.get_value("Course Schedule", self.own.name, "course")
		frappe.db.set_value("Course", course, "department", CSE)  # the HOD approving is the course department's
		attendance_corrections.seed_policy()

	def after_cutoff(self):
		later = add_to_date(now_datetime(), hours=attendance_corrections.cutoff_hours() + 1)
		return mock.patch("campus.faculty.attendance_corrections.now_datetime", return_value=later)

	def raise_at_desk(self, user, schedule, student, status="Absent"):
		"""The Faculty > Attendance Correction Request form: insert with the user's own rights, then Request Approval."""

		def raise_request():
			doc = frappe.get_doc({"doctype": CORRECTION, "course_schedule": schedule, "student": student, "new_status": status, "reason": "Marked by mistake"})
			doc.insert()
			request_approval(CORRECTION, doc.name)
			return doc.name

		return as_user(user, raise_request)

	def test_the_demo_teachers_hold_the_kit_role_sets(self):
		self.assertTrue(set(KIT_ROLES["faculty"]) <= set(frappe.get_roles(TEACHER)))
		self.assertTrue(set(KIT_ROLES["hod"]) <= set(frappe.get_roles(HOD)))
		for user in (TEACHER, COLLEAGUE, HOD):
			self.assertNotIn(OFFICE_ROLE, frappe.get_roles(user), user)

	def test_the_teacher_marks_attendance_of_own_period_only(self):
		self.assertEqual(self.mark(TEACHER, self.own.name, [self.students[1]]), {"present": 2, "absent": 1})
		self.assertEqual(self.attendance(self.own.name)[self.students[1]], "Absent")
		with self.assertRaises(frappe.PermissionError):
			self.mark(TEACHER, self.others.name, [])
		self.assertEqual(self.attendance(self.others.name), {})

	def test_the_teacher_raises_a_correction_at_the_desk_and_the_hod_approves_it(self):
		self.mark(TEACHER, self.own.name, [])
		first = self.students[0]
		self.assertTrue(frappe.has_permission("Course Schedule", "select", user=TEACHER))  # the form's period link
		with self.after_cutoff():
			name = self.raise_at_desk(TEACHER, self.own.name, first)
		correction = frappe.get_doc(CORRECTION, name)
		self.assertEqual((correction.owner, correction.department, correction.docstatus), (TEACHER, CSE, 0))
		with self.assertRaises(frappe.PermissionError):
			as_user(TEACHER, approve, latest_request(correction).name)  # the requester cannot approve it
		self.assertFalse(frappe.has_permission(CORRECTION, "read", correction, user=COLLEAGUE))  # own requests only
		self.assertTrue(frappe.has_permission(CORRECTION, "read", correction, user=HOD))
		as_user(HOD, approve, latest_request(correction).name)
		correction.reload()
		self.assertEqual(correction.docstatus, 1)
		self.assertEqual(self.attendance(self.own.name)[first], "Absent")

	def test_a_teacher_cannot_raise_a_correction_for_another_teachers_period(self):
		self.mark(TEACHER, self.own.name, [])
		with self.after_cutoff(), self.assertRaises(frappe.PermissionError):
			self.raise_at_desk(COLLEAGUE, self.own.name, self.students[0])
		self.assertFalse(frappe.db.exists(CORRECTION, {"course_schedule": self.own.name}))

	def test_a_teacher_cannot_create_a_student_or_a_record_correction(self):
		student = {"doctype": "Student", "first_name": "Pilot", "last_name": "Intruder", "student_email_id": "campus-pilot-intruder@example.com"}
		with self.assertRaises(frappe.PermissionError):
			as_user(TEACHER, lambda: frappe.get_doc(student).insert())
		correction = {"doctype": "Student Record Correction", "student": self.students[0], "category": "Personal", "reason": "x"}
		with self.assertRaises(frappe.PermissionError):
			as_user(TEACHER, lambda: frappe.get_doc(correction).insert())
		with self.assertRaises(frappe.PermissionError):
			as_user(TEACHER, corrections.raise_correction, "Contact", "student_mobile_number", "9876543210", "x", student=self.students[0])
		self.assertFalse(frappe.db.exists("Student", {"student_email_id": student["student_email_id"]}))

	def test_the_teacher_sees_own_mentees_in_the_portal(self):
		frappe.db.set_value("Student", self.students[0], "oc_mentor", employee("faculty_1"))
		self.assertEqual([m["student"] for m in as_user(TEACHER, portal.my_mentees)], [self.students[0]])

	def audience_rule(self, name, rule_type="Department", **filters):
		values = {"department": CSE} if rule_type == "Department" and not filters else filters
		return frappe.get_doc(
			{"doctype": "Audience Rule", "rule_name": name, "company": demo.ENGINEERING, "rule_type": rule_type, **values}
		).insert().name  # fmt: skip

	def circular(self, rule, submit=True):
		doc = frappe.get_doc(
			{"doctype": "Circular", "title": "Department meeting", "company": demo.ENGINEERING, "audience_rule": rule,
			"publish_from": now_datetime(), "body": "Friday 3 pm"}
		).insert()  # fmt: skip
		if submit:
			doc.submit()
		return doc.name

	def test_the_hod_writes_and_submits_a_circular_to_the_department(self):
		rule = self.audience_rule("Pilot roles CSE staff")
		self.assertTrue(frappe.has_permission("Audience Rule", "read", rule, user=HOD))
		self.assertEqual(frappe.db.get_value("Circular", as_user(HOD, self.circular, rule), "docstatus"), 1)
		with self.assertRaises(frappe.PermissionError):
			as_user(TEACHER, self.circular, rule)

	def test_the_hod_cannot_edit_or_submit_the_offices_draft(self):
		draft = self.circular(self.audience_rule("Pilot roles office draft"), submit=False)  # written by the office
		for ptype in ("write", "submit"):
			self.assertFalse(frappe.has_permission("Circular", ptype, draft, user=HOD), ptype)

		def edit_and_submit():
			doc = frappe.get_doc("Circular", draft)
			doc.body = "Changed by the HOD"
			doc.submit()

		with self.assertRaises(frappe.PermissionError):
			as_user(HOD, edit_and_submit)
		self.assertEqual(frappe.db.get_value("Circular", draft, ["docstatus", "body"]), (0, "Friday 3 pm"))

	def test_the_hod_cannot_address_the_college_or_another_department(self):
		other = frappe.get_all("Department", {"company": demo.ENGINEERING, "is_group": 0, "name": ["!=", CSE]}, pluck="name", limit=1)[0]
		college_wide = self.audience_rule("Pilot roles all students", "Role", role="Student")
		other_department = self.audience_rule("Pilot roles other department", department=other)
		for rule in (college_wide, other_department):
			with self.assertRaisesRegex(frappe.PermissionError, "your own department"):
				as_user(HOD, self.circular, rule)
			self.assertFalse(frappe.db.exists("Circular", {"audience_rule": rule}), rule)
		self.assertTrue(frappe.db.exists("Circular", self.circular(college_wide, submit=False)))  # the office may

	def test_the_teacher_cannot_submit_a_correction_without_its_approval(self):
		self.mark(TEACHER, self.own.name, [])

		def raise_and_submit():
			doc = frappe.get_doc({"doctype": CORRECTION, "course_schedule": self.own.name, "student": self.students[0], "new_status": "Absent", "reason": "x"})
			doc.insert()
			doc.submit()

		with self.after_cutoff(), self.assertRaisesRegex(frappe.ValidationError, "approval"):
			as_user(TEACHER, raise_and_submit)
		self.assertEqual(self.attendance(self.own.name)[self.students[0]], "Present")

	def test_an_approval_request_is_not_listed_to_a_desk_user_who_is_not_a_party(self):
		self.mark(TEACHER, self.own.name, [])
		with self.after_cutoff():
			name = self.raise_at_desk(TEACHER, self.own.name, self.students[0])
		request = latest_request(frappe.get_doc(CORRECTION, name)).name

		def report_view_names():
			self.addCleanup(setattr, frappe.local, "form_dict", frappe.local.form_dict)
			frappe.local.form_dict = frappe._dict(
				doctype="Approval Request", fields=json.dumps(["`tabApproval Request`.`name`", "`tabApproval Request Approver`.`user`"]),
				filters=json.dumps([]), limit=100, order_by="`tabApproval Request`.`creation` desc",
			)  # fmt: skip
			result = reportview.get()
			return {row[0] for row in result.get("values", [])} if result else set()

		def listed():
			return set(frappe.get_list("Approval Request", pluck="name"))

		self.assertIn(request, as_user(TEACHER, report_view_names))  # the requester: the call does show rows
		for user in (COLLEAGUE, kit_login("coe")):  # their only Approval Request right is Desk User's
			self.assertNotIn(request, as_user(user, listed), user)
			self.assertNotIn(request, as_user(user, report_view_names), user)


class TestClassAdvisorWithoutTheOfficeRole(MentoringFixture):
	def setUp(self):
		super().setUp()
		drop_office_role(self, MENTOR_A)
		frappe.get_doc("User", MENTOR_A).add_roles("Class Advisor")
		frappe.clear_cache(user=MENTOR_A)

	def test_the_advisor_holds_the_kit_role_set(self):
		held = set(frappe.get_roles(MENTOR_A))
		self.assertTrue(set(KIT_ROLES["class_advisor"]) <= held)
		self.assertNotIn(OFFICE_ROLE, held)

	def test_the_advisor_adds_a_mentoring_note_and_lists_the_mentees(self):
		mentees = [m["student"] for m in as_user(MENTOR_A, portal.my_mentees)]
		self.assertIn(self.a_student, mentees)
		self.assertNotIn(self.b_student, mentees)
		note = as_user(MENTOR_A, portal_mentoring.add_note, self.a_student, "Personal", "Settling in", follow_up_date=add_days(today(), 7))
		self.assertEqual([n["name"] for n in as_user(MENTOR_A, portal_mentoring.mentee_record, self.a_student)["notes"]], [note])
		with self.assertRaises(frappe.PermissionError):
			as_user(MENTOR_A, portal_mentoring.mentee_record, self.b_student)

	def condonation(self, student):
		doc = frappe.get_doc(
			{"doctype": "Condonation Request", "student": student, "academic_term": frappe.db.get_value("Academic Term", {}, "name"),
			"company": demo.ENGINEERING, "evidence_kind": "Medical", "evidence": "/private/files/x.pdf", "reason": "Fever"}
		)  # fmt: skip
		doc.name = f"CND-PILOT-{frappe.generate_hash(length=6)}"
		doc.db_insert()  # past the attendance band check: only who reads it matters here
		evidence = frappe.get_doc(
			{"doctype": "File", "file_name": f"{doc.name}.txt", "content": "medical certificate", "is_private": 1,
			"attached_to_doctype": "Condonation Request", "attached_to_name": doc.name}
		).insert()  # fmt: skip
		return doc.name, evidence

	def test_the_advisor_reads_condonation_only_for_the_advised_class(self):
		frappe.db.set_value("Student", self.a_student, "oc_class_advisor", MENTOR_A)
		frappe.db.set_value("Student", self.b_student, "oc_class_advisor", None)
		(own, own_file), (other, other_file) = self.condonation(self.a_student), self.condonation(self.b_student)
		self.assertTrue(frappe.has_permission("Condonation Request", "read", own, user=MENTOR_A))
		self.assertTrue(frappe.has_permission("File", "read", own_file, user=MENTOR_A))
		self.assertFalse(frappe.has_permission("Condonation Request", "read", other, user=MENTOR_A))
		self.assertFalse(frappe.has_permission("File", "read", other_file, user=MENTOR_A))  # the evidence of another class
		listed = as_user(MENTOR_A, frappe.get_list, "Condonation Request", filters={"name": ["in", [own, other]]}, pluck="name")
		self.assertEqual(listed, [own])
		self.assertTrue(frappe.has_permission("Condonation Request", "read", other, user=HOD))  # HOD scope unchanged

	def test_the_advisor_reads_leave_and_condonation_but_creates_no_student(self):
		for doctype in ("Student Leave Application", "Condonation Request", "Parent Interaction"):
			self.assertTrue(frappe.has_permission(doctype, "read", user=MENTOR_A), doctype)
		self.assertEqual(as_user(MENTOR_A, get_report_doc, "Attendance Shortage").name, "Attendance Shortage")
		for doctype in ("Student", "Student Record Correction", "Condonation Request"):
			self.assertFalse(frappe.has_permission(doctype, "create", user=MENTOR_A), doctype)


class TestMalpracticeAsInstructor(MalpracticeFixture):
	def setUp(self):
		super().setUp()
		drop_office_role(self, INVIGILATOR, OTHER_INVIGILATOR)

	def test_the_invigilator_files_a_report_with_instructor_rights_only(self):
		self.assertNotIn(OFFICE_ROLE, frappe.get_roles(INVIGILATOR))
		frappe.set_user(INVIGILATOR)
		try:
			report = self.report(decision="Debarment")
			self.assertFalse(report.committee_decision)  # permlevel 1: the committee's, not the invigilator's
			with self.assertRaisesRegex(frappe.ValidationError, "approval"):
				frappe.get_doc("Malpractice Report", report.name).submit()
		finally:
			frappe.set_user("Administrator")
		self.assertEqual(frappe.db.get_value("Malpractice Report", report.name, "docstatus"), 0)
		self.assertFalse(frappe.has_permission("Malpractice Report", "read", report.name, user=OTHER_INVIGILATOR))
		self.assertNotIn(report.name, as_user(OTHER_INVIGILATOR, frappe.get_list, "Malpractice Report", pluck="name"))
		decided = frappe.get_doc("Malpractice Report", report.name)
		decided.committee_decision = "Debarment"
		with self.assertRaisesRegex(frappe.ValidationError, "Only the COE"):
			as_user(INVIGILATOR, decided.save, ignore_permissions=True)


def custom_perms(doctype):
	fields = ["role", "permlevel", "if_owner", "read", "write", "create", "submit", "report", "export", "print", "select"]
	return {(p.role, p.permlevel, p.if_owner): p for p in frappe.get_all("Custom DocPerm", {"parent": doctype}, fields)}


class TestTeachingStaffRights(IntegrationTestCase):
	def test_a_rerun_keeps_every_row_and_what_the_college_changed(self):
		hod_row = frappe.db.get_value("Custom DocPerm", {"parent": "Student Attendance", "role": "HOD", "permlevel": 0, "if_owner": 0})
		frappe.db.set_value("Custom DocPerm", hod_row, {"print": 1, "report": 0})  # the college adds print, removes report
		before = {doctype: custom_perms(doctype) for doctype in ("Course Schedule", "Student Attendance")}
		grant_teaching_staff_rights()
		grant_teaching_staff_rights()
		for doctype, rows in before.items():
			self.assertEqual(custom_perms(doctype), rows, doctype)
			standard = {(p.role, p.permlevel, p.if_owner) for p in frappe.get_all("DocPerm", {"parent": doctype}, ["role", "permlevel", "if_owner"])}
			self.assertTrue(standard <= set(rows), f"{doctype}: lost {standard - set(rows)}")  # Academics User, Student, Guardian...
		self.assertEqual(frappe.db.get_value("Custom DocPerm", hod_row, ["print", "report"]), (1, 0))
		instructor = before["Course Schedule"][("Instructor", 0, 0)]
		self.assertEqual((instructor.select, instructor.read, instructor.write, instructor.export), (1, 0, 0, 0))

	def test_a_rule_is_added_once_with_exactly_its_rights(self):
		key = {"parent": "Student Attendance", "role": "Class Advisor", "permlevel": 0, "if_owner": 0}
		frappe.db.delete("Custom DocPerm", key)
		granted = set(frappe.parse_json(frappe.db.get_global(GRANTED_RIGHTS_KEY))) - {"Student Attendance:Class Advisor"}
		frappe.db.set_global(GRANTED_RIGHTS_KEY, frappe.as_json(sorted(granted), indent=None))
		grant_teaching_staff_rights()
		row = custom_perms("Student Attendance")[("Class Advisor", 0, 0)]
		self.assertEqual((row.read, row.report, row.export, row.write, row.create), (1, 1, 0, 0, 0))
		self.assertEqual(len(frappe.parse_json(frappe.db.get_global(GRANTED_RIGHTS_KEY))), len(TEACHING_STAFF_RIGHTS))
		frappe.db.delete("Custom DocPerm", key)  # the college takes the rule away
		grant_teaching_staff_rights()
		self.assertFalse(frappe.db.exists("Custom DocPerm", key))
