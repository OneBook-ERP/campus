frappe.listview_settings["Curriculum"] = {
	add_fields: ["company", "status"],
	get_indicator(doc) {
		const colors = {
			Draft: "orange",
			Approved: "green",
			Effective: "blue",
			Superseded: "gray",
		};
		const status = doc.status || "Draft";
		return [__(status), colors[status] || "gray", "status,=," + status];
	},
};
