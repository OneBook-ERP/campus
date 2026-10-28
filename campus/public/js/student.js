// M05-01 Student form: onboarding completion link and ID card issue (photo + verifiable QR).
// M04-09 Confirm Joining: an admitted student's enrollment, ID, fee invoice and attendance start follow joining.
const CONFIRM_JOINING = "campus.admissions.api.confirm_joining";

function confirm_joining(frm) {
	const dialog = new frappe.ui.Dialog({
		title: __("Confirm Joining"),
		fields: [
			{
				fieldname: "joining_date",
				fieldtype: "Date",
				label: __("Joining Date"),
				reqd: 1,
				default: frappe.datetime.get_today(),
			},
			{
				fieldname: "override_justification",
				fieldtype: "Small Text",
				label: __("Seat Override Justification"),
				description: __(
					"Only when the seat matrix has no free seat or no row and you hold Admission Override."
				),
			},
		],
		primary_action_label: __("Confirm Joining"),
		primary_action(values) {
			frappe
				.call({
					method: CONFIRM_JOINING,
					type: "POST",
					freeze: true,
					args: { applicant: frm.doc.student_applicant, ...values },
				})
				.then((r) => {
					dialog.hide();
					frappe.show_alert({
						message: __("Joined: register no {0}", [r.message.register_no]),
						indicator: "green",
					});
					frm.reload_doc();
				});
		},
	});
	dialog.show();
}

// BUG-07: Clean link-query for Guardian field in guardians child table.
// Prevents upstream education bug where empty/undefined guardian_list produced "Filtered by: Name is not one of ."
function setup_guardians_filter(frm) {
	if (!frm.fields_dict.guardians || !frm.fields_dict.guardians.grid) return;

	const filter_fn = function (doc, cdt, cdn) {
		const already_selected = (doc.guardians || [])
			.filter((d) => (!cdn || d.name !== cdn) && Boolean(d.guardian))
			.map((d) => d.guardian);

		const filters = {};
		if (already_selected.length > 0) {
			filters["name"] = ["not in", already_selected];
		}
		return { filters: filters };
	};

	frm.set_query("guardian", "guardians", filter_fn);
	const grid = frm.fields_dict.guardians.grid;
	if (grid.get_field("guardian")) {
		grid.get_field("guardian").get_query = filter_fn;
	}
}

frappe.ui.form.on("Student", {
	onload(frm) {
		setup_guardians_filter(frm);
	},
	refresh(frm) {
		setup_guardians_filter(frm);
		if (frm.is_new()) return;
		const officer =
			frappe.user.has_role("Admission Officer") || frappe.session.user === "Administrator";
		const eligible_status =
			["Admitted", "Joined"].includes(frm.doc.oc_academic_status) ||
			(!frm.doc.joining_date && frm.doc.student_applicant);
		if (officer && frm.doc.student_applicant && eligible_status) {
			frm.add_custom_button(__("Confirm Joining"), () => confirm_joining(frm));
		}
		frm.add_custom_button(
			__("Student Onboarding"),
			() => {
				frappe.set_route("List", "Student Onboarding", { student: frm.doc.name });
			},
			__("View")
		);
		frm.add_custom_button(
			__("Student 360"),
			() => frappe.set_route("student-360", frm.doc.name),
			__("View")
		);
		frm.add_custom_button(
			__("Correct Record"),
			() => {
				frappe.new_doc("Student Record Correction", { student: frm.doc.name });
			},
			__("Create")
		);
		frm.add_custom_button(
			__("Issue ID Card"),
			() => {
				frappe.call({
					method: "campus.student_services.api.issue_id_card",
					args: { student: frm.doc.name },
					freeze: true,
					callback: () => {
						frappe.set_route("print", "Student", frm.doc.name);
					},
				});
			},
			__("Create")
		);
	},
});

frappe.ui.form.on("Student Guardian", {
	form_render(frm) {
		setup_guardians_filter(frm);
	},
	guardians_add(frm) {
		setup_guardians_filter(frm);
	},
});
