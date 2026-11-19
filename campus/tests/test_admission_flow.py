"""P1-T16 tests (M04-01, M04-04, M04-09): Admit (approval) and joining confirmation, register numbers, cancellation
refund, prints and reports."""

import threading
from types import SimpleNamespace
from unittest.mock import patch

import frappe
from frappe.utils import add_days, flt, getdate, today

from campus.admissions import admit as admit_module
from campus.admissions import joining as joining_module
from campus.admissions import numbers, refund
from campus.admissions.api import admit, check_seat_available, confirm_joining
from campus.admissions.report.admissions_by_quota_and_category import (
	admissions_by_quota_and_category as quota_report,
)
from campus.admissions.report.refund_register import refund_register
from campus.student_services import onboarding
from campus.tests.approval_fixtures import approval_not_required, drop_policies
from campus.tests.factories import make_company, make_user
from campus.tests.finance_factories import make_account, make_fee_structure, make_finance_company
from campus.tests.test_admissions import VALID, delete_log_rows, log_rows, own_connection
from campus.tests.utils import CampusTestCase

COLLEGE, ABBR = "Admit Flow College", "AFC"
PROGRAM, CODE = "Admit Flow BE CSE", "CS"
YEAR = "Admit Flow 2026"
QUOTA, FREE_QUOTA = "AFGQ", "AFMQ"
OC, BC = "AF OC", "AF BC"
OFFICER, PLAIN = "admit-officer@example.com", "admit-plain@example.com"
OTHER_COLLEGE, OTHER_OFFICER = "Admit Flow Other College", "admit-other@example.com"
OVERRIDE_OFFICER = "admit-override@example.com"
PURE_OFFICER = "admit-pure@example.com"  # the pilot kit officer: Admission Officer and no other role
TUITION_FEE, EXAM_FEE, BC_TUITION = 50000, 2000, 30000
SERIAL = [0]


def slab(from_day, to_day, percent=0, fee=0):
	return SimpleNamespace(
		days_from_admission_start=from_day,
		days_to_admission_start=to_day,
		deduction_percent=percent,
		processing_fee=fee,
	)


class TestRefundCalculation(CampusTestCase):
	"""The pure refund functions: every branch."""

	slabs = [slab(0, 15, fee=1000), slab(16, 30, percent=20), slab(31, 60, percent=50, fee=500)]

	def test_days_since_start_never_negative(self):
		self.assertEqual(refund.days_since_start("2026-07-01", "2026-07-11"), 10)
		self.assertEqual(refund.days_since_start("2026-07-01", "2026-07-01"), 0)
		self.assertEqual(refund.days_since_start("2026-07-01", "2026-06-20"), 0)

	def test_full_refund_less_processing_fee_before_the_cutoff(self):
		result = refund.refund_for(self.slabs, 15, 52000)
		self.assertEqual((result["deduction"], result["refundable"]), (1000, 51000))

	def test_percent_deduction_after_the_cutoff(self):
		result = refund.refund_for(self.slabs, 16, 52000)
		self.assertEqual((result["deduction"], result["refundable"]), (10400, 41600))

	def test_percent_and_fee_add_up(self):
		result = refund.refund_for(self.slabs, 31, 10000)
		self.assertEqual((result["deduction"], result["refundable"]), (5500, 4500))

	def test_deduction_is_capped_at_the_amount(self):
		result = refund.refund_for([slab(0, 5, fee=1000)], 3, 400)
		self.assertEqual((result["deduction"], result["refundable"]), (400, 0))

	def test_no_slab_after_the_last_one_means_no_refund(self):
		result = refund.refund_for(self.slabs, 61, 52000)
		self.assertIsNone(result["slab"])
		self.assertEqual((result["deduction"], result["refundable"]), (52000, 0))

	def test_slab_boundaries_are_inclusive(self):
		self.assertEqual(refund.slab_for(self.slabs, 0).days_to_admission_start, 15)
		self.assertEqual(refund.slab_for(self.slabs, 30).days_from_admission_start, 16)
		self.assertEqual(refund.slab_for(self.slabs, 60).days_to_admission_start, 60)

	def test_pattern_validation(self):
		numbers.validate_pattern("{abbr}{yy}{program_code}{####}")
		for bad in ("{abbr}", "{abbr}{####}x", "{nope}{####}", "{#}", ""):
			with self.assertRaises(frappe.ValidationError):
				numbers.validate_pattern(bad)


