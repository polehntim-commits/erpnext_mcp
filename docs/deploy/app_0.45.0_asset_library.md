**Deploy: FarmOps 0.45.0 (build 40) — Library documents on an asset (server v0.255.0)**

App release. Ship instead of 0.44.0 (it contains it). The Library rows appear with server **v0.255.0**; on an older
server the asset screen is unchanged. fafo_ios main, scheme **FarmOps**.

**1. Build (Tim)**

Archive and ship FarmOps 0.45.0 (build 40). Verified here: `xcodebuild build -scheme FarmOps` succeeds; FarmOpsKit
tests pass on the simulator (2,936).

**2. Phone checks**

1. Scan a well whose log is cited (v0.255.0 §3): Documentation lists it with a books icon and "Record · year · pp.".
2. Tap it: the PDF opens. Leave signal and open it again: it opens from the phone.
3. Spanish: the Documentation footer reads in tú.

**3. Rollback**

Ship 0.44.0.
