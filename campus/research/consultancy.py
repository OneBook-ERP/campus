"""M08-10 Consultancy: GST invoicing of a Consultancy Project and the revenue share of its paid invoices.

Invoicing. A project is invoiced once its agreement is signed (Agreed, until Completed) by an ERPNext Sales Invoice for the
client with the project's service Item, tagged `oc_consultancy_project`. The draft takes its place of supply and GST
template from india_compliance (company GSTIN against the client's GSTIN or address state: CGST + SGST within the state,
IGST across states); Accounts reviews and submits it. A consultancy invoice is submitted only for the project's client
and college, while the project is Agreed or Completed, and the project's invoices (net of credit notes, before GST) never
exceed the agreement value.

Revenue share. A submitted invoice is shared once, and only when it is received in full: outstanding nil, settled only
by Payment Entries and by Journal Entries with a Bank or Cash row (TDS the client deducts in a Payment Entry counts as
received: it is a tax credit), with no credit note (returned against it or reconciled to it), no write-off on the invoice
and no other voucher allocated to it. A Consultancy Revenue Share (approved through the approval engine) splits its
taxable value by the project's rule and pays each team member, an Active Employee of the college, through an HRMS
Additional Salary on the Campus-owned "Consultancy Share" component. A shared invoice can't be cancelled or credited and
its payments can't be cancelled or unreconciled until the share is cancelled. Every check of a share takes the Sales
Invoice row lock first and then reads the shares with locking reads, so a share submit and a cancel queue.

Amounts are in the college's currency: a client billed in another currency is refused.

Figures. AQAR 3.5.1 revenue is the taxable value of the college's consultancy invoices posted in the academic year, net
of credit notes (corporate training, also in 3.5.1, is not modelled). NIRF asks for the amount received per financial
year: consultancy_received sums the receipts above dated in the financial year that holds the academic year's start,
less their GST share (pro rata to the invoice's taxable value over its total)."""

import frappe
from frappe import _
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields
from frappe.utils import flt, fmt_money, today

from campus.research.publications import year_bounds

PROJECT = "Consultancy Project"
SHARE = "Consultancy Revenue Share"
SALARY_COMPONENT = "Consultancy Share"
INVOICEABLE = ("Agreed", "Completed")
PAYMENT_VOUCHERS = ("Payment Entry", "Journal Entry")
INVOICE_FIELDS = ["name", "oc_consultancy_project", "customer", "posting_date", "is_return", "base_net_total",
	"base_total_taxes_and_charges", "base_grand_total", "outstanding_amount"]  # fmt: skip


# ---- invoicing ----------------------------------------------------------------------------------------------------


@frappe.whitelist()
def make_sales_invoice(project, amount, description=None):
	"""A draft Sales Invoice for `amount` (before GST) of the project; the caller needs write access to the project."""
	doc = frappe.get_doc(PROJECT, project)
	doc.check_permission("write")
	return draft_sales_invoice(doc, flt(amount), description).name


def draft_sales_invoice(project, amount, description=None):
	"""Runs with ignore_permissions: client, college, item and amount come from the project and the checked amount."""
	if project.status not in INVOICEABLE:
		frappe.throw(
			_("Consultancy Project {0} is {1}: invoice it once the agreement is signed.").format(
				project.name, _(project.status)
			)
		)
	remaining = flt(project.agreement_value) - invoiced_value(project.name)
	if amount <= 0 or flt(amount, 2) > flt(remaining, 2):
		frappe.throw(
			_("Enter an amount above zero and within the agreement value still to invoice ({0}).").format(
				fmt_money(remaining, currency=project.currency)
			)
		)
	invoice = frappe.new_doc("Sales Invoice")
	invoice.update(
		{
			"company": project.company, "customer": project.client, "customer_address": project.client_address,
			"posting_date": today(), "oc_consultancy_project": project.name,
			"remarks": _("Consultancy Project {0}: {1}").format(project.name, project.title),
			"items": [{"item_code": project.service_item, "qty": 1, "rate": amount, "description": description or project.title}],
		}
	)  # fmt: skip
	invoice.flags.ignore_permissions = True
	invoice.set_missing_values()  # party details: india_compliance's place of supply and GST template, with its taxes
	if invoice.currency != project.currency:
		frappe.throw(
			_("Client {0} is billed in {1}: consultancy invoices are in {2}.").format(
				project.client, invoice.currency, project.currency
			)
		)
	invoice.insert()
	return invoice


