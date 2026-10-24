"""P2-T10 (M07-07) integration tests: payroll for aided and self-financing staff on HRMS.

Fixture (Demo College of Engineering, September 2026, a holiday list without holidays so 30 working days, fiscal year
2026-27): the seeded Aided and Self-financing Salary Structures are submitted as a college would after review, and
assigned from 2026-04-01 by campus.faculty.api.assign_salary_structure. Professional Tax stays unconfigured (its seeded
row is disabled), so no PT is deducted.

Deductions use the Campus-owned components Campus EPF and Campus PT; HRMS's Provident Fund and Professional Tax are
never changed.

Hand figures from the seeded structure records (DA, AGP and Campus EPF round to the nearest rupee; HRMS's HRA does not):
- faculty_1, Aided, base (pay) 40,000 and variable (AGP) 9,000: Basic 40,000, AGP 9,000, DA 50% of 49,000 = 24,500,
  HRA 10% of 49,000 = 4,900; gross 78,400. PF 12% of (Basic + AGP + DA) 73,500 capped at the 15,000 wage ceiling
  = 1,800; net 76,600.
- faculty_2, Self-financing, base 30,000: Basic 30,000, DA 10% = 3,000, HRA 10% = 3,000; gross 36,000. PF 12% of
  min(33,000, 15,000) = 1,800; net 34,200.
- faculty_2 with 15 days' leave without pay (15 of 30 payment days): Basic 15,000, DA 1,500, HRA 1,500; gross 18,000.
  PF on the prorated wage min(16,500, 15,000) = 1,800 (not halved again); net 16,200.
- faculty_2 with 20 days' leave without pay (10 of 30): Basic 10,000, DA 1,000, HRA 1,000; gross 12,000. PF 12% of
  11,000 (under the ceiling) = 1,320; net 10,680.
- faculty_3, Self-financing, base 12,346: Basic 12,346, DA 1,234.6 -> 1,235, HRA 1,234.6; gross 14,815.6.
  PF 12% of 13,581 = 1,629.72 -> 1,630; net 13,185.6.
- faculty_1 after a 3% increment (Employee Promotion from 2026-08-01, new assignment base 41,200): Basic 41,200,
  AGP 9,000, DA 25,100, HRA 5,020; gross 80,320, PF 1,800, net 78,520. Its July slip keeps the old figures.
"""

from unittest import mock

import frappe
from frappe.utils import flt

from campus.faculty import api, payroll_setup
from campus.faculty.payroll_setup import EPF, PT, seed_salary_components, seed_salary_structures, structure_name
from campus.tests import demo
from campus.tests.utils import CampusTestCase

START, END = "2026-09-01", "2026-09-30"
ASSIGNED_FROM = "2026-04-01"
AIDED, SELF_FINANCING = "Aided", "Self-financing"


class PayrollFixture(CampusTestCase):
	demo_scale = "small"

	def setUp(self):
		super().setUp()
		frappe.db.savepoint("payroll_test")
		self.addCleanup(frappe.db.rollback, save_point="payroll_test")  # tearDown is skipped when setUp raises
		self.company = demo.ENGINEERING
		self.aided, self.self_financing = demo.employee_of("faculty_1"), demo.employee_of("faculty_2")
		seed_salary_components()
		seed_salary_structures(self.company)
		self.make_holiday_list()
		if not frappe.db.exists("Fiscal Year", {"year_start_date": ["<=", START], "year_end_date": [">=", END]}):
			frappe.get_doc(
				{"doctype": "Fiscal Year", "year": "Payroll Test 2026-27", "year_start_date": "2026-04-01", "year_end_date": "2027-03-31"}
			).insert(ignore_permissions=True)

	def submit_structures(self, company=None):
		for mode in (AIDED, SELF_FINANCING):
			frappe.get_doc("Salary Structure", structure_name(mode, company or self.company)).submit()

	def make_holiday_list(self):
		holiday_list = frappe.get_doc(
			{"doctype": "Holiday List", "holiday_list_name": "Payroll Test 2026", "from_date": "2026-01-01", "to_date": "2026-12-31"}
		).insert(ignore_permissions=True)
		frappe.get_doc(
			{
				"doctype": "Holiday List Assignment", "applicable_for": "Company", "assigned_to": self.company,
				"holiday_list": holiday_list.name, "from_date": "2026-01-01",
			}
		).insert(ignore_permissions=True).submit()  # fmt: skip

	def assign_demo_staff(self):
		api.assign_salary_structure(self.aided, ASSIGNED_FROM, base=40000, variable=9000)
		api.assign_salary_structure(self.self_financing, ASSIGNED_FROM, base=30000)

	def amounts(self, slip):
		rows = {r.salary_component: r.amount for r in (*slip.earnings, *slip.deductions)}
		return rows, (slip.gross_pay, slip.total_deduction, slip.net_pay)

	def leave_without_pay(self, employee, from_date, to_date):
		leave_type = demo.get_or_insert("Leave Type", {"leave_type_name": "Payroll Test LWP"}, {"is_lwp": 1})
		frappe.db.set_single_value("HR Settings", "leave_approver_mandatory_in_leave_application", 0)
		frappe.get_doc(
			{
				"doctype": "Leave Application", "employee": employee, "leave_type": leave_type, "from_date": from_date,
				"to_date": to_date, "status": "Approved", "company": self.company, "posting_date": from_date,
			}
		).insert(ignore_permissions=True).submit()  # fmt: skip

	def direct_slip(self, employee, start=START, end=END):
		return frappe.get_doc(
			{"doctype": "Salary Slip", "employee": employee, "start_date": start, "end_date": end, "posting_date": end, "payroll_frequency": "Monthly"}
		).insert(ignore_permissions=True)