class AdmitTestCase(CampusTestCase):
	"""One college with fee structures, a refund rule, an officer and a plain user; each test runs in a savepoint."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.original_skip = frappe.db.get_single_value("Education Settings", "user_creation_skip")
		frappe.db.set_single_value("Education Settings", "user_creation_skip", 1)
		cls.accounts = make_finance_company(COLLEGE, ABBR)
		frappe.db.set_value("Company", COLLEGE, "default_bank_account", cls.accounts["bank"])
		cls.make_year_program()
		cls.make_masters()
		cls.structure = make_fee_structure(
			COLLEGE, PROGRAM, YEAR, {"Tuition": TUITION_FEE, "Exam": EXAM_FEE}, cls.accounts
		)
		cls.bc_structure = make_fee_structure(
			COLLEGE,
			PROGRAM,
			YEAR,
			{"Tuition": BC_TUITION, "Exam": EXAM_FEE},
			cls.accounts,
			student_category=BC,
		)
		make_company(OTHER_COLLEGE, "AFO")
		make_user(OFFICER, [COLLEGE])
		make_user(PLAIN, [COLLEGE])
		make_user(OTHER_OFFICER, [OTHER_COLLEGE])
		make_user(OVERRIDE_OFFICER, [COLLEGE])
		frappe.get_doc("User", OFFICER).add_roles("Admission Officer")
		frappe.get_doc("User", OVERRIDE_OFFICER).add_roles("Admission Officer", "Admission Override")
		frappe.get_doc("User", OTHER_OFFICER).add_roles("Admission Officer")

	@classmethod
	def tearDownClass(cls):
		frappe.db.set_single_value("Education Settings", "user_creation_skip", cls.original_skip)
		super().tearDownClass()

	@classmethod
	def make_year_program(cls):
		if not frappe.db.exists("Academic Year", YEAR):
			frappe.get_doc(
				{
					"doctype": "Academic Year",
					"academic_year_name": YEAR,
					"year_start_date": today(),
					"year_end_date": add_days(today(), 330),
				}
			).insert()
		if not frappe.db.exists("Program", PROGRAM):
			frappe.get_doc(
				{
					"doctype": "Program",
					"program_name": PROGRAM,
					"program_abbreviation": CODE,
					"oc_company": COLLEGE,
				}
			).insert()

	@classmethod
	def make_masters(cls):
		for category in (OC, BC):
			if not frappe.db.exists("Student Category", category):
				frappe.get_doc({"doctype": "Student Category", "category": category}).insert()
		if not frappe.db.exists("Gender", "Male"):
			frappe.get_doc({"doctype": "Gender", "gender": "Male"}).insert()
		for code in (QUOTA, FREE_QUOTA):
			if not frappe.db.exists("Admission Quota", code):
				frappe.get_doc({"doctype": "Admission Quota", "code": code, "title": code}).insert()

	def setUp(self):
		frappe.set_user("Administrator")
		frappe.db.savepoint("admit_test")
		self.addCleanup(frappe.db.rollback, save_point="admit_test")
		self.addCleanup(frappe.set_user, "Administrator")
		self.touched = []
		self.addCleanup(lambda: delete_log_rows(self.touched))
		was_importing, frappe.flags.in_import = (
			frappe.flags.in_import,
			True,
		)  # skip the user-creation throttle
		self.addCleanup(setattr, frappe.flags, "in_import", was_importing)
		frappe.db.delete("Seat Matrix")
		frappe.db.delete("Refund Rule")
		# most admit tests have no Seat Matrix: they run non-strict (M04-08 strict mode is tested in TestStrictAdmit)
		self.settings(
			register_no_pattern="{abbr}{yy}{program_code}{####}", roll_no_pattern="", strict_seat_matrix=0
		)
		self.rule(slabs=[(0, 15, 0, 1000), (16, 30, 20, 0)])

	def settings(self, **values):
		doc = frappe.get_doc("Admission Settings")
		doc.update({"active_admission_year": YEAR, **values})
		doc.save()

	def rule(self, slabs, year=None):
		frappe.get_doc(
			{
				"doctype": "Refund Rule",
				"company": COLLEGE,
				"admission_year": year,
				"slabs": [
					{
						"days_from_admission_start": a,
						"days_to_admission_start": b,
						"deduction_percent": p,
						"processing_fee": f,
					}
					for a, b, p, f in slabs
				],
			}
		).insert()

	def matrix(self, rows):
		return frappe.get_doc(
			{
				"doctype": "Seat Matrix",
				"company": COLLEGE,
				"program": PROGRAM,
				"academic_year": YEAR,
				"rows": [{"quota": q, "category": c, "sanctioned": n} for q, c, n in rows],
			}
		).insert()

	def applicant(self, category=OC, quota=QUOTA, status="Approved", eligibility="Eligible", aadhaar=None):
		SERIAL[0] += 1
		doc = frappe.get_doc(
			{
				"doctype": "Student Applicant",
				"first_name": f"Flow{SERIAL[0]}",
				"program": PROGRAM,
				"academic_year": YEAR,
				"student_email_id": f"flow{SERIAL[0]}-{frappe.generate_hash(length=6)}@flow.example.com",
				"oc_company": COLLEGE,
				"student_category": category,
				"oc_admission_quota": quota,
				"application_status": status,
				"oc_aadhaar": aadhaar,
				"gender": "Male",
				"state": "Tamil Nadu",
			}
		).insert()
		frappe.db.set_value("Student Applicant", doc.name, "oc_eligibility_status", eligibility)
		if aadhaar:
			self.touched.append(doc.name)
		return doc.name

	def admit_as_officer(self, applicant, officer=OFFICER):
		frappe.set_user(officer)
		try:
			return admit(applicant)
		finally:
			frappe.set_user("Administrator")

	def join_as_officer(self, applicant, officer=OFFICER, **kwargs):
		frappe.set_user(officer)
		try:
			return confirm_joining(applicant, **kwargs)
		finally:
			frappe.set_user("Administrator")

	def admit_and_join(self, applicant, officer=OFFICER):
		"""Both steps (approval, then joining today); returns the joining's result."""
		self.admit_as_officer(applicant, officer)
		return self.join_as_officer(applicant, officer)

	def counts(self):
		return {
			dt: frappe.db.count(dt) for dt in ("Student", "Program Enrollment", "Sales Invoice", "Customer")
		}  # fmt: skip

	def seat_free(self, quota=QUOTA, category=OC):
		"""check_seat_available passing for a user without the override role."""
		frappe.set_user(PLAIN)
		try:
			return check_seat_available(PROGRAM, YEAR, quota, category) is True
		except frappe.ValidationError:
			return False
		finally:
			frappe.set_user("Administrator")

	def advance(self, student, amount):
		"""Pre-joining money: a Payment Entry (Receive) against the student's customer, with no invoice to settle."""
		ensure_customer_party_type()  # advances are found through its account type
		receivable = self.structure.receivable_account
		payment = frappe.get_doc(
			{
				"doctype": "Payment Entry", "payment_type": "Receive", "company": COLLEGE, "posting_date": today(),
				"party_type": "Customer", "party": frappe.db.get_value("Student", student, "customer"),
				"paid_from": receivable, "paid_to": self.accounts["bank"], "paid_amount": amount, "received_amount": amount,
				"reference_no": "ADV-UTR", "reference_date": today(),
			}
		)  # fmt: skip
		return payment.insert().submit().name


