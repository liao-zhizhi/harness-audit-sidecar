# Release checklist

Short and mechanical. Anything that can be checked by running a command is
listed as a command, not as a reminder — a checklist item that depends on
someone remembering is the item that gets skipped.

## 1. The `reason` prefixes are a published contract

`failure_kind` (and every downstream `case` statement) keys on the first clause
of `reason`. These three strings are the wire contract:

| constant | current value | `failure_kind` |
|---|---|---|
| `SELF_AUDIT_REASON_PREFIX` | `self-audit failed` | `self_audit` |
| `MALFORMED_REASON_PREFIX` | `declaration malformed` | `declaration_malformed` |
| `NO_DECLARATION_REASON_PREFIX` | `no declaration given` | `declaration_malformed` |

`tests/test_check.py` asserts these literals against this file, so a rename in
`audit_sidecar/check.py` fails the suite until this table is updated — which is
the point: it forces the author to read this section before shipping.

Diff against the last released tag (replace `v0.10.1`):

```bash
git show v0.10.1:audit_sidecar/check.py \
  | grep -E '^(SELF_AUDIT|MALFORMED|NO_DECLARATION)_REASON_PREFIX'
grep -E '^(SELF_AUDIT|MALFORMED|NO_DECLARATION)_REASON_PREFIX' \
  audit_sidecar/check.py
```

**If any line differs, this is a breaking change, not a copy edit.**

1. Bump major (never ship a prefix rename in a patch or minor).
2. Put an old → new mapping table in the release notes, so a reader can update
   their `case "${reason}"` branches without diffing the source.
3. Say it in the release notes explicitly — in-repo tests go green when the
   sentence and the constant change together, so nothing inside this repo can
   detect that an external workflow stopped matching.

Adding a *new* kind is additive: keep the existing prefixes, add one, and
extend the table above. Renaming is never additive.

## 2. Tests

```bash
python3 -m unittest discover -s tests
```

Expect `OK` with the skip count stated in the release notes (7 today: the
`node`-dependent renderer tests). A green `-q` run alone is not enough — check
the skip count, or those tests did not run.

The classification pairing specifically, which no other test covers:

```bash
python3 -m unittest tests.test_check.FailureKindTests
```

That class asserts `(verdict, failure_kind)` pairs against real CLI output, not
prefix substrings: it is what proves a rewording did not move a failure into
another bucket.

## 3. Version strings

Three places, all of which move together:

```
pyproject.toml              version = "..."
audit_sidecar/__init__.py   __version__ = "..."
README.md                   status line and test count
```

## 4. Decisions deliberately deferred

Recorded here so a future release does not "fix" them without weighing the cost.

### The fourth Action output

**Action does not expose `failure_kind` as a fourth output.** The Action
publishes `verdict` / `reason` / `exit_code` and nothing else; external jobs
match the prefix (section 1). A fourth output would be a second contract to
keep in sync with the sentences. If adopted, derive it from
`CheckOutcome.failure_kind` — never a second classifier.

The signal to adopt it is **not** "someone renamed a prefix". It is what
external workflows actually condition on. Read the README? That tells you
nothing; read their YAML. Survey with:

```bash
python3 scripts/observe_branches.py path/to/workflows/
```

Then act on the counts, not on one sample:

| survey result | reading | action |
|---|---|---|
| `reason_prefix` | the documented path is being read and written | none |
| `verdict_only` | authors branch on `verdict`, which cannot separate the three INVALIDs | the real argument for the output; count before acting |
| `exit_code_only` | a gate that only wants to block | none |
| `reason_other` | bespoke `contains(...)` matching | check whether a published prefix would have worked |
| `fourth_output` | a workflow already assumes the output exists | adopt now — this is an incident, not a survey row |
| `no_branch` | nothing conditions on any output | the deferred decision is moot |

**A file count is not a count of decisions.** Every row above is reported on
three axes, and only the last one answers "how many people independently
decided this":

- `files` — how much YAML exists.
- `repos` — how many owners. Ten `verdict_only` files from one organisation's
  one template is one decision, not ten.
- `tmpl` — distinct branch skeletons (step ids and pinned refs normalised
  away). `files >> tmpl` means copies.

