# LeanWarp reference

## Commands

Run commands from the Lean project, or pass `leanwarp --project DIR`. Every
command prints one JSON object. `leanwarp COMMAND --help` lists its options.

A **workspace** holds the project's uploaded files and results on LeanWarp. A
**worker** is the server that runs Lean for it. Each upload makes a new
**revision**.

| Command | What it does |
| --- | --- |
| `auth login [--no-browser]`, `auth logout` | Open the dashboard to create a key, then save the key you paste. `logout` forgets the key on this machine. |
| `setup [--agent NAME] [--no-mcp]` | Install the skill and MCP server for the agents installed here: Claude Code, Codex, Cursor and `~/.agents`. No key needed. |
| `setup --remove [--agent NAME]` | Remove what `setup` recorded installing in `~/.config/leanwarp/setup.json`, and only what is still as it wrote it: a changed file or entry stays and is reported `modified`. Without a record, nothing is removed. |
| `update` | Upgrade `leanwarp` to the latest release and refresh the skills `setup` installed. |
| `doctor` | Show the project's toolchain, its environment and whether it runs, and summarise the upload. Starts no worker. Before you sign in, it checks the toolchain against the supported ones. |
| `files [--dependencies]` | List the workspace Lean files that would upload, their sizes and any problems. `--dependencies` adds the path-dependency files `connect` sends, with their sizes. No key needed. |
| `connect [--timeout SECONDS] [--retry] [--max-spend USD] [--ephemeral]` | Build the project's environment once, wait until it is ready, and create the project's workspace. `--retry` builds again after a failure you fixed. The default wait is four hours. If it expires, run `connect` again to rejoin the build. `--max-spend` caps workspace use; a new environment build uses account credit separately. `--ephemeral` makes it this run's alone, for `stop --delete` to delete. |
| `connect --workspace ID` | Connect the project to an existing workspace instead, such as one from another machine. Reports what the next sync uploads or deletes, and the files only the workspace has (`remote_only`), which stay. |
| `check FILE [--draft] [--retention short]` | Compile a file and report Lean's errors and warnings. |
| `check --all [--draft]` | Check every file that uploads, in batches of up to 64, as one result. Waits until the last batch finishes. |
| `inspect FILE --line N --column N [--retention short]` | Show the goals and hypotheses at a tactic or proof hole. An outside or ambiguous position returns `cursor_outside_proof` or `ambiguous_cursor_capture`. |
| `try-tactics FILE --line N --column N --tactic T [--tactic T …] [--retention short]` | Try tactics without editing the file. |
| `verify FILE --declaration NAME --target STATEMENT [--context-file PATH]` | Check that a declaration proves exactly the given statement. |
| `holes FILE [--no-goals]` | List every `sorry` and `?_` with its position, and the goals of the first 16. |
| `proof-states FILE [--cursor N] [--max-bytes N]` | List every tactic in the file with its goals before and after, one page at a time. Pass a page's `next_cursor` as `--cursor` for the next. |
| `suggest FILE --line N --column N [--tactic T …] [--tactic-timeout MS]` | Find a tactic that closes the hole at a position. Never edits the file. |
| `apply` | Write the latest `suggest`, `sorry2lemma`, `have2lemma`, `simplify-proofs` or `repair-proofs` result into its file and upload it. |
| `sorry2lemma FILE [--max N] [--prefix NAME] [--strict-final-check]` | For each theorem that still has a `sorry`, move its proof into a named lemma, and make the theorem use that lemma. Never edits the file. |
| `have2lemma FILE [--max N] [--prefix NAME]` | Move each named `have` in a theorem's proof into its own lemma, which the `have` then uses. Never edits the file. |
| `simplify-proofs FILE [--tactic T …] [--tactic-timeout MS] [--max N] [--min-saving BYTES]` | Replace theorem proofs with shorter tactic proofs that still check. Never edits the file. |
| `repair-proofs FILE [--tactic T …] [--tactic-timeout MS] [--max N]` | Try to repair `sorry` holes within the time budget and repair limit. Never edits the file. |
| `declarations FILE` | List the declarations a file adds, with types and dependencies. |
| `check-files FILE [FILE …] [--draft] [--file-timeout SECONDS]` | Check up to 64 files in one operation. Strict unless `--draft`. |
| `verify-batch FILE.jsonl [--draft] [--proof-timeout SECONDS]` | Check up to 64 inline proofs in one operation. Strict unless `--draft`. Each line of the file is `{"custom_id": "...", "proof": "..."}`, optionally with `"target": {"context": "...", "statement": "...", "declaration": "..."}` to bind the proof to a statement. Pass `-` to read standard input. The proofs are not uploaded as files. |
| `wait [--timeout SECONDS \| --block]` | Wait for the latest operation's result. |
| `status` | Show the workspace, the latest operation and the workspace's use of each history limit. |
| `workspaces` | List your account's workspaces. `this_project` marks this project's workspace. |
| `export --output FILE [--workspace ID]` | Save every stored operation and result to a new file, as JSON Lines, newest first. |
| `cancel` | Cancel the latest operation. |
| `stop [--generation N \| --delete]` | End billing for the selected worker generation and request cleanup. Files are kept. `--delete` also cancels any running operation, then deletes the workspace `connect --ephemeral` created, and no other, and disconnects. It never deletes while a lost request cannot be resent: the request stays available for `recover`, and the response reports the last confirmed state and any errors. |
| `feedback --kind KIND --summary TEXT [--details TEXT] [--severity S] [--operation ID]` | Report a confirmed problem with LeanWarp itself to its team. The report names the latest operation. Free, and starts no worker. |
| `recover` | Resend a request whose response was lost. |
| `reconcile` | After another client changed the workspace, adopt its revision. Reports what the next sync uploads or deletes, and the new files only the workspace has (`remote_only`), which stay. |
| `prune [--confirm TOKEN]` | List the files only the workspace has. With the token from that list, delete them. This cannot be undone: ask the user first. |
| `disconnect` | Forget a stopped workspace, so the project can connect to another environment. |
| `delete --confirm ID` | Delete the named workspace with its files and results. Your credit is kept. This cannot be undone. |
| `environments`, `versions`, `resources`, `account` | List your account's environments, the supported Lean toolchains, the published worker's resources and pricing, or your credit. |
| `skill [--reference]` | Print the agent instructions or this reference. No key needed. |
| `mcp` | Run the MCP server for this project over stdio. |