class TestAdmit(AdmitTestCase):
	"""M04-09: the admission approval makes an admitted Student and nothing billable, no enrollment and no ID."""

	def test_admit_creates_an_admitted_student_and_nothing_billable(self):
		self.matrix([(QUOTA, OC, 2)])
		applicant = self.applicant()
		before = self.counts()
		result = self.admit_as_officer(applicant)
		self.assertEqual(set(result), {"student"})
		student = frappe.get_doc("Student", result["student"])
		self.assertEqual(
			(student.student_applicant, student.oc_company, student.oc_admission_quota),
			(applicant, COLLEGE, QUOTA),
		)
		self.assertEqual(student.oc_academic_status, "Admitted")
		self.assertTrue(student.customer)  # the customer an advance is taken against
		self.assertFalse(student.oc_register_no)
		self.assertFalse(student.oc_roll_no)
		after = self.counts()
		self.assertEqual(
			(after["Program Enrollment"], after["Sales Invoice"]),
			(before["Program Enrollment"], before["Sales Invoice"]),
		)
		self.assertFalse(frappe.db.exists("Sales Invoice", {"customer": student.customer}))
		self.assertFalse(frappe.db.exists("Student Onboarding", {"student": student.name}))
		self.assertEqual(
			frappe.db.get_value("Student Applicant", applicant, "application_status"), "Admitted"
		)
		self.assertTrue(self.seat_free())  # no seat is held by the approval

	def test_strict_mode_does_not_stop_the_approval(self):
		self.settings(strict_seat_matrix=1)
		applicant = self.applicant()  # no Seat Matrix at all: the seat is checked at joining
		self.admit_as_officer(applicant)
		self.assertEqual(
			frappe.db.get_value("Student Applicant", applicant, "application_status"), "Admitted"
		)
		self.assertEqual(frappe.db.count("Seat Override Log", {"applicant": applicant}), 0)

	def test_aadhaar_is_copied_through_the_gate_and_logged(self):
		result = self.admit_as_officer(self.applicant(aadhaar=VALID))
		self.touched.append(result["student"])
		self.assertEqual(
			frappe.db.get_value("Student", result["student"], "oc_aadhaar_masked"), f"XXXX-XXXX-{VALID[-4:]}"
		)
		reasons = [r.reason for r in log_rows(self.touched[0])]
		self.assertEqual(reasons, ["admission copy"])

	def test_preconditions_are_enforced(self):
		for kwargs in ({"status": "Applied"}, {"eligibility": "Pending"}, {"eligibility": "Ineligible"}):
			applicant = self.applicant(**kwargs)
			with self.assertRaises(frappe.ValidationError, msg=str(kwargs)):
				self.admit_as_officer(applicant)
			self.assertNotEqual(
				frappe.db.get_value("Student Applicant", applicant, "application_status"), "Admitted"
			)

	def test_incomplete_documents_block_admission(self):
		applicant = self.applicant()
		with patch.object(
			admit_module.documents, "uncleared_mandatory", return_value=["Transfer Certificate"]
		):
			with self.assertRaises(frappe.ValidationError):
				self.admit_as_officer(applicant)

	def test_only_admission_officers_can_admit_and_a_second_admit_fails(self):
		applicant = self.applicant()
		frappe.set_user(PLAIN)
		with self.assertRaises(frappe.PermissionError):
			admit(applicant)
		frappe.set_user("Administrator")
		self.admit_as_officer(applicant)
		with self.assertRaises(frappe.ValidationError):
			self.admit_as_officer(applicant)
		self.assertEqual(frappe.db.count("Student", {"student_applicant": applicant}), 1)

	def test_a_failed_admission_leaves_nothing_half_created(self):
		applicant = self.applicant(aadhaar=VALID)
		before = self.counts()
		real, writes = frappe.db.set_value, []

		def fail_on_the_applicant_status(doctype, *args, **kwargs):
			writes.append(doctype)
			if (
				doctype == "Student Applicant" and writes.count(doctype) == 2
			):  # admit's own write, after the Student insert
				raise frappe.ValidationError("boom")
			return real(doctype, *args, **kwargs)

		with patch.object(frappe.db, "set_value", side_effect=fail_on_the_applicant_status):
			with self.assertRaises(frappe.ValidationError):
				self.admit_as_officer(applicant)
		self.assertIn("Student", writes)  # the Student and its Customer were made before the failure
		self.assertEqual(self.counts(), before)
		self.assertEqual(
			frappe.db.get_value("Student Applicant", applicant, "application_status"), "Approved"
		)
		self.assertFalse(frappe.db.exists("Student", {"student_applicant": applicant}))

	def test_upstream_enroll_student_is_refused_and_creates_nothing(self):
		"""Refuse Education's enroll_student mapper override and leave Student and Program Enrollment untouched."""
		from frappe.model.mapper import make_mapped_doc

		applicant = self.applicant()
		before = self.counts()
		self.assertEqual(
			frappe.override_whitelisted_method("education.education.api.enroll_student"),
			"campus.admissions.api.enroll_student",
		)
		with self.assertRaisesRegex(frappe.ValidationError, "Use Admit, then Confirm Joining"):
			make_mapped_doc("education.education.api.enroll_student", applicant)
		self.assertEqual(self.counts(), before)
		self.assertFalse(frappe.db.exists("Student", {"student_applicant": applicant}))


