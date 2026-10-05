// v0.247.0. Signing off a knowledge check is a person's act (decision 45: Desk or phone). It files the
// Employee Training Record; below the course's video minimum it needs a reason.
frappe.ui.form.on("Training Evidence", {
	refresh(frm) {
		if (frm.is_new() || frm.doc.evidence_kind !== "Quiz attempt" || frm.doc.signoff_status !== "Ready for sign-off") return;
		frm.add_custom_button(__("Sign Off"), () => {
			const go = (reason) =>
				frappe.call({
					method: "erpnext_mcp.training_quiz.desk_sign_off",
					args: { name: frm.doc.name, reason: reason || "" },
					freeze: true,
					callback: () => frm.reload_doc(),
				});
			if (frm.doc.video_minimum === "Not met") {
				frappe.prompt({ fieldname: "reason", fieldtype: "Small Text", label: __("Video minimum not met — reason"), reqd: 1 },
					(v) => go(v.reason), __("Sign Off"));
			} else {
				frappe.confirm(__("Sign off this training? The training record is filed with you as reviewer."), () => go());
			}
		});
	},
});
