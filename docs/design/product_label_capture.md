# Product label capture, validation and the EPA label: contract (frozen for v0.201.0)

Source: Tim, on PROWLER™ (UPC-A 048745228174, EPA Reg. No. 12455-97-3240):

> "it looks like we are not using the workflow we were before for the document validation list
> for prowler … we are not getting the full results we were. Also the PDF is not getting
> attached to the item." · "For some reason when we added the label it is not storing." ·
> "Its not a Crop Protection Product is a control product for field mice etc."

What the code and OML's audit log show:

- **The label was never stored, on any path.**
  - *Sep 27, UPC → New product → "Read the label":* the flow ran OCR and the on-device model,
    then **threw the photos away**. The only server calls were `find_item_by_barcode` and
    `create_item`. There was no upload, no `validate_document` and no attach.
  - *Sep 24, Inventory → "Scan a pesticide label":* four `validate_document` calls with **OCR text
    only**. The image payload was built but never passed, and `source_name` was left empty on
    purpose. The results are DVAL-2026-0001..0004: no Item, no scan, no model. DVAL-0001 *is*
    PROWLER.
  - The server couldn't have done better anyway. `finalize_staged_file` commits files
    unattached, Item is not phone-attachable, and `Item.label_scan_validation` can't be set from
    a phone.
- **Thin extraction.**
  - OCR lines are ordered by height only, so the columns of a curved, multi-column tub
    interleave.
  - The EPA-number pattern needs the digits to follow the caption directly.
  - The model is not asked for use sites, target pests, storage/disposal or PPE.
- **0.05 confidence.**
  - The pesticide rules score a missing REI, PHI and PHI crop as three ERRORs, which hits the
    floor for every rodenticide, where none of them apply.
  - Nothing supplies an assessment, because the server never calls a model by design.
- **Item group.** The phone sends none. The server files anything with an EPA number under Crop
  Protection Products, which is wrong for a mouse bait.
- **AFB-2026-00025.** Both stock attempts (18:13:47 and 18:14:01) failed on the missing Stock
  Adjustment Account (fixed in v0.200.0 as a clear refusal). The item's `valuation_rate` is 0,
  which would stop the same receipt again at submit.

The phone builds against this document. Everything is additive: an older phone keeps working,
and an older server answers 404 on the new route.

## 1. Use scope: crop or non-crop

A pesticide is **`Crop`** (applied to crops, which is where REI, PHI, PHI crop, MRL and rate per
acre live) or **`Non-crop`** (rodenticides, structural and indoor pest control: buildings,
burrows, bait stations). Both are EPA-registered, so the label capture, validation, EPA number,
signal word, active ingredients, PPE and storage/disposal apply to both.

New Item compliance fields (installed on migrate, shown with the other label fields):

| field | type | |
|---|---|---|
| `pesticide_use_scope` | Select `\nCrop\nNon-crop` | which set of label rules applies |
| `storage_disposal` | Small Text | the label's storage and disposal statement |

## 2. Item group

`create_item` (MCP and `/mobile`) takes **`item_group`** (the phone may now send it) and
**`pesticide_use_scope`**.

- With no `item_group` and an EPA number, the group follows the scope. `Non-crop` goes to
  **Pest Control Products**; `Crop` or unknown goes to **Crop Protection Products**. Either group
  is created as a leaf under All Item Groups the first time it's needed.
- An explicit `item_group` always wins. A missing group is refused with the list, as today,
  except that these two named groups are created on first use.
- The Desk's pesticide-field rule (`CHEMICAL_ITEM_DEPENDS_ON`) also matches `pest control` and
  `rodent`.

`/mobile/list_item_groups` (read, open on enrolment) returns `{groups: [{name, is_group, parent}],
suggested: {Crop: "Crop Protection Products", "Non-crop": "Pest Control Products"}}`, leaf groups
only, so the phone can offer a picker.

## 3. Registering a label: `/mobile/register_product_label`