def invoiced_value(project, exclude=None, for_update=False):
	"""Taxable value of the project's submitted invoices, net of credit notes."""
	filters = {"oc_consultancy_project": project, "docstatus": 1}
	if exclude:
		filters["name"] = ["!=", exclude]
	values = frappe.db.get_values(
		"Sales Invoice", filters, "base_net_total", for_update=for_update, pluck=True
	)
	return flt(sum(flt(v) for v in values), 2)


def check_invoice(invoice, method=None):
	"""Sales Invoice before_submit: a consultancy invoice bills the project's client for its college within the agreement
	value (the project row is locked so concurrent invoices queue); a credit note never reverses a shared invoice."""
	project_id = getattr(invoice, "oc_consultancy_project", None) or invoice.get("oc_consultancy_project")
	if not project_id:
		return
	project = frappe.db.get_value(
		PROJECT,
		project_id,
		["name", "status", "company", "client", "agreement_value"],
		as_dict=True,
		for_update=True,
	)
	if not project or (invoice.company, invoice.customer) != (project.company, project.client):
		frappe.throw(
			_("Sales Invoice {0} must bill Consultancy Project {1}'s client for its college.").format(
				invoice.name, invoice.oc_consultancy_project
			)
		)
	if invoice.is_return:
		share = invoice.return_against and locked_shares([invoice.return_against])
		if share:
			frappe.throw(
				_("Sales Invoice {0} is shared by {1}: cancel the revenue share before crediting it.").format(
					invoice.return_against, share[0].name
				)
			)
		return
	if project.status not in INVOICEABLE:
		frappe.throw(
			_("Consultancy Project {0} is {1}: it can't be invoiced.").format(project.name, _(project.status))
		)
	total = invoiced_value(project.name, exclude=invoice.name, for_update=True) + flt(invoice.base_net_total)
	if flt(total, 2) > flt(project.agreement_value, 2):
		frappe.throw(
			_("Consultancy Project {0} would be invoiced {1}, above its agreement value of {2}.").format(
				project.name,
				fmt_money(total, currency=invoice.currency),
				fmt_money(project.agreement_value, currency=invoice.currency),
			)
		)


def refuse_invoice_cancel(invoice, method=None):
	"""Sales Invoice before_cancel: a shared invoice stays until its revenue share is cancelled."""
	share = invoice.oc_consultancy_project and locked_shares([invoice.name])
	if share:
		frappe.throw(
			_("Sales Invoice {0} is shared by {1}: cancel the revenue share first.").format(
				invoice.name, share[0].name
			)
		)


def refuse_payment_cancel(voucher, method=None):
	"""Payment Entry / Journal Entry before_cancel: a payment of a shared consultancy invoice stays (the share was paid
	out because it was received)."""
	if voucher.doctype == "Payment Entry":
		invoices = {r.reference_name for r in voucher.references if r.reference_doctype == "Sales Invoice"}
	else:
		invoices = {r.reference_name for r in voucher.accounts if r.reference_type == "Sales Invoice"}
	refuse_if_shared(invoices, _("{0} {1}").format(_(voucher.doctype), voucher.name))


def refuse_unreconcile(unreconcile, method=None):
	"""Unreconcile Payment before_submit: a payment is not taken off a shared consultancy invoice."""
	invoices = {r.reference_name for r in unreconcile.allocations if r.reference_doctype == "Sales Invoice"}
	refuse_if_shared(
		invoices, _("Unreconciling {0} {1}").format(_(unreconcile.voucher_type), unreconcile.voucher_no)
	)


def refuse_if_shared(invoices, what):
	shared = invoices and locked_shares(invoices)
	if shared:
		frappe.throw(
			_("{0} pays a shared consultancy invoice ({1}): cancel the revenue share first.").format(
				what, ", ".join(f"{r.sales_invoice} / {r.name}" for r in shared)
			)
		)


def locked_shares(invoices):
	"""Submitted shares of `invoices`, read after locking the invoice rows (in name order): the lock order of every share
	check, so a share submit and a cancel, credit note or unreconcile of its invoice queue instead of racing."""
	invoices = sorted(set(invoices))
	frappe.db.get_values(
		"Sales Invoice", {"name": ["in", invoices]}, "name", order_by="name", for_update=True
	)
	return frappe.db.get_values(
		SHARE,
		{"sales_invoice": ["in", invoices], "docstatus": 1},
		["name", "sales_invoice"],
		order_by="name",
		as_dict=True,
		for_update=True,
	)


