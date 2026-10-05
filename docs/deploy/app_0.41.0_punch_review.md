**Deploy: FarmOps 0.41.0 (build 36) — punch review for supervisors (server v0.251.0)**

App release. Needs server **v0.251.0** (punch review routes); deploy that first. Ship instead of 0.40.0 (it contains
it). fafo_ios main, scheme **FarmOps**.

**1. Build (Tim)**

Archive and ship FarmOps 0.41.0 (build 36). Verified here: `xcodebuild build -scheme FarmOps` succeeds; FarmOpsKit
tests pass on the simulator (2,931); the Spanish table lints.

**2. Phone checks** (as a Foreman, Farm Manager or HR)

1. Today: "Punches to review: N (M flagged)" when this week has unreviewed punches.
2. **Review this week's punches**: flagged punches say why (no clock-out, still clocked in, sent late, edited, GPS
   outside the block, short breaks). **Approve these N** approves the unflagged ones together.
3. A flagged punch: **Approve** asks for a reason; **Fix the time** sets the in / out and a reason. A missing clock-out
   offers only Fix. Afterwards the punch is locked for payroll and its compliance alert clears.
4. The old "Punch times to review" screen (late offline punches) links to the weekly review.
5. Spanish reads in tú.

**3. Rollback**

Ship 0.40.0. Reviews already made stay on the server.