**Mutating**, dispatch role, the same gate as `create_item`. It runs for every registration: a
new product, an existing product found by UPC, and a product whose packaging differs from the
registration (for example a distributor label on a base registration). It's safe to call again;
each call is a new capture.

| arg | |
|---|---|
| `item_code` | required, an existing Item |
| `file_tokens` | list of staged File docnames from `finalize_staged_file` (the label photos, in page order). At least one, or `ocr_text` |
| `ocr_text` | the phone's OCR, pages joined |
| `extracted_fields` | the phone's reading, in the `validate_document` keys: `product_name`, `epa_registration_number`, `signal_word`, `rei_hours`, `phi_days`, `phi_crop`, `active_ingredients` (JSON list), `application_rate`, `ppe_requirements`, plus the new `pesticide_use_scope`, `storage_disposal`, `target_pests` |
| `llm_assessment` | optional, `{status, issues, confidence, reasoning}` from the on-device model |
| `llm_model` | optional, e.g. `apple-foundation-models` |
| `fetch_epa_label` | optional, default true |
| `company` | optional |

It does four things, and each is reported separately.

1. **Photos.** Each staged File is moved onto the Item (`attached_to_doctype = "Item"`, private).
   A token that isn't a staged, unattached File the caller uploaded is refused before anything is
   written.
2. **Validation.** A Document Validation is created with `source_doctype = "Item"`,
   `source_name = item_code`, `scan_file_url` set to the first photo, the OCR, the extraction,
   the assessment and the model. `Item.label_scan_validation` is set to it.
3. **EPA record.**
   - The base registration (the first two segments: 12455-97-3240 → 12455-97) is looked up at
     `https://ordspub.epa.gov/ords/pesticides/cswu/ppls/{base}`.
   - The newest `pdffiles[]` entry is downloaded from
     `https://www3.epa.gov/pesticides/chem_search/ppls/{pdffile}` through `url_fetch.fetch`
     (public-address check, 25 MB cap) and attached to the Item as `EPA label {base} ({date}).pdf`.
   - The EPA record's product name, registrant, signal word and active ingredients (name, CAS,
     percent) are returned. Its signal word and ingredients fill blanks on the Item; they never
     overwrite a value already there.
   - This step is best effort. A network failure or an unknown number is reported as
     `epa_label.status`, and the call still succeeds.
4. **Item fields.** Blanks only (the never-overwrite rule): `epa_registration_number`,
   `signal_word`, `active_ingredients`, `ppe_requirements`, `pesticide_use_scope` and
   `storage_disposal`, from the validated extraction, then from the EPA record.

The answer:

```json
{"item_code": "PROWLER™",
 "photos": [{"file": "…", "file_name": "label-1.jpg", "file_url": "/private/files/…"}],
 "validation": {"name": "DVAL-2026-0005", "status": "Pending", "confidence": 0.78,
                "issues": [...], "llm_model": "apple-foundation-models"},
 "epa_label": {"status": "attached" | "not_found" | "failed" | "skipped",
               "registration": "12455-97", "product_name": "F-TRAC PLACE PACS",
               "registrant": "BELL LABORATORIES, INC", "signal_word": "Caution",
               "active_ingredients": [{"name": "Bromethalin", "cas": "63333-35-7", "percent": 0.01}],
               "pdf": {"file": "…", "file_name": "EPA label 12455-97 (2019-12-13).pdf"} | null,
               "reason": null | "…"},
 "item_updates": {"epa_registration_number": "12455-97-3240", ...},
 "warnings": ["…"]}
```

An MCP tool **`attach_epa_label`** (mutating, default off, the unit-register roles) runs step 3
alone for an existing Item. It's for back-filling PROWLER from the Desk.

## 4. Scoring a non-crop label fairly

`document_intel` pesticide rules read `pesticide_use_scope`. When it's absent, the scope is
inferred from the OCR: rat, mice, mouse, rodent, vole, bait station, burrow or "in and around
buildings" words, with no crop or "Agricultural Use Requirements" wording, mean `Non-crop`. For
`Non-crop`:

