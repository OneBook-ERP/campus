frappe.ui.form.on("Student Onboarding", {
	student(frm) {
		const current_student = frm.doc.student;
		const current_template = frm.doc.template;
		if (!current_student || (frm.doc.items && frm.doc.items.length)) {
			return;
		}
		frappe.call({
			method: "campus.student_services.doctype.student_onboarding.student_onboarding.get_onboarding_template_details",
			args: { student: current_student, template: current_template },
			callback(r) {
				if (
					frm.doc.student !== current_student ||
					frm.doc.template !== current_template ||
					(frm.doc.items && frm.doc.items.length)
				) {
					return;
				}
				if (r.message) {
					if (!frm.doc.template && r.message.template) {
						frm.set_value("template", r.message.template);
					}
					if (r.message.items) {
						populate_items(frm, r.message.items);
					}
				}
			},
		});
	},

	template(frm) {
		const current_student = frm.doc.student;
		const current_template = frm.doc.template;
		if (!current_template || (frm.doc.items && frm.doc.items.length)) {
			return;
		}
		frappe.call({
			method: "campus.student_services.doctype.student_onboarding.student_onboarding.get_onboarding_template_details",
			args: { student: current_student, template: current_template },
			callback(r) {
				if (
					frm.doc.student !== current_student ||
					frm.doc.template !== current_template ||
					(frm.doc.items && frm.doc.items.length)
				) {
					return;
				}
				if (r.message && r.message.items) {
					populate_items(frm, r.message.items);
				}
			},
		});
	},
});

function populate_items(frm, items) {
	if (!items || !items.length) return;
	frm.clear_table("items");
	items.forEach((row) => {
		let child = frm.add_child("items");
		child.item_code = row.item_code;
		child.title = row.title;
		child.mandatory = row.mandatory;
		child.requires_attachment = row.requires_attachment;
		child.status = "Pending";
	});
	frm.refresh_field("items");
}
