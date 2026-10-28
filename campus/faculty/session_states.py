"""M05-12 Class session states on a Course Schedule: Scheduled, Conducted, Cancelled, Rescheduled (to the new session),
Makeup (for the session it replaces). How each state counts is campus.student_services.attendance_rules.

A change of a session's state or of its Rescheduled-to / Makeup-for link that touches a marked session (the session
itself, or the session a link points at, before or after) needs a new reason, and the change is logged as a comment on
the session and on each marked session it touches: a Makeup drops the marks of the session it replaces. A session has
at most one Makeup. Marking attendance makes a Scheduled session Conducted; a Cancelled or Rescheduled one cannot be
marked.
"""

import frappe
from frappe import _
from frappe.utils import today

from campus.student_services.attendance_rules import CANCELLED, CONDUCTED, MAKEUP, RESCHEDULED, SCHEDULED

NOT_HELD = (CANCELLED, RESCHEDULED)
LINKS = {RESCHEDULED: "oc_rescheduled_to", MAKEUP: "oc_makeup_for"}


def validate_session(doc, method=None):
	"""Course Schedule validate: the state's link is set (to another session of the group) and the others cleared; a
	change touching a marked session needs a new reason."""
	session_state = getattr(doc, "oc_session_state", None) or doc.get("oc_session_state") or SCHEDULED
	doc.oc_session_state = session_state
	for state, fieldname in LINKS.items():
		if doc.oc_session_state != state:
			doc.set(fieldname, None)
		else:
			check_link(doc, fieldname)
	if doc.oc_session_state == MAKEUP:
		refuse_second_makeup(doc)
	if not any(doc.has_value_changed(f) for f in ("oc_session_state", *LINKS.values())):
		return
	touched = marked_sessions_touched(doc)
	if not touched:
		return
	state_reason = getattr(doc, "oc_state_reason", None) or doc.get("oc_state_reason")
	if not (state_reason or "").strip() or not doc.has_value_changed("oc_state_reason"):
		frappe.throw(
			_("Attendance is marked for {0}: give the reason for changing the state of {1}.").format(
				", ".join(touched), doc.name or _("this session")
			)
		)
	doc.flags.log_state_change = touched


def marked_sessions_touched(doc):
	"""The marked sessions among this one (when saved before) and those its links point at, before and after."""
	before = doc.get_doc_before_save()
	names = {doc.get(f) for f in LINKS.values()} | {before.get(f) for f in LINKS.values() if before}
	if not doc.is_new():
		names.add(doc.name)
	return sorted(name for name in names if name and is_marked(name))


def refuse_second_makeup(doc):
	"""One Makeup per session: a second would count the period twice. The replaced session's row lock serialises
	two Makeups saved at once; the check re-reads with a locking read."""
	target = doc.oc_makeup_for
	frappe.db.get_value("Course Schedule", target, "name", for_update=True)
	other = frappe.db.get_value(
		"Course Schedule",
		{"oc_session_state": MAKEUP, "oc_makeup_for": target, "name": ["!=", doc.name or ""]},
		"name",
		for_update=True,
	)
	if other:
		frappe.throw(_("{0} already has a Makeup session: {1}.").format(target, other))


def check_link(doc, fieldname):
	label = _(doc.meta.get_label(fieldname))
	other = doc.get(fieldname)
	if not other:
		frappe.throw(_("{0} is needed for a {1} session.").format(label, _(doc.oc_session_state)))
	if (
		other == doc.name
		or frappe.db.get_value("Course Schedule", other, "student_group") != doc.student_group
	):
		frappe.throw(_("{0} must be another session of {1}.").format(label, doc.student_group))


def log_state_change(doc, method=None):
	"""Course Schedule on_update: the comments recording a change touching marked sessions, on this session and on
	each touched one."""
	touched = doc.flags.log_state_change
	if not touched:
		return
	doc.flags.log_state_change = None
	before, reason = describe(doc.get_doc_before_save()), doc.oc_state_reason.strip()
	doc.add_comment(
		"Info", _("Session state changed from {0} to {1}: {2}").format(before, describe(doc), reason)
	)
	for name in touched:
		if name != doc.name:
			frappe.get_doc("Course Schedule", name).add_comment(
				"Info",
				_("{0} changed from {1} to {2}, which concerns this session: {3}").format(
					doc.name, before, describe(doc), reason
				),
			)


def describe(doc):
	"""The state with its link, e.g. "Makeup for EDU-CSH-0001"; a new session's before is Scheduled."""
	state = (doc and doc.oc_session_state) or SCHEDULED
	link = doc and doc.get(LINKS.get(state, ""))
	if state == MAKEUP and link:
		return _("Makeup for {0}").format(link)
	if state == RESCHEDULED and link:
		return _("Rescheduled to {0}").format(link)
	return _(state)


def is_marked(course_schedule):
	return bool(frappe.db.exists("Student Attendance", {"course_schedule": course_schedule, "docstatus": 1}))


def check_markable(course_schedule, state):
	"""Refuse marking a session that was not held."""
	if state in NOT_HELD:
		frappe.throw(_("{0} is {1}; its attendance cannot be marked.").format(course_schedule, _(state)))


def check_held(course_schedule, state):
	"""check_markable, and refuse a session a held Makeup replaces (an approved correction re-checks this)."""
	check_markable(course_schedule, state)
	makeup = frappe.db.get_value(
		"Course Schedule",
		{"oc_session_state": MAKEUP, "oc_makeup_for": course_schedule, "schedule_date": ["<=", today()]},
		"name",
	)
	if makeup:
		frappe.throw(
			_("{0} is replaced by the Makeup session {1}; its attendance cannot be changed.").format(
				course_schedule, makeup
			)
		)


def mark_conducted(course_schedule, state):
	"""Marking a Scheduled (or blank) session makes it Conducted."""
	if (state or SCHEDULED) == SCHEDULED:
		frappe.db.set_value("Course Schedule", course_schedule, "oc_session_state", CONDUCTED)
