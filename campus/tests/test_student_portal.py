"""P1-T26 integration test: the student portal's data methods and pages resolve the student from the session user."""

import frappe
from frappe.utils import add_days, now_datetime, today

from campus.campus_platform.verification import verify  # noqa: F401  (keeps the QR hooks loaded as in production)
from campus.student_services import portal
from campus.student_services.api import attendance_summary
from campus.tests.broadcast_fixtures import make_guardian
from campus.tests.factories import make_company
from campus.tests.test_attendance_eligibility import RULES
from campus.tests.test_results import COLLEGE, FACULTY, GUARDIAN_USER, STUDENT_22_USER, STUDENT_26_USER, ExamFixture, make_login
from campus.www.campus_portal import circulars as circulars_page, fees as fees_page, index as index_page

TRUST = "Portal Test Trust"
BOTH_GUARDIAN_USER = "portal-both-guardian@example.com"
NO_LINK_USER = "portal-nolink@example.com"


def make_invoice(student, company, amount, outstanding, name):
	"""A submitted invoice row; the portal only reads it, so no ledger postings are needed."""
	doc = frappe.new_doc("Sales Invoice")
	doc.update(
		{
			"name": name, "company": company, "student": student.name, "customer": student.customer,
			"docstatus": 1, "posting_date": today(), "due_date": add_days(today(), 10), "currency": "INR",
			"grand_total": amount, "outstanding_amount": outstanding, "status": "Unpaid",
		}
	)  # fmt: skip
	doc.db_insert()


class PortalFixture(ExamFixture):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		make_company(TRUST, "PTT")
		frappe.db.delete("Attendance Rule", {"regulation": ["like", "%-EXM"]})
		for regulation, rule in (("R2022-EXM", RULES["R2022"]), ("R2026-EXM", RULES["R2026"])):
			frappe.get_doc(
				{"doctype": "Attendance Rule", "regulation": frappe.db.get_value("Regulation", {"name": ["like", f"{regulation}%"]}), **rule}
			).insert()
		make_login(BOTH_GUARDIAN_USER, "Guardian")
		make_login(NO_LINK_USER, "Student")
		make_guardian("Portalboth", [cls.s22.name, cls.s26.name], user=BOTH_GUARDIAN_USER)
		for invoice in ("PORTAL-INV-1", "PORTAL-INV-2", "PORTAL-INV-3"):
			frappe.db.delete("Sales Invoice", invoice)
		make_invoice(cls.s22, COLLEGE, 1000, 400, "PORTAL-INV-1")
		make_invoice(cls.s22, TRUST, 500, 500, "PORTAL-INV-2")
		make_invoice(cls.s26, COLLEGE, 7777, 7777, "PORTAL-INV-3")

	def as_user(self, user):
		frappe.set_user(user)

	def publish_session(self):
		self.run_session()
		self.publish(add_days(now_datetime(), -1))