- a missing REI, PHI or PHI crop is not an issue, and a missing per-acre rate is not an issue;
- coverage is measured over `epa_registration_number`, `signal_word`, `active_ingredients`,
  `ppe_requirements`, `application_rate` and `storage_disposal`.

The EPA record, when fetched, is a check: an extracted signal word or active ingredient that
disagrees with EPA's is a WARNING that names both.

## 5. Stock receipts at no cost

A Material Receipt line with no `basic_rate`, for an Item whose `valuation_rate` is 0, is
created with `allow_zero_valuation_rate = 1` (where the column exists). The answer lists it in
`zero_valued_items` with a sentence, so the entry can be submitted. A `basic_rate` that is sent is
used as sent. The phone's Log stock form gains an optional **Cost each** field for Received.

## 6. Surface

- **New `/mobile` routes:** `register_product_label` (mutating, dispatch role) and
  `list_item_groups` (read).
- **`/mobile/create_item`** gains `item_group` and `pesticide_use_scope`.
- **New MCP tool:** `attach_epa_label`.
- **New Item fields:** `pesticide_use_scope` and `storage_disposal`.
- **Deploy:** `bench migrate`, then an image rebuild. fafo_ios SERVER_CHANGES §48.

## 7. Amendment for v0.202.0, after the first live registration on OML (frozen)

Tim's iPhone (fafo_ios df18906 against OML v0.201.0) was refused three times with *"No permission for
Item Group. Nothing was changed."* `create_item` tried to **insert** the missing "Pest Control
Products" group as the phone user. The same screen showed "Rates by crop: As labelled: ly from other
laundry. Remove PPE imnest". The parser's `rate:` pattern matched the "rate" inside "sepa**rate**ly".
"Counted in" ended up Box rather than Block.

- **The groups always exist.** `after_migrate` seeds **Crop Protection Products** and **Pest
  Control Products** (create-only, by name, as leaves under All Item Groups). A group an operator
  already made, wherever it is in the tree, is left where it is.
- **Registration never creates master data.** `create_item` no longer inserts an Item Group, on any
  path. When the wanted group (named, or chosen by scope) is missing, the Item is created in the
  site's default group, `needs_review` gains **`"item_group"`**, and `item_group_note` names the
  missing group. `item_group_created` is always false.
