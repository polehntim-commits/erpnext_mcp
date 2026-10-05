**Deploy: FarmOps 0.38.4 (build 29) — background sync (app only; server stays v0.231.2)**

No server change, no migrate. Any box on v0.231.2 (or one built from `catchup_v0.230.3-v0.231.1.md` up to
v0.231.2) takes this app.

**1. Build (Tim)**

Archive and ship FarmOps 0.38.4 (build 29) from fafo_ios main. The archive now carries two Info.plist keys:
`UIBackgroundModes` = `fetch` and `BGTaskSchedulerPermittedIdentifiers` = `farm.fafo.FarmOps.sync`. App Store /
TestFlight review sees "Background fetch"; no location or audio background mode is added.

**2. Phone checks**

1. Settings (iOS) → General → Background App Refresh: Farm Ops is listed and on.
2. Airplane mode on: record a spray and pause a task, so the badge shows two. Press the Home / swipe up (do not swipe
   Farm Ops away), turn airplane mode off, leave the phone on a charger on Wi-Fi. Within an hour or so (iOS picks
   the moment) the farm has both without the app being opened. Opening the app still sends at once, as before.
3. Swiping the app away stops background wakes until it is opened again — that is iOS, not a fault.

**3. Also on fafo_ios main since 0.38.3 (no behaviour change)**

Comment wording that had been sitting uncommitted on main (b785aee). One reworded message was reverted because a
test pins it (b3e48a2). Everything found uncommitted is preserved on `wip/main-stray-edits-2026-10-04`.

**4. Rollback**

Ship 0.38.3; nothing stored changes.
