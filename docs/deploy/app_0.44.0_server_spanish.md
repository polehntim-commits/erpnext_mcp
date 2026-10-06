**Deploy: FarmOps 0.44.0 (build 39) — refusals from the farm read in Spanish (server v0.254.0)**

App release. Ship instead of 0.43.0 (it contains it). Works with any server; the Spanish appears with **v0.254.0**.
fafo_ios main **`79f952d`**, scheme **FarmOps**.

**1. Build (Tim)**

Archive and ship FarmOps 0.44.0 (build 39). Verified here: `xcodebuild build -scheme FarmOps` succeeds; FarmOpsKit
tests pass on the simulator (2,935).

**2. Phone checks** (server on v0.254.0)

1. Phone language Spanish: claim a task somebody else is doing — the alert reads in Spanish ("… ya está haciendo …").
2. Phone language English: the same refusal shows the full English sentence, as before.
3. A refusal the server has no phrase for still shows the English (never "Algo salió mal" in place of a reason).

**3. Rollback**

Ship 0.43.0.
