---
name: sync-predbat-upstream
description: Update the custom Enphase Predbat fork or testing branch with official Predbat main while preserving local and remote feature commits.
---

# Sync Predbat upstream

Use the existing checkout. Defaults are branch `codex/enphase-local-api`, fork remote
`origin` (`https://github.com/chinedu40/batpred.git`), and official remote `upstream`
(`https://github.com/springfall2008/batpred.git`). Honour an explicitly requested feature-branch override.
Invocation authorises routine fetch, merge, conflict resolution, tests, commits and
normal pushes to that fork branch. Continue without another confirmation unless
actual ambiguity or a destructive action requires a user decision.

Never force-push, rebase, reset, discard dirty work, overwrite unrelated remote
URLs, push `main`, push to `upstream`, or deploy to Home Assistant.

## Inspect and preserve

1. Read `AGENTS.md` and applicable instructions. Inspect the current branch,
   tracked/untracked changes, local history and any merge/rebase in progress.
   Require a clean working tree and no unfinished Git operation before syncing.
   Do not stash or discard user changes. Finish already-authorised task changes
   first when appropriate; otherwise report the concrete obstruction.
2. Verify remote identities without printing credentials. Add a remote only when
   its name is absent; preserve existing Git proxy/authentication configuration.
   If an existing URL points elsewhere, investigate instead of replacing it.

   ```bash
   # Only when the corresponding remote is absent:
   git remote add origin https://github.com/chinedu40/batpred.git
   git remote add upstream https://github.com/springfall2008/batpred.git
   ```

3. Use a task-specific branch variable and logs outside the checkout. Check the
   branch name with `git check-ref-format --branch` and reject `main` as a target.

   ```bash
   sync_branch=codex/enphase-local-api  # replace only for an explicit override
   sync_stamp=$(date -u +%Y%m%dT%H%M%SZ)
   sync_logs="/tmp/predbat-upstream-sync-$sync_stamp"
   mkdir -p "$sync_logs"
   ```

4. Bound each network operation, for example with `timeout 120s` below. If that
   command is unavailable, use an available execution deadline instead of an
   unbounded retry. Check remote-branch existence before fetching it:

   ```bash
   timeout 120s git ls-remote --exit-code --heads origin "refs/heads/$sync_branch" > "$sync_logs/origin-head.log"
   ```

   Exit zero means present; exit 2 means absent; any other status is an access or
   network failure to diagnose. Do not treat a failed request as an absent branch.
   This checkout may fetch only `main` by default, so name both source and destination refs:

   ```bash
   # Run the origin fetch only when its branch exists:
   timeout 120s git fetch --no-tags origin "refs/heads/$sync_branch:refs/remotes/origin/$sync_branch"
   timeout 120s git fetch --no-tags upstream refs/heads/main:refs/remotes/upstream/main
   ```

   Do not add `+` or force flags to suppress a rejected ref update. Investigate
   unexpected rewritten history. Select the local target branch, or create it
   tracking the fetched feature branch when no local branch exists. If neither
   exists, establish where the feature commits are before creating a replacement.
5. Before any merge, save the target's full HEAD and create a unique local backup
   branch. Never move an existing backup ref:

   ```bash
   pre_sync_sha=$(git rev-parse --verify HEAD^{commit})
   printf '%s\n' "$pre_sync_sha" > "$sync_logs/pre-sync-head.txt"
   git branch "backup/predbat-sync-$sync_stamp" "$pre_sync_sha"
   ```

## Integrate without rewriting history

1. Compare local HEAD with the fetched origin feature branch. Preserve both sets
   of commits: skip an ancestor already included, fast-forward when possible, and
   merge diverged feature history with `git merge --no-commit "origin/$sync_branch"`.
   Resolve, validate and commit that merge using the checks below before starting
   another merge. Never replace local commits with the remote tip.
2. Merge the fetched official main into the feature branch:

   ```bash
   git merge --no-commit upstream/main
   ```

   Fast-forwards and already-current results are acceptable. For a nontrivial
   merge, keep the merge uncommitted until validation finishes. Resolve conflicts
   semantically, preserving upstream fixes and custom Enphase behaviour; never
   choose blanket `ours` or `theirs`. Follow `AGENTS.md`: run GitNexus impact
   analysis before manually changing symbols, report significant risk, and run
   `detect_changes()` before each commit. Do not silently skip required analysis.

