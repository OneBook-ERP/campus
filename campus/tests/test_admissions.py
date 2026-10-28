"""P1-T14 tests (M04-01, M04-02, M04-04): Verhoeff, masking, eligibility per category, Aadhaar at rest."""

from types import SimpleNamespace
from unittest.mock import patch

import frappe
from frappe.database import get_db
from frappe.tests import UnitTestCase

from campus.admissions import eligibility as elig
from campus.admissions.aadhaar import is_valid_aadhaar, mask, verhoeff_valid
from campus.admissions.api import copy_aadhaar, evaluate_eligibility, reveal_aadhaar
from campus.tests.factories import make_company, make_user
from campus.tests.test_regulation import make_program, make_regulation, make_year, year_name
from campus.tests.utils import CampusTestCase

COLLEGE = "Admissions Test College"
PROGRAM = "Admissions Test BE"
VIEWER, PLAIN = "aadhaar-viewer@example.com", "aadhaar-plain@example.com"
OTHER_COLLEGE, OTHER_VIEWER = "Admissions Other College", "aadhaar-other@example.com"
SUBJECTS = "Mathematics\nPhysics\nChemistry"


def own_connection():
	conf = frappe.local.conf
	return get_db(
		socket=conf.db_socket, host=conf.db_host, port=conf.db_port,
		user=conf.db_user or conf.db_name, password=conf.db_password, cur_db_name=conf.db_name,
	)  # fmt: skip


def log_rows(reference_name=None):
	"""Committed Aadhaar Access Log rows, read on a fresh connection (the test transaction's snapshot predates them)."""
	connection = own_connection()
	try:
		where, values = ("where reference_name = %s", (reference_name,)) if reference_name else ("", ())
		return connection.sql(
			f"select * from `tabAadhaar Access Log` {where} order by creation", values, as_dict=True
		)
	finally:
		connection.close()


def delete_log_rows(reference_names):
	connection = own_connection()
	try:
		for name in reference_names:
			connection.sql("delete from `tabAadhaar Access Log` where reference_name = %s", (name,))
		connection.commit()
	finally:
		connection.close()


def with_check_digit(body):
	return next(body + str(d) for d in range(10) if verhoeff_valid(body + str(d)))


VALID = with_check_digit("23412341234")
INVALID = VALID[:-1] + str((int(VALID[-1]) + 1) % 10)


class TestAadhaarUnit(UnitTestCase):
	def test_known_verhoeff_vector(self):
		self.assertTrue(verhoeff_valid("2363"))  # standard Verhoeff example
		self.assertFalse(verhoeff_valid("2364"))

	def test_valid_and_invalid_numbers(self):
		self.assertTrue(is_valid_aadhaar(VALID))
		self.assertTrue(is_valid_aadhaar(f"{VALID[:4]} {VALID[4:8]}-{VALID[8:]}"))
		self.assertFalse(is_valid_aadhaar(INVALID))
		self.assertFalse(is_valid_aadhaar(VALID[:-1]))  # 11 digits
		self.assertFalse(is_valid_aadhaar("ABCDEFGHIJKL"))

	def test_leading_zero_or_one_rejected_even_with_good_checksum(self):
		for lead in "01":
			self.assertFalse(is_valid_aadhaar(with_check_digit(lead + "3412341234")))

	def test_mask_shows_last_four_only(self):
		self.assertEqual(mask(VALID), f"XXXX-XXXX-{VALID[-4:]}")
		self.assertNotIn(VALID[:8], mask(VALID))


