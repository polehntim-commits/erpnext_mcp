**Deploy: FarmOps 0.47.0 (build 42) — sign-out leaves nothing of the last person's (server v0.260.0)**

App release. Ship instead of 0.46.0 (it contains it). Works with any server. fafo_ios main, scheme **FarmOps**.

**1. Build (Tim)**

Archive and ship FarmOps 0.47.0 (build 42). Verified here: `xcodebuild build -scheme FarmOps` succeeds; FarmOpsKit
tests pass on the simulator (2,942).

**2. Phone checks**

1. With a queued (offline) task start or receipt, Sign out warns about unsent work (before 0.47.0 it did not count
   these). Signing out anyway discards them.
2. Sign in as someone else on the same phone: no previous scans (cabin, badge), drafts, crew lists or previewed files
   remain.

**3. Rollback**

Ship 0.46.0.