Operations are the commands that run Lean: `check`, `inspect`, `try-tactics`,
`verify`, `holes`, `suggest`, `sorry2lemma`, `have2lemma`, `simplify-proofs`,
`repair-proofs`, `declarations`, `check-files` and `verify-batch`. Each first uploads changed files and removes deleted ones. They
accept:

- `--wait SECONDS`: how long to wait for the result, 0 to 40 (default 30).
- `--block`: wait until the operation finishes instead, for CI. The wait ends at
  the execution timeout (600 seconds unless set) plus five minutes to start a
  worker. `wait --block` does the same for the latest operation.
- `--execution-timeout SECONDS`: the operation's time limit on the server, 1 to 600
  (or up to the account's own limit, at most 1,800).

`verify --fresh` runs on a temporary worker. Otherwise, operations share the
workspace's worker, which keeps imports loaded between calls.

`check` is strict by default: `sorry` is an error, and the result is what a
final build would say. `check --draft` is faster while you edit. The worker
keeps the file open, and after an edit Lean reuses its work on the unchanged
part of the file. In a draft check, `sorry` is only a warning. So finish with a
full `check` or with `verify`.

`try_tactics` runs the supplied tactics in one race from the same captured
goal. The file stays unchanged.
A per-tactic timeout above 60,000 ms is capped at 60,000 ms.
The default tactic-trial limit is 30 seconds plus the sum of those
tactic budgets and 100 ms, rounded up to seconds and bounded to 120–600 seconds.
Eight 30-second tactics get 271 seconds. An explicit limit shorter than the sum
scales the per-tactic budgets down to fit the remaining time.
Results preserve the order of the supplied tactics. To see the path that
actually ran, read each result's `execution` fields.

`suggest` searches LeanWarp's ranked portfolio for a tactic that closes the
goal and passes a recheck. It draws on default tactics, the goal's premises
and what has worked before. Use `--tactic` to search your own alternatives. Without `--tactic-timeout`, each tactic gets
LeanWarp's learned time budget for its family.

A tactic that closes the goal is accepted only if it changes nothing else:

- Every other hole and every declaration stays as it was.
- The edit stays inside its own declaration, so a "tactic" cannot append a
  command.

Then a fresh recheck sets `verification`:

- `strict`: the whole file passes the strict check.
- `declaration`: other declarations still have holes, but this one is complete.
  By Lean's `#print axioms`, it depends only on `propext`, `Classical.choice`
  and `Quot.sound`. A proof through an unproved lemma or `native_decide` fails
  here. This is an editing aid: the audit runs in the same process as your
  source, so it is not a verdict. Only `strict` comes from the independent
  checker. For a verdict, run `check` or `verify_target` on the finished file.

A hole in a declaration that still has other holes is not closed, with the
reason `declaration_has_other_holes`: those holes contribute `sorryAx`
themselves, so no audit can vouch for this edit. Solve the remaining holes
together or manually separate them into lemmas before retrying. Both tiers use
the kernel's axiom audit, so a tactic that reaches `sorry` by any route,
including a macro, is refused.

When a search tactic such as `exact?` closes the goal, the patch holds the term
it found (`exact Nat.add_comm a b`), and `found_by` names the search. This way,
compiling the file doesn't repeat the search. If the found term does not pass,
LeanWarp checks the search tactic itself instead.

Preparing imports, the portfolio and its rechecks share the operation's time limit.

`apply` writes a result only onto the exact bytes it was computed from. Run it
straight after `suggest` or a file rewrite (`sorry2lemma`, `have2lemma`,
`simplify-proofs`, `repair-proofs`). To write the edit for the first time,
the result must:

- be this project's latest operation, and successful;
- have completed at the revision the project last uploaded;
- be for a file unchanged since then.

Each result names the SHA-256 of the source it read and of the source it
produces. `apply` also uploads other changed files, as `sync` would. It prints
`status: applied` with the new `revision`. If the file was written and the
upload response was lost, run `apply` again. It confirms the edit if the upload
landed, or finishes uploading it.

If `apply` reports a `revision_conflict`, your local files are preserved.
Check the file first. A conflict on an earlier pending upload can stop `apply`
before it writes the edit. Run `reconcile`, review what the next `sync` would
upload or delete, then run `sync` once you agree with those changes.

`apply` refuses a read-only or hard-linked file. It also refuses a result for a
file the operation was not asked about. The project records the files it
submitted in `.leanwarp/operation.json`. A result submitted by an earlier SDK
version has no such record, so run the operation again. Check the file
afterwards.

`apply` keeps a copy of each file it replaces in `.leanwarp/apply-backups` (the
printed `backup_directory`). The copy includes anything an editor wrote to the
old file. Each copy has a `RECOVERY.txt` naming its source. Copies are never
deleted automatically.

If an editor saves the file while `apply` runs, or `apply` is interrupted,
it leaves `.leanwarp/apply-recovery.txt` and refuses further applies and
uploads. Stop and ask the user to:

- close their editors;
- keep the version they want, saving it as a new file if the original is
  hard-linked;
- delete the marker.

Run the operation again if the source changed. `status` and `recover` report
this. They never choose between the two versions for you.

`sorry2lemma` moves the proof of each `theorem` or `lemma` that still has a
`sorry` into a generated lemma inserted before it. It rewrites the original to
call that lemma. The rest of the file must compile. The lemmas keep their
`sorry`, so prove each one next. Section variables are preserved in the
generated lemma application. A failed transformation leaves the source
unchanged.

`have2lemma`, `simplify-proofs` and `repair-proofs` rewrite proofs. The engine
keeps each edit only after a fresh check of the whole patched file. LeanWarp then
returns the rewritten file only if it has no errors, keeps every statement,
adds nothing but the lemmas `have2lemma` reports, changes no text between
declarations, and leaves every unchanged declaration referring to exactly what
it did. Each theorem `simplify-proofs` or `repair-proofs` edits must end
complete, using no axiom but Lean's standard ones, as for `suggest`: a theorem
that keeps another `sorry` is refused with `declaration_has_other_holes`.
Solve the remaining holes together or manually separate them into lemmas.
`have2lemma` moves proof text, so a new lemma may keep a `sorry` the theorem had.
Otherwise `status` is `rejected` and `reason` says which check failed.
`have2lemma` gives each new lemma the theorem's explicit parameters; a `have`
that uses an earlier local, or is unnamed, may be skipped or fail.
`simplify-proofs` keeps a shorter proof only if it saves at least
`--min-saving` bytes. `repair-proofs` keeps the first tactic the whole file accepts for
each eligible `sorry`. It leaves `?_` placeholders alone. Each attempt is bounded
by `--tactic-timeout`, or without it by its tactic's own budget (from 50 ms for
`rfl` to 2 seconds for `aesop`, `grind` and `nlinarith`). Holes outside the repair limit or time budget can be skipped. Tactics may not use `sorry` or
`admit`. Without `--tactic`, both try, cheapest first, those of `rfl`,
`norm_num`, `simp`, `omega`, `linarith`, `positivity`, `ring`, `simpa`,
`decide`, `trivial`, `constructor`, `assumption`, `exact True.intro`,
`nlinarith`, `aesop` and `grind` that the file's imports provide.

