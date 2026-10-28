"""PR-03 Curriculum of the 2025 batch: one Effective eight-semester R2022 curriculum per programme, in the credit range the
regulation's Curriculum Rule demands (165-170). Semesters 1 and 2 are common to all programmes, semester 3 of CSE has
its real course names, the rest are generic programme courses (the point is the credit structure, not a syllabus)."""

import frappe

from campus.campus_platform.doctype.regulation.regulation import regulation_name
from campus.curriculum.outcomes import seed_program_outcomes

THEORY, PRACTICAL = "Theory", "Practical"
OUTCOME_SEMESTERS = (1, 2, 3)
PSO_TEMPLATES = (
	("PSO1", "Apply core {name} principles to analyse, design, and build domain-specific engineering systems."),
	("PSO2", "Use modern engineering tools and practices to solve real-world {name} problems."),
)  # fmt: skip
CO_TEMPLATES = (
	("CO1", "Understand", "Explain the foundational concepts and principles of {course}."),
	("CO2", "Apply", "Apply core methods and techniques of {course} to solve engineering problems."),
	("CO3", "Analyse", "Analyse practical scenarios and evaluate solutions in {course}."),
)  # fmt: skip
# title, category, L, T, P, exam type. Credits at the R2022 rates (L 1, T 1, P 0.5): 3-1-0 = 4, 3-0-2 = 4, 3-0-0 = 3, 0-0-4 = 2.
COMMON = {
	1: [
		("Matrices and Calculus", "BSC", 3, 1, 0, THEORY), ("Engineering Physics", "BSC", 3, 0, 2, THEORY),
		("Problem Solving and Python Programming", "ESC", 3, 0, 2, THEORY), ("Technical English", "HSMC", 3, 0, 0, THEORY),
		("Engineering Graphics", "ESC", 3, 0, 2, THEORY), ("Physics and Chemistry Laboratory", "BSC", 0, 0, 4, PRACTICAL),
	],
	2: [
		("Statistics and Numerical Methods", "BSC", 3, 1, 0, THEORY), ("Engineering Chemistry", "BSC", 3, 0, 2, THEORY),
		("Basic Electrical and Electronics Engineering", "ESC", 3, 0, 2, THEORY), ("Professional English", "HSMC", 3, 0, 0, THEORY),
		("Engineering Mechanics", "ESC", 3, 1, 0, THEORY), ("Engineering Practices Laboratory", "ESC", 0, 0, 4, PRACTICAL),
	],
}  # fmt: skip
CSE_SEMESTER_3 = [
	("Discrete Mathematics", "BSC", 3, 1, 0, THEORY), ("Data Structures", "PCC", 3, 0, 2, THEORY),
	("Digital Principles and Computer Organization", "ESC", 3, 1, 0, THEORY), ("Object Oriented Programming", "PCC", 3, 1, 0, THEORY),
	("Database Management Systems", "PCC", 3, 0, 0, THEORY), ("Data Structures Laboratory", "PCC", 0, 0, 4, PRACTICAL),
]  # fmt: skip
GENERIC_SEMESTER = [  # semesters 4-7 (and 3 outside CSE): 4 + 4 + 4 + 4 + 3 + 2 = 21 credits
	("Core {sem}.1", "PCC", 3, 1, 0, THEORY), ("Core {sem}.2", "PCC", 3, 1, 0, THEORY), ("Core {sem}.3", "PCC", 3, 1, 0, THEORY),
	("Core {sem}.4", "PCC", 3, 1, 0, THEORY), ("Core {sem}.5", "PCC", 3, 0, 0, THEORY), ("Laboratory {sem}", "PCC", 0, 0, 4, PRACTICAL),
]  # fmt: skip
SEMESTER_8 = [  # 10 + 4 + 4 = 18 credits: with 21 x 7 = 147 the total is 165, the regulation's minimum
	("Project Work", "EEC", 0, 0, 20, "Project"), ("Professional Elective VI", "PEC", 3, 1, 0, THEORY),
	("Open Elective", "OEC", 3, 1, 0, THEORY),
]  # fmt: skip


