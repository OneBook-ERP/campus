"""M04-09 Joining confirmation: an admitted student joins, and only then gets a Program Enrollment, the student ID
(register and roll numbers), the fee invoice, the onboarding checklist and an attendance start date.

`confirm_joining(applicant, joining_date, override_justification)` (role Admission Officer) runs in one transaction
behind a savepoint, after a locking read of the applicant and of its Student:
- the seat check (M04-08, strict mode included) under the Seat Matrix lock, then a submitted Program Enrollment
  dated the joining date, with `oc_attendance_start_date` the joining date and `oc_joining_key` the applicant
  (unique: one joining enrollment per applicant). An override needs the officer's typed justification, stored on
  the Seat Override Log;
- register and roll numbers from the Admission Settings patterns (the ID card can be issued from now on);
- lifecycle Joined from the joining date (M05-09), never before the last lifecycle change; a joining dated after the
  student's reporting deadline needs an approved Late Joining Request for that date (M04-10,
  campus.admissions.reporting); the fee Sales Invoice from the quota/category Fee Structure,
  with the advances paid before joining (unallocated Payment Entries against the student's customer) allocated to
  it; the onboarding checklist; lifecycle Active, which marks the joining complete.

The applicant is the stable key. A retry after the joining returns the earlier result and writes nothing. A
joining that stopped part-way (status Joined) resumes: every step first looks for what an earlier run made
(numbers, enrollment, invoice, onboarding) and makes only what is missing. A failure rolls back to the savepoint,
and a deadlock is retried once (as admit does).

`validate_enrollment` (Program Enrollment validate) keeps the gate closed for enrollments made by hand: the first
enrollment of an admitted student into a campus program comes only from here (or from a legacy import).
"""

import frappe
from frappe import _
from frappe.utils import add_days, getdate, today

from campus.admissions import numbers
from campus.admissions import seat_matrix as seats
from campus.admissions.admit import (
	admission_year,
	check_cleared,
	check_company_access,
	lock_applicant,
	retry_on_deadlock,
)
from campus.admissions.reporting import check_late_joining, student_deadline, withdraw_referrals
from campus.admissions.setup import OFFICER_ROLE
from campus.campus_finance.api import fee_structure_for
from campus.student_services.api import (
	ACTIVE,
	ADMITTED,
	CANCELLED,
	JOINED,
	create_onboarding,
	last_change_date,
	transition,
)

SAVEPOINT = "campus_joining"
JOINING = (ADMITTED, JOINED)  # statuses a joining starts from (Joined: resuming one that stopped part-way)


def confirm_joining(applicant, joining_date=None, override_justification=None):
	"""See `campus.admissions.api.confirm_joining`."""
	frappe.only_for(OFFICER_ROLE)
	action = _("confirming the joining of {0}").format(applicant)
	return retry_on_deadlock(join_once, applicant, joining_date, override_justification, action=action)


def join_once(applicant, joining_date, justification):
	state = lock_applicant(applicant)
	check_company_access(state.oc_company)
	student = lock_student(applicant)
	if student.oc_academic_status not in JOINING:
		return earlier_result(applicant, student)
	date = check_joining_date(student.name, joining_date)
	check_joinable(applicant, state, student)
	check_late_joining(applicant, student_deadline(student.oc_reporting_deadline, state.program), date)
	frappe.db.savepoint(SAVEPOINT)
	try:
		return join(applicant, state, student, date, justification)
	except frappe.QueryDeadlockError:
		raise  # the transaction and its savepoints are gone; confirm_joining() rolls back and retries
	except Exception:
		frappe.db.rollback(save_point=SAVEPOINT)
		raise


def lock_student(applicant):
	"""The applicant's Student (Education allows one), read with a locking read: its lifecycle status says whether
	the joining is still to do."""
	student = frappe.db.get_value(
		"Student", {"student_applicant": applicant},
		["name", "oc_academic_status", "enabled", "oc_register_no", "oc_roll_no", "oc_reporting_deadline"],
		as_dict=True, for_update=True,
	)  # fmt: skip
	if not student:
		frappe.throw(
			_("Applicant {0} is not admitted yet: admit them before confirming the joining.").format(
				applicant
			)
		)
	return student


def earlier_result(applicant, student):
	"""The result of the joining already done, or a refusal when the student never joined (e.g. cancelled)."""
	enrollment = joining_enrollment(applicant)
	if not enrollment:
		frappe.throw(
			_("{0} cannot join: their lifecycle status is {1}.").format(
				frappe.bold(student.name), _(student.oc_academic_status)
			)
		)
	return result(student.name, enrollment)