- **`list_uoms` names a context's default.** With `context` sent, the answer carries
  **`default_uom`** (that context's default unit, or null). The phone uses
  `list_uoms(context: "Bait")` to default a non-crop bait's "Counted in" to **Block**.
- **Phone, non-crop rate.** `rate` is matched as a word only, never inside another word. For a
  Non-crop product the rate is read from the directions/application section: a count of blocks,
  place pacs, pellets or baits per placement, station or burrow ("Place 1 to 2 blocks per
  placement"). Text that isn't a rate is left blank rather than guessed. The heading is **"Rate"**,
  not "Rates by crop", for a Non-crop product.
- **An on-device assessment is advisory.** DVAL-2026-0007 (PROWLER®) was flagged by the phone's
  model alone, for a well-formed EPA number it said "lacks hyphens", a "100% total is implausible",
  and a missing REI on a non-crop bait. An assessment whose `llm_model` starts with `apple-` is kept
  in the issue list with errors downgraded to warnings. It does not change the status or the
  confidence, which stay the rules'. An MCP client's assessment still judges as before. The phone
  also drops findings the rules contradict before sending.

## 8. Bait forms, unit aliases, product matching and advisory findings (frozen, v0.202.0)

From the second live registration (PROWLER® Place Pacs, DVAL-2026-0007) and Tim's corrections:

- The rate came from the PPE laundry sentence and not from APPLICATION DIRECTIONS.
- The product is counted in **Place Pacs** ("22 x 3 oz (85 g) Place Pacs"), not Blocks. Tim added
  the "Place Pac" unit live, but "pacs" still resolves to nothing.
- The on-device model invented errors.
- PROWLER™ (refillable bait station, blocks, UPC 048745228174) and PROWLER® (Place Pacs, EPA
  12455-97-3240, UPC 048745221441) are **two different products** that share a brand.

### 8.1 Bait units and aliases

- **Seeded units** (create-only, all whole-number count units): **Place Pac**, **Pouch** and
  **Bait Station**, alongside Block.
- **The Bait context** offers Block (still the default), Place Pac, Pouch and Bait Station.
  - A new site gets all four from the seed.
  - An existing Bait context gets the missing ones from patch `add_bait_units_to_context`. It runs
    once, adds a unit only if absent, and never removes one.
- **Built-in resolver aliases:**

| label wording | unit |
|---|---|
| place pac, place pacs, pac, pacs, pack, packs | Place Pac |
| block, blocks | Block |
| pouch, pouches, packet, packets | Pouch |
| bait station, bait stations, station, stations | Bait Station |

  Plurals resolve through the singular form. The first site unit an alias names wins, as before.
- **Site-editable aliases:**
  - A new Custom Field on UOM, **`uom_aliases`** (Small Text, one spelling per line). The resolver
    matches it case- and plural-insensitively after exact names and before the built-in table.
  - A new MCP tool, **`set_uom_aliases`** (mutating, default off, the unit-register roles), takes
    `uom` plus `add`, `remove` or `replace` (lists).
  - It refuses a spelling that is another unit's name or alias, which is how two units would come
    to answer to one word.
  - `get_uom`, `list_uoms` and `/mobile/list_uoms` rows carry **`aliases`**.
  - New spellings need no deploy.

### 8.2 One product, one Item; two products, two Items

- New Item fields **`product_form`** (Data: "Place Pacs", "Bait Station", "Blocks", "Pellets", …)
  and **`package_size`** (Data, as printed, e.g. "22 × 3 oz (85 g)").
- `create_item` and `register_product_label` accept both, filling blanks only.
- **`/mobile/match_product`** is a read, open on enrolment. It takes `epa_registration_number`,
  `barcode`, `product_form` and `package_size` (all optional, at least one).

```json
{"verdict": "new" | "existing" | "related",
 "matches": [{"item_code": "…", "item_name": "…", "product_form": "Place Pacs",
              "package_size": "22 × 3 oz (85 g)", "epa_registration_number": "12455-97-3240",
              "barcodes": ["048745221441"], "match": "same" | "same_registration_other_form"}]}
```

- **Same** means the barcode is on that Item, or the base EPA registration **and** the normalised
  product form **and** the normalised package size are all equal.
- The same registration with a different form or package is **related**. It is shown, and never
  merged or updated automatically.
- **The name is never compared.**
- The phone offers "Update <item>" for a `same` match (the barcode is linked and the label
  registered on it) and "Create a new product" otherwise.
- The default `item_name` is brand + product form + package size from the label (for example
  "PROWLER Place Pacs (22 × 3 oz)"). The user can edit it.

### 8.3 Findings the rules contradict are dropped

For an advisory (`apple-*`) assessment, the server **drops**, rather than just downgrading:

- an EPA-number finding when the extracted number is well-formed (two or three hyphenated parts,
  as `_check_epa_number` accepts);
- REI, PHI, PHI-crop and per-acre findings on a Non-crop label;
- findings that a 100% ingredient total is wrong.

The remaining messages are truncated at 240 characters. The phone applies the same filter and
tells the model the use scope and these rules.

### 8.4 Phone (fafo_ios SERVER_CHANGES §49)

- **The rate** comes from APPLICATION DIRECTIONS / DIRECTIONS FOR USE, never from PPE, first-aid
  or laundry text ("rate" is matched as a whole word). Recognised forms:
  - "Place 1 place pac per bait placement";
  - "Up to two place pacs …";
  - "place 1 or 2 blocks of bait in the bait station";
  - "N blocks per station";
  - the spacing, "8- to 12- feet".
- **The unit** comes from the package line ("22 x 3 oz (85 g) Place Pacs" gives Place Pac) and the
  rate text. It defaults `stock_uom` and the rate unit when the user hasn't touched them.
- **Pickers** list the live `list_uoms(context: "Bait")` units, never a hardcoded list.