# ---- revenue share ------------------------------------------------------------------------------------------------


def submitted_share(invoice, exclude=None, for_update=False):
	filters = {"sales_invoice": invoice, "docstatus": 1}
	if exclude:
		filters["name"] = ["!=", exclude]
	rows = frappe.db.get_values(SHARE, filters, "name", order_by="name", for_update=for_update, pluck=True)
	return rows[0] if rows else None


def shareable_invoice(invoice, project, share=None, for_update=False):
	"""The invoice's figures; throws unless it is a submitted invoice of `project` (not a credit note), received in full
	(see the module docstring) and not shared by another submitted share. `for_update` locks the invoice row first and
	reads with locks."""
	row = frappe.db.get_value(
		"Sales Invoice", invoice, ["name", "docstatus", "is_return", "outstanding_amount", "oc_consultancy_project", "base_net_total",
		"base_write_off_amount", "company", "customer", "currency"], as_dict=True, for_update=for_update,
	)  # fmt: skip
	if not row or row.oc_consultancy_project != project or row.docstatus != 1 or row.is_return:
		frappe.throw(
			_("Sales Invoice {0} is not a submitted invoice of Consultancy Project {1}.").format(
				invoice, project
			)
		)
	credit_notes = frappe.db.get_values(
		"Sales Invoice",
		{"return_against": invoice, "docstatus": 1},
		"name",
		order_by="name",
		for_update=for_update,
		pluck=True,
	)
	if credit_notes:
		frappe.throw(
			_("Sales Invoice {0} has credit notes ({1}): it can't be shared.").format(
				invoice, ", ".join(credit_notes)
			)
		)
	if flt(row.outstanding_amount) > 0:
		frappe.throw(
			_(
				"Sales Invoice {0} is not fully paid ({1} outstanding): revenue is shared only once it is received."
			).format(invoice, fmt_money(row.outstanding_amount, currency=row.currency))
		)
	if flt(row.base_write_off_amount):
		frappe.throw(_("Sales Invoice {0} has a write-off: only revenue received is shared.").format(invoice))
	not_received = [
		f"{v.voucher_type} {v.voucher_no}" for v in settling_vouchers(invoice, for_update) if not v.received
	]
	if not_received:
		frappe.throw(
			_("Sales Invoice {0} is settled by {1}, not a receipt: only revenue received is shared.").format(
				invoice, ", ".join(not_received)
			)
		)
	other = submitted_share(invoice, exclude=share, for_update=for_update)
	if other:
		frappe.throw(_("Sales Invoice {0} is already shared by {1}.").format(invoice, other))
	return row


def settling_vouchers(invoice, for_update=False):
	"""[{voucher_type, voucher_no, received}] of every other voucher allocated to the invoice (payment ledger, not
	delinked). A receipt is a Payment Entry, or a Journal Entry with a Bank or Cash row."""
	rows = frappe.db.get_values(
		"Payment Ledger Entry",
		{"against_voucher_type": "Sales Invoice", "against_voucher_no": invoice, "voucher_no": ["!=", invoice], "delinked": 0},
		["voucher_type", "voucher_no"], order_by="voucher_no", as_dict=True, for_update=for_update,
	)  # fmt: skip
	vouchers = {(r.voucher_type, r.voucher_no) for r in rows}
	bank_journals = cash_journals([no for kind, no in vouchers if kind == "Journal Entry"])
	return [
		frappe._dict(
			voucher_type=kind, voucher_no=no, received=kind == "Payment Entry" or no in bank_journals
		)
		for kind, no in sorted(vouchers)
	]


def cash_journals(journals):
	"""The Journal Entries of `journals` with a Bank or Cash account row."""
	if not journals:
		return set()
	row, account = frappe.qb.DocType("Journal Entry Account"), frappe.qb.DocType("Account")
	found = (
		frappe.qb.from_(row).join(account).on(account.name == row.account)
		.select(row.parent).distinct()
		.where(row.parent.isin(list(journals)) & account.account_type.isin(("Bank", "Cash")))
	).run(pluck=True)  # fmt: skip
	return set(found)


def check_members(share):
	"""Throw unless every member is an Active Employee of the share's college (an Employee Transfer can move one)."""
	employees = [m.employee for m in share.members]
	rows = frappe.get_all("Employee", {"name": ["in", employees or [""]]}, ["name", "company", "status"])
	valid = {r.name for r in rows if r.company == share.company and r.status == "Active"}
	others = [m.employee_name or m.employee for m in share.members if m.employee not in valid]
	if others:
		frappe.throw(
			_("{0} are not Active Employees of {1}: update the project's team before sharing.").format(
				", ".join(others), share.company
			)
		)


