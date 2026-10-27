"""Universal data sanitization utilities and doc_events hooks for OneBook Campus."""

import frappe

MOBILE_FIELDS = {
	"Student": ["student_mobile_number"],
	"Student Applicant": ["student_mobile_number"],
	"Guardian": ["mobile_number"],
	"Student Attendance": ["student_mobile_number"],
}


def clean_mobile_number(val):
	"""BUG-08: Drops a trailing .0 or .00 from mobile numbers resulting from float imports.
	Leaves all other formats (e.g. extension, multiple numbers, spaces, non-float strings) untouched.
	"""
	if val is None:
		return ""

	if isinstance(val, float):
		if val.is_integer():
			return str(int(val))
		return str(val)

	val_str = str(val).strip()
	if not val_str:
		return ""

	if val_str.endswith(".00"):
		prefix = val_str[:-3]
		if prefix.isdigit() or (prefix.startswith("+") and prefix[1:].isdigit()):
			return prefix
	if val_str.endswith(".0"):
		prefix = val_str[:-2]
		if prefix.isdigit() or (prefix.startswith("+") and prefix[1:].isdigit()):
			return prefix

	return val_str


def validate_mobile(doc, method=None):
	"""Doc event hook on validate: ensures mobile number fields are clean before saving."""
	fields = MOBILE_FIELDS.get(doc.doctype) or []
	for fieldname in fields:
		if doc.meta.has_field(fieldname):
			val = doc.get(fieldname)
			if val:
				cleaned = clean_mobile_number(val)
				if cleaned != val:
					doc.set(fieldname, cleaned)
