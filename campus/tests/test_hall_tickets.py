"""M06-02 integration tests: bulk Hall Ticket generation, the withheld list, QR verification, portal ownership."""

from unittest.mock import patch

import frappe
from frappe.utils import add_days, now_datetime, today

from campus.campus_finance.api import raise_fee_invoice
from campus.campus_platform import bulk, verification
from campus.examinations import api, exam_fee, hall_tickets, registration
from campus.student_services import portal
from campus.tests.test_attendance_condonation import inline_bulk
from campus.tests.test_exam_registration import (
	COE_USER,
	DROP_USER,
	FACULTY,
	GUARDIAN_USER,
	OK_USER,
	SA_USER,
	ExamRegistrationFixture,
)  # fmt: skip
from campus.tests.test_verification import request_as

VD = "Verifiable Document"


class HallTicketFixture(ExamRegistrationFixture):
	def setUp(self):
		super().setUp()
		session = frappe.get_doc("Exam Session", self.session.name)
		session.append("schedule", {"course": "EXR D", "exam_date": add_days(today(), 24), "slot": "FN"})
		session.append("schedule", {"course": "EXR OLD", "exam_date": add_days(today(), 26), "slot": "AN"})
		session.save()
		frappe.db.set_value("Student", self.ok, "image", "/files/exr-ok.png")
		raise_fee_invoice(self.dues, "Exam", 500, "Exam Reg Test College", "Previous session exam fee")
		self.register_all()
		for student in (self.ok, self.sa, self.dues):
			with patch("frappe.enqueue"):
				registration.confirm(self.reg(student).name)
		exam_fee.raise_fee(self.reg(self.dues).name)  # the worker the confirmation queued
		# drop and old stay unconfirmed

	def generate(self):
		with patch.object(hall_tickets, "run_bulk", inline_bulk):
			hall_tickets.start(self.session.name)

	def ticket(self, student):
		return frappe.get_doc("Hall Ticket", hall_tickets.ticket_name(self.session.name, student))

	def documents(self, student, status=None):
		filters = {
			"reference_doctype": "Hall Ticket",
			"reference_name": hall_tickets.ticket_name(self.session.name, student),
		}
		if status:
			filters["status"] = status
		return frappe.get_all(VD, filters, ["code", "status", "revocation_reason"], order_by="creation asc")

	def release(self, days=-1):
		frappe.db.set_value(
			"Exam Session", self.session.name, "hall_ticket_release_on", add_days(now_datetime(), days)
		)


class TestBulkGeneration(HallTicketFixture):
	def test_start_queues_every_registration_of_the_session_and_needs_the_coe(self):
		frappe.set_user(FACULTY)
		with self.assertRaises(frappe.PermissionError):
			api.generate_hall_tickets(self.session.name)
		frappe.set_user(COE_USER)
		with patch.object(bulk, "enqueue_run") as enqueue:
			name = api.generate_hall_tickets(self.session.name)
		enqueue.assert_called_once_with(name)
		run = frappe.get_doc("Bulk Run", name)
		names = sorted(frappe.get_all("Exam Registration", {"exam_session": self.session.name}, pluck="name"))
		self.assertEqual((frappe.parse_json(run.items), run.handler), (names, hall_tickets.HANDLER))
		self.assertEqual(
			frappe.allowed_http_methods_for_whitelisted_func[api.generate_hall_tickets], ["POST"]
		)
		frappe.db.set_value("Bulk Run", name, "status", "Completed")  # the queued run has finished
		self.generate()  # the handler runs as the COE who queued it
		self.assertEqual((self.ticket(self.ok).status, len(self.documents(self.ok, "Valid"))), ("Issued", 1))

	def test_a_ticket_carries_photo_courses_dates_and_a_valid_qr(self):
		self.generate()
		ticket = self.ticket(self.ok)
		self.assertEqual(
			(ticket.status, ticket.photo, ticket.withheld_reasons), ("Issued", "/files/exr-ok.png", None)
		)
		self.assertEqual(
			[(r.course, str(r.exam_date), r.slot) for r in ticket.courses],
			[("EXR A", add_days(today(), 20), "FN"), ("EXR B", add_days(today(), 22), "AN")],
		)
		(document,) = self.documents(self.ok)
		self.assertEqual(document.status, "Valid")
		html = frappe.get_print("Hall Ticket", ticket.name, hall_tickets.PRINT_FORMAT)
		for text in (
			"/files/exr-ok.png",
			"EXR A",
			"EXR B",
			document.code,
			frappe.utils.formatdate(add_days(today(), 20)),
		):
			self.assertIn(text, html)

	def test_the_sa_student_sits_the_eligible_arrear_only(self):
		self.generate()
		ticket = self.ticket(self.sa)
		self.assertEqual((ticket.status, [r.course for r in ticket.courses]), ("Issued", ["EXR OLD"]))

	def test_an_eligible_course_without_an_exam_date_fails_the_item(self):
		frappe.db.delete("Exam Schedule", {"parent": self.session.name, "course": "EXR B"})
		with self.assertRaisesRegex(frappe.ValidationError, "EXR B"):
			hall_tickets.generate(self.reg(self.ok).name)

	def test_a_published_session_is_refused(self):
		frappe.db.set_value(
			"Exam Session", self.session.name, {"status": "Published", "publish_at": now_datetime()}
		)
		with self.assertRaises(frappe.ValidationError):
			hall_tickets.start(self.session.name)

	def test_the_print_escapes_values(self):
		self.generate()
		ticket = self.ticket(self.ok)
		ticket.db_set("student_name", "<script>alert(1)</script>")
		html = frappe.get_print("Hall Ticket", ticket.name, hall_tickets.PRINT_FORMAT)
		self.assertNotIn("<script>alert(1)</script>", html)
		self.assertIn("&lt;script&gt;", html)


