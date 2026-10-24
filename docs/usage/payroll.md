# Payroll for aided and self-financing staff (M07-07)

Payroll is HRMS payroll: Salary Component, Salary Structure, Salary Structure Assignment, Employee Promotion,
Payroll Entry and Salary Slip. Campus adds no payroll DocTypes. It adds a Funding Mode field on Salary Structure,
seeds components and two draft structures, and provides one helper that assigns a structure by the employee's
Funding Mode (Employee > Faculty tab > Funding Mode: Aided or Self-financing).

## What migrate seeds (once; your edits are never overwritten)
- **Salary Components**, each run, when missing: Basic (B), Dearness Allowance (DA), House Rent Allowance (HRA),
  Academic Grade Pay (AGP), and Campus's own statutory components **Campus EPF** (CEPF) and **Campus PT** (CPT).
  Campus never changes a component it did not create: HRMS's Basic, HRA, Provident Fund and Professional Tax, and
  any DA / AGP your college already had, stay as they are.
  - DA and AGP created by Campus round to the nearest rupee; HRMS's Basic and HRA keep paise unless you tick Round to
    the Nearest Integer on them.
  - Campus EPF: Component Type Provident Fund (it shows as its own column in the HRMS Salary Register), **not** by
    payment days (it is computed on the already-prorated wage, see below), rounded.
  - Campus PT: Component Type Professional Tax (its own Salary Register column), not by payment
    days, rounded; a placeholder until you configure it.
  - Component Type is a field HRMS India adds with the first Indian company. On a new site set up before any company,
    the next migrate fills it on Campus EPF / PT.
- **Two draft Salary Structures per college, once** (each non-group company with an Institution Profile; the hidden
  Company flag Payroll Templates Seeded records it), named "Aided Staff - ABBR" and "Self-financing Staff - ABBR",
  with Funding Mode set and the Review Note "PLACEHOLDER RATES - review every rate before submitting". If you delete
  one, it is not seeded again.

**All rates below are PLACEHOLDERS.** Check each against your pay commission, state orders and EPF registration
before submitting the structures.

| Row | Aided | Self-financing |
| --- | --- | --- |
| Basic | `base` (pay band pay, or 7th CPC pay-matrix cell) | `base` |
| Academic Grade Pay | `variable` (0 by default) | - |
| Dearness Allowance | `(base + variable) * 0.50` (PLACEHOLDER) | `base * 0.10` (PLACEHOLDER) |
| House Rent Allowance | `(base + variable) * 0.10` (PLACEHOLDER, Z-class city) | `base * 0.10` (PLACEHOLDER) |
| Campus EPF | `min(B + AGP + DA, 15000) * 0.12` | `min(B + DA, 15000) * 0.12` |
| Campus PT | disabled: amount 0, Condition `0 and "TODO ..."` | same |

`base` and `variable` are the Base and Variable of the Salary Structure Assignment. Earnings use them rather than
component abbreviations because HRMS's Basic and HRA depend on payment days, and HRMS refuses a payment-days
formula that also reads another payment-days component. Each earning is prorated once for leave without pay.
Campus EPF does not depend on payment days and reads the prorated Basic, AGP and DA, so the 15,000 ceiling applies
to the wage actually earned (15 of 30 days on base 30,000: PF on 16,500 capped at 15,000 = 1,800, not 900).

**DA and AGP (6th vs 7th CPC).** Under the 7th CPC pay matrix there is no AGP: put the matrix pay in Base, leave
Variable 0, and set DA to the current central/state rate on Basic. Under the 6th CPC (pay band + AGP), put the band
pay in Base and the AGP in Variable; DA and HRA are then on band pay + AGP. Choose one per structure.

**HRA.** 7th CPC HRA is 30% / 20% / 10% of Basic for X / Y / Z class cities once DA reaches 50% (24/16/8% below 25% DA, 27/18/9% from 25%). The
seed uses 10% (Z class); set your city's rate. Self-financing colleges set their own policy.

**PF ceiling and GPF.** PF is capped at 12% of a 15,000 wage by default, the statutory EPF minimum. To deduct on the
full wage (voluntary higher contribution), change the formula to `(B + AGP + DA) * 0.12`. Aided teachers on GPF
(state GPF/CPS instead of EPF): replace the Campus EPF row with your GPF/CPS component and its rate (CPS
is usually 10% of Basic + DA), and keep EPF only for staff who are covered by it.

## Steps for a college
1. **Accounts**: on the Company, Default Payroll Payable Account must be an account of type **Payable** (open
   "Payroll Payable - ABBR" and set Account Type). Set the default account of each Salary Component for the
   company (Salary Component > Accounts) before you submit salary slips; Payroll Entry posts the accrual Journal Entry
   from them.
