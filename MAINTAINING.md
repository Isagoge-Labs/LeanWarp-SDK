# Maintaining the private SDK distribution

MathTree Cloud's `sdk/leanwarp_cloud/` directory is the source of truth. Develop
and review SDK changes there. This repository receives one-way exports as normal
Git commits; do not develop a second, divergent copy here. Its CI builds and tests
the installed package without backend credentials or a hosted worker.

## Private versions and history

Keep the package version at `0.1.0` during private development. The initial
`v0.1.0` release and tag identify one specific commit and must never be moved or
have their assets replaced. Later updates advance `main` with ordinary commits.
Use a full SDK commit hash to install and report those updates. A package version
of `0.1.0` alone is not enough to identify a private test result.

Record `Source-Cloud-Commit` and `Source-SDK-Tree` Git trailers in every export
commit. These identify the original Cloud revision and the exported directory.
The SDK repository has its own history; it does not contain Cloud's parent
commits, deleted historical files, backend, infrastructure or secrets.

## Export an update

Use an authenticated checkout of each repository. Select a reviewed Cloud commit
and a clean SDK checkout. SDK releases are deliberate; an ordinary Cloud merge
does not push to this repository. These commands use standard Git archive,
rsync, commit and push, with no cross-repository CI credential.

```sh
# Set these to your checkouts and the reviewed full Cloud commit SHA.
cloud_checkout=/absolute/path/to/mathtree-cloud
sdk_checkout=/absolute/path/to/LeanWarp-SDK
source_commit=REVIEWED_CLOUD_COMMIT_SHA

set -euo pipefail
test "$(git -C "$sdk_checkout" remote get-url origin)" = \
  'https://github.com/Isagoge-Labs/LeanWarp-SDK.git'
test -z "$(git -C "$sdk_checkout" status --porcelain)"
git -C "$sdk_checkout" switch main
git -C "$sdk_checkout" pull --ff-only origin main
source_commit=$(git -C "$cloud_checkout" rev-parse "$source_commit^{commit}")
source_tree=$(git -C "$cloud_checkout" rev-parse "$source_commit:sdk/leanwarp_cloud")
export_directory=$(mktemp -d)
git -C "$cloud_checkout" archive "$source_commit:sdk/leanwarp_cloud" |
  tar -x -C "$export_directory"

# Check the exact tracked-file list before copying. No Cloud root files belong here.
git -C "$cloud_checkout" ls-tree -r --name-only "$source_commit:sdk/leanwarp_cloud"
rsync -av --dry-run --delete --exclude='.git/' --exclude='.venv/' --exclude='dist/' \
  "$export_directory/" "$sdk_checkout/"
```

Inspect that preview. Then run the same rsync command without `--dry-run`. It
removes files retired from the SDK source while preserving Git history, the local
virtual environment and build outputs. Never point it at the Cloud checkout.
Do not copy environment files or use a whole-repository mirror.

Review and check the resulting change before committing:

```sh
git -C "$sdk_checkout" add -A
git -C "$sdk_checkout" diff --cached --check
git -C "$sdk_checkout" diff --cached --stat
git -C "$sdk_checkout" diff --cached
# Run the standalone checks below in sdk_checkout before continuing.
git -C "$sdk_checkout" commit \
  -m 'Sync reviewed SDK changes from MathTree Cloud' \
  -m "Source-Cloud-Commit: $source_commit
Source-SDK-Tree: $source_tree"
git -C "$sdk_checkout" push origin main
```

If there is no SDK diff, skip the commit and push. If another writer updated the
remote, fetch and inspect their changes; never force-push over them. The first
export uses an empty private repository initialized with `git init -b main` and
the same remote, skips the initial pull, and makes an initial commit.

## Standalone checks and first release

Build in a clean temporary export or checkout so no stale files remain in `dist`:

```sh
uv build
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python 'dist/leanwarp_cloud-0.1.0-py3-none-any.whl[mcp,test]'
.venv/bin/python -I -m pytest -q
.venv/bin/leanwarp --help
.venv/bin/leanwarp skill
uvx --from twine==7.0.0 twine check --strict dist/*
```

CI repeats the installed-wheel tests on Python 3.12, 3.13 and 3.14, including the
local stdio MCP journey. The SDK CI needs no Modal, Railway, payment or API keys.
After SDK CI passes, download the release files from that exact commit's run.
Check the wheel/sdist file lists before attaching those same tested artifacts to
the private release. Record their SHA-256 checksums and the Cloud/SDK commit IDs.
Use `gh release create v0.1.0 --target SDK_COMMIT --prerelease --verify-tag`
after creating and pushing the annotated `v0.1.0` tag. Pass the tested files as
assets and the prepared notes with `--notes-file`. Verify repository visibility is
still private immediately before publishing the GitHub release.

For later private testing, install the exact selected commit in your authenticated
checkout with `python -m pip install --force-reinstall '.[mcp]'`. Do not overwrite
the initial release merely because the package version remains unchanged.

## Public publication later

Public publication is a separate decision. Choose and include the client license,
update onboarding for the public service, qualify the installed package against
that environment, and begin distinct versions for every released package. Enable
PyPI Trusted Publishing with a protected release environment then. Nothing in
this private workflow publishes to PyPI or changes repository visibility.
