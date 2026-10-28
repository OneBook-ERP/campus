"""M05-09 Student lifecycle service: the one way a Student's lifecycle status (Student.oc_academic_status) changes.

`transition` takes the Student row lock, checks the move against the enabled Lifecycle Transition Rules (the
configurable table, seeded with lifecycle_rules.DEFAULT_TRANSITIONS) and the effective date against the student's
last change, sets the status and writes a Lifecycle Transition (from, to, effective date, reason, source document,
approved by, program enrollment). A retry from the same source returns the earlier record. `revert` undoes a
source's latest transition with a Reversal record, only while it is still the student's latest change.

A new Student starts in a status the table permits from blank (Initial record); saving a Student that changes the
status is refused (validate_student). Flows: admit (Admitted, Joined, Active), Admission Cancellation (Cancelled),
Break of Study (On Break; cancel reverts), Rejoin Application (back to the status before the break), transfer
certificate (Withdrawn when still on roll; cancel reverts), Convocation List (Study Completed, Award Eligible,
Graduated). migrate_students gives students recorded before L-01 their status from their records (Migrated).
"""

import frappe
from frappe import _
from frappe.utils import getdate, today

from campus.hostel.api import vacate_for_student
from campus.student_services import lifecycle_rules as rules
from campus.transport.api import vacate_seat_for_student

LOG, RULE, FIELD = "Lifecycle Transition", "Lifecycle Transition Rule", "oc_academic_status"
DETAINED = "oc_detained"  # progression's detention flag (not a lifecycle status)
TRANSITION, INITIAL, MIGRATED, REVERSAL = "Transition", "Initial", "Migrated", "Reversal"
NEW_RECORD = "New Record"  # the rule name part for a blank From Status
SIDE_EXIT_STATUSES = (rules.ON_BREAK, rules.WITHDRAWN, rules.DISCONTINUED, rules.CANCELLED)


def rule_name(from_status, to_status):
	return f"{from_status or NEW_RECORD} to {to_status}"


def transition(
	student,
	to_status,
	effective_date=None,
	reason=None,
	source=None,
	approved_by=None,
	program_enrollment=None,
):
	"""Move `student` to `to_status` from `effective_date` (default today); returns the Lifecycle Transition name.

	`source` is the document causing the change (a Document or (doctype, name)). `approved_by` defaults to the
	session user, `program_enrollment` to the student's latest submitted one. Throws when the table does not
	permit the move or the date is before the student's last live change."""
	source_doctype, source_name = source_ref(source)
	current = lock_student(student)
	latest = latest_transition(student)
	if (
		latest
		and source_name
		and (latest.source_doctype, latest.source_name, latest.to_status)
		== (source_doctype, source_name, to_status)
	):
		if current.status == to_status:
			return latest.name  # a retry of the same change
	effective = getdate(effective_date or today())
	check(student, current.status, to_status, effective, latest)
	return write(
		student, current, to_status, effective, TRANSITION, reason, (source_doctype, source_name),
		approved_by or frappe.session.user, program_enrollment,
	)  # fmt: skip


def revert(source, reason, student=None, legacy_status=None, statuses=None):
	"""Undo the change `source` made to a student (its latest live record) with a Reversal record; returns the
	Reversal name, or None when the source has no live change of the student.

	`student` defaults to the one student the source changed. A Migrated record (the patch's, naming the source
	as its evidence) goes back to `legacy_status`, since its From is a pre-L-01 academic status. With `statuses`
	(a tidy-up such as a Convocation List refill, which must never abort): only a change to one of them is undone,
	a source whose change is no longer the latest is left alone (None), and a Migrated record with no
	`legacy_status` goes back to its From mapped onto the lifecycle statuses (lifecycle_rules.migrated_status), or
	is left alone when that is its current status. Otherwise a change that is no longer the latest, or a Migrated
	record with no `legacy_status`, throws. An Initial record is never undone."""
	source_doctype, source_name = source_ref(source)
	student = student or frappe.db.get_value(
		LOG, {"source_doctype": source_doctype, "source_name": source_name}, "student"
	)
	if not student:
		return None
	current = lock_student(student)
	live = live_history(student)
	from_source = [
		row for row in live if (row.source_doctype, row.source_name) == (source_doctype, source_name)
	]
	if not from_source:
		return None
	latest = live[-1]
	if latest.name != from_source[-1].name or current.status != latest.to_status:
		if statuses:
			return None
		frappe.throw(
			_("{0} {1} cannot be undone: the lifecycle status of {2} has changed since ({3}).").format(
				_(source_doctype), source_name, frappe.bold(student), current.status
			)
		)
	if statuses and latest.to_status not in statuses:
		return None
	if statuses and latest.kind == MIGRATED and not legacy_status:
		legacy_status = rules.migrated_status(latest.from_status)
		if legacy_status == current.status:
			return None
	to_status = restored_status(latest, legacy_status)
	return write(
		student, current, to_status, latest.effective_date, REVERSAL, reason, (source_doctype, source_name),
		frappe.session.user, latest.program_enrollment, reverses=latest.name,
	)  # fmt: skip