class TestWithheld(HallTicketFixture):
	def test_withheld_students_get_no_ticket_and_the_reason(self):
		self.generate()
		expected = {
			self.drop: "Exam registration not confirmed",
			self.old: "Exam registration not confirmed",
			self.dues: "Unpaid exam dues of 1,100.00",  # 500 earlier + 2 x 300 this session
		}
		for student, reason in expected.items():
			ticket = self.ticket(student)
			self.assertEqual((ticket.status, ticket.withheld_reasons), ("Withheld", reason), student)
			self.assertEqual(ticket.courses, [])
			self.assertEqual(self.documents(student), [], student)  # no QR, nothing to verify
		withheld = frappe.get_all(
			"Hall Ticket", {"exam_session": self.session.name, "status": "Withheld"}, pluck="student"
		)
		self.assertEqual(sorted(withheld), sorted(expected))

	def test_detention_after_issue_revokes_the_ticket(self):
		self.generate()
		doc = self.reg(self.ok)
		doc.detained, doc.detention_reason = 1, "Disciplinary committee order"
		doc.save()
		self.generate()
		ticket = self.ticket(self.ok)
		self.assertEqual(
			(ticket.status, ticket.withheld_reasons), ("Withheld", "Detained: Disciplinary committee order")
		)
		(document,) = self.documents(self.ok)
		self.assertEqual(document.status, "Revoked")
		self.assertEqual(
			document.revocation_reason, "Hall ticket withdrawn"
		)  # the public record never says why

	def test_a_photo_outside_the_files_folders_is_dropped(self):
		frappe.db.set_value("Student", self.ok, "image", "https://evil.example/x.png")
		frappe.db.set_value("Student", self.sa, "image", "/private/files/exr-sa.png")
		self.generate()
		self.assertIsNone(self.ticket(self.ok).photo)
		self.assertEqual(self.ticket(self.sa).photo, "/private/files/exr-sa.png")


class TestLocking(HallTicketFixture):
	def test_a_stale_snapshot_refreshes_the_existing_ticket_and_keeps_its_qr(self):
		self.generate()
		before = self.documents(self.ok)
		real_exists = frappe.db.exists

		def stale_exists(
			doctype, *args, **kwargs
		):  # what a plain read of a snapshot older than the rows sees
			return None if doctype in ("Hall Ticket", VD) else real_exists(doctype, *args, **kwargs)

		with patch.object(frappe.db, "exists", side_effect=stale_exists):
			hall_tickets.generate(self.reg(self.ok).name)
		self.assertEqual(self.documents(self.ok), before)

	def test_start_refuses_while_a_hall_ticket_run_of_the_session_is_live(self):
		frappe.set_user(COE_USER)
		with patch.object(bulk, "enqueue_run"):
			first = api.generate_hall_tickets(self.session.name)
			with self.assertRaisesRegex(frappe.ValidationError, first):
				api.generate_hall_tickets(self.session.name)
			frappe.db.set_value("Bulk Run", first, "status", "Completed with errors")
			self.assertNotEqual(api.generate_hall_tickets(self.session.name), first)


