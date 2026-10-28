// M03-02 CO-PO Matrix: Fill Grid adds a 0 cell for each CO x PO / PSO pair not in the grid yet.
frappe.ui.form.on("CO-PO Matrix", {
	refresh(frm) {
		frm.set_df_property("correlations", "cannot_add_rows", true);
		frm.set_df_property("correlations", "cannot_delete_rows", true);
		if (frm.doc.docstatus === 0 && !frm.is_new()) {
			frm.add_custom_button(__("Fill Grid"), () =>
				frm.call("fill_grid").then(() => frm.refresh_field("correlations"))
			);
		}
	},
});