`check-files` runs each file as `check` would, on the workspace's warm (already
running) workers, within the operation's time limit. A file the limit does not reach
is `not_checked`, and the batch does not pass. Each file is promised its
`--file-timeout`, or 60 seconds without one, from when its imports have loaded;
a timeout over 300 seconds, or over `--execution-timeout` less 31 seconds, is
refused. A file that had its whole time and did not finish is `timeout`; one
that started with less and did not finish is `not_checked`, which a file on the
default 60 seconds can be under a shorter `--execution-timeout`.
A batch still running when its operation's time limit expires keeps the files
that finished; the rest are `not_checked`.

`verify-batch` checks inline proofs the same way, without uploading them as
files. Each `custom_id` must be unique and 1 to 128 characters long. The request
holds at most 256 KiB of UTF-8. A proof can import what the environment
provides, such as Mathlib, but not the project's own modules. Such a proof is
refused without running: `rejected`, with `failure.kind` `policy` and
`error.code` `unsupported_import`. Use
`check-files` for proofs that need the project's modules.

Without a target, a proof's `ok` means it compiles, its declarations pass Lean's
kernel and it uses no extra axiom: not that it proves what you meant
(`theorem x : True := trivial` is `ok`). A `target` fixes the statement: the
proof is checked as `verify` checks a file, against `statement` in `context`,
for the theorem `declaration`, and an accepted entry carries a `receipt`. A
true theorem of another statement is `rejected` with `failure.kind`
`target_mismatch`. A target needs a strict batch, and its context may import
only the environment's packages.

## Environments

Your project defines its environment: its `lean-toolchain`, its lakefile, its
`lake-manifest.json` (with `.lake/package-overrides.json` if present) and the
source trees of its path dependencies. `connect` sends those, never your
build outputs. A path dependency sends its files except what its own top-level
`.gitignore` and `.leanwarpignore` leave out, build outputs, version control
and credential files (`files --dependencies` lists them). A package URL or
lakefile URL with a credential in it is refused before anything is sent.
LeanWarp builds the dependency closure once for your
account: it fetches pinned public Git dependencies itself, uses the public
Mathlib cache when the closure is standard Mathlib, and waits until the
environment is ready. The same inputs reuse the same environment, so a second
`connect` builds nothing. LeanWarp never runs Lake on your machine.

A private dependency on GitHub is built like any other once LeanWarp has access.
Connect it in the dashboard, under Private repositories, with a GitHub account
that administers the repository; until then `connect` stops with
`grant_required`, and once access is connected `connect --retry` builds again.
If access is paused later, your workspaces, files and results are kept. A
private dependency hosted anywhere other than GitHub returns
`private_host_unsupported`.

A Git dependency on a repository on this machine is sent as Lake's checkout of it
in `.lake/packages`, which must be at the commit `lake-manifest.json` pins; run
`lake build` (or `lake update`) once if `connect` says otherwise. Path
dependencies Lake copies (`copy = true`) work as they do locally. A lakefile that
reads files outside the project gets what this machine holds there: `connect`
answers each read the build asks for, so let it run until it returns. A file
committed in its git repository is answered as that commit holds it; any other is
read from disk, and if it changes after the build read it, `connect` stops and
asks you to run it again or commit the file. Each dependency's default libraries
(what `lake build` of it builds) are in the environment; a check that imports a
module from one of its other libraries (tests, examples, documentation) is refused
with an error naming the module and its package. Outside the project it
answers only inside path dependencies (never what their ignore files leave out)
and the directories `.leanwarpread` lists
(one per line, relative to the project, such as `../shared`); it refuses any other
read with an error naming the file. Add a directory there only if the project's
build really needs it. Workers load the project from `lean-toolchain`, the
lakefile and `lake-manifest.json` alone, so a lakefile that reads any other file
when Lake loads it is refused before the environment is ready, with
`configuration_not_reproducible` naming the file; make the configuration depend
on those three files only.

| The project has | LeanWarp uses |
| --- | --- |
| A Lake project (lakefile, lockfile and toolchain) | That project's own environment |
| Plain `.lean` files and no lakefile | The newest supported Lean with Mathlib, or the `lean-toolchain` beside them |
| A toolchain LeanWarp does not support | Nothing; `leanwarp versions` lists the supported ones |

A lakefile without `lake-manifest.json` asks you to run `lake update` first.
If a build fails, the error names the package or file; fix it locally, then run
`leanwarp connect --retry`. `leanwarp environments` lists your environments and
their build status.

A workspace stays on its environment. If the project's toolchain, lakefile,
lockfile or path dependencies change, run `stop`, `disconnect` and `connect`
again. Engine updates never rebuild your environment.

## Results

Every operation result starts with `success` and `decided`. The operation
follows, as LeanWarp returned it:

