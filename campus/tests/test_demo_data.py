"""PR-03 tests: the SXCCE demo dataset loads once, loading again creates nothing, the key counts hold, every role user sees data in
its main list, the AISHE export and an AQAR data point have content, and a site that is not a demo site is refused."""

from unittest.mock import patch

import frappe
from frappe.cache_manager import clear_defaults_cache
from frappe.tests import IntegrationTestCase

from campus.iqac import aishe
from campus.iqac.registry import data_point
from campus.setup import people
from campus.setup.demo_data import Context, academics, check_kit, load, names, run_steps, term
from campus.setup.pilot import setup_college
from campus.setup.profile import load_profile

PROFILE = load_profile("sxcce")
COLLEGE = PROFILE["college"]["name"]
# role user key -> (DocType of the main list, filters)
MAIN_LISTS = {
	"admissions": "Student Applicant", "office": "Student", "faculty": "Student Group", "hod": "Student",
	"class_advisor": "Student", "coe": "Student Result", "accounts": "Sales Invoice", "principal": "Student",
	"iqac": "Accreditation Cycle", "student": "Student Result", "guardian": "Student",
}  # fmt: skip


class TestDemoData(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		cls.addClassCleanup(
			clear_defaults_cache
		)  # runs after the rollback: drops the kit's default company from the cache
		super().setUpClass()
		setup_college("sxcce", test_passwords=True)
		cls.first = load("sxcce", 2026)
		cls.points_first = data_point("total_students", COLLEGE, "2025-26")["value"]
		cls.second = load("sxcce", 2026)

	def count(self, doctype, **filters):
		return frappe.db.count(doctype, filters)

	def test_loading_again_creates_and_updates_nothing(self):
		self.assertEqual(self.second["created"], {})
		self.assertEqual(self.second["updated"], {})

	def test_key_counts(self):
		self.assertEqual(self.count("Student Applicant", oc_company=COLLEGE), 60)
		self.assertEqual(self.count("Lead", company=COLLEGE), 18)
		self.assertEqual(self.count("Applicant Upload", company=COLLEGE), 1)
		self.assertEqual(
			self.count("Student Applicant", oc_company=COLLEGE, application_status="Admitted"), 48
		)
		self.assertEqual(
			self.count("Student Applicant", oc_company=COLLEGE, application_status="Approved"), 6
		)
		self.assertEqual(self.count("Student", oc_company=COLLEGE, oc_batch="2025-26"), 120)
		self.assertEqual(
			self.count("Student", oc_company=COLLEGE, oc_batch="2026-27", oc_academic_status="Active"), 42
		)
		self.assertEqual(self.count("Student", oc_company=COLLEGE, oc_academic_status="Admitted"), 6)
		self.assertEqual(self.count("Instructor", oc_company=COLLEGE), 18)
		self.assertEqual(self.count("Curriculum", company=COLLEGE, status="Effective"), 6)
		self.assertEqual(self.count("Program Outcome", company=COLLEGE), 72)
		self.assertEqual(self.count("Program Specific Outcome", company=COLLEGE), 12)
		self.assertEqual(self.count("Course Outcome", company=COLLEGE), 324)
		self.assertEqual(self.count("Exam Session", company=COLLEGE, status="Published"), 2)
		self.assertEqual(self.count("Student Result", company=COLLEGE, is_latest=1), 240)

	def test_fees_are_paid_in_part_and_outstanding_in_part(self):
		invoices = frappe.get_all(
			"Sales Invoice", {"company": COLLEGE, "docstatus": 1}, ["grand_total", "outstanding_amount"]
		)
		self.assertEqual(len(invoices), 42)
		self.assertTrue(any(i.outstanding_amount == 0 for i in invoices))
		self.assertTrue(any(i.outstanding_amount == i.grand_total for i in invoices))
		self.assertTrue(any(0 < i.outstanding_amount < i.grand_total for i in invoices))

	def test_the_first_year_has_cgpa_and_arrears(self):
		results = frappe.get_all(
			"Student Result", {"company": COLLEGE, "is_latest": 1}, ["cgpa", "arrear_count"]
		)
		self.assertGreater(sum(1 for r in results if r.cgpa), len(results) * 0.9)
		self.assertTrue(any(r.arrear_count for r in results))
		self.assertTrue(any(not r.arrear_count for r in results))

	def test_the_current_term_has_attendance_leave_and_internal_marks(self):
		self.assertGreater(self.count("Student Attendance", status="Leave"), 0)
		kinds = set(frappe.get_all("Student Leave Application", {"docstatus": 1}, pluck="oc_leave_kind"))
		self.assertTrue({"Medical", "On Duty"} <= kinds)
		self.assertEqual(self.count("Mark Sheet Entry", company=COLLEGE), 108)

	def test_every_role_user_sees_data_in_its_main_list(self):
		domain = PROFILE["user_domain"]
		self.addCleanup(frappe.set_user, "Administrator")
		for key, doctype in MAIN_LISTS.items():
			frappe.set_user(f"{key}@{domain}")
			self.assertTrue(frappe.get_list(doctype, limit=1), f"{key} sees no {doctype}")

	def test_services_exist(self):
		self.assertEqual(self.count("Counselling Note", is_confidential=0), 1)
		self.assertEqual(self.count("Parent Interaction"), 1)
		self.assertEqual(self.count("Placement Drive", company=COLLEGE), 1)
		self.assertTrue(self.count("Drive Registration", company=COLLEGE) > 0)
		self.assertEqual(self.count("Degree Awarding Authority", company=COLLEGE), 6)
		self.assertEqual(self.count("Official Document", company=COLLEGE), 6)
		self.assertEqual(self.count("Scholarship Application"), 1)

	def test_aishe_export_and_an_aqar_data_point_have_content(self):
		sections = aishe.build(COLLEGE, "2025-26")
		for name in ("Programmes", "Enrolment", "Faculty"):
			self.assertTrue(sections[name]["rows"], name)
		self.assertGreater(data_point("total_students", COLLEGE, "2025-26")["value"], 0)

	def test_loading_again_leaves_the_accreditation_figures_unchanged(self):
		self.assertEqual(data_point("total_students", COLLEGE, "2025-26")["value"], self.points_first)

	def test_personal_data_is_synthetic(self):
		domain = PROFILE["user_domain"]
		self.assertFalse(
			frappe.db.count(
				"Student", {"oc_company": COLLEGE, "student_email_id": ["not like", f"%@{domain}"]}
			)
		)
		self.assertFalse(
			frappe.db.count("Student", {"oc_company": COLLEGE, "student_mobile_number": ["not like", "0%"]})
		)
		self.assertFalse(
			frappe.db.count("Student", {"oc_company": COLLEGE, "aadhaar_number": ["is", "set"]})
			if frappe.db.has_column("Student", "aadhaar_number")
			else 0
		)


class TestDemoGuard(IntegrationTestCase):
	def test_a_site_that_is_not_a_demo_site_is_refused(self):
		with patch.object(people, "test_passwords_allowed", return_value=False):
			with patch("campus.setup.demo_data.pilot_people.test_passwords_allowed", return_value=False):
				with self.assertRaisesRegex(frappe.ValidationError, "developer-mode or demo sites only"):
					load("sxcce", 2026)


class TestDemoShape(IntegrationTestCase):
	"""Pieces of the loader that do not need the 10-minute load: run on the loaded site inside the test transaction."""

	@classmethod
	def setUpClass(cls):
		cls.addClassCleanup(
			clear_defaults_cache
		)  # runs after the rollback: drops the kit's default company from the cache
		super().setUpClass()
		setup_college(
			"sxcce", test_passwords=True
		)  # idempotent and rolled back with the class: the kit is always there

	# TestDemoData rolls its 10-minute load back, so the tests that need the loaded people skip unless it was committed (bench execute)

	def require_loaded_demo(self):
		# a few Students from other tests can be committed on a site without the demo: the demo is loaded when its taught groups exist
		if not frappe.db.exists("Student Group Instructor", {"parenttype": "Student Group"}):
			self.skipTest("the SXCCE demo is not loaded on this site (campus.setup.demo_data.load)")

	def test_production_code_does_not_import_the_tests_package(self):
		import pathlib

		import campus.setup.demo_data as package

		for path in pathlib.Path(package.__file__).parent.glob("*.py"):
			self.assertNotIn("campus.tests", path.read_text(), path.name)

	def test_a_person_is_the_same_person_every_time_it_is_asked_for(self):
		people = names.People(2026, "x.example.edu")
		first = people.make("student-1", 2007, 2006)
		self.assertIs(people.make("student-1", 2007, 2006), first)
		self.assertEqual(people.make("student-2", 2007, 2006).email, "student-2@x.example.edu")

	def test_the_session_dates_are_the_last_three_weeks_before_the_load_date(self):
		term_name = academics.term_of(term.YEAR, term.TERM_NAME)
		with patch.object(term, "today", return_value="2026-10-22"):
			dates = term.session_dates(term_name)
		self.assertEqual(len(dates), 9)
		self.assertEqual({d.weekday() for d in dates}, {0, 2, 4})
		self.assertEqual(str(dates[-1]), "2026-10-21")
		self.assertEqual(str(dates[0]), "2026-10-02")

	def test_the_session_dates_stay_inside_the_term(self):
		term_name = academics.term_of(term.YEAR, term.TERM_NAME)
		with patch.object(term, "today", return_value="2027-02-01"):
			self.assertEqual(str(term.session_dates(term_name)[-1]), "2026-11-30")

	def test_the_guard_also_stops_run_steps_and_keeps_the_user(self):
		ctx = Context(PROFILE, 2026)
		with patch("campus.setup.demo_data.pilot_people.test_passwords_allowed", return_value=False):
			with self.assertRaisesRegex(frappe.ValidationError, "developer-mode or demo sites only"):
				run_steps(ctx, ["staff"])

	def test_run_steps_restores_the_session_user(self):
		ctx = Context(PROFILE, 2026)
		self.addCleanup(frappe.set_user, "Administrator")
		frappe.set_user("guest@example.com")
		run_steps(ctx, [])
		self.assertEqual(frappe.session.user, "guest@example.com")

	def test_the_kit_is_checked_before_anything_is_written(self):
		ctx = Context(PROFILE, 2026)
		with patch.object(frappe.db, "get_single_value", return_value=None):
			with self.assertRaisesRegex(frappe.ValidationError, "selling price list"):
				check_kit(ctx)

	def test_the_kit_check_names_a_missing_default_company_and_accepts_either_store(self):
		ctx = Context(PROFILE, 2026)
		self.addCleanup(clear_defaults_cache)
		frappe.db.set_single_value("Global Defaults", "default_company", None)
		frappe.db.delete("DefaultValue", {"parent": "__default", "defkey": "company"})
		clear_defaults_cache()
		with self.assertRaisesRegex(frappe.ValidationError, "default company"):
			check_kit(ctx)
		frappe.db.set_single_value("Global Defaults", "default_company", COLLEGE)
		try:
			check_kit(ctx)
		except frappe.ValidationError as error:
			self.assertNotIn("default company", str(error))

	def test_the_curriculum_step_seeds_outcomes_once_and_fills_a_matrix_grid(self):
		"""The curriculum step seeds 12 NBA POs, 2 PSOs per programme, and COs for the demo courses idempotently."""
		programmes = [p["name"] for p in PROFILE["programmes"]]
		curricula = frappe.get_all("Curriculum", {"company": COLLEGE}, pluck="name")
		for matrix_name in frappe.get_all("CO-PO Matrix", {"company": COLLEGE}, pluck="name"):
			frappe.delete_doc("CO-PO Matrix", matrix_name, force=True)
		if curricula:
			frappe.db.delete("Course Outcome", {"curriculum": ["in", curricula]})
		frappe.db.delete("Program Specific Outcome", {"program": ["in", programmes]})
		frappe.db.delete("Program Outcome", {"program": ["in", programmes]})
		first_ctx = Context(PROFILE, 2026)
		run_steps(first_ctx, ["curriculum"])
		self.assertEqual(first_ctx.out.as_dict()["created"]["Program Outcome"], 72)
		self.assertEqual(first_ctx.out.as_dict()["created"]["Program Specific Outcome"], 12)
		self.assertEqual(first_ctx.out.as_dict()["created"]["Course Outcome"], 324)
		second_ctx = Context(PROFILE, 2026)
		run_steps(second_ctx, ["curriculum"])
		self.assertEqual(second_ctx.out.as_dict()["created"], {})
		self.assertEqual(second_ctx.out.as_dict()["updated"], {})
		curriculum = frappe.db.get_value(
			"Curriculum", {"company": COLLEGE, "program": "B.E. CSE", "status": "Effective"}
		)
		matrix = frappe.get_doc(
			{"doctype": "CO-PO Matrix", "course": "Discrete Mathematics", "curriculum": curriculum}
		).insert()
		self.assertEqual(len(matrix.correlations), 42)
		matrix.correlations[0].correlation = 3
		matrix.save()
		matrix.reload()
		self.assertEqual(matrix.correlations[0].correlation, 3)

	def test_attendance_and_leave_go_through_the_real_documents(self):
		self.require_loaded_demo()
		ctx = Context(PROFILE, 2026)
		cse = next(p for p in PROFILE["programmes"] if p["abbr"] == "CSE")
		students = ctx.cohort.students(ctx, cse)
		term_name = academics.term_of(term.YEAR, term.TERM_NAME)
		taught = frappe.get_all("Student Group Instructor", {"parenttype": "Student Group"}, pluck="parent")
		source = frappe.get_doc(
			"Student Group",
			frappe.db.get_value(
				"Student Group", {"name": ["in", taught], "academic_term": term_name, "program": cse["name"]}
			),
		)
		offerings = {}
		for number in (1, 2):
			group = frappe.copy_doc(source)
			group.student_group_name = f"TEST attendance group {number}"
			offerings[f"T{number}"] = group.insert(ignore_permissions=True).name
		with patch.object(term, "today", return_value="2026-10-22"):
			term.make_timetable_and_attendance(ctx, cse, term_name, offerings, students)
		medical = frappe.get_doc(
			"Student Leave Application",
			{"student": students[2], "oc_leave_kind": "Medical", "from_date": ["like", "2026-10%"]},
		)
		self.assertEqual(medical.docstatus, 1)
		self.assertTrue(medical.oc_certificate)
		on_leave = frappe.get_all(
			"Student Attendance",
			{"student": students[2], "leave_application": medical.name, "docstatus": 1},
			pluck="status",
		)
		self.assertTrue(on_leave)
		self.assertEqual(set(on_leave), {"Leave"})
		self.assertEqual(
			frappe.db.count(
				"Student Attendance",
				{"student": students[0], "student_group": ["in", list(offerings.values())], "docstatus": 1},
			),
			18,
		)
