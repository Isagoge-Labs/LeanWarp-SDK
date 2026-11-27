# Lean 4.35 example

This Lake project pins Lean 4.35 and Mathlib. Run `leanwarp versions` to see
the hosted toolchains. A first-time environment build uses prepaid credit when
project builds are available. To try the example without a build, copy only
`LeanWarpExample.lean` into a new folder outside any Lake project. From your
chosen folder, after `leanwarp auth login`:

```sh
leanwarp doctor
leanwarp connect
leanwarp verify LeanWarpExample.lean \
  --declaration add_zero_example --target '∀ n : Nat, n + 0 = n'
```

If the result has `"success": null` or exits with code `3`, it is still
running. Wait for that same operation; do not submit it again:

```sh
leanwarp wait --timeout 300
```

A wait timeout leaves the operation running. Wait again, or use `leanwarp cancel`
and keep polling until it reports `completed`, `failed` or `cancelled`. Once it
is terminal, stop the worker:

```sh
leanwarp stop
```

`verify` prints `"success": true` when the proof is verified. Edit
`LeanWarpExample.lean` and verify again: the same workspace and warm worker are
reused. To start your own project on this environment, copy this directory and
keep `lean-toolchain` and `lake-manifest.json` as they are.

You don't need Lean installed to run these commands. A local editor or a local
`lake build` still needs its own Lean setup.