class TestJoining(AdmitTestCase):
	"""M04-09: the joining confirmation makes the enrollment, ID, invoice and attendance start, once per applicant."""

	def admitted(self, **kwargs):
		applicant = self.applicant(**kwargs)
		return applicant, self.admit_as_officer(applicant)["student"]

	def test_joining_creates_enrollment_numbers_invoice_and_attendance_start(self):
		self.matrix([(QUOTA, OC, 2)])
		applicant, student_name = self.admitted()
		result = self.join_as_officer(applicant)
		student = frappe.get_doc("Student", result["student"])
		self.assertEqual(student.name, student_name)
		self.assertEqual(student.oc_register_no, f"{ABBR}{YEAR_YY}{CODE}0001")
		self.assertEqual(result["register_no"], student.oc_register_no)
		self.assertEqual(student.oc_academic_status, "Active")
		enrollment = frappe.get_doc("Program Enrollment", result["program_enrollment"])
		self.assertEqual(
			(
				enrollment.docstatus,
				enrollment.oc_admission_quota,
				enrollment.student_category,
				enrollment.academic_year,
			),
			(1, QUOTA, OC, YEAR),
		)
		self.assertEqual(
			(str(enrollment.enrollment_date), str(enrollment.oc_attendance_start_date)), (today(), today())
		)
		self.assertEqual(enrollment.oc_joining_key, applicant)
		invoice = frappe.get_doc("Sales Invoice", result["invoice"])
		self.assertEqual(
			(invoice.docstatus, invoice.student, invoice.customer), (1, student.name, student.customer)
		)
		self.assertEqual(flt(invoice.grand_total), TUITION_FEE + EXAM_FEE)
		self.assertEqual(invoice.oc_program_enrollment, enrollment.name)
		self.assertEqual(
			frappe.db.get_value("Seat Matrix Row", {"quota": QUOTA, "category": OC}, "filled"), 1
		)
		self.assertEqual(
			frappe.get_all("Lifecycle Transition", {"student": student.name}, ["to_status", "source_doctype"], order_by="creation, name"),
			[{"to_status": "Admitted", "source_doctype": "Student Applicant"}, {"to_status": "Joined", "source_doctype": "Program Enrollment"},
			 {"to_status": "Active", "source_doctype": "Program Enrollment"}],
		)  # fmt: skip

	def test_invoice_uses_the_category_fee_structure(self):
		applicant, _student = self.admitted(category=BC)
		result = self.join_as_officer(applicant)
		self.assertEqual(
			flt(frappe.db.get_value("Sales Invoice", result["invoice"], "grand_total")), BC_TUITION + EXAM_FEE
		)

	def test_a_retry_returns_the_earlier_result_and_creates_nothing(self):
		applicant, student = self.admitted()
		first = self.join_as_officer(applicant)
		before = self.counts()
		transitions = frappe.db.count("Lifecycle Transition", {"student": student})
		self.assertEqual(self.join_as_officer(applicant), first)
		self.assertEqual(self.counts(), before)
		self.assertEqual(frappe.db.count("Lifecycle Transition", {"student": student}), transitions)
		self.assertEqual(frappe.db.count("Program Enrollment", {"student": student}), 1)
		self.assertEqual(frappe.db.count("Sales Invoice", {"student": student}), 1)

	def test_a_joining_that_stopped_part_way_resumes_without_a_second_enrollment_or_number(self):
		from campus.student_services.api import JOINED, transition

		onboarding.ensure_default_template(COLLEGE)
		applicant, student = self.admitted()
		state = admit_module.lock_applicant(applicant)
		row = joining_module.lock_student(applicant)
		joining_module.assign_numbers(
			row, state, YEAR
		)  # an earlier run got this far: numbers, enrollment, Joined
		enrollment = joining_module.create_enrollment(applicant, row, state, YEAR, getdate(), None)
		transition(
			student,
			JOINED,
			today(),
			"earlier run",
			("Program Enrollment", enrollment),
			program_enrollment=enrollment,
		)
		number = frappe.db.get_value("Student", student, "oc_register_no")
		result = self.join_as_officer(applicant)
		self.assertEqual((result["program_enrollment"], result["register_no"]), (enrollment, number))
		self.assertTrue(result["invoice"])
		self.assertEqual(frappe.db.count("Program Enrollment", {"student": student}), 1)
		self.assertEqual(frappe.db.get_value("Student", student, "oc_academic_status"), "Active")
		self.assertTrue(frappe.db.exists("Student Onboarding", {"student": student}))

	def test_joining_fills_the_seat(self):
		self.matrix([(QUOTA, OC, 1)])
		first, _first_student = self.admitted()
		second, other = self.admitted()  # both approvals pass: the approval holds no seat
		self.assertTrue(self.seat_free())
		self.join_as_officer(first)
		self.assertFalse(self.seat_free())
		with self.assertRaises(frappe.ValidationError):
			self.join_as_officer(second)
		self.assertEqual(frappe.db.get_value("Student", other, "oc_academic_status"), "Admitted")
		self.assertFalse(frappe.db.exists("Program Enrollment", {"student": other}))

	def test_the_joining_date_dates_the_enrollment_attendance_and_lifecycle(self):
		applicant, student = self.admitted()
		earlier = add_days(today(), -3)
		frappe.db.set_value(
			"Lifecycle Transition", {"student": student}, "effective_date", add_days(today(), -5)
		)  # admitted then
		with self.assertRaisesRegex(frappe.ValidationError, "future"):
			self.join_as_officer(applicant, joining_date=add_days(today(), 1))
		with self.assertRaisesRegex(frappe.ValidationError, "before the last status change"):
			self.join_as_officer(applicant, joining_date=add_days(today(), -6))  # before the admission
		result = self.join_as_officer(applicant, joining_date=earlier)
		enrollment = frappe.db.get_value(
			"Program Enrollment",
			result["program_enrollment"],
			["enrollment_date", "oc_attendance_start_date"],
			as_dict=True,
		)
		self.assertEqual(
			(str(enrollment.enrollment_date), str(enrollment.oc_attendance_start_date)), (earlier, earlier)
		)
		self.assertEqual(str(frappe.db.get_value("Student", student, "joining_date")), earlier)
		dates = frappe.get_all(
			"Lifecycle Transition",
			{"student": student, "to_status": ["in", ["Joined", "Active"]]},
			pluck="effective_date",
		)
		self.assertEqual([str(d) for d in dates], [earlier, earlier])

	def test_an_advance_paid_before_joining_is_allocated_to_the_invoice(self):
		from campus.campus_finance.api import paid_by_student

		applicant, student = self.admitted()
		advance = self.advance(student, 10000)
		result = self.join_as_officer(applicant)
		invoice = frappe.get_doc("Sales Invoice", result["invoice"])
		self.assertEqual(
			[(r.reference_name, flt(r.allocated_amount)) for r in invoice.advances], [(advance, 10000)]
		)
		self.assertEqual(flt(invoice.outstanding_amount), TUITION_FEE + EXAM_FEE - 10000)
		self.assertEqual(flt(frappe.db.get_value("Payment Entry", advance, "unallocated_amount")), 0)
		self.assertEqual(
			paid_by_student(invoice.name), 10000
		)  # counted as paid, e.g. by a cancellation refund

	def test_only_officers_join_and_only_an_admitted_applicant(self):
		applicant, student = self.admitted()
		frappe.set_user(PLAIN)
		with self.assertRaises(frappe.PermissionError):
			confirm_joining(applicant)
		frappe.set_user("Administrator")
		with self.assertRaises(frappe.PermissionError):
			self.join_as_officer(applicant, officer=OTHER_OFFICER)
		approved = self.applicant()
		with self.assertRaisesRegex(frappe.ValidationError, "not admitted yet"):
			self.join_as_officer(approved)
		self.assertFalse(frappe.db.exists("Program Enrollment", {"student": student}))

	def test_clearances_are_checked_again_at_joining(self):
		applicant, student = self.admitted()
		with patch.object(
			admit_module.documents, "uncleared_mandatory", return_value=["Transfer Certificate"]
		):
			with self.assertRaisesRegex(frappe.ValidationError, "incomplete mandatory documents"):
				self.join_as_officer(applicant)
		self.assertFalse(frappe.db.exists("Program Enrollment", {"student": student}))

	def test_failure_leaves_nothing_half_created(self):
		self.matrix([(QUOTA, OC, 1)])
		applicant, student = self.admitted()
		before = self.counts()
		with patch.object(joining_module, "create_fee_invoice", side_effect=frappe.ValidationError("boom")):
			with self.assertRaises(frappe.ValidationError):
				self.join_as_officer(applicant)
		self.assertEqual(self.counts(), before)
		self.assertEqual(
			frappe.db.get_value("Student", student, ["oc_academic_status", "oc_register_no"]),
			("Admitted", None),
		)
		self.assertTrue(self.seat_free())  # the seat is not consumed
		self.assertEqual(
			self.join_as_officer(applicant)["register_no"], f"{ABBR}{YEAR_YY}{CODE}0001"
		)  # and neither is the number

	def test_missing_fee_structure_rolls_back_too(self):
		applicant, _student = self.admitted(quota=FREE_QUOTA, category=OC)
		frappe.db.set_value("Fee Structure", self.structure.name, "docstatus", 2)
		self.addCleanup(frappe.db.set_value, "Fee Structure", self.structure.name, "docstatus", 1)
		before = self.counts()
		with self.assertRaises(frappe.ValidationError):
			self.join_as_officer(applicant)
		self.assertEqual(self.counts(), before)

	def test_roll_numbers_follow_their_own_pattern(self):
		self.settings(roll_no_pattern="R{yyyy}{program_code}-{###}")
		first = frappe.get_doc("Student", self.admit_and_join(self.applicant())["student"])
		second = frappe.get_doc("Student", self.admit_and_join(self.applicant())["student"])
		self.assertEqual(
			(first.oc_roll_no, second.oc_roll_no), (f"R{YEAR_FULL}{CODE}-001", f"R{YEAR_FULL}{CODE}-002")
		)
		self.assertEqual(second.oc_register_no[-4:], "0002")

	def test_the_id_card_is_issued_only_after_joining(self):
		from campus.student_services import api as student_api

		applicant, student = self.admitted()
		with self.assertRaisesRegex(frappe.ValidationError, "has not joined yet"):
			student_api.issue_id_card(student)
		self.assertFalse(frappe.db.exists("Verifiable Document", {"reference_name": student}))

	def test_a_program_enrollment_made_by_hand_is_refused_before_joining(self):
		frappe.flags.in_import = False  # a desk or API insert (setUp restores the flag)
		applicant, student = self.admitted()
		by_hand = {
			"doctype": "Program Enrollment", "student": student, "program": PROGRAM, "academic_year": YEAR,
			"enrollment_date": today(), "student_category": OC, "oc_admission_quota": QUOTA,
		}  # fmt: skip
		with self.assertRaisesRegex(frappe.ValidationError, "has not joined yet"):
			frappe.get_doc(by_hand).insert()
		legacy = frappe.get_doc(by_hand)
		legacy.flags.legacy_import = True
		frappe.db.savepoint("legacy_enrollment")
		legacy.insert()  # a legacy import may load it directly
		self.assertIsNone(
			legacy.oc_attendance_start_date
		)  # only the joining confirmation sets it: the whole term counts
		frappe.db.rollback(save_point="legacy_enrollment")
		self.join_as_officer(applicant)
		later = self.later_year()
		frappe.get_doc(
			{**by_hand, "academic_year": later, "enrollment_date": add_days(today(), 330)}
		).insert()  # promotion

	def later_year(self):
		name = "Admit Flow 2027"
		if not frappe.db.exists("Academic Year", name):
			frappe.get_doc(
				{
					"doctype": "Academic Year",
					"academic_year_name": name,
					"year_start_date": add_days(today(), 331),
					"year_end_date": add_days(today(), 690),
				}
			).insert()
		return name