| `success` | `decided` | Meaning | Exit code |
| --- | --- | --- | --- |
| `true` | `true` | The check passed, the proof was verified, inspection returned goal context, the tactic trial ran, a suggestion closed its hole, an extraction completed, or a listing (`holes`, `declarations`) completed, even of a file that does not compile yet. For `check-files` and `verify-batch`, every file or proof passed. | `0` |
| `false` | `true` | The operation did not succeed. For checks and verification, read the diagnostics or rejection reason. For inspection, read `goal_status` to see whether there were goals at the position. | `1` |
| `false` | `false` | No verdict: the check timed out, LeanWarp could not complete it, it was not checked, every tactic was skipped, or the operation failed or was cancelled. Retry when appropriate; do not change the proof because of it. | `4` |
| `null` | `null` | Still running. Run `wait`. Never submit it again. | `3` |

Every verification result (a check, a batch entry, a fixed-target
verification) has one `status`:

- Verdicts: `ok`; `error`, Lean's errors, in `diagnostics`; `rejected`, refused,
  with `failure.kind` `axioms`, `kernel`, `policy`, `target_mismatch`,
  `declaration_missing`, `target_elaboration` or `candidate_elaboration`.
- No verdict: `timeout`, the whole requested time ran out;
  `infrastructure_failure`, LeanWarp could not complete the check;
  `not_checked`, it never started, or started with less than its requested time
  and did not finish. `failure.kind` is `timeout`, `memory_limit`,
  `resource_limit` or `internal`, and `failure.retryable` says whether running it
  again can succeed. Exceeding `maxHeartbeats` or `maxRecDepth` is a verdict:
  `error` for a proof, `rejected` with `target_elaboration` for a target.

Other exit codes:

- `1` from `doctor` when no environment matches, and from `files` when a file
  cannot upload.
- `4` when there was no verdict (`decided` is false): retry the work.
- `2` when a command could not run. Its JSON then has `error` (a stable code),
  `message` and `retryable`: true when running the same command again may
  succeed, with `retry_after` seconds when the service asked for a pause.

`check --all` prints one result for the whole project: `success`, `files`,
`passed`, `failed`, `not_checked`, every file's entry in `results`, and the
`operations` it ran. Its exit code is `0`, `1`, `3` or `4` as above. A batch that
outlives the wait leaves its files and the rest `not_checked` and is named in
`running`.

Any result, from the CLI or MCP, can also carry `update_available` (with
`current`, `latest` and `command`) when the service names a newer SDK release,
and `sunset`, the date it stops accepting this one, when it names that.
An SDK older than the service's minimum is refused with `sdk_upgrade_required`
until you update it with `leanwarp update`. An SDK installed from GitHub before
the PyPI package (`leanwarp-sdk`) has no `update`. Replace it with `uv tool
uninstall leanwarp-sdk && uv tool install leanwarp`.

What to read in `result.result`:

| Operation | Fields |
| --- | --- |
| `check` | `status` (`ok` when there are no errors), `diagnostics` and `cached`. |
| `inspect` | `goal_status` describes the selected state. `available` includes every goal and hypothesis. `no_goals` means a known empty tactic state. `omitted` counts the hidden goals in `context_omitted.goal_contexts`. `retained_proof_state`, when present, says whether the worker kept a proof state. It does not promise which tactic execution path will run. Also `selected_tactic` or `selected_hole`, `enclosing_declaration`, `premise_hints` and `context_omitted`. |
| `try_tactics` | `results`: one entry per tactic, in the order given, each with `tactic`, `outcome`, `status`, `diagnostics`, `elapsed_ms` and `execution`. Tactics that differ only in surrounding whitespace share one trial. Later positions report `execution: repeated` and zero additional `elapsed_ms`. `skipped` lists tactics that did not run, with a reason. |
| `verify_target` | `status` `ok` and `receipt.policy` `fixed_target_kernel_check_v2`. A rejection, a timeout or an infrastructure failure has `failure`. |
| `holes` | `holes`: each with `kind`, `token`, its exact semantic `state` (`resolved`, `sorry`, `unresolved` or `unknown`), `unfinished` and source positions. A filled `?_` is resolved independently of other holes in its declaration. `unfinished_count` counts confirmed `sorry` and `unresolved` placeholders. `unknown_count` is separate. The legacy `unfinished` boolean stays true for unknowns. False means positively resolved. `hole_count` counts all syntax placeholders, not remaining goals. `resolved_count`, `holes_omitted` and `goals_omitted` account for resolved entries and output limits. Goals (`target`, `hypotheses`) are returned only for the first 16 unfinished or unknown entries. `goal_status: omitted` means the context was not returned. `status: error` and `diagnostics` keep compilation errors. `diagnostics_complete` says whether elaboration finished. |
| `proof_states` | `rows` in command order, a tactic before those nested in it. Each has `index`, `kind` (the syntax kind; null with `kind_omitted` when too long), `source_range` (UTF-8 byte offsets, 1-based lines and columns; null when the tactic does not map back to the file), `occurrence` (how many earlier rows share the range: `t <;> s` runs `s` once per goal), and `before` and `after`, each with `status` `available` (with `goal_count` and `state`, an `id` in `states`), `no_goals`, `omitted` (a goal too large, with `goal_count`) or `unavailable`. `states` holds each distinct goal list once per page. `cursor`, `next_cursor` (null after the last page), `tactic_count` (every row), `commands_unavailable`, and `diagnostics`. A file with errors is listed as far as Lean elaborated it, with `status` `error`. `status` `unsupported`: the environment cannot list tactic states yet; use `inspect`. |
| `suggest` | Either `status` `closed`, with `tactic`, `found_by` (when `tactic` is what a search such as `exact?` found), `verification` (`strict` or `declaration`), `patch` (`start_utf8_offset`, `end_utf8_offset`, `replacement`), `source_sha256` and `patched_source_sha256`. Or `not_closed`, with `tried`, `candidates`, `rejected_by_recheck`, `rejections` (each closing `tactic` the recheck refused, with its `reason`, listed below) and `exhausted` (`portfolio` or `budget`). `portfolio` is `leanwarp` or `caller`. |
| `extract_decls` | `declarations` written in the file, not those Lean generates, such as recursors. Each has `name`, `kind`, `type`, `local_dependencies`, `dependencies` (up to 128, then `dependencies_omitted`), `level_params`, `is_unsafe`, `is_partial` and the source range. `declarations_omitted` counts any beyond 512 or beyond the result's size. |
| `sorry2lemma` | `status` (`extracted`, `partial`, `unchanged`, `no_targets` or `failed`), and `targets`, each with `declaration_name`, `generated_lemma_name` and `status`. When `source_changed`, also `transformed_source` with its `transformed_source_sha256`. Then `final_check` and, if requested, `strict_final_check`. A transformed file too large to return gives `status` `error` with `error.code` `result_too_large`. |
| `have2lemma`, `simplify_proofs`, `repair_proofs` | As `sorry2lemma`, without `final_check`: `status` (`have2lemma`: `extracted`, `partial`, `unchanged`, `no_targets` or `failed`; `simplify_proofs`: `simplified` in place of `extracted`; `repair_proofs`: `repaired`, `partial`, `no_targets` or `failed`), or `rejected` with `reason` (`file_has_errors`, `changes_declarations`, `changes_dependencies`, `edit_outside_declaration`, `declaration_has_other_holes`, `uses_unproved_or_extra_axiom`, `axiom_audit_unavailable`, `too_many_declarations`, `recheck_incomplete`). Counts `extracted`, `simplified` or `repaired`, with `failed` and `skipped`; `simplify_proofs` also has `bytes_saved`. Each target has its `status` and `replacement` when accepted; `have2lemma` names `declaration_name`, `have_name` and `generated_lemma_name`; `simplify_proofs` names `declaration_name`; `repair_proofs` gives the hole's `kind` and `range`. A `timeout_seconds` too short for one attempt with every tactic gives `status` `error` with `error.code` `budget_too_small`. |
| `check_files` | `status`: `ok` when every file passed, `failed` when Lean or the checker refused at least one, otherwise `incomplete`. `passed`, `failed` (`error` and `rejected`), `timed_out`, `infrastructure_failures` and `not_checked` count every file once. `results` in the order given, each with `file`, `status`, `diagnostics`, `elapsed_ms` and, for `rejected`, `timeout` and `infrastructure_failure`, `failure`. A file whose imports could not be prepared has `phase` `dependencies`. A file LeanWarp could not finish is `infrastructure_failure`; the other results still count. |
| `verify_batch` | As `check_files`, with `strict`, and each result has `custom_id` instead of `file`. An accepted entry with a `target` also has its `receipt`. A proof whose imports were refused or could not be read has `phase` `dependencies`. |

