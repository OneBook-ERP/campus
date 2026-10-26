"""M06-02 Hall tickets: bulk generation from the Exam Registrations of a session, the withheld list, the portal row.

Each registration gets one Hall Ticket. It is Issued with the eligible courses, their exam dates and the student's
photo, and a Verifiable Document (QR) is issued for it; or it is Withheld with the reasons (hall_ticket_rules)
and has no valid QR. Its QR is revoked with a neutral reason ("Hall ticket withdrawn"): the public /verify page must
never show why (malpractice, detention, dues). One generation run of a session is live at a time. The bulk handler
is DB-only and idempotent: an unchanged ticket is left as it is (same code), a changed one is saved and its QR revoked and issued again; a ticket that becomes withheld has its QR revoked.
Students download from /campus-portal once the session's hall_ticket_release_on has passed (access.py).
"""

from urllib.parse import quote

import frappe
from frappe import _
from frappe.utils import get_datetime, now_datetime

from campus.campus_platform.bulk import live_run, run_bulk
from campus.campus_platform.verification import issue, revoke_for
from campus.examinations import exam_fee
from campus.examinations.access import COE_ROLE
from campus.examinations.doctype.hall_ticket.hall_ticket import ticket_name  # noqa: F401  (public)
from campus.examinations.hall_ticket_rules import withheld_reasons

DOCTYPE, SESSION, REGISTRATION = "Hall Ticket", "Exam Session", "Exam Registration"
PRINT_FORMAT = "Hall Ticket"
RUN_TYPE = "Hall Tickets"
HANDLER = "campus.examinations.hall_tickets.generate_ticket"
ISSUED, WITHHELD = "Issued", "Withheld"
PHOTO_FOLDERS = ("/files/", "/private/files/")


def start(exam_session):
	"""Queue the generation of the session's hall tickets (COE only); returns the Bulk Run name."""
	frappe.only_for((COE_ROLE, "System Manager"))
	session = frappe.get_doc(SESSION, exam_session)
	session.check_permission("write")
	if session.status == "Published":
		frappe.throw(_("A published Exam Session gets no hall tickets."))
	frappe.db.get_value(SESSION, exam_session, "name", for_update=True)  # one start of a session at a time
	running = live_run(RUN_TYPE, exam_session=exam_session)
	if running:
		frappe.throw(_("Hall ticket run {0} of this session is still running.").format(frappe.bold(running)))
	registrations = frappe.get_all(
		REGISTRATION, {"exam_session": exam_session}, pluck="name", order_by="name"
	)
	return run_bulk(RUN_TYPE, registrations, HANDLER, parameters={"exam_session": exam_session})


def generate_ticket(registration, run):
	"""Bulk handler: DB-only, no commit."""
	return generate(registration)


@frappe.whitelist()
def get_ticket_preview(registration):
	"""Return preview ticket_values for client form populating."""
	reg = frappe.get_doc(REGISTRATION, registration)
	reg.check_permission("read")
	return ticket_values(reg)


def generate(registration):
	"""Create or refresh the Hall Ticket of a registration; returns its name."""
	frappe.flags.in_hall_ticket_generation = True
	try:
		reg = frappe.get_doc(REGISTRATION, registration, for_update=True)
		values = ticket_values(reg)
		name = ticket_name(reg.exam_session, reg.student)
		# locking reads: a plain one after the registration lock sees this transaction's older snapshot
		exists = frappe.db.get_value(DOCTYPE, name, "name", for_update=True)
		ticket = frappe.get_doc(DOCTYPE, name, for_update=True) if exists else frappe.new_doc(DOCTYPE)
		changed = not exists or content(ticket) != content(frappe._dict(values))
		if changed:
			ticket.update({**values, "courses": []})
			for row in values["courses"]:
				ticket.append("courses", row)
			ticket.save()
		refresh_qr(ticket, changed)
		return ticket.name
	finally:
		frappe.flags.in_hall_ticket_generation = False