YEAR_FULL = int(today()[:4])
YEAR_YY = today()[2:4]


def ensure_customer_party_type():
	if not frappe.db.exists("Party Type", "Customer"):  # the bare test site lacks ERPNext's party types
		frappe.get_doc(
			{"doctype": "Party Type", "party_type": "Customer", "account_type": "Receivable"}
		).insert()


class TestStrictJoining(AdmitTestCase):
	"""M04-08 at the joining (where the enrollment is made): a missing matrix or row refuses unless an override is logged."""

	def setUp(self):
		super().setUp()
		self.settings(strict_seat_matrix=1)

	def test_joining_without_a_seat_matrix_is_refused_and_nothing_is_created(self):
		applicant = self.applicant()
		student = self.admit_as_officer(applicant)["student"]
		before = self.counts()
		with self.assertRaisesRegex(frappe.ValidationError, "No Seat Matrix"):
			self.join_as_officer(applicant)
		self.assertEqual(self.counts(), before)
		self.assertEqual(frappe.db.get_value("Student", student, "oc_academic_status"), "Admitted")
		self.assertEqual(frappe.db.count("Seat Override Log", {"applicant": applicant}), 0)

	def test_joining_into_a_missing_row_is_refused(self):
		self.matrix([(QUOTA, OC, 5)])
		applicant = self.applicant(category=BC)
		student = self.admit_as_officer(applicant)["student"]
		with self.assertRaisesRegex(frappe.ValidationError, "no row"):
			self.join_as_officer(applicant)
		self.assertFalse(frappe.db.exists("Program Enrollment", {"student": student}))

	def test_an_override_needs_the_officers_justification(self):
		self.matrix([(QUOTA, OC, 5)])
		applicant = self.applicant(category=BC)
		self.admit_as_officer(applicant)
		for blank in (None, "  "):
			with self.assertRaisesRegex(frappe.ValidationError, "justification"):
				self.join_as_officer(applicant, officer=OVERRIDE_OFFICER, override_justification=blank)
		self.assertEqual(frappe.db.count("Seat Override Log", {"applicant": applicant}), 0)

	def test_an_override_officer_joins_into_a_missing_row_and_it_is_logged_with_the_justification(self):
		matrix = self.matrix([(QUOTA, OC, 5)])
		applicant = self.applicant(category=BC)
		self.admit_as_officer(applicant)
		result = self.join_as_officer(
			applicant, officer=OVERRIDE_OFFICER, override_justification=" University order 12/2026 "
		)
		self.assertEqual(
			frappe.db.get_value("Program Enrollment", result["program_enrollment"], "docstatus"), 1
		)
		log = frappe.get_all(
			"Seat Override Log",
			{"applicant": applicant},
			["user", "reason", "quota", "category", "justification"],
		)
		self.assertEqual(
			[(r.user, r.reason, r.quota, r.category, r.justification) for r in log],
			[(OVERRIDE_OFFICER, "No Seat Matrix Row", QUOTA, BC, "University order 12/2026")],
		)
		rows = frappe.get_all("Seat Matrix Row", {"parent": matrix.name}, ["quota", "category", "sanctioned"])
		self.assertEqual([(r.quota, r.category, r.sanctioned) for r in rows], [(QUOTA, OC, 5)])
		self.join_as_officer(
			applicant, officer=OVERRIDE_OFFICER, override_justification="again"
		)  # a retry logs nothing more
		self.assertEqual(frappe.db.count("Seat Override Log", {"applicant": applicant}), 1)

	def test_a_free_seat_in_a_configured_row_joins_without_a_log(self):
		self.matrix([(QUOTA, OC, 1)])
		applicant = self.applicant()
		self.admit_and_join(applicant)
		self.assertEqual(frappe.db.count("Seat Override Log", {"applicant": applicant}), 0)

	def test_non_strict_mode_joins_without_a_matrix_as_before(self):
		self.settings(strict_seat_matrix=0)
		applicant = self.applicant()
		result = self.admit_and_join(applicant)
		self.assertEqual(
			frappe.db.get_value("Program Enrollment", result["program_enrollment"], "docstatus"), 1
		)
		self.assertEqual(frappe.db.count("Seat Override Log", {"applicant": applicant}), 0)


class TestJoiningGatePatch(AdmitTestCase):
	"""campus.patches.joining_gate: a student admitted and enrolled before the gate counts as joined (its parts; the
	patch's custom-field step is DDL and is not run in a test)."""

	def test_an_enrolled_admitted_student_is_joined_on_the_enrollment_and_keyed_to_the_applicant(self):
		from campus.patches import joining_gate

		applicant = self.applicant()
		student = self.admit_as_officer(applicant)["student"]
		enrollment = frappe.get_doc(
			{"doctype": "Program Enrollment", "student": student, "program": PROGRAM, "academic_year": YEAR, "enrollment_date": today(),
			 "student_category": OC, "oc_admission_quota": QUOTA}
		)  # fmt: skip
		enrollment.flags.legacy_import = True  # made by the admission before the gate
		enrollment.insert().submit()
		for _run in range(2):  # idempotent
			joining_gate.key_joining_enrollments()
			joining_gate.mark_admitted_students_joined()
		row = frappe.db.get_value(
			"Program Enrollment",
			enrollment.name,
			["oc_joining_key", "oc_attendance_start_date"],
			as_dict=True,
		)
		self.assertEqual(
			(row.oc_joining_key, row.oc_attendance_start_date), (applicant, None)
		)  # the gate fills no start date
		self.assertEqual(frappe.db.get_value("Student", student, "oc_academic_status"), "Active")
		changes = frappe.get_all(
			"Lifecycle Transition",
			{"student": student, "kind": "Transition"},
			["to_status", "source_name", "effective_date"],
			order_by="creation, name",
		)
		self.assertEqual([(c.to_status, c.source_name, str(c.effective_date)) for c in changes], [
			("Joined", enrollment.name, today()), ("Active", enrollment.name, today()),
		])  # fmt: skip
		result = self.join_as_officer(
			applicant
		)  # the joining confirmation returns it instead of enrolling again
		self.assertEqual(result["program_enrollment"], enrollment.name)
		self.assertEqual(frappe.db.count("Program Enrollment", {"student": student}), 1)