## Validate the result

Inspect changed paths, conflict resolutions, staged and unstaged diffs, and
`git diff --check`. Reuse existing setup and the installed virtual environment.
Capture complete test/check output in files and preserve each command's exit
status; inspect logs afterwards rather than piping test output into a filter.

For Enphase changes, run the existing focused suites from `coverage/`, for example
`PREDBAT_KERNEL_REQUIRED=1 venv/bin/python -u ../apps/predbat/unit_test.py -k enphase`.
Also run affected configuration, control, installer or helper tests when those
paths change. Inspect the current registry instead of assuming historical counts.
For changes to shared runtime logic, run the complete `--quick` suite and confirm
the intended tests completed; distinguish exclusions and failures. Rebuild the
ignored native kernel before kernel-dependent checks if its source changed.

Run the current repository's applicable pre-commit checks against added/modified
paths. Review automatic fixes, re-stage intended files, rerun affected checks and
repeat GitNexus change detection before committing. Never disable checks or
assertions to obtain a pass. A no-op merge needs no repeated runtime suite; use
checks proportional to actual changes. Diagnose failures before claiming success.

## Commit core changes and refresh the add-on pin

Commit a validated pending merge with a clear merge message. If the result was a
fast-forward or no-op, do not create an empty commit. Once the working tree is
clean, check whether `ha_addons/predbat_enphase_local/source.json` exists. When it
does, record the resulting source commit before changing add-on metadata:

```bash
core_sync_sha=$(git rev-parse --verify HEAD^{commit})
addon_source_sha=$(python3 -c 'import json; print(json.load(open("ha_addons/predbat_enphase_local/source.json"))["ref"])')
git diff --quiet "$addon_source_sha" "$core_sync_sha" -- apps/predbat requirements.txt templates/enphase_local.yaml
```

Inspect that comparison: exit zero means the add-on's application source is
unchanged; exit 1 means the pin needs updating; other
statuses are errors to diagnose. Check that the pin is a full existing commit SHA
and the repository is `chinedu40/batpred`. For a source change, set `source.json`'s
`ref` to **`$core_sync_sha`**, bump the patch version in the add-on's `config.yaml`
and add an entry to its `CHANGELOG.md`. Pure add-on or skill changes do not require
a new source pin. Never pin to the later metadata commit or repeatedly advance the
pin to HEAD: the source commit must already exist, be clean and have passed the
application checks.

Run the add-on's unit tests, applicable pre-commit checks and available source/build
smoke checks; capture logs and preserve exit statuses. Inspect the metadata diff,
run GitNexus `detect_changes()` and commit the validated pin/version/changelog
changes separately before pushing. If this branch has no add-on package, record
that and skip this step. A no-op sync needs no metadata commit when the pin already
matches the application source.

## Publish and verify

Check the target branch and working tree again, then push normally:

```bash
git push origin "HEAD:refs/heads/$sync_branch"
```

If another writer wins the push race, explicitly fetch the feature ref again,
merge its new commits without rewriting history, inspect and retest affected
changes, recheck the add-on source pin against the resulting source, then retry
the normal push. Bound retries and report a persistent race.
Do not repeat unchanged failed pushes or use force.

Fetch the feature ref explicitly once more to verify the published result; the
checkout's limited fetch configuration may not update it automatically on push.
Require `git merge-base --is-ancestor upstream/main HEAD`, the same check for
`"$pre_sync_sha"`, `git rev-parse "origin/$sync_branch"` equal to local HEAD, and
a clean working tree. When the add-on exists, reread its pinned SHA and require
`git diff --quiet "$addon_source_sha" HEAD -- apps/predbat requirements.txt templates/enphase_local.yaml`
to succeed. Confirm the published branch includes both the validated source
commit and any subsequent add-on metadata commit; require
`git merge-base --is-ancestor "$addon_source_sha" HEAD` too. If official main
advanced during the task, report exactly which fetched SHA was incorporated;
sync again if the user requested the new tip.

Report the branch, preserved pre-sync SHA/backup ref, incorporated upstream SHA,
merge/conflict outcome, checks and log locations, pinned source SHA and add-on
version/checks when applicable, final commit, push verification, and any remaining
blocker. Do not imply that a Git push deployed Home Assistant.
