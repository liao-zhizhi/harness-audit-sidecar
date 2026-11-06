# Launch copy — Hacker News / social

Internal working file. Not part of the package, not shipped in the wheel.

**Rule for everything below: nothing here may claim a measurement we did not take.** The
kinetic constants behind the R5/R6 numbers are placeholders; the drift detector is real and
tested, the biology that motivated it is a simulation. Every draft keeps that distinction
visible, because the first informed comment will test it.

---

## 1. Titles (three options)

**A. The claim-first title** — leads with the defect, no adjectives.

> **False Completion: your agent's "done" is a claim, not an observation**

*Keywords: False Completion. Reads like a paper, which is what makes it credible on HN. Weak
on curiosity — it states a known problem, so the hook has to come from the first comment.*

**B. The number-first title** — leads with R6, the finding nobody expects.

> **A loop with R² = 0.9997 that was dying: False Completion, Causal Grounding, and what
> metabolism taught us about both**

*Keywords: all three. Long, but HN tolerates long titles when the number is the hook. This is
the one that makes people click out of disbelief, and the number is defensible — it is
reproducible from nine values and is asserted in a test.*

**C. The reframe title** — leads with the architectural claim.

> **Causal Grounding for agents: four verdicts instead of "done", borrowed from cell
> metabolism**

*Keywords: Causal Grounding, Metabolism. Softer, better for a technical audience that already
knows "agents lie"; positions the post as design, not complaint.*

**Recommendation:** B for HN, C for X/LinkedIn. B's number is the only thing in any of these
that a reader cannot guess, and it survives scrutiny. A is the safest and the least
interesting.

---

## 2. First comment (author's note)

> Author here. The short version of what we built and, more usefully, what made us build it.
>
> Every audit tool eventually hits the same wall. You ask a second model to check the first
> one's work. Both come from the same family, so they share a likelihood function — and a
> **fluent** hallucination is a high-likelihood sample for both. The judge isn't lying. Its
> sensitivity on the error class is just ~0, and precision follows it down:
> `P = s·p / (s·p + (1-sp)(1-p))`. The false-positive term in the denominator doesn't shrink
> with `s`, so there is no threshold that works. Tighter: everything fails. Looser: everything
> passes. Re-reading the same output with the same model is not a second opinion, it's the same
> sample drawn twice.
>
> The only thing that broke the wall for us was changing the **type** of evidence, not the
> quality of the judge. A digest, an exit code, a git object — a model can lie about those, but
> it cannot *produce* them without changing the world first.
>
> That forces four verdicts, not two: `PASS / FAIL / INVALID / NOT RUN`, worst-wins. The two
> that do the work are the middle ones. `INVALID ≠ PASS` (missing evidence is not success) and
> `NOT RUN ≠ FAIL` (never executed is not executed-and-wrong). A boolean has to file "I didn't
> look" under one of the two, and whichever you pick, the information is destroyed at record
> time and cannot be recovered later.
>
> The sidecar is small: an MCP proxy that asks for witnesses instead of conclusions, compares
> `artefact + digest`, and writes a hash-chained ledger. Refusal is a first-class outcome — a
> saturated judge that *must* answer will answer `INVALID` for everything, which is a failure
> the observer manufactured and then billed to the subject.
>
> The part that will interest this crowd: the verdict table is not a UX convention. It's the
> quadrant diagram of two state variables — available material, and internal charge — from a
> formalisation of cell metabolism we did separately. `FAIL` is (no material, no charge).
> `INVALID` is (material present, cannot take it up). Those are opposite corners, and the
> interesting one is the fourth: (no material, high charge) is **dynamically unreachable**,
> because with zero uptake the charge can only fall. That corner is the definition of a
> hallucination, so in this framing anti-hallucination isn't a check you add — it's an empty
> set in the state space.
>
> Same derivation gave us a failure a point-in-time verdict cannot see: a loop that looks
> periodic but is a decaying transient. In our reference run the substrate at each wake fell
> 19.265 → 17.041 mM over nine cycles. Nine near-identical peaks. Fitted: slope −0.2788/cycle,
> **R² = 0.9997**, extrapolated zero around cycle 70. Per-cycle loss ≈1.3% — below any
> per-round threshold, which is exactly why it's dangerous. We ship `analyze_chronic_drift`,
> which requires **both** high R² and material magnitude, because R² measures regularity, not
> size: gate on R² alone and a 1e-7 wobble becomes an alarm.
>
> The ugly part is what this says about our whole monitoring culture. We have spent forty years
> using variance and standard deviation as the definition of stability — control charts, p99
> SLOs, confidence intervals, loss-curve smoothness, seed variance. That toolkit quietly assumes
> **an anomaly announces itself with jitter.** If the collapse is perfectly smooth, variance has
> nothing to report: the residual standard deviation here is 0.0133 mM, 0.073% of the mean, and
> a three-cycle rolling std never exceeds 1.3%. Every alarm stays silent, because the anomaly's
> *shape* falls into the null space of the statistic. The ramp moved its variance into the mean,
> and nobody puts a threshold on the mean.
>
> That is why AI systems can look extraordinarily stable for months and then fail in a single
> day. A system losing 1.3% per cycle is the *most reproducible* system you have — tiny
> seed-to-seed variance — and also the first to hit zero. Low variance measures reproducibility,
> not survivability. So the sidecar doesn't do variance analysis. It does **causal labelling**:
> every verdict is tied to a witness that a model cannot manufacture without changing the world
> first, and the chronic check is a cumulative quantity — how much margin has already been spent,
> and how many cycles are left — not how much this round wobbled.
>
> Honest limits, since someone will ask: the kinetic constants are placeholders, so the drift
> example establishes the mechanism, not a biological measurement. We have not measured a
> benchmark improvement from any of this. The deadlock and drift claims are falsifiable and we
> say how in the repo.
>
> Code: `agent-harness-audit` (v0.10.0). Happy to be told which of these is wrong.

