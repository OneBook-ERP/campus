"""M03-02 CO-PO Matrix: how strongly each Course Outcome of a course serves each PO / PSO of its programme (0-3), for
one curriculum version, approved by the HOD of the course's department through the approval engine (the final
approval submits it; a BoS resolution may be recorded).

A new matrix is filled with every CO x PO / PSO at 0. Only one live (draft or approved) matrix exists per course and
curriculum; a revision is an amendment. It cannot be sent for approval or submitted while a CO of the course maps to
nothing (AC1). On submit, POs / PSOs with zero total correlation across the curriculum's approved matrices are warned
about; the CO-PO Coverage report lists them.
"""

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint

from campus.campus_platform.api import ApprovableDocument
from campus.curriculum import outcomes


class COPOMatrix(ApprovableDocument, Document):
	def validate(self):
		super().validate()
		self.set_scope()
		if self.is_new() and not self.correlations:
			self.fill_grid()
		self.check_one_live_matrix()
		self.check_cells()

	def before_approval_request(self):
		"""Called by the approval engine before a request is created: refuse what submit would refuse anyway."""
		self.check_every_co_mapped()

	def before_submit(self):
		frappe.db.get_value(
			"Curriculum", self.curriculum, "name", for_update=True
		)  # serialises submits per curriculum
		self.check_one_live_matrix(locking=True)
		self.check_every_co_mapped()
		super().before_submit()

	def on_submit(self):
		unmapped = outcomes.unmapped_codes(self.curriculum)
		if unmapped:
			frappe.msgprint(
				_("Across {0}, these outcomes have no correlation in any approved matrix yet: {1}. See the CO-PO Coverage report.").format(
					frappe.bold(self.curriculum), ", ".join(unmapped)
				),
				title=_("Outcomes not covered"), indicator="orange",
			)  # fmt: skip

	def set_scope(self):
		"""Populate program, regulation, department, and company from the curriculum and course."""
		curriculum = frappe.db.get_value(
			"Curriculum", self.curriculum, ["program", "regulation"], as_dict=True
		)
		self.program, self.regulation = curriculum.program, curriculum.regulation
		course_dept = frappe.db.get_value("Course", self.course, "department")
		self.department = course_dept or frappe.db.get_value("Program", self.program, "department")
		self.company = frappe.db.get_value("Program", self.program, "oc_company")

	def require_outcomes(self):
		"""Return (course_outcomes, programme_outcomes) or raise when either master is empty."""
		cos = outcomes.course_outcomes(self.course, self.curriculum)
		if not cos:
			frappe.throw(
				_("Define the course outcomes of {0} in {1} first.").format(
					frappe.bold(self.course), frappe.bold(self.curriculum)
				)
			)
		targets = outcomes.programme_outcomes(self.program, self.regulation)
		if not targets:
			frappe.throw(
				_("Define the programme outcomes (PO / PSO) of {0} under {1} first.").format(
					frappe.bold(self.program), frappe.bold(self.regulation)
				)
			)
		return cos, targets

	@frappe.whitelist()
	def fill_grid(self):
		"""Add a 0 cell for every CO x PO / PSO pair not yet in the grid (a CO or PSO added later)."""
		if not (self.program and self.regulation):
			self.set_scope()
		cos, targets = self.require_outcomes()
		present = {(row.course_outcome, row.outcome) for row in self.correlations}
		for co in cos:
			for outcome in targets:
				if (co.name, outcome.name) not in present:
					self.append(
						"correlations",
						{
							"course_outcome": co.name,
							"co_code": co.co_code,
							"outcome_type": outcome.doctype,
							"outcome": outcome.name,
							"outcome_code": outcome.code,
							"correlation": 0,
						},
					)

	def check_one_live_matrix(self, locking=False):
		filters = {"course": self.course, "curriculum": self.curriculum, "docstatus": ["<", 2]}
		if not self.is_new():
			filters["name"] = ["!=", self.name]
		other = frappe.db.get_value(outcomes.MATRIX, filters, "name", for_update=locking)
		if other:
			frappe.throw(
				_("{0} already has a CO-PO Matrix in {1}: {2}. Amend it instead.").format(
					frappe.bold(self.course), frappe.bold(self.curriculum), frappe.bold(other)
				)
			)

	def check_cells(self):
		"""Verify that every correlation row references valid, unique outcomes of this curriculum and 0-3 values."""
		cos_list, targets_list = self.require_outcomes()
		cos = {co.name: co.co_code for co in cos_list}
		targets = {(o.doctype, o.name): o.code for o in targets_list}
		seen = set()
		for row in self.correlations:
			if row.course_outcome not in cos:
				frappe.throw(
					_("Row {0}: {1} is not a course outcome of {2} in {3}.").format(
						row.idx,
						frappe.bold(row.course_outcome),
						frappe.bold(self.course),
						frappe.bold(self.curriculum),
					)
				)
			if (row.outcome_type, row.outcome) not in targets:
				frappe.throw(
					_("Row {0}: {1} is not an outcome of {2} under {3}.").format(
						row.idx,
						frappe.bold(row.outcome),
						frappe.bold(self.program),
						frappe.bold(self.regulation),
					)
				)
			if (row.course_outcome, row.outcome) in seen:
				frappe.throw(
					_("Row {0}: {1} against {2} appears twice.").format(
						row.idx, cos[row.course_outcome], targets[(row.outcome_type, row.outcome)]
					)
				)
			seen.add((row.course_outcome, row.outcome))
			row.correlation = cint(row.correlation)
			if row.correlation not in outcomes.CORRELATIONS:
				frappe.throw(_("Row {0}: a correlation runs from 0 to 3.").format(row.idx))
			row.co_code, row.outcome_code = cos[row.course_outcome], targets[(row.outcome_type, row.outcome)]

	def check_every_co_mapped(self):
		"""Refuse approval or submission when any course outcome has no non-zero correlation."""
		cos, _targets = self.require_outcomes()
		mapped = {row.course_outcome for row in self.correlations if row.correlation}
		unmapped = [co.co_code for co in cos if co.name not in mapped]
		if unmapped:
			frappe.throw(
				_(
					"These course outcomes map to nothing: {0}. Give each at least one correlation (1-3), or Fill Grid for new ones."
				).format(", ".join(unmapped))
			)
