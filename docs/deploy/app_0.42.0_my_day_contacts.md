**Deploy: FarmOps 0.42.0 (build 37) — my Start / End of Day, and Contacts → Where met (server v0.252.0)**

App release. Needs server **v0.252.0** (`my_day_check`, `search_contacts`, `update_contact_where_met`); deploy that
first. Ship instead of 0.41.0 (it contains it). fafo_ios main **`d16964f`**, scheme **FarmOps**.

**1. Build (Tim)**

Archive and ship FarmOps 0.42.0 (build 37). Verified here: `xcodebuild build -scheme FarmOps` succeeds; FarmOpsKit
tests pass on the simulator (2,934); the Spanish table lints.

**2. Phone checks**

1. With **Personal Day Checks** ticked in ERPNext MCP Settings: Today shows **My day** — "Start of Day" after the first
   task is started, "End of Day" from the button or after clocking out. End of Day asks the hour meter of each machine
   used today (never below its last reading) and the crop stage of blocks worked whose stage is stale; finishing it
   files them. With the switch off, My day does not appear.
2. As a Foreman or higher: Receipts → **Business cards** → **Contacts**, search "Sheppard", open Ben Sheppard →
   **Where met**, change the place or date, save. Desk → Contact shows the new Met At / Met On.
3. Scan business card still works from the same menu.
4. Spanish reads in tú.

**3. Rollback**

Ship 0.41.0. Day checks and contact edits already made stay on the server.
