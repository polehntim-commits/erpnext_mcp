// SPDX-License-Identifier: MIT
// Lot Line Adjustment — the Record Survey button. v0.169.0.
//
// Shown only where the server would allow it: a submitted adjustment at status
// Recorded, whose survey has not been recorded yet, to a System Manager. The
// server checks all of that again (`land_adjustment.survey_refusals` and the
// role gate in `tools/land.py`); hiding the button is courtesy, not the control.
frappe.ui.form.on("Lot Line Adjustment", {
	refresh(frm) {
		const ready =
			frm.doc.docstatus === 1 &&
			frm.doc.status === "Recorded" &&
			!frm.doc.survey_recorded_on &&
			frappe.user.has_role("System Manager");
		if (!ready) {
			return;
		}
		frm.add_custom_button(__("Record Survey"), () => {
			frappe.confirm(
				__(
					"Write the adjusted Parcel records from this survey? This is the step that puts the adjustment on the operating books."
				),
				() => {
					frm.call("record_survey").then((r) => {
						frm.reload_doc();
						const parcels = ((r.message && r.message.survey) || [])
							.filter((entry) => entry.parcel)
							.map((entry) => `${entry.parcel} (${entry.acres_after} ac)`);
						frappe.show_alert({
							message: __("Survey recorded: {0}", [parcels.join(", ")]),
							indicator: "green",
						});
					});
				}
			);
		}).addClass("btn-primary");
	},
});