A `suggest` rejection `reason` is one of:

- `uses_sorry`: the tactic is or contains `sorry` or `admit`.
- `changes_other_holes`: the edit removed or added another hole, such as a new `?_`.
- `changes_declarations`: the edit added, removed or changed a declaration.
- `edit_outside_declaration`: the edit reaches past the declaration it is in.
- `uses_unproved_lemma`: the proof cites a lemma that still has a `sorry`.
- `uses_unproved_or_extra_axiom`: the proof relies on `sorry` or an axiom beyond
  `propext`, `Classical.choice` and `Quot.sound` (for example `native_decide`).
- `new_errors`: the file has an error it did not have before.
- `axiom_audit_unavailable`: the engine could not supply structured,
  kernel-checked axiom evidence for the exact declaration. This does not mean
  the proposed tactic is invalid.
- `recheck_incomplete`: the recheck timed out or did not finish. The tactic may
  still be correct. Add it by hand and check the file.

A recheck that runs out of the operation's time ends the search with
`exhausted: budget`. It does not count as a rejection.

A trial's `outcome` is `closed_proof`, `open_proof_state` (the tactic ran and
goals remain, shown in its diagnostics), `tactic_error`, `timeout` or
`rejected`. An unavailable context doesn't mean a goal is solved. A tactic that
succeeds in a trial isn't a proof until the file checks with it. Engine errors,
such as a position outside any proof, arrive as `status` `error`, with an
`error` object holding `code` and `message`.

Inspection also works in completed proofs. In the following file, run
`leanwarp inspect Main.lean --line 2 --column 3` to see the goal and
hypothesis before `exact h`.

```lean
example (h : True) : True := by
  exact h
```

A tactic selection includes its states in source order. A recovered draft
hole can carry a retained proof state. For either selection, use
`goal_status` to distinguish complete goals, a known empty state, and
omitted or unavailable goals.

### How it ran

Completed operations also carry `execution`, which says what LeanWarp reused,
and `timing`, which says where the time went. Only the facts that apply appear.

| `execution` field | Values |
| --- | --- |
| `worker` | `warm`: the workspace's running worker. `started`: a new worker started for this operation. `temporary`: a separate worker that stopped afterwards. |
| `imports` | `reused`: the file's imports were already loaded. `extended`: loaded imports were extended. `loaded`: imported now. |
| `document` | Draft checks only. `incremental`: Lean reused its earlier work on the file. `full`: Lean processed (elaborated) the file from the start. |
| `restarted` | Present only when the operation was interrupted and started again. `count`: how many times. `reason`, for the last one: `release` when a LeanWarp update moved it, `worker_lost` when its worker or server stopped responding. |
| `trials` | Distinct tactic trials, counted by how they ran. Repeated inputs in the same request count once. `proof_state`: from Lean's captured proof state. `document_state`: replayed from the captured document. `source_recheck`: the file was rechecked with the tactic in place. `cached`: an identical earlier trial. |

`timing` has `queued_ms`, `worker_ms` (getting a ready worker, including any
start), `upload_ms` and `run_ms`. Parts of `run_ms` appear when Lean timed them:
`elaboration_ms` (checks, including `imports_ms` when import loading was timed), and `proof_state_ms` and `tactics_ms` (tactic trials). `queued_ms` is
left out when the operation was recovered after a failed attempt. Trials also report
their own `execution` and `elapsed_ms`.

Each result names the operation ID and source revision it belongs to. The SDK
rejects a result that doesn't match the operation the project submitted.
`result.generation` identifies the worker that ran it. A fresh or resized
verification runs on its own worker.

### Verification policy

