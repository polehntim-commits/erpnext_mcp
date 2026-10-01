# Receipt OCR regression fixtures

One file per slip: the OCR text as the phone read it, one line per OCR line. The phone's
tests carry byte-identical copies (`FarmOpsKit/Tests/FarmOpsKitTests/Fixtures/receipts/`).

- `EXR-2026-0018.txt` — The Home Depot, The Dalles, 2026-09-30 (AFB-2026-00029). The logo
  read as "How doers"; the total prints ABOVE its label (`$626.94` then `TOTAL`); the card
  line is `USD$ 626.94`; and a Pro Xtra year-to-date spend of `$1,937.00` sits lower down,
  which Receipt@1's reader took for the total. Expected: amount 626.94, merchant
  "The Home Depot", and no line item made of "SALE SELF CHECKOUT", "USD$" or the spend summary.
  Copied from the record's `ocr_raw_text`, with the HTML anchor tags the MCP rendering
  added removed.
