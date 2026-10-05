**Deploy: FarmOps 0.38.5 (build 30) — the app compiles again; 793 Spanish strings; accessibility pass (app only; server stays v0.231.2)**

**SHIP THIS ONE, NOT 0.38.1–0.38.4.** Those four builds were checked with the `fafo_ios` scheme, which does not
compile the FarmOps app target, and FarmOps did not compile from 0.38.1 on. An archive of any of them fails in
Xcode. 0.38.5 contains everything they promised (offline shift starts and reports, inspection walks, task clock,
sprays, SOPs, REI board, background sync) plus the fix. Every deploy file that names 0.38.1–0.38.4 now points
here; their phone checks still apply, on 0.38.5.

No server change, no migrate.

**1. Build (Tim)**

Archive and ship FarmOps 0.38.5 (build 30) from fafo_ios main (scheme **FarmOps**). Verified here: `xcodebuild
build -scheme FarmOps` succeeds on fafo_ios main as checked out (including the untracked Geo prototype files),
and FarmOpsKit's 2,913 tests pass.

**2. Phone checks**

1. The checks in `v0.231.1_offline.md` §3, `v0.231.2_offline_tasks.md` §3 and `app_0.38.4_background_sync.md` §2.
2. Settings → Language → Spanish: Work, Today, task detail, crew clock, bucket capture, scanning, spray, inspections,
   housing walks, training, safety, forms and receipts read in Spanish (tú). Office screens — Ground, Assets, My
   Records, Field Reference, Inventory, Harvest Day — are still largely English.
3. VoiceOver on: a crew task's finished section reads "selected"; decorative ticks and icons are not read out; the
   Today map's expand / recentre buttons and the bucket screen's settings button are easy to hit.

**3. Rollback**

Ship 0.37.4 (the last build that compiled before 0.38.0's line). Nothing stored on the phone is incompatible.
