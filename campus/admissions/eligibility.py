"""M04-02 Eligibility evaluation from Eligibility Rule records.

Percentage is total obtained / total maximum over the rule's required subjects
(or over all entered subjects when the rule lists none).

Subjects and categories match case-insensitively. A category without a row in
the rule has no minimum.
"""

import frappe
from frappe import _
from frappe.utils import flt

from campus.campus_platform.api import resolve_regulation


def subject_lines(text):
	"""Convert comma-separated or newline-separated subjects into a list."""
	return [subject.strip() for subject in (text or "").replace(",", "\n").splitlines() if subject.strip()]


def check_eligibility(required, minimums, marks, category, exam=None):
	"""Validate marks and return eligibility reasons.

	Args:
	    required: List of required subject names.
	    minimums: Dictionary mapping student categories to minimum percentages.
	    marks: List of (subject, maximum_marks, obtained_marks) tuples.
	    category: Student category.
	    exam: Qualifying examination name.

	Returns:
	    List of eligibility or validation reasons.
	"""
	if not marks:
		return [_("No qualifying marks entered.")]

	reasons = []
	by_subject = {}

	for subject, maximum, obtained in marks:
		subject_name = (subject or "").strip()
		subject_key = subject_name.lower()

		if not subject_key:
			reasons.append(_("A qualifying mark has no subject specified."))
			continue

		if subject_key in by_subject:
			reasons.append(_("Duplicate qualifying marks entered for subject {0}.").format(subject_name))
			continue

		maximum = flt(maximum)
		obtained = flt(obtained)

		if maximum <= 0:
			reasons.append(_("Maximum marks for subject {0} must be greater than zero.").format(subject_name))
			continue

		if obtained < 0 or obtained > maximum:
			reasons.append(
				_("Obtained marks for subject {0} must be between zero and maximum marks.").format(
					subject_name
				)
			)
			continue

		by_subject[subject_key] = (maximum, obtained)

	if reasons:
		return reasons

	required_keys = []
	for subject in required:
		key = subject.strip().lower()
		if key and key not in required_keys:
			required_keys.append(key)

	missing_subjects = [
		subject for subject in required if subject.strip() and subject.strip().lower() not in by_subject
	]

	if missing_subjects:
		return [
			_("Subject {0} is missing from the qualifying marks.").format(subject)
			for subject in missing_subjects
		]

	if required_keys:
		counted = [by_subject[key] for key in required_keys]
	else:
		counted = list(by_subject.values())

	total_maximum = sum(maximum for maximum, _obtained in counted)

	if total_maximum <= 0:
		return [_("Total maximum marks must be greater than zero.")]

	total_obtained = sum(obtained for _maximum, obtained in counted)

	percentage = total_obtained * 100 / total_maximum

	if minimums:
		reasons.extend(minimum_reasons(minimums, category, percentage, exam))

	return reasons


def minimum_reasons(minimums, category, percent, exam):
	"""Return reasons when category-based minimum marks are not satisfied."""
	if not category:
		return [_("Student category is not set; the minimum marks depend on it.")]

	normalized = {(name or "").strip().lower(): value for name, value in minimums.items()}
	key = category.strip().lower()

	if key in normalized and percent < normalized[key]:
		return [
			_("{0} marks of {1:.2f}% are below the {2}% required for category {3}.").format(
				exam or _("Qualifying"),
				percent,
				flt(normalized[key]),
				category,
			)
		]

	return []


def find_rule(program, regulation=None):
	"""Find an enabled rule for the program.

	Prefer a rule matching the resolved regulation. If none exists or no regulation
	was resolved, fall back to an enabled rule with no regulation.
	"""
	rules = frappe.get_all(
		"Eligibility Rule",
		filters={
			"program": program,
			"enabled": 1,
		},
		fields=["name", "regulation"],
		order_by="modified desc",
	)

	if regulation:
		for rule in rules:
			if rule.regulation == regulation:
				return frappe.get_doc("Eligibility Rule", rule.name)

	for rule in rules:
		if not rule.regulation:
			return frappe.get_doc("Eligibility Rule", rule.name)

	return None


def save_result(doc, status, reasons):
	"""Persist the result and keep the in-memory document in sync."""
	reason_text = "\n".join(reasons) or None

	doc.oc_eligibility_status = status
	doc.oc_eligibility_reasons = reason_text

	if not doc.is_new():
		frappe.db.set_value(
			"Student Applicant",
			doc.name,
			{
				"oc_eligibility_status": status,
				"oc_eligibility_reasons": reason_text,
			},
			update_modified=False,
		)


def compute_result(doc):
	"""Evaluate eligibility for a Student Applicant doc and return (status, reasons)."""
	if not doc.program:
		return "Pending", []

	regulation = resolve_regulation(
		doc.oc_company,
		doc.program,
		doc.academic_year,
	)

	rule = find_rule(doc.program, regulation)

	if not rule:
		return "Pending", [_("No Eligibility Rule for program {0}.").format(doc.program)]

	marks = [
		(
			row.subject,
			flt(row.max_marks),
			flt(row.obtained_marks),
		)
		for row in (doc.oc_qualifying_marks or [])
	]

	minimums = {
		row.student_category: flt(row.min_percent) for row in rule.category_marks if row.student_category
	}

	reasons = check_eligibility(
		subject_lines(rule.required_subjects),
		minimums,
		marks,
		doc.student_category,
		rule.qualifying_exam,
	)

	status = "Ineligible" if reasons else "Eligible"
	return status, reasons


def evaluate(applicant):
	"""Evaluate an applicant and store the eligibility result.

	Args:
	    applicant: Student Applicant document name.

	Returns:
	    Dictionary containing eligibility status and reasons.
	"""
	doc = frappe.get_doc("Student Applicant", applicant)
	status, reasons = compute_result(doc)
	save_result(doc, status, reasons)

	return {
		"eligible": status == "Eligible",
		"status": status,
		"reasons": reasons,
	}


def validate_applicant(doc, method=None):
	"""Validate hook on Student Applicant: evaluate eligibility and update status."""
	status, reasons = compute_result(doc)
	doc.oc_eligibility_status = status
	doc.oc_eligibility_reasons = "\n".join(reasons) or None
