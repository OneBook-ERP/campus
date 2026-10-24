# Admission Officer

**Login (demo):** `admissions@sxcce.example.edu`

**What you do:** take enquiries, turn them into applicants, check documents and seats, admit, and confirm joining when the student reports.

**You see (desk):** Admissions, Student Services (the Student list, where Confirm Joining is), Approvals. You hold the Admission Officer role only (not the office role, Academics User).

## Daily tasks
1. **Review new enquiries:** Admissions > Enquiries > Lead. Filter by status; open one and follow up. The public form `/admission-enquiry` creates Leads for your college.
2. **Convert an enquiry:** open the Lead and click **Convert to Applicant** (set the Programme of interest first). The Student Applicant is created and linked both ways; a second click opens the same applicant (**Open Applicant**). See docs/usage/enquiry-crm.md. For a batch instead: Admissions > Applications > Applicant Upload; **Validate** checks every row before anything is created. (Admissions > Reports > Import Check checks the legacy Data Import files you made, during the pilot data load.)
3. **Check an applicant:** Admissions > Applications > Student Applicant. Pending papers: Admissions > Reports > Pending Documents. Duplicates: Candidate Duplicate Review.
4. **Admit:** open an Approved Student Applicant, click **Admit**, confirm.
5. **Confirm joining:** the admitted applicant becomes a Student. Open the Student (Student Services > Records & Certificates > Student) and click **Confirm Joining**, set the Joining Date. Register number and fee invoice follow.
6. **Watch seats:** Admissions > Reports > Live Seat Matrix, Admission No-Shows, Admissions by Quota and Category.
7. **Late joining or cancellation:** Admissions > Joining > Late Joining Request, Admission Cancellation. Refund rules: Refund Register report.

You cannot see fees, marks or exams: You can read (not change, print or export) the fee invoices and payments that Confirm Joining and a cancellation raise, because ERPNext checks them as you; Fees and Finance is not in your dock.
