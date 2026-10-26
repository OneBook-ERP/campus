frappe.ui.form.on("Hall Ticket", {
	setup(frm) {
		frm.set_query("exam_registration", () => ({
			filters: {
				confirmed: 1,
			},
		}));
	},

	exam_registration(frm) {
		if (!frm.doc.exam_registration) {
			frm.set_value("exam_session", "");
			frm.set_value("student", "");
			frm.set_value("student_name", "");
			frm.set_value("program", "");
			frm.set_value("company", "");
			frm.set_value("academic_term", "");
			frm.set_value("photo", "");
			frm.set_value("status", "Issued");
			frm.set_value("withheld_reasons", "");
			frm.clear_table("courses");
			frm.refresh_fields();
			return;
		}

		frappe.call({
			method: "campus.examinations.hall_tickets.get_ticket_preview",
			args: { registration: frm.doc.exam_registration },
			freeze: true,
			freeze_message: __("Fetching Registration Details..."),
			callback(r) {
				if (!r.message) return;
				const values = r.message;
				frm.set_value("exam_session", values.exam_session);
				frm.set_value("student", values.student);
				frm.set_value("student_name", values.student_name);
				frm.set_value("program", values.program);
				frm.set_value("company", values.company);
				frm.set_value("academic_term", values.academic_term);
				frm.set_value("photo", values.photo);
				frm.set_value("status", values.status);
				frm.set_value("withheld_reasons", values.withheld_reasons);

				frm.clear_table("courses");
				(values.courses || []).forEach((row) => {
					frm.add_child("courses", row);
				});
				frm.refresh_fields();
			},
		});
	},
});
