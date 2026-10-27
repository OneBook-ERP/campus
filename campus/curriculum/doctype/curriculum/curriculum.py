"""M03-01 Curriculum: the regulation-year course structure of a programme with credits and categories.

Credits derive from L-T-P at the regulation's Curriculum Rule rates (manual override per row),
totals are checked against the rule's range and the category minimums, an Effective curriculum is
read-only and changes go through `create_new_version`.
"""

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, cstr, flt

from campus.campus_platform.regulation_setup import DEAN_ROLE
from campus.curriculum.credit_rules import category_totals, credit_shortfalls, derive_credits, range_problem
from campus.curriculum.registration_rules import prerequisite_codes

LOCKED = ("Effective", "Superseded")
TRANSITIONS = {
	None: ("Draft",),
	"Draft": ("Draft", "Approved"),
	"Approved": ("Approved", "Draft", "Effective"),
}
NO_VALUE_FIELDS = frappe.model.no_value_fields
CHILD_SKIP = {"name", "modified", "creation", "modified_by", "owner", "idx"}
COPIED_COURSE_FIELDS = (
	"semester", "course", "category", "lecture_periods", "tutorial_periods", "practical_periods", "credits",
	"credits_override", "is_elective", "exam_type", "cia_max", "ese_max", "repeatable", "prerequisites",
)  # fmt: skip