class TestEligibilityUnit(UnitTestCase):
	REQUIRED = ["Mathematics", "Physics", "Chemistry"]
	MINIMUMS = {"OC": 50.0, "BC": 45.0, "SC": 40.0}

	def marks(self, m, p, c):
		return [("Mathematics", 100, m), ("physics", 100, p), ("Chemistry", 100, c)]

	def check(self, marks, category, required=None):
		return elig.check_eligibility(
			self.REQUIRED if required is None else required, self.MINIMUMS, marks, category, "HSC"
		)

	def test_passes_at_exact_minimum(self):
		self.assertEqual(self.check(self.marks(50, 50, 50), "OC"), [])

	def test_below_minimum_for_category_fails_with_reason(self):
		reasons = self.check(self.marks(49, 49, 49), "OC")
		self.assertEqual(len(reasons), 1)
		self.assertIn("49.00%", reasons[0])
		self.assertIn("OC", reasons[0])

	def test_same_marks_pass_for_lower_threshold_category(self):
		self.assertEqual(self.check(self.marks(46, 46, 46), "BC"), [])
		self.assertEqual(self.check(self.marks(41, 41, 41), "SC"), [])
		self.assertNotEqual(self.check(self.marks(41, 41, 41), "BC"), [])

	def test_missing_subject_is_reported(self):
		reasons = self.check([("Mathematics", 100, 90), ("Physics", 100, 90)], "OC")
		self.assertTrue(any("Chemistry" in r for r in reasons))

	def test_no_marks_and_no_category(self):
		self.assertEqual(len(self.check([], "OC")), 1)
		self.assertTrue(any("category" in r for r in self.check(self.marks(90, 90, 90), None)))

	def test_category_without_row_has_no_minimum_and_no_subjects_uses_all_rows(self):
		self.assertEqual(self.check(self.marks(1, 1, 1), "ST"), [])
		self.assertEqual(elig.check_eligibility([], self.MINIMUMS, [("Any", 200, 100)], "OC"), [])
		self.assertNotEqual(elig.check_eligibility([], self.MINIMUMS, [("Any", 200, 99)], "OC"), [])