def post_additional_salaries(share):
	"""A submitted Additional Salary per member with an amount, on the member's next payroll. Runs as the final approver
	submitting the share: every input comes from the server-computed share."""
	from campus.examinations.api import next_payroll_date

	for member in share.members:
		if flt(member.amount) <= 0:
			continue
		additional = frappe.get_doc(
			{
				"doctype": "Additional Salary", "employee": member.employee, "company": share.company, "currency": share.currency,
				"salary_component": SALARY_COMPONENT, "amount": member.amount,
				"payroll_date": next_payroll_date(member.employee, share.company, share.posting_date),
				"overwrite_salary_structure_amount": 0, "ref_doctype": SHARE, "ref_docname": share.name,
			}
		)  # fmt: skip
		additional.flags.ignore_permissions = True
		additional.insert()
		additional.submit()
		member.db_set("additional_salary", additional.name)


def cancel_additional_salaries(share):
	"""HRMS refuses while a submitted Salary Slip has paid one."""
	for member in share.members:
		if (
			member.additional_salary
			and frappe.db.get_value("Additional Salary", member.additional_salary, "docstatus") == 1
		):
			additional = frappe.get_doc("Additional Salary", member.additional_salary)
			additional.flags.ignore_permissions = True
			additional.cancel()


# ---- revenue figures (Consultancy Revenue report, AQAR 3.5.1 / NIRF data points) ----------------------------------


def college_projects(company, department=None):
	"""The college's projects the user can read."""
	filters = {"company": company}
	if department:
		filters["department"] = department
	return frappe.get_list(PROJECT, filters, pluck="name", limit_page_length=0)


def invoices_in_year(company, academic_year=None, department=None):
	"""The submitted consultancy invoices (and credit notes) of the college's projects the user can read, posted in the
	academic year (all years when none)."""
	projects = college_projects(company, department)
	if not projects:
		return []
	invoice_filters = {"oc_consultancy_project": ["in", projects], "docstatus": 1, "company": company}
	if academic_year:
		invoice_filters["posting_date"] = ["between", list(year_bounds(academic_year))]
	return frappe.get_all("Sales Invoice", invoice_filters, INVOICE_FIELDS, order_by="posting_date, name")


def receipts(invoices, start=None, end=None):
	"""[{invoice, voucher_no, posting_date, amount}] received against `invoices` (Payment Entries, and Journal Entries
	with a Bank or Cash row), optionally dated between `start` and `end`."""
	if not invoices:
		return []
	ledger = frappe.qb.DocType("Payment Ledger Entry")
	query = (
		frappe.qb.from_(ledger)
		.select(ledger.against_voucher_no.as_("invoice"), ledger.voucher_type, ledger.voucher_no, ledger.posting_date, ledger.amount)
		.where(
			(ledger.against_voucher_type == "Sales Invoice") & ledger.against_voucher_no.isin(list(invoices))
			& ledger.voucher_type.isin(PAYMENT_VOUCHERS) & (ledger.delinked == 0)
		)
	)  # fmt: skip
	if start and end:
		query = query.where(ledger.posting_date.between(start, end))
	rows = query.run(as_dict=True)
	journals = cash_journals({r.voucher_no for r in rows if r.voucher_type == "Journal Entry"})
	return [
		frappe._dict(
			invoice=r.invoice, voucher_no=r.voucher_no, posting_date=r.posting_date, amount=-flt(r.amount)
		)
		for r in rows
		if r.voucher_type == "Payment Entry" or r.voucher_no in journals
	]


def received_by_invoice(invoices):
	"""{invoice: amount received to date}."""
	totals = {}
	for row in receipts(invoices):
		totals[row.invoice] = totals.get(row.invoice, 0) + row.amount
	return totals


