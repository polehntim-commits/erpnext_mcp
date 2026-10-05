**Deploy: FarmOps 0.38.6 (build 31) — Spanish for the office screens (app only; any server from v0.231.2)**

No server change. Ship instead of 0.38.5 (it contains it).

**1. Build (Tim)**

Archive and ship FarmOps 0.38.6 (build 31) from fafo_ios main, scheme **FarmOps**. Verified here: `xcodebuild
build -scheme FarmOps` succeeds; the Spanish table lints (`plutil -lint`); FarmOpsKit was not changed.

**2. Phone checks**

1. Settings → Language → Spanish: Ground (map, valves, structures, slope layers), Assets, My Records (pay stubs,
   I-9), Field Reference, Inventory (stock entry, label capture, barcodes), Harvest Day, Visits, Sync diagnostics
   now read in Spanish (tú). 3,646 strings in all.
2. Counts read as whole sentences in both languages: "1 bucket · 2 pickers", "3 more assets have no position
   recorded", "Retired on 3 Oct — …", "1 item hasn't reached the farm" (the English used to say "haven't").
3. Still English, on purpose: developer screens, units, and server values dropped into a sentence (an asset type, a
   valve state) — those need translated data, not app strings.

**3. Rollback**

Ship 0.38.5. Nothing stored changes.
