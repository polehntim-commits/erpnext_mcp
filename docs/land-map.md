# Land map — `/app/land-map`

Draw a proposed lot line on satellite imagery and get back what a surveyor
actually needs: a bearing and a distance for every course, the acreage enclosed,
how well the figure closes, the easements the line runs through, and a draft
metes-and-bounds description.

**It is a draft for a surveyor. It is not a survey**, and every description it
writes says so in its own last paragraph. A monument on the ground beats a click
on an aerial photograph every time. What this buys is the hour before the
surveyor is called: a conversation about where the line might go becomes a
document with the acreage and the courses roughly right, which is something a
surveyor can price and correct.

---

## Getting there

`/app/land-map`, or **Land Map** in the awesomebar, or the card on the
**Land & Parcels** workspace, or the **Survey → Land Map** button on any saved
Lot Line Adjustment. The button carries the adjustment through, so the page opens
with that record chosen and anything already drawn on it on screen.

The Page record ships with the app and appears after `bench migrate`.
`bench build` matters too: the page reads the Leaflet bootstrap out of this app's
own asset path, and *"the map widget loaded but is an older build"* means the
build has not run — a different problem from no network, which is why it has its
own sentence.

---

## What is on the map

| Layer | Comes from |
|---|---|
| **Parcels**, **Fields**, **Irrigation Zones** | the same whitelisted method `/app/farm-overview` uses, with its entity picker and its permission rules unchanged |
| **County Tax Lots** | `County Tax Lot.geometry` — the county's own polygons, cached |

The tax lots are the layer this page adds, and they are **not** narrowed by the
entity picker. That is the point: a lot line adjustment is an agreement with
whoever owns the ground on the other side, and their lot has to be on the map.

---

## Drawing

- **Draw a line** / **Draw an area** — click each corner; click the last point
  twice to finish. The drawing is a proposal and belongs to no record until you
  save it.
- **Draw an easement** — a corridor (a ditch, a shared driveway, a power line).
  It is checked against, never measured, and several can be drawn.
- **Compute** — sends the points to the server and fills the three panels.
- **Clear drawing** — throws away the proposal and the corridors.

Nothing is computed in the browser. Every number on the page comes from
`erpnext_mcp/surveying.py`, which is the module the test suite measures against
published geodetic figures — so the acreage here and the acreage a tool reports
can never drift apart.

---

## What comes back

**Courses.** One row per leg: a quadrant bearing the way a deed prints it
(`N 45°30'15" E`) and a distance in feet. The last course closes the figure and
says so. Bearings are **grid azimuths from true north** on WGS84 — not magnetic,
and not referred to a state plane basis of bearings. Distances are
**international feet** (0.3048 m exactly); the metres come with them.

**Closure.** How far the traverse misses its own starting point, and the
precision ratio (`1 part in 4,300`). It is reported and never silently fixed: a
figure drawn by clicking does not close, and by how much is exactly what a
surveyor checks first.

**What the line runs through.** Every easement corridor the line crosses or lies
inside, by name. A corridor somewhere else is not listed — which is the half that
makes the list worth reading.

**Acreage before and after.** For each mapped lot the drawing falls on: what it
holds now, the acreage of the overlap, and what it would hold **if it gave** that
piece and **if it received** it. The page will not guess which, because a polygon
drawn across a boundary does not say. Where the adjustment already exists as a
record, its own pieces and parties answer the question properly and that answer
is shown underneath.

**The draft description.** Metes and bounds, beginning at the nearest mapped
corner — "Commencing at the northwest corner of Tax Lot 200; thence…". It is
editable on the page before it is saved.

> **No PLSS corner is named.** A section corner is a monument in the BLM's
> database; this app ships no PLSS layer and does not fetch one, so a description
> that said "from the NW corner of Section 7" would be an invention. The point of
> beginning is a corner taken from mapped parcel data, and the disclaimer says so.

---

## Saving, and the printable package

**Save to adjustment** writes three columns onto a Lot Line Adjustment:
`proposed_geometry`, `easement_geometry` and `generated_legal_description`. You
can pick an existing adjustment or create one by giving it a title.

The write goes through the same `lla_create` / `lla_update` tools an operator or
the AI would use, so the role gate (`Land Agreements`), the status machine and
the party checks all apply. All three columns stay editable after the adjustment
is submitted — like a piece's geometry, because the surveyor's answer arrives
after the paperwork has gone to the county, and the drawing it corrects is this
one.

**Surveyor package** prints the description, the course table, the area, the
closure figure, the easements crossed and the disclaimer — the sheet to send with
the request for a survey.

---

## When there is no map

Leaflet and the tiles come from a CDN. On a bench with no outbound internet the
page says the library could not be reached and lists what it would have drawn as
a table of records with links. Drawing needs the library, and the page says that
in a sentence rather than offering a button that does nothing.
