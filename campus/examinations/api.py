"""M12-02 Examinations service API. Other modules call these, never the controllers (D7)."""

import frappe
from frappe import _
from frappe.utils import get_datetime, now_datetime

from campus.examinations import results
from campus.examinations.access import (  # noqa: F401  (public API)
	held_scopes,
	instructed_offerings,
	instructor_users,
	mentees_of,
	students_of,
)
from campus.examinations.arrears import arrear_courses, has_passed, passed_courses  # noqa: F401  (public API)
from campus.examinations.registration_rules import TERM_KINDS  # noqa: F401  (public API)
from campus.examinations.rules import WITHDRAWAL_CODES  # noqa: F401  (public API)

# Exam registration and its fee are imported where used: they import campus_finance.api, which imports this module.


@frappe.whitelist(methods=["POST"])
def run_results(exam_session, program):
	"""Queue the Result Run for `program` in `exam_session` (COE only); returns the Bulk Run name.

	Idempotent: before publication a re-run replaces the Student Results of the session; after it, a re-run makes a
	new version of each changed result (P2-T19).
	"""
	return results.start_run(exam_session, program)


@frappe.whitelist(methods=["POST"])
def issue_grade_sheet(student_result):
	"""Issue a verifiable Grade Sheet (QR) for a published Student Result; returns the Verifiable Document."""
	from campus.campus_platform.verification import issue
	from campus.examinations.access import MARKS_ROLES

	frappe.only_for(MARKS_ROLES)
	doc = frappe.get_doc("Student Result", student_result)
	doc.check_permission("read")
	status, publish_at = frappe.db.get_value("Exam Session", doc.exam_session, ["status", "publish_at"])
	if status != "Published" or not publish_at or get_datetime(publish_at) > now_datetime():
		frappe.throw(
			_("A Grade Sheet is issued only for a published Exam Session whose publication time has passed.")
		)
	return issue(doc, "Grade Sheet").name


@frappe.whitelist(methods=["POST"])
def fill_convocation_list(convocation_list):
	"""Fill a Convocation List with the eligible graduates (write access to it); returns their number (P2-T22)."""
	from campus.examinations import convocation

	return convocation.fill_graduates(convocation_list)


@frappe.whitelist()
def download_convocation_list(convocation_list):
	"""The Convocation List as CSV in its column format (read access to it)."""
	from campus.examinations import convocation

	convocation.download(convocation_list)


@frappe.whitelist(methods=["POST"])
def graduate_convocation_list(convocation_list):
	"""Record the list's Award Eligible graduates as Graduated from its convocation date (COE); returns {graduated,
	refused: [{student, reason}]} (L-01)."""
	from campus.examinations import convocation

	return convocation.graduate(convocation_list)


@frappe.whitelist(methods=["POST"])
def rerun_progression(progression_run):
	"""Queue a submitted Progression Run again (COE only, idempotent); returns the Bulk Run name."""
	from campus.examinations import progression

	return progression.rerun(progression_run)


def desk_only_certificate_types():
	"""Certificate Types requested on the desk only, never in the portal (the Degree Data Sheet, P2-T22)."""
	from campus.examinations.exam_certificates import desk_only_types

	return desk_only_types()


@frappe.whitelist(methods=["POST"])
def start_registration(exam_session, program=None):
	"""Queue the bulk Exam Registration of the session (COE only); returns the Bulk Run name.

	Idempotent: a re-run adds only the missing courses of unconfirmed registrations.
	"""
	from campus.examinations import registration

	return registration.start(exam_session, program or None)


@frappe.whitelist(methods=["POST"])
def generate_hall_tickets(exam_session):
	"""Queue the bulk Hall Ticket generation of the session (COE only); returns the Bulk Run name.

	Idempotent: unchanged tickets keep their QR; changed ones are re-issued, newly withheld ones revoked.
	"""
	from campus.examinations import hall_tickets

	return hall_tickets.start(exam_session)