def restored_status(record, legacy_status):
	"""The status a reversal of `record` restores."""
	if record.kind == TRANSITION:
		return record.from_status
	if record.kind == MIGRATED and legacy_status:
		return legacy_status
	frappe.throw(
		_("The {0} record {1} of {2} cannot be undone automatically.").format(
			_(record.kind), record.name, frappe.bold(record.student)
		)
	)


def current_status(student):
	return frappe.db.get_value("Student", student, FIELD)


def check(student, from_status, to_status, effective, latest):
	problem = rules.refusal(
		permitted_moves(), from_status, to_status, effective, latest and latest.effective_date
	)
	if problem == rules.UNKNOWN_STATUS:
		frappe.throw(_("{0} is not a lifecycle status.").format(frappe.bold(to_status)))
	if problem == rules.SAME_STATUS:
		frappe.throw(_("{0} is already {1}.").format(frappe.bold(student), _(to_status)))
	if problem == rules.NOT_PERMITTED:
		frappe.throw(
			_("{0} cannot move from {1} to {2}: no enabled Lifecycle Transition Rule permits it.").format(
				frappe.bold(student), from_status or _("a new record"), to_status
			)
		)
	if problem == rules.BEFORE_LATEST:
		frappe.throw(
			_("The effective date {0} is before the last status change of {1} ({2} from {3}).").format(
				frappe.format(effective, "Date"),
				frappe.bold(student),
				latest.to_status,
				frappe.format(latest.effective_date, "Date"),
			)
		)


def permitted_moves():
	return {
		(row.from_status or rules.NEW, row.to_status)
		for row in frappe.get_all(RULE, {"enabled": 1}, ["from_status", "to_status"])
	}


def lock_student(student):
	"""The Student row lock (one status change at a time) with the status and company read under it."""
	row = frappe.db.get_value("Student", student, [FIELD, "oc_company"], as_dict=True, for_update=True)
	if not row:
		raise frappe.DoesNotExistError(_("Student {0} does not exist.").format(student))
	return frappe._dict(status=row.get(FIELD), company=row.oc_company)


def latest_transition(student):
	"""The student's last live Lifecycle Transition (see live_history), or None."""
	live = live_history(student)
	return live[-1] if live else None


def live_history(student, lock=True):
	"""The student's live Lifecycle Transitions, oldest first: every record except Reversals and the records they
	reverse. A locking read by default (take the Student lock first)."""
	log = frappe.qb.DocType(LOG)
	query = (
		frappe.qb.from_(log)
		.select(
			log.name,
			log.student,
			log.kind,
			log.from_status,
			log.to_status,
			log.effective_date,
			log.source_doctype,
			log.source_name,
			log.program_enrollment,
			log.reverses,
		)  # fmt: skip
		.where(log.student == student)
		.orderby(log.creation)
		.orderby(log.name)
	)
	rows = (query.for_update() if lock else query).run(as_dict=True)
	reversed_names = {row.reverses for row in rows if row.kind == REVERSAL}
	return [row for row in rows if row.kind != REVERSAL and row.name not in reversed_names]


def last_change_date(student):
	"""Effective date of the student's last live change (a plain read: transition() checks again under the lock)."""
	live = live_history(student, lock=False)
	return live[-1].effective_date if live else None


def write(
	student,
	current,
	to_status,
	effective,
	kind,
	reason,
	source,
	approved_by,
	program_enrollment,
	reverses=None,
):
	frappe.db.set_value("Student", student, FIELD, to_status)
	log = insert_log(
		student, current.company, current.status, to_status, effective, kind, reason, source, approved_by,
		program_enrollment or latest_enrollment(student), reverses,
	)  # fmt: skip
	if to_status in SIDE_EXIT_STATUSES:
		on_side_exit(student, to_status, effective)
	return log.name


