# Cherry degree-day stage table — proposal (for Tim)

Status: **PROPOSAL, not built.** Nothing in this file changes a setting. Written 2026-10-09.

## What this is for

`degree_days.for_block` (v0.249) already turns a block's weather into °F·days since the biofix and, when the crop has a
**stage table**, says which BBCH stage the heat suggests and when the next one is due. Go / Hold presets and
scheduled templates (v0.278: `bbch_from/to`, `dd_from/to`) can lean on it. Today no cherry stage table exists on either
server, so the "suggested stage" is always blank. This proposes one. The plan
(docs/design/plan_patterns_consolidation_2026_10.md §5) makes it a config kind (draft → preview → publish), not a
Settings text box.

The estimate never passes a stage check by itself. Only an observed stage does (decision 12). The table tells the
crew when to go and look.

## Decision needed first: the base temperature

The engine's default is **base 50 °F, cutoff 86 °F**. That is the insect convention, used for codling moth and similar
pests. Cherry *plant* development models use a much lower base:

| Source | Crop / cultivar | Base | Start | What it gives |
|---|---|---|---|---|
| Kaiser, Coop & Meland (OSU Milton-Freewater + Norway), ASHS 2014 | sweet cherry 'Van' | 2 °C (35.6 °F), single sine | March 1 | **254 °C·days to bloom**; beat a 4 °C sour-cherry model |
| Zavalloni, Andresen & Flore 2006, J. Amer. Soc. Hort. Sci. 131:601–607 | sour cherry 'Montmorency' | 4 °C (39.2 °F) | — | flower-bud stages and fruit growth (table not reproduced here; get the paper) |
| Hochmaier, ISHS Acta Hort. 1020 | sweet cherry Bing, Van, Lapins, Sweetheart | 4.5 °C (40.1 °F) | swollen bud | **765 (Van) – 916 (Sweetheart) °C·days to harvest** |
| WSU tree fruit guide (western cherry fruit fly) | the pest, not the tree | 41 °F (5 °C), no upper | March 1 | first fly ~950 °F·days; spray by 1,060 |

At 50 °F the engine undercounts cool spring days badly. In March and April most days never reach 50 °F, so bud
development that really happens shows as zero. That is why the base matters more than any single threshold below.

**Recommendation: 41 °F (5 °C) for the cherry stage table, from March 1.** Reasons:
- It is inside the range the cherry phenology papers use (36–40 °F), close to Hochmaier's 4.5 °C.
- It is the base WSU uses for the cherry fruit fly. One farm-wide cherry number keeps the Go / Hold presets and the
  stage table counting the same heat.
- The pest models keep their own bases (`ipm_reference`: codling moth 50 °F, etc.). This only sets the cherry *crop*
  line (`Cherry: base = 41`), not the pests.

The alternative is **40 °F** (rounder, ≈ 4.5 °C). The difference from 41 °F is small next to year-to-year scatter.
**Tim: 41 or 40?** (or keep 50 and accept late, blank stages in March–April).

## The table — what can be filled now and what cannot

Degree-days **cannot be converted between bases** by arithmetic. A °C·day figure at 2 °C is not a °F·day figure at
41 °F, so the literature numbers above are *anchors to check against*, not values to type in. The defensible way to
fill the table is the plan's method: OML's recorded stages measured against the farm's own degree days.

**What OML has today (read 2026-10-09):** no Crop Observation with a growth stage and no scale tickets. There is
nothing yet to fit, so the proposal is a structure plus a calibration plan, not a set of numbers.

Proposed rows (BBCH, the words the crew sees, and where the number will come from):

| BBCH | Stage | Value (°F·days, base 41 °F, from Mar 1) | Source of the number |
|---|---|---|---|
| 51 | Bud swell | to calibrate | spring 2027 observations |
| 53 | Bud burst / side green | to calibrate | " |
| 55 | Single flower buds visible (tight cluster) | to calibrate | " |
| 57 | Flower pedicels elongating (first white) | to calibrate | " |
| 60 | First flowers open | to calibrate | " ; cross-check Kaiser 'Van' bloom |
| 65 | Full bloom | to calibrate | " ; cross-check Kaiser 'Van' bloom |
| 69 | End of flowering / petal fall | to calibrate | " ; drives the pollination gather-up (v0.275 `petal_fall_bbch`) |
| 71 | Fruit set | to calibrate | " |
| 81 | Colour change (straw) | to calibrate | " ; SWD risk starts around here |
| 87 | Harvest maturity | to calibrate | scale tickets / harvest dates; cross-check Hochmaier 765–916 °C·d |

Per-variety offsets (Bing vs Sweetheart vs Regina…) are a second column once there are two seasons of data. Hochmaier
shows about 150 °C·days between Van and Sweetheart at harvest, so one row per stage for every variety will not be
enough at harvest.

## Calibration plan

1. **This winter:** publish the table with the base only (`Cherry: base = 41`, `cutoff = 86`) and no stage rows. The
   suggestion stays blank, but `get_degree_days` reports cherry heat on the right base from March 1.
2. **Spring 2027:** the crew records stages on the phone (stage capture, v0.241; the Start/End-of-Day stage prompt)
   on a few reference blocks: one early (Chelan/Burlat-type), one Bing, one late (Sweetheart/Regina).
3. **After petal fall and after harvest:** for each recorded stage, read the block's accumulated °F·days on that date
   (`get_degree_days as_of=…`). Take the median per stage, draft the table as a config version, and preview it
   against the season (does it call each stage within ±3 days?). Then a person publishes.
4. **Harvest:** Tim's historical harvest-dates-by-block files, when they arrive, give BBCH 87 for past seasons at once.
   Weather history is available back years, so they calibrate the harvest row without waiting a season.

## What gets built (later, P5)

A config kind `dd_stage_table` (one version per crop) in the existing lifecycle: draft, validate (stages ascending,
°F·days ascending, base 32–65 °F, the same guard v0.276.1 put on pest DD models), preview against a past season, and
publish by a person in the Desk. `degree_days.stage_table` reads the published version instead of the Settings text.
It reuses the generic `config` tool. No new tool.

## Sources

- Kaiser, Coop & Meland, sweet cherry 'Van' bloom model, ASHS 2014: https://ashs.confex.com/ashs/2014/webprogram/Paper17026.html
- Hochmaier, chilling and degree-day requirements of four sweet cherry cultivars, Acta Hort. 1020: https://ishs.org/ishs-article/1020_29/
- Zavalloni, Andresen & Flore 2006 (cited by MSU): https://www.canr.msu.edu/nwmihort/research/develop-a-risk-assessment-model-to-help-manage-swd-in-michigan-tart-cherries
- WSU western cherry fruit fly: https://treefruit.wsu.edu/crop-protection/opm/western-cherry-fruit-fly/
- UC IPM western cherry fruit fly phenology model: https://ipm.ucanr.edu/weather/phenology-models-description/western-cherry-fruit-fly/