class TestRegeneration(HallTicketFixture):
	def test_a_rerun_without_change_keeps_the_ticket_and_its_code(self):
		self.generate()
		before = (self.ticket(self.ok).modified, self.documents(self.ok))
		self.generate()
		self.assertEqual((self.ticket(self.ok).modified, self.documents(self.ok)), before)

	def test_a_changed_date_revokes_and_reissues(self):
		self.generate()
		(old,) = self.documents(self.ok)
		frappe.db.set_value(
			"Exam Schedule",
			{"parent": self.session.name, "course": "EXR B"},
			"exam_date",
			add_days(today(), 23),
		)
		self.generate()
		first, second = self.documents(self.ok)
		self.assertEqual((first.code, first.status), (old.code, "Revoked"))
		self.assertEqual(second.status, "Valid")
		self.assertEqual(str(self.ticket(self.ok).courses[1].exam_date), add_days(today(), 23))

	def test_paying_the_dues_releases_the_ticket(self):
		from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry

		self.generate()
		self.assertEqual(self.ticket(self.dues).status, "Withheld")
		for invoice in frappe.get_all(
			"Sales Invoice",
			{"student": self.dues, "docstatus": 1, "outstanding_amount": [">", 0]},
			pluck="name",
		):
			payment = get_payment_entry("Sales Invoice", invoice)
			payment.reference_no, payment.reference_date = f"HT-{invoice}", today()
			payment.insert().submit()
		self.generate()
		self.assertEqual(self.ticket(self.dues).status, "Issued")
		self.assertEqual([d.status for d in self.documents(self.dues)], ["Valid"])


class TestVerification(HallTicketFixture):
	def test_the_qr_verifies_with_the_exam_session_and_registration_status(self):
		from frappe.website.serve import get_response

		from campus.www import verify

		self.generate()
		(document,) = self.documents(self.ok)
		found = verification.public_lookup(document.code)
		self.assertEqual(found.status, "Valid")
		details = dict(found.details)
		self.assertEqual(details["Exam session"], self.session.name)
		self.assertEqual(details["Registration"], "Confirmed")
		self.assertEqual(details["Hall ticket"], "Issued")
		self.assertNotIn(self.ok, str(found))  # the public page never shows the student id
		frappe.form_dict.code = document.code
		context = frappe._dict()
		verify.get_context(context)
		self.assertEqual(dict(context.record.details)["Exam session"], self.session.name)
		frappe.set_user("Guest")
		request_as("10.0.0.22", f"/verify/{document.code}")
		frappe.local.form_dict = frappe._dict()
		frappe.local.response = frappe._dict()
		body = get_response(f"verify/{document.code}").get_data(as_text=True)
		self.assertIn(self.session.name, body)
		self.assertIn("Confirmed", body)

	def guest_page(self, ip, code):
		from frappe.website.serve import get_response

		frappe.set_user("Guest")
		request_as(ip, f"/verify/{code}")
		frappe.local.form_dict = frappe._dict()
		frappe.local.response = frappe._dict()
		body = get_response(f"verify/{code}").get_data(as_text=True)
		frappe.set_user("Administrator")
		return body

	def test_a_withheld_ticket_shows_the_public_no_reason_and_revocation_text_is_escaped(self):
		self.generate()
		doc = self.reg(self.ok)
		doc.detained, doc.detention_reason = 1, "Disciplinary committee order"
		doc.save()
		self.generate()
		(document,) = self.documents(self.ok)
		found = verification.public_lookup(document.code)
		self.assertEqual((found.status, found.details), ("Revoked", []))
		body = self.guest_page("10.0.0.31", document.code)
		self.assertIn("Hall ticket withdrawn", body)
		for secret in ("Disciplinary", "Detained", self.session.name):
			self.assertNotIn(secret, body)
		(issued,) = self.documents(self.sa)
		verification.revoke(issued.code, "Staff note")
		# a save sanitises the reason; plant the raw text as legacy or imported rows may hold it
		frappe.db.set_value(VD, {"code": issued.code}, "revocation_reason", "<script>alert(9)</script>")
		body = self.guest_page("10.0.0.32", issued.code)
		self.assertNotIn("<script>alert(9)</script>", body)
		self.assertIn("&lt;script&gt;alert(9)&lt;/script&gt;", body)

	def test_a_document_without_details_shows_none(self):
		self.assertEqual(
			verification.reference_details(
				frappe._dict(reference_doctype="Exam Session", reference_name=self.session.name)
			),
			[],
		)


