# SPDX-License-Identifier: MIT
"""The live budget. v0.274.0 (docs/contracts/live_budget_v0_274.yaml).

A made-up pro forma in the real one's shape (never the real numbers) imported as a DRAFT plan — lines, accounts and
cost centres from the CoA Mapping, acres, the spray program; published; then budget / actual / committed / forecast
from the ledger, open POs and Material Requests; 90% / 100% alerts once each; spray materials still ahead; a request
netted against the shed; ERPNext Budgets drafted with Warn; spray cost per block; off until switched on.
"""

import io
import json
import zipfile
from xml.sax.saxutils import escape

import frappe

from erpnext_mcp import config_lifecycle, flags, live_budget, phone_config, receiving, xlsx_lite

from .fixtures import MAIN, V12TestCase
from .harness import STORE, register_doctype

CHEM, FERT = "5310 - Chemicals & Crop Protection - CF", "5320 - Fertilizer & Soil - CF"
CP, PC = "120 - Crop Protection - CF", "110 - Perennial Care - CF"
SHED = "Chemical Shed - ETC"


def workbook(sheets: dict) -> bytes:
	"""A minimal .xlsx: inline strings and numbers, one XML part per sheet."""
	buffer = io.BytesIO()
	with zipfile.ZipFile(buffer, "w") as z:
		names = list(sheets)
		z.writestr("xl/workbook.xml", '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
		           'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>'
		           + "".join(f'<sheet name="{escape(n)}" sheetId="{i + 1}" r:id="rId{i + 1}"/>' for i, n in enumerate(names))
		           + "</sheets></workbook>")
		z.writestr("xl/_rels/workbook.xml.rels", '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/'
		           'relationships">' + "".join(f'<Relationship Id="rId{i + 1}" Target="worksheets/sheet{i + 1}.xml"/>'
		                                       for i in range(len(names))) + "</Relationships>")
		for i, name in enumerate(names):
			rows = []
			for r, row in enumerate(sheets[name], start=1):
				cells = []
				for c, value in enumerate(row):
					ref = f"{chr(65 + c)}{r}"
					if value is None:
						continue
					if isinstance(value, (int, float)):
						cells.append(f'<c r="{ref}"><v>{value}</v></c>')
					else:
						cells.append(f'<c r="{ref}" t="inlineStr"><is><t>{escape(str(value))}</t></is></c>')
				rows.append(f'<row r="{r}">{"".join(cells)}</row>')
			z.writestr(f"xl/worksheets/sheet{i + 1}.xml", '<worksheet xmlns="http://schemas.openxmlformats.org/'
			           f'spreadsheetml/2006/main"><sheetData>{"".join(rows)}</sheetData></worksheet>')
	return buffer.getvalue()


PRO_FORMA = {
	"Growing Budget": [
		["Growing budget — test"], [],
		["Code", "Budget line", "Basis", "Benchmark", "Used 2027 $/yr → P&L"],
		["CROP PROTECTION"],
		["cp_fung", "Airblast fungicide", "per year", 100, 12000],
		["cp_ins", "Airblast insecticide", "per year", 100, 6000],
		["FERTILITY"],
		["fert_soil", "Fertilizer — soil", "per cherry acre", 110, 4000],
		["power", "Power — camp", "per year", 3600, 1200],
	],
	"CoA Mapping": [
		["Model → ERPNext chart"], [],
		["Sheet", "Model line", "ERPNext account (CF)", "Cost center (CF)", "Status"],
		["Growing Budget", "Airblast fungicide", CHEM, CP, "In draft CoA"],
		["Growing Budget", "Airblast insecticide", CHEM, CP, "In draft CoA"],
		["Growing Budget", "Fertilizer — soil", FERT, PC, "In draft CoA"],
		["Growing Budget", "Power — camp", "Template indirect: Utilities", "310 - G and A - CF", "GAP"],
	],
	"Cost per Acre": [
		["Growing cost per acre"], [],
		["Acres", 10, 5],
		["2027 $ per cherry acre", "Early varieties", "Skeena", "All cherry acres"],
	],
	"Spray Program": [
		["Airblast spray program"], [],
		["#", "Timing", "Target", "Fungicide", "Insecticide", "Bactericide (copper)", "Growth regulator (GA3)",
		 "Dormant oil", "Total", "Early", "Skeena"],
		[1, "Delayed dormant", "Scale", 0, 0, 35, 0, 25, 60, 1, 1],
		[2, "Bloom", "Brown rot", 110, 0, 0, 0, 0, 110, 1, 1],
		[3, "Straw color", "Firmness", 90, 0, 0, 110, 0, 200, 0, 1],
		[4, "Pre-harvest", "SWD", 120, 100, 0, 0, 0, 220, 1, 1],
		["Applications per year", 3, 4],
	],
}


