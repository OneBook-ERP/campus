"""BUG-08: Strip floating-point decimal suffix ('.0') from mobile numbers across student, applicant and guardian records."""

import frappe

TARGETS = [
	(
		"Student",
		"student_mobile_number",
		"""
		UPDATE `tabStudent`
		SET `student_mobile_number` = SUBSTRING(`student_mobile_number`, 1, CHAR_LENGTH(`student_mobile_number`) - 2)
		WHERE `student_mobile_number` REGEXP '^[+]?[0-9]+[.]0\\\\z'
		""",
		"""
		UPDATE `tabStudent`
		SET `student_mobile_number` = SUBSTRING(`student_mobile_number`, 1, CHAR_LENGTH(`student_mobile_number`) - 3)
		WHERE `student_mobile_number` REGEXP '^[+]?[0-9]+[.]00\\\\z'
		""",
	),
	(
		"Student Applicant",
		"student_mobile_number",
		"""
		UPDATE `tabStudent Applicant`
		SET `student_mobile_number` = SUBSTRING(`student_mobile_number`, 1, CHAR_LENGTH(`student_mobile_number`) - 2)
		WHERE `student_mobile_number` REGEXP '^[+]?[0-9]+[.]0\\\\z'
		""",
		"""
		UPDATE `tabStudent Applicant`
		SET `student_mobile_number` = SUBSTRING(`student_mobile_number`, 1, CHAR_LENGTH(`student_mobile_number`) - 3)
		WHERE `student_mobile_number` REGEXP '^[+]?[0-9]+[.]00\\\\z'
		""",
	),
	(
		"Guardian",
		"mobile_number",
		"""
		UPDATE `tabGuardian`
		SET `mobile_number` = SUBSTRING(`mobile_number`, 1, CHAR_LENGTH(`mobile_number`) - 2)
		WHERE `mobile_number` REGEXP '^[+]?[0-9]+[.]0\\\\z'
		""",
		"""
		UPDATE `tabGuardian`
		SET `mobile_number` = SUBSTRING(`mobile_number`, 1, CHAR_LENGTH(`mobile_number`) - 3)
		WHERE `mobile_number` REGEXP '^[+]?[0-9]+[.]00\\\\z'
		""",
	),
	(
		"Student Attendance",
		"student_mobile_number",
		"""
		UPDATE `tabStudent Attendance`
		SET `student_mobile_number` = SUBSTRING(`student_mobile_number`, 1, CHAR_LENGTH(`student_mobile_number`) - 2)
		WHERE `student_mobile_number` REGEXP '^[+]?[0-9]+[.]0\\\\z'
		""",
		"""
		UPDATE `tabStudent Attendance`
		SET `student_mobile_number` = SUBSTRING(`student_mobile_number`, 1, CHAR_LENGTH(`student_mobile_number`) - 3)
		WHERE `student_mobile_number` REGEXP '^[+]?[0-9]+[.]00\\\\z'
		""",
	),
]


def execute():
	"""Cleans existing records in Student, Student Applicant, Guardian and Student Attendance that end in .0 or .00."""
	for doctype, field, sql_dot_zero, sql_dot_double_zero in TARGETS:
		if not frappe.db.has_column(doctype, field):
			continue

		frappe.db.sql(sql_dot_zero)
		frappe.db.sql(sql_dot_double_zero)
