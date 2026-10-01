# Approved card artwork (Tim, 2026-10-01)

Golden references for `erpnext_mcp/card_art.py` (docs/design/card_print_queue.md, Amendment 1 §A2).

- `OML_AssetTag_Test_40-WM-SE_A_standard.pdf` — the asset tag Tim approved (front landscape, back portrait).
- `OML_EmployeeID_Test_OML-0001_portrait-back.pdf` — the employee ID he approved.
- `approved_geometry.json` — page sizes and every text item's position, font and size, read off
  those two files **and** their `_back-rotCW` / `_back-rotCCW` variants. The four rotated PDFs are
  not copied (2.4 MB each, the same drawing turned 90°); their geometry is what the tests need.

`test_card_art.py` renders the same sample data and compares page sizes and text positions.