class TestPortal(HallTicketFixture):
	def hall_ticket_row(self, user, student):
		frappe.set_user(user)
		frappe.form_dict.clear()
		(row,) = portal.my_exam_registrations(student)["registrations"]
		return row["hall_ticket"]

	def test_the_student_downloads_only_after_the_release_date(self):
		self.generate()
		row = self.hall_ticket_row(OK_USER, self.ok)
		self.assertEqual((row["status"], row["download_url"]), ("Issued", None))
		frappe.set_user("Administrator")
		self.release()
		row = self.hall_ticket_row(OK_USER, self.ok)
		self.assertIn("format=Hall%20Ticket", row["download_url"])
		self.assertTrue(frappe.has_permission("Hall Ticket", "print", self.ticket(self.ok), user=OK_USER))

	def test_a_withheld_student_sees_the_reason_and_cannot_download(self):
		self.generate()
		self.release()
		for user in (DROP_USER, GUARDIAN_USER):
			row = self.hall_ticket_row(user, self.drop)
			self.assertEqual(
				(row["status"], row["reasons"], row["download_url"]),
				("Withheld", "Exam registration not confirmed", None),
			)
		frappe.set_user("Administrator")
		self.assertFalse(
			frappe.has_permission("Hall Ticket", "print", self.ticket(self.drop), user=DROP_USER)
		)

	def test_ownership_is_checked(self):
		self.generate()
		self.release()
		ticket = self.ticket(self.ok)
		self.assertFalse(frappe.has_permission("Hall Ticket", "print", ticket, user=SA_USER))
		self.assertFalse(
			frappe.has_permission("Hall Ticket", "print", ticket, user=GUARDIAN_USER)
		)  # guards drop only
		self.assertTrue(frappe.has_permission("Hall Ticket", "print", ticket, user=COE_USER))
		frappe.set_user(SA_USER)
		self.assertEqual(frappe.get_list("Hall Ticket", pluck="student"), [self.sa])
		with self.assertRaises(frappe.PermissionError):
			portal.my_exam_registrations(self.ok)

	def test_the_dashboard_shows_the_hall_ticket(self):
		from frappe.website.serve import get_response

		self.generate()
		self.release()
		frappe.set_user(OK_USER)
		request_as("10.0.0.21", "/campus-portal")
		frappe.local.form_dict = frappe._dict()
		frappe.local.response = frappe._dict()
		body = get_response("campus-portal").get_data(as_text=True)
		self.assertIn("format=Hall%20Ticket", body)
		frappe.set_user(DROP_USER)
		frappe.local.response = frappe._dict()
		body = get_response("campus-portal").get_data(as_text=True)
		self.assertIn("Exam registration not confirmed", body)
		self.assertNotIn("format=Hall%20Ticket", body)


class TestManualHallTicketCreation(HallTicketFixture):
	def test_get_ticket_preview(self):
		preview = hall_tickets.get_ticket_preview(self.reg(self.ok).name)
		self.assertEqual(preview["student"], self.ok)
		self.assertEqual(preview["exam_session"], self.session.name)
		self.assertEqual(preview["status"], "Issued")
		self.assertTrue(len(preview["courses"]) > 0)

	def test_manual_creation_auto_populates_and_issues_qr(self):
		reg_name = self.reg(self.ok).name
		ticket = frappe.new_doc("Hall Ticket")
		ticket.exam_registration = reg_name
		ticket.insert()

		self.assertEqual(ticket.student, self.ok)
		self.assertEqual(ticket.exam_session, self.session.name)
		self.assertEqual(ticket.status, "Issued")
		self.assertEqual(len(ticket.courses), 2)

		# Verifiable document (QR) was issued
		docs = self.documents(self.ok, "Valid")
		self.assertEqual(len(docs), 1)