class TestRegisterNumbers(AdmitTestCase):
	def test_rolled_back_number_is_reissued(self):
		frappe.db.savepoint("numbering")
		first = numbers.next_number("{abbr}{yy}{program_code}{####}", COLLEGE, PROGRAM, YEAR)
		frappe.db.rollback(save_point="numbering")
		self.assertEqual(numbers.next_number("{abbr}{yy}{program_code}{####}", COLLEGE, PROGRAM, YEAR), first)


class TestCancellation(AdmitTestCase):
	def admitted(self, **kwargs):
		"""An admission that has joined (enrollment and fee invoice exist): the admission a cancellation cancels."""
		result = self.admit_and_join(self.applicant(**kwargs))
		return SimpleNamespace(
			applicant=frappe.db.get_value("Student", result["student"], "student_applicant"), **result
		)

	def cancel(self, admission, days, submit=True, user=OFFICER):
		"""Cancel today, `days` days after the admission year started (the refund slab counts from that start)."""
		frappe.db.set_value("Academic Year", YEAR, "year_start_date", add_days(today(), -days))
		return self.cancel_on(admission, today(), submit, user)

	def cancel_on(self, admission, date, submit=True, user=OFFICER):
		doc = frappe.get_doc(
			{
				"doctype": "Admission Cancellation",
				"student": admission.student,
				"program_enrollment": admission.program_enrollment,
				"cancellation_date": date,
				"reason": "Joined another college",
			}
		)
		frappe.set_user(user)
		try:
			doc.insert()
			return doc.submit() if submit else doc
		finally:
			frappe.set_user("Administrator")

	def pay_in_full(self, invoice):
		from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry

		payment = get_payment_entry("Sales Invoice", invoice)
		payment.reference_no, payment.reference_date = "UTR1", today()
		payment.insert().submit()

	def gl(self, voucher):
		rows = frappe.get_all(
			"GL Entry", {"voucher_no": voucher, "is_cancelled": 0}, ["account", "debit", "credit"]
		)
		return {r.account: (flt(r.debit), flt(r.credit)) for r in rows}

	def test_refund_preview_on_the_draft(self):
		admission = self.admitted()
		doc = self.cancel(admission, 10, submit=False)
		self.assertEqual(
			(doc.days_since_start, flt(doc.deduction), flt(doc.refundable_amount)), (10, 1000, 51000)
		)
		self.assertEqual(doc.fee_invoice, admission.invoice)

	def test_before_the_cutoff_unpaid_invoice_full_refund_less_fee(self):
		self.matrix([(QUOTA, OC, 1)])
		admission = self.admitted()
		self.assertFalse(self.seat_free())
		doc = self.cancel(admission, 10)
		self.assertEqual(
			(flt(doc.deduction), flt(doc.refundable_amount), flt(doc.cash_refund)), (1000, 51000, 0)
		)
		note = frappe.get_doc("Sales Invoice", doc.credit_note)
		self.assertEqual((note.docstatus, note.is_return, note.return_against), (1, 1, admission.invoice))
		self.assertEqual(flt(note.grand_total), -51000)
		entries = self.gl(note.name)
		self.assertEqual(entries[f"Sales - {ABBR}"], (51000, 0))  # income reversed
		self.assertEqual(sum(d for d, _c in entries.values()), sum(c for _d, c in entries.values()))
		self.assertEqual(
			flt(frappe.db.get_value("Sales Invoice", admission.invoice, "outstanding_amount")), 1000
		)
		self.assertFalse(doc.payment_entry)  # nothing was paid, so nothing to pay back
		self.assertEqual(
			frappe.db.get_value("Program Enrollment", admission.program_enrollment, "docstatus"), 2
		)
		self.assertTrue(self.seat_free())  # seat freed
		self.assertEqual(frappe.db.get_value("Student", admission.student, "enabled"), 0)
		self.assertEqual(
			frappe.db.get_value("Student Applicant", admission.applicant, "application_status"), "Rejected"
		)

	def test_after_the_cutoff_the_slab_deduction_applies_and_a_payment_entry_is_drafted(self):
		admission = self.admitted()
		self.pay_in_full(admission.invoice)
		doc = self.cancel(admission, 20)
		self.assertEqual(
			(flt(doc.deduction), flt(doc.refundable_amount), flt(doc.cash_refund)), (10400, 41600, 41600)
		)
		payment = frappe.get_doc("Payment Entry", doc.payment_entry)
		self.assertEqual(
			(payment.docstatus, payment.payment_type, payment.party_type), (0, "Pay", "Customer")
		)
		self.assertEqual(flt(payment.paid_amount), 41600)
		self.assertEqual([r.reference_name for r in payment.references], [doc.credit_note])
		self.assertEqual(flt(frappe.db.get_value("Sales Invoice", doc.credit_note, "grand_total")), -41600)

	def test_beyond_the_last_slab_nothing_is_refunded(self):
		admission = self.admitted()
		doc = self.cancel(admission, 45)
		self.assertEqual((flt(doc.refundable_amount), flt(doc.deduction)), (0, 52000))
		self.assertFalse(doc.credit_note)
		self.assertEqual(
			frappe.db.get_value("Program Enrollment", admission.program_enrollment, "docstatus"), 2
		)

	def test_the_rule_of_the_admission_year_wins_over_the_general_rule(self):
		self.rule(slabs=[(0, 60, 50, 0)], year=YEAR)
		doc = self.cancel(self.admitted(), 5, submit=False)
		self.assertEqual(flt(doc.deduction), 26000)

	def test_a_cancellation_is_final_and_cannot_be_repeated(self):
		admission = self.admitted()
		doc = self.cancel(admission, 1)
		with self.assertRaises(frappe.ValidationError):
			doc.cancel()
		with self.assertRaises(frappe.ValidationError):
			self.cancel(admission, 2)

	def test_an_officer_of_another_company_cannot_cancel_and_sees_no_amounts(self):
		admission = self.admitted()
		for submit in (False, True):
			with self.assertRaises(frappe.PermissionError) as raised:
				self.cancel_on(admission, today(), submit, user=OTHER_OFFICER)
			self.assertNotIn("51000", str(raised.exception))
			self.assertNotIn("52000", str(raised.exception))
		self.assertEqual(frappe.db.count("Admission Cancellation"), 0)

	def test_cancellation_date_is_not_future_and_not_before_admission(self):
		admission = self.admitted()
		with self.assertRaises(frappe.ValidationError):
			self.cancel_on(admission, add_days(today(), 1), submit=False)
		frappe.db.set_value(
			"Program Enrollment", admission.program_enrollment, "enrollment_date", add_days(today(), -2)
		)
		with self.assertRaises(frappe.ValidationError):
			self.cancel_on(admission, add_days(today(), -3), submit=False)
		self.assertTrue(self.cancel_on(admission, add_days(today(), -2), submit=False))

	def test_documents_are_dated_today_and_the_frozen_date_applies(self):
		admission = self.admitted()
		self.pay_in_full(admission.invoice)
		earlier = add_days(today(), -2)
		frappe.db.set_value(
			"Program Enrollment", admission.program_enrollment, "enrollment_date", add_days(today(), -5)
		)
		frappe.db.set_value(
			"Lifecycle Transition", {"student": admission.student}, "effective_date", add_days(today(), -5)
		)  # admitted then
		doc = self.cancel_on(admission, earlier)
		self.assertEqual(str(frappe.db.get_value("Sales Invoice", doc.credit_note, "posting_date")), today())
		self.assertEqual(
			str(frappe.db.get_value("Payment Entry", doc.payment_entry, "posting_date")), today()
		)
		second = self.admitted()
		frappe.db.set_value(
			"Company", COLLEGE, "accounts_frozen_till_date", today()
		)  # rolled back with the test savepoint
		with self.assertRaises(frappe.ValidationError):
			self.cancel(second, 5)  # an officer has no frozen-accounts modifier role: no bypass

	def test_the_officer_session_survives_admit_joining_and_cancellation(self):
		"""No user switching: sid, session data, csrf token, user and form_dict are exactly as the request had them."""
		frappe.set_user(OFFICER)
		session_data = frappe._dict(
			user=OFFICER, csrf_token="csrf-token", data=frappe._dict(user=OFFICER, session_expiry="06:00:00")
		)
		frappe.local.session = frappe._dict(
			user=OFFICER, sid="sid-123", data=session_data.data, csrf_token="csrf-token", session_obj=None
		)
		frappe.local.form_dict = frappe._dict(applicant="x", cmd="campus.admissions.api.admit")
		snapshot = lambda: (  # noqa: E731
			frappe.session.user,
			frappe.session.sid,
			frappe.session.csrf_token,
			dict(frappe.session.data),
			dict(frappe.local.form_dict),
		)
		before = snapshot()
		try:
			applicant = self.applicant()
			admit(applicant)
			self.assertEqual(snapshot(), before)
			result = confirm_joining(applicant)
			self.assertEqual(snapshot(), before)
			self.cancel_with_current_user(result)
			self.assertEqual(snapshot(), before)
		finally:
			frappe.set_user("Administrator")

	def cancel_with_current_user(self, result):
		student = frappe.db.get_value("Student", result["student"], "name")
		doc = frappe.get_doc(
			{"doctype": "Admission Cancellation", "student": student, "program_enrollment": result["program_enrollment"],
			 "cancellation_date": today(), "reason": "Session check"}
		)  # fmt: skip
		doc.insert()
		doc.submit()

	def award(self, student, scheme_type, amount):
		"""Disburse `amount` of a Government (Payment Entry) or Institutional (Journal Entry) scholarship; returns the voucher."""
		from campus.campus_finance import scholarships

		frappe.flags.in_import = False  # defaults apply (setUp restores the flag)
		ensure_customer_party_type()
		account = (
			self.accounts["bank"]
			if scheme_type == "Government"
			else make_account(COLLEGE, ABBR, "Scholarship Expense", "Indirect Expenses", root_type="Expense")
		)
		scheme = frappe.get_doc(
			{"doctype": "Scholarship Scheme", "scheme_name": f"AF {scheme_type} Scheme", "company": COLLEGE,
			 "scheme_type": scheme_type, "account": account, "amount_type": "Fixed", "amount": amount}
		).insert()  # fmt: skip
		drop_policies(
			"Scholarship Application"
		)  # sanctioned by submitting; test_mandatory_approval covers the approval list
		with approval_not_required():
			application = frappe.get_doc(
				{"doctype": "Scholarship Application", "student": student, "scheme": scheme.name, "academic_year": YEAR}
			).insert().submit()  # fmt: skip
		row = scholarships.disburse(application.name, amount, "SCH-UTR")
		return frappe.get_doc(row["voucher_type"], row["voucher"])

	def test_a_cancellation_never_pays_out_a_scholarship(self):
		for scheme_type in ("Government", "Institutional"):
			admission = self.admitted()
			voucher = self.award(admission.student, scheme_type, 20000)
			self.pay_in_full(admission.invoice)  # the student pays the other 32000
			with self.assertRaisesRegex(frappe.ValidationError, f"scholarship.*{voucher.name}"):
				self.cancel(admission, 20, submit=False)
			voucher.cancel()  # Accounts reverses the scholarship first
			doc = self.cancel(admission, 20)
			self.assertEqual(
				(flt(doc.amount_paid), flt(doc.deduction), flt(doc.cash_refund)),
				(32000, 10400, 21600),
				scheme_type,
			)
			self.assertEqual(
				flt(frappe.db.get_value("Payment Entry", doc.payment_entry, "paid_amount")), 21600
			)

	def test_only_money_received_counts_as_paid(self):
		admission = self.admitted()
		from campus.campus_finance import api as finance

		finance.make_credit_note(
			frappe.get_doc("Sales Invoice", admission.invoice), 2000
		)  # accounts credited it by hand
		doc = self.cancel(admission, 10, submit=False)  # deduction 1000
		self.assertEqual((flt(doc.amount_paid), flt(doc.cash_refund)), (0, 0))

	def test_refund_register_and_admission_report(self):
		admission = self.admitted()
		self.pay_in_full(admission.invoice)
		doc = self.cancel(admission, 20)
		_columns, rows = refund_register.execute({"company": COLLEGE})
		self.assertEqual(
			[(r.name, flt(r.refundable_amount), r.payment_status) for r in rows], [(doc.name, 41600, "Draft")]
		)
		_columns, rows = refund_register.execute({"company": COLLEGE, "from_date": add_days(today(), 25)})
		self.assertEqual(rows, [])
		make_user(PLAIN, [COLLEGE])
		frappe.set_user(PLAIN)
		self.assertEqual(refund_register.execute({"company": OTHER_COLLEGE})[1], [])
		self.assertEqual(quota_report.execute({"company": OTHER_COLLEGE})[1], [])