Every accepted strict check and fixed-target verification
(`fixed_target_kernel_check_v2`) comes from LeanWarp's checker, a separate
process that never runs your source. Lean elaborates the source in a worker; the
checker re-checks with Lean's kernel every declaration it added, against a
trusted base of the environment's toolchain and packages, which your workspace
cannot change. Nothing your source does at elaboration time reaches the verdict,
so macros, custom elaborators and options are all fine in the proof: it is
accepted only if its kernel terms check.

For a fixed target, the statement is elaborated independently of the candidate,
with only the context you supply, so helper declarations in the candidate file
can't change what the target means. Supply the target's imports and definitions
with `--context-file` (MCP: `target_context`); it may import only the
environment's packages, such as Mathlib, never your project's modules
(`policy`). Declaration names must be ASCII.

Every declaration's axioms are audited. Only `propext`, `Classical.choice` and
`Quot.sound` are allowed. `sorry`, `admit`, `native_decide`'s compiler axioms
and any other axiom are rejected (`axioms`), including ones reached through
imports. A declaration installed without kernel checking is rejected
(`kernel`). A rejection is not permission to weaken the statement.

The receipt records hashes and environment identities for the check. It is not
a signed, portable certificate. Lean's kernel, the checker and the environment's
dependencies are trusted.

## MCP

Add the server to your client:

```json
{
  "mcpServers": {
    "leanwarp": {
      "command": "leanwarp",
      "args": ["--project", "/absolute/path/to/lean-project", "mcp"]
    }
  }
}
```

If the client can't find `leanwarp` on its `PATH`, use its absolute path. The
server reads the key saved by `auth login`, or `LEANWARP_API_KEY`. It starts
without a key. Tools that need a key then say how to sign in. The resources
`leanwarp://guide` and `leanwarp://reference` serve the agent instructions and
this reference without a key. The CLI and MCP share the project's `.leanwarp/`
state. Commands on one project take turns: each waits up to 10 seconds for the
one before it, so tools can be called in parallel.

Tools: `doctor`, `files`, `environments`, `connect`, `check` (`draft`),
`inspect`, `try_tactics` (these three take `retention`), `verify_target`
(`file`, `candidate_declaration`, `target_statement`, `target_context`),
`holes`, `suggest` (`tactics` optional), `apply`, `sorry2lemma`, `have2lemma`,
`simplify_proofs`, `repair_proofs`, `extract_decls`, `proof_states` (`cursor`; full
detail by default), `check_files` (`files`, `draft`),
`verify_batch` (`codes`, `draft`), `wait`, `status`, `cancel`, `stop`,
`recover`, `reconcile`, `disconnect`, `workspaces`, `history` (one page; pass
`next_cursor` for older ones), `delete` (`workspace_id`), `account`, `resources`
and `feedback` (`kind`, `summary`, `details`, `severity`, `operation_id`).

`connect` accepts `workspace_id` to connect to an existing workspace. Operation
tools wait up to `wait_seconds` (default 20, at most 40) and `wait` up to
`timeout` (default 30, at most 40). Like the CLI, they lead their result with
`success`, and every one returns `operation_id`. `wait`, `status` and `cancel`
act on the latest operation, or on the `operation_id` you pass.

Operation tools, `wait`, `cancel`, `recover` and `history` return a compact
result by default: `success`, `operation_id`, `kind`, `state`, `revision`, any
`error_code` and `error_message`, `counts` of diagnostics by severity, and
`result` with errors first and long lists and text shortened. `omitted` maps
each shortened path to how many entries or characters were left out. Pass
`detail: "full"` for the whole operation. `status` gives only an operation's
state, never its result. `feedback` is not offered when `LEANWARP_FEEDBACK=off`.

## Python

Install the library in your application's environment:

```sh
uv add leanwarp
```

Without uv, use `python -m pip install leanwarp`. The CLI's isolated install
doesn't make the library importable in your application.

```python
from leanwarp_cloud import OperationOutcome, ProjectSession, load_client

with load_client() as cloud:
    project = ProjectSession(cloud, "/path/to/lean-project")
    project.connect()
    result = project.submit_and_wait(
        "verify_target",
        {
            "file": "LeanWarpExample.lean",
            "candidate_declaration": "add_zero_example",
            "target_statement": "∀ n : Nat, n + 0 = n",
            "execution_mode": "reusable",
        },
        wait_seconds=30,
    )
    if not OperationOutcome(result).terminal:
        result = project.wait(timeout=300)
    project.stop()
    if not OperationOutcome(result).verified:
        raise RuntimeError("the proof was not verified", result)
```

`load_client()` reads the same key as the CLI. Like the CLI, `ProjectSession`
keeps the workspace, revisions and any unfinished request in
`.leanwarp/session.json`. `connect(timeout=..., retry=True)` takes the CLI's
`--timeout` and `--retry`; when its wait ends first it raises `ConnectTimeout`,
and calling `connect()` again rejoins the build. Closing the client doesn't
stop the worker, so call `stop()`.

`project.apply()` writes the latest `suggest` or file-rewrite result, as the
CLI does. With the lower-level client, `leanwarp_cloud.edits.edited_source(kind,
result, source)` returns the edited file. It raises `EditError` unless the
source and the result's checksums match.

`project.submit_and_wait("verify_batch", {"codes": [...]}, wait_seconds=...)`
checks up to 64 inline proofs, each `{"custom_id": ..., "proof": ...}`, in one
operation. The lower-level `LeanWarpCloud.verify_batch(workspace_id, codes,
expected_revision=...)` does the same. Results keep each `custom_id`, in order.

