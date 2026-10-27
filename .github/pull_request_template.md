## Summary

<!-- What changes and why. Link the issue: Fixes #123 -->

## Acceptance criteria

<!-- Copy the checklist from the issue's triage comment and tick what this PR meets. -->

- [ ]

## Checklist

- [ ] Targets `develop`; commit subjects follow `type(scope): subject`
- [ ] Tests added or updated, run on a `campus-test*` site (never a dev or pilot site)
- [ ] Permission checks on every new whitelisted method; no `ignore_permissions` on user-chosen records without a check
- [ ] Records stay within their college (company scope)
- [ ] Patches are idempotent and only touch rows campus wrote; listed in `patches.txt`
- [ ] Upstream apps (frappe, erpnext, hrms, education) are not edited
- [ ] Docs updated (`docs/usage/`) if a user-visible flow changed
