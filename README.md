# agent-harness-audit

**An audit tool for the agent execution layer**

## Install (two commands)

```bash
pip install harness-audit-sidecar
harness-audit setup
```

`setup` detects the Cursor / VS Code `mcp.json`, adds the server, merges
`ref_roots.json`, and takes a baseline. It backs up every file it replaces and
refuses to rewrite one it cannot parse.

Three errors, and what to run:

| What you see | What it means | Fix |
|---|---|---|
| `harness-audit: command not found` | installed into another interpreter | `python -m pip install harness-audit-sidecar`, then `python -m audit_sidecar.cli --help` |
| `setup: refusing to write: ...` | the existing `mcp.json` is not valid JSON | fix or move the file, then `harness-audit setup` again |
| `INVALID ... no recording says which changes came before` | there is no baseline to compare against | `harness-audit baseline` (or `harness-audit baseline --file src/main.py` for one file) |

`INVALID` is not a bug report: it means the tool declined to judge because
nothing witnessed the claim. Re-running the fix above is the whole remedy.

> ⚠️ **Read this first:**
>
> - **PASS means the world matches the intent, not that the agent did anything.** A no-op on a
>   target that was already correct is a `PASS` — correctly, because an outcome witness can
>   only report the world. Do not read it as "this agent did the work".
> - **This tool can tell you what it witnessed; it cannot tell you whether it was in a position
>   to witness.** Read `reason`, check `witness_scope`, and treat a `PASS` whose evidence you
>   have not seen as an unverified claim — including the ones it produced about itself.
>
> For full disclosures of our own limits (`LIM-1` to `LIM-12`), read
> [`SELF_AUDIT.md`](SELF_AUDIT.md) — including the `chmod 000` defect that produced a false
> `PASS` before we found it. We would rather publish that than ship it.

Four verdicts, not two, because **failure is two-dimensional.** A subject can fail because it
ran out of material, or because the material was there and it could not take it up. Those are
opposite corners, not degrees of one thing: the first is a fact about the world, the second is
a fact about the interface, and a boolean collapses them into one word that cannot be argued
with afterwards. The vocabulary here comes from a formalisation of metabolic dynamics — energy
charge `A` and substrate `S` — where the same two coordinates decide dormancy, deadlock and
recovery, and where one corner turns out to be **dynamically unreachable**. That corner is the
definition of a hallucination, which is why anti-hallucination here is a structural property
rather than an extra check.

## Bio-Inspired Architecture

The system is layered on two fronts: **eight laws govern the inside, five laws govern how
the outside may attach.** The two sets share no state, only verdict vocabulary — deliberately,
because a brake that reads the ledger of the system it audits can be poisoned by the same
defect twice.

**External interface: the five laws (`audit_sidecar/tools/eco_api.py`)**

| Law | Ecological prototype | Callable form in this system |
|---|---|---|
| 1 | Energy gradient | `get_energy_gradient()` — per-call token / time cost and its slope |
| 2 | Succession from margin to mainstream | `get_evolution_history()` — verdict time series, and whether each tool has "graduated" |
| 3 | Waste as resource | `get_recyclable_waste()` — how many failures in `ERRATA.md` became standing rules |
| 4 | Keystone species | `get_evolution_history()["keystone"]` — the tool whose removal loses the most evidence |
| 5 | Carrying capacity | `headroom` from `get_energy_gradient(..., capacity=)` — how much slack remains before refusal |

Laws 1–3 are the three explicitly named in the interface requirement; laws 4 and 5 are
proposals added by this implementation and are marked `"specified": False` in `LAWS`, so they
are not passed off as requirements. The requirement ends with "etc." and never defines a
fourth or fifth law.

**One deliberate refusal**: when a trace lacks token / duration fields,
`get_energy_gradient` returns `INVALID`, not `0`. The existing trace schema records verdicts,
not what a verdict cost to obtain; "zero cost" would be read as a measurement, when it is in
fact an *absence* of measurement.

### The semi-permeable interface: potential difference, one-way locking, breach cost

The three functions above are **passive reads** — they describe the environment but do not
change who may do what. A real interface needs a potential difference: crossing the membrane
must be paid for first, and **the acceptance criterion must not be set by the requester**.

`MembraneGateway` in `audit_sidecar/tools/membrane.py` is the only entry point:

```python
gw = MembraneGateway("audit.jsonl")            # automatic head anchor + fsync
gw.register_artefact("write", "out/spec.yaml") # register the expectation first (outside the membrane)
w = gw.request_action_warrant(
    {"kind": "file", "path": "out/spec.yaml", "digest": "sha256:..."}, "write")
gw.redeem(w)                                    # single use
```

| Property | How it is implemented | Boundary |
|---|---|---|
| **Potential difference** | The digest is **recomputed** by the membrane and must match a registration made **before the request arrived** | Unregistered → `NOT_REGISTERED`. If the agent can set its own acceptance criterion, the potential difference is zero |
| **One-way locking** | Every grant/denial/registration/redeem is a link in a hash chain; a Warrant is bound to the `seq + chain_head` at issuance | Rolling the log back → unredeemed Warrants become invalid immediately |
| **Breach cost** | A refusal **writes the ledger first, then raises** `MembraneRejectError` (with a stable `reason` code) | Not recording a refusal disguises it as "never happened" |
| **Single entry point** | `ImmuneState` leaves only as a scalar dict (`brake()`); the five laws are computed by this gateway | This is an **interface boundary, not a sandbox** — code in the same process can still `import` the underlying objects |

Refusal codes: `NO_WITNESS` / `MALFORMED_WITNESS` / `ARTEFACT_MISSING` / `DIGEST_MISMATCH` /
`NOT_REGISTERED` / `UNKNOWN_CAPABILITY` / `BRAKE_TRIPPED` / `WARRANT_ROLLED_BACK` /
`WARRANT_EXHAUSTED` / `LEDGER_BROKEN`.

**On the "delete the last line" hole — this must be stated plainly.** A pure hash chain
**cannot** stop it: remove the tail and the remaining prefix is still a self-consistent chain,
so `verify_chain()` returns `PASS`. The chain proves "the record was not rewritten", not "the
record was not removed". Preventing removal requires a **head anchor** (`seal()` writes
`{seq, head_hash}` to a separate path), and the anchor must live somewhere the attacker
**cannot write** (a commit, another host, read-only media) — otherwise rewriting ledger and
anchor together leaves both self-consistent. This is a hard boundary of a local, file-only
scheme, and this repository does not claim to cross it. See item 7 of [ERRATA.md](ERRATA.md).

**Internal: the eight laws (L1–L7 + penalty ledger)** are enforced by the runtime itself.
L1–L4, L6 and L7 live in `agent-metabolism-os`, while **L5 allosteric braking is carried by
this repository** — the contradictory evidence needed to trigger it comes from the auditing
side, not the scheduling side. This repository exposes L5 to the metabolic gateway through
`ImmuneState`:
`MetabolicGateway(immune_state=...).get_current_laws_status()["L5_Brake"]`. This layer
deliberately does not import from this repository into the metabolic side; it attaches by
protocol duck typing, keeping both sides independently installable.

**The membrane also enforces L3's write gate.** A policy that only computes but never blocks
is decoration: `evaluate_regime` would compute `EMERGENCY` at 10% energy with a `CRITICAL`
penalty, yet nothing previously refused the write. The membrane now accepts `penalty_ledger=`
and, at `CRITICAL`, closes `write` / `execute` with `PENALTY_CRITICAL`, independently of the
inflammatory brake and reported separately:

```python
gateway = MembraneGateway(ledger, immune_state=immune, penalty_ledger=penalty)
gateway.request_action_warrant(witness, "write")   # MembraneRejectError: PENALTY_CRITICAL
```

The two are deliberately separate because they ask different questions and have different
lifetimes: inflammation heals after a few passes, a penalty does not. Accordingly the
`PenaltyLedger` state attribute is named `penalty_state`, not `state` — two vocabularies
(`HEALTHY/WARNING/CRITICAL` and `NORMAL/INFLAMED`) should not share one word, and wiring one
into the other's slot now raises `TypeError` instead of quietly returning "all fine".

**Blocked is not the same as failed.** A request stopped before execution is recorded as
`record_type="deny"` (`outcome="DENIED"`, carrying no verdict) and does not enter the damage
count; one that executed without authorisation is still recorded as `FAIL`. Recording both as
`FAIL` would inflame the system through its own successful defence — two attacks and it
flares, earlier than any real false completion.

> See [ERRATA.md](ERRATA.md) for a list of false completions caught by our own audit during development.

> **Project status: MVP / early version (v0.10.1) — interfaces may change; not recommended for production yet.**
>
> Implemented: the four-valued verdict engine, the trust-energy account, the self-audit loop,
> three witness sources, the external judgment base, condition-triggered tiered immune
> response (with cross-process persistence), citation witnesses (DOI/arXiv resolution), the
> external ecology interface (five laws as pure functions), the semi-permeable membrane
> (hash-chain ledger + potential-difference authorisation), the rule interface (digest-verified
> external rule packages, mounted through the membrane), a stdio server, and chronic drift
> detection (`analyze_chronic_drift`, §"The two failures"), and the MCP handshake
> (`initialize` / `tools/list` / `tools/call`). The core loop
> (execute → witness → verdict → record) runs, backed by 588 tests (581 passing, 7 skipped);
> but concurrency and forwarding to an external MCP server are missing, and the claim under
> audit is still not observable on `tools/call` (see "Known boundaries").
> **Trial use and feedback are welcome; do not wire it into critical paths yet.**
>
> **Read this first**: [`examples/sample_trace.jsonl`](examples/sample_trace.jsonl)
> — a real session in which the agent received `ok` while the ledger recorded `FAIL`.

This repository is the engineering implementation of the paper *From Formal Verification to
Agents: A Method-Transfer Audit of "False Completion" in AI Agent Harnesses*. The paper's
thesis is not "write another agent framework" but to transfer the notion of a *verdict* from
formal verification into the agent execution layer: **whether an action completed cannot be
decided by the agent's own report; it can only be decided by an independent witness.**

> **Implemented:** the four-valued verdict engine, the trust-energy account, the self-audit
> loop, **three witness sources** (filesystem / git change set / MCP protocol-layer claim),
> the **external judgment base (`rules/*.yaml`)**, the **condition-triggered tiered immune
> response** (innate stat, adaptive full, including inflammatory storm and recovery),
> a **`pip install`-able stdio server** (`harness-audit-server`), and an **MCP-compatible
> handshake** (`initialize` / `notifications/initialized` / `ping` / `tools/list` / `tools/call`).
> **Not implemented:** claims parsed from protocol frames (the client still supplies the claim),
> more witness sources (process exit-code family / network side effects), concurrency, and
> forwarding to an external MCP server.
> See "Known boundaries" and "Roadmap".