def on_side_exit(student, to_status=None, effective_date=None):
	"""End active hostel and transport allocations when a student moves to On Break, Withdrawn, Discontinued, or Cancelled."""
	vacate_for_student(student, end_date=effective_date)
	vacate_seat_for_student(student, end_date=effective_date)


def insert_log(
	student,
	company,
	from_status,
	to_status,
	effective,
	kind,
	reason,
	source,
	approved_by,
	program_enrollment,
	reverses=None,
):
	doc = frappe.get_doc(
		{
			"doctype": LOG, "student": student, "company": company, "from_status": from_status or rules.NEW,
			"to_status": to_status, "effective_date": effective, "kind": kind, "reason": reason,
			"source_doctype": source[0], "source_name": source[1], "approved_by": approved_by,
			"program_enrollment": program_enrollment, "reverses": reverses,
		}
	)  # fmt: skip
	doc.flags.from_lifecycle_service = True
	doc.flags.ignore_permissions = True
	doc.flags.ignore_links = True  # a cancelled enrollment or source stays the record's reference
	return doc.insert()


def latest_enrollment(student):
	return frappe.db.get_value(
		"Program Enrollment",
		{"student": student, "docstatus": 1},
		"name",
		order_by="enrollment_date desc, creation desc",
	)


def source_ref(source):
	if source is None:
		return None, None
	if isinstance(source, (tuple, list)):
		return source[0], source[1]
	return source.doctype, source.name


# Student hooks


def validate_guardians(doc):
	guardians = [row.get("guardian") for row in (doc.get("guardians") or []) if row.get("guardian")]
	if len(guardians) != len(set(guardians)):
		frappe.throw(_("Duplicate guardians are not allowed."))


def validate_student(doc, method=None):
	"""A new Student starts in a status the table permits from blank and not detained; afterwards a save never
	changes either (the lifecycle service and the Progression Run write them)."""
	validate_guardians(doc)
	if doc.is_new():
		doc.set(FIELD, doc.get(FIELD) or rules.ACTIVE)
		problem = rules.refusal(permitted_moves(), rules.NEW, doc.get(FIELD), None, None)
		if problem:
			frappe.throw(
				_(
					"A new student cannot start as {0}: no enabled Lifecycle Transition Rule permits it."
				).format(_(doc.get(FIELD)))
			)
		doc.set(DETAINED, 0)  # only the Progression Run detains
	elif doc.has_value_changed(FIELD):
		frappe.throw(
			_(
				"The lifecycle status changes only through a lifecycle transition (admission, break, rejoin, cancellation, TC, convocation), not by editing the Student."
			)
		)
	elif doc.has_value_changed(DETAINED):
		frappe.throw(_("Detained is set by the Progression Run only, not by editing the Student."))


def log_initial_status(doc, method=None):
	"""The Initial record of a new Student, dated from the start of its record; the source is its applicant."""
	source = ("Student Applicant", doc.student_applicant) if doc.get("student_applicant") else (None, None)
	insert_log(
		doc.name, doc.get("oc_company"), rules.NEW, doc.get(FIELD), start_date(doc.joining_date, doc.get("oc_batch")),
		INITIAL, _("New student record"), source, frappe.session.user, None,
	)  # fmt: skip


def start_date(joining_date, batch, fallback=None):
	"""When the student's record starts: the earlier of the joining date (Education defaults it to the day the
	record is made) and the start of the batch (admission year); else `fallback`, else today."""
	batch_start = batch and frappe.db.get_value("Academic Year", batch, "year_start_date")
	dates = [getdate(d) for d in (joining_date, batch_start) if d]
	return min(dates) if dates else getdate(fallback or today())


# Migration of students recorded before L-01 (patch student_lifecycle_status)


def migrate_students():
	"""Give every student without a Lifecycle Transition its status from its records (one Migrated record each)."""
	recorded = set(frappe.get_all(LOG, pluck="student", distinct=True))
	for student in frappe.get_all("Student", pluck="name", order_by="creation asc"):
		if student not in recorded:
			migrate_student(student)
	migrate_break_statuses()


