# Example: audit records from a single session (including one inflammatory storm)

`sample_trace.jsonl` was not written by hand. It was produced by the command shown below
and saved verbatim. It demonstrates the project's central claim: **the reply an agent
receives and the audit ledger may contradict each other, and both are correct for their
respective readers.**

The file holds two session blocks; fields are only ever added, never changed:

| Range | Version | Fields |
|:--|:--|:--|
| id=1..4 | v0.6.1 | `rule_id` / `rules_matched` / `rules_violated` |
| id=5..7 | v0.7.0 | the above + `tier` / `witness_scope` / `witness_mtime` / `immune_state` |

The first half demonstrates verdicts; the second demonstrates the **conditional immune
response** (see the "inflammatory storm" section at the end of this document).

## What the protocol layer saw (what the agent received)

```
{"jsonrpc": "2.0", "id": 1, "result": {"status": "ok", "isError": false, ...}}
{"jsonrpc": "2.0", "id": 2, "result": {"status": "ok", "isError": false, ...}}   <- disguised
{"jsonrpc": "2.0", "id": 3, "result": {"status": "error", "isError": true, ...}}
{"jsonrpc": "2.0", "id": 4, "result": {"status": "ok", "isError": false, ...}}   <- disguised
```

## What the audit ledger saw (`audit_trace.jsonl`)

| id | Call | What the fields mean | Verdict | Note |
|--:|:--|:--|:--|:--|
| 1 | `write_file target.py` | ✅ success / ✅ written | **PASS** | Honest call, rule satisfied |
| 2 | `write_file missing_dir/target.py` | ✅ success / ❌ no change | **FAIL** | Tool layer swallowed a path error, violating `no_file_drift` |
| 3 | `write_file target.py` | ❌ failure / ✅ written | **FAIL** | False failure: it was written but reported as failed |
| 4 | `run_command` (expected to modify `target.py`) | ✅ success / ❌ modified `wrong.py` | **FAIL** | Target drift |

**Three of the four calls are FAIL, yet the agent was explicitly told about a failure only
once (id=3).** For the other two it received `ok`.

## The added field: `rule_id`

This sample was produced with the judgment base (`rules/`), so every record carries three
extra fields:

```json
{
  "rule_id": "no_file_drift",
  "rules_matched": ["no_file_drift"],
  "rules_violated": ["no_file_drift"]
}
```

How to read them matters:

| Case | `rule_id` | Meaning |
|:--|:--|:--|
| **PASS** (id=1) | `null` | The verdict came from the four-valued grid and was not rewritten by a rule; `rules_matched` shows the rule **was checked and passed** |
| **FAIL, with a rule** (id=2) | `"no_file_drift"` | The rule itself decided it; `reason` carries `rule 'no_file_drift' violated` |
| **FAIL, no rule** (id=4) | `null` | No rule for that tool, so the grid decided |
| **INVALID, with a rule** | `"<rule_id>"` | The witness the rule required was never collected |

`null` does not mean "not checked". A non-empty `rules_matched` means the rule was loaded,
matched, and let the call through: a PASS with `"rule_id": null` and
`"rules_matched": ["no_file_drift"]` means "this rule read it and had no objection", not
"there was no rule".

**Why `rule_id` has to be recorded:** a verdict tells you the outcome, not **who** decided
it. The same `INVALID` may mean "no witness was collected at all" or "policy demanded a
different kind of witness" — two situations handled quite differently, and without
`rule_id` they cannot be told apart. Policy changes; the trace must not blur with it.

## Look at id=2 in particular

```json
{
  "id": 2,
  "tool": "write_file",
  "check_observed": true,
  "check_source": "protocol",
  "effect_observed": false,
  "verdict": "FAIL",
  "rule_id": "no_file_drift",
  "rules_violated": ["no_file_drift"],
  "reason": "rule 'no_file_drift' violated: expectation 'file_modified' not met (false completion: report says success, witness says no effect) (expected but untouched: missing_dir/target.py) [tool layer reported success, underlying command exited 1]",
  "missing_expected": ["missing_dir/target.py"],
  "exit_code": 1,
  "pipe_status": [1]
}
```

