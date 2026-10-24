"""P1-T25 tests (M09-06): gateway routing by company, the ownership-checked Pay-now link, and the signed, idempotent payment webhook.

The gateway is mocked: the `payments` app that supplies Razorpay's controller is not installed here, so the Payment Request
gets its URL from a patched controller call and webhook bodies are signed in the test with a configured secret."""

import hashlib
import hmac
import json
from types import SimpleNamespace
from unittest.mock import patch

import frappe
from erpnext.accounts.doctype.payment_request.payment_request import PaymentRequest
from frappe.utils import flt

from campus.campus_finance import api as finance
from campus.campus_finance import gateways
from campus.student_services import portal
from campus.tests.broadcast_fixtures import make_guardian
from campus.tests.factories import make_user
from campus.tests.finance_factories import make_enrolled_students, make_finance_company, make_year_and_program
from campus.tests.utils import CampusTestCase

COLLEGE, TRUST = "Gateway Test College", "Gateway Test Trust"
YEAR, PROGRAM = "Gateway Test 2026", "Gateway Test BE"
STUDENT_USER, GUARDIAN_USER, STRANGER = "gw-student@example.com", "gw-guardian@example.com", "gw-stranger@example.com"
ACCOUNTS_USER = "gw-accounts@example.com"
URL = "https://pay.example.test/checkout/"
SECRETS = {}


def make_gateway_account(company, bank, gateway="Razorpay"):
	"""A default Payment Gateway Account. The Payment Gateway DocType belongs to the `payments` app, so its link is not checked."""
	name = frappe.db.get_value("Payment Gateway Account", {"company": company, "payment_gateway": gateway})
	if name:
		return name
	doc = frappe.get_doc(
		{"doctype": "Payment Gateway Account", "payment_gateway": gateway, "payment_account": bank, "company": company, "currency": "INR", "is_default": 1}
	)
	doc.flags.ignore_links = True
	return doc.insert().name


def signed(body, secret):
	return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def captured(payment_request, amount, payment_id="pay_1", currency="INR"):
	entity = {"id": payment_id, "amount": int(amount * 100), "notes": {"payment_request": payment_request}}
	if currency:
		entity["currency"] = currency
	return json.dumps({"event": "payment.captured", "payload": {"payment": {"entity": entity}}}).encode()


def make_pure_guardian(user):
	"""A login holding the Guardian role alone, as a real guardian's does (no Student role, no desk role)."""
	doc = frappe.get_doc("User", user)
	doc.remove_roles(*[row.role for row in doc.roles if row.role != "Guardian"])
	doc.add_roles("Guardian")