def semester_courses(abbr, semester):
	"""[(course name, category, L, T, P, exam type)] of one semester of the programme `abbr`."""
	if semester in COMMON:
		return COMMON[semester]
	if semester == 3 and abbr == "CSE":
		return CSE_SEMESTER_3
	template = SEMESTER_8 if semester == 8 else GENERIC_SEMESTER
	return [(f"{abbr} {title.format(sem=semester)}", *rest) for title, *rest in template]


def load(ctx):
	"""Make the courses, the Effective curriculum, and the OBE outcomes (POs, PSOs, COs) of every programme."""
	regulation = regulation_name(ctx.profile["regulation"]["code"], ctx.college)
	for programme in ctx.profile["programmes"]:
		curriculum = ensure_curriculum(ctx, programme, regulation)
		ensure_outcomes(ctx, programme, regulation, curriculum)


def ensure_curriculum(ctx, programme, regulation):
	"""Create the 8-semester Effective curriculum of `programme` under `regulation` when missing."""
	existing = frappe.db.get_value(
		"Curriculum", {"program": programme["name"], "regulation": regulation, "version": 1}
	)
	if existing:
		return existing
	rows = []
	for semester in range(1, 9):
		for title, category, lecture, tutorial, practical, exam_type in semester_courses(
			programme["abbr"], semester
		):
			name = title
			if not frappe.db.exists("Course", name):
				frappe.get_doc({"doctype": "Course", "course_name": name, "oc_company": ctx.college}).insert(
					ignore_permissions=True
				)
				ctx.out.created["Course"] += 1
			row = {
				"semester": semester, "course": name, "category": category, "lecture_periods": lecture,
				"tutorial_periods": tutorial, "practical_periods": practical, "exam_type": exam_type,
			}  # fmt: skip
			if exam_type == "Project":
				row.update(credits=10, credits_override=1)
			rows.append(row)
	doc = frappe.get_doc(
		{
			"doctype": "Curriculum", "program": programme["name"], "regulation": regulation, "version": 1, "company": ctx.college,
			"courses": rows,
		}
	)  # fmt: skip
	doc.insert(ignore_permissions=True)
	for status in ("Approved", "Effective"):
		doc.status = status
		doc.save(ignore_permissions=True)
	ctx.out.created["Curriculum"] += 1
	return doc.name


def ensure_outcomes(ctx, programme, regulation, curriculum):
	"""Seed the 12 NBA POs, sample PSOs, and COs for the demo courses of `programme` under `regulation`."""
	created_pos = seed_program_outcomes(programme["name"], regulation)
	if created_pos:
		ctx.out.created["Program Outcome"] += len(created_pos)
	existing_psos = set(
		frappe.get_all(
			"Program Specific Outcome",
			{"program": programme["name"], "regulation": regulation},
			pluck="pso_code",
		)
	)
	for code, template in PSO_TEMPLATES:
		if code in existing_psos:
			continue
		frappe.get_doc(
			{
				"doctype": "Program Specific Outcome",
				"program": programme["name"],
				"regulation": regulation,
				"pso_code": code,
				"statement": template.format(name=programme["name"]),
			}
		).insert(ignore_permissions=True)
		ctx.out.created["Program Specific Outcome"] += 1
	existing_cos = {
		(row.course, row.co_code)
		for row in frappe.get_all("Course Outcome", {"curriculum": curriculum}, ["course", "co_code"])
	}
	for semester in OUTCOME_SEMESTERS:
		for course, *_rest in semester_courses(programme["abbr"], semester):
			for code, bloom_level, template in CO_TEMPLATES:
				if (course, code) in existing_cos:
					continue
				frappe.get_doc(
					{
						"doctype": "Course Outcome",
						"course": course,
						"curriculum": curriculum,
						"co_code": code,
						"bloom_level": bloom_level,
						"statement": template.format(course=course),
					}
				).insert(ignore_permissions=True)
				ctx.out.created["Course Outcome"] += 1
