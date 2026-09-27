# The unit register — contract (frozen for v0.197.0)

Source: OML App Feedback **AFB-2026-00023**. Registering **PROWLER™** rodent bait
(UPC-A 048745228174) from the phone was refused: the product was sent with
`stock_uom: "Noi"`, and `create_item` answered *"no UOM called 'Noi' on this site …
Nothing was created."* The label's rate is *"Norway rats: 1 or 2 blocks of bait; roof
rats: …; house mice: …"*, and Tim confirmed the product **is counted in blocks of bait**:
the unit is **Block** (a whole number) and "1 or 2" is the quantity.

This document is the contract the phone (fafo_ios) builds against. The names, keys and
shapes below are fixed. Any change to them is a new section, not an edit to this one.

## 1. The resolver — one answer shape everywhere

`uom_resolve.resolve_rate(text)` and `uom_resolve.resolve_unit(text)` return the
**Resolution** object. The same shape comes back from the `resolve_uom` MCP tool, from
`create_item`'s `rate_uom` key, and from `/mobile/list_uoms`'s `suggestion`.

```json
{
  "text": "Norway rats: 1 or 2 blocks of bait; roof rats: 2 blocks of bait",
  "phrase": "blocks of bait",
  "quantity": {"min": 1.0, "max": 2.0},
  "uom": "Block",
  "matched_by": "alias",
  "status": "resolved",
  "candidates": ["Block"],
  "clauses": [
    {"target": "Norway rats", "phrase": "blocks of bait", "quantity": {"min": 1.0, "max": 2.0}, "uom": "Block"},
    {"target": "roof rats", "phrase": "blocks of bait", "quantity": {"min": 2.0, "max": 2.0}, "uom": "Block"}
  ],
  "mixed": false
}
```

| key | meaning |
|---|---|
| `phrase` | The unit words printed **after the quantity**, up to `/`, `per`, `,`, `(`, `;` or the end of the clause. Words before the number are the **target** (a crop or a pest), never a unit. This is the rule that stops "Norway" becoming a unit. |
| `quantity` | `{min, max}`. "1 or 2", "1-2", "1–2" and "1 to 2" give a range. A single number gives `min == max`. `null` when no number was printed. |
| `uom` | A **UOM docname that exists and is enabled on this site**, or `null`. Never a guess. |
| `matched_by` | `exact` \| `case` \| `alias` \| `plural`, or `null`. |
| `status` | `resolved` (a `uom` was found), `unresolved` (there was a phrase, or a unit was named, and no UOM matched), or `absent` (nothing to resolve). |
| `candidates` | Up to 10 enabled site UOMs for a person to pick from. A resolved answer has its `uom` first. |
| `clauses` | One per `;`- or newline-separated clause of a rate. Present only for `resolve_rate`. |
| `mixed` | `true` when the clauses resolve to more than one unit. The first one wins `uom`, and the result is flagged for review. |

Alias table (`uom_resolve.ALIASES`), with the first name that exists on the site winning:
`block/blocks/bait block/blocks of bait → Block`; `oz/ounce → Ounce`;
`fl oz/fluid ounce → Fluid Ounce, Fluid Ounce (US)`; `lb/lbs/pound → Pound`;
`gal/gallon → Gallon, Gallon Liquid (US)`; `pt/pint → Pint, Pint, Liquid (US)`;
`qt/quart → Quart, Quart Liquid (US)`; `kg → Kg`; `g/gram → Gram`; `ml → Millilitre`;
`l/liter/litre → Litre`; `each/ea/count → Nos`; `ac/acre → Acre`; plus the seeded
containers (bin, lug, bucket, bushel).

## 2. Seeded units and contexts (after_migrate, create-only)

New in `ag_uom.SEED_UOMS`: **Block** (`must_be_whole_number: 1`, measures Count) and
**Ounce** (`0`, Weight). Pound was already seeded. `bench migrate` creates only the ones
that are missing. New contexts, also create-only:

- **Bait** — `applies_to: Count` — Block (default).
- **Dry Product** — `applies_to: Weight` — Pound (default), Ounce.

Bait and dry product are two contexts, not one "Crop Protection" context, because the
context controller refuses a list that mixes two measurements.

New Exact conversion: Pound → Ounce, 16.

## 3. Item: `application_rate_uom`

A new compliance Custom Field on Item: **`application_rate_uom`**, Link → UOM, inserted
after `application_rate`. The rate text stays the law. This field says which unit its
numbers are in, so a rate can be tied to stock, to a tank mix and to a conversion.

## 4. `create_item` (MCP tool and `/mobile/create_item`)

New optional argument: **`application_rate_uom`**.