class TestAdmissionsIntegration(CampusTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		make_company(COLLEGE, "ATC")
		make_program(PROGRAM, COLLEGE)
		make_year("2022")
		cls.regulation = make_regulation("R2022", COLLEGE, [PROGRAM], "2022").name
		make_company(OTHER_COLLEGE, "ATO")
		for user in (VIEWER, PLAIN):
			make_user(user, [COLLEGE])
		make_user(OTHER_VIEWER, [OTHER_COLLEGE])
		frappe.get_doc("User", OTHER_VIEWER).add_roles("Aadhaar Viewer", "Academics User")
		frappe.get_doc("User", VIEWER).add_roles("Aadhaar Viewer", "Academics User")
		frappe.get_doc("User", PLAIN).add_roles("System Manager")
		for dt in ("Student Category",):
			for cat in ("OC", "BC"):
				if not frappe.db.exists(dt, cat):
					frappe.get_doc({"doctype": dt, "category": cat}).insert()
		frappe.db.delete("Student Applicant", {"program": PROGRAM})
		frappe.db.delete("Eligibility Rule", {"program": PROGRAM})
		frappe.get_doc(
			{
				"doctype": "Eligibility Rule", "program": PROGRAM, "qualifying_exam": "HSC",
				"required_subjects": SUBJECTS,
				"category_marks": [
					{"student_category": "OC", "min_percent": 50},
					{"student_category": "BC", "min_percent": 45},
				],
			}
		).insert()  # fmt: skip

	def setUp(self):
		frappe.set_user("Administrator")
		self.touched = []  # documents whose log rows (committed on their own connection) are cleaned up

	def tearDown(self):
		frappe.set_user("Administrator")
		delete_log_rows(self.touched)
		self.touched = []

	def applicant(self, aadhaar=None, category="OC", marks=(60, 60, 60)):
		doc = self.build_applicant(aadhaar, category, marks)
		self.touched.append(doc.name)
		return doc

	def build_applicant(self, aadhaar, category, marks):
		return frappe.get_doc(
			{
				"doctype": "Student Applicant", "first_name": "Admit", "program": PROGRAM,
				"student_email_id": f"admit-{frappe.generate_hash(length=8)}@example.com", "oc_company": COLLEGE,
				"student_category": category, "oc_aadhaar": aadhaar, "academic_year": year_name("2022"),
				"oc_qualifying_marks": [
					{"subject": s, "max_marks": 100, "obtained_marks": m}
					for s, m in zip(SUBJECTS.splitlines(), marks, strict=True)
				],
			}
		).insert()  # fmt: skip

	# Aadhaar
	def test_invalid_checksum_rejected_without_echoing_number(self):
		with self.assertRaises(frappe.ValidationError) as ctx:
			self.applicant(INVALID)
		self.assertNotIn(INVALID, str(ctx.exception))

	def test_valid_number_encrypted_at_rest_masked_everywhere_else(self):
		doc = self.applicant(VALID)
		column = frappe.db.sql("select oc_aadhaar from `tabStudent Applicant` where name=%s", doc.name)[0][0]
		self.assertNotIn(VALID, column)
		self.assertEqual(set(column), {"*"})
		stored = frappe.db.sql(
			"select password from __Auth where doctype='Student Applicant' and name=%s and fieldname='oc_aadhaar'",
			doc.name,
		)[0][0]
		self.assertNotIn(VALID, stored)  # ciphertext
		self.assertEqual(
			frappe.db.get_value("Student Applicant", doc.name, "oc_aadhaar_masked"), f"XXXX-XXXX-{VALID[-4:]}"
		)
		reloaded = frappe.get_doc("Student Applicant", doc.name)
		self.assertNotIn(VALID, frappe.as_json(reloaded.as_dict()))
		self.assertNotIn(VALID, frappe.as_json(frappe.client.get("Student Applicant", doc.name)))
		self.assertNotIn(
			VALID,
			frappe.as_json(frappe.get_list("Student Applicant", fields=["oc_aadhaar", "oc_aadhaar_masked"])),
		)
		html = frappe.get_print("Student Applicant", doc.name)
		self.assertNotIn(VALID, html)
		self.assertNotIn(VALID[:8], html)

	def test_resave_keeps_number_and_mask_and_client_cannot_forge_mask(self):
		doc = self.applicant(VALID)
		doc = frappe.get_doc("Student Applicant", doc.name)
		doc.oc_aadhaar_masked = "XXXX-XXXX-0000"
		doc.save()
		self.assertEqual(doc.oc_aadhaar_masked, f"XXXX-XXXX-{VALID[-4:]}")
		self.assertEqual(doc.get_password("oc_aadhaar"), VALID)

	def test_student_has_the_same_field(self):
		student = frappe.get_doc(
			{"doctype": "Student", "first_name": "Aadhaar", "student_email_id": f"aadhaar-student-{frappe.generate_hash(length=8)}@example.com",
			 "oc_company": COLLEGE, "oc_aadhaar": VALID}
		).insert()  # fmt: skip
		self.touched.append(student.name)
		self.assertEqual(student.oc_aadhaar_masked, f"XXXX-XXXX-{VALID[-4:]}")
		self.assertEqual(student.get_password("oc_aadhaar"), VALID)

	def test_reveal_denied_without_role_and_not_logged(self):
		doc = self.applicant(VALID)
		frappe.set_user(PLAIN)
		with self.assertRaises(frappe.PermissionError):
			reveal_aadhaar("Student Applicant", doc.name)
		self.assertEqual(log_rows(doc.name), [])

	def test_get_password_cannot_bypass_the_role(self):
		doc = self.applicant(VALID)
		frappe.set_user(PLAIN)  # System Manager but not Aadhaar Viewer
		# the dotted path the request handler would run for frappe.client.get_password
		method = frappe.get_attr(
			frappe.get_hooks("override_whitelisted_methods")["frappe.client.get_password"][-1]
		)
		with self.assertRaises(frappe.PermissionError):
			method("Student Applicant", doc.name, "oc_aadhaar")

	def test_reveal_by_viewer_returns_number_and_logs(self):
		doc = self.applicant(VALID)
		frappe.set_user(VIEWER)
		self.assertEqual(reveal_aadhaar("Student Applicant", doc.name), VALID)
		log = log_rows(doc.name)
		self.assertEqual(
			[(r.user, r.reference_doctype, r.reason) for r in log], [(VIEWER, "Student Applicant", "reveal")]
		)
		self.assertTrue(log[0].accessed_on)

	# eligibility
	def test_eligibility_stored_on_applicant(self):
		ok = self.applicant(marks=(60, 60, 60))
		res = evaluate_eligibility(ok.name)
		self.assertTrue(res["eligible"])
		self.assertEqual(res["status"], "Eligible")
		self.assertEqual(res["reasons"], [])
		self.assertEqual(
			frappe.db.get_value("Student Applicant", ok.name, "oc_eligibility_status"), "Eligible"
		)

	def test_ineligible_with_reasons_per_category(self):
		low_oc = self.applicant(category="OC", marks=(46, 46, 46))
		result = evaluate_eligibility(low_oc.name)
		self.assertFalse(result["eligible"])
		self.assertIn("50", result["reasons"][0])
		self.assertEqual(
			frappe.db.get_value("Student Applicant", low_oc.name, "oc_eligibility_status"), "Ineligible"
		)
		self.assertIn("50", frappe.db.get_value("Student Applicant", low_oc.name, "oc_eligibility_reasons"))
		self.assertTrue(
			evaluate_eligibility(self.applicant(category="BC", marks=(46, 46, 46)).name)["eligible"]
		)

	def test_no_rule_for_program_is_not_eligible(self):
		frappe.db.set_value("Eligibility Rule", {"program": PROGRAM}, "enabled", 0)
		try:
			result = evaluate_eligibility(self.applicant().name)
		finally:
			frappe.db.set_value("Eligibility Rule", {"program": PROGRAM}, "enabled", 1)
		self.assertFalse(result["eligible"])

	# masters
	def test_refund_rule_rejects_overlap_and_bad_percent(self):
		def rule(*slabs):
			return frappe.get_doc({"doctype": "Refund Rule", "company": COLLEGE, "slabs": [
				{"days_from_admission_start": a, "days_to_admission_start": b, "deduction_percent": p} for a, b, p in slabs]})  # fmt: skip

		rule((0, 10, 0), (11, 30, 10)).insert()
		with self.assertRaises(frappe.ValidationError):
			rule((0, 10, 0), (10, 30, 10)).insert()
		with self.assertRaises(frappe.ValidationError):
			rule((0, 10, 120)).insert()

	def test_counselling_authority_mapping_targets_must_exist(self):
		def authority(target):
			return frappe.get_doc({"doctype": "Counselling Authority", "authority_name": "Test Authority",
				"column_mapping": [{"source_column": "Rank", "target_field": target}]})  # fmt: skip

		frappe.db.delete("Counselling Authority", {"authority_name": "Test Authority"})
		with self.assertRaises(frappe.ValidationError):
			authority("no_such_field").insert()
		authority("oc_rank").insert()

	# review fixes
	def test_reveal_endpoints_are_post_only(self):
		self.assertEqual(frappe.allowed_http_methods_for_whitelisted_func[reveal_aadhaar], ["POST"])
		doc = self.applicant(VALID)
		method = frappe.get_attr(
			frappe.get_hooks("override_whitelisted_methods")["frappe.client.get_password"][-1]
		)
		frappe.set_user(VIEWER)
		with patch.object(frappe.local, "request", SimpleNamespace(method="GET"), create=True):
			with self.assertRaises(frappe.PermissionError):
				method("Student Applicant", doc.name, "oc_aadhaar")
		with patch.object(frappe.local, "request", SimpleNamespace(method="POST"), create=True):
			self.assertEqual(method("Student Applicant", doc.name, "oc_aadhaar"), VALID)

	def test_reveal_log_carries_ip(self):
		doc = self.applicant(VALID)
		frappe.set_user(VIEWER)
		previous = getattr(frappe.local, "request_ip", None)
		frappe.local.request_ip = "10.1.2.3"
		try:
			reveal_aadhaar("Student Applicant", doc.name)
		finally:
			frappe.local.request_ip = previous
		self.assertEqual(log_rows(doc.name)[0].ip_address, "10.1.2.3")

	def test_jinja_get_password_is_gated_and_logged(self):
		doc = self.applicant(VALID)
		template = "{{ doc.get_password('oc_aadhaar') }}"
		frappe.set_user(PLAIN)
		with self.assertRaises(frappe.PermissionError):
			frappe.render_template(template, {"doc": frappe.get_doc("Student Applicant", doc.name)})
		frappe.set_user(VIEWER)
		self.assertEqual(
			frappe.render_template(template, {"doc": frappe.get_doc("Student Applicant", doc.name)}), VALID
		)
		self.assertEqual(len(log_rows(doc.name)), 1)

	def test_other_password_fields_still_readable_by_system_manager(self):
		name = "Admissions Pwd Test"
		frappe.db.delete("Connected App", {"name": name})
		app = frappe.get_doc(
			{"doctype": "Connected App", "provider_name": name, "client_id": "cid", "client_secret": "s3cret",
			 "authorization_uri": "https://example.com/a", "token_uri": "https://example.com/t",
			 "scopes": [{"scope": "read"}]}
		).insert()  # fmt: skip
		frappe.set_user(PLAIN)
		method = frappe.get_attr(
			frappe.get_hooks("override_whitelisted_methods")["frappe.client.get_password"][-1]
		)
		self.assertEqual(method("Connected App", app.name, "client_secret"), "s3cret")

	def test_viewer_of_another_company_is_denied(self):
		doc = self.applicant(VALID)
		frappe.set_user(OTHER_VIEWER)
		with self.assertRaises(frappe.PermissionError):
			reveal_aadhaar("Student Applicant", doc.name)

	def test_access_log_rows_cannot_be_deleted_or_edited(self):
		doc = self.applicant(VALID)
		frappe.set_user(VIEWER)
		reveal_aadhaar("Student Applicant", doc.name)
		frappe.set_user("Administrator")
		row = frappe.get_doc({**log_rows(doc.name)[0], "doctype": "Aadhaar Access Log"})  # a saved row
		with self.assertRaises(frappe.PermissionError):
			row.on_trash()
		with self.assertRaises(frappe.PermissionError):
			row.validate()

	def test_rule_with_matching_regulation_wins_and_duplicates_rejected(self):
		blank = frappe.db.get_value("Eligibility Rule", {"program": PROGRAM, "enabled": 1})
		with self.assertRaises(frappe.ValidationError):
			frappe.get_doc(
				{"doctype": "Eligibility Rule", "program": PROGRAM, "qualifying_exam": "HSC"}
			).insert()
		self.assertEqual(elig.find_rule(PROGRAM, "No Such Regulation").name, blank)  # falls back to blank
		specific = frappe.get_doc(
			{
				"doctype": "Eligibility Rule",
				"program": PROGRAM,
				"qualifying_exam": "HSC",
				"regulation": self.regulation,
			}
		).insert()
		self.assertEqual(elig.find_rule(PROGRAM, self.regulation).name, specific.name)
		self.assertEqual(elig.find_rule(PROGRAM).name, blank)

	def test_every_reveal_row_survives_a_rollback_by_the_caller(self):
		doc = self.applicant(VALID)
		frappe.set_user(VIEWER)
		frappe.db.savepoint("reveals")
		frappe.get_doc("Student Applicant", doc.name).get_password("oc_aadhaar")  # as a Server Script would
		reveal_aadhaar("Student Applicant", doc.name)
		frappe.db.rollback(save_point="reveals")
		self.assertEqual(len(log_rows(doc.name)), 2)

	def test_reveal_fails_closed_when_the_log_cannot_be_written(self):
		doc = self.applicant(VALID)
		frappe.set_user(VIEWER)
		with patch("campus.admissions.aadhaar.write_on_own_connection", side_effect=RuntimeError("db down")):
			with patch(
				"frappe.log_error", side_effect=RuntimeError("log_error down")
			):  # must not mask the failure
				with self.assertRaises(frappe.ValidationError) as ctx:
					reveal_aadhaar("Student Applicant", doc.name)
		self.assertNotIn(VALID, str(ctx.exception))
		self.assertEqual(log_rows(doc.name), [])

	def test_failed_log_write_records_an_error_without_site_secrets(self):
		doc = self.applicant(VALID)
		frappe.set_user(VIEWER)
		with patch("campus.admissions.aadhaar.get_db", side_effect=RuntimeError("db down")):
			with self.assertRaises(frappe.ValidationError):
				reveal_aadhaar("Student Applicant", doc.name)
		frappe.set_user("Administrator")
		row = frappe.get_all(
			"Error Log", filters={"method": "Aadhaar Access Log write failed"}, fields=["name", "error"],
			order_by="creation desc", limit=1,
		)[0]  # fmt: skip
		self.addCleanup(frappe.db.delete, "Error Log", row.name)
		error = row.error
		self.assertIn("db down", error)
		for secret in (frappe.conf.db_password, frappe.conf.encryption_key):
			self.assertTrue(secret)
			self.assertFalse(
				secret in error, "Error Log holds a site secret"
			)  # never echo the secret on failure
		self.assertFalse(VALID in error, "Error Log holds the Aadhaar number")
		self.assertEqual(log_rows(doc.name), [])

	def test_reveal_without_a_stored_number_writes_no_log_row(self):
		doc = self.applicant()
		frappe.set_user(VIEWER)
		with patch("campus.admissions.aadhaar.write_on_own_connection") as write:
			self.assertFalse(reveal_aadhaar("Student Applicant", doc.name))
		write.assert_not_called()
		self.assertEqual(log_rows(doc.name), [])

	def test_copy_aadhaar_carries_number_and_logs_a_system_copy(self):
		source = self.applicant(VALID)
		student = frappe.get_doc(
			{"doctype": "Student", "first_name": "Copy", "oc_company": COLLEGE,
			 "student_email_id": f"copy-{frappe.generate_hash(length=8)}@example.com"}
		)  # fmt: skip
		copy_aadhaar(frappe.get_doc("Student Applicant", source.name), student)
		student.insert()
		self.assertEqual(student.oc_aadhaar_masked, f"XXXX-XXXX-{VALID[-4:]}")
		self.touched.append(student.name)
		self.assertEqual(frappe.get_doc("Student", student.name).get_password("oc_aadhaar"), VALID)
		log = log_rows(source.name)[0]
		self.assertEqual((log.reason, log.user), ("admission copy", "Administrator"))
		self.assertFalse(hasattr(copy_aadhaar, "__wrapped__") or copy_aadhaar in frappe.whitelisted)

	def test_copy_without_a_number_does_nothing(self):
		source, student = self.applicant(), frappe.new_doc("Student")
		copy_aadhaar(source, student)
		self.assertFalse(student.get("oc_aadhaar"))

	def test_log_references_the_document_by_dynamic_link(self):
		"""Frappe's delete-time link check follows Dynamic Links, so reveal history blocks deleting the document.

		Not exercised end to end: the log row is committed on its own connection, which the test transaction's
		snapshot cannot see; in a real request the delete runs in a new transaction.
		"""
		meta = frappe.get_meta("Aadhaar Access Log")
		field = meta.get_field("reference_name")
		self.assertEqual((field.fieldtype, field.options), ("Dynamic Link", "reference_doctype"))

	def test_enabled_rule_check_takes_a_lock_and_rejects_clash(self):
		rule = frappe.get_doc(
			{"doctype": "Eligibility Rule", "program": PROGRAM, "qualifying_exam": "HSC", "regulation": None}
		)
		with patch.object(frappe.db, "get_value", wraps=frappe.db.get_value) as get_value:
			with self.assertRaises(frappe.ValidationError):
				rule.insert()
		self.assertTrue(any(c.kwargs.get("for_update") for c in get_value.call_args_list))