class BudgetCase(V12TestCase):
	def setUp(self):
		super().setUp()
		self.configure(enabled=1)
		STORE.seed("Account", [{"name": a, "account_name": a, "company": MAIN, "is_group": 0} for a in (CHEM, FERT)])
		STORE.seed("Cost Center", [{"name": c, "cost_center_name": c, "company": MAIN, "is_group": 0} for c in (CP, PC)])
		STORE.commit()

	def imported(self, publish=True):
		out = live_budget.import_pro_forma(workbook(PRO_FORMA), MAIN, 2027, actor="tim", source="test.xlsx")
		if publish:
			with config_lifecycle.desk_action():
				phone_config.publish(live_budget.KIND, out["plan"], out["version"], "test", "Administrator")
		STORE.commit()
		return out

	def turn_on(self):
		flags.upsert(live_budget.FLAG, "Flag", True, company=MAIN, description="t", owner_area="budget", active=True)
		STORE.commit()

	def gl(self, account, centre, amount, day="2027-04-10"):
		STORE.seed("GL Entry", [{"name": f"GL-{len(STORE.rows('GL Entry')) + 1}", "account": account,
		                         "cost_center": centre, "debit": amount, "credit": 0, "company": MAIN,
		                         "is_cancelled": 0, "posting_date": day, "voucher_type": "Purchase Invoice",
		                         "voucher_no": "PINV-1"}])
		STORE.commit()


class TheReader(BudgetCase):
	def test_values_by_sheet_and_refusals(self):
		book = xlsx_lite.open_workbook(workbook({"A": [["x", 1, 2.5], [None, "y"]]}))
		self.assertEqual(book["A"], [["x", 1, 2.5], [None, "y"]])
		with self.assertRaises(xlsx_lite.XlsxError):
			xlsx_lite.open_workbook(b"not a zip")
		self.assertEqual(xlsx_lite.as_date(46388), "2027-01-01")


class TheImport(BudgetCase):
	def test_a_draft_plan_with_accounts_acres_and_the_program(self):
		out = self.imported(publish=False)
		self.assertEqual((out["status"], out["lines"], out["annual_total"]), ("Draft", 4, 23200))
		self.assertEqual(out["unmapped"], ["power"], "a GAP in the CoA Mapping is listed, not guessed")
		self.assertEqual(out["acres"], {"Early": 10, "Skeena": 5})
		self.assertEqual(out["spray_applications"], 4)
		with self.assertRaisesRegex(live_budget.BudgetError, "no published Input Plan"):
			live_budget.plan(MAIN, 2027)
		with self.assertRaisesRegex(live_budget.BudgetError, "Growing Budget"):
			live_budget.import_pro_forma(workbook({"Cover": [["x"]]}), MAIN, 2027)


class TheLiveView(BudgetCase):
	def test_budget_actual_committed_forecast_and_levels(self):
		self.imported()
		self.gl(CHEM, CP, 15000)
		self.gl(FERT, PC, 500)
		self.gl("5999 - Something unplanned - CF", PC, 75)
		view = live_budget.live(MAIN, 2027, "2027-06-30")
		chem = next(r for r in view["rows"] if r["account"] == CHEM)
		self.assertEqual((chem["budget"], chem["actual"], chem["level"]), (18000, 15000, "ok"))
		self.assertEqual(chem["budget_to_date"], 9000, "even twelfths by default: six months of 18,000")
		self.assertEqual(chem["forecast"], 15000 + 9000)
		self.assertEqual(view["rows"][0]["account"], CHEM)
		self.gl(CHEM, CP, 1500)
		chem = next(r for r in live_budget.live(MAIN, 2027, "2027-06-30")["rows"] if r["account"] == CHEM)
		self.assertEqual((chem["used_pct"], chem["level"]), (91.7, "warn"))
		self.assertEqual([u["account"] for u in view["unplanned"]], ["5999 - Something unplanned - CF"])

	def test_alerts_once_per_level_and_only_when_on(self):
		self.imported()
		self.gl(CHEM, CP, 18500)
		self.assertEqual(live_budget.daily("2027-06-30"), {})
		self.turn_on()
		live_budget.daily("2027-06-30")
		live_budget.daily("2027-06-30")
		alerts = [a for a in STORE.rows("Compliance Alert") if str(a.get("alert_type")).startswith("budget_")]
		self.assertEqual([(a["alert_type"], a["severity"]) for a in alerts], [("budget_over", "Warning")])
		self.assertIn("102.8%", alerts[0]["alert_message"])


class TheSprayProgram(BudgetCase):
	def test_dollars_still_ahead_by_month_and_category(self):
		self.imported()
		ahead = live_budget.forecast_needs(MAIN, 2027, "2027-05-15")
		self.assertEqual({(a["month"], a["category"], a["dollars"]) for a in ahead["applications"]},
		                 {("2027-06", "Fungicide", 450.0), ("2027-06", "Growth regulator (GA3)", 550.0),
		                  ("2027-06", "Fungicide", 1800.0), ("2027-06", "Insecticide", 1500.0)})
		self.assertEqual(ahead["by_category"]["Fungicide"], 2250.0)