def ticket_values(reg):
	"""The fields of the registration's ticket as they should be now."""
	eligible = reg.eligible_courses()
	dues = exam_fee.exam_dues_block(reg.student, reg.exam_session)
	reasons = withheld_reasons(
		confirmed=reg.confirmed, dues=dues.amount if dues.blocked else 0,
		debarred=reg.debarred and reg.debarment_reason, detained=reg.detained and reg.detention_reason,
		shortage=reg.attendance_status == "Shortage", eligible_courses=len(eligible),
	)  # fmt: skip
	return {
		"exam_registration": reg.name, "exam_session": reg.exam_session, "student": reg.student,
		"student_name": reg.student_name, "program": reg.program, "company": reg.company,
		"academic_term": reg.academic_term, "photo": file_url(frappe.db.get_value("Student", reg.student, "image")),
		"status": WITHHELD if reasons else ISSUED, "withheld_reasons": "\n".join(reasons) or None,
		"courses": [] if reasons else scheduled_courses(reg.exam_session, eligible),
	}  # fmt: skip


def file_url(url):
	"""`url` when it points into the site's files folders, else None (a print never loads a foreign or script URL)."""
	return url if url and url.startswith(PHOTO_FOLDERS) else None


def scheduled_courses(exam_session, eligible):
	"""[{course, kind, exam_date, slot}] in exam order; an eligible course with no exam date fails (fail closed)."""
	schedule = {
		row.course: row
		for row in frappe.get_all(
			"Exam Schedule", {"parent": exam_session, "parenttype": SESSION}, ["course", "exam_date", "slot"]
		)
	}
	missing = [row.course for row in eligible if row.course not in schedule]
	if missing:
		frappe.throw(
			_("No exam date is scheduled for {0} in {1}.").format(
				", ".join(missing), frappe.bold(exam_session)
			)
		)
	rows = [
		{
			"course": r.course,
			"kind": r.kind,
			"exam_date": schedule[r.course].exam_date,
			"slot": schedule[r.course].slot,
		}
		for r in eligible
	]
	return sorted(rows, key=lambda row: (str(row["exam_date"]), row["slot"] != "FN", row["course"]))


def content(ticket):
	"""What a ticket shows: the comparison that decides whether it changed."""
	courses = tuple(
		(r.get("course"), r.get("kind"), str(r.get("exam_date")), r.get("slot"))
		for r in ticket.get("courses") or []
	)
	fields = ("status", "withheld_reasons", "photo", "student_name", "program", "academic_term")
	return tuple(ticket.get(f) or None for f in fields) + (courses,)


def refresh_qr(ticket, changed):
	"""Withheld: revoke any valid QR. Issued: revoke and issue again when the ticket changed or has none valid."""
	if ticket.status == WITHHELD:
		revoke_for(ticket, _("Hall ticket withdrawn"))  # never the withheld reasons: the record is public
		return
	valid = frappe.db.get_value(
		"Verifiable Document", {"reference_doctype": DOCTYPE, "reference_name": ticket.name, "status": "Valid"}, "name",
		for_update=True,
	)  # fmt: skip
	if valid and not changed:
		return
	revoke_for(ticket, _("Re-issued: the hall ticket changed"))
	issue(ticket, PRINT_FORMAT)


# ---- portal -------------------------------------------------------------------------------


def portal_row(registration):
	"""{status, reasons, release_on, download_url} of the registration's ticket for its student, or None.

	The caller (the portal) has resolved the student; the download itself is gated again by access.py."""
	ticket = frappe.db.get_value(
		DOCTYPE,
		{"exam_registration": registration},
		["name", "status", "withheld_reasons", "exam_session"],
		as_dict=True,
	)
	if not ticket:
		return None
	release_on = frappe.db.get_value(SESSION, ticket.exam_session, "hall_ticket_release_on")
	downloadable = ticket.status == ISSUED and is_released(release_on)
	return {
		"status": ticket.status,
		"reasons": ticket.withheld_reasons,
		"release_on": release_on,
		"download_url": download_url(ticket.name) if downloadable else None,
	}


def is_released(release_on):
	return bool(release_on) and get_datetime(release_on) <= now_datetime()


def download_url(name):
	return (
		"/api/method/frappe.utils.print_format.download_pdf?doctype=Hall%20Ticket"
		f"&name={quote(name)}&format={quote(PRINT_FORMAT)}"
	)