@frappe.whitelist(methods=["POST"])
def reraise_exam_fee_invoices(exam_session):
	"""Queue the missing exam fee invoices of the session's confirmed registrations (COE only); returns the Bulk Run
	name or None. Idempotent: a registration gets at most one invoice."""
	from campus.examinations import exam_fee

	return exam_fee.reraise_invoices(exam_session)


@frappe.whitelist(methods=["POST"])
def generate_seating(seating_plan):
	"""Seat the plan's students in its Exam Halls (COE only); returns the number seated. Refused once attendance
	is marked."""
	from campus.examinations import seating

	return seating.generate(seating_plan)


@frappe.whitelist(methods=["POST"])
def mark_exam_absent(seating_plan, students, value=1):
	"""Mark (1) or clear (0) students absent on a Seating Plan (COE only); the Result Run grades them AB."""
	from campus.examinations import seating

	return seating.mark_absent(seating_plan, students, value)


@frappe.whitelist(methods=["POST"])
def generate_invigilation(roster):
	"""Build the Invigilation Roster's duties from the session's Seating Plans (COE only); returns the count."""
	from campus.examinations import invigilation

	return invigilation.generate(roster)


def exam_dues_block(student, exam_session):
	"""{blocked, amount}: the hall ticket gate. Unpaid Exam-head dues in the session's company block a hall ticket
	(never the registration itself)."""
	from campus.examinations import exam_fee

	return exam_fee.exam_dues_block(student, exam_session)


def confirm_registration(registration_name):
	"""Confirm an Exam Registration (the caller checks who may): fixes the fee and queues the invoice; returns it."""
	from campus.examinations import registration

	return registration.confirm(registration_name)


def student_registrations(student):
	"""The student's Exam Registrations of unpublished sessions as plain dicts (the caller resolves the student)."""
	from campus.examinations import registration

	return registration.student_registrations(student)


def refresh_student_result(student, exam_session):
	"""Recompute the student's Student Result of the session when the Result Run has made one (an approved or
	cancelled exam withdrawal changes a course's code). After publication this makes a new result version. An
	Affiliated session's results are the university's (M06-10): nothing is recomputed."""
	from campus.examinations.affiliation import AFFILIATED, session_mode

	if session_mode(exam_session) == AFFILIATED:
		return
	if frappe.db.exists(results.RESULT, {"student": student, "exam_session": exam_session}):
		results.compute_student_result(student, exam_session)


def cia_lock_state(cia_schedule):
	"""Open before the CIA entry deadline, Unlocked inside an approved unlock window, else Locked."""
	from campus.examinations import cia

	return cia.lock_state(cia_schedule)


def evaluation_grievances(company, academic_year, programs=None):
	"""{value, records}: distinct students with a submitted Revaluation or Review application in the company's
	published sessions of the academic year (AQAR 2.5.2; `programs` narrows by the application's program)."""
	from campus.examinations import revaluation

	return revaluation.evaluation_grievances(company, academic_year, programs)


def students_appeared(company, academic_year, programs=None):
	"""{value, records}: distinct students who sat a course in the company's published sessions of the year (M17-06)."""
	from campus.examinations import coe_reports

	return coe_reports.students_appeared(company, academic_year, programs)


def pass_percentage(company, academic_year, programs=None):
	"""{value, records}: % of the final-year results that appeared and passed every course (AQAR 2.6.3)."""
	from campus.examinations import coe_reports

	return coe_reports.pass_percentage(company, academic_year, programs)


def days_exam_to_result(company, academic_year, programs=None):
	"""{value, records}: average days from the last exam to the publication of the year's sessions (AQAR 2.5.1)."""
	from campus.examinations import coe_reports

	return coe_reports.days_exam_to_result(company, academic_year, programs)


def cgpa(student, courses=None):
	"""The student's CGPA from published results (latest, ratified versions), over `courses` only when given."""
	from campus.examinations import graduation

	return graduation.cgpa(student, courses)


def had_arrears(student):
	"""Whether the student ever failed or missed a course at its first appearance (published results), cleared or not."""
	from campus.examinations import graduation

	return graduation.had_arrears(student)