class GatewayFixture(CampusTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.college = make_finance_company(COLLEGE, "GWC")
		cls.trust = make_finance_company(TRUST, "GWT")
		if not frappe.db.exists("Party Type", "Customer"):
			frappe.get_doc({"doctype": "Party Type", "party_type": "Customer", "account_type": "Receivable"}).insert()
		make_year_and_program(COLLEGE, PROGRAM, YEAR)
		_group, students = make_enrolled_students(COLLEGE, PROGRAM, YEAR, 2, "GwStu")
		cls.a, cls.b = students
		for user in (STUDENT_USER, GUARDIAN_USER, STRANGER, ACCOUNTS_USER):
			make_user(user, [COLLEGE, TRUST])
		for user in (STUDENT_USER, STRANGER):
			frappe.get_doc("User", user).add_roles("Student")
		make_pure_guardian(GUARDIAN_USER)
		frappe.get_doc("User", ACCOUNTS_USER).add_roles("Accounts User", "Guardian")
		frappe.db.set_value("Student", cls.a, "user", STUDENT_USER)
		make_guardian("Gwguardian", [cls.b], user=GUARDIAN_USER)
		cls.college_account = make_gateway_account(COLLEGE, cls.college["bank"])
		cls.trust_account = make_gateway_account(TRUST, cls.trust["bank"])
		SECRETS.update({cls.college_account: "college-secret", cls.trust_account: "trust-secret"})

	def setUp(self):
		frappe.db.savepoint("gateway_test")
		self.addCleanup(frappe.db.rollback, save_point="gateway_test")
		self.addCleanup(frappe.set_user, "Administrator")
		for patcher in (
			patch.object(PaymentRequest, "payment_gateway_validation", return_value=True),
			patch.object(PaymentRequest, "get_payment_url", lambda request: URL + request.name),
		):
			patcher.start()
			self.addCleanup(patcher.stop)

	def invoice(self, student, company, amount=5000, head="Tuition"):
		return finance.raise_fee_invoice(student, head, amount, company, "Gateway test")

	def link(self, invoice, user=STUDENT_USER):
		frappe.set_user(user)
		try:
			return finance.fee_payment_link(invoice.name)
		finally:
			frappe.set_user("Administrator")

	def request_of(self, invoice):
		return frappe.get_all(
			"Payment Request", {"reference_name": invoice.name, "docstatus": 1}, ["name", "payment_gateway_account", "payment_account", "company", "grand_total", "status"]
		)


class TestPayNow(GatewayFixture):
	def test_a_college_invoice_is_paid_through_the_colleges_gateway_account(self):
		invoice = self.invoice(self.a, COLLEGE)
		url = self.link(invoice)
		(request,) = self.request_of(invoice)
		self.assertEqual(url, URL + request.name)
		self.assertEqual((request.payment_gateway_account, request.payment_account, request.company, flt(request.grand_total)), (self.college_account, self.college["bank"], COLLEGE, 5000))

	def test_a_trust_invoice_is_paid_through_the_trusts_gateway_account(self):
		invoice = self.invoice(self.a, TRUST, 3000, "Hostel")
		self.link(invoice)
		(request,) = self.request_of(invoice)
		self.assertEqual((request.payment_gateway_account, request.payment_account, request.company), (self.trust_account, self.trust["bank"], TRUST))

	def test_a_guardian_pays_a_wards_invoice(self):
		invoice = self.invoice(self.b, COLLEGE)
		self.assertTrue(self.link(invoice, GUARDIAN_USER).startswith(URL))

	def test_nobody_else_can_get_a_link_and_all_refusals_look_alike(self):
		invoice = self.invoice(self.b, COLLEGE)
		for user in (STUDENT_USER, STRANGER, "Guest"):  # STUDENT_USER is student a, not b
			with self.assertRaisesRegex(frappe.PermissionError, "cannot pay this invoice"):
				self.link(invoice, user)
		frappe.set_user(STRANGER)
		with self.assertRaisesRegex(frappe.PermissionError, "cannot pay this invoice"):
			finance.fee_payment_link("NO-SUCH-INVOICE")
		self.assertFalse(self.request_of(invoice))

	def test_a_paid_invoice_and_a_credit_note_have_no_link(self):
		invoice = self.invoice(self.a, COLLEGE)
		frappe.db.set_value("Sales Invoice", invoice.name, "outstanding_amount", 0)
		with self.assertRaisesRegex(frappe.ValidationError, "Nothing is due"):
			self.link(invoice)
		note = finance.make_credit_note(frappe.get_doc("Sales Invoice", self.invoice(self.a, COLLEGE, 1000).name), 500)
		with self.assertRaises(frappe.PermissionError):
			self.link(note)

	def test_a_company_without_a_gateway_gets_no_link_and_never_another_companys(self):
		frappe.db.set_value("Payment Gateway Account", self.trust_account, "company", COLLEGE)  # the trust now has none
		frappe.db.set_value("Payment Gateway Account", self.trust_account, "is_default", 0)
		invoice = self.invoice(self.a, TRUST, 3000, "Hostel")
		with self.assertRaisesRegex(frappe.ValidationError, "no payment gateway"):
			self.link(invoice)

	def test_a_phone_channel_account_gives_no_link(self):
		frappe.db.set_value("Payment Gateway Account", self.college_account, "payment_channel", "Phone")
		with self.assertRaisesRegex(frappe.ValidationError, "no payment gateway"):
			self.link(self.invoice(self.a, COLLEGE))
		self.assertFalse(finance.can_pay_online(COLLEGE))

	def test_a_gateway_without_an_adapter_is_refused(self):
		frappe.db.set_value("Payment Gateway Account", self.college_account, "payment_gateway", "PayU")
		with self.assertRaisesRegex(frappe.ValidationError, "PayU is not supported"):
			self.link(self.invoice(self.a, COLLEGE))

	def test_a_second_click_reuses_the_open_request(self):
		invoice = self.invoice(self.a, COLLEGE)
		self.assertEqual(self.link(invoice), self.link(invoice))
		self.assertEqual(len(self.request_of(invoice)), 1)

	def test_a_stale_request_is_cancelled_when_the_amount_due_changes(self):
		invoice = self.invoice(self.a, COLLEGE)
		self.link(invoice)
		frappe.db.set_value("Sales Invoice", invoice.name, "outstanding_amount", 2000)
		self.link(invoice)
		(request,) = self.request_of(invoice)
		self.assertEqual(flt(request.grand_total), 2000)

	def test_no_url_means_no_link(self):
		with patch.object(PaymentRequest, "payment_gateway_validation", return_value=False):
			with self.assertRaisesRegex(frappe.ValidationError, "not available"):
				self.link(self.invoice(self.a, COLLEGE))


class TestGuardianInvoiceAccess(GatewayFixture):
	"""ERPNext's Payment Request reads its Sales Invoice as the payer (ERPNext 58de4a5c36): the Guardian role reads
	Sales Invoice, narrowed by the portal scope to the invoices of the guardian's wards."""

	def as_user(self, user, call, *args, **kwargs):
		frappe.set_user(user)
		try:
			return call(*args, **kwargs)
		finally:
			frappe.set_user("Administrator")

	def test_a_guardian_pays_a_wards_invoice_through_the_portal(self):
		invoice = self.invoice(self.b, COLLEGE)
		result = self.as_user(GUARDIAN_USER, portal.pay_invoice, invoice.name, self.b)
		(request,) = self.request_of(invoice)
		self.assertEqual(result["url"], URL + request.name)

	def test_a_guardian_reads_and_lists_only_the_wards_invoices(self):
		ward, other = self.invoice(self.b, COLLEGE), self.invoice(self.a, COLLEGE)
		both = {"name": ["in", [ward.name, other.name]]}
		listed = self.as_user(GUARDIAN_USER, frappe.get_list, "Sales Invoice", filters=both, pluck="name")
		self.assertEqual(listed, [ward.name])
		self.assertEqual(self.as_user(GUARDIAN_USER, frappe.client.get_count, "Sales Invoice", both), 1)
		self.assertTrue(frappe.has_permission("Sales Invoice", "read", doc=ward.name, user=GUARDIAN_USER))
		self.assertFalse(frappe.has_permission("Sales Invoice", "read", doc=other.name, user=GUARDIAN_USER))
		with self.assertRaises(frappe.PermissionError):
			self.as_user(GUARDIAN_USER, frappe.client.get, "Sales Invoice", other.name)

	def test_a_guardian_has_read_alone_on_sales_invoice(self):
		ward = self.invoice(self.b, COLLEGE)
		for ptype in ("write", "create", "submit", "cancel", "delete", "report", "export", "import", "share", "print", "email"):
			with self.subTest(ptype=ptype):
				self.assertFalse(frappe.has_permission("Sales Invoice", ptype, user=GUARDIAN_USER))
				self.assertFalse(frappe.has_permission("Sales Invoice", ptype, doc=ward.name, user=GUARDIAN_USER))

	def test_a_desk_users_invoice_access_is_unchanged(self):
		ward, other = self.invoice(self.b, COLLEGE), self.invoice(self.a, COLLEGE)
		both = {"name": ["in", [ward.name, other.name]]}
		listed = self.as_user(ACCOUNTS_USER, frappe.get_list, "Sales Invoice", filters=both, pluck="name")
		self.assertEqual(set(listed), {ward.name, other.name})  # Accounts User also holding Guardian is not narrowed
		self.assertEqual(self.as_user(ACCOUNTS_USER, frappe.client.get_count, "Sales Invoice", both), 2)
		for ptype in ("read", "write", "report", "print"):
			with self.subTest(ptype=ptype):
				self.assertTrue(frappe.has_permission("Sales Invoice", ptype, doc=other.name, user=ACCOUNTS_USER))


class TestWebhook(GatewayFixture):
	def setUp(self):
		super().setUp()
		patcher = patch.dict(frappe.conf, {"campus_gateway_secrets": SECRETS})
		patcher.start()
		self.addCleanup(patcher.stop)
		self.logs = []  # Error Log rows are not written: the log is captured
		logger = patch.object(frappe, "log_error", lambda **kwargs: self.logs.append(kwargs))
		logger.start()
		self.addCleanup(logger.stop)

	def pay_request(self, company=COLLEGE, student=None, amount=5000):
		invoice = self.invoice(student or self.a, company, amount)
		self.link(invoice, STUDENT_USER)
		return invoice, self.request_of(invoice)[0]

	def deliver(self, account, body, secret=None, user="Guest"):
		"""Post `body` to the webhook as the gateway would: no session, the signature header set."""
		secret = SECRETS[account] if secret is None else secret
		headers = {"X-Razorpay-Signature": signed(body, secret)}
		frappe.set_user(user)
		with patch.object(frappe, "request", SimpleNamespace(get_data=lambda: body)), patch.object(frappe, "get_request_header", headers.get):
			try:
				return gateways.webhook(account)
			finally:
				frappe.set_user("Administrator")

	def payment_entries(self, invoice):
		return frappe.get_all(
			"Payment Entry Reference", {"reference_name": invoice.name, "docstatus": 1}, ["parent", "allocated_amount"]
		)

	def test_a_captured_payment_marks_the_invoice_paid(self):
		invoice, request = self.pay_request()
		self.assertEqual(self.deliver(request.payment_gateway_account, captured(request.name, 5000)), {"status": "accepted"})
		self.assertEqual(flt(frappe.db.get_value("Sales Invoice", invoice.name, "outstanding_amount")), 0)
		self.assertEqual(frappe.db.get_value("Payment Request", request.name, "status"), "Paid")
		(entry,) = self.payment_entries(invoice)
		payment = frappe.get_doc("Payment Entry", entry.parent)
		self.assertEqual((payment.docstatus, payment.company, payment.payment_type, payment.paid_to, flt(payment.paid_amount)), (1, COLLEGE, "Receive", self.college["bank"], 5000))
		self.assertEqual((payment.oc_student, payment.reference_no), (self.a, request.name))
		self.assertTrue(payment.oc_receipt_no)

	def test_a_duplicate_delivery_does_not_make_a_second_payment_entry(self):
		invoice, request = self.pay_request()
		body = captured(request.name, 5000)
		self.deliver(request.payment_gateway_account, body)
		self.deliver(request.payment_gateway_account, body)  # the gateway retries the same delivery
		self.deliver(request.payment_gateway_account, captured(request.name, 5000, "pay_retry"))
		self.assertEqual(gateways.settle_payment(request.payment_gateway_account, request.name, 5000, "INR", "pay_1"), {"status": "duplicate"})
		self.assertEqual(len(self.payment_entries(invoice)), 1)
		self.assertEqual(flt(frappe.db.get_value("Sales Invoice", invoice.name, "outstanding_amount")), 0)

	def test_a_bad_or_missing_signature_changes_nothing(self):
		invoice, request = self.pay_request()
		account = request.payment_gateway_account
		with self.assertRaises(frappe.PermissionError):
			self.deliver(account, captured(request.name, 5000), secret="guessed")
		with self.assertRaises(frappe.PermissionError):
			self.deliver(account, captured(request.name, 5000), secret=SECRETS[self.trust_account])  # the other company's secret
		with patch.dict(frappe.conf, {"campus_gateway_secrets": {}}), self.assertRaises(frappe.PermissionError):
			self.deliver(account, captured(request.name, 5000))  # no secret configured: fail closed
		with self.assertRaises(frappe.PermissionError):
			self.deliver("No Such Account", captured(request.name, 5000), secret="x")
		self.assertFalse(self.payment_entries(invoice))
		self.assertEqual(flt(frappe.db.get_value("Sales Invoice", invoice.name, "outstanding_amount")), 5000)

	def test_one_companys_account_cannot_settle_anothers_request(self):
		invoice, request = self.pay_request()
		self.deliver(self.trust_account, captured(request.name, 5000))  # signed with the trust's own secret
		self.assertFalse(self.payment_entries(invoice))
		self.assertEqual(gateways.settle_payment(self.trust_account, request.name, 5000), {"status": "ignored"})

	def test_a_wrong_amount_is_refused(self):
		invoice, request = self.pay_request()
		self.deliver(request.payment_gateway_account, captured(request.name, 4000))
		self.assertFalse(self.payment_entries(invoice))
		self.assertIn("paid 4000", self.not_settled_logs()[0].error)

	def test_other_events_and_unknown_requests_are_ignored(self):
		invoice, request = self.pay_request()
		account = request.payment_gateway_account
		failed = json.dumps({"event": "payment.failed", "payload": {}}).encode()
		self.assertEqual(self.deliver(account, failed), {"status": "ignored"})
		self.deliver(account, captured("ACC-PRQ-NOPE", 5000))
		self.assertEqual(gateways.settle_payment(account, "ACC-PRQ-NOPE", 5000), {"status": "ignored"})
		self.assertFalse(self.payment_entries(invoice))

	def test_a_malformed_body_is_refused_after_the_signature_check(self):
		with self.assertRaisesRegex(frappe.ValidationError, "Invalid payment notification"):
			self.deliver(self.college_account, b"not json")

	def test_the_settlement_job_runs_as_administrator(self):
		invoice, request = self.pay_request()
		users = []
		real = gateways.settle
		with patch.object(gateways, "settle", lambda *args: users.append(frappe.session.user) or real(*args)):
			self.deliver(request.payment_gateway_account, captured(request.name, 5000))
		self.assertEqual(users, ["Administrator"])

	def test_the_webhook_queues_the_settlement_after_commit(self):
		invoice, request = self.pay_request()
		with patch.object(frappe, "enqueue") as enqueue:
			self.deliver(request.payment_gateway_account, captured(request.name, 5000))
		enqueue.assert_called_once()
		self.assertEqual((enqueue.call_args.args[0], enqueue.call_args.kwargs["enqueue_after_commit"]), (gateways.JOB, True))
		self.assertFalse(self.payment_entries(invoice))

	def not_settled_logs(self):
		return [SimpleNamespace(error=log["message"]) for log in self.logs if log["title"] == "Payment not settled"]

	def test_a_payment_in_another_currency_is_not_settled(self):
		invoice, request = self.pay_request()
		for currency in ("USD", None):  # a missing currency is refused too
			self.deliver(request.payment_gateway_account, captured(request.name, 5000, currency=currency))
		self.assertFalse(self.payment_entries(invoice))
		self.assertEqual(len(self.not_settled_logs()), 2)  # logged, not raised: a raise would roll the log back

	def test_a_capture_for_a_cancelled_or_unknown_request_is_logged_not_lost(self):
		invoice, request = self.pay_request()
		frappe.get_doc("Payment Request", request.name).cancel()
		self.deliver(request.payment_gateway_account, captured(request.name, 5000, "pay_cancelled"))
		self.deliver(request.payment_gateway_account, captured("ACC-PRQ-NOPE", 5000, "pay_unknown"))
		logs = [row.error for row in self.not_settled_logs()]
		self.assertEqual(len(logs), 2)
		self.assertTrue(any("pay_cancelled" in text for text in logs) and any("pay_unknown" in text for text in logs))
		self.assertFalse(self.payment_entries(invoice))

	def test_a_second_distinct_capture_on_a_paid_request_is_logged_and_a_repeat_is_not(self):
		invoice, request = self.pay_request()
		account = request.payment_gateway_account
		self.deliver(account, captured(request.name, 5000, "pay_first"))
		self.deliver(account, captured(request.name, 5000, "pay_first"))
		self.assertFalse(self.not_settled_logs())
		self.deliver(account, captured(request.name, 5000, "pay_second"))
		(log,) = self.not_settled_logs()
		self.assertIn("pay_second", log.error)
		self.assertEqual(len(self.payment_entries(invoice)), 1)

	def test_the_log_carries_no_secret_or_traceback(self):
		invoice, request = self.pay_request()
		self.deliver(request.payment_gateway_account, captured("ACC-PRQ-NOPE", 5000, "pay_unknown"))
		for row in self.not_settled_logs():
			self.assertNotIn("secret", row.error.lower())
			self.assertNotIn("Traceback", row.error)

	def test_a_non_object_body_is_a_bad_request(self):
		for body in (b"[1, 2]", b'"text"', b'{"event": "payment.captured", "payload": "x"}'):
			with self.assertRaises(gateways.BadPayload):
				self.deliver(self.college_account, body)
		self.assertEqual(gateways.BadPayload.http_status_code, 400)

	def test_the_adapter_interface_is_abstract(self):
		with self.assertRaises(NotImplementedError):
			gateways.GatewayAdapter().verify_signature(b"", "", "")
		with self.assertRaises(NotImplementedError):
			gateways.GatewayAdapter().parse_event({})