---

## 3. Short variants

**X / Mastodon (≤ 280):**

> Your agent says done. That's a claim, not an observation.
>
> A judge from the same model family can't tell a fluent hallucination from a fact — same
> likelihood function. So we stopped asking models and started demanding digests.
>
> Four verdicts: PASS / FAIL / INVALID / NOT RUN. INVALID ≠ PASS.

**LinkedIn (3 lines):**

> A boolean verdict has no word for "I didn't look" — so it files that under "done".
> We rebuilt the audit around four verdicts and physical witnesses, and the verdict table
> turned out to be the quadrant diagram of a cell's energy charge.
> v0.10.0 adds chronic drift detection: a loop with R² = 0.9997 was losing 11.5% over nine
> cycles.

### 中文版首评（公众号 / 知乎 / 即刻）

> 我们习惯用方差和标准差来评估系统的稳定性。控制图看 z 分数，SLO 看 p99，评估看多 seed
> 的方差，训练看 loss 的滑动标准差——这套工具背后有一条从未写下来的公理：
> **异常必然伴随着剧烈的抖动。**
>
> 但如果系统的衰落是完美平滑的呢？R² = 0.9997，去趋势残差标准差只有均值的 0.073%，
> 3 周期滑窗的相对标准差不超过 1.3%。方差彻底失效。这不是"没有异常"，是异常的形状
> 刚好落进了这个统计量的零空间——**斜坡把方差搬进了均值，而均值没有人设阈值。**
>
> 这就是为什么很多 AI 系统看起来运行得极其稳定，却会在某一天突然彻底崩溃。
> 一个每轮掉 1.3% 的系统，是你能拥有的**最可复现**的系统——seed 间方差极小——
> 也是第一个归零的系统。方差小度量的是"复现得出来"，不是"撑得下去"。
>
> 我们的 Sidecar 不做方差分析，我们只做**因果标号**：每一条判词都绑定一个模型无法
> 凭空制造、必须先改变世界才能取得的见证；慢性漂移检测测的是**存量**——已经花掉了多少
> 余量、还剩多少个周期——而不是这一轮抖了多少。
>
> 四值判词（PASS / FAIL / INVALID / NOT RUN）与这套架构，来自对代谢动力学的严格推演：
> 失败在 (底物, 能荷) 平面上是二维的，`FAIL` 与 `INVALID` 是对角两格，第四格在动力学上
> 不可达——那一格就是幻觉的定义。

---

## 4. What not to say

| Don't | Why |
|:--|:--|
| "We discovered a new class of AI failure" | False completions are documented by others; what's new here is the four-valued split and the drift criterion |
| "Improves benchmark scores by X%" | We never ran that experiment |
| "Based on real biology" | Placeholder constants; the *form* transfers, the numbers don't |
| "Catches all false completions" | It catches the ones with witnesses. Anything without a witness is `INVALID`, by design |
| "Production-ready" | Status line says MVP, and it should stay that way until concurrency is done |

---

## 5. Likely first objections, and the honest answers

**"This is just property-based testing / TLA+ / contracts."** Partly. The contribution isn't
witnessing, it's the verdict algebra: separating "evidence contradicts" from "no evidence" and
proving that a saturated observer produces the second while reporting the first. Property tests
don't have a word for the auditor's own failure.

**"A hash chain can't stop deletion of the last line."** Correct, and the README says so. You
need a head anchor the attacker can't write. We don't claim to cross that boundary.

**"R² = 0.9997 on nine points."** Yes — nine points, and the fit is asserted in
`tests/test_drift.py` against the exact series, not a synthetic one. Nine points are enough to
reject a limit cycle; they are not enough to predict the exact death date, which is why the
detector reports `steps_to_zero` as an extrapolation, not a forecast.

**"Variance isn't blind — CUSUM and EWMA catch ramps."** True, and worth conceding before
anyone else says it: cumulative statistics do catch a slope. But that is the argument, not a
rebuttal — the thing that works is the *cumulative* one, which is exactly why we ship Φ and τ
instead of a variance threshold. And CUSUM still assumes an anchored reference mean. If the
baseline is re-estimated from a recent window (adaptive thresholds, rolling quantiles, a
drifting baseline), the ramp is absorbed as the new normal and the sum resets. That is how the
R6 case hides.

**"Causal labelling — isn't that just a signature?"** No. A signature proves *who* produced a
record. A witness proves *whether the world changed*, and it is collected outside the subject's
output space: an exit code, a digest recomputed by the gate, a git object. The subject cannot
manufacture one without doing the thing first.

**"Where's the biology?"** In a private repo, and it stays private. What ships is the part that
survives contact with a witness.
