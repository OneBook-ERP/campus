"""M07-07 Payroll setup for aided and self-financing staff, on HRMS payroll (no parallel payroll DocTypes).

Campus never changes a Salary Component it did not create. Each run creates, when missing:
- the pay components Basic, Dearness Allowance (DA), House Rent Allowance (HRA) and Academic Grade Pay (AGP)
  (DA and AGP round to the nearest rupee);
- its own statutory components "Campus EPF" (Component Type Provident Fund, so PF listings over the slips find
  it; not by payment days, rounded) and "Campus PT" (Component Type Professional Tax, a disabled
  placeholder). HRMS's Provident Fund and Professional Tax are left alone. The Component Type field comes from HRMS
  India with the first Indian company, so it is filled on Campus's components on a later run if it was missing.
Once per college (Company.oc_payroll_templates_seeded): one draft Salary Structure per funding mode (Aided,
Self-financing) with PLACEHOLDER rates, for the college to review and submit. A deleted one is not seeded again.

The rates (DA %, HRA %, PF % and wage ceiling) live in those structure records: payroll reads only the records, the
defaults below are used only to create them. Earnings are written on `base` (pay, from the Salary Structure
Assignment) and `variable` (AGP), because HRMS's Basic and HRA depend on payment days and HRMS refuses a payment-days
formula that reads another payment-days component. Campus EPF does not depend on payment days, so it reads the
prorated Basic, AGP and DA. Campus PT is seeded disabled (amount 0, a false Condition with a TODO): state slabs must be
entered by the college. See docs/usage/payroll.md.

`retire_statutory_wiring` (patch use_campus_statutory_components), on a site an earlier version wired, moves Campus's
draft structures to Campus EPF / PT, changes no HRMS or college component, and prints what may need a manual review.
"""

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

MODULE = "Faculty"
FUNDING_MODES = ("Aided", "Self-financing")

EPF, PT = "Campus EPF", "Campus PT"
# name, abbreviation, type, HRMS India component type, depends on payment days, round to the nearest rupee
COMPONENTS = [
	("Basic", "B", "Earning", None, 1, 0),
	("Dearness Allowance", "DA", "Earning", None, 1, 1),
	("House Rent Allowance", "HRA", "Earning", None, 1, 0),
	("Academic Grade Pay", "AGP", "Earning", None, 1, 1),
	(EPF, "CEPF", "Deduction", "Provident Fund", 0, 1),
	(PT, "CPT", "Deduction", "Professional Tax", 0, 1),
]
SEED_NOTE = "PLACEHOLDER RATES - review every rate before submitting (seeded by OneBook Campus; see the payroll guide)."
PT_TODO = '0 and "TODO: enter your state Professional Tax slab as the condition and amount"'

# funding mode -> earnings, deductions as (component, formula or None, amount, condition). In formulas {B}, {AGP} and
# {DA} are replaced by the site's abbreviations of Basic, Academic Grade Pay and Dearness Allowance.
STRUCTURES = {
	"Aided": (
		[
			("Basic", "base", 0, None),
			("Academic Grade Pay", "variable", 0, None),
			("Dearness Allowance", "(base + variable) * 0.50", 0, None),
			("House Rent Allowance", "(base + variable) * 0.10", 0, None),
		],
		[
			(EPF, "min({B} + {AGP} + {DA}, 15000) * 0.12", 0, None),
			(PT, None, 0, PT_TODO),
		],
	),
	"Self-financing": (
		[
			("Basic", "base", 0, None),
			("Dearness Allowance", "base * 0.10", 0, None),
			("House Rent Allowance", "base * 0.10", 0, None),
		],
		[
			(EPF, "min({B} + {DA}, 15000) * 0.12", 0, None),
			(PT, None, 0, PT_TODO),
		],
	),
}


def setup_payroll():
	create_custom_fields({"Salary Structure": salary_structure_fields(), "Company": company_fields()}, update=True)
	seed_salary_components()
	for company in college_companies():
		seed_salary_structures(company)


def salary_structure_fields():
	return [
		{
			"fieldname": "oc_funding_mode", "fieldtype": "Select", "label": "Funding Mode", "insert_after": "company",
			"options": "\n" + "\n".join(FUNDING_MODES), "in_standard_filter": 1, "module": MODULE,
			"description": "The staff this structure is for; campus.faculty.api.assign_salary_structure picks it by the employee's funding mode.",
		},
		{
			"fieldname": "oc_seed_note", "fieldtype": "Small Text", "label": "Review Note", "insert_after": "oc_funding_mode",
			"module": MODULE, "no_copy": 1,
		},
	]  # fmt: skip


def company_fields():
	return [
		{
			"fieldname": "oc_payroll_templates_seeded", "fieldtype": "Check", "label": "Payroll Templates Seeded", "insert_after": "default_payroll_payable_account",
			"hidden": 1, "read_only": 1, "no_copy": 1, "module": MODULE,
		}
	]  # fmt: skip