Three fields are worth reading together:

- `check_observed: true` — what the tool layer told the agent.
- `effect_observed: false` — what git and the filesystem actually saw.
- `exit_code: 1` — the command's own exit status. The tool layer saw 1 and replaced it with `ok`.

The clause `[tool layer reported success, underlying command exited 1]` in `reason` keeps
**both observations** on file. Had `check` been derived from `exit_code`, this record would
say `check: false` and the verdict would read "intent not realised" — **the same verdict by
coincidence, but the defendant would have changed**: the tool layer should have been
suspected, and instead the agent would have been.

## The inflammatory storm: id=5..7

id=5..7 is a second session block added in v0.7.0, demonstrating that **the same policy
costs different amounts under different immune states**:

| id | Call | `tier` | `witness_scope` | `immune_state` | Verdict |
|--:|:--|:--|:--|:--|:--|
| 5 | `write_file missing_dir/target.py` | tier2 (rule declares `high`) | `full` | `NORMAL` | **FAIL** |
| 6 | `append_file no_such_dir/log.txt` | tier1 (rule declares `low`) | `stat` | **`INFLAMED`** | **FAIL** |
| 7 | `append_file log.txt` | **tier2** (same `low` rule) | `full` | `INFLAMED` | **PASS** |

Three things are worth comparing:

1. **id=5 is the first failure**: danger count 1, below the threshold, `immune_state` still
   `NORMAL` — the state recorded here is the one **after this verdict**, and "not inflamed
   yet" is itself information.
2. **id=6 is the second failure**: two consecutive `FAIL`s, so the state becomes `INFLAMED`
   **after this record**. Note that it is itself still `tier1` — **a verdict does not change
   the cost of the call that produced it, only of the next one.**
3. **id=7 is the same `low` rule**, which should have taken tier1, but is promoted to tier2
   because the system is already inflamed, and `changed_files` carries a real git change
   set. **Inflammation closes the cheap route.**

This is readable straight out of `reason`:

```
id=6: ... (expected but untouched: no_such_dir/log.txt)
      [innate tier: existence and mtime only, no git change set]
      [tool layer reported success, underlying command exited 1]
id=7: witness matches intent, report agrees (rules satisfied: append_landed)
```

**Why a tier1 verdict must carry that clause:** a `PASS` resting on mtime and a `PASS`
resting on a git change set are assertions of different strength. A trace that does not say
which one was used turns every cheap green light into a claim of full verification — that
would be the real fabrication.

