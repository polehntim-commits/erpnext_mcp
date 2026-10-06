"""Form 940 and W-3 — the payroll filing set completed. v0.259.0."""

from erpnext_mcp import form_generators, form_pdf_renderer as renderer

from .fixtures import MAIN
from .test_tax_form_pdfs import NEEDS_REPORTLAB, text_of
from .test_tax_forms import TaxFormToolTestCase, or_slip

ON = {"allow_generate_tax_form": 1, "allow_get_futa_summary": 1, "allow_render_tax_form_pdf": 1,
      "allow_get_tax_form": 1}


class W3Arithmetic(TaxFormToolTestCase):
	def test_the_transmittal_is_the_w2s_added_up(self):
		slips = [or_slip("E1", 1000.0), or_slip("E1", 2000.0, period_end="2025-06-13"), or_slip("E2", 500.0)]
		w3 = form_generators.generate_w3_data(slips, {"name": MAIN, "ein": "12-3456789"}, 2025)
		w2s = [form_generators.generate_w2_data({"employee": e}, [s for s in slips if s["employee"] == e],
		                                        {"name": MAIN}, 2025) for e in ("E1", "E2")]
		self.assertEqual(w3["box_c_total_w2_forms"], 2)
		for box in ("box1_wages", "box2_federal_income_tax_withheld", "box3_social_security_wages",
		            "box4_social_security_tax_withheld", "box5_medicare_wages", "box6_medicare_tax_withheld"):
			self.assertAlmostEqual(w3[box], sum(w2[box] for w2 in w2s), places=2, msg=box)
		self.assertEqual(w3["box1_wages"], 3500.0)
		self.assertEqual([s["box15_state"] for s in w3["state_boxes"]], ["OR"])
		self.assertEqual(w3["state_boxes"][0]["box16_state_wages"], 3500.0)

	def test_the_wage_base_is_capped_per_person_before_totalling(self):
		slips = [or_slip("E1", 180000.0), or_slip("E2", 1000.0)]
		w3 = form_generators.generate_w3_data(slips, {"name": MAIN, "ss_wage_base": 176100.0}, 2025)
		self.assertEqual(w3["box3_social_security_wages"], 177100.0)
		self.assertEqual(w3["box5_medicare_wages"], 181000.0)

	def test_an_agricultural_filer_ticks_943_and_941_says_so(self):
		w3 = form_generators.generate_w3_data([or_slip()], {"name": MAIN}, 2025)
		self.assertEqual(w3["box_b_kind_of_payer"], "941")
		self.assertTrue(any("943" in w for w in w3["warnings"]))
		farm = form_generators.generate_w3_data([or_slip()], {"name": MAIN, "kind_of_payer": "943"}, 2025)
		self.assertEqual(farm["box_b_kind_of_payer"], "943")
		self.assertFalse(any("ticks 943" in w for w in farm["warnings"]))


class TheRecordedForms(TaxFormToolTestCase):
	def setUp(self):
		super().setUp()
		self.configure(enabled=1, **ON)

	def test_a_w3_is_generated_recorded_and_rendered(self):
		self.seed_a_year("HR-EMP-00001")
		data = self.tool_data("generate_tax_form", {"form_type": "W-3", "company": MAIN, "fiscal_year": "2025",
		                                            "kind_of_payer": "943"})
		self.assertEqual((data["status"], data["form_data"]["box_c_total_w2_forms"]), ("Generated", 1))
		self.assertEqual(data["form_data"]["box1_wages"], 4000.0)
		self.assertEqual(data["form_data"]["box_b_kind_of_payer"], "943")
		self.assertIn("already exists", self.tool_error("generate_tax_form", {"form_type": "W-3", "company": MAIN,
		                                                                      "fiscal_year": "2025"}))
		if renderer.available():
			pdf = self.tool_data("render_tax_form_pdf", {"name": data["name"]})
			self.assertTrue(pdf.get("file_url") or pdf.get("file") or pdf.get("name"))

	def test_a_940_is_the_same_walk_as_the_futa_summary(self):
		self.seed_a_year("HR-EMP-00001")
		recorded = self.tool_data("generate_tax_form", {"form_type": "940", "company": MAIN, "fiscal_year": "2025"})
		summary = self.tool_data("get_futa_summary", {"company": MAIN, "fiscal_year": "2025"})["form_940"]
		for line in ("line3_total_payments", "line7_total_taxable_futa_wages", "line12_total_futa_tax"):
			self.assertEqual(recorded["form_data"][line], summary[line], line)
		self.assertIn("quarter", self.tool_error("generate_tax_form", {"form_type": "940", "company": MAIN,
		                                                               "fiscal_year": "2026", "quarter": "Q1"}))


class ThePages(TaxFormToolTestCase):
	@NEEDS_REPORTLAB
	def test_both_pages_render_with_the_disclaimer_and_their_filing_channel(self):
		w3 = form_generators.generate_w3_data([or_slip("E1", 1000.0)], {"name": MAIN, "ein": "12-3456789"}, 2025)
		f940 = form_generators.generate_form_data("940", [or_slip("E1", 9000.0)], {"name": MAIN, "ein": "12-3456789"}, 2025)
		for form_type, data, needle in (("W-3", w3, "Business Services Online"), ("940", f940, "EFTPS")):
			with self.subTest(form=form_type):
				text = text_of(renderer.render_form_pdf(form_type, data, {"name": MAIN}))
				self.assertIn("WORKING COPY - NOT AN OFFICIAL FORM", text)
				self.assertIn(needle, text)
		self.assertIn("Transmittal of Wage and Tax Statements", text_of(renderer.render_form_pdf("W-3", w3, {})))

	def test_the_form_types_are_known_everywhere(self):
		for form_type in ("940", "W-3"):
			self.assertIn(form_type, form_generators.FORM_TYPES)
			self.assertIn(form_type, renderer.RENDERERS)
			self.assertIn(form_type, renderer.FILING_CHANNEL)