AIDED_ROWS = {
	"Basic": 40000, "Academic Grade Pay": 9000, "Dearness Allowance": 24500, "House Rent Allowance": 4900,
	"Campus EPF": 1800,
}  # fmt: skip
SELF_FINANCING_ROWS = {"Basic": 30000, "Dearness Allowance": 3000, "House Rent Allowance": 3000, "Campus EPF": 1800}


class TestPayrollEntry(PayrollFixture):
	def test_payroll_entry_slips_match_the_fixture(self):
		self.submit_structures()
		self.assign_demo_staff()
		entry = self.payroll_entry()
		slips = {s.employee: frappe.get_doc("Salary Slip", s.name) for s in frappe.get_all("Salary Slip", {"payroll_entry": entry.name}, ["name", "employee"])}

		self.assertEqual(set(slips), {self.aided, self.self_financing})
		self.assertEqual(self.amounts(slips[self.aided]), (AIDED_ROWS, (78400, 1800, 76600)))
		self.assertEqual(self.amounts(slips[self.self_financing]), (SELF_FINANCING_ROWS, (36000, 1800, 34200)))

	def test_increment_by_promotion_is_a_new_assignment(self):
		self.submit_structures()
		self.assign_demo_staff()
		promotion = frappe.get_doc(
			{
				"doctype": "Employee Promotion", "employee": self.aided, "promotion_date": "2026-08-01", "company": self.company,
				"current_ctc": 49000 * 12, "revised_ctc": 50200 * 12,
			}
		).insert(ignore_permissions=True)  # fmt: skip
		promotion.submit()
		api.assign_salary_structure(self.aided, promotion.promotion_date, base=41200, variable=9000)

		rows, totals = self.amounts(self.direct_slip(self.aided))
		self.assertEqual(rows["Basic"], 41200)
		self.assertEqual(rows["Dearness Allowance"], 25100)
		self.assertEqual(totals, (80320, 1800, 78520))
		self.assertEqual(self.amounts(self.direct_slip(self.aided, "2026-07-01", "2026-07-31")), (AIDED_ROWS, (78400, 1800, 76600)))

	def test_pf_on_the_prorated_wage_with_leave_without_pay(self):
		self.submit_structures()
		self.assign_demo_staff()
		self.leave_without_pay(self.self_financing, "2026-09-01", "2026-09-15")
		slip = self.direct_slip(self.self_financing)
		self.assertEqual(slip.payment_days, 15)
		self.assertEqual(
			self.amounts(slip),
			({"Basic": 15000, "Dearness Allowance": 1500, "House Rent Allowance": 1500, "Campus EPF": 1800}, (18000, 1800, 16200)),
		)

	def test_pf_under_the_ceiling_with_leave_without_pay(self):
		self.submit_structures()
		self.assign_demo_staff()
		self.leave_without_pay(self.self_financing, "2026-09-01", "2026-09-20")
		slip = self.direct_slip(self.self_financing)
		self.assertEqual(slip.payment_days, 10)
		self.assertEqual(
			self.amounts(slip),
			({"Basic": 10000, "Dearness Allowance": 1000, "House Rent Allowance": 1000, "Campus EPF": 1320}, (12000, 1320, 10680)),
		)

	def test_submitted_slips_carry_campus_epf_as_provident_fund(self):
		"""HRMS 16 dropped its Provident Fund Deductions report; PF listings now read the slips' Provident Fund-type rows."""
		self.submit_structures()
		self.assign_demo_staff()
		self.direct_slip(self.self_financing).submit()
		slip, detail, component = frappe.qb.DocType("Salary Slip"), frappe.qb.DocType("Salary Detail"), frappe.qb.DocType("Salary Component")
		pf = (
			frappe.qb.from_(slip)
			.join(detail).on((detail.parent == slip.name) & (detail.parenttype == "Salary Slip") & (detail.parentfield == "deductions"))
			.join(component).on(component.name == detail.salary_component)
			.where((slip.docstatus == 1) & (slip.company == self.company) & (slip.start_date == START))
			.where(component.component_type == "Provident Fund")
			.select(slip.employee, detail.salary_component, detail.amount)
			.run()
		)
		self.assertEqual([(e, c, flt(a)) for e, c, a in pf], [(self.self_financing, EPF, 1800)])

	def test_fractional_amounts_round_on_the_seeded_components(self):
		self.submit_structures()
		employee = demo.employee_of("faculty_3")
		api.assign_salary_structure(employee, ASSIGNED_FROM, base=12346)
		self.assertEqual(
			self.amounts(self.direct_slip(employee)),
			(
				{"Basic": 12346, "Dearness Allowance": 1235, "House Rent Allowance": 1234.6, "Campus EPF": 1630},
				(14815.6, 1630, 13185.6),
			),
		)


	def payroll_entry(self):
		company = frappe.get_cached_doc("Company", self.company)
		entry = frappe.new_doc("Payroll Entry")  # new_doc: the defaults the employee filter needs (not timesheet-based)
		entry.update(
			{
				"company": self.company, "posting_date": END, "payroll_frequency": "Monthly",
				"start_date": START, "end_date": END, "currency": company.default_currency, "exchange_rate": 1,
				"cost_center": company.cost_center, "payroll_payable_account": company.default_payroll_payable_account,
			}
		)  # fmt: skip
		frappe.db.set_value("Account", entry.payroll_payable_account, "account_type", "Payable")  # a college sets this once (guide step 1)
		entry.fill_employee_details()
		entry.insert(ignore_permissions=True)
		entry.submit()  # creates the draft slips (fewer than 30 employees: synchronously)
		return entry


