# Lean 4.26 example

This project contains a small proof and the locked dependencies for the supported
Lean 4.26 / Mathlib environment. From this directory, after authenticating:

```sh
leanwarp doctor
leanwarp connect
leanwarp verify LeanWarpExample.lean \
  --declaration add_zero_example --target '∀ n : Nat, n + 0 = n'
leanwarp wait
leanwarp stop
```

Continue only if `doctor` reports `compatible: true`. If verification is still
running after a polling timeout, wait again or cancel it and wait until terminal
before stopping compute.

Edit `LeanWarpExample.lean` and repeat verification in the same directory to reuse
the workspace. Keep `lean-toolchain` and `lake-manifest.json` unchanged; the server
requires an exact metadata match. Use this as a separate example, not as a
replacement for the dependencies of an existing research project.

The SDK sends Lean source to the hosted environment. You do not need to install
Lean or build Mathlib locally for these commands. A local editor or local builds
may still require their own Lean setup.