The audit logic itself uses only the Python standard library; **the only third-party
dependency is PyYAML**, used to move *policy* out of the code (see
[External judgment base](#dynamic-judgment-base)).

---

## Example: four records from one session

[`examples/sample_trace.jsonl`](examples/sample_trace.jsonl) is a genuine audit trail
(not hand-written). In one session, the protocol-layer replies the agent received and the
ledger records were:

| id | Call | Agent's claim | Actual effect | Verdict |
|--:|:--|:--|:--|:--|
| 1 | `write_file target.py` | success | written | **PASS** |
| 2 | `write_file missing_dir/target.py` | success | no change | **FAIL** (tool layer swallowed the error) |
| 3 | `write_file target.py` | failure | written | **FAIL** (false failure) |
| 4 | `run_command` (expected to modify `target.py`) | success | modified `wrong.py` | **FAIL** (target drift) |

Three of the four are FAIL, while the agent was explicitly told about a failure only once.
The ledger record for id=2:

```json
{"id":2,"tool":"write_file",
 "check_observed":true,"check_source":"protocol","effect_observed":false,
 "verdict":"FAIL","exit_code":1,
 "reason":"false completion: report says success, witness says no effect (...)
           [tool layer reported success, underlying command exited 1]"}
```

`check_observed: true` is what the tool layer told the agent, `effect_observed: false` is what
git saw, and `exit_code: 1` is the command's own status. **All three observations are kept**,
so it is not possible afterwards to mistake whom to suspect.

For the full reading, redaction notes and reproduction commands see
[`examples/README.md`](examples/README.md).

The file also holds a second session block (id=5..7) demonstrating the rising cost of the same
tool after inflammation; see
[Conditional Immune Response](#conditional-immune-response).

---

## Why this is needed

Behind an agent saying "done" there are three quite different worlds:

| World | Agent reports | Disk truth | What a boolean verdict says |
|:--|:--|:--|:--|
| Genuinely done | success | landed | done ✓ |
| **False completion** | success | **not landed** | done ✓ ← **a lie** |
| Not collected | success | **unknown** | done ✓ ← **pretending to know** |

Rows two and three are the structural defect of boolean verdicts: there is no word for
"I did not look at all". A four-valued verdict splits them apart:

- **FAIL** — I looked, and I saw failure (false completion falls here).
- **INVALID** — I did not look. **PASS is not permitted.**

---

## The two failures, and why the vocabulary has four values

A verdict per record catches one lie: the agent says done, the witness says nothing landed.
There is a second failure it cannot catch, because **that one has no bad record to point at**.
Both are worth stating precisely, because the architecture below is built around them.

### Observed = Capability × Availability (R5)

A published score is always the left-hand side. The right-hand side has a term that is a
property of the **harness**, not of the model:

```
observed = capability × availability
```

Multiplication, not addition, and that is the whole problem: **if availability is 0, the score
is 0 and the capability term is unobservable.** Swapping in a better model cannot move a
number that is already zero, so the improvement is invisible and the diagnosis points at the
wrong component.

Two ways availability reaches 0 while capability is untouched, both of which this repository
already has vocabulary for:

| Failure | What the receipt says | What actually happened |
|:--|:--|:--|
| Deadlock (charge ceiling below the wake threshold) | task failed | the model was **never called** |
| Saturated auditor (`W = 1/(μ-λ)`, timeouts above ~93% utilisation) | `INVALID` | **the judge** gave up; the agent was never judged |

The second row is why the auditor has to be able to **refuse to judge**. A saturated judge that
must still produce a verdict produces `INVALID` for everything, and `INVALID` in the receipt
looks exactly like a defective subject. That is a false failure manufactured by the observer —
which is why the verdict has to travel with its witness and its own load, not just its value.

### Chronic drift (R6): the loop that looks perfect because it is dying

`analyze_chronic_drift()` in `audit_sidecar/drift.py`.

Some agent loops produce a number per cycle — tokens per retry, files touched, recovery
margin, remaining budget. The series looks periodic. It may not be.

Reference run (a nine-cycle fed-batch simulation; **the constants are placeholders, so this
establishes the mechanism, not a biological measurement**), substrate level at each wake:

```
19.265  18.982 ...  18.688  18.400  18.118  17.841  17.570  17.303  17.041   mM
```

Plotted: nine near-identical peaks. Fitted: a line with

| quantity | value |
|:--|--:|
| total change | **−11.5%** over 9 cycles |
| per-cycle loss | **≈1.3%** (11.5%/9, relative to the first cycle; ≈1.5% if each step is measured against its own level) |
| linear fit | slope **−0.2788** mM/cycle |
| **R²** | **0.9997** |
| extrapolated zero crossing | ≈ cycle **70** |

No per-round alarm fires: the loss is 1.3-1.5% depending on which denominator you
pick, and every per-round threshold is set well above that. The nine "clean cycles" were the first 13% of the decay.
Cumulative blood loss: each loss is below the noise floor of a single round, and the sum kills
the system.

This is the failure a variance alarm structurally cannot see, so the detector is not a
variance alarm. Two gates, both required:

```python
from audit_sidecar.drift import analyze_chronic_drift

report = analyze_chronic_drift(levels, r2_threshold=0.98, min_total_change=0.01)
report.verdict            # FAIL
report.pattern            # 'chronic'  (quiet and material)
report.r2                 # 0.9997
report.relative_total_change   # -0.1154
report.steps_to_zero      # 69.05  -> cycle 70 for a series starting at cycle 1
```

**R² alone is the wrong test**, and this is the part worth arguing about:

- `R²` measures **regularity**, not **magnitude**. A metric wandering by 1e-7 per cycle gives
  `R² ≈ 1.0` and means nothing. Gating on `R²` alone just builds a noise alarm.
- A metric falling 40% *with jitter* lands around `R² ≈ 0.86` — under the gate, and genuinely
  dying — but every variance-based monitor would already have caught it.

So the dangerous case is the **conjunction**: high `R²` *and* material magnitude. That is
`pattern == "chronic"` — quiet enough to evade per-round thresholds, regular enough to look
healthy. Drift that clears the magnitude gate but not the regularity gate is reported as
`"visible"` instead of being merged in; the two have different responses.

The detector reuses the same verdict vocabulary and the same refusals as the rest of the system:

| input | verdict | why |
|:--|:--|:--|
| empty series | `NOT RUN` | nothing was measured |
| fewer than `min_points` (default 6) | `INVALID` | a trend from three points is an opinion |
| a `NaN` or `inf` sample | `INVALID` | fitting across a gap invents the missing measurement |
| stationary | `PASS` | no material change |
| material + regular | `FAIL` / `chronic` | the R6 case |
| material + noisy | `FAIL` / `visible` | real, but catchable |
| net change ~0, but fell ≥10% and came back | `FAIL` / `recovered-drawdown` | the endpoint sum cancels; something almost died |

The last row is a correction found by auditing this module itself. A V-shaped series has
`total_change == 0`, so the cumulative sum — the same quantity as Φ — reported `PASS` for a
system that dropped 40% and recovered. Net loss cannot see a dip that was repaid. The
detector now also reports `max_drawdown` and refuses `PASS` when it exceeds `min_drawdown`
(default 10%, a policy input, not a measurement).

A constant series reports `r2 = 1.0` by convention and is still `PASS`: the magnitude gate is
what decides, not the fit quality. All of the above is asserted in `tests/test_drift.py`,
including the published numbers above.

**Where these came from.** Both are results of a private derivation from metabolic dynamics
(energy charge, substrate inhibition, hysteresis) rather than intuitions about software. The
same dimensionless groups that bound a cell's dormancy also bound an agent's recovery; the
form `observed = capability × availability` falls out of that, and so does the drift criterion
— a biological loop whose amplitude decays 11% over nine cycles looks exactly as "stable" as a
software one. The repository does not ship that derivation; what it ships is the part that
survives contact with a witness.

---

## Dynamic Judgment Base

Verdicts used to live entirely in code, with one concrete drawback: **you cannot diff a rule,
so you cannot argue about it.** Tightening one tool's acceptance criterion meant editing
Python and cutting a release; in practice nobody tightened anything.

The external judgment base moves **per-tool policy** into YAML:

```yaml
# audit_sidecar/rules/write_file.yaml (shipped with the package)
rule_id: "no_file_drift"
target_tool: "write_file"
require_witness: true
witness_type: "git_diff"
expectation: "file_modified"
```

Changing an acceptance criterion is now a diff in `rules/`, and the person who can read it
need not be the person who writes code. Default rules ship inside the package, so the state
"no rule configured" does not exist — unconfigured means this one.

### What it actually adds

Note the `require_witness` clause: **the four-valued grid cannot express it.** The file
witness has already proved the write landed, and the grid's decision is correct — `PASS`. But
policy says: "a file witness is not enough; I want a git change set."

The same observation, two verdicts:

| Observation | Four-valued grid | With the rule | `rule_id` |
|:--|:--|:--|:--|
| File witness only | `PASS` | **`INVALID`** | `no_file_drift` |
| File + git witness | `PASS` | `PASS` | `null` (rule checked, allowed) |
| Write did not land | `FAIL` | `FAIL` | `no_file_drift` |
| Tool has no rule | `PASS` | `PASS` | `null` (falls back to the grid) |

The first line is the point of the upgrade: it does not say "the write failed", it says
"**this success does not count as evidence**". There is no way to write that sentence in code
that has no rules.

### Monotonicity: rules may only tighten, never loosen

This is a hard constraint, not an implementation detail:

```
NOT RUN  <-  nothing ran, so no rule can be violated; kept as is
FAIL     <-  witness contradicts the record. A missing witness does not explain the contradiction away
INVALID  <-  the witness a rule demanded was not collected. No evidence, no PASS
PASS     <-  the grid agrees, and every rule is satisfied
```

**A rule that can turn `FAIL` into `PASS` is a way of bargaining with the witness** — and
that is precisely what this tool exists to prevent.
`tests/test_rule_engine.py::TestMonotonicity` nails it down.

### Loading is strict

Unknown fields, unknown `witness_type`, unknown `expectation`, a `rule_id` duplicated across
files, `require_witness: true` without a `witness_type` — **all fail at load time**.

The reason is the same as for self-audit: a rule you believe is live but is in fact dead is
worse than no rule. So it would rather blow up:

```
RuleError: audit_sidecar/rules/write_file.yaml: unknown field(s) ['require_witnes'];
the loader refuses to ignore a guard it does not understand
```

### Usage

```bash
# Active on install: bundled default rules load automatically, no --rules needed
harness-audit-server --cwd /path/to/repo

# Project-specific policy: put .yaml files under ./rules and omit --rules;
# the local layer is merged over the bundled defaults -- it may add rules or
# tighten a shipped one, never delete one
harness-audit-server --cwd /path/to/repo

# An explicit policy directory: loaded alone, exactly as named
harness-audit-server --cwd /path/to/repo --rules ./my-policy

# Explicitly require "four-valued grid only", rather than accidentally finding no rules
harness-audit-server --cwd /path/to/repo --rules none
```

```python
from audit_sidecar import JudgmentBase, RuleEngine, MCPProxy

proxy = MCPProxy(cwd=repo, rules_dir="rules")   # library default loads no rules; opt in explicitly
```

The server and the library differ on purpose: **the server** should block once installed, so
it loads bundled rules automatically; **the library** caller may want the bare grid, so
omitting `rules_dir` gives the pure grid. `--rules none` and "auto-detection found nothing"
are two different things: the former is the operator's choice, the latter an accident.

### Self-audit follows the upgrade

The judgment base moves policy out of code, and with it the **failure modes**. So self-audit
gained a group of samples with known answers, checking three things: the rules **were actually
loaded**, violating runs are **judged FAIL/INVALID**, and the verdict **names the `rule_id`
responsible**. If any of the three fails, the rule layer is decoration.

Traces therefore carry `rule_id` / `rules_matched` / `rules_violated`; see
[`examples/README.md`](examples/README.md).

---

## Rule Interface: a rule package is an interface, not a config file

A YAML file in the working directory is a note to oneself: whoever is present edits it, the
same process it constrains reads it, and nobody outside can tell whether the copy in force is
the copy that was reviewed. That is adequate while one operator writes every rule, and it stops
being adequate the moment a rule is written by someone who will never run the agent.

`audit_sidecar/tools/rule_interface.py` is the distribution layer above the judgment base. A
package must answer three questions a local file never had to:

| Question | Field | Why it cannot be left implicit |
|---|---|---|
| **Who is this?** | `rule_id` (namespaced), `version`, `author` | A name unique only inside one directory cannot be distributed, and a trace has to name the policy that decided rather than the file it lived in |
| **What does it need?** | `interface_spec.required_witnesses` | A rule that cannot state its evidence requirement can be checked by nobody but its author |
| **What does breaking it cost?** | `penalty_weight` | A rule whose violation is free is an advertisement, not a gate |

### The seal precedes the reading

Installation verifies the SHA-256 digest of the package's **exact bytes** *before* any YAML is
parsed. A mismatch raises and the registry is left exactly as it was — a half-installed policy
is worse than none, because its gaps are invisible.

**There is no unsigned local mode.** A working directory is precisely where a rule file is most
likely to be edited by hand, and that edit is what the digest exists to make visible. Every
package needs a digest: supplied out of band, or read from a `<file>.sha256` sidecar.

Loading is strict for the same reason as the judgment base, and stricter in one respect:
unknown fields, an unsupported witness name, a duplicated `rule_id` across two packages, and a
package schema the loader does not speak are all refused. A guard you believe is live and that
is in fact dead is worse than no guard.

```python
from audit_sidecar.tools.rule_interface import RuleRegistry

registry = RuleRegistry()
package = registry.pull(URL, expected_digest="sha256:...")   # verified, then parsed
```

### The package crosses the membrane before its rules may act

`RuleRegistry.mount(gateway)` is not a table registration; it is an admission. Construction
registers the package digest for `read`, requests an inline warrant over the package's own
text, redeems it, and appends the mount to the hash-chained ledger as an audited action. Only
then does any rule in that package decide anything.

Two properties follow, and neither is optional:

- `membrane.py` is **not modified** and does not learn that rules exist — injection uses the
  public API only, so the policy layer cannot acquire a privileged corner inside the gate it
  configures;
- a package that will not cross **does not load**. The interface is subject to the interface,
  which is the only arrangement in which the gate's own configuration is evidence rather than
  assertion.

### Enforcement keeps the two layers apart

| Layer | Question | Failure looks like |
|---|---|---|
| Rule | Was the evidence an action needs actually collected? | `RULE_INTERFACE_VIOLATION` denial carrying `rule_id`, missing witness names, `penalty_weight` |
| Membrane | Are the exact bytes offered the bytes someone registered *before* the request arrived? | `NOT_REGISTERED` / `DIGEST_MISMATCH` |

A missing `compiler_exit_code` is a denial even when the shell command exited `0`. Passing
`witness_payload` asks both questions in one step; leaving it out proves only that the evidence
*exists*, which is adequate for a dry run and insufficient for a write.

**Denial is not damage.** A request stopped before execution is written as a denial carrying no
verdict at all, so a defence that works cannot inflame the system it protects.

### Honest limits

- **The digest is a transport seal, not a signature.** In the sense `sha256sum -c` checks one:
  it detects corruption. If the package and its digest arrive over the same channel, an
  attacker who can replace the package can usually replace the digest beside it, and `pull()`
  logs a warning when it is forced to take a digest from a sidecar. Authorship requires a
  publisher key and a signature scheme over the digest — not implemented.
- **The witness vocabulary is closed** (`git_diff`, `file`, `agent_report`,
  `compiler_exit_code`, `test_exit_code`, `lint_exit_code`, `static_analysis_report`,
  `coverage_report`, `transaction_receipt`, `simulation_receipt`). Adding one is a code change,
  deliberately: somebody must agree to collect it. A rule demanding evidence no runner can
  produce would be a guard that always fires, indistinguishable from one that never loads.
- **`expectation` currently has one supported value** (`file_modified`), and `risk_level` two
  (`low`, `high`). Both are validated, so a package cannot invent a third and have it silently
  ignored.
- **Rules perform no cross-tool reasoning.** Each rule judges independently; there are no "A
  then B" ordering constraints.

See [`demo_rule_registry.py`](demo_rule_registry.py) for a runnable end-to-end example: an
external package is pulled against a published digest, mounted, blocks a high-risk call whose
compiler witness is missing, allows the same call once the full evidence set arrives, and
refuses a tampered package before it can zero its own penalty.

---

## Conditional Immune Response

The first two sections address **judging correctly**; this one addresses **being able to
afford it**.

Full witnessing is correct but expensive: a git change set means a snapshot plus a diff, a
content check means reading a whole file. If an irrelevant append costs the same as
overwriting core configuration, this line of defence will eventually be switched off in a long
session — **and "switched off" has the same outcome as "never existed".**

So there are two tiers, borrowed from biology:

| | Tier 1 innate | Tier 2 adaptive |
|:--|:--|:--|
| Trigger | rule declares `risk_level: low` (omitted counts as low) and the system is not inflamed | `risk_level: high`, a rule requires a witness, or the system is already inflamed |
| Witness | existence + mtime (one `stat` before and after the command) | git change set + content read-back |
| Cost | no git process, no file read | one snapshot, one diff, one file read |
| Can see | "did it touch at all" | "was it the only thing it touched" |
| Cannot see | a third file changed along the way | — |

### The inflammatory storm

A system that has just been contradicted should not keep trusting cheap checks. So verdicts
accumulate:

- **2 consecutive `FAIL`s**, or **1 `INVALID`** → enter `INFLAMED`;
- while inflamed, **every call** is forced to Tier 2, including calls whose own policy declares low;
- **5 consecutive `PASS`es** are required to return to `NORMAL`.

`INVALID` has the lower threshold because it means "I did not see" — a blind line of defence
is already failing.

**Healing is deliberately slower than injury.** Two failures light it, five passes put it out;
one more failure during recovery resets the progress to zero. A symmetric response stops
nothing.

### Inflammation does not vanish on restart

Inflammation held only in memory is a **jailbreak channel**: crash the sidecar, restart, and
suspicion is back to zero. And "can crash the sidecar" is exactly what an audited agent can
do. So inflammation is persisted.

```
<trace dir>/.immune_state.json
{"version": 1, "state": "INFLAMED", "danger_count": 2,
 "recovery_progress": 0, "recovery_passes": 5, ...}
```

- **Atomic write after every verdict** (temp file in the same directory + `fsync` +
  `os.replace`), so a crash cannot leave a half-written file — a half-written file would drop
  exactly the inflammation being recorded.
- Read back at startup. **Missing** = a normal first run; **corrupt / unknown version /
  invalid field** = record `ERROR` (an *amnesia event* in the log), because quietly forgetting
  and being made to forget look identical from outside.
- **Every ambiguity is rounded toward suspicion.** `INFLAMED` with a danger count of 0 → still
  inflamed; recovery progress beyond the threshold → clamped to one step short of it. A
  tampered record may make the system **more** wary, never more relaxed.

```bash
# Default: right next to the trace
harness-audit-server --cwd /path/to/repo

# Somewhere the agent cannot write (recommended: a process that can write the state file can delete it)
harness-audit-server --cwd /path/to/repo --immune-state /var/lib/audit/immune.json

# Explicitly request amnesia — this is the only amnesia entry point
harness-audit-server --cwd /path/to/repo --immune-state none
```

> **Residual risk:** the file is plain JSON and unsigned. An agent able to write that path can
> still delete or alter it. Tamper resistance requires placing it where the audited process
> cannot write, or adding an HMAC — not done in the MVP.
> `test_inflammation_survives_a_restart` proves only that "a restart no longer clears it
> automatically", not that "it cannot be forged".

### A real trace: same policy, two costs

id=5..7 of [`examples/sample_trace.jsonl`](examples/sample_trace.jsonl) are one session:

| id | Call | tier | `immune_state` | Verdict |
|--:|:--|:--|:--|:--|
| 5 | `write_file missing_dir/target.py` | tier2 (declared high) | NORMAL | **FAIL** |
| 6 | `append_file no_such_dir/log.txt` | tier1 (declared low) | **INFLAMED** | **FAIL** |
| 7 | `append_file log.txt` | **tier2** (same low rule) | INFLAMED | **PASS** |

id=7 and id=6 are the same rule and the same tool, at different cost: **inflammation promoted
even a low-risk call to full verification**. This is the second scheduler besides `--rules` —
one driven by history rather than by declaration.

### No relaxation, just less looking

This is a hard constraint: **tiering decides which witnesses are collected, not what a
witness means**.

- A Tier 1 `PASS` is **a PASS on weaker evidence**, not a PASS against contrary evidence:
  report success with no file change and Tier 1 still says `FAIL`
  (`test_innate_tier_still_catches_false_completion`).
- A rule requiring a `git_diff` witness → promoted to Tier 2 automatically, because a stat
  cannot satisfy it; forcing it into Tier 1 would only make it permanently `INVALID`.
- **A tool with no rule is always Tier 2**: **"not declared" is not "low risk"**, otherwise
  adding a `rules/` directory would weaken exactly the tools nobody mentioned.
- A Tier 1 `PASS` carries `[innate tier: existence and mtime only, no git change set]` in
  `reason`, and records `tier` / `witness_scope` / `witness_mtime` in the trace.
- **A content assertion the tier cannot read escalates, it is not dropped.** Tier 1 never opens
  the file, so a call carrying `expected_content` is promoted to Tier 2 and the assertion is
  actually checked. The one exception is a *payload* assertion — the asserted text is the very
  argument the command writes — which is dropped **and recorded** (`dropped_assertions` in the
  record, "content assertion dropped; tier=innate" in `reason`).
  **The payload whitelist is currently exactly one tool: `append_file`.** It is declared by the
  builder (`ToolInvocation(content_is_write_payload=True)`), so a new tool of that shape must set
  the flag itself; if it does not, the call simply escalates to Tier 2. That costs a git change
  set and never yields a false `PASS`, which is why the whitelist is allowed to be implicit in
  behaviour — but it is pinned by `test_only_append_file_declares_a_payload_assertion`, so
  extending it is a deliberate edit rather than a drive-by.
  **Outside a git repository the promotion can still finish — if nobody claimed a change set.**
  What the full witness needs depends on the assertion: a content read needs no repository, so a
  matching assertion there is a real `PASS` (`test_matching_assertion_without_a_change_set_is_a_pass`).
  A claimed `expected_files_modified` does need one, so the same low-risk tool in a scratch
  directory answers `INVALID`, with the missing repository named in `reason`, instead of `PASS`
  on the weaker witness (`test_non_payload_assertion_in_a_non_git_directory_is_invalid`). Both are
  pinned because the failure to avoid is "we did not look" in the first case and "we looked but
  refuse to say so" in the second — the same receipt defect from opposite sides.

### Known blind spot (written into the code, not into a footnote)

Tier 1 cannot see "changed one extra file along the way".
`test_adaptive_tier_sees_drift_the_innate_tier_cannot` turns that blind spot into an assertion:
the same tool is judged `PASS` when declared low and `FAIL` when declared high.

This is also why `write_file` ships declared `high`: overwriting is the one operation whose
content looks right while its side effects can be entirely elsewhere; and **a rewrite of equal
length within one filesystem timestamp tick** is invisible to mtime. Handing that to the cheap
tier trades a few saved milliseconds for an invisible hole.

### Usage

Policy is code; one line changes the tier:

```yaml
rule_id: "append_landed"
target_tool: "append_file"
risk_level: "low"      # changing this to "high" is a one-line diff that goes to review
expectation: "file_modified"
```

```python
from audit_sidecar import ImmuneState, MCPProxy

proxy = MCPProxy(cwd=repo, rules_dir="rules", immune_state=ImmuneState())
proxy.immune_state.is_inflamed()      # whether it is currently inflamed
proxy.immune_state.snapshot()         # {"state": ..., "danger_count": ..., ...}
```

Thresholds are adjustable (`ImmuneState(fail_threshold=3, recovery_passes=6)`), but
`recovery_passes < fail_threshold` is rejected — healing faster than injury is a
misconfiguration, not a choice.

---

## Directory layout

```
agent-harness-audit/
├── README.md
├── audit_sidecar/
│   ├── __init__.py       public interface
│   ├── verdict.py        four-valued verdict engine
│   ├── drift.py          chronic drift detection (R6: a "perfect loop" that is decaying)
│   ├── confidence.py     trust-energy account (sliding window)
│   ├── judgment_base.py  external judgment base (loads rules/*.yaml)
│   ├── rule_engine.py    rule verdicts (tighten on top of the four-valued grid)
│   ├── immune_state.py   immune state machine (inflammation / recovery / persistence) + tier routing (tier1 / tier2)
│   ├── runner.py         witness collection (real execution + independent read-back; full / stat)
│   ├── git_witness.py    git witness (real change set)
│   ├── mcp_proxy.py      MCP interception (protocol-layer claim vs physical effect)
│   ├── mcp_server.py     stdio JSON-RPC server (console script)
│   ├── self_audit.py     self-audit (mode 4 defence)
│   ├── cli.py            command-line entry point
│   ├── tools/
│   │   ├── membrane.py        semi-permeable gate (hash-chained ledger, warrants)
│   │   ├── rule_interface.py  rule packages: digest verification, install, mount, enforce
│   │   ├── ledger.py          hash-chained append-only record
│   │   ├── eco_api.py         the five ecological laws as pure functions
│   │   └── citation_audit.py  DOI/arXiv resolution witness
│   └── rules/
│       ├── write_file.yaml  risk_level: high (overwrites always take the full path)
│       └── append_file.yaml risk_level: low (appends take innate immunity)
├── pyproject.toml        packaging (runtime needs only PyYAML)
├── .gitignore
├── demo_rule_registry.py runnable rule-interface demo (no network, no sockets)
├── examples/
│   ├── sample_trace.jsonl   real audit trail (agent told ok / ledger records FAIL / inflammation escalates)
│   └── README.md            sample notes
└── tests/
    ├── test_verdict.py
    ├── test_confidence.py
    ├── test_judgment_base.py
    ├── test_rule_engine.py
    ├── test_rule_interface.py  packages: integrity, schema, mount, enforcement, tampering
    ├── test_immune.py        tiered immunity (tier routing / inflammation / recovery / still inflamed after restart)
    ├── test_runner.py
    ├── test_git_witness.py
    ├── test_drift.py         chronic drift: the reference run, both gates, refusals
    ├── test_mcp_proxy.py
    └── test_e2e_stdio.py    end to end (real subprocess + real pipe)
```

---

## Quick start

```bash
# 1. Install (PyYAML comes along; default rules are included in the package)
pip install harness-audit-sidecar

# 2. Run it, no path arguments required
harness-audit-server --cwd /path/to/project
```

> The distribution name is `harness-audit-sidecar`; the import package is
> `audit_sidecar`. They differ on purpose: the dist name is what a developer
> types into `pip`, the import name is what existing code already says, and
> renaming the latter would break every `import audit_sidecar` for no gain
> (cf. `beautifulsoup4` -> `bs4`).
>
> Until this release is on PyPI, the same commit installs from git:
> `pip install "git+https://github.com/liao-zhizhi/harness-audit-sidecar.git"`.
>
> Publishing goes through `scripts/publish_pypi.sh`, which builds into
> `.pypi-build/`, asserts exactly two artifacts at the declared version, runs
> `twine check`, and installs the wheel into an empty venv before anything is
> uploaded — `python -m build` succeeding says nothing about whether the artifact
> runs. There are two ways to reach an index:
>
> - **Trusted Publishing (recommended, no stored secret).** Publish a GitHub
>   release, or run the `release` workflow from the Actions tab. PyPI mints a
>   short-lived upload token for that run via OIDC, and the wheels carry PEP 740
>   provenance attestations. A first upload works because PyPI supports *pending*
>   publishers — register one at
>   <https://pypi.org/manage/account/publishing/> (project `harness-audit-sidecar`,
>   workflow `release.yml`, environment `pypi`) before the project exists.
> - **A project-scoped API token**, for a first release before CI is wired up:
>   `TWINE_USERNAME=__token__ TWINE_PASSWORD=pypi-... scripts/publish_pypi.sh --upload`.
>   Scope the token to *this project*, not "Entire account" — an account-wide
>   token can publish to, yank from, or add collaborators to every project you
>   own. PyPI has no API to report a token's scope back, so the script cannot
>   check this for you and says so instead of pretending.

**The package ships with a default rule `no_file_drift` out of the box.**
It is active on install: `write_file` must have both a file witness and a git change set, or
the verdict is `INVALID`. A second bundled rule, `append_landed` (`risk_level: low`),
demonstrates innate immunity: appends are checked for existence + mtime only. To use your own
policy, put `.yaml` files under `rules/` in the working directory -- they are **merged over** the
bundled defaults, so a local rule can add a check or tighten a shipped one but cannot delete one
(a weaker or merely identical redefinition is rejected) -- or point `$HARNESS_AUDIT_RULES` at a
directory, which is used **alone**.

> When run inside a **non-git directory**, the git witness cannot be collected and the verdict
> is `INVALID` (with the reason stated), not a crash — "did not look" is not "looked and
> passed". In that case point `--repo` at a repository, or accept the pure grid explicitly with
> `--rules none`.

## One declaration in, three lines out

`harness-audit check` is the interface for a caller who should not have to
describe how they will be checked. Supply the declaration; the command picks
the witness (`independent`, `local`) and calls the same four-valued judge.

```bash
harness-audit check --file src/main.py    # claim: this file changed
harness-audit check --command "pytest -q" # claim: this command succeeded
harness-audit check --diff                # claim: the working tree changed
```

Default output is the whole public answer:

```text
FAIL
reason: the command exited 3, so the claim that it succeeded is contradicted
next_step: re-run the command by hand to see why it failed, then re-run this check
```

`PASS` prints two lines (there is no next step). Internal fields are wrapped, not
removed — `--verbose` (human) or `--json` (machine) adds `witness_type`,
`producer`, `topology`, `cap_topology`, `failure_kind`, `tier`,
`dropped_assertions`, `cap_fired`, `renegotiation_class`, and the evidence
itself. The trace shows both
`topology: local` and `cap_topology: sidecar`: the provenance enum has no
`local`, so the receipt's word is mapped onto the cap's word explicitly rather
than being hidden in a call site — it is a **semantic mapping, not a claim that
a sidecar process was involved**.

**What `--command` does and does not cover.** It runs the command here and reads
*this process's* exit status, so it judges the operator's claim ("I ran it and
it succeeded"). It does **not** observe an agent's tool layer and cannot tell
you an agent swallowed an exit code and reported success — that is a
`protocol` witness, produced by `SafeRunner(reported_success=...)`, and the
one-line form deliberately does not expose it. If the claim is "my tool says
the tests passed", the party being judged is the tool, not the command.

Exit codes (`0` PASS, `1` FAIL, `2` usage error, `3` INVALID, `4` NOT RUN):
`INVALID` is **not** zero, so a CI step that simply runs the command is blocked
by an ungrounded claim as well as by a contradicted one. That is deliberate —
`INVALID` means "nobody looked", and a gate that passes on that is decoration.
If you want to allow it, branch on the verdict instead of weakening the code:

```yaml
- id: audit
  uses: liao-zhizhi/harness-audit-sidecar@v1
  continue-on-error: true          # or: if: steps.audit.outputs.verdict != 'INVALID'
  with:
    check: file src/main.py
- run: echo "verdict was ${{ steps.audit.outputs.verdict }}"
```

**Three different repairs hide behind one `INVALID`.** Exit code 3 cannot tell
them apart, and neither can `verdict` — both are `INVALID`. What separates them
is the reason's first clause, which is a stable contract:

| kind | reason starts with | what to fix |
|---|---|---|
| tool failure | `self-audit failed` | the tool refused to run; fix the tool |
| bad invocation | `declaration malformed` / `no declaration given` | the call; fix the arguments |
| missing witness | anything else | nothing observed it; collect the witness |

**Rewording a `reason` is a breaking change, not a copy edit.** The prefixes
above are the wire contract: they are named constants in `audit_sidecar/check.py`
(`SELF_AUDIT_REASON_PREFIX`, `MALFORMED_REASON_PREFIX`,
`NO_DECLARATION_REASON_PREFIX`) and every sentence is *built* from them rather
than retyped, so a sentence cannot drift away from the classifier that reads it.
Changing one of those strings will silently move the failure into another row of
this table — the tests catch that (`failure_kind` is asserted end to end against
real CLI output), but a reworded prefix still breaks every workflow that matches
on it. Treat the first clause as an API version: add a new kind, do not rename
an existing one. The rename gate is in
[`RELEASE_CHECKLIST.md`](RELEASE_CHECKLIST.md), and the tests fail if that
checklist and these constants disagree.

The Action exposes `verdict` / `reason` / `exit_code`, so a workflow branches on
the **reason prefix**. A direct CLI consumer gets the same distinction as a
field instead of a string: `--json` carries `failure_kind`, one of
`self_audit` / `declaration_malformed` / `witness_missing` (`null` for PASS and
FAIL, which are decided). The field is derived from the sentence it labels, so
the two cannot disagree — never branch on `verdict` to separate these, it is
`INVALID` for all three:

```yaml
- id: audit
  uses: liao-zhizhi/harness-audit-sidecar@v1
  continue-on-error: true
  with:
    check: file src/main.py
- run: |
    reason="${{ steps.audit.outputs.reason }}"
    case "${reason}" in
      "self-audit failed"*)
        echo "::error::harness-audit itself is broken; the claim was not judged"; exit 2 ;;
      "declaration malformed"*|"no declaration given"*)
        echo "::error::bad invocation: ${reason}"; exit 2 ;;
      "")
        ;;
      *)
        echo "not judged (witness missing): ${reason}"
        echo "run \`harness-audit baseline\`, then re-run the check" ;;
    esac
```

"Changed" is a comparison, so `--file` and `--diff` need a recording first:

```bash
harness-audit baseline --file src/main.py   # ~/.gradia/baseline.json, keyed by repo
harness-audit baseline --local              # ./.harness-baseline.json instead
```

Without one, `--file` can only answer "exists / does not exist" and `--diff`
only "the change set is empty / not empty". A file that exists but was never
recorded is `INVALID` with a `next_step` naming **this path**, e.g. `run
\`harness-audit baseline --file src/main.py\` first, ...` — never a quiet `PASS`
on existence, and never a placeholder the operator has to edit. A *missing*
file is still `FAIL` (that fact needs no before), and a deletion since the
recording counts as a change because the recording held a digest.

Two limits of the recording, stated rather than discovered:

* **A recording is per path.** Only the paths named with `--file` get a digest.
  A path the recording does not name is `INVALID` with "the recording does not
  name it" and a next step that adds *that one path* — not an instruction to
  re-take a full recording.
* **`--local` writes where it was invoked.** A recording taken in `repo/sub/`
  is invisible to a check run in `repo/` (the search is the invocation
  directory, then the repository root, then `~/.gradia`). The `INVALID` receipt
  lists the exact files it looked for under `baseline_searched`, so the miss is
  diagnosable; take the recording at the root, or pass the same `--cwd`.

`git status` being dirty is **not** an implicit baseline: "changed vs HEAD" is a
different claim from "changed since your recording", and answering the second
with the first would borrow a witness the caller never asked for. `--file` still
records `git_dirty` in its evidence (`--verbose`), it just never lets it decide.

### `harness-audit setup`: the install step, applied

`init` prints the MCP stanza and never edits a client config. `setup` is the
opt-in version of that trust: it detects Cursor's config (`./.cursor/mcp.json`
first, then `~/.cursor/mcp.json`), merges one `harness-audit` entry into it,
merges one `@alias` into `ref_roots.json`, and records a baseline.

```bash
harness-audit setup --print-only   # preview; writes nothing
harness-audit setup                # apply
```

* **Merged, not replaced.** Other servers in `mcp.json` and other aliases in
  `ref_roots.json` survive. A file that cannot be parsed is refused (exit `2`)
  rather than rewritten: "it was backed up" is not a substitute for not
  destroying it.
* **Backed up first.** Every replaced file is copied to `<file>.bak`, then
  `.bak.1`, `.bak.2` on later runs -- overwriting `.bak` would replace the
  original content with the already-modified content, which is a backup that
  cannot restore anything.
* **No second console script.** The entry is
  `{"command": "harness-audit", "args": ["mcp"]}`; `harness-audit mcp` is the
  stdio server.
* **The baseline is capped and the cap is reported** (default 200 files,
  `--max-files`). Paths past the cap stay `INVALID` for `--file` with the exact
  command that fixes it; the alternative -- an unbounded walk over a monorepo --
  is a setup command that appears to hang.
* **`--print-only` runs the same detection and merge** and prints the result.
  It is the same code path with the writes withheld, not a second description
  that can drift from the real one.

Output names every file it wrote, how many baseline entries it recorded, and
the next step (restart Cursor so the MCP config is reloaded).

### `check --upload`: a display row, not a witness

`--upload` is off by default. When set, the verdict is POSTed to the
dashboard's `/api/v1/check/report` (base URL from `--upload-url` or
`HARNESS_AUDIT_UPLOAD_URL`; default `http://127.0.0.1:8000`).

Exactly three fields leave the machine: `verdict`, `reason`, `check`. Both text
fields are redacted first -- `file /home/me/app/main.py` becomes
`file <redacted>`, and any absolute path left in the reason is replaced with a
fixed marker. No code, no digest, no agent id, no `agent_id` field at all.

The same flag also POSTs the usage aggregate (`verdicts`, `baseline_misses`,
`tool_names`) to `/api/v1/usage/report`. That is a *second* request carrying
counts only -- the usage rows themselves, with their timestamps and path hashes,
never leave the machine.

A failed upload never changes the exit code: the verdict was decided locally,
and a display being down is not evidence about the claim. Its result goes to
stderr so `--json` stays parseable and the first line of stdout remains the
verdict.

### `harness-audit up`: one command, three lines

`setup`, `baseline` and `check` are each honest about their preconditions, which
means a first run needs three of them. `up` runs whichever are missing and then
judges, printing **at most three lines**: the verdict, `reason:`, and -- unless
the verdict is `PASS` -- `next_step:`.

```bash
harness-audit up                      # configure if needed, record if needed, check --diff
harness-audit up --file src/main.py   # judge one file
harness-audit up --command "pytest -q"  # judge one command
```

* **It orchestrates; it does not judge.** No new verdict logic, no new reason
  vocabulary. A failed prerequisite is reported as `INVALID` -- "nothing judged
  this" -- which is the value that already means that.
* **Failures end in a runnable command with this invocation's arguments in it**,
  e.g. `harness-audit baseline --cwd /work/app --file src/main.py && harness-audit up --file src/main.py --cwd /work/app`.
  A placeholder is a command the operator must edit before it can answer them.
* **Setup and baseline chatter is captured, not printed.** A first run that
  prints twelve lines makes the one line that matters unfindable; `--verbose`
  adds the check's trace *after* the three lines, on request.

### `harness-audit watch`: notify, never repair

Polls the current repository (default every 2s), runs `check --diff` when the
working tree changes, and raises a desktop notification on `FAIL` or `INVALID`.

```bash
harness-audit watch            # ctrl-c to stop
harness-audit watch --interval 5
```

* **The signature is `git status --porcelain` plus the size and mtime of every
  dirty or untracked path.** Porcelain alone does not change when an
  already-dirty file is saved again, so a watcher built on it alone goes quiet
  during an agent's edit-edit-edit sequence.
* **`notify-send` on Linux, `osascript` on macOS, skipped on Windows.** The body
  goes to stderr either way, so the loop still works over SSH; the notification
  reads `FAIL: <first sentence of the reason> run \`harness-audit up\` for details`.
* **No auto-fix, no auto-baseline, no ledger, no upload.** Re-baselining after a
  change would make the change look like it happened before the claim -- the
  exact fabrication `check` refuses. `PASS` is not notified: an interruption for
  "everything is fine" is how notifications get muted.

### Usage log: what was asked, never what was shown

`check`, `baseline`, `setup`, `mcp_tool_call` and `suspected_uncovered` append
one JSON row each to `~/.gradia/usage.jsonl` (override the directory with
`HARNESS_AUDIT_USAGE_DIR`). It is append-only and never rewrites a row.

* **No claim text, no plaintext paths, no code.** A command claim records
  `declaration: "command"`; a file claim records `path_hash`, an HMAC-SHA256 of
  the repository-relative path under a machine-local key
  (`~/.gradia/usage.salt`, created `0600`). Anyone holding both the key and the
  log can confirm a guessed path -- that is the honest limit of a hash -- so the
  key is the boundary, and deleting it makes old rows unlinkable.
* **`baseline_present`** is recorded on every `check`: `false` is the common
  first-run state, and counting it is how "the operator never recorded" gets
  distinguished from "the claim was contradicted".
* **`suspected_uncovered`** is written when a `FAIL` has no audited write tool
  call for the same path in the previous ten minutes. That is the signature of a
  change made through a path the harness cannot see -- the editor's own file API,
  a shell it did not launch -- and it is a *suspicion*, because a plain lie
  produces the same trace.
* **It never votes.** `record` swallows I/O errors, so a read-only home cannot
  turn a `PASS` into a traceback; `check`'s exit code is computed before the log
  is touched.
* **`--upload` sends three aggregates** to `/api/v1/usage/report` --
  `verdicts`, `baseline_misses`, `tool_names` -- and never the rows. The export
  is a separate function from the append so that adding a field to a row cannot
  silently widen what leaves the machine.

### GitHub Action

The action takes one input. `check` is the same declaration as above:

```yaml
- uses: liao-zhizhi/harness-audit-sidecar@v1
  with:
    check: file src/main.py
```

No `pre-pr-audit-path` (or any other path input) is needed: the action installs
`harness-audit-sidecar` from PyPI and calls `harness-audit check`. The witness is
collected on the runner, so the workflow supplies the claim, not a location.

Note the trade-off that comes with installing from the index: `@v1` pins the
*workflow*, not the bytes. If a release must be reproducible, pin the package
version in your own step instead of relying on the ref.

Legacy `file:` / `command:` / `diff:` inputs are still read when `check` is
empty, so an existing workflow keeps working; `check` wins when both are set.
The step propagates `check`'s exit code, so `FAIL` (1) and `INVALID` (3) both
fail the job by default; the `verdict` / `reason` / `exit_code` outputs are the
stable contract if you need to distinguish them.

Developing from source:

```bash
cd agent-harness-audit
pip install -e .

python3 -m unittest discover -s tests -p 'test_*.py'   # 707 tests (must run from the repository root)
python3 -m audit_sidecar.cli                # demo: self-audit first, refuses to start if it fails

harness-audit-server --cwd /path/to/project # no --rules needed
```

Programmatic use:

```python
from audit_sidecar import ConfidenceAccount, Verdict, VerdictEngine

engine = VerdictEngine()

# Intent is to write new content; the witness (file hash) shows the old content; the agent reports success
verdict = engine.evaluate(
    intent="sha256:new",
    effect="sha256:old",
    check=True,
)
assert verdict is Verdict.FAIL        # false completion

account = ConfidenceAccount(window=100)
account.record(verdict)
account.get_balance()                 # 0.0
account.get_label()                   # "low"
```

Run a real command and take a witness (see "Witness collection" below):

```python
from audit_sidecar import SafeRunner

result = SafeRunner().run_shell(
    "echo 'feature_x=on' >> config.yaml",
    cwd="/tmp/project",
    intent_desc="append feature_x=on to config.yaml",
    expected_file="config.yaml",
    expected_content="feature_x=on",
)
print(result.verdict, result.effect_observed, result.check_observed)
```

A verdict with the judgment base (see "External judgment base" below):

```python
from audit_sidecar import JudgmentBase, MCPProxy, RuleEngine

engine = RuleEngine(JudgmentBase("rules"))
decision = engine.judge(
    "write_file",
    intent=True,
    effect=True,                     # file witness: the write did land
    check=True,                      # tool layer tells the agent it succeeded
    witnesses={"file", "agent_report"},  # but no git change set
)
print(decision.verdict, decision.rule_id)   # INVALID no_file_drift

# With the interception layer attached, the same judgment happens automatically and goes into the trace
proxy = MCPProxy(cwd="/path/to/repo", rules_dir="rules")
```

---

## Verdict rules (`verdict.py`)

Three inputs:

| Argument | Meaning | Supplied by |
|:--|:--|:--|
| `intent` | a comparable representation of the intended action (e.g. "the hash it should have afterwards") | task definition |
| `effect` | the side effect observed **independently of the agent**; `None` means not collected | witness collector |
| `check` | the state the agent itself read (bool / status string / object with `.success`) | agent self-report |

Rules match in order, **first match wins**:

| # | Condition | Verdict | Meaning |
|:--:|:--|:--|:--|
| 1 | `effect is None` | `INVALID` | no witness. **Never PASS**, even when `check=True` |
| 2 | `intent is None` | `NOT RUN` | no intent recorded, the scenario never executed |
| 3 | `effect != intent` | `FAIL` | intent not realised; if `check` reports success, that is **false completion** |
| 4 | `check` reports failure | `FAIL` | **false failure**: the side effect landed but the agent reported an error |
| 5 | otherwise | `PASS` | witness and report agree |

Two deliberate design decisions:

1. **All three arguments are keyword-only.** `effect` and `check` have the same shape, and a
   positional slip produces a "plausible-looking" wrong verdict rather than an error. An
   audit tool cannot afford that kind of silent failure.
2. **The enum member is `NOT_RUN` and its value is `"NOT RUN"`.** Python identifiers cannot
   contain a space; the value keeps the paper's original spelling.

`engine.explain(...)` returns `(Verdict, reason)`, where reason is a machine-checkable English
sentence, so a verdict can be logged rather than merely tagged. `evaluate` and `explain`
share one block of decision logic, so two sets of rules cannot drift apart.

Normalisation of `check` accepts only **unambiguously interpretable** reports; an unrecognised
string (such as `"probably fine"`) raises `ValueError` rather than being guessed as success.
Guessing is exactly the behaviour this tool exists to eliminate.

---

## Trust-energy account (`confidence.py`)

```python
account = ConfidenceAccount(window=100)   # default 100
account.record(verdict)
account.get_balance()   # PASS / (PASS + FAIL + INVALID)
account.get_label()     # high / medium / low
```

- A sliding window keeps the most recent N verdicts; older events age out.
- **`NOT RUN` occupies a window slot but not the denominator**: it carries no information
  about the harness and must not dilute the ratio.
- `PASS / (PASS+FAIL+INVALID)`:
  - `>= 0.7` → `high`
  - `[0.5, 0.7)` → `medium`
  - `< 0.5` → `low`
- With no valid events in the window, `balance = 0.0` and `label = low`: **no evidence is not
  trust**.

---

## Witness collection (`runner.py`)

`SafeRunner` really executes a shell command and then **reads the result back from the
filesystem itself**, rather than believing what the command says about itself.

```python
from audit_sidecar import SafeRunner

runner = SafeRunner()
result = runner.run_shell(
    "echo 'feature_x=on' >> config.yaml",
    cwd="/tmp/project",
    intent_desc="append feature_x=on to config.yaml",
    expected_file="config.yaml",
    expected_content="feature_x=on",
)

result.check_observed    # True   -- bash exit code (under pipefail)
result.effect_observed   # True   -- really read from the file, independent of the exit code
result.verdict           # Verdict.PASS
```

`ExecutionResult` fields: `intent` (description), `effect_observed`
(`True`/`False`/`None`), `check_observed`, `verdict`, plus `exit_code`, `pipe_status`,
`witness_path`, `reason`, `stdout`, `stderr` and `error` as forensic record.

### Defence one: a pipeline eats the status (mode 1)

`false | cat` exits **0** in default bash — the last stage succeeded, and nobody watched the
earlier failure. An agent that has run its work through `| head`, `| tee` or `| grep` can
therefore hide a failure **without having lied**.

`SafeRunner` runs with `set -o pipefail` and records each stage's `PIPESTATUS` in `pipe_status`:

```
$ bash -c 'false | cat'; echo $?            # naive reading
0
$ bash -o pipefail -c 'false | cat'; echo $?  # audit reading
1
```

> Implementation note: the command runs from a temporary wrapper script rather than
> `bash -c`. This lets `PIPESTATUS` be read **before any subsequent command**, and the wrapper
> explicitly `exit`s with the pipeline status, since otherwise the final `printf` would
> overwrite the exit code with 0.

### Defence two: witness coordinate drift (a mode 3 variant)

This is the subtler class: the agent does its work **in the wrong directory**, and the audit
looks for evidence **in the same wrong directory**. It finds it, and returns a confident
PASS — the work never reached the target.

`witness_root` exists for this: the command's `cwd` and the witness coordinates are
**specified separately**.

```python
# the agent runs in drift/, but the intent's coordinates are project/
result = runner.run_shell(
    "echo 'feature=on' >> config.yaml",
    cwd=drift,                                  # the action happens here
    expected_file="config.yaml",
    witness_root=project,                       # the witness is read here
    expected_content="feature=on",
)
result.verdict   # Verdict.FAIL -- false completion
```

When `expected_file` is an absolute path it is used directly and `witness_root` is ignored.

### Real verdicts (section [3] of `python3 -m audit_sidecar.cli`)

| Case | Command | exit | PIPESTATUS | effect | check | Verdict |
|:--|:--|--:|:--|:--|:--|:--|
| A1 pipeline eats status | `false \| cat` | 1 | `1 0` | `None` | False | **INVALID** |
| A2 same + witness | `false \| cat > result.txt` | 1 | `1 0` | False | False | **FAIL** |
| B1 witness follows drift | `echo 'feature=on' >> config.yaml` (cwd=drift, witness drift) | 0 | `0` | True | True | **PASS (audit blinded)** |
| B2 witness pinned to intent | same, witness project | 0 | `0` | False | True | **FAIL** |

Four points are worth noting:

- **A1 is not PASS, nor is it "guessed" into FAIL.** The exit code says failure, but no
  artefact was specified, so `effect` is `None` and the engine can only return `INVALID`.
  **No witness, no conclusion** — this is precisely where four-valued and boolean verdicts
  part ways.
- **A2 is the real FAIL.** The artefact exists but is empty; the intent was not realised, and
  `pipefail` makes the exit code agree with the witness.
- **B1's PASS is a blinded audit, not a correct agent.** `project/config.yaml` was never
  touched. The same situation is caught in B2; the only difference is where the witness is read.
- To catch drift, **the witness must be pinned to the intent's coordinates.** Letting the
  witness follow the action is letting the suspect hold the evidence.

### Failure paths

| Situation | `effect` | Verdict | Reason |
|:--|:--|:--|:--|
| `cwd` missing / cannot start | False | `NOT_RUN` | the command never ran; `INVALID` is reserved for "ran but no witness" |
| killed on timeout | `None` | `INVALID` | file state after a kill is intermediate and cannot serve as evidence |
| no `expected_file` given | `None` | `INVALID` | it was not asked to look, so it did not look |
| command contains a top-level `exit` | read normally | judged normally | the wrapper terminates early, so `pipe_status` falls back to `(exit_code,)` |



## Git witness (`git_witness.py`)

File existence can prove "some file was written"; it cannot prove "**that file** was written
correctly". The most common form of mode 3 is an agent working in the wrong coordinates and
then reading back the file it just wrote and reporting success — its self-report and its
read-back are **perfectly consistent with each other**, and both are off target.

Git is the only record the agent did not help write. `GitWitness` reads it.

```python
from audit_sidecar import GitWitness

witness = GitWitness("/path/to/repo")
witness.get_changed_files()      # ['wrong_dir/target.py']  repository-root relative, POSIX, sorted
witness.repo_root                # '/path/to/repo'
```

Underneath it is `git status --porcelain=v1 -z --untracked-files=all --no-renames`:

- **`-z`**: filenames are not escaped, so quoting cannot turn a path into a different string.
- **`--untracked-files=all`**: lists files inside newly created directories. Drift usually
  looks like this, and reporting only the directory name would miss it.
- **`--no-renames`**: a rename does not produce "two paths, one record", so parsing cannot
  swallow the next entry.
- Covers staged, unstaged, untracked and deleted; anything ignored by `.gitignore` does not
  appear (git does not report it).

### Two problems you must solve yourself

**Coordinates.** `git status` reports paths **relative to the repository root**. A command in
`sub/` correctly editing `sub/target.py` has git say `sub/target.py`, while the caller means
`target.py` — comparing them directly would **judge a correct edit as drift**.
`resolve_expected()` first maps the expected path into repository-root coordinates using the
command's `cwd`. This rule and `witness_root` teach the same lesson: **before comparing two
things, confirm they are in the same coordinate system**.

**Baseline.** The working tree is usually already dirty before the command runs. A single
snapshot afterwards cannot distinguish "changed by this command" from "already like that".
`snapshot()` records a content fingerprint for every dirty path, and `changed_since(baseline)`
reports only paths that are **new or whose content really changed**. Without this step, a
long-lived repository would present all its historical changes as evidence for this call.

```python
baseline = witness.snapshot()
# ... execute the command ...
witness.changed_since(baseline)   # only changes caused by this command
```

### Wiring into `SafeRunner`

```python
result = runner.run_shell(
    "mkdir -p wrong_dir && echo 'value=1' > wrong_dir/target.py",
    cwd="/repo",
    intent_desc="edit target.py",
    expected_files_modified=["target.py"],   # relative to cwd
)
result.changed_files      # ('wrong_dir/target.py',)
result.missing_expected   # ('target.py',)
result.unexpected_changes # ('wrong_dir/target.py',)
result.verdict            # Verdict.FAIL
```

- The baseline is taken automatically **before** the command runs.
- `expected_files_modified` is relative to `cwd`; when `cwd` is outside the repository (with
  `repo_path`), it is relative to the repository root.
- Given together with `expected_file` / `expected_content` it is a **conjunction**: any
  witness saying no means no PASS.
- If an artefact witness is also given, that artefact file itself does not count as an
  "unexpected change".

### Strict vs permissive: strict by default

| Mode | Rule | Scenario |
|:--|:--|:--|
| `allow_extra_changes=False` (default) | the change set must be **exactly** the expected set | edited A correctly but also B → FAIL |
| `allow_extra_changes=True` | the expected set is a **subset** of the change set | permits build artefacts, formatting and similar collateral |

Strict is the default because "changed the right thing and something else too" is more
dangerous than "changed nothing", and harder to notice afterwards.
> Note: the two modes were described both ways in the requirements ("subset means True" and
> "a file outside the expectation means False"), which give opposite results for the
> "contamination" case. The latter is implemented as the default and the former kept as an
> explicit switch.

### Real output for Case C (section [4] of `cli.py`)

```
    case                             verdict  changed (from git)                 effect  check
    -------------------------------- -------- ---------------------------------- ------- ------
    C1  correct edit                 PASS     target.py                          True    True
    C2  edited wrong_dir/target.py   FAIL     wrong_dir/target.py                False   True
    C3  edited target.py + other.py  FAIL     other.py, target.py                False   True
```

**C2 is the point**: the agent's exit code says success, and its own read-back of
`wrong_dir/target.py` really does return the correct content — consistent in both places, and
both off target. Only git knows the truth, because git is the record the agent did not write.
C3 demonstrates the strict default: changing one unrelated file also counts as drift, not as
extra credit.

### Failure paths

| Situation | Behaviour |
|:--|:--|
| `repo_path` is not a git repository | raises `GitWitnessError`, **the command does not run** |
| `git` not on PATH | raises `GitWitnessError` (no silent downgrade to "no changes") |
| expected path outside the repository | raises `ValueError` |
| bare string instead of a list | raises `TypeError` (`"abc"` would be iterated as three paths) |
| empty expected set | raises `ValueError` (use `expected_file`, or pass nothing) |

**Never downgrade silently**: if it cannot look, it says so. A witness that reports "cannot
observe" as "no changes" is worse than no witness.



## MCP interception (`mcp_proxy.py`)

The first three layers audit **commands**. This layer audits the **tool layer** — an entirely
different defendant.

In a real harness the command's exit code is seen by the tool layer first, and the agent sees
only the tool layer's reply. If the wrapper swallows a failure and returns `success`, the agent
goes wrong **sincerely**: its self-report is perfectly consistent with everything it received,
and not one word in the conversation is a lie. This class of false completion is the hardest to
find, because there is nowhere to look.

`MCPProxy` sits between the agent and the tool layer:

```
Agent ──tool_call──▶ MCPProxy ──▶ tool layer ──▶ real command
   ▲                    │                        │
   └──── returned as is ┘                        ▼
        (the agent's beliefs are not corrected)  independent witness: GitWitness
                        │
                        └──▶ VerdictEngine(intent, effect=physical witness, check=protocol claim)
                             └──▶ internal audit log (invisible to the agent)
```

```python
from audit_sidecar import MCPProxy

proxy = MCPProxy(cwd="/repo")

# agent_reported_success is the status the tool layer handed the agent, i.e. the claim under audit
response = proxy.intercept_tool_call(
    "write_file",
    {"path": "missing_dir/target.py", "content": "x"},
    agent_reported_success=True,      # the tool layer says it succeeded
)
response            # {'status': 'ok', 'isError': False, ...}  <- the agent believes all is well
proxy.audit_log[-1].verdict    # Verdict.FAIL                   <- the audit record disagrees
```

### Two non-negotiable principles

**One: `check` must come from the protocol layer and must not be re-derived from the exit code.**

This is the sole reason this module exists. Writing `check = reported_success and exit_code == 0`
for convenience replaces the tool layer's reply with the exit code the tool layer itself saw —
**letting the defendant testify for itself**. Measured, that "whitewash" really does flip
verdicts:

| Scenario | Correct implementation (protocol claim as check) | Deriving from exit code (wrong) |
|:--|:--|:--|
| tool says success, command exits 0, file unchanged | `FAIL` false completion | `FAIL` false completion (same by luck) |
| tool says success, command exits 1, file unchanged | `FAIL` false completion | `FAIL` intent not realised (**wrong diagnosis**) |
| **tool says failure, command exits 0, file really changed** | **`FAIL` false failure** | **`PASS`** ← the lying tool is whitewashed |

The last row is the crux. `test_check_is_not_rederived_from_exit_status` asserts
`check_source == "protocol"`, `check_observed is True` and `exit_code != 0`, so any change to
the derivation fails the test immediately.

**Two: the proxy is passive.**

The response returned to the agent **mirrors** the tool layer's claim exactly and never mixes in
the witness's conclusion. `MCPProxy` does not correct the agent's beliefs; it only records the
disagreement in its own log. An "auditor" that corrects the agent on its behalf stops being an
observer and becomes part of the system under audit — and once it errs, there is no second pair
of eyes.

`test_response_stays_passive` pins this down: in the lying scenario the response is still
`status='ok'`, and neither `FAIL` nor `witness` appears in it.

### Real output for Case D (section [5] of `cli.py`)

```
    case                               exit  agent check  actual effect  verdict
    ---------------------------------- ----- ------------ -------------- --------
    D1  write_file, honest tool        0     True         True           PASS
    D2  tool swallowed a path error    1     True         False          FAIL
    D3  tool under-reported success    0     False        True           FAIL
```

D2 is the one to look at: **`Agent Check: True` / `Actual Effect: False`**. The reason string
also points out the tool layer's self-contradiction:

```
false completion: report says success, witness says no effect
(expected but untouched: missing_dir/target.py)
[tool layer reported success, underlying command exited 1]
```

D3 is the other side: the write really landed and the tool layer reported an error. The agent
believes it failed and retries — repeated writes caused by a "false failure" are usually harder
to clean up than a false completion.

### Argument safety

Tool arguments are spliced into a really executed shell command, so **every interpolation goes
through `shlex.quote`**. Otherwise the audited content becomes code: `content="x; rm -rf /"`
would write the file and then execute.

`tests/test_mcp_proxy.py` pins this with three payloads (`evil; touch PWNED`,
`$(touch PWNED2)`, `` `touch PWNED3` ``), asserting the file content matches the payload
**byte for byte** and that no PWNED file exists.

### Failure paths

| Situation | Behaviour |
|:--|:--|
| unknown tool name | raises `UnknownToolError`, **not executed, not logged** (refuses to forge an audit record) |
| `agent_reported_success` not a bool | raises `TypeError` (`None` is refused too: use `False` for "no status reached the agent") |
| missing / non-string argument | raises `ValueError` / `TypeError` |
| `expected_files_modified` given a bare string | raises `TypeError` |

**Why an unknown tool is refused rather than answered with an error response**: returning a
generic error and logging nothing lets unaudited calls accumulate silently. An interception
layer that "looks diligent but has a blind spot" is more dangerous than none, because the caller
believes it is watching. Production should record and pass through so the agent's flow is not
interrupted; the MVP chooses to fail loudly.

## Attaching as a sidecar (stdio)

Install:

```bash
pip install .
# or build locally: python3 -m pip wheel . --no-deps -w dist/
```

That gives a directly startable stdio service:

```bash
harness-audit-server --cwd /path/to/project
# equivalent to: python3 -m audit_sidecar.mcp_server --cwd /path/to/project
```

| Argument | Default | Meaning |
|:--|:--|:--|
| `--cwd` | `.` | execution directory for the audited commands |
| `--repo` | same as `--cwd` | repository root used by the git witness |
| `--trace` | `<cwd>/audit_trace.jsonl` | audit trail (JSONL) |
| `--immune-state` | `.immune_state.json` (same directory as `--trace`) | persistence file for the inflammation count; `none` keeps it in memory only (forgotten on restart, the only amnesia entry point) |
| `--timeout` | `30` | per-command timeout (seconds) |
| `--quiet` | off | silence stderr diagnostics |

### Protocol

Newline-delimited JSON-RPC 2.0, one object per line (the line mode most stdio MCP transports
use).

**Request**

```json
{"jsonrpc":"2.0","method":"tool_call",
 "params":{"tool":"write_file",
           "args":{"path":"target.py","content":"v = 1\n"},
           "reported_success":true},
 "id":1}
```

**Response** (this is what the agent sees)

```json
{"jsonrpc":"2.0","id":1,
 "result":{"status":"ok","isError":false,
           "content":[{"type":"text","text":"write_file: ok"}]}}
```

**Audit trail** (invisible to the agent, written to `audit_trace.jsonl`)

```json
{"id":1,"tool":"write_file","intent":"modify file target.py",
 "agent_reported_success":true,"check_observed":true,"check_source":"protocol",
 "effect_observed":true,"verdict":"PASS","reason":"witness matches intent, report agrees",
 "exit_code":0,"pipe_status":[0],"changed_files":["target.py"], ...}
```

### Wiring into an agent client

Any framework that can launch a stdio MCP server will work. A typical client configuration:

```json
{
  "mcpServers": {
    "harness-audit": {
      "command": "harness-audit-server",
      "args": ["--cwd", "/path/to/your/project", "--quiet"]
    }
  }
}
```

The client will call `initialize`, send `notifications/initialized`, then discover the tools with
`tools/list` and call them with `tools/call` — no adapter needed. The same server also accepts the
legacy `tool_call` method, so a hand-written script keeps working.

Key points:

1. `--cwd` must be the **audited project's** directory, not the proxy's own.
2. For a git project, `--repo` is usually left empty (it takes `--cwd`); specify it explicitly
   when they differ.
3. Poll `audit_trace.jsonl` for alerting / dashboards; do **not** take verdicts from stdout —
   that carries only what the agent is meant to see.
4. The verdict file is appended line by line and can safely be read in parallel by `tail -f` or
   a log collector.
5. **The inflammation count is written next to the trace by default** (`.immune_state.json`) and
   survives restarts. If the audited agent can write to that directory, it can delete or tamper
   with the file — in that case point `--immune-state` somewhere the agent cannot write (see
   [Inflammation does not vanish on restart](#inflammation-does-not-vanish-on-restart)).

### Measured: it blocks right after install

```bash
$ pip install .
$ printf '%s\n' \
  '{"jsonrpc":"2.0","method":"tool_call","params":{"tool":"write_file","args":{"path":"ok.py","content":"fine"},"reported_success":true},"id":1}' \
  '{"jsonrpc":"2.0","method":"tool_call","params":{"tool":"write_file","args":{"path":"nope/ok.py","content":"lie"},"reported_success":true},"id":2}' \
  | harness-audit-server --cwd /repo --quiet
{"jsonrpc": "2.0", "id": 1, "result": {"status": "ok", ...}}
{"jsonrpc": "2.0", "id": 2, "result": {"status": "ok", ...}}    <- the agent was told success twice
$ cat /repo/audit_trace.jsonl
  id=1 check=True(protocol) effect=True  -> PASS
  id=2 check=True(protocol) effect=False -> FAIL   <- the lie is here
```

### Running in a container

```bash
docker build -t harness-audit-sidecar:0.10.1 .
mkdir -p .audit-state
docker run --rm -i \
  --user "$(id -u):$(id -g)" \
  -v "$PWD:/work" -v "$PWD/.audit-state:/state" \
  harness-audit-sidecar:0.10.1
```

`-i` is required: the server speaks stdio, and without an open stdin it exits after a handshake
that never arrives. `--user` is not cosmetic — two failures follow from getting it wrong, and
both were reproduced while writing this section:

| Without `--user` | What you see | Why |
|:--|:--|:--|
| container runs as uid 10001, the mount belongs to you | `cannot read a git repository at /work: ... detected dubious ownership` | git refuses another user's repo |
| same | `Permission denied` writing the working tree | every `write_file` fails physically |

So the image ships `git` (the change-set witness is read from it; without git every declared
change set degrades to `INVALID`) and the entry point refuses to start unless the engine still
rejects its own known-bad fixtures. `EXPOSE 8080` is reserved for the local REST API, which does
not exist yet — nothing listens on it, and the stdio server does not need it.

### Protocol-layer errors

| Situation | JSON-RPC error code |
|:--|:--|
| invalid JSON | `-32700` parse error |
| not a JSON object / `jsonrpc` not `2.0` | `-32600` invalid request |
| unknown `method` | `-32601` method not found |
| `tools/list` or `tools/call` before `initialize` | `-32002` server not initialized |
| `params` missing fields / wrong types / **missing `reported_success`** on `tool_call` | `-32602` invalid params |
| unknown tool name on `tools/call` | `-32602` (MCP's documented code for this case) |
| unknown tool name on the legacy `tool_call` | `-32001` (application code, **not executed, not traced**) |
| `OSError` during execution | `-32603` internal error |

Three deliberate choices:

- **Refuse when `reported_success` is missing; do not default to `True`.** A default would
  invent "the claim under audit" out of thin air, so every call would judge PASS — auditing
  becomes rubber-stamping.
- **Notifications (no `id`) get no response.** JSON-RPC forbids it, and replying would
  desynchronise the client's state machine. They are **still executed and recorded**.
- **A failed trace write does not stop the service.** It only reports to stderr. An auditor
  that refuses to serve when it cannot log would drag the agent down with it; a logging fault
  is an incident, not an excuse.

### MCP handshake

A standard client handshake works as-is. `initialize` negotiates the protocol version, and only
capabilities this server implements are advertised:

```
$ printf '%s\n' \
  '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"probe","version":"1.0"}}}' \
  '{"jsonrpc":"2.0","method":"notifications/initialized"}' \
  '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' \
  | harness-audit-server --cwd /repo --quiet
{"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2025-06-18",
  "capabilities": {"tools": {"listChanged": false}},
  "serverInfo": {"name": "harness-audit-sidecar", "version": "0.10.1"}}}
{"jsonrpc": "2.0", "id": 2, "result": {"tools": [
  {"name": "append_file", "description": "...", "inputSchema": {...}},
  {"name": "run_command", ...}, {"name": "validate_citation", ...}, {"name": "write_file", ...}]}}
```

Supported versions: `2025-06-18`, `2025-03-26`, `2024-11-05`. A request for any other version is
answered with the newest supported one, which is what the spec asks for; the client decides
whether to continue. Four decisions worth stating:

- **`tools/*` before `initialize` is refused (`-32002`), not tolerated.** Initialization is
  required to be the first interaction; accepting the omission hides a client bug until a
  capability mismatch surfaces somewhere less obvious. The refusal happens **before** executing:
  a rejected call must not have written the file.
- **`instructions` is omitted from the handshake result.** It is the one handshake field that
  reaches the model, and telling an agent it is being audited removes the false completions the
  audit exists to measure.
- **`tool_call` is exempt from the handshake.** It predates it, and gating it would invalidate
  every trace recorded before v0.10.1.
- **`tools/call` has no claim field in MCP** — the response *is* the report. So the claim comes
  from `params._audit.reported_success` when a client supplies it (out-of-band, so it never
  reaches the model and cannot be set by it), and otherwise from the meaning of a non-error tool
  result. Every trace record says which, in `claim_basis` (`explicit` or `tool-return`), because
  an implicit claim is a weaker statement than a supplied one.

## Self-audit (`self_audit.py`)

The paper's failure **mode 4** is "the auditor itself becomes decoration": it always returns
PASS (or always FAIL), and nobody notices, because the auditor is never audited.

`run_self_audit()` feeds the engine samples whose **answers are known**:

| Sample | Construction | Expected |
|:--|:--|:--|
| known-bad | pretend to write (nothing written), hash still old, agent reports success | `FAIL` |
| known-good | really write, hash changed, agent reports success | `PASS` |
| no-witness | agent reports success, but `effect=None` | `INVALID` |

Feeding only known-bad is not enough — an "always FAIL" engine would pass that check. So the
last two samples are not decoration: they close off both degenerate behaviours, "always PASS"
and "always FAIL":

```
$ python3 -c "..."    # swap VerdictEngine for a stub that always returns PASS / FAIL
baseline                 : True
always-PASS engine       : False
always-FAIL engine       : False
```

`cli.py` calls it at startup; on failure it prints
`Self-audit failed, tool is a decoration.` and calls `exit(1)`, **refusing to start**.
`harness-audit-server` calls it too, before serving a single request, and exits `3` on
failure — until v0.10.0 only the demo CLI ran the gate, so the path an agent actually
connects to was never checked. That gap is recorded in [`SELF_AUDIT.md`](SELF_AUDIT.md)
along with the rest of the defects this gate failed to catch.

The samples use `tempfile` to really write files and really compute SHA-256, not hard-coded
fake data; if abnormal filesystem behaviour makes a sample itself invalid (hash unchanged / not
landed), self-audit also returns False.

`tests/test_self_audit_defects.py` is the regression suite for the self-audit itself, which
had none before this audit: a broken judgment base, an empty explicit base, a missing base,
and the server's refusal to start.

---

## Tests

```bash
python3 -m unittest discover -s tests -p 'test_*.py' -v
# Ran 707 tests ... OK (skipped=7)
```

> **Must be run from the repository root.** The start directory is the relative path `tests`;
> from another directory unittest silently collects only some modules (measured: running from
> `/tmp` collected 31 and reported 9 errors) instead of failing outright — a test run that
> "passes" while skipping 317 tests is more dangerous than a red one. So both `-s` and `-p` are
> given explicitly: `-p 'test_*.py'` keeps future helper modules under `tests/` from being
> collected as tests.

Coverage:

- **Verdicts**: the trigger for each of the four verdicts (with emphasis on `INVALID` never
  becoming `PASS`, and the three `FAIL` branches: false completion / false failure / intent not
  realised), verdict priority, `check` normalisation and refusal, the keyword-only constraint.
- **Immune tiering**: `ImmuneState` thresholds / consecutive counts / `NOT RUN` excluded /
  recovery reset / argument validation; the whole decision table of `select_tier` (no rule,
  declared low, field omitted, declared high, rule requires a witness, already inflamed, no
  observable path) plus type validation of the three arguments; observation and refusal of the
  `stat` witness (`expected_content` must not be silently dropped at the cheap tier); the four
  tier-routing combinations in a **real repository**; the same low rule escalating to a git change
  set once inflamed; recovery after five passes; one `INVALID` inflaming; an injected
  `ImmuneState` shared with the caller; responses not leaking the tier.
- **Inflammation persistence**: inflamed / half-recovered / healed restored across instances; a
  missing file is neither created nor written; `NOT RUN` does not rewrite the file; corrupt JSON,
  unknown version, unknown state and negative counts each record ERROR and fall back; a forged
  recovery count is clamped to one step short of the threshold (the wait cannot be skipped);
  `INFLAMED` with a danger count of 0 stays inflamed; atomic writes leave no temp file; an
  unwritable parent only logs; `reset()` persists; `--immune-state none` versus an explicit path.
- **Restart**: two real `python -m audit_sidecar.mcp_server` subprocesses, the second reading the
  first's state back from disk after the first exits. Verifies inflammation survives across
  processes, recovery survives across processes, `--immune-state none` honestly returns to
  amnesia, and an explicit path takes precedence. **This is the only empirical proof that "a
  restart cannot clear suspicion".**
- **The auditor does not create drift itself**: the state file lives in the audited repository
  and the git witness lists untracked files — two consecutive `write_file` calls must both be
  PASS with empty `unexpected_changes`, or the audit would treat its own notebook as evidence.
- **Account**: window eviction, `NOT RUN` taking a slot but not the denominator, label threshold
  boundaries (0.7 / 0.5).
- **File witness**: `false | true`'s exit code not swallowed, `PIPESTATUS` recorded per stage,
  the gap between naive and audit readings, content checks, a missing artefact judged as false
  completion, coordinate drift (witness pinned / witness following, as a control pair), timeouts
  and a nonexistent `cwd`, the temporary wrapper not landing in the working directory.
- **Git witness**: all running real `git` in **real temporary repositories**
  (`skipUnless(git and bash available)`), covering modify / untracked / new subdirectory /
  delete / staged, sorting and POSIX paths, `.gitignore` excluded, non-repository error, baseline
  excluding pre-existing dirt, a second edit to the same file, subdirectory coordinate mapping,
  `repo_path` selecting the repository, both strict and permissive contamination policies, and
  the conjunction of two witnesses.
- **MCP interception**: honest call judged PASS, swallowed error judged FAIL, `check` not
  re-derived from the exit status (regression guard), the reason string naming the tool layer's
  self-contradiction, the response staying passive, false failure, a silent tool (`true` yet
  reporting success), unknown tools refused and not executed, argument-injection protection
  (three payloads), paths containing spaces, contract validation.
- **End to end**: `subprocess` really starts a child, really writes stdin and really reads
  stdout. Verifies the response is valid JSON-RPC and **does not leak the verdict**, that
  `audit_trace.jsonl` records the correct verdict (including `FAIL`), multiple calls in one
  session, string id echo, error codes for invalid JSON / unknown method / wrong version /
  missing `reported_success` / non-bool claim, notifications unanswered but **still executed and
  recorded**, service continuing after a bad request, the trace defaulting to `--cwd`, appending
  across sessions, and exit code 2 for bad startup arguments.
- Everything uses `tempfile.TemporaryDirectory()`, leaving no litter (including temporary git
  repositories).

---

## Known boundaries (MVP scope)

- **Framing is newline-delimited UTF-8, which is what MCP specifies.** Messages are JSON-RPC 2.0
  objects separated by `\n`; the transport has no `Content-Length` header (that is LSP, not MCP).
  An earlier version of this section said MCP had a "Content-Length mode" — it does not, and
  tolerating the claim would only have hidden a broken client.
- **Handshake implemented since v0.10.1.** `initialize` (with version negotiation and capability
  advertisement), `notifications/initialized`, `ping`, `tools/list` and `tools/call` are served;
  `tools/*` before `initialize` answer `-32002`. The legacy `tool_call` method is kept, and is
  exempt from the handshake, so every session recorded before this version still replays.
- **No server-initiated requests.** The server never sends `sampling` / `roots` / `elicitation`
  requests, and emits no `list_changed` notifications; `initialize` therefore advertises only
  `tools: {listChanged: false}`. Asking for a capability this server does not use would be a
  promise it cannot keep.
- **Declared tools are the built-in ones plus any registered witness tool.** `tools/list` reports
  `write_file` / `append_file` / `run_command` / `validate_citation`, with the JSON Schema the
  builders actually enforce. A tool registered without a schema is still listed, with a
  permissive one, because hiding it would keep it callable while claiming it does not exist.
- **The tool registry is built in.** Tool implementations are generated by this repository
  (command splicing) rather than forwarded to an external MCP server.
- **`reported_success` is supplied by the client on `tool_call`, and absent on `tools/call`.**
  This is the largest honesty gap in this layer: it guards "tool layer vs physical world", but
  the "client vs tool layer" segment has no witness. Over MCP the claim can only come from
  `params._audit.reported_success` (out-of-band, never reaches the model) or from the meaning of
  a non-error tool result — the trace records which, as `claim_basis`, rather than letting the
  weaker one read like the stronger.
- **Single process, single thread, no concurrency.** One request per line, processed serially; a
  slow command blocks later calls. No batching (JSON-RPC batch is not implemented).
- **Witnesses are only filesystem and git.** No process exit-code family, no network side
  effects; the `git` witness depends on working-tree state and cannot see changes already
  committed (the baseline is a working-tree snapshot).
- **Tier 1 has a clear blind spot.** Innate immunity looks only at existence and mtime, and
  **cannot see "changed one extra file along the way"**; a rewrite of equal length within one
  filesystem timestamp tick is entirely invisible to it. This is a trade-off rather than a defect,
  which is why overwrites are declared in `write_file`'s `risk_level: "high"` and the blind spot
  is written as a test (`test_adaptive_tier_sees_drift_the_innate_tier_cannot`).
- **Inflammatory state is persisted across processes (since v0.7.0).** By default
  `harness-audit-server` writes the count to `.immune_state.json` next to `--trace`, atomically
  after each verdict, and reads it back at startup. **Killing the process and restarting no
  longer clears suspicion** — which is exactly why it exists: the party able to arrange a crash
  is the audited agent itself. Use `--immune-state none` to request amnesia explicitly (the only
  amnesia entry point), or `--immune-state PATH` to place the file outside the audited directory.

  **Residual risk (not hidden):** the state file is plain JSON, unsigned. An agent with **write
  access to that path** can still delete or alter it; on corruption or absence the system falls
  back to `NORMAL` and records ERROR on stderr (an amnesia event in the log). Tamper resistance
  requires placing the file where the agent cannot write, or adding an HMAC — not done in the
  MVP. **This is much stronger than "forgotten on restart", but it is not unforgeable.**
- **Bad actions are not prevented.** No hooks, no proxy enforcement; this is a sidecar verdict
  tool, which does not stop a bad action, only declines to endorse it.
- **`pipe_status` degrades for a top-level `exit`.** A top-level `exit` in the command terminates
  the wrapper early, so per-stage status is unavailable and it falls back to `(exit_code,)`;
  the verdict is unaffected.
- **State after a timeout is not trusted.** The process was killed and files may be intermediate,
  so the verdict is `INVALID` rather than `FAIL`. Conservatism in the safe direction.
- **The git witness only sees the working tree.** If the command runs `git commit` itself, the
  change disappears from the uncommitted set and strict mode reports "the expected file was not
  changed". That is a deliberate conservative choice, not an omission.
- **The trace has no rotation and no locking.** Append-only, never split; concurrent writers rely
  on the atomicity of a single `write` (usually safe below PIPE_BUF), with no explicit locking.
- **A trace that cannot be written does not block a verdict.** If the file is read-only or the
  disk is full, `TraceWriter.write` logs on stderr and returns; the agent still gets its answer.
  The verdict is enforced but unrecorded, which is a quieter failure than an unenforced one.
  Recorded as `LIM-1` in [`SELF_AUDIT.md`](SELF_AUDIT.md), with the recommended fix
  (an `audit_recorded` field in the response, or a fail-closed switch) left to the operator.
- **A witness that cannot be read is `INVALID`, not `FAIL`.** Since v0.10.0 an unreadable target
  file (EACCES, EIO, descriptor exhaustion) produces `INVALID` with `effect_observed=None`.
  Before the fix it produced `FAIL` with `reason: "witness says no effect"` — a sentence the
  witness never said. An unreadable path in the git change set used to be worse: the fingerprint
  string differed from the baseline digest, so it *looked like a change*, and
  `chmod 000 target.py` produced `PASS` with no work done.
- **A non-regular file in the dirty set makes change-set verdicts `INVALID` — deliberately.**
  Since the `ln -s /dev/zero` hang was closed, `snapshot()` takes `os.lstat` and hashes only
  regular files; a symlink, device or FIFO is recorded as `__unreadable__` and never opened.
  This is fail-closed, not a false positive: one dirty symlink anywhere in the repository is
  enough to turn every `expected_files_modified` judgement into `INVALID`, exactly as a
  `chmod 000` file does. When an audit answers `INVALID` for no visible reason, run
  `git status` and look for a dirty symlink. Recorded as `LIM-10`.
- **Auto-detected `./rules` is resolved against the process's cwd, not `--cwd`.** The local layer
  is looked up with `os.getcwd()`, so `harness-audit-server --cwd /repo` launched from elsewhere
  merges `/repo`'s policy only if the process was started there. Explicit `--rules` and
  `$HARNESS_AUDIT_RULES` are unaffected. Known gap, recorded as `LIM-11`.
- **A process killed by the OOM killer is judged `FAIL`, not `INVALID`.** The intent was not
  realised and the receipt says so, but the receipt carries no cause: the model and the machine
  produce the same word. Under R5 that is a false failure manufactured by the environment;
  attributing it needs the signal number, which is not classified yet.
- **There is no witness for abstention.** The lattice can verify that something happened; it
  cannot verify that something *did not* happen. A tool call whose intent is "leave this file
  alone" has nothing to point at, so a correctly reported non-action can only ever be `INVALID`.
  `allow_extra_changes=False` covers the narrow case of "changed something else", not "was
  supposed to do nothing".
- **A verdict is about outcomes, not effort.** If the world already matches the intent, a command
  that does nothing is `PASS`. That is correct for an outcome witness and wrong for an
  "prove you did the work" audit, and the two must not be confused.
- **`intent` echoes the client's path verbatim.** `changed_files` / `missing_expected` /
  `unexpected_changes` are repository-root relative (the `git status` coordinate system), but
  `intent` records the call argument as given. If the client passes absolute paths, local paths
  enter the trace. Before sharing an audit log, check yourself:
  `grep -cE '/tmp/|/home/' audit_trace.jsonl`.
- **The rule layer is opt-in.** `MCPProxy` without `rules_dir` is the pure four-valued grid; only
  the server auto-detects and merges `./rules`. Providing no rules does not silently change the
  verdicts of existing calls.
- **`rules/` ships with the package (since v0.6.1).** Default rules live in `audit_sidecar/rules/`
  and are packed into the wheel as package data, so **no configuration is needed** after
  `pip install`. Resolution order is `$HARNESS_AUDIT_RULES` (used alone) → `./rules` **merged over**
  bundled `rules/`, which is the floor: a local rule may add a check or tighten a shipped rule,
  but a weaker or identical redefinition is a load error, so a local directory cannot delete a
  shipped guard. `$HARNESS_AUDIT_RULES` pointing at a
  nonexistent directory **raises an error** rather than quietly falling back, since otherwise the
  operator would believe their rules were running.
- **`$HARNESS_AUDIT_RULES` is an escape hatch, and it is exclusive.** Setting it means "this
  directory alone is my policy": the shipped floor is *not* merged and its rule ids are *not*
  required to be present, because a fully custom base is a legitimate thing to run. The bypass is
  never silent — loading such a base logs a WARNING naming every shipped rule that is out of
  force — and the self-audit still exercises every rule the named base contains. Recorded as
  `LIM-12`: closing it would mean refusing any override that lacks a shipped id, which would make
  custom policies unusable.
- **Rules cover only `write_file` and `append_file`.** Two ship with the package: `no_file_drift`
  (`risk_level: high`) and `append_landed` (`risk_level: low`); `run_command` has no rule, goes
  straight to the grid, and defaults to Tier 2. Rule expressiveness is limited too: only three
  `witness_type`s, one `expectation: file_modified` and two `risk_level`s; anything else fails to
  load — refusal over silent ignoring.
- **Only the file witness exists in a non-git directory.** Tools needing a change set
  (`write_file`) are judged `INVALID` there: not "passed", but "not seen". Either point `--repo`
  at a repository, or accept the weaker policy explicitly with `--rules none`.
- **Rules perform no cross-tool reasoning.** Each rule judges independently; there are no "A then
  B" ordering constraints and no dependencies between rules.
- **No persistence.** The account and the audit log live in memory and vanish with the process
  (except the trace file).

## Roadmap

1. **Witness collection**:
   - Filesystem: available (existence + content substring).
   - Git change set: available (`git_witness.py`).
   - To extend: content hashes, process exit-code family, network side effects.
   - mtime: already used by Tier 1 (existence + mtime, see
     [Conditional Immune Response](#conditional-immune-response)); precision handling and content
     hashing are still missing.
2. **MCP interception** (handshake and transport available):
   - Available: `MCPProxy`'s call boundary, passive response, tool registry, injection
     protection; `mcp_server.py`'s stdio JSON-RPC server and JSONL audit trail; and since
     v0.10.1 the `initialize` / `notifications/initialized` / `ping` / `tools/list` /
     `tools/call` surface, with version negotiation and per-tool JSON Schemas.
   - To extend: take the claim from the protocol frame's `result.isError` rather than from the
     client, forwarding to an external MCP server, server-initiated requests, concurrency and
     JSON-RPC batching.
3. **External judgment base** (basic form available):
   - Available: `rules/*.yaml` loading and strict validation, per-tool rule selection, monotonic
     tightening, `rule_id` in the trace, rule-layer self-audit.
   - Available since v0.9.0: distribution as digest-verified packages — namespaced ids with
     versions and authors, a declared witness requirement per rule, a `penalty_weight`, install
     gated on the SHA-256 of the exact bytes, and mounting through the membrane's public API
     only (`tools/rule_interface.py`).
   - To extend: more `expectation`s (`file_created` / `content_matches` /
     `no_unexpected_changes`), rule inheritance and grouping, path scoping (`target_path` glob),
     effective dates, and **an author signature** over the package digest. The digest currently
     seals the download, not the author: whoever can reach both channels can replace both.
4. Account persistence and cross-session aggregation; exporting the verdict stream as a
   reviewable audit log.

## License

> Code and reproduction scripts are licensed under the MIT License. The paper text
> (Markdown and PDF) is licensed under CC BY 4.0.

Copyright (c) 2026 Zhizhi Liao. See [`LICENSE`](LICENSE) for the code and
[`LICENSE-CC-BY`](LICENSE-CC-BY) for the prose.

The MIT License is the instrument that grants permission to the code: use, copy,
modify, merge, publish, distribute, sublicense, and sell, subject only to keeping the
copyright and permission notice. It also carries the warranty and liability disclaimer
that makes the grant workable — the Software is provided "AS IS", and the author is not
liable for damages arising from it.

The split follows the two kinds of artefact. The code is meant to be integrated and
re-run, so it is MIT: a sidecar that cannot be embedded in someone else's harness is not
a sidecar. The prose is meant to be cited and quoted, so it is CC BY 4.0, which requires
attribution and permits reuse including commercial reuse — which is the point of
publishing an audit method at all.

Neither licence grants a certification, and neither is an audit. Running this sidecar
produces a record of what it witnessed; it does not produce an accreditation, and a
report it emits should not be presented as one. The audit service and the open-source
tool are separate things that happen to share a judgment base.
