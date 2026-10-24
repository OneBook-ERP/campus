# UAT: Accounts
Login `accounts@sxcce.example.edu`. Guide: roles/accounts.md.

| # | Step | Expected |
|---|---|---|
| 1 | Fees and Finance > Billing > Student Invoices | Paid, part paid and outstanding invoices |
| 2 | Open an outstanding invoice, **Create > Payment**, submit | Payment Entry submitted; invoice shows paid |
| 3 | Check the student's `/campus-portal/fees` as `student@` or the invoice's student | Outstanding reduced |
| 4 | Fees and Finance > Reports > Student Fee Ledger, pick the student of step 2 | The payment shows as a credit; the balance is reduced |
| 5 | Scholarship Application waiting for sanction | Visible; sanction is by the Principal |
| N1 | Open Examinations, Admissions | Not visible |
| N2 | Cancel a Fee Structure that has invoices | Refused or warned |