class TestConcurrentNumbers(CampusTestCase):
	def test_concurrent_numbers_are_unique_and_gap_free(self):
		"""Eight real connections draw from a brand new series at once (committed, then removed)."""
		prefix = f"CONC{frappe.generate_hash(length=6).upper()}"
		site, sites_path, errors, drawn = frappe.local.site, frappe.local.sites_path, [], []

		def worker():
			try:
				frappe.init(site, sites_path=sites_path)
				frappe.connect()
				drawn.append(numbers.next_number("X{####}", COLLEGE, PROGRAM, YEAR))
				frappe.db.commit()
			except Exception as error:
				errors.append(repr(error))
			finally:
				frappe.destroy()

		threads = [threading.Thread(target=worker) for _ in range(8)]
		try:
			# patched once for all threads: eight overlapping patch/unpatch pairs can leave the mock in place
			with patch.object(numbers, "render_prefix", return_value=prefix):
				for thread in threads:
					thread.start()
				for thread in threads:
					thread.join()
		finally:
			connection = own_connection()
			connection.sql("delete from `tabSeries` where name = %s", (prefix,))
			connection.commit()
			connection.close()
		self.assertEqual(errors, [])
		self.assertEqual(sorted(drawn), [f"{prefix}{n:04d}" for n in range(1, 9)])