- **The rate unit.** Taken from `application_rate_uom` when that is sent, otherwise
  resolved from `application_rate`. If it resolves, it is written to the Item. If it does
  not, the Item is **still created**, the field is left blank, and the answer flags it.
- **The stock unit.** `stock_uom` is resolved through the same resolver, so "blocks"
  gives Block. When it is omitted, the stock unit is the resolved rate unit if that unit
  is a whole-number one (Block), and **Nos** otherwise. When `stock_uom` was sent and
  cannot be resolved, the Item is **still created**. It falls back to the resolved
  whole-number rate unit or to Nos, and the answer flags it.
- Everything else refuses as before: a duplicate item code, a barcode already on
  another Item, a bad label value.

New answer keys, always present:

```json
{
  "stock_uom": "Block",
  "application_rate_uom": "Block",
  "rate_uom": { "...Resolution..." },
  "stock_uom_resolution": {"requested": "Noi", "uom": "Block", "matched_by": null,
                           "status": "fallback", "candidates": ["Nos"]},
  "needs_review": ["stock_uom"]
}
```

`stock_uom_resolution.status` is one of `resolved`, `defaulted` (nothing sent) or
`fallback` (sent but unresolved). `needs_review` is a list containing any of
`"stock_uom"` and `"application_rate_uom"`, and is `[]` when nothing needs a person.

## 5. `update_item` (MCP)

New optional arguments: `application_rate_uom` (`""` clears it) and `stock_uom`. Both go
through the resolver. An explicit pick that does not resolve is **refused** and the
refusal lists the candidates. A `stock_uom` change is refused once the Item has stock
ledger entries, because ERPNext's own rule is that the stock unit is fixed after the
first transaction.

## 6. Phone routes

`POST|GET /farmops/api/mobile/list_uoms` — **read**, open on enrolment (scope only).

| arg | |
|---|---|
| `search` | optional substring of the name |
| `rate_text` | optional. When sent, the answer's `suggestion` is `resolve_rate(rate_text)` |
| `unit_text` | optional. When sent (and `rate_text` is not), `suggestion` is `resolve_unit(unit_text)` |
| `context` | optional Agricultural UOM Context. Only its units are returned |

```json
{"uoms": [{"name": "Block", "must_be_whole_number": true, "measures": "Count"}],
 "count": 1, "truncated": false, "suggestion": { "...Resolution..." }}
```

Enabled units only, sorted by name, capped at 500. Seeded farm units are listed first,
then the rest alphabetically. `suggestion` is `null` when neither text is sent.

`POST /farmops/api/mobile/create_item` — gains **`application_rate_uom`**. The answer
carries the §4 keys.

`POST /farmops/api/mobile/update_item_units` — **mutating**, dispatch role (the same
gate as `create_item`). Arguments: `item_code` (required), `stock_uom`,
`application_rate_uom` (at least one). This is how a person answers `needs_review` from
the phone. Answer: `{"item_code", "stock_uom", "application_rate_uom", "changed": {...}}`.

An older server answers 404 on the two new routes. The phone then falls back to a
free-text unit and says so.

## 7. MCP tools — the unit register (all require ERPNext)

Read, default **ON**: `list_uoms`, `get_uom`, `resolve_uom`.

Mutating, default **OFF**, gated to one of **System Manager, Stock Manager, Item Manager,
Farm Manager** (`uoms.UOM_ROLES`):

| tool | args | refuses |
|---|---|---|
| `create_uom` | `uom_name`, `must_be_whole_number`, `enabled` | a name that exists in any casing, or that the alias table already sends to another unit |
| `update_uom` | `uom`, `must_be_whole_number`, `enabled` | renaming (disable the unit and create a new one) |
| `disable_uom` | `uom` | a unit listed in an active context (it names the contexts). The answer reports how many Items still stock in the unit |
| `set_uom_conversion_factor` | `from_uom`, `to_uom`, `value`, `category` | same unit on both sides, `value <= 0`, a reverse row that disagrees |
| `delete_uom_conversion_factor` | `from_uom`, `to_uom` | a pair with no row (destructive, own switch) |
| `create_ag_uom_context` | `context_name`, `applies_to`, `description`, `uoms: [{uom, is_default, notes}]` | an existing name; the controller's own rules (empty, duplicate, two defaults, mixed measurement) |
| `update_ag_uom_context` | `context`, `is_active`, `description`, `default_uom` | a `default_uom` not in the context |
| `add_uom_to_context` | `context`, `uom`, `is_default`, `notes` | a unit already listed; a unit of another measurement |
| `remove_uom_from_context` | `context`, `uom` | the last unit (switch the context off instead) |

All mutating tools end their refusals with "Nothing was changed." / "Nothing was created."
