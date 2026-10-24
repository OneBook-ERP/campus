# Pilot setup kit (PR-02)

Configures a fresh site for one college in a single re-runnable command, and lists the sheets that load the college's
own master data afterwards.

> **Placeholder fees.** The kit submits Fee Structures with placeholder amounts and never edits a submitted one. Before
> admissions open, cancel and amend each Fee Structure (or edit the profile before the first run). Do not invoice
> students from a site set up with the sample profile.

## 1. Configure the site

```
bench --site <site> migrate
bench --site <site> execute campus.setup.pilot.setup_college --kwargs '{"profile": "sxcce"}'
```

Run `migrate` first: roles, positions and DocTypes come from the app install. The command prints what it created and
updated, plus notes for anything it left alone on purpose. Running it again changes nothing; if you edit the profile
file and run again, only the values that differ are updated.

What it builds, from `campus/setup/profiles/<profile>.json`:

| Step | Records |
|---|---|
| Companies | Trust (group) and the college under it; the college's Institution Profile (segment, status, affiliating university, Examination Mode, principal / COE / IQAC coordinator) |
| Calendar | Academic Years and their Odd / Even Academic Terms |
| Structure | Departments, Programmes (level, degree, duration, semesters, sanctioned intake) |
| Regulation | The regulation (Effective) with its Curriculum, Registration and Attendance Rule, and the Grading Scheme, Exam Rule Set, Classification Rule and CIA templates seeded by `campus.examinations.seed` for that code |
| Admission | Student Categories, Admission Quotas, a Seat Matrix per programme (intake split by quota share, then category share), active admission year and register number pattern (only where blank) |
| Fees | Item defaults of the fee categories for the college, one submitted Fee Structure per programme for the default quota and for each quota with its own amounts |
| Shared services | A Shared Service Agreement per service the trust provides (hostel, transport) with the college as member |
| Approval policies | Certificate Request, Result Ratification, Rejoin Application, Examiner Remuneration Claim (Principal), only for a document type with no policy yet; Fee Refund, Fee Concession and the others come from `migrate` |
| People | One user per role (office, admission officer, faculty, HOD, class advisor, COE, accounts, principal, IQAC, student, guardian) with its roles and Company User Permission; Employee / Instructor records for staff; Position Holders (HOD, Class Advisor, COE, Principal, IQAC); a class group; a pilot Student linked to the student user and a Guardian |

Roles: teachers (faculty, HOD, class advisor), the COE and the admission officer hold only their own roles, never the
office role Academics User. A re-run gives each kit user the roles its spec lists and takes away only a retired role:
today just Academics User, from a user whose spec is a teacher's, the COE's or the admission officer's (`RETIRED_ROLES`
in `campus/setup/people.py`). So re-running the kit on a site set up before this change takes Academics User from those
logins. The Admission Officer role itself carries what admissions work needs (applicants, the Student list for Confirm
Joining, programmes, seat matrices; `OFFICER_RIGHTS` in `campus/admissions/setup.py`) and no invoice or payment read. No other role is ever taken
away: HRMS's Employee, any role added by hand (even one the kit gives other users, such as Mentor for a teacher) and a
role an active position gives all stay. The demo data's staff step (`campus.setup.demo_data.load`, step `staff`) does
the same for the other departments' HODs and faculty.

Dock: Frappe lists every installed app's modules in a desk user's Dock, and hides one only through the user's blocked
modules. So each kit desk user gets a Module Profile from the profile's `module_profiles` (Campus Teaching Staff for
faculty, HOD, class advisor and COE; Campus Office; Campus Accounts; Campus IQAC; Campus Placement; Campus Principal).
A profile names the modules to keep and blocks every other module of the non-campus apps (erpnext, hrms, education,
frappe, india_compliance, ...), worked out from the site on each run (`campus/setup/module_profiles.py`). Only Campus
Accounts keeps one (Accounts); the others see the campus areas alone. Portal users and users outside the kit are not
touched. To let a role keep another module, add it to that profile's `keep` list and re-run the kit.

Teaching rights on Education doctypes (Instructor select on Course Schedule, HOD and Class Advisor read on Student
Attendance) are added once, on the first `migrate` that knows them, as Custom DocPerm rows. After that the kit never
touches them: a right the college removes or adds in the Role Permission Manager stays as the college left it. Note
that once a doctype has Custom DocPerm rows, later Education upgrades to its standard permissions do not reach the site.
The same holds for the Admission Officer's rules (Student Applicant, Program Enrollment, Program, Academic Year, Student Category, Student Admission, Customer and the like; all without export) and for the Student role's Sales Invoice rule, which is narrowed to read and print.

Lab In-charge reads Asset and Asset Maintenance only for assets located in a laboratory of their own college (permission hooks in `campus.labs.access`); a user who also holds another role that reads Asset (Accounts User, Stock User and so on) keeps that role's wider read.

Locked records are never edited on a re-run: an Effective regulation and its rules, and a submitted Fee Structure (a
difference is reported in the notes; amend it by cancelling and re-submitting by hand).

### Passwords

- On a developer-mode or demo site (`campus_demo_site` in the site config) every new user gets the profile's fixed
  TEST password (`test_password`, `Campus@Pilot2026` in `sxcce`). It is public: never use it for real people.
- On any other site no password is stored. The result lists a one-time `reset_links` entry per new user; the user opens
  it and chooses their own password at the first login (the link opens the "password expired" page).