class TestPrintsAndReports(AdmitTestCase):
	def test_prints_render(self):
		applicant = self.applicant()
		doc = frappe.get_doc("Student Applicant", applicant)
		doc.append(
			"oc_documents", {"document_type": "HSC Marksheet", "status": "Verified", "original_retained": 1}
		)
		doc.append("oc_documents", {"document_type": "Photo", "status": "Submitted", "original_retained": 0})
		doc.save()
		letter = frappe.get_print("Student Applicant", applicant, "Provisional Admission Letter")
		self.assertIn(PROGRAM, letter)
		self.assertIn(YEAR, letter)
		receipt = frappe.get_print("Student Applicant", applicant, "Original Documents Receipt")
		self.assertIn("HSC Marksheet", receipt)
		self.assertNotIn("Photo", receipt)
		student = self.admit_and_join(applicant)["student"]
		self.assertIn(
			frappe.db.get_value("Student", student, "oc_register_no"),
			frappe.get_print("Student", student, "Provisional Admission Letter Student"),
		)

	def test_admissions_report_counts_by_quota_category_gender_state(self):
		self.matrix([(QUOTA, OC, 5), (QUOTA, BC, 5)])
		for category in (OC, OC, BC):
			self.admit_and_join(self.applicant(category=category))
		_columns, rows = quota_report.execute({"company": COLLEGE, "academic_year": YEAR})
		table = {(r["grouping"], r["value"]): r["admitted"] for r in rows}
		self.assertEqual(table[("Total", "")], 3)
		self.assertEqual(
			(table[("Quota", QUOTA)], table[("Category", OC)], table[("Category", BC)]), (3, 2, 1)
		)
		self.assertEqual((table[("Gender", "Male")], table[("State", "Tamil Nadu")]), (3, 3))
		sanctioned = {r["value"]: r["sanctioned"] for r in rows if r["grouping"] == "Category"}
		self.assertEqual(sanctioned, {OC: 5, BC: 5})


class TestAqarCounts(CampusTestCase):
	"""AQAR 2.1.1 / 2.1.2: the report equals independent counts of the demo enrollments."""

	demo_scale = "small"

	def test_report_matches_the_demo_enrollments(self):
		from campus.tests.demo import ENGINEERING

		_columns, rows = quota_report.execute({"company": ENGINEERING})
		table = {(r["grouping"], r["value"]): r["admitted"] for r in rows}
		total = frappe.db.sql(
			"select count(*) from `tabProgram Enrollment` where docstatus = 1 and oc_company = %s",
			ENGINEERING,
		)[0][0]
		self.assertGreater(total, 0)
		self.assertEqual(table[("Total", "")], total)
		by_category = frappe.db.sql(
			"select student_category, count(*) from `tabProgram Enrollment` where docstatus = 1 and oc_company = %s group by 1",
			ENGINEERING,
		)
		self.assertEqual({v: n for (g, v), n in table.items() if g == "Category"}, dict(by_category))
		for grouping in ("Quota", "Category", "Gender", "State"):
			self.assertEqual(sum(n for (g, _v), n in table.items() if g == grouping), total, grouping)


class TestOfficerWithoutInvoiceRights(AdmitTestCase):
	"""I3: the pilot kit's Admission Officer holds no other role (no Academics User, Accounts or System Manager);
	Confirm Joining and Admission Cancellation run with an advance and a refund. Its Sales Invoice / Payment Entry rule is
	read only (ERPNext's validation reads them as the session user); it cannot write, create or export them."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		make_user(PURE_OFFICER, [COLLEGE])
		user = frappe.get_doc("User", PURE_OFFICER)
		user.remove_roles("Academics User")
		user.add_roles("Admission Officer")

	def test_the_officer_holds_only_its_own_role_and_reads_invoices_without_changing_them(self):
		roles = set(frappe.get_roles(PURE_OFFICER)) - {"All", "Desk User", "Guest"}
		self.assertEqual(roles, {"Admission Officer"})
		for doctype in ("Sales Invoice", "Payment Entry"):
			self.assertTrue(frappe.has_permission(doctype, "read", user=PURE_OFFICER), doctype)
			for ptype in ("write", "create", "submit", "cancel", "delete", "print", "email"):
				self.assertFalse(
					frappe.has_permission(doctype, ptype, user=PURE_OFFICER), f"{doctype} {ptype}"
				)

	def test_confirm_joining_allocates_a_pre_joining_advance(self):
		applicant = self.applicant()
		student = self.admit_as_officer(applicant, PURE_OFFICER)["student"]
		advance = self.advance(student, 10000)
		result = self.join_as_officer(applicant, PURE_OFFICER)
		invoice = frappe.get_doc("Sales Invoice", result["invoice"])
		self.assertEqual(
			[(r.reference_name, flt(r.allocated_amount)) for r in invoice.advances], [(advance, 10000)]
		)

	def test_a_cancellation_after_joining_credits_the_invoice_and_drafts_the_refund(self):
		result = self.admit_and_join(self.applicant(), PURE_OFFICER)
		from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry

		payment = get_payment_entry("Sales Invoice", result["invoice"])
		payment.reference_no, payment.reference_date = "UTR-PURE", today()
		payment.insert().submit()
		frappe.db.set_value("Academic Year", YEAR, "year_start_date", add_days(today(), -20))
		doc = self.cancel_as_pure_officer(
			student=result["student"], program_enrollment=result["program_enrollment"]
		)
		self.assertEqual((flt(doc.deduction), flt(doc.cash_refund)), (10400, 41600))
		self.assertEqual(frappe.db.get_value("Payment Entry", doc.payment_entry, "docstatus"), 0)
		self.assertTrue(doc.credit_note)

	def test_a_cancellation_before_joining_drafts_the_advance_refund(self):
		applicant = self.applicant()
		student = self.admit_as_officer(applicant, PURE_OFFICER)["student"]
		self.advance(student, 8000)
		doc = self.cancel_as_pure_officer(student=student)
		self.assertEqual(flt(doc.cash_refund), 8000)
		payment = frappe.get_doc("Payment Entry", doc.payment_entry)
		self.assertEqual(
			(payment.docstatus, payment.payment_type, flt(payment.paid_amount)), (0, "Pay", 8000)
		)

	def cancel_as_pure_officer(self, **fields):
		doc = frappe.get_doc(
			{
				"doctype": "Admission Cancellation",
				"cancellation_date": today(),
				"reason": "Joined another college",
				**fields,
			}
		)
		frappe.set_user(PURE_OFFICER)
		try:
			doc.insert()
			return doc.submit()
		finally:
			frappe.set_user("Administrator")
