"""M01-05 / M03-03 / M07-04 (P3-T07) Feedback engine: forms with CO-tagged questions, campaigns, anonymous responses
that keep no user link, one response per respondent through a separate hashed token table, k-anonymous analysis and
the indirect CO scores of a course offering."""

import json

import frappe
from frappe.tests import IntegrationTestCase, UnitTestCase
from frappe.utils import add_days, getdate, today

from campus.iqac import api, feedback, feedback_invites
from campus.iqac.feedback_seed import SEEDED_FORMS, seed_feedback_forms
from campus.iqac.report.feedback_analysis import feedback_analysis
from campus.tests import broadcast_fixtures as fx
from campus.tests.factories import make_user

COURSE = "T07 Data Structures"
OFFERING = "T07 Data Structures A"
COORDINATOR = "t07-iqac@example.com"
TEACHER_USER = "t07-teacher@example.com"
EXTRA_USERS = [f"t07-student-{n}@example.com" for n in range(4)]
MEMBER = "T07 Employer"


def make_offering(students):
	if not frappe.db.exists("Course", COURSE):
		frappe.get_doc({"doctype": "Course", "course_name": COURSE, "oc_company": fx.COLLEGE}).insert(ignore_permissions=True)
	name = frappe.db.get_value("Student Group", {"student_group_name": OFFERING})
	if name:
		return name
	return frappe.get_doc(
		{
			"doctype": "Student Group", "student_group_name": OFFERING, "group_based_on": "Course", "academic_year": fx.YEAR,
			"program": fx.CSE, "course": COURSE, "oc_company": fx.COLLEGE, "students": [{"student": s} for s in students],
		}
	).insert(ignore_permissions=True).name  # fmt: skip


def make_member():
	name = frappe.db.get_value("External Member", {"member_name": MEMBER})
	return name or frappe.get_doc(
		{"doctype": "External Member", "member_name": MEMBER, "organisation": "T07 Industries", "email": "t07-employer@example.com"}
	).insert(ignore_permissions=True).name


class TestRules(UnitTestCase):
	def test_reminders_fall_every_n_days_after_the_start_and_never_on_it(self):
		start = getdate("2026-01-01")
		self.assertFalse(feedback_invites.reminder_due(start, 3, getdate("2026-01-01")))
		self.assertFalse(feedback_invites.reminder_due(start, 3, getdate("2026-01-02")))
		self.assertTrue(feedback_invites.reminder_due(start, 3, getdate("2026-01-04")))
		self.assertTrue(feedback_invites.reminder_due(start, 3, getdate("2026-01-07")))
		self.assertFalse(feedback_invites.reminder_due(start, 0, getdate("2026-01-04")))

	def test_the_token_is_a_keyed_hash_of_campaign_and_respondent_and_never_contains_them(self):
		token = feedback.token_name("FBC-1", "Student:EDU-STU-1")
		self.assertEqual(len(token), 64)
		self.assertNotIn("EDU-STU-1", token)
		self.assertEqual(token, feedback.token_name("FBC-1", "Student:EDU-STU-1"))
		self.assertNotEqual(token, feedback.token_name("FBC-2", "Student:EDU-STU-1"))
		self.assertNotEqual(token, feedback.token_name("FBC-1", "Student:EDU-STU-2"))

	def test_an_invitation_signature_checks_campaign_and_member(self):
		signature = feedback.invite_signature("FBC-1", "External Member:M1")
		self.assertTrue(feedback.valid_signature("FBC-1", "External Member:M1", signature))
		self.assertFalse(feedback.valid_signature("FBC-1", "External Member:M2", signature))
		self.assertFalse(feedback.valid_signature("FBC-2", "External Member:M1", signature))
		self.assertFalse(feedback.valid_signature("FBC-1", "External Member:M1", ""))


