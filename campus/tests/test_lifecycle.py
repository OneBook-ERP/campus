"""L-01 integration tests (M05-09, NFR history preservation): the lifecycle service, its transition table and log,
the refusal of direct status writes, the migration of existing students, the status summary report, and the
admission and admission-cancellation flows recording their transitions (break, rejoin, TC, convocation and
progression are covered in their own modules)."""

from types import SimpleNamespace
from unittest.mock import patch

import frappe
from frappe.utils import add_days, getdate, today

from campus.student_services import lifecycle
from campus.student_services import lifecycle_rules as rules
from campus.student_services.report.student_status_summary import student_status_summary as summary
from campus.tests.factories import make_company, make_user
from campus.tests.test_admission_flow import OFFICER, AdmitTestCase
from campus.tests.utils import CampusTestCase

LOG, RULE = lifecycle.LOG, lifecycle.RULE
COLLEGE, OTHER = "Lifecycle College", "Lifecycle Other College"
PROGRAM, OTHER_PROGRAM = "Lifecycle BE CSE", "Lifecycle BE ECE"
YEAR, LATER_YEAR = "Lifecycle 2024", "Lifecycle 2025"
CLERK, OUTSIDER = "lifecycle-clerk@example.com", "lifecycle-outsider@example.com"
SERIAL = [0]


def transitions(student):
	return [
		(row.from_status or "", row.to_status, row.kind, row.source_doctype)
		for row in frappe.get_all(
			LOG,
			{"student": student},
			["from_status", "to_status", "kind", "source_doctype"],
			order_by="creation asc, name asc",
		)
	]


def status(student):
	return frappe.db.get_value("Student", student, "oc_academic_status")


