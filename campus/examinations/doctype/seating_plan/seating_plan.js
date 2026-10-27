frappe.ui.form.on("Seating Plan", {
	setup(frm) {
		frm.set_query("room", "halls", () => {
			const filters = { oc_room_type: "Exam Hall" };
			if (frm.doc.company) {
				filters.oc_company = frm.doc.company;
			}
			return { filters };
		});
	},
	exam_session(frm) {
		if (frm.doc.exam_session && !frm.doc.company) {
			frappe.db.get_value("Exam Session", frm.doc.exam_session, "company", (r) => {
				if (r && r.company) {
					frm.set_value("company", r.company);
				}
			});
		}
	},
	refresh(frm) {
		if (frm.is_new()) return;
		frm.add_custom_button(__("Generate Seating"), () =>
			frappe
				.xcall("campus.examinations.api.generate_seating", { seating_plan: frm.doc.name })
				.then((seated) => {
					frappe.show_alert({
						message: __("{0} students seated", [seated]),
						indicator: "green",
					});
					frm.reload_doc();
				})
		);
	},
});

frappe.ui.form.on("Seating Plan Hall", {
	room(frm, cdt, cdn) {
		const row = locals[cdt][cdn];
		if (!row.room) {
			frappe.model.set_value(cdt, cdn, "seat_rows", 0);
			frappe.model.set_value(cdt, cdn, "seat_columns", 0);
			frappe.model.set_value(cdt, cdn, "capacity", 0);
			return;
		}
		const target_room = row.room;
		frappe.db.get_value(
			"Room",
			target_room,
			["oc_seat_rows", "oc_seat_columns", "seating_capacity"],
			(r) => {
				const current = locals[cdt] && locals[cdt][cdn];
				if (!current || current.room !== target_room) {
					return;
				}
				if (r) {
					frappe.model.set_value(cdt, cdn, "seat_rows", r.oc_seat_rows || 0);
					frappe.model.set_value(cdt, cdn, "seat_columns", r.oc_seat_columns || 0);
					frappe.model.set_value(
						cdt,
						cdn,
						"capacity",
						parseInt(r.seating_capacity, 10) || 0
					);
				}
			}
		);
	},
});