def check_joining_date(student, joining_date):
	"""The joining date (default today): not in the future, not before the last lifecycle change. Checked before any
	write (transition() checks the floor again under the Student lock)."""
	date = getdate(joining_date or today())
	if date > getdate(today()):
		frappe.throw(_("The joining date cannot be in the future."))
	last = last_change_date(student)
	if last and date < getdate(last):
		frappe.throw(
			_("The joining date {0} is before the last status change of {1} ({2}).").format(
				frappe.format(date, "Date"), frappe.bold(student), frappe.format(last, "Date")
			)
		)
	return date


def check_joinable(applicant, state, student):
	if state.application_status != "Admitted":
		frappe.throw(
			_("Only an admitted applicant can join; {0} is {1}.").format(applicant, state.application_status)
		)
	if not student.enabled:
		frappe.throw(_("Student {0} is disabled.").format(student.name))
	check_cleared(applicant, state)


def join(applicant, state, student, date, justification):
	year = admission_year(state)
	assign_numbers(student, state, year)
	resuming = (
		student.oc_academic_status == JOINED
	)  # only then can the enrollment exist (see joining_enrollment)
	enrollment = (resuming and joining_enrollment(applicant)) or create_enrollment(
		applicant, student, state, year, date, justification
	)
	if student.oc_academic_status == ADMITTED:
		transition(
			student.name,
			JOINED,
			date,
			_("Joining confirmed"),
			("Program Enrollment", enrollment),
			program_enrollment=enrollment,
		)
		withdraw_referrals(student.name)
	if not admission_invoice(enrollment):
		create_fee_invoice(student.name, enrollment, state, year)
	create_onboarding(student.name)  # Education's enrollment submit already set the student's joining date
	transition(
		student.name,
		ACTIVE,
		date,
		_("Joining confirmed"),
		("Program Enrollment", enrollment),
		program_enrollment=enrollment,
	)
	return result(student.name, enrollment)


def assign_numbers(student, state, year):
	"""Register (and roll) number: the student ID, drawn once (a resumed joining keeps the numbers it has)."""
	settings = frappe.get_doc("Admission Settings")
	values = {}
	if not student.oc_register_no:
		values["oc_register_no"] = numbers.next_number(
			settings.register_no_pattern, state.oc_company, state.program, year
		)
	if settings.roll_no_pattern and not student.oc_roll_no:
		values["oc_roll_no"] = numbers.next_number(
			settings.roll_no_pattern, state.oc_company, state.program, year
		)
	if values:
		frappe.db.set_value("Student", student.name, values)


def joining_enrollment(applicant):
	"""The submitted enrollment the applicant's joining made, by a locking read (shared: seat counts are not blocked).

	Read only for a student who has joined (or is resuming): for one still Admitted there is none, and a locking read
	of a missing key would gap-lock the unique index, where a concurrent joining inserts its key while holding the
	Seat Matrix lock this transaction is about to ask for (a deadlock)."""
	rows = frappe.db.sql(
		"select name from `tabProgram Enrollment` where oc_joining_key = %s and docstatus = 1 lock in share mode",
		applicant,
	)
	return rows[0][0] if rows else None


def create_enrollment(applicant, student, state, year, date, justification):
	"""The seat check under the matrix lock (taken before any enrollment row lock), then the submitted enrollment."""
	seats.check_seat_available(
		state.program, year, state.oc_admission_quota, state.student_category, applicant, justification
	)
	release_cancelled_key(applicant)
	enrollment = frappe.get_doc(
		{
			"doctype": "Program Enrollment", "student": student.name, "program": state.program, "academic_year": year,
			"academic_term": state.academic_term, "enrollment_date": date, "oc_attendance_start_date": date,
			"student_category": state.student_category, "oc_admission_quota": state.oc_admission_quota,
			"oc_joining_key": applicant,
		}
	)  # fmt: skip
	enrollment.flags.campus_joining = True
	enrollment.insert(ignore_permissions=True)
	return enrollment.submit().name


def release_cancelled_key(applicant):
	"""A cancelled enrollment holds no joining: the applicant's key (unique) moves to the new joining enrollment.
	Run after the seat check, so the key's index lock is taken under the matrix lock, as the insert's is."""
	cancelled = {"oc_joining_key": applicant, "docstatus": 2}
	if frappe.db.exists("Program Enrollment", cancelled):
		frappe.db.set_value("Program Enrollment", cancelled, "oc_joining_key", None, update_modified=False)