For a whole dataset, `LeanWarpCloud.verify_many(items, output="verdicts.jsonl",
environment_id=...)` verifies every item, `{"id", "proof", "target":
{"context", "statement", "declaration"}}`, against its own statement. It sends
chunks of up to 64 over `workspaces` new workspaces (4 by default) and writes one
JSON line per verdict as it arrives. `client.toolchain_environment()` returns
the ready Mathlib environment; a project's own is listed by `leanwarp
environments`. As with `verify_batch`, proofs and targets may import the
environment's packages, such as Mathlib, but not a workspace's own modules.
Items need a `target` unless `require_target=False`.

Run it again with the same `output` and `environment_id` to resume. Items
already written are skipped. A chunk that was running is recovered from the
`.journal` file beside the output: if the journal names its operation, the
result is read back while LeanWarp still holds it; if the request's answer
never arrived, the exact request is sent again to the same workspace, within 6
days of its journal entry. A chunk that can be recovered neither way (its
result was removed, its workspace is gone, its items changed, or the 6 days
have passed) runs again only with `rerun_lost=True`. A resumed run must
give each written item the same proof, target and settings.

`AsyncLeanWarpCloud` offers every `LeanWarpCloud` method as a coroutine, with
the same arguments, retries and deadlines; calls made together run together, up
to `concurrency` (16 by default) at a time.

`project.feedback(kind, summary, details=..., severity=...)` reports a problem
with LeanWarp itself, naming the project's workspace and latest operation.

`project.check_all()` checks every file, as `check --all` does.
`connect(ephemeral=True)` and `stop(delete=True)` are `connect --ephemeral` and
`stop --delete`. `wait`, `status` and `cancel` take `operation_id=`.

`LeanWarpCloud(api_key)` is the lower-level HTTP client, for applications that
manage their own state. Before each create, sync or submit, save the request and
its idempotency key. After an uncertain response, resend exactly that request
with the same key. See the [HTTP
API](https://github.com/Isagoge-Labs/LeanWarp-SDK/blob/main/docs/http-api.md).

For many workspaces at once, `LeanWarpCloud.wait_operations(ids, wait_seconds=20)`
waits on up to 100 operations in one request and returns as soon as any ends:
`{"operations": [...], "unread": [...]}`, where `unread` names finished operations
to read with `operation(id)`. Free storage as you go with
`delete_operation(id)` once you have read a result, or pass `retention="short"`
to `check`, `inspect`, `try_tactics`, `submit` or `run` so history may remove the
result a minute after it finishes, with no delete.

The client resends reads, generation-scoped stops, `cancel`, `delete` and writes with an
idempotency key after `429`, `502`, `503`, `504` or a lost connection. It waits
between attempts with jittered backoff, or as long as `Retry-After` asks, until
the call's `deadline` or, without one, for a minute (15 seconds under MCP).
`LeanWarpCloud(retries=N)` also caps the number of resends; `0` sends each
request once. `wait()` returns only a finished operation: anything transient is
a missed poll, and it raises `OperationTimeout` only when its `timeout` passes,
or a definitive error such as `not_found` or `authentication_required`.

Every exception the SDK raises is a `LeanWarpError`, exported from
`leanwarp_cloud`, with `code`, `status` (the HTTP status, or `None`), `message`,
`retryable`, `retry_after` and, for a write, the `idempotency_key` it was sent
with. `LeanWarpCloudError` is a refusal from the service,
`LeanWarpTransportError` a request with no usable response, and
`OperationTimeout` a wait that ran out. `SessionError`, `UsageError`,
`ProjectError`, `ConfigurationError` and `EditError` are local. Every argument
the SDK rejects, before anything is sent, raises `UsageError`, which is also a
`ValueError`.

`logging.getLogger("leanwarp")` gets one debug line per attempt: method,
resource, status, attempt and latency. It never logs a key, token, source or
proof. When the service names the date it stops accepting this SDK release, the
library emits one `DeprecationWarning` per process.

## Recovery

| Situation | What to do |
| --- | --- |
| A create, upload or submit response was lost | Run the next command. It first resends the saved request, with the same key, even if files changed since; running the same operation again returns the one that resend started. `recover` resends it alone. |
| A file can't be uploaded, or the upload is too large | Run `files` to see every problem. Then exclude unrelated files in `.leanwarpignore`. |
| `success` is `null`, or `wait` timed out | Run `wait` again. To abandon it, run `cancel`, then `wait` until it ends. |
| Another LeanWarp command is using the project | Commands wait up to 10 seconds for it; after that the error is `project_busy`. Let it finish. |
| `revision_conflict` from another machine | Decide with the other writer which files to keep. Then run `reconcile`: it adopts the workspace's revision and lists what the next sync uploads over theirs or deletes. Don't delete `.leanwarp/` to force it. |
| The key was rotated, or lacks a permission | Sign in with a key for the same account that has the permission, then run the command again. `stop` works meanwhile. |
| The toolchain or lockfile changed | `stop`, `disconnect`, then `doctor` and `connect`. |
| `.leanwarp/session.json` is damaged | Keep it. It may record an unfinished request. |
| Not enough credit, or a spending cap reached | Add credit or raise the cap in the dashboard, then retry. |

Submitting an operation is one request: changed files travel with the
operation. Every create, upload and submission is saved in `.leanwarp/` before it
is sent, so a lost response never runs anything twice.

- If `recover` returns an `operation_id`, the operation was submitted. Wait for
  it.
- If it returns only a workspace and revision, the upload or creation finished.
  Run the intended command again.
- With nothing to recover, `recover` returns the same view as `status`.

A plain `stop` is sent once. If the reply is lost, run `status` to see which
generation is active. To retry a stop for a known generation, use
`stop --generation N` (or `generation=N` in Python and MCP).
A `stopping` reply confirms billing has ended for that generation. Cleanup
continues until the workspace reports `stopped`. A saved request stays
available for recovery.

The project's connection lives in its `.leanwarp/` folder. A clone of the
project on another machine can create its own workspace with `connect`. Or it
can share one with `connect --workspace ID` (IDs from `workspaces`). Connecting
to an existing workspace takes its current files as the baseline, so the next
sync uploads only what differs and leaves files you don't have in place, as in a
partial checkout. The two copies then share one revision history, and `reconcile`
resolves a `revision_conflict` between them.

`disconnect` needs a confirmed `stop` and no queued or running operation.
`delete` refuses while work is queued or running. Deleting this project's own
workspace also disconnects the project and discards any request awaiting
`recover`. Run `export` first to keep its results. You can also stop or delete
a workspace in the dashboard.

## Credentials, credit and limits

`auth login` saves the key in `$XDG_CONFIG_HOME/leanwarp/credentials.json`
(normally `~/.config/leanwarp/credentials.json`), readable only by you.
`LEANWARP_API_KEY` takes precedence. The key decides which LeanWarp service is
used, so there is no URL to configure. `auth logout` forgets the saved key. To
disable the key, revoke it in the dashboard.

Workers are billed per second while they run, including idle time. `resources`
reports one published `worker`: its starting minute quote, `usage_based`,
resource limits, startup credit hold and idle timeout. LeanWarp selects the
worker. You can set a spending cap when you connect, or in the dashboard.
Existing workspaces keep their saved billing terms.

LeanWarp is in beta. For more capacity, larger workers, higher limits or a custom
setup for your team, email contact@isagoge.in or tell us with `leanwarp feedback`.

While a worker runs, the service holds (reserves) credit. It returns the unused
part once shutdown and final usage are confirmed. A stopped workspace can keep
reserved credit until that billing cleanup finishes.

Time a worker spends starting is not charged; charging starts when it is ready.
When LeanWarp's own systems raise a failure (its storage, no capacity to start
a worker, or a LeanWarp update stopping the worker), that worker
time is credited back automatically at the worker's average charged rate, never
more than it charged and up to $20 per operation; `account` shows the total.
Other failures can be reviewed for a credit on request, and after an incident on
LeanWarp's side it credits affected accounts itself.

`leanwarp connect --max-spend 2` creates a workspace capped at $2 in total,
including reserved credit. USD amounts allow up to six decimal places, without
rounding. Python accepts `max_spend_microusd=2_000_000` on `connect`,
`create_workspace` and `create_workspace_for_project`. MCP `connect` accepts
`max_spend_microusd` in the same units. The allowed range is zero to
$1,000,000.

If you omit the cap, a first connection creates an uncapped workspace, and a
reconnect keeps the existing cap. A reconnect with a different cap is rejected.
Change an existing cap in the dashboard. A cap of zero blocks workspace compute.
Building a new environment is billed separately from the workspace.

When `usage_based` is true, CPU and RAM charges rise independently above their
reserved baselines. The starting rate remains a paid minimum while the worker
runs, including idle time. Bursting depends on available
host capacity. When `usage_based` is false, the quoted minute rate applies for
the time the worker runs.

A worker starts with the first operation. It stays warm (running) between
operations until the idle timeout, its maximum lifetime or `stop`. Closing the
SDK client only closes its HTTP connection. It does not stop the worker or
billing. `stop` discards in-memory state but keeps uploaded files and completed
results. The next operation rebuilds the worker without another upload.

A cap change applies to future worker starts and elastic lease renewals. If the
next credit hold cannot fit, the worker stops, and an unfinished operation
reports `leanwarp_budget_exceeded`.

`account` reports `balance_microusd`, `reserved_microusd` and
`available_microusd` as strings ($1 = 1000000).

| Limit | Value |
| --- | --- |
| Upload | 256 `.lean` files and 1 MiB per project, including paths |
| Module paths | ASCII letters, digits and `_`; not under `Init`, `Lean`, `Lake`, `Std` or `Mathlib` |
| Statement context | 128 KiB |
| Inline wait | 0 to 40 seconds (`wait` and `--block` can wait longer) |
| Operation time limit | 1 to 600 seconds\* |
| Operations per workspace | One at a time |
| Operation history | The newest 10,000 operations per workspace\* |
| File sync history | The newest 10,000 syncs per workspace\* |
| Stored results | 512 MiB per account, across workspaces\*. Each queued or running operation reserves its request and 1 MiB |
| Stored source | 64 MiB per account, across workspaces\* |
| Workspaces | 100 per account\*, and 500 created an hour\* |
| Feedback | 60 requests an hour and 300 a day per account, repeats included. Of these, 20 an hour and 100 a day may be new reports. A summary of 8 to 200 characters, and up to 4,000 characters of details. Reports expire 180 days after their last repeat. At most 500 are kept per account, and they are deleted with their workspace |
| Active API keys | 100 per account\* |

\* The default. These limits are per account, and an account's own limits may be
higher. Email contact@isagoge.in to raise them. `status` reports the account's
history limits.

History is a window, not a lifetime allowance. When a new operation or sync
would pass a history limit, LeanWarp removes finished operations and sync
records that finished more than an hour ago (a minute for an operation with
short retention), soonest removable first, but only if that makes enough room.
Otherwise nothing is removed, and the request reports the limit with HTTP
`429` and `Retry-After`, the seconds until room frees; the client resends after
it, within the call's deadline. LeanWarp never removes queued or running work, each workspace's newest
operation and sync, or your uploaded files.

Until history removes an operation, at least an hour after it finished (a day
for `check-files` and `verify-batch`, a minute for short retention), retrying its request with the same idempotency key returns
the original result. Later, a removed operation reads as `not_found`. For at
least 7 days, resubmitting it returns `operation_expired` without running it
again.
Keep results you need with `export`. `status` reports `history_usage`: each
limit's `used` and `limit`.

To free storage sooner, delete a finished operation's result once you have read
it (`DELETE /operations/{id}`). Resubmitting that request returns
`operation_expired`; see the [HTTP
API](https://github.com/Isagoge-Labs/LeanWarp-SDK/blob/main/docs/http-api.md#releasing-results).

A search loop that submits many `check`, `inspect` or `try_tactics` operations
can skip the delete: submit them with `retention="short"` (`--retention short`
in the CLI, `retention` in MCP, `"retention": "short"` over HTTP). Each result is then eligible for removal about 60 seconds after it
finishes. That is not an erasure time: history removes results only when it
needs room, so a quiet workspace may keep one longer, and each workspace's newest
operation is kept. Until the result is removed, a resent request returns the
original; once it has been removed, `operation_expired`. Other kinds refuse
short retention.

Hidden directories, `.lake`, `lakefile.lean` and symlinks are never uploaded,
and symlinked Lean files are rejected. Lake scripts and dependency builds never
run.

Patterns in the project's top-level `.gitignore` and `.leanwarpignore` exclude
files, using git's syntax. `.leanwarpignore` is read last. So it can exclude
something git tracks, such as an `archive/` folder, or re-include a file git
ignores with `!path`. A file inside an excluded directory can't be re-included.
`files` lists exactly what would upload.