`witness_mtime` has a value only in tier1 (it is that tier's evidence); in tier2 records it
is `null`, because the evidence there is a change set, not a timestamp.

## Inflammation survives the process

These four records were produced in **one** process. The more dangerous case in practice is
the other one: the agent finds a way to crash the sidecar, and after a restart the ledger is
back to zero. So the state is persisted to `.immune_state.json` next to the trace:

```bash
# first process: two false completions -> INFLAMED persisted
cat "$D/.immune_state.json"
# {"danger_count": 2, "recovery_progress": 0, "state": "INFLAMED", "version": 1, ...}

# kill it, restart the same command: the next low-risk append still goes to tier2
```

`tests/test_immune.py::CrashRestartTest` verifies exactly this: two real subprocesses, the
second reading the first one's inflammation back from disk. To see the old amnesia for
yourself, add `--immune-state none` — after a restart that low-risk call is tier1 again.

## Why nothing is redacted

This file contains **no absolute paths** (verify with the command below, which prints 0):

```bash
grep -cE '/tmp/|/home/' examples/sample_trace.jsonl
```

That is not luck; it follows from how the fields are defined:

| Field | Path form |
|:--|:--|
| `changed_files` | repository-root relative (`git status` coordinates) |
| `missing_expected` | repository-root relative (mapped by `resolve_expected`) |
| `unexpected_changes` | repository-root relative |
| `intent` | echoes the client's `args.path` **verbatim** |

`rule_id` / `rules_matched` / `rules_violated` record rule names, which are independent of
the machine and contain no paths. `tier` / `witness_scope` / `immune_state` are enum values
and `witness_mtime` is a float timestamp; none of them contains a path.

**The one exception is `intent`**: it echoes the call arguments as given. The client in this
sample passed relative paths (`target.py`), so the whole file is clean. If your client passes
absolute paths, `intent` will contain local paths (of the form `<TEMP_DIR>/target.py`) —
that is not a redaction oversight but the price of recording the statement under audit.
Before sharing a trace, check it with the `grep` above.

> This boundary is recorded in the "Known boundaries" section of `README.md`.

## How to regenerate it

Run inside any temporary git repository (pointing `--cwd` at it):

```bash
D=$(mktemp -d) && cd "$D"
git init -q && : > target.py && : > other.py
git add -A && git -c user.name=T -c user.email=t@e commit -qm init

printf '%s\n' \
 '{"jsonrpc":"2.0","method":"tool_call","params":{"tool":"write_file","args":{"path":"target.py","content":"value = 1\n"},"reported_success":true},"id":1}' \
 '{"jsonrpc":"2.0","method":"tool_call","params":{"tool":"write_file","args":{"path":"missing_dir/target.py","content":"value = 2\n"},"reported_success":true},"id":2}' \
 '{"jsonrpc":"2.0","method":"tool_call","params":{"tool":"write_file","args":{"path":"target.py","content":"value = 3\n"},"reported_success":false},"id":3}' \
 '{"jsonrpc":"2.0","method":"tool_call","params":{"tool":"run_command","args":{"command":"printf \"x\" > wrong.py","expected_files_modified":["target.py"]},"reported_success":true},"id":4}' \
 | PYTHONPATH=/path/to/agent-harness-audit python3 -m audit_sidecar.mcp_server --cwd "$D" --quiet

cat "$D/audit_trace.jsonl"
```

Dropping `--rules none` yields the pure four-valued grid version, whose records carry none
of the three `rule_id` fields — both traces are legitimate; the difference is whether this
one can answer "who issued this verdict". By default `harness-audit-server` loads the
bundled `audit_sidecar/rules/` automatically (still present after `pip install`), so
reproducing this needs no path arguments.

The id=3 record has to be constructed with `reported_success:false`, because a real tool
layer does not lie about failure on its own — it covers the other side of the verdict
(false failure), not a reproduction of a real fault.

### The appended inflammation block (id=5..7)

Run three more calls in the same repository; the third is the one taken while `INFLAMED`:

```bash
printf 'session\n' > log.txt && git add -A && git -c user.name=T -c user.email=t@e commit -qm log

printf '%s\n' \
 '{"jsonrpc":"2.0","method":"tool_call","params":{"tool":"write_file","args":{"path":"missing_dir/target.py","content":"value = 5\n"},"reported_success":true},"id":5}' \
 '{"jsonrpc":"2.0","method":"tool_call","params":{"tool":"append_file","args":{"path":"no_such_dir/log.txt","content":"one\n"},"reported_success":true},"id":6}' \
 '{"jsonrpc":"2.0","method":"tool_call","params":{"tool":"append_file","args":{"path":"log.txt","content":"two\n"},"reported_success":true},"id":7}' \
 | PYTHONPATH=/path/to/agent-harness-audit python3 -m audit_sidecar.mcp_server --cwd "$D" --quiet

tail -3 "$D/audit_trace.jsonl"
```

The two `FAIL`s at id=5 and id=6 push the state to `INFLAMED`, so id=7, although an append
with `risk_level: low`, also receives a git change set (`witness_scope: "full"`). All three
records are needed: **without the first two failures, the third has no inflammation to
inherit.**

## File format

One record per line, fields in alphabetical order (`json.dumps(..., sort_keys=True)`), no
indentation. A single `write` therefore cannot tear a record on append, and `tail -f` and log
collectors can read it safely in parallel.
