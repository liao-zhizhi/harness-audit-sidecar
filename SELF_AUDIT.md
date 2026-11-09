# SELF_AUDIT.md — the variance blind spot, turned on us

> **Why this file exists.** R6 says the deadliest failure has near-zero variance: a smooth
> 11.5% decline whose per-round loss is 1.3%, and every variance alarm stays silent because
> the variance moved into the mean. That argument does not stop at the subject. The same shape
> can be present in the auditor, and if it is, we are the thing we claim to catch: an instrument
> that reports health it has not observed.
>
> So this is not a code review. Nothing below is about syntax, naming or style. Every item is a
> way this system could **claim a fact it did not establish**, or **destroy a distinction it
> exists to preserve**.

**How this was produced.** By running the system, not by reading it. Every finding marked
`VERIFIED` has a command in [`Evidence`](#appendix-evidence) that reproduces it against the
code as it was before the fix. Findings without a command are marked `ARGUED` and say so.

**Method, in one line.** For each layer, ask two questions:

1. *What does this layer report when it cannot look?* (If the answer is "success", that is a
   defect, and the severity is the blast radius of the answer.)
2. *What distinction does this layer collapse?* (`INVALID` vs `FAIL`, "not read" vs "not
   changed", "no evidence" vs "evidence of absence", "the same number" vs "the same meaning".)

---

## Summary

| ID | Finding | Class | Severity | Disposition |
|:--|:--|:--|:--|:--|
| **D2.1** | `chmod 000 target.py` produced **`PASS`** with no work done | INVALID → **PASS** | **Critical** | `FIXED` |
| **D1.3** | the self-audit gate never ran on the production path | decoration | **High** | `FIXED` |
| **D2.2** | an unreadable witness was reported as `FAIL` + "witness says no effect" | INVALID → **FAIL** | **High** | `FIXED` |
| **D1.4** | the self-audit that guards the gate had **zero tests** | decoration | **High** | `FIXED` |
| **D1.5** | a recovered drawdown (V shape) was `PASS` | metric blind spot | **High** | `FIXED` |
| **D1.1** | the production entry point was the least-covered module (60%, `main()` at 0%) | pseudo-metric | Medium | `FIXED` |
| **D3.1** | the canonical citation sentence said **10** placeholders while the registry said 11 | doc drift | Medium | `FIXED` |
| **D1.6** | a full disk makes the trace a silent no-op | silent drop | Medium | `LIM-1` |
| **D2.3** | OOM/SIGKILL is judged `FAIL` with no cause attribution | R5 leak | Medium | `LIM-2` |
| **D4.2** | the auditor's own source has no digest; rule packages do | trust root | Medium | `LIM-3` |
| **D2.4** | no witness for abstention: "did nothing" is unwitnessable | structural | Medium | `LIM-4` |
| **D3.2** | per-cycle loss quoted without naming its denominator (1.28% vs 1.52%) | doc drift | Low | `FIXED` |
| **D3.3** | R6's `+8.5%` row has `R²=0.86`; only the other row clears our own gate | doc drift | Low | `FIXED` |
| **D3.4** | registry: "10 constants → 3 dimensionless groups", actual 11 and 4 | doc drift | Low | `FIXED` |
| **D2.5** | `BrokenPipeError` → exit `0` | false success | Low | `LIM-5` |
| **D1.7** | the git witness cannot see work that was committed | witness gap | Low | `LIM-6` |
| **D1.8** | innate tier: `touch` is `PASS`; `rm` is `FAIL` | tier limit | Low | `LIM-7` |
| **D1.9** | drift thresholds have one positive example and no false-positive corpus | uncalibrated | Medium | `LIM-8` |

`FIXED` = code and regression test in this commit. `LIM-n` = a stated limit, documented in
`README.md` → "Known boundaries", with the fix that would remove it.

---

## D1 — Pseudo-health metrics

### D1.1 The tests were green and the entry point had never been executed · VERIFIED

At the start of this audit the suite reported 529 tests: 522 passing, 7 skipped. That number said nothing about whether the *process* works,
because a count is not a coverage claim and no one had measured coverage at all.

Measured now (`coverage run --source=audit_sidecar -m pytest`):

```
TOTAL                                    3341    604    82%
audit_sidecar/cli.py                      254    254     0%
audit_sidecar/mcp_server.py               169     68    60%
```

`cli.py` is the demo, and 0% is defensible. `mcp_server.py` is not: it is the only path an
agent ever connects to, and **lines 393–427 — the whole of `main()` — were never run by any
test**. The 522 green ticks were assembled entirely from the library API plus a subprocess e2e
test that happened to bypass `main()`.

The lesson is the R6 shape exactly: a number that is *true* (522 tests pass) and *irrelevant*
(about a path nothing exercises). A test count is a measurement of the tests, not of the system.

*Disposition:* `FIXED` — `tests/test_self_audit_defects.py::ServerRefusesToStartTest` drives
`mcp_server.main()` through the refusal branch. The full happy-path startup is still untested
and is folded into D1.3.

### D1.2 "100% coverage" was never claimed, and that was the only thing right about it · —

Recorded explicitly because the honest answer to "do you have a fake coverage metric?" is
**no, we had no metric at all**, which is a different failure: the absence of a claim was
mistaken for the presence of a guarantee. 82% is now measured and the two weakest modules are
named. The number is not a quality gate; it is a map of where the next blind spot probably is.

### D1.3 The self-audit gate was not on the path that matters · VERIFIED

`run_self_audit()` existed, was correct, was documented as *"known-answer gate that must pass
before the tool runs"* — and was called from exactly one place: `cli.py`, the demo.

```
$ grep -rn "run_self_audit" --include="*.py" audit_sidecar/
audit_sidecar/cli.py:562:    if not run_self_audit():
```

`harness-audit-server` — the console script, the thing wired into an agent's `mcpServers`
config — started serving verdicts without ever asking whether it could still say no. This is
failure mode 4 of our own paper, committed by our own repository: the gate existed as a
demonstration rather than a gate.

That is the auditor's version of a variance blind spot. Nothing looked wrong: the demo printed
"Self-audit passed", the docs said the gate ran, the tests were green. The gate was *present*
and *not in the path*, which is indistinguishable from absent in every observable except this
one grep.

*Disposition:* `FIXED` — `mcp_server.main()` runs the gate against the resolved judgment base
before constructing the proxy, and exits `3` on failure. Verified against a broken base:

```
$ printf 'not: [valid\n' > badrules/x.yaml
$ echo | harness-audit-server --cwd . --rules badrules
ERROR self-audit: judgment base badrules could not be loaded: ... not valid YAML
ERROR self-audit failed: refusing to serve verdicts. ...
exit=3
```

`3` is distinct from `2` (bad configuration), so a supervisor can tell "refused because it
cannot judge" from "refused because it could not start".

Residual: with `--rules none` the gate still validates the *bundled* rules, because
`run_self_audit(None)` auto-detects. That is over-blocking, not under-blocking, and is left
deliberately — the conservative direction.

### D1.4 The gate that guards the gate had no tests · VERIFIED

```
$ grep -rn "self_audit" tests/*.py
(no output)
```

Zero. `run_self_audit` was the only module in the package with no test touching it, and it is
the module whose failure makes every other test meaningless. Had `_fixtures` stopped producing
a known-bad case, the only signal would have been at runtime — on a machine that had already
started trusting it.

This is the self-reference hole in its purest form: `tests/` asserted the engine rejects a
known-bad case, but nothing asserted the *self-audit* rejects a known-bad case. The recursion
terminated one level too early.

*Disposition:* `FIXED` — `SelfAuditGateTest` covers: shipped base passes, invalid YAML fails,
explicitly empty base fails (`explicit=True` means "I configured this", so empty is an error,
not a skip), missing base fails.

### D1.5 Non-linear decay: the R² gate is narrower than advertised · VERIFIED

Asked directly: would exponential or step decay slip past `r2 >= 0.98`? Measured, not reasoned:

| shape | verdict before | pattern |
|:--|:--|:--|
| exponential, half-life ≈ 12 cycles | `FAIL` | `chronic` — R² = 0.9917 |
| coarse staircase (4 steps over 12) | `FAIL` | `visible` — R² = 0.9441 |
| fine staircase (−1% per 4 cycles) | `FAIL` | `chronic` — R² = 0.9906 |
| **V shape: −40% and back** | **`PASS`** | — R² = 0.0020 |

So the answer to the literal question is **no, and the gate is sound for monotone decay** —
exponential decay is nearly linear over short windows and still clears 0.98, and a staircase
that does not clear it is still `FAIL`, only labelled `visible`. The label is wrong; the
alarm is not. That distinction had to be checked rather than assumed.

But the table exposed a blind spot of a different shape, and it is the one that matters:

**A series that fell 40% and recovered was reported `PASS`.** The verdict rested on
`total_change = values[-1] - values[0]`, and `Φ_n = S_0 - Ŝ_n` is exactly that same cumulative
endpoint sum. A dip that is fully repaid contributes **zero** to Φ. The quantity I introduced
to replace variance has the same structural weakness as the quantity it replaced: neither is a
function of the *path*, only of the *endpoints*.

This is R6's own argument, applied one level up. Variance cannot see a drift because the drift
lives in the mean; a cumulative sum cannot see a near-death because the near-death was repaid.
Both report "fine" about a trajectory that nearly terminated.

*Disposition:* `FIXED` — `DriftReport.max_drawdown` (largest peak-to-trough fall, signed
fraction of the running peak) plus a `recovered-drawdown` pattern: when the net change is *not*
material but the drawdown exceeds `min_drawdown` (default 0.10), the verdict is `FAIL`. The
threshold is documented as a **policy input, not a measurement** — a system that fell 10% and
came back did survive, and whether that is an incident is the operator's call. Seven tests,
including `test_small_dip_is_still_pass` so the gate does not become a noise alarm.

### D1.6 A full disk turns the audit into a silent no-op · VERIFIED

```python
except OSError as problem:
    # An audit log that cannot be written is an incident, but refusing
    # to answer would take the agent down with it. Report, keep serving.
    _LOG.error("could not append to trace %s: %s", self._path, problem)
```

Verified with a read-only trace file: `write()` returns normally, **0 bytes** are appended, the
RPC still answers, and the log line is the whole event. The verdict is enforced and
unrecorded. That is a quieter failure than an unenforced one, and it is exactly the class of
"no ERROR visible, program did not actually run to completion" this audit was asked to look
for — except that here the ERROR *is* printed, and goes to stderr, which in a stdio MCP server
is a channel nobody reads.

*Disposition:* `LIM-1`. The comment states the real trade-off (refusing to answer would take
the agent down), and reversing it is a policy decision, not a bug fix. Recommended: add
`"audit_recorded": bool` to every response so the client cannot be mistaken about whether the
record exists, or a `--trace-strict` switch that fails closed.

### D1.7–D1.9 Witness and calibration gaps

- **D1.7 (git witness cannot see committed work)** · VERIFIED. `modify → git add → git commit`
  leaves `changed() == []`, because the baseline is a working-tree snapshot. In strict mode
  this reports `FAIL` on a correct agent; in permissive mode it also hides *unexpected*
  committed side effects. Already listed in README's boundaries; now marked `LIM-6` with the
  fix (record `rev-parse HEAD` before and after and diff the trees).
- **D1.8 (innate tier)** · VERIFIED. `touch b.txt` → `PASS`; `rm a.txt` → `FAIL` with
  `expected but untouched`. Both follow from the tier's declared claim ("existence + mtime"), so
  they are limits, not defects — but the `touch` case means a Tier-1 `PASS` is not evidence of
  *content*, and the `rm` case means deletion is unwitnessable at that tier. `LIM-7`.
- **D1.9 (no calibration corpus)** · ARGUED. `r2_threshold=0.98`, `min_total_change=0.01`,
  `min_drawdown=0.10` are policy constants validated against **one** positive series. There is
  no corpus of stationary series that must stay `PASS`, so the false-positive rate is unknown.
  A detector whose false-positive rate is unknown gets switched off in production, and
  "switched off" has the same outcome as "never existed". `LIM-8`.

---

## D2 — The four-valued boundary

The lattice is `PASS < INVALID < FAIL < NOT RUN` with worst-wins composition. The failure this
section hunts is not "the wrong verdict" but **the destroyed distinction**: a case where the
code produced one value while the evidence supported another, and the receipt then asserted
something the witness never said.

### D2.1 `chmod 000` was a `PASS` · VERIFIED, CRITICAL

```
$ git init -q . && printf 'OLD\n' > t.txt && git add . && git commit -qm init
$ python - <<'PY'
res = runner.run_shell("chmod 000 t.txt", cwd=".", intent_desc="update t.txt",
                       expected_files_modified=["t.txt"], repo_path=".", reported_success=True)
PY
verdict: PASS | effect: True | changed: ('t.txt',)
reason: witness matches intent, report agrees
```

An agent performed **no work**, made the target unreadable, and received `PASS` — and with it
a full credit in the trust account.

Root cause, in `GitWitness._fingerprint`:

```python
except OSError as problem:
    _LOG.warning("could not fingerprint %s: %s", absolute, problem)
    return "{}:unreadable".format(status)
```

The marker was a *string in the same slot as a digest*. `changed_since` compares
`baseline.get(path) != state`; `"M:unreadable"` differs from `"M:<sha256>"`, so an
**inability to read was recorded as a modification**. `wanted` then contained the path,
`missing` was empty, and `effect_observed` stayed `True`.

Two things make this the worst finding in this file:

1. It is exactly the failure the repository exists to catch — a success verdict resting on
   evidence that does not exist — produced by the evidence layer itself.
2. It is *forgeable on demand by the party under audit*, with `chmod`, a command that looks
   like housekeeping.

*Disposition:* `FIXED`. `_fingerprint` now returns a sentinel `__unreadable__` that
`changed_since` **refuses to compare**, raising `GitWitnessError`; the runner catches it,
marks the observation unknown, and the engine returns `INVALID` with `effect_observed=None`.
Verified: the same command now yields `INVALID`.

The general rule this establishes, and the one I would keep if I could keep only one line of
this document: **a witness has three outcomes, not two. `None` is a value, and code that maps
`None` onto `False` is code that manufactures evidence.** Every `except ...: return False` in
a witness collector is this bug waiting for a permission error.

### D2.2 An unreadable witness was quoted as saying "no effect" · VERIFIED

The file witness had the same collapse, one layer up:

```python
except OSError as problem:
    _LOG.warning("witness %s could not be read: %s", path, problem)
    return False          # <- "I could not look" reported as "I looked and there is nothing"
```

which produced:

```
FAIL | reason: false completion: report says success, witness says no effect
```

**The witness never said that.** It said nothing, and the receipt put words in its mouth. This
is the mirror image of D2.1 and it matters for a different reason: a false `FAIL` against an
honest agent is how an audit tool loses its users. `INVALID → FAIL` and `INVALID → PASS` are
the same defect — the destruction of `None` — pointed at different victims.

*Disposition:* `FIXED`. `_witness` returns `Optional[bool]`; `None` propagates to
`effect_observed=None`; the receipt now distinguishes the two causes:

```
INVALID | ... (a witness was requested but could not be read; this is INVALID, not FAIL)
INVALID | ... (no independent witness requested)
```

The distinction is the point. "You asked for nothing" and "I was asked and could not look" are
different sentences, and a receipt that cannot tell them apart is not auditable.

### D2.3 Degradation paths, measured

| condition | verdict | is that right? |
|:--|:--|:--|
| timeout | `INVALID` (`effect=None`, `exit=None`) | ✔ file state after a kill is intermediate, not evidence |
| `cwd` missing / command never started | `NOT RUN` | ✔ nothing ran, so no rule can be violated |
| no witness requested | `INVALID` | ✔ "did not look" is not "passed" |
| **SIGKILL (OOM killer)** | **`FAIL`** (`exit=-9`) | ✘ correct verdict, wrong story |
| unreadable witness (before fix) | `FAIL` | ✘ fixed → `INVALID` |
| unreadable change set (before fix) | **`PASS`** | ✘ fixed → `INVALID` |

The timeout path is the one already handled well, and it is worth naming why: `timed_out`
routes to `effect_observed = None` *before* the witness branch, so the intermediate world is
never read. That is the pattern the unreadable paths now follow.

**D2.3 (OOM)** · `LIM-2`. The verdict `FAIL` is defensible under the definition (the intent
was not realised), but the receipt carries no cause, so a model whose process was killed by the
kernel is indistinguishable from a model that lied. That is R5's `observed = capability ×
availability` leaking into the verdict layer with `availability = 0`. Fix: classify negative
exit codes, and report `INVALID (killed by signal 9: resource exhaustion)` rather than
`FAIL`. Not done here because it changes the lattice's meaning for `run_command`, which is an
architectural decision, not a patch.

### D2.4 No witness for abstention · VERIFIED

```
$ runner.run_shell("/bin/false", cwd=".", intent_desc="...", reported_success=False)
INVALID
```

A perfectly honest failure report cannot be confirmed, because confirming it requires
witnessing an *absence*. There is no `expected_file_absent`, no `expected_unchanged`, no
"should have done nothing" expectation anywhere in the API.

This is a structural asymmetry, not a bug: witnesses are positive by construction. But it has
a consequence worth stating plainly. The sidecar can always verify **that something happened**
and can never verify **that nothing happened**. Everything an agent achieves by *not* acting —
including the dangerous version, where a claimed action was unnecessary because the state was
already correct — is outside the lattice. `LIM-4`.

### D2.5 A broken transport is reported as a clean shutdown · VERIFIED

```python
except BrokenPipeError:
    # The agent hung up. Nothing to report, and stdout is gone anyway.
    return 0
```

Exit `0` is the one word a supervisor reads. A mid-session transport failure is reported as
"finished normally". The comment's reasoning is sound for the *client* (there is nowhere to
write) and wrong for the *supervisor* (which can only see the code). `LIM-5`.

### D2.6 Every deliberate swallow, classified

Not every `except: pass` is a bug; the question is whether the swallowed failure can be seen
downstream. Full list from the audit:

| site | swallows | verdict |
|:--|:--|:--|
| `mcp_server.TraceWriter.write` | EACCES/ENOSPC on the record | **silent drop** → `LIM-1` |
| `mcp_server.main` `BrokenPipeError` | transport death | **exit 0** → `LIM-5` |
| `immune_state._persist` | cannot write inflammation | logs ERROR, continues → memory loss on restart; acceptable, logged |
| `immune_state._persist` temp cleanup | stray temp file | fine — real failure already logged |
| `ledger._fsync_dir` | directory fsync failure | **should be reviewed**; see note below |
| `membrane._reject` ledger write | cannot record a refusal | **fail-closed**: the refusal still happens and the message carries the storage fault |
| `membrane._brake_tripped` | any exception from `is_inflamed()` | **fail-closed**: returns `True` (brake tripped) |
| `runner._read_pipe_status` | unparsable `PIPESTATUS` | returns `()` → falls back to `(exit_code,)`; documented |
| `runner._stat_one` | `stat` failure | returns `None` = "absent" → see D1.8 |
| `git_witness._fingerprint` | unreadable content | **was fallopen** → `FIXED` (D2.1) |
| `citation_witness._request` | per-attempt network failure | retries, then reports the cause; fine |
| `cli` demo sections | no git / no rules | prints `skipped:` — the demo is explicitly non-authoritative |

The pattern that separates the good from the bad: **fail-closed swallows (`membrane`) are
fine; fail-open swallows in a witness (`git_witness`) were not.** The test is whether the
swallowed condition can produce a `PASS`.

One item is flagged for the human architect rather than changed: `ledger` records an appended
hash-chain entry, then fsyncs the *directory*. If the directory fsync fails, the entry is in
the page cache and not yet durable, and the chain's promise ("the record was not rewritten")
is temporarily unbacked. It is not a false `PASS`, so it is not in the table above as a
defect — but it is the same class of "durability claimed, durability not established" and
belongs in the next review.

### D2.7 A no-op is a `PASS` when the world already matches · VERIFIED

```
$ printf 'EXPECTED\n' > t.txt
$ runner.run_shell("true", expected_file="t.txt", expected_content="EXPECTED", reported_success=True)
PASS | effect: True
trust account balance: 1.0
```

The agent did nothing, and was correct. This is not a bug in the engine — the world matches the
intent, and an *outcome* witness can only report the world. It is a bug in any reading of the
verdict as "this agent did the work".

The distinction has a comfortable half and an uncomfortable half. Comfortable: for idempotent
intents (reconcilers, config appliers) a no-op `PASS` is the desired semantics. Uncomfortable:
`PASS` credits the trust account, so an agent that repeatedly "succeeds" at tasks whose target
state was already correct builds a reputation out of nothing. `LIM-4`, and the reason the
`intent` should describe the *transition*, not the *state*, wherever the caller can express it.

---

## D3 — Documentation / code drift

R6 changed three times in one evening: `1.25%` → `1.3%` (the value depended on which
denominator the rolling std used), the per-cycle figure needed its denominator named, and the
reference fit moved into a module. Each edit was applied by hand to four documents. That is the
drift this repository exists to detect, being produced by hand in its own repository.

**The systematic fix, not the individual fixes:** `tests/test_docs_consistency.py` recomputes
the fit from `R6_SERIES` and asserts that every published figure appears in the form the code
rounds it to, in every document that quotes it. A sentence can still be wrong about *why* a
number matters; a number can no longer silently become a different number.

### D3.1 The canonical citation sentence was one behind · VERIFIED

`parameter_registry.md` §7 is the rule that every downstream citation must repeat verbatim. It
said:

> 使用了 **10** 个未标定的 PLACEHOLDER 常数，模型可复现但不可外推；

while the same file's header said **11**, the whitepapers said **11**, and the code defines
**11**. So the one string designed to propagate into external material was the one string that
was wrong. Every citation that obeyed the rule faithfully inherited the error.

This is the highest-consequence form of doc drift: not a stale README paragraph, but a stale
**mandated quotation**. A rule that says "you must repeat this sentence" also says "I have
checked this sentence".

*Disposition:* `FIXED` — 10 → 11, and the accompanying "10 个常数压成 **3** 个无量纲群" →
"动力学常数压成 **4** 个无量纲群" (the boxed formula lists β, γ, κ, χ: four).

Cross-checked the 11 values themselves against `metabolic_simulation.py`. They agree:
`v_max=10.0, k_s=1.0, k_i=4.0, y_atp=2.0, m0=1.2, phi_dorm=0.12, phi_v=0.05, mu_max=0.4,
k_a=0.5, hill_n=3, k_g=1.0`. Registry document and code are now consistent; the *values* are
still unmeasured, which is the point of the label.

### D3.2 The per-cycle loss had three defensible values · VERIFIED

| definition | value |
|:--|--:|
| total loss / cycles, relative to the first sample | **1.28%** |
| mean of each step relative to its own level | **1.52%** |
| slope / mean level | **1.54%** |

The docs quoted `≈1.3%` without saying which. A reader recomputing the second definition finds
1.5% and cannot tell whether that is a different number or the same number measured differently
— and in a project whose entire argument is "1.3% is below every threshold", an unexplained
discrepancy in the headline figure is expensive. The claim is still true under all three, and
is now stated with its denominator.

*Disposition:* `FIXED` (README, `drift.py` docstring, `RELEASE_v0.10.0.md`,
`marketing/launch.md`).

### D3.3 R6's second column does not clear R6's own gate · VERIFIED

Recomputed from `results/fed_batch_cycles.csv`:

| column | change | slope | R² |
|:--|--:|--:|--:|
| `wake_S_mM` | −11.54% | −0.2788 | **0.9997** |
| `dormant_h` | +8.55% | +0.0944 | **0.8600** |

The table published the slope for both and the R² for neither, so nothing was false — but the
pair reads as one phenomenon. Run through the shipped gate, only the first row is `chronic`;
the second is `visible`, i.e. ordinary trend noise that any monitor catches. R6's claim holds
on **one** column, and the whitepaper now says so explicitly rather than leaving it to a reader
who recomputes.

This is the most useful kind of self-audit result: the headline survives, the *scope* of the
headline shrinks.

### D3.4 Other numbers re-verified against the artefacts

- `−11.5%`, `−0.2788`, `R²=0.9997`, `69.05` (0-based) = cycle `70`, `+8.5%`, `+0.0944`: all
  reproduced from the CSV and the fit. `steps_to_zero` is a 0-based index and the README and
  docstring both say so — that off-by-one was fixed in an earlier session and is now pinned by
  `test_zero_crossing_convention_is_stated`.
- Rolling-window std bound: `≤1.3%` is true under both denominators (1.27% of the mean, 1.25%
  of the first value). Earlier `≤1.25%` was false under one of them; the widening is on the
  record here rather than silently applied.
- Test count: now `556` total, `549` passing, `7` skipped — measured, not summed by hand:
  `Ran 559 tests ... OK (skipped=7)`. The 7 skips are all environment-conditional
  (git/bash/`agent_metabolism`/live citation resolution) plus one `ERRATA.md`-absent skip.
- Version string in the README status line is now asserted against
  `audit_sidecar.__version__` by a test, so a release cannot forget it again.

---

## D4 — Self-reference

### D4.1 Can the trust account be fooled? · Yes, and the bypass is the verdict layer

The account is a pure function of the verdict stream (`PASS / (PASS+FAIL+INVALID)`), so it
inherits every defect above and adds none of its own. That is the right design — one place to
fix — but it means `D2.1` was a **trust-account forgery**: `chmod 000` yielded `PASS`, and one
`PASS` moves the balance to `1.0`.

Verified: `ConfidenceAccount().record(Verdict.PASS).get_balance() == 1.0`.

After the D2.1/D2.2 fixes, that specific path is closed. The structural statement stands:
**the account cannot be more trustworthy than the least trustworthy verdict that feeds it**,
and it has no way to weight a `PASS` by how strong its witness was. A `PASS` from the innate
tier (mtime only) counts exactly as much as a `PASS` from the full tier (content + change
set). A trace reader can tell them apart via `witness_scope`; the account cannot. Not changed
here — weighting is a modelling decision — but recorded.

### D4.2 Physical witnesses versus forgery · measured

The specific attack in the brief — `touch` to fake a modification — was tested against each
witness:

| witness | `touch` with unchanged content | verdict |
|:--|:--|:--|
| innate tier (`stat`) | mtime moved → "modified" | **forged** (`PASS`) — declared limit of the tier |
| file witness, `expected_content` given | content still matches → `PASS` | **correct**: the claim was about content, and content is what was checked |
| file witness, existence only | `PASS` | **correct**: the claim was existence |
| git change set | content digest unchanged → not a change | **immune** |

So the answer to "can `touch` fool the Witness layer?" is: **not the git witness, yes the
innate tier, and the file witness only if the caller asked for nothing but existence.** The
honest generalisation: *the strength of the verdict is set by what the caller asked to have
checked*, and `touch` is not a forgery against a claim it satisfies.

The one case worth stating loudly is the last column of the file-witness rows. A `PASS` on
existential intent is not evidence that the agent *wrote* anything — see D2.7.

### D4.3 The auditor's own code is the only unverified link · ARGUED

Rule packages are digest-verified before parsing. The ledger is hash-chained and anchored. The
witnesses are independent of the agent. And `audit_sidecar/verdict.py`, `self_audit.py` and
`runner.py` — the modules that decide what all of that *means* — are protected by **nothing**.
An operator who can edit the installed package can make `run_self_audit()` return `True`
unconditionally, and every other guarantee in this repository becomes decoration. The
self-audit cannot detect this, because it is the thing being edited.

This is the bootstrap problem and it is not solvable inside the process. What can be done is
to make the *gap* visible: ship a digest manifest of the auditing modules, publish it out of
band (a release artefact, a git tag, an SBOM), and provide a `--verify-integrity` mode that
checks the running code against it. That converts "trust the process" into "trust the channel
you fetched the manifest from" — narrower, but honest. `LIM-3`, and the substance of the
Achilles' heel answer at the end of this document.

### D4.4 Can the self-audit be made vacuous without editing it? · ARGUED

Three escape hatches existed or exist:

1. `run_self_audit()` with no `rules_dir` and no discoverable base returns `True` with a
   *warning*, to keep "plain lattice" mode usable. Defensible, and the reason `--rules none`
   must be explicit elsewhere. But it means "no policy configured" and "policy verified" both
   print a passing gate.
2. `rules_dir` pointing at an empty directory: **fails** when explicit, **warns and passes**
   when auto-detected. Correct asymmetry (explicit means the operator asserted something), and
   now pinned by a test.
3. `--rules none` on the server still runs the gate against the *bundled* rules, so the server
   validates policy it will not use. Over-blocking, left as is.

The residual: a probe that cannot distinguish "verified 3 rule fixtures" from "verified 0" in
its exit code. Both exit `0`. The log line says which; the exit code does not. Recorded, not
changed, because making "0 fixtures" a hard failure would break the plain-lattice mode that
the design deliberately supports.

---

## What was changed in this commit

| File | Change |
|:--|:--|
| `audit_sidecar/git_witness.py` | `__unreadable__` sentinel; `changed_since` raises instead of comparing; module docstring records the exploit |
| `audit_sidecar/runner.py` | `_witness` → `Optional[bool]`; `observed_unknown` overrides the effect to `None`; `GitWitnessError` caught; receipt distinguishes the two no-witness causes |
| `audit_sidecar/mcp_server.py` | self-audit before serving; exit `3` on failure |
| `audit_sidecar/drift.py` | `max_drawdown`, `recovered-drawdown` pattern, `min_drawdown` policy gate; sampling contract in the docstring; per-cycle denominator named |
| `tests/test_self_audit_defects.py` | **new** — 11 tests for D2.1, D2.2, D1.3, D1.4 |
| `tests/test_drift.py` | +7 tests for the drawdown gate and the sampling contract |
| `tests/test_docs_consistency.py` | **new** — 9 tests: every published figure recomputed, the version line and the two no-witness reasons pinned |
| `README.md` | test counts; the drift table's new row; six new known boundaries; the self-audit paragraph |
| `RELEASE_v0.10.0.md`, `marketing/launch.md` | per-cycle denominator |
| `../bio-metabolism-lab/parameter_registry.md` | 10 → 11 placeholders in the mandated citation; 3 → 4 dimensionless groups |
| `../bio-metabolism-lab/whitepaper/04_Emergent_Relations.md` | the two R² values stated separately, and R6's scope narrowed to the column it holds on |

Test suite: **559 tests, 552 passing, 7 skipped** (from 529 total: 522 passing, 7 skipped). Coverage: **82%** total,
measured for the first time; the two weakest modules are named above.

Nothing was pushed. `git log` locally has the change.

---

## Known limits, consolidated

| ID | Limit | What it takes to remove |
|:--|:--|:--|
| `LIM-1` | A trace that cannot be written does not block the verdict | `audit_recorded` in the response, or `--trace-strict` |
| `LIM-2` | OOM/SIGKILL is `FAIL` with no cause | classify negative exit codes; a `killed` sub-reason or `INVALID` |
| `LIM-3` | The auditor's own modules have no digest | digest manifest published out of band + `--verify-integrity` |
| `LIM-4` | No witness for abstention; a no-op `PASS` credits trust | `expectation: unchanged / absent`; weight `PASS` by witness strength |
| `LIM-5` | `BrokenPipeError` exits `0` | exit nonzero; the supervisor can distinguish a normal end from a dropped transport |
| `LIM-6` | Committed work is invisible to the git witness | record `HEAD` before/after and diff the trees |
| `LIM-7` | Innate tier: `touch` is `PASS`, `rm` is `FAIL` | content digest at Tier 1, or refuse delete intents at that tier |
| `LIM-8` | Drift thresholds are calibrated on one positive example | a corpus of stationary series that must stay `PASS` |
| `LIM-9` | `Φ` and `total_change` are endpoint functions | already partially addressed: `max_drawdown` is the path-sensitive companion; a full path integral is not implemented |
| `LIM-10` | A dirty non-regular file (symlink/device/FIFO) makes every change-set verdict `INVALID` | read a bounded prefix and mark the witness "content not fully witnessed", or ignore non-regular paths under an explicit sub-reason. Current behaviour is fail-closed on purpose: `ln -s /dev/zero` must not block the snapshot |
| `LIM-11` | Auto-detected `./rules` resolves against the process cwd, not `--cwd` | resolve the local layer against `--cwd` before merging; until then launch the server from the repository root, or pass `--rules` |
| `LIM-12` | `$HARNESS_AUDIT_RULES` replaces the shipped floor instead of merging, so a custom base can omit a shipped rule | refuse overrides that lack a shipped `rule_id` (and document the break), or add `--rules-extra`. Today the bypass logs a WARNING naming the missing rules; it is an escape hatch, not a silent default |

---

## The Achilles' heel

Asked to name one thing, here it is.

**This system's deepest weakness is not a metric, a threshold or a missed branch. It is that
the chain of custody terminates at the auditor's own code, and nothing in the system can
verify that code.**

Every other guarantee is real and was paid for: the witness is independent of the agent, the
ledger is anchored, rule packages are digest-sealed, the four-valued lattice refuses to
collapse "did not look" into "passed". All of it is enforced by Python modules that:

- are not signed and carry no digest, unlike the rule packages they load;
- are audited by a gate implemented *inside* those same modules (`self_audit.py`), which by
  construction cannot detect its own modification;
- typically run as the same user, in the same process tree, on the same machine as the agent
  they observe — so `import audit_sidecar; audit_sidecar.self_audit.run_self_audit = lambda: True`
  is a one-line defeat, and so is editing one file in site-packages.

Two honest readings of that, and I think both are true.

*The narrow reading:* it is a bootstrap problem, every audit system has it, and the fix is
organisational rather than architectural — publish a digest manifest out of band, run the
sidecar under a different principal, keep the trace on a host the agent cannot reach. That is
`LIM-3`, and it is tractable.

*The wide reading, which is the one this audit actually supports:* **the system has no way to
represent "my own judgement may be compromised".** The lattice has four values, and every one
of them is a statement about the *subject*. There is no fifth value meaning "I cannot vouch for
this verdict because I cannot vouch for myself". So when the auditor is compromised, it does
not degrade — it keeps emitting confident `PASS`/`FAIL`, and the confidence is indistinguishable
from the genuine article. That is R6 one level up: the failure has near-zero variance, every
metric stays in range, and the thing that is dying is the instrument.

That is why D2.1 frightens me more than it should. That bug was not a compromise; it was an
ordinary `except OSError`. And it produced a `PASS` — smoothly, quietly, with a green test
suite, a passing self-audit, and a trust balance of 1.0. If the system can generate a
confident false `PASS` from a permission error, then the distance between "I am being lied to
by the agent" and "I am lying to myself" is a single `except` clause, and no quantity we
currently compute can tell the two apart.

The honest consequence, and the sentence I would put at the top of the README:

> **This tool can tell you what it witnessed. It cannot tell you whether it was in a position
> to witness. Read the `reason`, check the `witness_scope`, and treat a `PASS` whose evidence
> you have not seen as an unverified claim — including the ones it produced about itself.**

That is not a comfortable answer, and it is the one the audit supports.

---

## Appendix: evidence

Reproduce the pre-fix behaviour by checking out the parent commit; the commands below are the
ones that produced the `VERIFIED` findings.

```bash
# D2.1 the chmod exploit: PASS with no work done
cd "$(mktemp -d)" && git init -q . && git config user.email a@b.c && git config user.name t
printf 'OLD\n' > t.txt && git add . && git commit -qm init
python - <<'PY'
from audit_sidecar.runner import SafeRunner
print(SafeRunner(timeout=20).run_shell(
    "chmod 000 t.txt", cwd=".", intent_desc="update t.txt",
    expected_files_modified=["t.txt"], repo_path=".", reported_success=True).verdict)
PY

# D2.2 the unreadable file witness: FAIL quoting a sentence nobody said
python - <<'PY'
from audit_sidecar.runner import SafeRunner
r = SafeRunner(timeout=10).run_shell(
    "chmod 000 t.txt", cwd=".", intent_desc="x",
    expected_file="t.txt", expected_content="OLD", reported_success=True)
print(r.verdict, "|", r.reason)
PY

# D1.3 the gate was not on the production path
grep -rn "run_self_audit" --include="*.py" audit_sidecar/

# D1.4 the gate had no tests
grep -rn "self_audit" tests/*.py

# D1.5 the drawdown blind spot (pre-fix: PASS)
python -c "
from audit_sidecar.drift import analyze_chronic_drift as d
r = d([100,92,84,76,68,60,62,70,78,86,94,100])
print(r.verdict, r.pattern, r.max_drawdown)"

# D1.6 the trace write is a silent no-op
python -c "
import os
from audit_sidecar.mcp_server import TraceWriter
t = TraceWriter('/tmp/t.jsonl'); os.chmod('/tmp/t.jsonl', 0o444)
t.write({'verdict':'PASS'}); print('bytes:', os.path.getsize('/tmp/t.jsonl'))"

# D3.3 R6's second column does not clear the gate
python -c "
import csv
from audit_sidecar.drift import analyze_chronic_drift as d
rows = list(csv.DictReader(open('results/fed_batch_cycles.csv')))[:9]
for col in ('wake_S_mM','dormant_h'):
    r = d([float(x[col]) for x in rows]); print(col, r.pattern, round(r.r2,4))"

# Coverage (measured for the first time in this audit)
python -m coverage run --source=audit_sidecar -m pytest -q
python -m coverage report --skip-covered --sort=cover
```