class TestPortalOwnership(PortalFixture):
	def test_students_resolve_to_themselves_and_guardians_to_linked_students(self):
		self.assertEqual([s.name for s in portal.linked_students(STUDENT_22_USER)], [self.s22.name])
		self.assertEqual([s.name for s in portal.linked_students(GUARDIAN_USER)], [self.s22.name])
		self.assertEqual(
			sorted(s.name for s in portal.linked_students(BOTH_GUARDIAN_USER)), sorted([self.s22.name, self.s26.name])
		)
		self.assertEqual(portal.linked_students(NO_LINK_USER), [])

	def test_fees_are_own_only_and_total_per_company(self):
		self.as_user(STUDENT_22_USER)
		fees = portal.get_fees()
		self.assertEqual(sorted(i.name for i in fees.invoices), ["PORTAL-INV-1", "PORTAL-INV-2"])
		self.assertEqual(
			{r["company"]: r["outstanding"] for r in fees.outstanding_by_company}, {COLLEGE: 400, TRUST: 500}
		)
		self.assertEqual(fees.total_outstanding, 900)
		self.assertEqual(portal.get_fees(self.s22.name).student, self.s22.name)
		with self.assertRaises(frappe.PermissionError):
			portal.get_fees(self.s26.name)
		self.as_user(STUDENT_26_USER)
		self.assertEqual([i.name for i in portal.get_fees().invoices], ["PORTAL-INV-3"])
		with self.assertRaises(frappe.PermissionError):
			portal.get_fees(self.s22.name)

	def test_the_student_role_reads_and_prints_its_invoices_but_cannot_write_or_export_them(self):
		"""Step 14: Education's Student rule on Sales Invoice is narrowed to read and print (the portal receipt)."""
		for ptype, expected in (("read", True), ("print", True), ("write", False), ("export", False), ("email", False), ("share", False)):
			self.assertEqual(
				frappe.has_permission("Sales Invoice", ptype, "PORTAL-INV-1", user=STUDENT_22_USER), expected, ptype
			)
		self.assertFalse(frappe.has_permission("Sales Invoice", "read", "PORTAL-INV-3", user=STUDENT_22_USER))  # a classmate's
		self.as_user(STUDENT_22_USER)
		self.assertEqual(sorted(frappe.get_list("Sales Invoice", pluck="name")), ["PORTAL-INV-1", "PORTAL-INV-2"])

	def test_attendance_is_own_only_and_works_without_student_read_permission(self):
		self.as_user(STUDENT_22_USER)
		data = portal.get_attendance(academic_term=self.term)
		self.assertEqual(data["student"], self.s22.name)
		self.assertEqual(len(data["summary"]["offerings"]), 4)
		self.assertEqual(data["summary"]["status"], "Eligible")  # nothing conducted yet
		self.assertEqual(data["terms"], [self.term, self.pre_term])
		with self.assertRaises(frappe.PermissionError):
			portal.get_attendance(self.s26.name, self.term)
		with self.assertRaises(frappe.PermissionError):
			portal.get_attendance(self.s22.name, "Some Other Term")  # a term the student never studied in
		self.as_user(STUDENT_26_USER)
		self.assertEqual(portal.get_attendance()["student"], self.s26.name)
		with self.assertRaises(frappe.PermissionError):
			portal.get_attendance(self.s22.name)

	def test_results_stay_hidden_until_published_then_own_only(self):
		self.run_session()
		self.as_user(STUDENT_22_USER)
		self.assertEqual(portal.get_results()["results"], [])  # computed but not published
		self.as_user("Administrator")
		self.publish(add_days(now_datetime(), -1))
		self.as_user(STUDENT_22_USER)
		results = portal.get_results()["results"]
		self.assertEqual([r["name"] for r in results], [self.result(self.s22).name])  # the earlier session is unpublished
		self.assertEqual({c["course"]: c["grade"] for c in results[0]["courses"]}["EXM TH"], "B+")
		self.assertIn("Grade%20Sheet", results[0]["grade_sheet_url"])
		with self.assertRaises(frappe.PermissionError):
			portal.get_results(self.s26.name)
		self.assertFalse(frappe.has_permission("Student Result", "read", self.result(self.s26), user=STUDENT_22_USER))
		self.as_user(STUDENT_26_USER)
		self.assertEqual([r["name"] for r in portal.get_results()["results"]], [self.result(self.s26).name])
		with self.assertRaises(frappe.PermissionError):
			portal.get_results(self.s22.name)

	def test_guardian_sees_only_linked_students(self):
		self.publish_session()
		self.as_user(GUARDIAN_USER)
		self.assertEqual(portal.get_fees()["student"], self.s22.name)
		self.assertEqual(portal.get_results(self.s22.name)["student"], self.s22.name)
		for method in (portal.get_fees, portal.get_results, portal.get_attendance):
			with self.assertRaises(frappe.PermissionError):
				method(self.s26.name)
		self.as_user(BOTH_GUARDIAN_USER)
		self.assertEqual(portal.get_fees(self.s26.name).total_outstanding, 7777)
		self.assertEqual(portal.get_fees(self.s22.name).total_outstanding, 900)
		self.assertEqual(len(portal.my_students()), 2)

	def test_student_role_reads_only_linked_students_everywhere(self):
		for user, visible in (
			(STUDENT_22_USER, [self.s22.name]),
			(STUDENT_26_USER, [self.s26.name]),
			(GUARDIAN_USER, [self.s22.name]),
			(BOTH_GUARDIAN_USER, sorted([self.s22.name, self.s26.name])),
			(NO_LINK_USER, []),
		):
			self.as_user(user)
			mine = {self.s22.name, self.s26.name}
			self.assertEqual(sorted(frappe.get_list("Student", {"name": ["in", list(mine)]}, pluck="name")), visible, user)
			for student in mine - set(visible):
				self.assertFalse(frappe.has_permission("Student", "read", student, user=user), (user, student))
				with self.assertRaises(frappe.PermissionError):
					frappe.get_doc("Student", student).check_permission("read")
		self.as_user(STUDENT_22_USER)
		self.assertEqual(frappe.get_doc("Student", self.s22.name).name, self.s22.name)
		self.assertEqual(len(portal.get_fees().invoices), 2)  # the portal keeps working

	def test_staff_still_read_every_student(self):
		for user in ("Administrator", FACULTY):
			self.as_user(user)
			names = frappe.get_list("Student", {"name": ["in", [self.s22.name, self.s26.name]]}, pluck="name")
			self.assertEqual(len(names), 2, user)

	def test_attendance_summary_is_not_reachable_over_http(self):
		self.assertNotIn(attendance_summary, frappe.whitelisted)
		self.as_user(STUDENT_22_USER)
		with self.assertRaises(frappe.PermissionError):
			frappe.is_whitelisted(attendance_summary)
		with self.assertRaises(frappe.PermissionError):
			attendance_summary(self.s26.name, self.term)  # even called directly, a classmate is refused

	def test_user_without_a_linked_student_is_refused_and_guest_is_refused(self):
		self.as_user(NO_LINK_USER)
		for method in (portal.get_fees, portal.get_results, portal.get_attendance):
			with self.assertRaises(frappe.PermissionError):
				method()
		self.as_user("Guest")
		for method in (portal.get_fees, portal.get_results, portal.get_attendance, portal.my_students):
			with self.assertRaises(frappe.PermissionError):
				method()
		with self.assertRaises(frappe.PermissionError):
			portal.get_fees(self.s22.name)