class LifecycleFixture(CampusTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		make_company(COLLEGE, "LCC")
		make_company(OTHER, "LCO")
		for name, start in ((YEAR, "2024-06-01"), (LATER_YEAR, "2025-06-01")):
			if not frappe.db.exists("Academic Year", name):
				frappe.get_doc(
					{"doctype": "Academic Year", "academic_year_name": name, "year_start_date": start,
					"year_end_date": str(add_days(getdate(start), 364))}
				).insert()  # fmt: skip
		for program, company in ((PROGRAM, COLLEGE), (OTHER_PROGRAM, COLLEGE)):
			if not frappe.db.exists("Program", program):
				frappe.get_doc(
					{"doctype": "Program", "program_name": program, "program_abbreviation": program[-3:], "oc_company": company}
				).insert()  # fmt: skip
		make_user(CLERK, [COLLEGE])
		make_user(OUTSIDER, [OTHER])
		for user in (CLERK, OUTSIDER):
			frappe.get_doc("User", user).add_roles("Dean Academics")

	def setUp(self):
		frappe.set_user("Administrator")
		frappe.db.savepoint("lifecycle_test")
		self.addCleanup(frappe.db.rollback, save_point="lifecycle_test")
		self.addCleanup(frappe.set_user, "Administrator")

	def student(self, batch=YEAR, company=COLLEGE, **values):
		SERIAL[0] += 1
		return frappe.get_doc(
			{
				"doctype": "Student", "first_name": f"Life{SERIAL[0]}", "oc_company": company, "oc_batch": batch,
				"student_email_id": f"life{SERIAL[0]}-{frappe.generate_hash(length=6)}@lifecycle.example.com", **values,
			}
		).insert().name  # fmt: skip

	def enroll(self, student, program=PROGRAM, year=YEAR):
		return frappe.get_doc(
			{"doctype": "Program Enrollment", "student": student, "program": program, "academic_year": year,
			"enrollment_date": frappe.db.get_value("Academic Year", year, "year_start_date")}
		).insert().submit()  # fmt: skip

	def latest(self, student):
		return frappe.get_last_doc(LOG, {"student": student}, order_by="creation desc, name desc")


class TestTransitionService(LifecycleFixture):
	def test_a_new_student_records_its_first_status(self):
		student = self.student(joining_date="2024-05-15")
		self.assertEqual(status(student), rules.ACTIVE)
		log = self.latest(student)
		self.assertEqual(
			(log.from_status or "", log.to_status, log.kind), ("", rules.ACTIVE, lifecycle.INITIAL)
		)
		self.assertEqual((log.effective_date, log.company), (getdate("2024-05-15"), COLLEGE))
		without_joining = self.student()
		self.assertEqual(
			self.latest(without_joining).effective_date, getdate("2024-06-01")
		)  # the batch start, not today

	def test_a_student_cannot_start_in_a_status_the_table_does_not_start(self):
		with self.assertRaisesRegex(frappe.ValidationError, "no enabled Lifecycle Transition Rule"):
			self.student(oc_academic_status=rules.ON_BREAK)

	def test_a_direct_write_to_the_status_is_refused(self):
		student = self.student()
		doc = frappe.get_doc("Student", student)
		doc.oc_academic_status = rules.WITHDRAWN
		with self.assertRaisesRegex(frappe.ValidationError, "only through a lifecycle transition"):
			doc.save()
		self.assertEqual(status(student), rules.ACTIVE)
		doc.reload()
		doc.first_name = "Renamed"
		doc.save()  # other fields still save

	def test_duplicate_guardians_are_rejected(self):
		guardian_1 = frappe.get_doc({"doctype": "Guardian", "guardian_name": "Guardian One"}).insert().name
		guardian_2 = frappe.get_doc({"doctype": "Guardian", "guardian_name": "Guardian Two"}).insert().name

		student = self.student()
		doc = frappe.get_doc("Student", student)
		doc.append("guardians", {"guardian": guardian_1})
		doc.append("guardians", {"guardian": guardian_1})
		with self.assertRaisesRegex(frappe.ValidationError, "Duplicate guardians are not allowed"):
			doc.save()

		doc.reload()
		doc.set("guardians", [])
		doc.append("guardians", {"guardian": guardian_1})
		doc.append("guardians", {"guardian": guardian_2})
		doc.save()

	def test_an_allowed_transition_changes_the_status_and_logs_it(self):
		student = self.student()
		enrollment = self.enroll(student)
		frappe.set_user(CLERK)
		name = lifecycle.transition(
			student, rules.ON_BREAK, "2024-09-01", "Illness", source=("Program Enrollment", enrollment.name)
		)
		frappe.set_user("Administrator")
		self.assertEqual(status(student), rules.ON_BREAK)
		log = frappe.get_doc(LOG, name)
		self.assertEqual(
			(log.from_status, log.to_status, log.effective_date, log.reason, log.kind),
			(rules.ACTIVE, rules.ON_BREAK, getdate("2024-09-01"), "Illness", lifecycle.TRANSITION),
		)
		self.assertEqual(
			(log.source_doctype, log.source_name, log.approved_by, log.program_enrollment, log.company),
			("Program Enrollment", enrollment.name, CLERK, enrollment.name, COLLEGE),
		)

	def test_a_transition_the_table_does_not_permit_is_refused(self):
		student = self.student()
		with self.assertRaisesRegex(frappe.ValidationError, "cannot move from Active to Graduated"):
			lifecycle.transition(student, rules.GRADUATED, today(), "Skipping ahead")
		with self.assertRaisesRegex(frappe.ValidationError, "not a lifecycle status"):
			lifecycle.transition(student, "Detained", today(), "Old value")
		with self.assertRaisesRegex(frappe.ValidationError, "already Active"):
			lifecycle.transition(student, rules.ACTIVE, today(), "Again")
		self.assertEqual((status(student), len(transitions(student))), (rules.ACTIVE, 1))

	def test_the_table_is_configurable(self):
		student = self.student()
		frappe.db.set_value(RULE, lifecycle.rule_name(rules.ACTIVE, rules.ON_BREAK), "enabled", 0)
		with self.assertRaisesRegex(frappe.ValidationError, "cannot move from Active to On Break"):
			lifecycle.transition(student, rules.ON_BREAK, today(), "Illness")
		frappe.get_doc({"doctype": RULE, "from_status": rules.ACTIVE, "to_status": rules.ALUMNI}).insert()
		lifecycle.transition(student, rules.ALUMNI, today(), "Configured shortcut")
		self.assertEqual(status(student), rules.ALUMNI)

	def test_a_rule_is_unique_and_names_known_statuses(self):
		with self.assertRaises(frappe.DuplicateEntryError):
			frappe.get_doc(
				{"doctype": RULE, "from_status": rules.ACTIVE, "to_status": rules.ON_BREAK}
			).insert()
		with self.assertRaisesRegex(frappe.ValidationError, "same"):
			frappe.get_doc({"doctype": RULE, "from_status": rules.ACTIVE, "to_status": rules.ACTIVE}).insert()

	def test_effective_dates_never_go_back_before_the_last_change(self):
		student = self.student()
		lifecycle.transition(student, rules.ON_BREAK, "2025-01-10", "Illness")
		with self.assertRaisesRegex(frappe.ValidationError, "before the last status change"):
			lifecycle.transition(student, rules.ACTIVE, "2025-01-09", "Back")
		lifecycle.transition(student, rules.ACTIVE, "2025-01-10", "Back the same day")
		with self.assertRaisesRegex(frappe.ValidationError, "before the last status change"):
			lifecycle.transition(self.student(), rules.ON_BREAK, "2024-05-31", "Before the batch started")

	def test_a_retry_from_the_same_source_returns_the_earlier_transition(self):
		student = self.student()
		enrollment = self.enroll(student)
		source = ("Program Enrollment", enrollment.name)
		first = lifecycle.transition(student, rules.ON_BREAK, "2024-09-01", "Illness", source=source)
		self.assertEqual(
			lifecycle.transition(student, rules.ON_BREAK, "2024-09-01", "Illness", source=source), first
		)
		self.assertEqual(len(transitions(student)), 2)

	def test_the_student_row_is_locked_and_the_log_read_with_a_locking_read(self):
		student = self.student()
		calls = []
		real = frappe.db.get_value

		def spy(*args, **kwargs):
			calls.append((args[:2], kwargs.get("for_update")))
			return real(*args, **kwargs)

		with patch.object(frappe.db, "get_value", side_effect=spy):
			lifecycle.transition(student, rules.ON_BREAK, today(), "Illness")
		self.assertEqual(calls[0], (("Student", student), True))
		with patch.object(frappe.db, "sql", wraps=frappe.db.sql) as sql:
			lifecycle.transition(student, rules.ACTIVE, today(), "Back")
		self.assertTrue(
			any(
				"tabLifecycle Transition" in str(c.args[0]) and "FOR UPDATE" in str(c.args[0])
				for c in sql.call_args_list
			)
		)

	def test_a_reversal_restores_the_status_and_keeps_both_records(self):
		student = self.student()
		enrollment = self.enroll(student)
		source = ("Program Enrollment", enrollment.name)
		original = lifecycle.transition(student, rules.ON_BREAK, "2024-09-01", "Illness", source=source)
		reversal = lifecycle.revert(source, "Recorded in error")
		self.assertEqual(status(student), rules.ACTIVE)
		log = frappe.get_doc(LOG, reversal)
		self.assertEqual(
			(log.from_status, log.to_status, log.kind, log.reverses, log.effective_date),
			(rules.ON_BREAK, rules.ACTIVE, lifecycle.REVERSAL, original, getdate("2024-09-01")),
		)
		self.assertTrue(frappe.db.exists(LOG, original))
		self.assertIsNone(lifecycle.revert(source, "Again"))  # nothing live left to undo

	def test_a_reversal_after_a_later_change_is_refused(self):
		student = self.student()
		enrollment = self.enroll(student)
		source = ("Program Enrollment", enrollment.name)
		lifecycle.transition(student, rules.ON_BREAK, "2024-09-01", "Illness", source=source)
		lifecycle.transition(student, rules.WITHDRAWN, "2024-10-01", "Left")
		with self.assertRaisesRegex(frappe.ValidationError, "has changed since"):
			lifecycle.revert(source, "Recorded in error")
		self.assertEqual(status(student), rules.WITHDRAWN)

	def test_a_reversed_change_no_longer_dates_later_ones(self):
		student = self.student()
		enrollment = self.enroll(student)
		source = ("Program Enrollment", enrollment.name)
		lifecycle.transition(student, rules.ON_BREAK, "2025-01-10", "Recorded in error", source=source)
		lifecycle.revert(source, "Wrong date")
		lifecycle.transition(
			student, rules.ON_BREAK, "2025-01-05", "The real start"
		)  # before the reversed change
		self.assertEqual(status(student), rules.ON_BREAK)

	def test_detention_is_written_by_the_progression_run_only(self):
		student = self.student(oc_detained=1)
		self.assertEqual(frappe.db.get_value("Student", student, "oc_detained"), 0)
		doc = frappe.get_doc("Student", student)
		doc.oc_detained = 1
		with self.assertRaisesRegex(frappe.ValidationError, "Progression Run"):
			doc.save()

	def test_an_initial_record_is_never_undone(self):
		student = self.student()
		frappe.db.set_value(LOG, {"student": student}, {"source_doctype": "Program", "source_name": PROGRAM})
		with self.assertRaisesRegex(frappe.ValidationError, "cannot be undone automatically"):
			lifecycle.revert(("Program", PROGRAM), "No", student=student)

	def test_the_log_is_written_by_the_service_only_and_never_deleted(self):
		student = self.student()
		with self.assertRaisesRegex(frappe.ValidationError, "lifecycle service"):
			frappe.get_doc(
				{"doctype": LOG, "student": student, "from_status": rules.ACTIVE, "to_status": rules.WITHDRAWN,
				"effective_date": today(), "kind": lifecycle.TRANSITION}
			).insert()  # fmt: skip
		log = self.latest(student)
		log.reason = "Edited"
		with self.assertRaisesRegex(frappe.ValidationError, "lifecycle service"):
			log.save()
		with self.assertRaisesRegex(frappe.ValidationError, "cannot be deleted"):
			frappe.delete_doc(LOG, log.name)


class TestMigration(LifecycleFixture):
	def legacy(self, old_status, **values):
		"""A student as the patch finds it: a Phase 1/2 academic status and no lifecycle record."""
		student = self.student()
		frappe.db.delete(LOG, {"student": student})
		frappe.db.set_value("Student", student, {"oc_academic_status": old_status, **values})
		return student

	def test_detained_becomes_active_and_keeps_the_detention(self):
		student = self.legacy("Detained", joining_date="2024-05-15")
		lifecycle.migrate_student(student)
		self.assertEqual(
			frappe.db.get_value("Student", student, ["oc_academic_status", "oc_detained"]), (rules.ACTIVE, 1)
		)
		log = self.latest(student)
		self.assertEqual(
			(log.from_status, log.to_status, log.kind, log.effective_date),
			("Detained", rules.ACTIVE, lifecycle.MIGRATED, getdate("2024-05-15")),
		)
		self.assertIn("Detained", log.reason)

	def test_a_tc_holder_is_withdrawn_from_the_leaving_date(self):
		student = self.legacy(
			"Discontinued", leaving_certificate_number="TC/LCC/0001", date_of_leaving="2025-03-31"
		)
		lifecycle.migrate_student(student)
		log = self.latest(student)
		self.assertEqual((status(student), log.effective_date), (rules.WITHDRAWN, getdate("2025-03-31")))
		self.assertEqual(
			(log.source_doctype, log.source_name), (None, None)
		)  # no Certificate Request has that number

	def test_the_old_break_and_completed_values_map_onto_the_new_ones(self):
		on_break, completed = self.legacy("Break of Study"), self.legacy("Completed")
		lifecycle.migrate_student(on_break)
		lifecycle.migrate_student(completed)
		self.assertEqual((status(on_break), status(completed)), (rules.ON_BREAK, rules.STUDY_COMPLETED))

	def test_the_migration_logs_once_and_skips_students_with_a_record(self):
		student, recorded = self.legacy("Long Absent"), self.student()
		lifecycle.migrate_students()
		lifecycle.migrate_students()
		self.assertEqual(transitions(student), [("Long Absent", rules.ACTIVE, lifecycle.MIGRATED, None)])
		self.assertEqual(transitions(recorded), [("", rules.ACTIVE, lifecycle.INITIAL, None)])


class TestStatusSummary(LifecycleFixture):
	def test_counts_by_programme_and_batch_for_permitted_companies_only(self):
		first, second, third = self.student(), self.student(), self.student(batch=LATER_YEAR)
		self.student(company=OTHER)
		for student in (first, second):
			self.enroll(student)
		self.enroll(third, OTHER_PROGRAM, LATER_YEAR)
		lifecycle.transition(second, rules.ON_BREAK, today(), "Illness")
		frappe.set_user(CLERK)
		_columns, rows = summary.execute({"company": COLLEGE})
		by_key = {(row["program"], row["batch"]): row for row in rows}
		self.assertEqual(
			(
				by_key[(PROGRAM, YEAR)]["active"],
				by_key[(PROGRAM, YEAR)]["on_break"],
				by_key[(PROGRAM, YEAR)]["total"],
			),
			(1, 1, 2),
		)
		self.assertEqual(by_key[(OTHER_PROGRAM, LATER_YEAR)]["active"], 1)
		self.assertEqual(len(summary.execute({"company": COLLEGE, "program": PROGRAM})[1]), 1)
		self.assertEqual(len(summary.execute({"company": COLLEGE, "batch": LATER_YEAR})[1]), 1)
		frappe.set_user(OUTSIDER)
		self.assertEqual(summary.execute({"company": COLLEGE})[1], [])


class TestAdmissionLifecycle(AdmitTestCase):
	def test_admit_records_admitted_and_the_joining_records_joined_and_active(self):
		applicant = self.applicant()
		student = self.admit_as_officer(applicant)["student"]
		self.assertEqual(status(student), rules.ADMITTED)  # M04-09: approval alone is not joining
		self.assertEqual(transitions(student), [("", rules.ADMITTED, lifecycle.INITIAL, "Student Applicant")])
		result = self.join_as_officer(applicant)
		self.assertEqual(status(student), rules.ACTIVE)
		self.assertEqual(
			transitions(student),
			[
				("", rules.ADMITTED, lifecycle.INITIAL, "Student Applicant"),
				(rules.ADMITTED, rules.JOINED, lifecycle.TRANSITION, "Program Enrollment"),
				(rules.JOINED, rules.ACTIVE, lifecycle.TRANSITION, "Program Enrollment"),
			],
		)
		joined = frappe.get_last_doc(LOG, {"student": student, "to_status": rules.JOINED})
		self.assertEqual(
			(joined.program_enrollment, joined.approved_by), (result["program_enrollment"], OFFICER)
		)

	def test_an_admission_cancellation_records_cancelled(self):
		result = self.admit_and_join(self.applicant())
		admission = SimpleNamespace(**result)
		doc = frappe.get_doc(
			{
				"doctype": "Admission Cancellation",
				"student": admission.student,
				"program_enrollment": admission.program_enrollment,
				"cancellation_date": today(),
				"reason": "Joined another college",
			}
		)
		frappe.set_user(OFFICER)
		doc.insert().submit()
		frappe.set_user("Administrator")
		self.assertEqual(status(admission.student), rules.CANCELLED)
		log = frappe.get_last_doc(LOG, {"student": admission.student}, order_by="creation desc, name desc")
		self.assertEqual(
			(
				log.from_status,
				log.to_status,
				log.source_doctype,
				log.source_name,
				log.program_enrollment,
				log.reason,
			),
			(
				rules.ACTIVE,
				rules.CANCELLED,
				"Admission Cancellation",
				doc.name,
				admission.program_enrollment,
				"Joined another college",
			),
		)
