# The chosen card artwork (Tim, 2026-10-01)

Golden references for `erpnext_mcp/card_art.py` (docs/design/card_print_queue.md, Amendment 3).

- `OML_EmployeeID_Test_OML-0001_back-rotCW.pdf` — the employee ID Tim chose: both pages
  landscape CR80 (85.6 × 54 mm), the back's portrait design turned 90° clockwise.
- `OML_AssetTag_v2_40-WM-SE_A_standard_back-rotCW.pdf` — the asset tag in the same format.

`test_card_art.py` renders the same sample data and compares, against these files directly,
the page sizes, every text item's position, font and size, and the boxes of the QR codes,
the photo slot and the colour bar.