def consultancy_received(company, academic_year, department=None):
	"""(amount received before GST, invoices) in the financial year holding the academic year's start (NIRF)."""
	from erpnext.accounts.utils import get_fiscal_year

	start = year_bounds(academic_year)[0]
	_name, fy_start, fy_end = get_fiscal_year(start, company=company)[:3]
	projects = college_projects(company, department)
	invoices = frappe.get_all(
		"Sales Invoice", {"oc_consultancy_project": ["in", projects or [""]], "docstatus": 1, "company": company, "is_return": 0},
		["name", "base_net_total", "base_grand_total"],
	)  # fmt: skip
	taxable_share = {
		r.name: flt(r.base_net_total) / flt(r.base_grand_total) if flt(r.base_grand_total) else 1
		for r in invoices
	}
	rows = receipts(list(taxable_share), fy_start, fy_end)
	value = flt(sum(r.amount * taxable_share[r.invoice] for r in rows), 2)
	return value, sorted({r.invoice for r in rows})


def net_positive(invoices, key):
	"""Distinct `key` values whose invoices net above zero, in first-invoice order."""
	totals = {}
	for row in invoices:
		totals[row[key]] = totals.get(row[key], 0) + flt(row.base_net_total)
	return [name for name, total in totals.items() if total > 0]


def shares_by_invoice(invoices):
	"""{invoice: submitted share figures}."""
	if not invoices:
		return {}
	rows = frappe.get_all(
		SHARE, {"sales_invoice": ["in", list(invoices)], "docstatus": 1},
		["sales_invoice", "institution_amount", "faculty_amount", "staff_amount"],
	)  # fmt: skip
	return {row.sales_invoice: row for row in rows}


def consultancy_revenue(company, academic_year, department=None):
	"""(taxable value net of credit notes, invoices) of the year."""
	rows = invoices_in_year(company, academic_year, department)
	return flt(sum(flt(r.base_net_total) for r in rows), 2), rows


def revenue_by_project(company, academic_year=None, department=None):
	"""Consultancy Revenue report rows: per project, the year's taxable value, GST and total, the amount received to date on
	those invoices, the shares and the paid invoices not yet shared."""
	invoices = invoices_in_year(company, academic_year, department)
	names = [r.name for r in invoices]
	received, shares = received_by_invoice(names), shares_by_invoice(names)
	projects = {}
	for inv in invoices:
		row = projects.setdefault(inv.oc_consultancy_project, frappe._dict(consultancy_project=inv.oc_consultancy_project, invoices=0,
			taxable_value=0, gst=0, invoice_total=0, received=0, institution_share=0, faculty_share=0, staff_share=0,
			paid_not_shared=0))  # fmt: skip
		row.invoices += 0 if inv.is_return else 1
		row.taxable_value += flt(inv.base_net_total)
		row.gst += flt(inv.base_total_taxes_and_charges)
		row.invoice_total += flt(inv.base_grand_total)
		row.received += received.get(inv.name, 0)
		share = shares.get(inv.name)
		if (
			not share
			and not inv.is_return
			and flt(received.get(inv.name, 0), 2) >= flt(inv.base_grand_total, 2)
		):
			row.paid_not_shared += 1
		if share:
			row.institution_share += flt(share.institution_amount)
			row.faculty_share += flt(share.faculty_amount)
			row.staff_share += flt(share.staff_amount)
	details = {
		p.name: p
		for p in frappe.get_all(
			PROJECT, {"name": ["in", list(projects) or [""]]},
			["name", "title", "client", "department", "principal_consultant", "status", "agreement_value", "currency"],
		)
	}  # fmt: skip
	result = []
	for name, row in projects.items():
		project = details[name]
		row.update(
			{
				key: project[key]
				for key in (
					"title",
					"client",
					"department",
					"principal_consultant",
					"status",
					"agreement_value",
					"currency",
				)
			}
		)
		result.append(row)
	return result


# ---- setup --------------------------------------------------------------------------------------------------------


def setup_consultancy_fields():
	create_custom_fields(
		{
			"Sales Invoice": [
				{
					"fieldname": "oc_consultancy_project", "label": "Consultancy Project", "fieldtype": "Link", "options": PROJECT,
					"insert_after": "customer_name", "read_only": 1, "search_index": 1, "in_standard_filter": 1,
				}
			]
		},
		update=True,
	)  # fmt: skip


def seed_salary_component():
	"""Campus's own earning component, created once and never changed afterwards."""
	if frappe.db.exists("Salary Component", SALARY_COMPONENT):
		return
	frappe.get_doc(
		{
			"doctype": "Salary Component", "salary_component": SALARY_COMPONENT, "salary_component_abbr": "CONS", "type": "Earning",
			"description": "Consultancy revenue share (OneBook Campus)", "is_tax_applicable": 1, "depends_on_payment_days": 0,
		}
	).insert(ignore_permissions=True)  # fmt: skip