def released_sessions(company, academic_year=None, final_year=False):
	"""Names of the company's released Exam Sessions (Published, publish_at passed), optionally of one academic year
	and final-year only: the session reader every examination report and data point shares (P2-T24 fix 1)."""
	from campus.examinations import coe_reports

	return [s.name for s in coe_reports.released_sessions(company, academic_year, final_year=final_year)]


def revaluation_options(student):
	"""The student's released results whose photocopy / revaluation window is open, with the eligible courses (the
	caller, the portal, resolves the student). P2-T25."""
	from campus.examinations import student_revaluation

	return student_revaluation.options(student)


def student_revaluations(student):
	"""The student's photocopy, revaluation and review applications with their portal status (no marks)."""
	from campus.examinations import student_revaluation

	return student_revaluation.applications(student)


def apply_revaluation(student, exam_session, application_type, courses):
	"""Make the student's application under the revaluation rules, submitted unless a justification is due."""
	from campus.examinations import student_revaluation

	return student_revaluation.apply(student, exam_session, application_type, courses)


def submit_revaluation(student, application):
	"""Submit the student's own justified draft revaluation; its fee invoice follows."""
	from campus.examinations import student_revaluation

	return student_revaluation.submit(student, application)


def offering_assessment(offering):
	"""M03-03 The offering's CO-attainment inputs: {exam_sessions, students, cia, ese} with question-wise rows
	{student, question, max_marks, marks, course_outcome, absent}, from released, ratified results only (P3-T10)."""
	from campus.examinations import attainment_marks

	return attainment_marks.offering_assessment(offering)


def next_payroll_date(employee, company, posting_date):
	"""The payroll date of a one-off earning posted on `posting_date`: that date, or the day after the employee's last
	submitted Salary Slip of `company` if later (as examiner remuneration). A drafted slip of the month picks the earning
	up only when it is recalculated. Used by consultancy revenue shares (P3-T20)."""
	from campus.examinations import remuneration_payment, remuneration_rules

	return remuneration_rules.next_payroll_date(
		posting_date, remuneration_payment.last_processed_slip_end(employee, company)
	)


@frappe.whitelist()
@frappe.validate_and_sanitize_search_inputs
def get_students_for_assessment_result(
	doctype: str,
	txt: str,
	searchfield: str,
	start: int,
	page_len: int,
	filters: dict | None = None,
):
	"""Returns students belonging to the student_group or enrolled in the program for Assessment Result."""
	frappe.has_permission("Assessment Result", "read", throw=True)
	filters = filters or {}
	student_group = filters.get("student_group")
	program = filters.get("program")

	if not student_group and not program:
		return []

	Student = frappe.qb.DocType("Student")

	if student_group:
		group_company = frappe.db.get_value("Student Group", student_group, "oc_company")
		StudentGroupStudent = frappe.qb.DocType("Student Group Student")
		query = (
			frappe.qb.from_(Student)
			.inner_join(StudentGroupStudent)
			.on(StudentGroupStudent.student == Student.name)
			.select(Student.name, Student.student_name)
			.where(
				(StudentGroupStudent.parent == student_group)
				& (StudentGroupStudent.active == 1)
				& (Student.enabled == 1)
			)
		)
		if group_company:
			query = query.where(Student.oc_company == group_company)
	else:
		program_company = frappe.db.get_value("Program", program, "oc_company")
		ProgramEnrollment = frappe.qb.DocType("Program Enrollment")
		query = (
			frappe.qb.from_(Student)
			.inner_join(ProgramEnrollment)
			.on(ProgramEnrollment.student == Student.name)
			.select(Student.name, Student.student_name)
			.where(
				(ProgramEnrollment.program == program)
				& (ProgramEnrollment.docstatus != 2)
				& (Student.enabled == 1)
			)
			.distinct()
		)
		if program_company:
			query = query.where(Student.oc_company == program_company)

	if txt:
		query = query.where((Student.name.like(f"%{txt}%")) | (Student.student_name.like(f"%{txt}%")))

	query = query.orderby(Student.student_name).orderby(Student.name).offset(start).limit(page_len)
	return [list(row) for row in query.run()]