class AskingForInputs(BudgetCase):
	def setUp(self):
		super().setUp()
		register_doctype("Material Request", [{"fieldname": f, "fieldtype": "Data"} for f in
		                                      ("company", "material_request_type", "transaction_date", "schedule_date",
		                                       "status", "docstatus")] + [{"fieldname": "items", "fieldtype": "Table",
		                                                                   "options": "Material Request Item"}])
		register_doctype("Material Request Item", [{"fieldname": f, "fieldtype": "Data"} for f in
		                                           ("item_code", "qty", "schedule_date", "warehouse", "ordered_qty",
		                                            "expense_account", "cost_center")])
		STORE.seed("Warehouse", [{"name": SHED, "warehouse_name": "Chemical Shed", "company": MAIN}])
		STORE.seed("Item", [{"name": "CAPTAN-80", "item_code": "CAPTAN-80", "item_name": "Captan 80", "stock_uom": "Lb",
		                     "is_stock_item": 1, "valuation_rate": 6.0, "reorder_levels": [],
		                     "item_defaults": [{"company": MAIN, "expense_account": CHEM}]}])
		STORE.seed("Bin", [{"name": "BIN-1", "item_code": "CAPTAN-80", "warehouse": SHED, "actual_qty": 30}])
		flags.upsert(receiving.WAREHOUSE_FLAG, "Text", SHED, company=MAIN, description="t", owner_area="r", active=True)
		STORE.commit()

	def test_netted_valued_placed_and_flagged(self):
		self.imported()
		with self.assertRaisesRegex(live_budget.BudgetError, "off for"):
			live_budget.request_inputs(MAIN, [{"item_code": "CAPTAN-80", "qty": 100}])
		self.turn_on()
		self.assertIsNone(live_budget.request_inputs(MAIN, [{"item_code": "CAPTAN-80", "qty": 20}],
		                                             year=2027)["material_request"], "the shed holds it")
		out = live_budget.request_inputs(MAIN, [{"item_code": "CAPTAN-80", "qty": 1000}], needed_by="2027-03-01",
		                                 year=2027)
		self.assertEqual(out["netted"][0]["to_order"], 970)
		self.assertEqual(out["estimated_value"], 5820)
		self.assertTrue(out["needs_approval"])
		self.assertEqual(out["budget"][0]["account"], CHEM)
		self.assertEqual(out["budget"][0]["left_in_budget"], 18000)
		STORE.commit()
		view = live_budget.live(MAIN, 2027, "2027-03-01")
		self.assertEqual(next(r for r in view["rows"] if r["account"] == CHEM)["committed"], 5820)


class ERPNextBudget(BudgetCase):
	def test_drafts_with_warn_and_skips_what_the_site_lacks(self):
		register_doctype("Budget", [{"fieldname": f, "fieldtype": "Data"} for f in
		                            ("company", "budget_against", "cost_center", "fiscal_year",
		                             "action_if_annual_budget_exceeded", "action_if_accumulated_monthly_budget_exceeded")]
		                 + [{"fieldname": "accounts", "fieldtype": "Table", "options": "Budget Account"}])
		register_doctype("Budget Account", [{"fieldname": "account", "fieldtype": "Data"},
		                                    {"fieldname": "budget_amount", "fieldtype": "Data"}])
		self.imported()
		self.turn_on()
		out = live_budget.draft_erpnext_budget(MAIN, 2027)
		self.assertEqual(len(out["budgets"]), 2)
		doc = frappe.get_doc("Budget", out["budgets"][0])
		self.assertEqual(doc.get("action_if_annual_budget_exceeded"), "Warn")
		self.assertEqual(out["skipped"], [], "power has no account, so no centre is attempted for it")


class CostPerBlock(BudgetCase):
	def test_spray_materials_per_block(self):
		STORE.seed("Item", [{"name": "CAPTAN-80", "item_code": "CAPTAN-80", "item_name": "Captan", "stock_uom": "Lb",
		                     "valuation_rate": 6.0, "item_defaults": [], "reorder_levels": []}])
		STORE.seed("Spray Application", [{"name": "SPRAY-1", "company": MAIN, "completed_at": "2027-05-01 08:00:00",
		                                  "products_applied": json.dumps([{"item": "CAPTAN-80", "rate_per_acre": 4}]),
		                                  "blocks": [{"block": "Block 1", "acres": 10}]}])
		out = live_budget.input_cost_by_block(MAIN, 2027)
		self.assertEqual(out["blocks"][0]["cost"], 240.0)
		self.assertEqual(out["blocks"][0]["per_acre"], 24.0)