class Curriculum(Document):
	def autoname(self):
		abbreviation = frappe.db.get_value("Program", self.program, "program_abbreviation")
		self.name = f"{abbreviation or self.program}-{self.regulation}-v{cint(self.version) or 1}"
		if frappe.db.exists("Curriculum", self.name):
			link = frappe.utils.get_link_to_form("Curriculum", self.name)
			frappe.throw(
				_(
					"Curriculum {0} already exists. You can open it here: {1} or create a new version of it instead."
				).format(frappe.bold(self.name), link),
				title=_("Curriculum Already Exists"),
			)

	def validate(self):
		if self.program:
			prog_company = frappe.db.get_value("Program", self.program, "oc_company")
			if not prog_company:
				frappe.throw(
					_(
						"Program {0} has no company assigned. Assign a company to the Program before creating a Curriculum."
					).format(frappe.bold(self.program)),
					title=_("Missing Program Company"),
				)
		self.validate_status_change()
		if self.validate_lock():
			return
		self.validate_regulation()
		self.validate_courses()
		rule = self.get_rule()
		self.derive_credits(rule)
		problems = self.compute_totals(rule)
		self.report_problems(problems, rule)
		self.validate_single_effective()

	def on_update(self):
		if self.status == "Effective":
			self.supersede_previous()

	def on_trash(self):
		if self.status in LOCKED:
			frappe.throw(_("An {0} curriculum cannot be deleted.").format(_(self.status)))

	def get_rule(self):
		name = frappe.db.get_value("Curriculum Rule", {"regulation": self.regulation})
		return frappe.get_doc("Curriculum Rule", name) if name else None

	def validate_status_change(self):
		self.status = self.status or "Draft"
		before = self.get_doc_before_save()
		old = before.status if before else None
		if old in LOCKED or self.status == old:
			return
		if self.status not in TRANSITIONS.get(old, ()):
			frappe.throw(
				_("A curriculum cannot move from {0} to {1}.").format(_(old or "New"), _(self.status))
			)
		if (
			self.status == "Effective"
			and DEAN_ROLE not in frappe.get_roles()
			and not self.flags.via_academic_proposal
		):
			frappe.throw(_("Only the {0} can make a curriculum Effective.").format(DEAN_ROLE))

	def validate_lock(self):
		"""True when the curriculum is locked and unchanged; throws when a locked one is edited."""
		before = self.get_doc_before_save()
		if not before or before.status not in LOCKED:
			return False
		if self.snapshot() != before.snapshot() or self.status != before.status:
			frappe.throw(
				_("{0} is {1} and cannot be edited. Create a new version instead.").format(
					frappe.bold(self.name), _(before.status)
				),
				title=_("Curriculum Locked"),
			)
		return True

	def snapshot(self):
		"""Editable content: parent values (status excluded) and every child row."""
		values = {
			df.fieldname: cstr(self.get(df.fieldname))
			for df in self.meta.fields
			if df.fieldtype not in NO_VALUE_FIELDS and df.fieldname != "status"
		}
		for table in ("courses", "category_requirements", "applicable_batches"):
			values[table] = [
				{
					k: cstr(v)
					for k, v in row.as_dict().items()
					if k not in CHILD_SKIP and not k.startswith("parent")
				}
				for row in self.get(table)
			]
		return values

	def validate_regulation(self):
		regulation = frappe.get_doc("Regulation", self.regulation)
		if self.program not in [row.program for row in regulation.programs]:
			frappe.throw(
				_("Regulation {0} does not cover programme {1}.").format(
					frappe.bold(self.regulation), frappe.bold(self.program)
				)
			)

	def validate_courses(self):
		self.validate_duplicate_courses()
		semesters = cint(frappe.db.get_value("Program", self.program, "oc_total_semesters"))
		groups = {}
		for row in self.courses:
			self.validate_semester(row, semesters)
			self.validate_elective_group(row, groups)
			self.validate_prerequisites(row)
			if flt(row.credits) < 0 and row.credits_override:
				frappe.throw(_("Row {0}: credits cannot be negative.").format(row.idx))

	def validate_duplicate_courses(self):
		rows_of = {}
		for row in self.courses:
			rows_of.setdefault(row.course, []).append(row)
		for course, rows in rows_of.items():
			if len(rows) > 1 and not all(row.repeatable for row in rows):
				frappe.throw(
					_(
						"Course {0} appears more than once (rows {1}). "
						"Mark every occurrence Repeatable to allow this."
					).format(frappe.bold(course), ", ".join(str(row.idx) for row in rows)),
					title=_("Duplicate Course"),
				)

	def validate_semester(self, row, total_semesters):
		if row.semester < 1 or (total_semesters and row.semester > total_semesters):
			frappe.throw(
				_("Row {0}: semester {1} is outside the programme's 1 to {2}.").format(
					row.idx, row.semester, total_semesters or "n"
				)
			)

	def validate_elective_group(self, row, cache):
		if not row.elective_group:
			return
		if not row.is_elective:
			frappe.throw(_("Row {0}: only an elective can belong to an elective group.").format(row.idx))
		if row.elective_group not in cache:
			cache[row.elective_group] = frappe.db.get_value(
				"Elective Group", row.elective_group, ["curriculum", "semester"], as_dict=True
			)
		group = cache[row.elective_group]
		if not group or group.curriculum != self.name or group.semester != row.semester:
			frappe.throw(
				_("Row {0}: elective group {1} belongs to another curriculum or semester.").format(
					row.idx, frappe.bold(row.elective_group)
				)
			)

	def validate_prerequisites(self, row):
		for code in prerequisite_codes(row.prerequisites):
			if code == row.course:
				frappe.throw(_("Row {0}: a course cannot be its own prerequisite.").format(row.idx))
			if not frappe.db.exists("Course", code):
				frappe.throw(
					_("Row {0}: prerequisite {1} is not a Course.").format(row.idx, frappe.bold(code))
				)

	def derive_credits(self, rule):
		for row in self.courses:
			if rule and not row.credits_override:
				row.credits = derive_credits(
					row.lecture_periods, row.tutorial_periods, row.practical_periods, rule
				)

	def compute_totals(self, rule):
		"""Set total and per-category actuals; return the list of rule violations."""
		categories = [row.category for row in self.courses] + [
			row.category for row in self.category_requirements
		]
		counts = {
			c.name: c.counts_toward_credits
			for c in frappe.get_all(
				"Course Category",
				filters={"name": ["in", categories]},
				fields=["name", "counts_toward_credits"],
			)
		}
		totals, self.total_credits = category_totals([(r.category, r.credits) for r in self.courses], counts)
		minimums = {}
		for row in self.category_requirements:
			if row.category in minimums:
				frappe.throw(
					_("Row {0}: category {1} is listed twice.").format(row.idx, frappe.bold(row.category))
				)
			minimums[row.category] = flt(row.min_credits)
			row.actual_credits = totals.get(row.category, 0)
		return self.rule_problems(rule, totals, minimums)

	def rule_problems(self, rule, totals, minimums):
		problems = []
		if rule:
			problem = range_problem(self.total_credits, rule.min_total_credits, rule.max_total_credits)
			problems += [problem] if problem else []
			problems += self.too_many_courses(rule)
		for category, actual, required in credit_shortfalls(totals, minimums):
			problems.append(
				_("Category {0} has {1} credits; the minimum is {2}.").format(category, actual, required)
			)
		return problems

	def too_many_courses(self, rule):
		if not rule.max_courses_per_semester:
			return []
		per_semester = {}
		for row in self.courses:
			per_semester[row.semester] = per_semester.get(row.semester, 0) + 1
		return [
			_("Semester {0} has {1} courses; the maximum is {2}.").format(
				sem, n, rule.max_courses_per_semester
			)
			for sem, n in sorted(per_semester.items())
			if n > rule.max_courses_per_semester
		]

	def report_problems(self, problems, rule):
		"""Draft shows warnings; Approved and Effective curricula must satisfy the rules."""
		if not rule and self.status != "Draft":
			frappe.throw(
				_(
					"Regulation {0} has no Curriculum Rule. Define one before approving this curriculum."
				).format(frappe.bold(self.regulation)),
				title=_("Curriculum Rules Not Met"),
			)
		if not rule:
			frappe.msgprint(
				_(
					"Regulation {0} has no Curriculum Rule: credits are not derived "
					"and the total is not range-checked."
				).format(frappe.bold(self.regulation)),
				indicator="orange",
				alert=True,
			)
		if not problems:
			return
		message = "<br>".join(problems)
		if self.status == "Draft":
			frappe.msgprint(message, title=_("Curriculum Warnings"), indicator="orange")
		else:
			frappe.throw(message, title=_("Curriculum Rules Not Met"))

	def lock_siblings(self):
		"""Row-lock every version of this programme and regulation (held until commit) and read them fresh,
		each with the set of batches it is limited to (empty: the default for every batch)."""
		siblings = frappe.db.sql(
			"""select name, version, status from `tabCurriculum`
			where program = %s and regulation = %s for update""",
			(self.program, self.regulation),
			as_dict=True,
		)
		listed = {}
		for row in frappe.get_all(
			"Curriculum Batch",
			{"parenttype": "Curriculum", "parent": ["in", [s.name for s in siblings] or [""]]},
			["parent", "academic_year"],
		):
			listed.setdefault(row.parent, set()).add(row.academic_year)
		for sibling in siblings:
			sibling.batches = listed.get(sibling.name, set())
		return siblings

	def own_batches(self):
		return {row.academic_year for row in self.applicable_batches}

	def validate_single_effective(self):
		"""Taken under the sibling lock so two concurrent Effective transitions cannot both succeed.
		A newer Effective version blocks this one only when they cover a common batch (both default, or a shared batch)."""
		if self.status != "Effective":
			return
		for sibling in self.lock_siblings():
			if (
				sibling.status == "Effective"
				and sibling.version > cint(self.version)
				and batches_overlap(sibling.batches, self.own_batches())
			):
				frappe.throw(
					_(
						"A newer version of this curriculum ({0}) is already Effective for the same batches."
					).format(sibling.name)
				)

	def supersede_previous(self):
		"""An Effective curriculum without listed batches supersedes the older Effective default versions; versions
		limited to some batches keep serving theirs, and a batch-limited one supersedes nobody.

		Runs in the transaction that holds the sibling lock from validate. The direct db.set_value
		intentionally bypasses the Effective snapshot lock: this is the one allowed change to a locked record.
		"""
		if self.own_batches():
			return
		for sibling in self.lock_siblings():
			if sibling.status == "Effective" and sibling.name != self.name and not sibling.batches:
				frappe.db.set_value("Curriculum", sibling.name, "status", "Superseded")