class FeedbackFixture(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.world = fx.setup_world()
		cls.students = [cls.world["asha"], cls.world["bala"]]
		for number, email in enumerate(EXTRA_USERS):
			fx.make_login(email, "Student")
			cls.students.append(fx.make_student(f"T07Stu{number}", fx.COLLEGE, fx.CSE, user=email))
		cls.offering = make_offering(cls.students)
		cls.member = make_member()
		make_user(COORDINATOR, [fx.COLLEGE])
		frappe.get_doc("User", COORDINATOR).add_roles("IQAC Coordinator")
		fx.make_login(TEACHER_USER, "Academics User")
		frappe.db.set_value("Employee", cls.world["emp_1"], "user_id", TEACHER_USER)
		seed_feedback_forms()
		frappe.db.set_single_value("Campus Settings", "feedback_minimum_group_size", 3)

	def form(self, questions=None, name=None):
		questions = questions or [
			{"question": "The course met CO1", "question_type": "Likert", "course_outcome": "CO1"},
			{"question": "The course met CO2", "question_type": "Likert", "course_outcome": "CO2"},
			{"question": "Pace", "question_type": "Choice", "options": "Too slow\nRight\nToo fast"},
			{"question": "Comments", "question_type": "Text", "is_required": 0},
		]
		return frappe.get_doc(
			{"doctype": "Feedback Form", "form_name": name or f"T07 Form {frappe.generate_hash(length=6)}", "questions": questions}
		).insert()

	def campaign(self, form=None, **values):
		doc = {
			"doctype": "Feedback Campaign", "campaign_title": "T07 Exit Survey", "form": (form or self.form()).name,
			"company": fx.COLLEGE, "audience": "Course Offering Students", "course_offering": self.offering,
			"start_date": add_days(today(), -2), "end_date": add_days(today(), 5), "is_anonymous": 1, **values,
		}  # fmt: skip
		return frappe.get_doc(doc).insert()

	def answers(self, campaign, ratings=(5, 4), choice="Right", text="Good"):
		rows = frappe.get_doc("Feedback Form", campaign.form).questions
		values = dict(zip((r.name for r in rows), [*ratings, choice, text]))
		return json.dumps(values)

	def close(self, *campaigns):
		"""End the window yesterday (direct write: test setup); only closed campaigns are analysed."""
		for campaign in campaigns:
			frappe.db.set_value("Feedback Campaign", campaign.name, "end_date", add_days(today(), -1))

	def respond(self, campaign, user, **answers):
		frappe.set_user(user)
		try:
			return feedback.submit_response(campaign.name, self.answers(campaign, **answers))
		finally:
			frappe.set_user("Administrator")


class TestFormAndCampaign(FeedbackFixture):
	def test_a_choice_question_needs_options_and_a_co_tag_needs_a_likert_question(self):
		with self.assertRaises(frappe.ValidationError):
			self.form([{"question": "Pick", "question_type": "Choice", "options": "Only one"}])
		with self.assertRaises(frappe.ValidationError):
			self.form([{"question": "Say", "question_type": "Text", "course_outcome": "CO1"}])
		self.assertTrue(self.form([{"question": "Rate", "question_type": "Likert", "course_outcome": "CO3"}]).name)

	def test_the_window_must_end_after_it_starts_and_an_offering_audience_needs_the_offering(self):
		with self.assertRaises(frappe.ValidationError):
			self.campaign(start_date=today(), end_date=add_days(today(), -1))
		with self.assertRaises(frappe.ValidationError):
			self.campaign(course_offering=None)

	def test_form_audience_and_anonymity_are_locked_once_a_response_exists(self):
		campaign = self.campaign()
		self.respond(campaign, fx.STUDENT_USER)
		campaign.reload()
		campaign.is_anonymous = 0
		with self.assertRaises(frappe.ValidationError):
			campaign.save()
		campaign.reload()
		campaign.action_taken = "<p>Lab hours extended</p>"  # the action-taken note stays editable
		campaign.save()

	def test_the_seeded_forms_are_placeholders_and_the_course_exit_survey_is_co_tagged(self):
		for name in SEEDED_FORMS:
			self.assertTrue(frappe.db.get_value("Feedback Form", name, "is_placeholder"), name)
		tags = frappe.get_all("Feedback Question", {"parent": "Course Exit Survey", "question_type": "Likert"}, pluck="course_outcome")
		self.assertTrue(all(tags) and len(tags) >= 5)
		self.assertTrue({"Curriculum Feedback - Students", "Curriculum Feedback - Teachers", "Curriculum Feedback - Employers", "Curriculum Feedback - Alumni"} <= set(SEEDED_FORMS))

	def test_feedback_campaign_is_an_evidence_source_with_its_action_taken_report(self):
		from campus.iqac.setup import seed_evidence_sources

		seed_evidence_sources()
		field = frappe.db.get_value("Evidence Source Doctype", {"parent": "Campus Settings", "document_type": "Feedback Campaign"}, "file_field")
		self.assertEqual(field, "action_taken_report")


class TestAnonymity(FeedbackFixture):
	def stored(self, name):
		"""Every stored column of the response, its answers and the campaign's tokens, read straight from the tables."""
		response = frappe.db.sql("select * from `tabFeedback Response` where name=%s", name, as_dict=True)[0]
		answers = frappe.db.sql("select * from `tabFeedback Answer` where parent=%s", name, as_dict=True)
		tokens = frappe.db.sql("select * from `tabFeedback Response Token` where campaign=%s", response.campaign, as_dict=True)
		return response, answers, tokens

	def test_an_anonymous_response_stores_no_user_student_or_time_of_day(self):
		campaign = self.campaign()
		name = self.respond(campaign, fx.STUDENT_USER)
		response, answers, tokens = self.stored(name)
		identities = {fx.STUDENT_USER, self.world["asha"], "T11Asha", frappe.db.get_value("Student", self.world["asha"], "student_name")}
		for row in [response, *answers, *tokens]:
			for column, value in row.items():
				self.assertFalse(any(i and i in str(value or "") for i in identities), (column, value))
		for row in [response, *answers]:
			self.assertEqual((row.owner, row.modified_by), ("Guest", "Guest"))
			self.assertEqual((row.creation.hour, row.creation.minute, row.creation.second, row.creation.microsecond), (0, 0, 0, 0))
			self.assertEqual(row.modified, row.creation)
		self.assertEqual(response.response_date, getdate(today()))
		self.assertFalse(response.respondent)
		[token] = tokens
		self.assertEqual((token.owner, token.modified_by), ("Guest", "Guest"))
		self.assertEqual(token.creation.date(), getdate(campaign.start_date))  # the campaign's start, not the response day
		self.assertFalse(token._comments)  # a framework column (no longer in optional_fields since 16.53), left empty
		extra = set(token) - set(frappe.model.default_fields) - set(frappe.model.optional_fields) - {"campaign", "_comments"}
		self.assertEqual(extra, set())  # nothing but the hash and the campaign

	def test_no_version_follow_comment_or_log_names_the_response(self):
		frappe.db.set_value("User", fx.STUDENT_USER, "follow_created_documents", 1)
		campaign = self.campaign()
		name = self.respond(campaign, fx.STUDENT_USER)
		for doctype, field in (("Version", "docname"), ("Document Follow", "ref_docname"), ("Comment", "reference_name"), ("Activity Log", "reference_name")):
			self.assertFalse(frappe.db.exists(doctype, {field: name}), doctype)
		self.assertFalse(frappe.get_meta("Feedback Response").track_changes)

	def test_staff_cannot_read_single_responses_or_tokens(self):
		campaign = self.campaign()
		self.respond(campaign, fx.STUDENT_USER)
		frappe.set_user(COORDINATOR)
		self.assertFalse(frappe.has_permission("Feedback Response", "read"))
		self.assertFalse(frappe.has_permission("Feedback Response Token", "read"))

	def test_a_response_cannot_be_made_or_edited_through_the_document_api(self):
		campaign = self.campaign()
		with self.assertRaises(frappe.ValidationError):
			frappe.get_doc({"doctype": "Feedback Response", "campaign": campaign.name, "response_date": today()}).insert(ignore_permissions=True)
		name = self.respond(campaign, fx.STUDENT_USER)
		doc = frappe.get_doc("Feedback Response", name)
		doc.respondent = "Student:" + self.world["asha"]
		with self.assertRaises(frappe.ValidationError):
			doc.save(ignore_permissions=True)

	def test_no_error_log_holds_answers_after_a_refused_or_failed_submit(self):
		from unittest.mock import patch

		campaign, other = self.campaign(), self.campaign()
		before = set(frappe.get_all("Error Log", pluck="name"))
		secret = "SECRET-ANSWER-T07"
		self.respond(campaign, fx.STUDENT_USER, text=secret)
		frappe.set_user(fx.STUDENT_USER)
		try:
			with self.assertRaises(frappe.ValidationError):
				feedback.submit_response(campaign.name, self.answers(campaign, text=secret))  # duplicate
			for malformed in ("not json", "[1, 2]", "42"):
				with self.assertRaises(frappe.ValidationError):
					feedback.submit_response(campaign.name, malformed)
			with patch("campus.iqac.feedback.store_response", side_effect=KeyError(secret)):
				with self.assertRaisesRegex(frappe.ValidationError, "could not be recorded") as raised:
					feedback.submit_response(other.name, self.answers(other, text=secret))
				self.assertNotIn(secret, str(raised.exception))
		finally:
			frappe.set_user("Administrator")
		new = frappe.get_all("Error Log", {"name": ["not in", list(before) or [""]]}, ["method", "error"])
		self.assertEqual([row.error for row in new], ["KeyError"])  # the failure is logged by class name only
		for row in new:
			for text in (secret, fx.STUDENT_USER, self.world["asha"]):
				self.assertNotIn(text, f"{row.method} {row.error}")

	def test_a_failed_submit_inside_a_request_logs_no_form_data_or_user(self):
		from types import SimpleNamespace
		from unittest.mock import patch

		campaign = self.campaign()
		secret = "SECRET-REQUEST-T07"
		before = set(frappe.get_all("Error Log", pluck="name"))
		saved_request, saved_form = getattr(frappe.local, "request", None), frappe.local.form_dict
		frappe.local.request = SimpleNamespace(method="POST", path="/api/method/campus.iqac.feedback.submit_response", referrer=None)
		answers = self.answers(campaign, text=secret)
		frappe.set_user(fx.STUDENT_USER)
		frappe.local.form_dict = frappe._dict(
			cmd="campus.iqac.feedback.submit_response", campaign=campaign.name, answers=answers, member="T07-MEMBER", signature="T07-SIGNATURE"
		)
		try:
			with patch("campus.iqac.feedback.store_response", side_effect=KeyError(secret)):
				with self.assertRaisesRegex(frappe.ValidationError, "could not be recorded"):
					feedback.submit_response(campaign.name, answers)
			self.assertFalse({"campaign", "answers", "member", "signature"} & set(frappe.form_dict))  # nothing later sees them
		finally:
			frappe.set_user("Administrator")
			frappe.local.request, frappe.local.form_dict = saved_request, saved_form
		new = frappe.get_all("Error Log", {"name": ["not in", list(before) or [""]]}, ["error", "metadata"])
		self.assertEqual([row.error for row in new], ["KeyError"])
		for text in (secret, fx.STUDENT_USER, "T07-MEMBER", "T07-SIGNATURE", campaign.name):
			self.assertNotIn(text, new[0].metadata or "")

	def test_a_failed_rollback_or_error_log_still_ends_in_the_generic_refusal(self):
		from unittest.mock import patch

		campaign, other, third = self.campaign(), self.campaign(), self.campaign()
		secret = "SECRET-ROLLBACK-T07"
		self.respond(campaign, fx.STUDENT_USER)
		before = set(frappe.get_all("Error Log", pluck="name"))
		lost = frappe.db.OperationalError(2013, "Lost connection")
		frappe.set_user(fx.STUDENT_USER)
		try:
			with patch.object(frappe.db, "rollback", side_effect=lost):  # neither the savepoint nor a full rollback works
				with self.assertRaisesRegex(frappe.ValidationError, "already answered"):
					feedback.submit_response(campaign.name, self.answers(campaign, text=secret))  # the refusal, not the rollback error
				with patch("campus.iqac.feedback.store_response", side_effect=KeyError(secret)):
					with self.assertRaisesRegex(frappe.ValidationError, "could not be recorded") as raised:
						feedback.submit_response(other.name, self.answers(other, text=secret))
			self.assertIsNone(raised.exception.__context__)  # no chain carries the failure or its frames
			with patch("campus.iqac.feedback.store_response", side_effect=KeyError(secret)):
				with patch("campus.iqac.feedback.log_failure", side_effect=lost):
					with self.assertRaisesRegex(frappe.ValidationError, "could not be recorded") as raised:
						feedback.submit_response(third.name, self.answers(third, text=secret))  # other's token stayed: rollback was patched
			self.assertIsNone(raised.exception.__context__)
		finally:
			frappe.set_user("Administrator")
		new = frappe.get_all("Error Log", {"name": ["not in", list(before) or [""]]}, ["error", "metadata", "owner", "creation"])
		self.assertEqual([row.error for row in new], ["KeyError"])
		for text in (secret, fx.STUDENT_USER, self.world["asha"]):
			self.assertNotIn(text, f"{new[0].error} {new[0].metadata}")

	def test_the_failure_log_does_not_mark_the_respondent(self):
		from unittest.mock import patch

		campaign = self.campaign()
		before = set(frappe.get_all("Error Log", pluck="name"))
		frappe.set_user(fx.STUDENT_USER)
		try:
			with patch("campus.iqac.feedback.store_response", side_effect=KeyError("x")):
				with self.assertRaisesRegex(frappe.ValidationError, "could not be recorded"):
					feedback.submit_response(campaign.name, self.answers(campaign))
		finally:
			frappe.set_user("Administrator")
		(row,) = frappe.get_all("Error Log", {"name": ["not in", list(before) or [""]]}, ["owner", "modified_by", "creation"])
		self.assertEqual((row.owner, row.modified_by), ("Guest", "Guest"))
		self.assertEqual(str(row.creation), f"{today()} 00:00:00")  # the day only, like a response

	def test_a_call_missing_its_arguments_is_refused_cleanly(self):
		campaign = self.campaign()
		frappe.set_user(fx.STUDENT_USER)
		try:
			with self.assertRaisesRegex(frappe.PermissionError, "not available"):
				feedback.submit_response()
			with self.assertRaisesRegex(frappe.ValidationError, "expected form"):
				feedback.submit_response(campaign.name)
		finally:
			frappe.set_user("Administrator")

	def test_a_blank_minimum_group_size_means_the_default(self):
		settings = frappe.get_single("Campus Settings")
		settings.feedback_minimum_group_size = None
		settings.save()  # blank is not a save blocker
		self.assertEqual(frappe.db.get_single_value("Campus Settings", "feedback_minimum_group_size"), api.MINIMUM_GROUP_DEFAULT)
		frappe.db.set_single_value("Campus Settings", "feedback_minimum_group_size", 0)
		try:
			self.assertEqual(feedback.minimum_group_size(), api.MINIMUM_GROUP_DEFAULT)
		finally:
			frappe.db.set_single_value("Campus Settings", "feedback_minimum_group_size", 3)

	def test_the_audience_size_counts_people_not_their_records(self):
		from unittest.mock import patch

		asha, bala = self.world["asha"], self.world["bala"]
		keys = [f"Student:{asha}", f"User:{fx.STUDENT_USER}", f"Student:{bala}", "External Member:M1", "External Member:M2"]
		self.assertEqual(feedback.audience_size(keys), 4)  # Asha's student record and login are one person
		campaign = self.campaign()
		with patch("campus.iqac.feedback.respondents", return_value=keys[:3]):  # 3 keys, 2 people, minimum 3
			with self.assertRaisesRegex(frappe.ValidationError, "too small"):
				self.respond(campaign, fx.STUDENT_USER)

	def test_an_existing_campaign_with_a_rule_still_saves_when_only_its_note_changes(self):
		rule = fx.make_rule(f"T07 Rule {frappe.generate_hash(length=4)}", "Student Group", student_group=self.offering)
		campaign = self.campaign(audience="Students", audience_rule=rule.name, course_offering=None)
		frappe.db.set_value("Audience Rule", rule.name, "company", fx.OTHER_COLLEGE)  # the rule was re-scoped later
		campaign.reload()
		campaign.action_taken = "Lab hours added"
		campaign.save()
		campaign.audience_rule = None
		campaign.audience_rule = rule.name
		campaign.audience = "Parents"  # an audience change is checked again
		with self.assertRaisesRegex(frappe.ValidationError, "is not of"):
			campaign.save()

	def test_the_minimum_group_size_never_falls_below_three(self):
		settings = frappe.get_single("Campus Settings")
		settings.feedback_minimum_group_size = 2
		with self.assertRaisesRegex(frappe.ValidationError, "at least 3"):
			settings.save()
		frappe.db.set_single_value("Campus Settings", "feedback_minimum_group_size", 1)  # a direct write cannot lower it
		try:
			self.assertEqual(feedback.minimum_group_size(), 3)
		finally:
			frappe.db.set_single_value("Campus Settings", "feedback_minimum_group_size", 3)

	def test_an_anonymous_campaign_below_the_minimum_audience_takes_no_answers_or_invitations(self):
		parents = self.campaign(audience="Parents")  # one guardian, minimum 3
		with self.assertRaisesRegex(frappe.ValidationError, "too small"):
			self.respond(parents, fx.GUARDIAN_USER)
		with self.assertRaisesRegex(frappe.ValidationError, "too small"):
			feedback_invites.send_invitations(parents.name)

	def test_an_unknown_campaign_and_an_unaddressed_user_get_the_same_refusal(self):
		campaign = self.campaign()
		frappe.set_user(fx.OTHER_COLLEGE_USER)
		try:
			with self.assertRaises(frappe.PermissionError) as unknown:
				feedback.submit_response("FBC-NO-SUCH", "{}")
			with self.assertRaises(frappe.PermissionError) as unaddressed:
				feedback.submit_response(campaign.name, self.answers(campaign))
		finally:
			frappe.set_user("Administrator")
		self.assertEqual(str(unknown.exception), str(unaddressed.exception))

	def test_every_key_of_the_user_in_the_audience_is_claimed(self):
		from unittest.mock import patch

		campaign = self.campaign()
		keys = [f"Student:{self.world['asha']}", f"User:{fx.STUDENT_USER}", *(f"Student:{s}" for s in self.students[1:])]
		with patch("campus.iqac.feedback.respondents", return_value=keys):
			self.respond(campaign, fx.STUDENT_USER)
		self.assertTrue(feedback.has_responded(campaign.name, f"User:{fx.STUDENT_USER}"))
		self.assertTrue(feedback.has_responded(campaign.name, f"Student:{self.world['asha']}"))

	def test_a_named_campaign_keeps_the_respondent(self):
		campaign = self.campaign(is_anonymous=0)
		name = self.respond(campaign, fx.STUDENT_USER)
		self.assertEqual(frappe.db.get_value("Feedback Response", name, "respondent"), f"Student:{self.world['asha']}")


class TestOneResponseEach(FeedbackFixture):
	def test_a_second_response_from_the_same_respondent_is_refused(self):
		campaign = self.campaign()
		self.respond(campaign, fx.STUDENT_USER)
		with self.assertRaisesRegex(frappe.ValidationError, "already"):
			self.respond(campaign, fx.STUDENT_USER)
		self.assertEqual(frappe.db.count("Feedback Response", {"campaign": campaign.name}), 1)
		self.respond(campaign, EXTRA_USERS[0])  # another student is not blocked
		self.assertEqual(frappe.db.count("Feedback Response", {"campaign": campaign.name}), 2)

	def test_the_same_student_may_answer_another_campaign(self):
		self.respond(self.campaign(), fx.STUDENT_USER)
		self.respond(self.campaign(), fx.STUDENT_USER)

	def test_only_eligible_respondents_inside_the_window_may_answer(self):
		campaign = self.campaign()
		with self.assertRaises(frappe.PermissionError):
			self.respond(campaign, fx.OTHER_COLLEGE_USER)  # not a student of the offering
		closed = self.campaign(start_date=add_days(today(), -9), end_date=add_days(today(), -1))
		with self.assertRaisesRegex(frappe.ValidationError, "not open"):
			self.respond(closed, fx.STUDENT_USER)
		with self.assertRaises(frappe.PermissionError):
			self.respond(campaign, "Guest")  # a visitor without an invitation link

	def test_a_required_question_and_answer_values_are_checked(self):
		campaign = self.campaign()
		with self.assertRaises(frappe.ValidationError):
			self.respond(campaign, fx.STUDENT_USER, ratings=(6, 4))
		with self.assertRaises(frappe.ValidationError):
			self.respond(campaign, fx.STUDENT_USER, choice="Sideways")
		with self.assertRaises(frappe.ValidationError):
			self.respond(campaign, fx.STUDENT_USER, ratings=(None, 4))
		self.assertTrue(self.respond(campaign, fx.STUDENT_USER, text=None))  # the comment is optional

	def test_parents_and_teachers_of_the_offering_answer_once_each(self):
		parents = self.campaign(audience="Parents", is_anonymous=0)  # one guardian: below the minimum
		self.respond(parents, fx.GUARDIAN_USER)
		with self.assertRaisesRegex(frappe.ValidationError, "already"):
			self.respond(parents, fx.GUARDIAN_USER)
		group = frappe.get_doc("Student Group", self.offering)
		instructor = frappe.db.get_value("Instructor", {"employee": self.world["emp_1"]}) or frappe.get_doc(
			{"doctype": "Instructor", "instructor_name": "T07 Ravi", "employee": self.world["emp_1"], "oc_company": fx.COLLEGE}
		).insert(ignore_permissions=True).name
		if not group.instructors:
			group.append("instructors", {"instructor": instructor})
			group.save(ignore_permissions=True)
		teachers = self.campaign(audience="Teachers", is_anonymous=0)
		self.respond(teachers, TEACHER_USER)
		with self.assertRaises(frappe.PermissionError):
			self.respond(teachers, fx.STUDENT_USER)

	def test_an_invited_employer_answers_once_through_the_signed_link(self):
		campaign = self.campaign(audience="Employers", course_offering=None, invitees=[{"member": self.member}], is_anonymous=0)
		key = f"External Member:{self.member}"
		signature = feedback.invite_signature(campaign.name, key)
		frappe.set_user("Guest")
		try:
			with self.assertRaises(frappe.PermissionError):
				feedback.submit_response(campaign.name, self.answers(campaign), member=self.member, signature="0" * 32)
			self.assertTrue(feedback.submit_response(campaign.name, self.answers(campaign), member=self.member, signature=signature))
			with self.assertRaisesRegex(frappe.ValidationError, "already"):
				feedback.submit_response(campaign.name, self.answers(campaign), member=self.member, signature=signature)
		finally:
			frappe.set_user("Administrator")

	def test_the_answer_page_shows_the_form_to_a_signed_link_and_the_same_notice_to_anyone_else(self):
		from campus.www import feedback as page

		campaign = self.campaign(audience="Employers", course_offering=None, invitees=[{"member": self.member}], is_anonymous=0)
		signature = feedback.invite_signature(campaign.name, f"External Member:{self.member}")

		def render(**query):
			frappe.form_dict.clear()
			frappe.form_dict.update({"campaign": campaign.name, **query})
			context = frappe._dict()
			page.get_context(context)
			return context

		frappe.set_user("Guest")
		try:
			shown = render(member=self.member, signature=signature)
			self.assertEqual(shown.state, "open")
			self.assertEqual(len(shown.questions), 4)
			for query in ({"member": self.member, "signature": "bad"}, {"member": "Nobody", "signature": signature}):
				denied = render(**query)
				self.assertEqual(denied.state, "denied")
				self.assertFalse(denied.get("questions"))
			feedback.submit_response(campaign.name, self.answers(campaign), member=self.member, signature=signature)
			self.assertEqual(render(member=self.member, signature=signature).state, "answered")
		finally:
			frappe.form_dict.clear()
			frappe.set_user("Administrator")

	def test_the_portal_lists_open_campaigns_the_student_has_not_answered(self):
		campaign = self.campaign()
		frappe.set_user(fx.STUDENT_USER)
		self.assertIn(campaign.name, [c.name for c in feedback.open_campaigns_for()])
		frappe.set_user("Administrator")
		self.respond(campaign, fx.STUDENT_USER)
		frappe.set_user(fx.STUDENT_USER)
		self.assertNotIn(campaign.name, [c.name for c in feedback.open_campaigns_for()])
		frappe.set_user("Administrator")


class TestInvitations(FeedbackFixture):
	def run_items(self, run):
		"""The bulk run's items, each sent through its handler here (the worker would commit, a test must not)."""
		run = frappe.get_doc("Bulk Run", run)
		items = json.loads(run.items)
		for item in items:
			feedback_invites.send_one(item, run)
		return items

	def logs(self, campaign, event_key):
		return frappe.get_all(
			"Message Log", {"reference_doctype": "Feedback Campaign", "reference_name": campaign.name, "event_key": event_key},
			["recipient", "message"],
		)  # fmt: skip

	def test_invitations_go_to_every_eligible_respondent_through_messaging(self):
		fx.make_template(feedback_invites.INVITATION)
		campaign = self.campaign()
		items = self.run_items(feedback_invites.send_invitations(campaign.name))
		self.assertEqual(set(items), {f"Student:{s}" for s in self.students})
		self.assertEqual({log.recipient for log in self.logs(campaign, feedback_invites.INVITATION)}, set(items))
		self.assertTrue(frappe.db.get_value("Feedback Campaign", campaign.name, "invitations_sent"))

	def test_an_invited_member_gets_a_signed_link(self):
		campaign = self.campaign(audience="Employers", course_offering=None, invitees=[{"member": self.member}], is_anonymous=0)
		link = feedback_invites.link(campaign, f"External Member:{self.member}")
		self.assertIn(feedback.invite_signature(campaign.name, f"External Member:{self.member}"), link)
		self.assertNotIn("signature", feedback_invites.link(campaign, f"Student:{self.world['asha']}"))

	def test_reminders_go_to_everyone_invited_so_the_log_never_shows_who_answered(self):
		campaign = self.campaign(start_date=add_days(today(), -3), reminder_every_days=3)
		self.respond(campaign, fx.STUDENT_USER)
		runs = feedback_invites.send_reminders()
		[run] = [r for r in runs if json.loads(frappe.db.get_value("Bulk Run", r, "parameters"))["campaign"] == campaign.name]
		items = json.loads(frappe.db.get_value("Bulk Run", run, "items"))
		self.assertIn(f"Student:{self.world['asha']}", items)
		self.assertEqual(set(items), {f"Student:{s}" for s in self.students})
		not_due = self.campaign(start_date=add_days(today(), -2), reminder_every_days=3)
		self.assertNotIn(not_due.name, [json.loads(frappe.db.get_value("Bulk Run", r, "parameters"))["campaign"] for r in feedback_invites.send_reminders()])

	def test_only_iqac_staff_send_invitations(self):
		campaign = self.campaign()
		frappe.set_user(fx.STAFF_USER)
		with self.assertRaises(frappe.PermissionError):
			api.send_feedback_invitations(campaign.name)
		frappe.set_user("Administrator")


class TestAnalysis(FeedbackFixture):
	def setUp(self):
		names = frappe.get_all("Feedback Response", {"course_offering": self.offering}, pluck="name")
		frappe.db.delete("Feedback Answer", {"parent": ["in", names or [""]]})
		frappe.db.delete("Feedback Response", {"name": ["in", names or [""]]})

	def answered(self, users, ratings):
		campaign = self.campaign()
		for user, rating in zip(users, ratings):
			self.respond(campaign, user, ratings=rating)
		self.close(campaign)
		return campaign

	def test_indirect_co_scores_average_the_co_tagged_ratings_of_the_offering(self):
		self.answered([fx.STUDENT_USER, *EXTRA_USERS[:3]], [(5, 2), (4, 2), (3, 3), (4, 1)])
		scores = api.indirect_co_scores(self.offering)
		self.assertEqual(scores["CO1"], {"responses": 4, "average": 4.0, "percent": 80.0, "suppressed": 0})
		self.assertEqual(scores["CO2"]["average"], 2.0)

	def test_a_co_with_fewer_responses_than_the_minimum_group_is_suppressed(self):
		self.answered([fx.STUDENT_USER, EXTRA_USERS[0]], [(5, 2), (4, 2)])
		scores = api.indirect_co_scores(self.offering)
		self.assertEqual(scores["CO1"], {"responses": None, "average": None, "percent": None, "suppressed": 1})

	def test_the_report_shows_per_question_course_and_faculty_rows_and_hides_small_groups(self):
		big = self.answered([fx.STUDENT_USER, *EXTRA_USERS[:2]], [(5, 2), (4, 2), (3, 2)])
		small = self.campaign(campaign_title="T07 Small")
		self.respond(small, fx.STUDENT_USER)
		self.close(small)
		frappe.set_user(COORDINATOR)
		_columns, rows, message = feedback_analysis.execute({"company": fx.COLLEGE, "group_by": "Question", "campaign": big.name})[:3]
		co1 = next(r for r in rows if r["course_outcome"] == "CO1")
		self.assertEqual((co1["responses"], co1["average"]), (3, 4.0))
		pace = next(r for r in rows if r["question_type"] == "Choice")
		self.assertIn("Right: 3", pace["distribution"])
		self.assertFalse(any(r["question_type"] == "Text" and r.get("distribution") for r in rows))  # free text is never listed
		_columns, rows, message = feedback_analysis.execute({"company": fx.COLLEGE, "group_by": "Question", "campaign": small.name})[:3]
		self.assertEqual(rows, [])
		self.assertIn("minimum", message)
		_columns, rows, _message = feedback_analysis.execute({"company": fx.COLLEGE, "group_by": "Course", "campaign": big.name})[:3]
		self.assertEqual([(r["course_offering"], r["responses"]) for r in rows], [(self.offering, 3)])
		frappe.set_user("Administrator")

	def test_a_small_campaign_never_shows_by_difference_with_a_big_one(self):
		big = self.answered([fx.STUDENT_USER, *EXTRA_USERS[:2]], [(5, 2), (5, 2), (5, 2)])
		small = self.campaign(campaign_title="T07 One more")
		self.respond(small, EXTRA_USERS[3], ratings=(1, 1))
		self.close(small)
		frappe.set_user(COORDINATOR)
		course = lambda **f: feedback_analysis.execute({"company": fx.COLLEGE, "group_by": "Course", **f})[1]
		[together] = course()
		[alone] = course(campaign=big.name)
		self.assertEqual((together["responses"], together["average"]), (alone["responses"], alone["average"]))
		self.assertEqual(together["responses"], 3)
		frappe.set_user("Administrator")
		self.assertEqual(api.indirect_co_scores(self.offering)["CO1"]["average"], 5.0)  # the single 1 is not mixed in

	def test_an_optional_question_answered_by_too_few_never_shows_by_difference(self):
		form = self.form([
			{"question": "The lectures met CO1", "question_type": "Likert", "course_outcome": "CO1"},
			{"question": "The lab met CO1", "question_type": "Likert", "course_outcome": "CO1", "is_required": 0},
		])  # fmt: skip
		campaign = self.campaign(form=form)
		lectures, lab = (q.name for q in form.questions)
		for user in (fx.STUDENT_USER, *EXTRA_USERS[:2]):
			answers = {lectures: 5, lab: 1} if user == fx.STUDENT_USER else {lectures: 5}  # only one answers the lab question
			frappe.set_user(user)
			try:
				feedback.submit_response(campaign.name, json.dumps(answers))
			finally:
				frappe.set_user("Administrator")
		self.close(campaign)
		frappe.set_user(COORDINATOR)
		report = lambda group_by: feedback_analysis.execute({"company": fx.COLLEGE, "group_by": group_by, "campaign": campaign.name})[1]
		questions, [course], [faculty] = report("Question"), report("Course"), report("Faculty")
		frappe.set_user("Administrator")
		self.assertEqual([(r["question"], r["average"]) for r in questions], [("The lectures met CO1", 5.0)])
		self.assertEqual((course["responses"], course["average"]), (3, 5.0))  # the lone lab rating is not pooled in
		self.assertEqual(faculty["average"], 5.0)
		self.assertEqual(api.indirect_co_scores(self.offering)["CO1"], {"responses": 3, "average": 5.0, "percent": 100.0, "suppressed": 0})

	def test_only_closed_campaigns_are_analysed(self):
		campaign = self.campaign()
		for user in (fx.STUDENT_USER, *EXTRA_USERS[:2]):
			self.respond(campaign, user)
		self.assertNotIn("CO1", api.indirect_co_scores(self.offering))
		frappe.set_user(COORDINATOR)
		self.assertEqual(feedback_analysis.execute({"company": fx.COLLEGE, "group_by": "Question", "campaign": campaign.name})[1], [])
		frappe.set_user("Administrator")
		self.close(campaign)
		self.assertEqual(api.indirect_co_scores(self.offering)["CO1"]["responses"], 3)

	def test_a_closed_campaign_with_responses_cannot_reopen(self):
		campaign = self.answered([fx.STUDENT_USER], [(4, 4)])
		campaign.reload()
		campaign.end_date = add_days(today(), 3)
		with self.assertRaisesRegex(frappe.ValidationError, "reopen"):
			campaign.save()

	def test_co_scores_count_only_the_offerings_student_campaigns(self):
		parents = self.campaign(audience="Parents", is_anonymous=0)
		self.respond(parents, fx.GUARDIAN_USER, ratings=(1, 1))
		self.close(parents)
		self.answered([fx.STUDENT_USER, *EXTRA_USERS[:2]], [(5, 5), (5, 5), (5, 5)])
		self.assertEqual(api.indirect_co_scores(self.offering)["CO1"], {"responses": 3, "average": 5.0, "percent": 100.0, "suppressed": 0})
		rule = fx.make_rule(f"T07 Rule {frappe.generate_hash(length=4)}", "Student Group", student_group=self.offering)
		self.assertFalse(self.campaign(audience_rule=rule.name).audience_rule)  # offering students come from the offering

	def test_an_unknown_grouping_is_refused(self):
		with self.assertRaises(frappe.ValidationError):
			feedback_analysis.execute({"company": fx.COLLEGE, "group_by": "Student"})

	def test_the_report_is_for_iqac_staff_and_their_college(self):
		frappe.set_user(fx.OTHER_COLLEGE_USER)
		with self.assertRaises(frappe.PermissionError):
			feedback_analysis.execute({"company": fx.COLLEGE, "group_by": "Question"})
		frappe.set_user("Administrator")