- `test_passwords=true` on a real site is refused (`true`/`false`/`yes`/`no` words are accepted; anything else is refused). Existing users are never touched.
- The `bench execute` output holds the reset links: treat it as a secret. A lost link is not regenerated by the kit; the user uses Forgot Password.
- A production site cloned from a demo site keeps `campus_demo_site`: remove the flag before the clone goes live.
- Users, employees, student and guardian are synthetic (`@<user_domain>`); replace them with real people through the
  import sheets below.

### A second college

Copy `sxcce.json` to a new file in `campus/setup/profiles/` (lower-case letters, digits, `-`, `_`), change the names,
programmes, seats, fees and users, and run `setup_college` with its name. The file is checked before anything is written:
an unknown programme, department, quota, category, company token, regulation code without seeded exam rules, a missing
college, or a role that does not exist on the site stops the run with every problem listed.

### Values to confirm with the college

The profile ships synthetic placeholders; the regulation and rule values are the seeded ones (pending values stay as
listed in `docs/rulings.md`). Confirm these before the pilot:

| Item | Profile value |
|---|---|
| Programme intake | 60 for every programme |
| Quota shares | Government 65%, Management 35% |
| Category split of the Government quota | OC 31, BC 30, MBC 20, SC 18, ST 1 (Tamil Nadu community reservation, grouped), Management quota all OC |
| Fee amounts | Placeholders per quota (Tuition, Exam, Development, Library, Lab, Caution Deposit) |
| Autonomy date | Not set (the profile does not know it) |
| Rule values | Rs 500 condonation fee, OD / medical leave counted, no per-course attendance minimum, Honours semester 3 and no arrears, registration windows and caps: see `docs/rulings.md` (PR-02 table, items 6-11) |

## 2. Load the college's own master data

Use the Data Import templates of `campus.admissions.legacy_import` (Admissions > Reports > Import Check validates a file before
you import it). Download a blank template (CSV or Excel) with
`/api/method/campus.admissions.legacy_import.download_import_template?template=<name>`; the header labels are the
importer's own. A child row (a second guardian, a second fee component, a second curriculum course) goes on the next
line with the parent cells blank. Load in this order:

| Order | Template | DocType | Columns (required in bold) |
|---|---|---|---|
| 1 | Programs | Program | **Program Name**, Abbreviation, Department, **Company**, Level, Degree, Discipline, Duration (Years), Total Semesters, Sanctioned Intake |
| 2 | Courses | Course | **Course Name**, Department, **Company**, Course Code, Course Type, Default Lecture / Tutorial / Practical Periods, Default Credits |
| 3 | Employees | Employee (faculty) | **First Name**, **Gender**, **Date of Birth**, **Date of Joining**, **Company**, **Status**, Employee Number, Last Name, Department, Designation, Mobile, Personal Email |
| 4 | Instructors | Instructor | **Instructor Name**, **Employee**, Department, Status, Gender |
| 5 | Curriculum Rows | Curriculum + courses | **Program**, **Regulation**, Version, **Status**, **Company**, then per course line: **Semester**, **Course**, **Category**, Credits, Is Elective, Exam Type, CIA Max, ESE Max |
| 6 | Fee Structures | Fee Structure | **Company**, **Program**, **Academic Year**, Admission Quota (blank: the default structure), **Receivable Account**, **Cost Center**, then per component line: **Fees Category**, **Amount** |
| 7 | Students | Student (existing students) | **Register No**, **First Name**, Middle / Last Name, **Email**, Mobile, Date of Birth, Gender, Joining Date, **Company**, Batch, **Regulation**, Aadhaar Number, address columns, then guardian lines: **Guardian**, Guardian Name, Relation |
| 8 | Student Enrolments | Program Enrollment | **Student** (the Student ID from step 7), **Program**, **Academic Year**, **Category**, **Admission Quota**, **Company**, Enrolment Date |
| 9 | Opening Fee Balances | Sales Invoice (opening) | see `legacy_import.py` |

Course Offerings are not imported: they are generated from an Effective curriculum (Curriculum > Generate Offerings).
New applicants (not existing students) load through Admissions > Applications > Applicant Upload (`docs/usage/applicant-upload.md`).

A file whose Link cells name a master that does not exist is refused whole by Data Import: load the masters in the order
above, and check each sheet first with the Import Check report.

## Staging site for the SXCCE demo and intern UAT

The staging site is `campus.localhost`, served on its own port so it does not depend on the bench's default site:

```bash
cd /workspace/development/education-bench
bench --site campus.localhost serve --port 8005
```

Open http://campus.localhost:8005. Staff log in to the desk, students and guardians land on `/campus-portal`. Logins and the demo walkthrough are in `docs/usage/demo-data.md`; the intern UAT scripts are in `docs/usage/uat/`.

The site was built with developer mode, telemetry and the request recorder off and `campus_demo_site` on (test passwords and the UAT Tester role are allowed only there). To rebuild it from scratch: create the site with all apps, then run `campus.setup.pilot.setup_college` with `{"profile": "sxcce"}` and `campus.setup.demo_data.load` with `{"profile": "sxcce", "seed": 2026}` (about 10 minutes). After code changes, run `bench --site campus.localhost migrate`, re-run `setup_college`, and re-run the demo `staff` step.