class TestPortalPages(PortalFixture):
	def context(self, page, user, **form):
		frappe.set_user(user)
		frappe.form_dict.clear()
		frappe.form_dict.update(form)
		self.addCleanup(frappe.form_dict.clear)
		context = frappe._dict()
		page.get_context(context)
		return context

	def test_guests_are_redirected_to_login(self):
		for page in (index_page, fees_page, circulars_page):
			with self.assertRaises(frappe.Redirect):
				self.context(page, "Guest")
			self.assertEqual(frappe.local.flags.redirect_location, "/login?redirect-to=/campus-portal")

	def test_dashboard_and_fees_pages_show_the_session_users_student(self):
		context = self.context(index_page, STUDENT_22_USER)
		self.assertEqual(context.student, self.s22.name)
		self.assertEqual(context.fees.total_outstanding, 900)
		self.assertIsNone(context.latest_result)
		self.assertEqual(context.pending_circulars, 0)
		self.assertEqual(self.context(fees_page, STUDENT_26_USER).fees.total_outstanding, 7777)

	def test_student_param_must_be_a_linked_student(self):
		with self.assertRaises(frappe.PermissionError):
			self.context(fees_page, STUDENT_22_USER, student=self.s26.name)
		context = self.context(fees_page, BOTH_GUARDIAN_USER, student=self.s26.name)
		self.assertEqual((context.student, len(context.students)), (self.s26.name, 2))

	def test_unlinked_user_gets_an_empty_page(self):
		context = self.context(fees_page, NO_LINK_USER)
		self.assertIsNone(context.student)
		self.assertNotIn("fees", context)

	def test_circulars_page_lists_the_feed(self):
		self.assertEqual(self.context(circulars_page, STUDENT_22_USER).circulars, [])

	def test_students_and_guardians_land_on_the_portal(self):
		self.assertEqual(frappe.get_hooks("get_website_user_home_page")[-1], "campus.student_services.portal.home_page")
		self.assertEqual(portal.home_page(STUDENT_22_USER), "campus-portal")
		self.assertEqual(portal.home_page(GUARDIAN_USER), "campus-portal")

	def test_desk_users_holding_portal_roles_stay_on_desk(self):
		# Administrator holds every role, Student included; a role-keyed home page sent it to the student portal
		self.assertEqual(portal.home_page("Administrator"), "desk")