def seed_salary_components():
	"""Create the missing Campus components; fill Component Type on Campus EPF / PT once HRMS India has added the field."""
	typed = has_component_type()
	for name, abbr, kind, component_type, by_payment_days, rounded in COMPONENTS:
		if frappe.db.exists("Salary Component", name):
			if typed and component_type and name in (EPF, PT) and not frappe.db.get_value("Salary Component", name, "component_type"):
				frappe.db.set_value("Salary Component", name, "component_type", component_type)
			continue
		frappe.get_doc(
			{
				"doctype": "Salary Component", "salary_component": name, "salary_component_abbr": abbr, "type": kind,
				"description": name, "is_tax_applicable": int(kind == "Earning"), "depends_on_payment_days": by_payment_days,
				"round_to_the_nearest_integer": rounded, **({"component_type": component_type} if typed and component_type else {}),
			}
		).insert(ignore_permissions=True)  # fmt: skip


def has_component_type():
	return frappe.get_meta("Salary Component").has_field("component_type")


def college_companies():
	"""Colleges: companies with an Institution Profile that are not groups (a trust)."""
	profiles = frappe.get_all("Institution Profile", pluck="company")
	return frappe.get_all("Company", {"name": ["in", profiles], "is_group": 0}, pluck="name") if profiles else []


def structure_name(funding_mode, company):
	return f"{funding_mode} Staff - {frappe.get_cached_value('Company', company, 'abbr')}"


def seed_salary_structures(company):
	"""Once per company: a draft Salary Structure per funding mode, unless one exists for that mode."""
	if frappe.db.get_value("Company", company, "oc_payroll_templates_seeded"):
		return
	currency = frappe.get_cached_value("Company", company, "default_currency")
	abbrs = abbreviations()
	for mode, (earnings, deductions) in STRUCTURES.items():
		name = structure_name(mode, company)
		if frappe.db.exists("Salary Structure", name) or frappe.db.exists("Salary Structure", {"company": company, "oc_funding_mode": mode}):
			continue
		frappe.get_doc(
			{
				"doctype": "Salary Structure", "name": name, "company": company, "oc_funding_mode": mode, "currency": currency,
				"payroll_frequency": "Monthly", "is_active": "Yes", "oc_seed_note": SEED_NOTE,
				"earnings": rows(earnings, abbrs), "deductions": rows(deductions, abbrs),
			}
		).insert(ignore_permissions=True)  # fmt: skip
	frappe.db.set_value("Company", company, "oc_payroll_templates_seeded", 1)


def abbreviations():
	names = {"B": "Basic", "AGP": "Academic Grade Pay", "DA": "Dearness Allowance"}
	return {key: frappe.db.get_value("Salary Component", name, "salary_component_abbr") or key for key, name in names.items()}


def rows(components, abbrs):
	return [
		{
			"salary_component": component, "amount_based_on_formula": 1 if formula else 0,
			"formula": formula.format(**abbrs) if formula else None, "amount": amount, "condition": condition,
		}
		for component, formula, amount, condition in components
	]  # fmt: skip


# ---- retire the earlier one-time wiring of HRMS's components (patch use_campus_statutory_components) --------------

OLD_STATUTORY_FLAG = "campus_payroll_statutory_wired"
# what the earlier version changed, for the college to review by hand; Campus does not change these back
OLD_WIRING_NOTE = (
	"Provident Fund (Component Type, Depends on Payment Days off, Round to the Nearest Integer), "
	"Professional Tax (Component Type), Dearness Allowance and Academic Grade Pay (Round to the Nearest Integer)"
)
SWAPS = {"Provident Fund": EPF, "Professional Tax": PT}


def retire_statutory_wiring():
	"""On a site the earlier version wired: move Campus's draft structures to Campus EPF / PT, print what a college may
	review by hand, and clear the old flag. No HRMS or college Salary Component is changed. DML only."""
	if not frappe.db.get_global(OLD_STATUTORY_FLAG):
		return
	seed_salary_components()
	ours = campus_structures()
	swap_draft_rows(ours)
	print(
		"OneBook Campus no longer changes HRMS's salary components. An earlier version changed: "
		f"{OLD_WIRING_NOTE}. They are left as they are; review them if your own salary structures use them."
	)
	frappe.defaults.clear_default(OLD_STATUTORY_FLAG, parent="__global")


def campus_structures():
	"""Names of the structures Campus seeded (by their seeded names)."""
	names = [structure_name(mode, c) for c in frappe.get_all("Company", pluck="name") for mode in FUNDING_MODES]
	return frappe.get_all("Salary Structure", {"name": ["in", names]}, pluck="name") if names else []


def swap_draft_rows(structures):
	"""Draft Campus structures move from HRMS's PF / PT rows to Campus EPF / PT; submitted ones are listed, not changed."""
	drafts = frappe.get_all("Salary Structure", {"name": ["in", structures or [""]], "docstatus": 0}, pluck="name")
	for old, new in SWAPS.items():
		abbr = frappe.db.get_value("Salary Component", new, "salary_component_abbr")
		rows = frappe.get_all(
			"Salary Detail", {"parenttype": "Salary Structure", "parent": ["in", drafts or [""]], "salary_component": old}, pluck="name"
		)
		for row in rows:
			frappe.db.set_value("Salary Detail", row, {"salary_component": new, "abbr": abbr})
	for name in sorted(set(structures) - set(drafts)):
		if frappe.db.exists("Salary Detail", {"parent": name, "parenttype": "Salary Structure", "salary_component": ["in", list(SWAPS)]}):
			print(f"Salary Structure {name} is submitted and uses HRMS's Provident Fund / Professional Tax; review it.")