class TestAssignmentHelper(PayrollFixture):
	def test_assignment_follows_the_employee_funding_mode(self):
		self.submit_structures()
		self.assign_demo_staff()
		for employee, mode in ((self.aided, AIDED), (self.self_financing, SELF_FINANCING)):
			ssa = frappe.get_doc("Salary Structure Assignment", {"employee": employee, "from_date": ASSIGNED_FROM})
			self.assertEqual((ssa.salary_structure, ssa.company, ssa.docstatus), (structure_name(mode, self.company), self.company, 1))

	def test_assignment_refuses_bad_input(self):
		self.submit_structures()
		with self.assertRaises(frappe.DoesNotExistError):
			api.assign_salary_structure("HR-EMP-NOPE", ASSIGNED_FROM, base=40000)
		for base in (0, -100):
			with self.assertRaises(frappe.ValidationError):
				api.assign_salary_structure(self.aided, ASSIGNED_FROM, base=base)
		self.assertFalse(frappe.db.exists("Salary Structure Assignment", {"employee": self.aided}))

	def test_existing_draft_assignment_is_refused(self):
		self.submit_structures()
		self.draft_assignment(self.aided, structure_name(AIDED, self.company))
		with self.assertRaises(frappe.ValidationError):
			api.assign_salary_structure(self.aided, ASSIGNED_FROM, base=40000)

	def test_existing_assignment_of_another_mode_is_refused(self):
		self.submit_structures()
		self.draft_assignment(self.aided, structure_name(SELF_FINANCING, self.company)).submit()
		with self.assertRaises(frappe.ValidationError):
			api.assign_salary_structure(self.aided, ASSIGNED_FROM, base=40000)

	def draft_assignment(self, employee, structure):
		return frappe.get_doc(
			{
				"doctype": "Salary Structure Assignment", "employee": employee, "company": self.company, "from_date": ASSIGNED_FROM,
				"salary_structure": structure, "base": 40000, "currency": "INR",
			}
		).insert(ignore_permissions=True)  # fmt: skip

	def test_assignment_is_idempotent_per_employee_and_from_date(self):
		self.submit_structures()
		first = api.assign_salary_structure(self.aided, ASSIGNED_FROM, base=40000, variable=9000)
		again = api.assign_salary_structure(self.aided, ASSIGNED_FROM, base=45000)
		self.assertEqual(again, first)
		self.assertEqual(frappe.db.count("Salary Structure Assignment", {"employee": self.aided, "docstatus": ["<", 2]}), 1)
		self.assertEqual(frappe.db.get_value("Salary Structure Assignment", first, "base"), 40000)

	def test_only_the_employee_company_structure_is_used(self):
		seed_salary_structures(demo.ARTS)
		self.submit_structures(demo.ARTS)  # Engineering's stay drafts
		with self.assertRaises(frappe.ValidationError):
			api.assign_salary_structure(self.aided, ASSIGNED_FROM, base=40000)
		self.assertFalse(frappe.db.exists("Salary Structure Assignment", {"employee": self.aided}))

	def test_assignment_needs_a_funding_mode(self):
		self.submit_structures()
		frappe.db.set_value("Employee", self.aided, "oc_funding_mode", "")
		with self.assertRaises(frappe.ValidationError):
			api.assign_salary_structure(self.aided, ASSIGNED_FROM, base=40000)

	def test_two_active_structures_for_a_mode_are_refused(self):
		self.submit_structures()
		copy = frappe.copy_doc(frappe.get_doc("Salary Structure", structure_name(AIDED, self.company)))
		copy.name = "Aided Copy"
		copy.insert(ignore_permissions=True).submit()
		with self.assertRaises(frappe.ValidationError):
			api.assign_salary_structure(self.aided, ASSIGNED_FROM, base=40000)


