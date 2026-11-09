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

- **Action does not expose `failure_kind` as a fourth output.** The Action
  publishes `verdict` / `reason` / `exit_code` and nothing else; external jobs
  match the prefix (section 1). A fourth output would be a second contract to
  keep in sync with the sentences, for a benefit no external user has asked
  for yet. Revisit only if (a) a prefix rename becomes unavoidable, or
  (b) downstream string-matching causes an actual incident. If adopted, derive
  it from `CheckOutcome.failure_kind` — never a second classifier.
