// M04-01 Admit button (the admission approval): the server (campus.admissions.api.admit) checks role, eligibility and
// documents and creates the admitted Student. The joining is confirmed on the Student form (M04-09).
// M04-10 Temporary Clearance: the officer lists pending mandatory documents with a due date and a reason; until then
// the applicant may be admitted and join without them (campus.admissions.api.grant_temporary_clearance).
const ADMIT = "campus.admissions.api.admit";
const CLEARANCE = "campus.admissions.api.grant_temporary_clearance";
const CLEARABLE = ["Applied", "Approved", "Admitted"];

frappe.ui.form.on("Student Applicant", {
	refresh(frm) {
		const allowed =
			frappe.user.has_role("Admission Officer") || frappe.session.user === "Administrator";
		if (frm.is_new() || !allowed) return;
		add_clearance_button(frm);
		if (frm.doc.oc_eligibility_status === "Pending" || !frm.doc.oc_eligibility_status) {
			frm.add_custom_button(__("Check Eligibility"), () => {
				if (frm.is_dirty()) {
					frappe.msgprint(__("Save the applicant before checking eligibility."));
					return;
				}
				frappe.call({
					method: "campus.admissions.api.evaluate_eligibility",
					args: { applicant: frm.doc.name },
					freeze: true,
					callback(r) {
						if (r.message) {
							frm.reload_doc();
						}
					},
				});
			});
		}
		if (frm.doc.application_status !== "Approved") return;
		frm.add_custom_button(__("Admit"), () =>
			frappe.confirm(__("Admit {0}?", [frm.doc.name]), () =>
				frappe
					.call({
						method: ADMIT,
						type: "POST",
						args: { applicant: frm.doc.name },
						freeze: true,
					})
					.then((r) => {
						frappe.show_alert({
							message: __("Admitted as {0}; confirm the joining on the Student", [
								r.message.student,
							]),
							indicator: "green",
						});
						frm.reload_doc();
						frappe.set_route("Form", "Student", r.message.student);
					})
			)
		);
	},
});

function add_clearance_button(frm) {
	const pending = (frm.doc.oc_documents || [])
		.filter(
			(row) =>
				row.mandatory &&
				!(
					row.status === "Verified" ||
					(row.status === "Submitted" && !row.verification_needed)
				)
		)
		.map((row) => row.document_type);
	if (!pending.length || !CLEARABLE.includes(frm.doc.application_status)) return;
	frm.add_custom_button(__("Temporary Clearance"), () => {
		const dialog = new frappe.ui.Dialog({
			title: __("Temporary Clearance"),
			fields: [
				{
					fieldname: "documents",
					fieldtype: "MultiSelectList",
					label: __("Pending Documents"),
					reqd: 1,
					get_data: () => pending.map((value) => ({ value, description: "" })),
				},
				{
					fieldname: "due_date",
					fieldtype: "Date",
					label: __("Due Date"),
					reqd: 1,
					default: frappe.datetime.add_days(frappe.datetime.get_today(), 30),
				},
				{ fieldname: "reason", fieldtype: "Small Text", label: __("Reason"), reqd: 1 },
			],
			primary_action_label: __("Grant"),
			primary_action(values) {
				frappe
					.call({
						method: CLEARANCE,
						type: "POST",
						args: { applicant: frm.doc.name, ...values },
						freeze: true,
					})
					.then(() => {
						dialog.hide();
						frm.reload_doc();
					});
			},
		});
		dialog.show();
	});
}