def batches_overlap(batches, other):
	"""Two Effective curricula collide when both are defaults or they list a common batch."""
	return (not batches and not other) or bool(batches & other)


@frappe.whitelist()
def create_new_version(curriculum):
	"""Copy an Approved or Effective curriculum into a new Draft with the next version number."""
	frappe.has_permission("Curriculum", "create", throw=True)
	source = frappe.get_doc("Curriculum", curriculum)
	frappe.has_permission("Curriculum", "read", doc=source, throw=True)
	if source.status not in ("Approved", "Effective"):
		frappe.throw(_("Only an Approved or Effective curriculum can be versioned."))
	latest = frappe.db.get_all(
		"Curriculum",
		{"program": source.program, "regulation": source.regulation},
		pluck="version",
		order_by="version desc",
		limit=1,
	)
	new = frappe.get_doc(
		{
			"doctype": "Curriculum",
			"program": source.program,
			"regulation": source.regulation,
			"version": cint(latest[0]) + 1,
			"status": "Draft",
			"previous_version": source.name,
			"courses": [{field: row.get(field) for field in COPIED_COURSE_FIELDS} for row in source.courses],
			"category_requirements": [
				{"category": r.category, "min_credits": r.min_credits} for r in source.category_requirements
			],
		}
	).insert()
	group_map = copy_elective_groups(source, new)  # groups need the saved curriculum; link them afterwards
	for old_row, new_row in zip(source.courses, new.courses, strict=True):
		new_row.elective_group = group_map.get(old_row.elective_group)
	new.save()
	return new.name


def copy_elective_groups(source, new):
	"""Copy the source's elective groups onto `new`; returns old group name -> new group name."""
	mapping = {}
	for group in frappe.get_all("Elective Group", filters={"curriculum": source.name}, pluck="name"):
		old = frappe.get_doc("Elective Group", group)
		copy = frappe.get_doc(
			{
				"doctype": "Elective Group",
				"curriculum": new.name,
				"semester": old.semester,
				"group_name": old.group_name,
				"choose_n": old.choose_n,
				"seat_cap_per_course": old.seat_cap_per_course,
			}
		).insert()
		mapping[old.name] = copy.name
	return mapping