def carry_joining_key(doc, method=None):
	"""Program Enrollment on_submit: the amendment of a joining enrollment becomes the joining enrollment, so a retried
	joining returns it. The key moves from the cancelled original (cleared there first: the key is unique)."""
	key = doc.amended_from and frappe.db.get_value("Program Enrollment", doc.amended_from, "oc_joining_key")
	if key:
		frappe.db.set_value(
			"Program Enrollment", doc.amended_from, "oc_joining_key", None, update_modified=False
		)
		doc.db_set("oc_joining_key", key, update_modified=False)


def admission_invoice(enrollment):
	return frappe.db.get_value(
		"Sales Invoice", {"oc_program_enrollment": enrollment, "docstatus": 1, "is_return": 0}, "name"
	)


def create_fee_invoice(student, enrollment, state, year):
	"""Submitted Sales Invoice from the quota/category Fee Structure, flagged with the joining's enrollment; the
	student's advances (unallocated Payment Entries against their customer) are allocated to it."""
	name = fee_structure_for(
		state.program, year, state.oc_admission_quota, state.student_category, state.oc_company
	)
	if not name:
		frappe.throw(
			_("No Fee Structure for {0}, {1}, {2} / {3}.").format(
				state.program, year, state.oc_admission_quota, state.student_category
			)
		)
	structure = frappe.get_doc("Fee Structure", name)
	invoice = frappe.get_doc(
		{
			"doctype": "Sales Invoice", "company": structure.company,
			"selling_price_list": frappe.db.get_single_value("Selling Settings", "selling_price_list"),
			"customer": frappe.db.get_value("Student", student, "customer"), "student": student,
			"posting_date": today(), "due_date": add_days(today(), 30), "debit_to": structure.receivable_account,
			"oc_program_enrollment": enrollment, "allocate_advances_automatically": 1,
			"items": [
				{"item_code": row.item, "qty": 1, "price_list_rate": row.amount, "discount_percentage": row.discount, "cost_center": structure.cost_center}
				for row in structure.components
			],
		}
	)  # fmt: skip
	invoice.flags.ignore_permissions = (
		True  # the officer's role was checked in confirm_joining(); every input is server-side
	)
	invoice.insert()
	return invoice.submit()


def result(student, enrollment):
	register_no, roll_no = frappe.db.get_value("Student", student, ["oc_register_no", "oc_roll_no"])
	return {
		"student": student, "program_enrollment": enrollment, "invoice": admission_invoice(enrollment),
		"register_no": register_no, "roll_no": roll_no,
	}  # fmt: skip


# Program Enrollment validate


def validate_enrollment(doc, method=None):
	"""A new enrollment that only the joining may make is refused. The attendance start date is not defaulted: only
	the joining confirmation writes it (M05-12: it narrows the attendance denominator; blank, the whole term counts).

	That is the first enrollment of an admitted student (lifecycle Admitted, or made from a Student Applicant with no
	submitted enrollment yet) into a program of a college. A legacy import (Data Import, or `flags.legacy_import`)
	may load enrollments directly; later enrollments of a joined student (promotion) and the amendment of a joining
	enrollment are not affected."""
	if doc.is_new() and needs_joining(doc):
		frappe.throw(
			_(
				"{0} has not joined yet: the Program Enrollment is created by the joining confirmation, not by hand."
			).format(frappe.bold(doc.student)),
			title=_("Joining Not Confirmed"),
		)


def needs_joining(doc):
	if doc.flags.campus_joining or doc.flags.legacy_import or frappe.flags.in_import:
		return False
	if not doc.program or not frappe.db.get_value("Program", doc.program, "oc_company"):
		return False
	fields = ["student_applicant"]
	if frappe.db.has_column("Student", "oc_academic_status"):
		fields.append("oc_academic_status")
	student = frappe.db.get_value("Student", doc.student, fields, as_dict=True)
	if not student:
		return False
	if student.get("oc_academic_status") == ADMITTED:
		return True
	if (
		student.get("oc_academic_status") != CANCELLED
		and doc.amended_from
		and frappe.db.get_value("Program Enrollment", doc.amended_from, "oc_joining_key")
	):
		return (
			False  # a joined student's corrected joining enrollment (carry_joining_key moves the key to it)
		)
	return bool(student.student_applicant) and not frappe.db.exists(
		"Program Enrollment", {"student": doc.student, "docstatus": 1}
	)
