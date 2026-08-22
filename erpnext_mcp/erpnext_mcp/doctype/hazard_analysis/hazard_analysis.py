# SPDX-License-Identifier: MIT
"""Controller for Hazard Analysis — per-process-step hazard identification.

Each Hazard Analysis record identifies one hazard at one process step within
a Food Safety Plan. The controller computes a risk level from the likelihood
and severity selections using a standard risk matrix, so the QI does not have
to remember the mapping and the resulting risk level is always consistent.
"""

from frappe.model.document import Document

#: Likelihood x Severity -> Risk Level matrix.
#: Keys are (likelihood_lower, severity_lower) tuples.
RISK_MATRIX = {
	("remote", "minor"): "Low",
	("remote", "major"): "Low",
	("remote", "critical"): "Medium",
	("low", "minor"): "Low",
	("low", "major"): "Medium",
	("low", "critical"): "High",
	("moderate", "minor"): "Medium",
	("moderate", "major"): "High",
	("moderate", "critical"): "High",
	("high", "minor"): "High",
	("high", "major"): "High",
	("high", "critical"): "Critical",
}


class HazardAnalysis(Document):
	def validate(self):
		if self.likelihood and self.severity:
			key = (str(self.likelihood).strip().lower(), str(self.severity).strip().lower())
			self.risk_level = RISK_MATRIX.get(key, "")