class TestSeeding(PayrollFixture):
	def test_components_seeded_once_and_edits_kept(self):
		for component in ("Basic", "Dearness Allowance", "House Rent Allowance", "Academic Grade Pay"):
			self.assertTrue(frappe.db.exists("Salary Component", component))
		frappe.db.set_value("Salary Component", "Dearness Allowance", "description", "Edited by the college")
		seed_salary_components()
		self.assertEqual(frappe.db.get_value("Salary Component", "Dearness Allowance", "description"), "Edited by the college")

	def test_campus_statutory_components(self):
		epf = frappe.db.get_value("Salary Component", EPF, ["type", "component_type", "depends_on_payment_days", "round_to_the_nearest_integer"])
		pt = frappe.db.get_value("Salary Component", PT, ["type", "component_type", "depends_on_payment_days", "round_to_the_nearest_integer"])
		self.assertEqual(epf, ("Deduction", "Provident Fund", 0, 1))
		self.assertEqual(pt, ("Deduction", "Professional Tax", 0, 1))

	def test_hrms_statutory_components_are_never_changed(self):
		fields = ["component_type", "depends_on_payment_days", "round_to_the_nearest_integer"]
		for name in ("Provident Fund", "Professional Tax"):
			frappe.db.set_value("Salary Component", name, {"component_type": "", "depends_on_payment_days": 1, "round_to_the_nearest_integer": 0})
		frappe.db.set_value("Company", self.company, "oc_payroll_templates_seeded", 0)
		for mode in ("Aided", "Self-financing"):
			frappe.delete_doc("Salary Structure", structure_name(mode, self.company), ignore_permissions=True)
		seed_salary_components()
		seed_salary_structures(self.company)
		for name in ("Provident Fund", "Professional Tax"):
			self.assertEqual(frappe.db.get_value("Salary Component", name, fields), ("", 1, 0), name)
		used = frappe.get_all("Salary Detail", {"parent": structure_name("Aided", self.company)}, pluck="salary_component")
		self.assertNotIn("Provident Fund", used)
		self.assertIn(EPF, used)

	def test_fresh_site_order(self):
		"""On a fresh site Campus seeds before HRMS India adds Component Type (it does so with the first Indian company)."""
		frappe.db.delete("Salary Component", {"name": ["in", [EPF, PT]]})
		with mock.patch.object(payroll_setup, "has_component_type", return_value=False):
			seed_salary_components()
		self.assertFalse(frappe.db.get_value("Salary Component", EPF, "component_type"))
		seed_salary_components()  # the next migrate
		self.assertEqual(frappe.db.get_value("Salary Component", EPF, "component_type"), "Provident Fund")
		self.submit_structures()
		self.assign_demo_staff()
		self.assertEqual(self.amounts(self.direct_slip(self.self_financing)), (SELF_FINANCING_ROWS, (36000, 1800, 34200)))

	WIRED = {"component_type": "Provident Fund", "depends_on_payment_days": 0, "round_to_the_nearest_integer": 1}

	def wire_like_the_old_version(self):
		frappe.db.set_value("Salary Component", "Provident Fund", dict(self.WIRED))  # set_value adds modified to the dict
		frappe.db.set_value("Salary Component", "Professional Tax", "component_type", "Professional Tax")
		frappe.db.set_global(payroll_setup.OLD_STATUTORY_FLAG, 1)

	def hrms_pf(self):
		return frappe.db.get_value("Salary Component", "Provident Fund", list(self.WIRED))

	def test_retiring_the_old_wiring_swaps_drafts_and_changes_no_hrms_component(self):
		self.wire_like_the_old_version()
		seeded = frappe.get_doc("Salary Structure", structure_name(AIDED, self.company))
		frappe.db.set_value("Salary Detail", seeded.deductions[0].name, {"salary_component": "Provident Fund", "abbr": "PF"})
		payroll_setup.retire_statutory_wiring()
		self.assertEqual(self.hrms_pf(), ("Provident Fund", 0, 1))
		self.assertEqual(frappe.db.get_value("Salary Component", "Professional Tax", "component_type"), "Professional Tax")
		self.assertEqual(frappe.db.get_value("Salary Detail", seeded.deductions[0].name, ["salary_component", "abbr"]), (EPF, "CEPF"))
		self.assertFalse(frappe.db.get_global(payroll_setup.OLD_STATUTORY_FLAG))

	def test_submitted_structure_on_hrms_pf_keeps_its_pf(self):
		self.wire_like_the_old_version()
		name = structure_name(SELF_FINANCING, self.company)
		row = frappe.get_doc("Salary Structure", name).deductions[0].name
		frappe.db.set_value("Salary Detail", row, {"salary_component": "Provident Fund", "abbr": "PF"})
		self.submit_structures()
		self.assign_demo_staff()
		self.leave_without_pay(self.self_financing, "2026-09-01", "2026-09-20")
		before = self.amounts(self.direct_slip(self.self_financing))
		frappe.db.delete("Salary Slip", {"employee": self.self_financing})

		payroll_setup.retire_statutory_wiring()

		self.assertEqual(self.hrms_pf(), ("Provident Fund", 0, 1))
		self.assertEqual(frappe.db.get_value("Salary Detail", row, "salary_component"), "Provident Fund")
		after = self.amounts(self.direct_slip(self.self_financing))
		self.assertEqual(after, before)
		self.assertEqual(after[0]["Provident Fund"], 1320)

	def test_professional_tax_seeded_disabled_with_a_todo(self):
		doc = frappe.get_doc("Salary Structure", structure_name(SELF_FINANCING, self.company))
		pt = next(r for r in doc.deductions if r.salary_component == PT)
		self.assertEqual(pt.amount, 0)
		self.assertIn("TODO", pt.condition)
		self.assertFalse(frappe.safe_eval(pt.condition))
		self.assertIn("PLACEHOLDER", doc.oc_seed_note)

	def test_deleted_structure_is_not_seeded_again(self):
		frappe.delete_doc("Salary Structure", structure_name(AIDED, self.company), ignore_permissions=True)
		seed_salary_structures(self.company)
		self.assertFalse(frappe.db.exists("Salary Structure", structure_name(AIDED, self.company)))
		self.assertTrue(frappe.db.get_value("Company", self.company, "oc_payroll_templates_seeded"))

	def test_structures_seeded_as_drafts_once_per_mode(self):
		seed_salary_structures(self.company)
		for mode in (AIDED, SELF_FINANCING):
			rows = frappe.get_all("Salary Structure", {"company": self.company, "oc_funding_mode": mode}, ["name", "docstatus"])
			self.assertEqual(rows, [{"name": structure_name(mode, self.company), "docstatus": 0}])

	def test_edited_structure_is_not_overwritten(self):
		name = structure_name(AIDED, self.company)
		doc = frappe.get_doc("Salary Structure", name)
		doc.earnings[0].formula = "base * 2"
		doc.save(ignore_permissions=True)
		seed_salary_structures(self.company)
		self.assertEqual(frappe.get_doc("Salary Structure", name).earnings[0].formula, "base * 2")
