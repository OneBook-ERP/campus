"""M03-01 Back-fill `company` on existing Curriculum records so they appear in scoped list views."""

import frappe


def execute():
	"""Back-fill `company` on existing Curriculum records where company is NULL or blank."""
	if not frappe.db.table_exists("Curriculum"):
		return

	curricula = frappe.db.sql(
		"""
		SELECT name, program, regulation
		FROM `tabCurriculum`
		WHERE company IS NULL OR company = ''
		""",
		as_dict=True,
	)

	if not curricula:
		return

	for cur in curricula:
		company = None
		if cur.program:
			company = frappe.db.get_value("Program", cur.program, "oc_company")
		if not company and cur.regulation:
			company = frappe.db.get_value("Regulation", cur.regulation, "company")

		if company:
			frappe.db.set_value("Curriculum", cur.name, "company", company, update_modified=False)