2. **Fiscal Year and Holiday List**: an active Fiscal Year covering the payroll dates, and a submitted
   **Holiday List Assignment** for the company (or each employee).
3. **Review and submit** the two structures. Keep one active structure per company and funding mode. To revise,
   make a new structure with the same Funding Mode and set the old one's Is Active to No.
4. **Funding Mode** on each employee (Employee > Faculty).
5. **Assign** with `campus.faculty.api.assign_salary_structure(employee, from_date, base, variable=0)` (POST; needs
   permission to create Salary Structure Assignments). It picks the submitted, active structure of the employee's
   own company for their Funding Mode, then creates and submits the assignment. Calling it again for the same
   employee and From Date returns the existing submitted assignment unchanged. It refuses: an unknown employee, a
   Base of zero or less, an employee without a Funding Mode, a mode without a submitted structure, a mode with two
   active structures, an existing draft assignment for that date (submit or delete it), and an existing assignment
   for that date on a structure of another funding mode (cancel it).
6. **Run payroll**: Payroll Entry (company, month, payroll payable account), Get Employees, Submit (creates the
   draft slips), check, then Submit Salary Slips.

## Increments and promotions
An annual increment or a promotion (cadre or designation change) is an HRMS **Employee Promotion**: Promotion Date,
the changed properties, and Revised CTC. On submit HRMS updates the Employee and its CTC. The new pay then takes effect
through a new assignment from the promotion date:
`assign_salary_structure(employee, promotion_date, base=<new pay>, variable=<AGP>)`. Slips from that date use the new
assignment; earlier months keep the old one. The increment rate (for example 3% of Basic, rounded) is the college's
rule under its pay commission; Campus does not compute it, so no increment helper is added.

## PF, ESI and PT: what exists and what does not
india_compliance has no payroll features; HRMS provides the components (HRMS 16 dropped its Provident Fund and
Professional Tax Deductions reports: use the Salary Register, which has a column per component).
- **Provident Fund**: Campus EPF is the deduction row in both structures; the Salary Register lists it in its own
  column and its Component Type stays Provident Fund. HRMS's own Provident Fund component is not used or changed. Not provided: the
  employer's share (EPF 3.67% / EPS 8.33%) and the EPFO ECR file. Add an Employer Contribution component and row if
  you need the employer share on the slip. Aided colleges whose teachers subscribe to GPF can replace the row.
- **Professional Tax**: Campus PT is seeded **disabled**: the row has amount 0 and the Condition
  `0 and "TODO: enter your state Professional Tax slab ..."`, so no PT is deducted until you configure it. PT is a
  state tax with half-yearly slabs (for example Tamil Nadu, half-yearly income above 75,000: 1,250, about 208 a
  month). Replace the Condition with your slab test on the earned wage (for example `B + DA + HRA > 12500`) and set
  the amount, or add one PT row per slab. Not provided: slab tables or half-yearly deduction. Campus PT does not
  depend on payment days, so a configured PT is deducted in full in a month with leave without pay.
- **ESI**: not provided by HRMS or india_compliance (no component, no report). For staff earning up to 21,000 a month,
  add an "Employee State Insurance" deduction (0.75%, Condition on gross) and an employer contribution (3.25%) to
  the structure.
- **Income tax (TDS)**: not part of these templates; use HRMS Income Tax Slab, Payroll Period and an income tax
  component as usual.

## Sites set up by the earlier version
An earlier development version changed HRMS's components once: Component Type on Provident Fund and Professional
Tax, Provident Fund not by payment days and rounded, and rounding on DA and AGP. The patch
`use_campus_statutory_components` moves Campus's **draft** structures to Campus EPF / Campus PT and changes no HRMS or
college component back. It prints what the earlier version changed so you can review those components by hand if
your own structures use them, and lists submitted Campus structures that still use HRMS's Provident Fund or
Professional Tax (they keep working as they are; amend them to Campus EPF / PT when you next revise them).

## Demo and tests
`campus/tests/test_faculty_payroll.py` builds September 2026 payroll for Demo College of Engineering (no PT
configured):
- faculty_1 (Aided, base 40,000, AGP 9,000): gross 78,400, PF 1,800, net 76,600.
- faculty_2 (Self-financing, base 30,000): gross 36,000, PF 1,800, net 34,200. With 15 days' leave without pay:
  gross 18,000, PF 1,800, net 16,200 (ceiling); with 20 days: gross 12,000, PF 1,320, net 10,680.
- faculty_3 (Self-financing, base 12,346): DA 1,235 (rounded), HRA 1,234.6, PF 1,630 (rounded from 1,629.72).
- After an Employee Promotion with a new assignment from 2026-08-01 at base 41,200: faculty_1's September slip is
  gross 80,320, net 78,520; the July slip keeps 78,400 / 76,600.