def migrate_break_statuses():
	"""Map the pre-L-01 academic statuses kept on Break of Study (previous_status) onto lifecycle statuses, so a
	rejoin restores a valid one (Long Absent and Detained: Active). A student detained before a break still open
	keeps the detention (oc_detained). Idempotent: mapped values are left alone."""
	breaks = frappe.get_all(
		"Break of Study",
		{"previous_status": ["not in", [*rules.STATUSES, ""]]},
		["name", "student", "docstatus", "previous_status"],
	)
	for row in breaks:
		if row.previous_status == rules.LEGACY_DETAINED and row.docstatus == 1 and open_break(row.student):
			frappe.db.set_value(
				"Student", row.student, DETAINED, 1, update_modified=False
			)  # detained before the break
		frappe.db.set_value(
			"Break of Study",
			row.name,
			"previous_status",
			rules.migrated_status(row.previous_status),
			update_modified=False,
		)


def migrate_student(student):
	"""Derive and record the lifecycle status of a student recorded before L-01; returns the status."""
	current = lock_student(student)
	facts = legacy_facts(student)
	status = rules.migrated_status(
		current.status, cancelled=bool(facts.cancellation), left=bool(facts.leaving_certificate_number),
		open_break=bool(facts.open_break), convocation=bool(facts.convocation),
	)  # fmt: skip
	source, effective, evidence = migration_evidence(status, facts)
	detained = rules.LEGACY_DETAINED in (
		current.status,
		facts.open_break and facts.open_break.previous_status,
	)
	frappe.db.set_value("Student", student, {FIELD: status, DETAINED: int(detained)}, update_modified=False)
	reason = _("Migrated from the academic status {0}").format(current.status or _("(blank)"))
	insert_log(
		student, current.company, current.status, status, effective, MIGRATED, f"{reason}{evidence}", source,
		frappe.session.user, latest_enrollment(student),
	)  # fmt: skip
	return status


def legacy_facts(student):
	row = frappe.db.get_value(
		"Student",
		student,
		["joining_date", "oc_batch", "creation", "leaving_certificate_number", "date_of_leaving"],
		as_dict=True,
	)
	row.cancellation = frappe.db.get_value(
		"Admission Cancellation",
		{"student": student, "docstatus": 1},
		["name", "cancellation_date"],
		as_dict=True,
	)
	row.tc = row.leaving_certificate_number and frappe.db.get_value(
		"Certificate Request",
		{"student": student, "certificate_number": row.leaving_certificate_number, "docstatus": 1},
		"name",
	)
	row.open_break = open_break(student)
	parent = frappe.db.get_value(
		"Convocation List Graduate", {"student": student, "parenttype": "Convocation List"}, "parent"
	)
	row.convocation = parent and frappe.db.get_value(
		"Convocation List", parent, ["name", "convocation_date", "creation"], as_dict=True
	)
	return row


def open_break(student):
	"""The student's approved Break of Study with no approved rejoin, or None."""
	if not frappe.db.table_exists("Break of Study"):
		return None
	rejoined = frappe.get_all(
		"Rejoin Application", {"student": student, "docstatus": 1}, pluck="break_of_study"
	)
	return frappe.db.get_value(
		"Break of Study",
		{"student": student, "docstatus": 1, "name": ["not in", rejoined or [""]]},
		["name", "from_date", "previous_status"],
		as_dict=True,
	)


def migration_evidence(status, facts):
	"""(source, effective date, reason suffix) of a migrated status from the record that shows it."""
	started = start_date(facts.joining_date, facts.oc_batch, facts.creation)
	if status == rules.CANCELLED:
		return (
			("Admission Cancellation", facts.cancellation.name),
			getdate(facts.cancellation.cancellation_date),
			"",
		)
	if status == rules.AWARD_ELIGIBLE:
		return ("Convocation List", facts.convocation.name), listing_date(facts.convocation), ""
	if status == rules.WITHDRAWN:
		source = (
			("Certificate Request", facts.tc) if facts.tc else (None, None)
		)  # a TC number with no request: no source
		return (
			source,
			getdate(facts.date_of_leaving or started),
			_(" (transfer certificate {0})").format(facts.leaving_certificate_number),
		)
	if status == rules.ON_BREAK and facts.open_break:
		return ("Break of Study", facts.open_break.name), getdate(facts.open_break.from_date), ""
	return (None, None), started, ""


def listing_date(convocation):
	"""When a Convocation List made its graduates award eligible: its convocation date, else the day it was made
	(never after today), so a later Mark Graduated on the convocation date is not refused."""
	return min(getdate(convocation.convocation_date or convocation.creation), getdate(today()))