Only files that actually reference this Action are counted at all. `verdict` is
a generic output name: a workflow branching on `steps.x.outputs.verdict` while
calling **a different action entirely** is not weak evidence — it is no
evidence about this Action. Those files are printed with `NOT THIS ACTION` and
excluded. (One exception makes that bucket mixed; the next paragraph names it.)

**Attribution follows direct `uses:` lines only, so a wrapper is not counted.**
If a workflow calls `org/some-wrapper@v1` and that wrapper calls this Action
inside itself, the row reads `NOT THIS ACTION` even though adoption is real.
That case is *indirect* evidence, not zero evidence — and this tool cannot tell
the two apart. It is not followed for a deliberate reason: one hop of shell
alias is a text pattern, but one hop of `uses:` means fetching another
repository's `action.yml`, and a survey that clones arbitrary referenced repos
is a different, much larger tool. So the wrapper case is a **known blind spot**,
stated rather than guessed around. Read the `NOT THIS ACTION` rows in the first
batch by hand before concluding the count is low: a row whose `uses:` is an
action that wraps this one is adoption, and the number undercounts it. Revisit
only when a real wrapper sample exists — adding the hop then is a change with a
test, not a hunch.

### Reading the `NOT THIS ACTION` rows by hand

The tool cannot tell "a different action" from "a wrapper", so it does not
guess; the bucket is mixed and the reader separates it. The protocol, because
"read them by hand" without a criterion is just "look at them":

1. Filter to `uses_this_action: false` in the JSON.
2. **Do not read `action_refs: []` as "this workflow uses no action".** The
   field holds only refs that *match* this Action, so it is empty for every
   un-attributed row whether or not that row has a `uses:` line. Open the file
   and read its `uses:` lines; the JSON deliberately does not carry the others.
3. Look at the repository the `uses:` points at. If its name contains
   `harness-audit`, or it is a known wrapper, the row is indirect adoption.
4. If that cannot be decided from the name and the file, label it
   `unknown-wrapper` and **count it on no axis**. An undecided row is not a
   zero: putting it in the unattributed bucket and then reporting that bucket
   as "no adoption" is how a tool's blind spot becomes a finding.

If that bucket is large, the manual cost is the signal — the honest fix is the
tool that fetches the referenced `action.yml`, not a name-matching heuristic
pressed into service as evidence. A heuristic that guesses "this looks like a
wrapper" would put invented rows on an axis, which is the one thing this survey
must not do.

Record the provenance for the first batch, because the axis that matters is
ownership:

```bash
python3 scripts/observe_branches.py path/to/workflows/ --json > batch1.json
```

The JSON carries, per file: `repo` / `source` (`git-remote`, `git-toplevel`, or
`unknown` for a harvested folder that is not a repo), `action_refs` and
`action_versions` (**only refs that match this Action** — see the reading
protocol above), `class`, `conditioned`, `prefixes`, and `signature`. A repo
is reported as `unknown` rather than guessed from a directory name — inventing
an owner would manufacture the independence the count exists to measure.

Scope: a directory target is scanned for `.github/workflows` only, because that
is the only place GitHub runs a workflow and everything else found in a tree
(rule files, lockfiles, vendored docs) would report `NOT THIS ACTION` and bury
the real rows. If no such directory exists the scan falls back to every YAML and
says so on stderr — a flattened corpus must not report "nothing found", which
reads as "no adoption". `--all-yaml` forces the broad scan.

Two cautions, because a miscounted survey is worse than no survey:

- The scan is **line-level text**, not YAML parsing. It follows one alias hop
  (`reason="${{ ...outputs.reason }}"` then `case "${reason}"`), which is the
  pattern the README recommends; it does not resolve anchors, `env:`, or
  composite-action internals.
- It counts a reference as conditioning only when the same line compares it.
  Echoing an output into a log line is recorded under `echoed_only` and does
  not change the class — otherwise nearly every workflow would look like
  `verdict_only` and the signal would vanish into noise.

The fixtures in `tests/test_observe_branches.py` are hand-written to the shapes
above. They prove the classifier behaves as documented; they are **not**
samples of real external workflows, and no such corpus has been collected yet.
The first batch is the first evidence.
