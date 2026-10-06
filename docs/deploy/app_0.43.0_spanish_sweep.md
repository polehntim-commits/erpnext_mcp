**Deploy: FarmOps 0.43.0 (build 38) — Spanish sweep (no server change; server v0.252.0)**

App release only. Ship instead of 0.42.0 (it contains it). fafo_ios main **`2f1ac6c`**, scheme **FarmOps**.

**What changed**

133 longer explanations (irrigation zones and valves, ground and slope maps, ending a crew shift, heat, food safety,
receipts and scale tickets, lot traceability, MRL lookup, sensors, soils, certifications) were always English, even on
a Spanish phone: they were built by joining pieces of text, which the phone shows as-is and never looks up. They are
now single texts with Spanish (tú). Developer screens and units (°F, gal/min, I-9) are unchanged.

**1. Build (Tim)**

Archive and ship FarmOps 0.43.0 (build 38). Verified here: `xcodebuild build -scheme FarmOps` succeeds; the Spanish
table lints and the built app resolves the new texts.

**2. Phone checks** (phone language Spanish)

1. Crew → end a shift with nobody clocked out: "Nadie marcó la salida en este turno…".
2. Ground → an irrigation zone, then a valve: the explanations read in Spanish.
3. Receipts → a scale ticket: "El neto es bruto menos tara…".
4. English phone: every text unchanged.

**3. Rollback**

Ship 0.42.0.
