"""The one-line interface: a declaration in, a verdict out.

``harness-audit check`` exists because every other entry point asks the caller
to supply fields they have no business knowing: ``witness_type``, ``producer``,
``topology``, ``tier``. Those are the auditor's vocabulary, and a caller who
has to fill them in is being asked to describe *how* they will be checked,
which is the one thing a checked party should not control.

So this module takes three declarations and infers the rest:

``--file <path>``      "this file changed"
``--command "<cmd>"``  "this command succeeded"
``--diff``             "the working tree changed"

Each one picks its own witness and calls the same lattice
(``VerdictEngine.explain``) the rest of the package uses. Nothing here invents
a verdict value, a precedence, or a cap: it is a façade over the existing
judge, with the internals moved from the input side to the ``--verbose`` side.

Two rules are load-bearing:

* **No baseline, no "changed".** "Changed" is a comparison and a comparison
  needs a before. Without a recording the honest answers are "it exists" /
  "it does not" or "the change set is empty" / "it is not", and the rest is
  ``INVALID`` with a ``next_step`` -- never a guess dressed as a verdict.
* **The witness is collected here, not declared.** ``producer`` is
  ``independent`` because this process really did read the bytes, the exit
  status, or the change set. That is a fact about where the evidence came
  from, not a claim the caller made about it.

On the ``topology`` vocabulary: the provenance enum has no ``local`` (it has
``sidecar`` / ``direct_api`` / ``other``), and changing it would change the
cap's core. So ``PROVENANCE_TOPOLOGY = "sidecar"`` is a **semantic mapping for
the cap call**, not a claim that this check ran through a sidecar process: the
receipt says ``local``, the cap is told ``sidecar`` because both mean "an
observer other than the agent was in the path", and the combination
(``independent`` + ``sidecar``) is precisely the one the cap does not tighten.
Anyone reading the ``cap_verdict`` input must not infer a real hop.

What ``--command`` does and does not claim: it runs the command here and reads
*this process's* exit status. The claim is therefore the operator's ("I ran it
and it succeeded"), not the agent's ("my tool said it succeeded"). The case
where an agent's tool layer swallows an exit code is a *different* observation
-- ``SafeRunner(reported_success=...)`` records it as a ``protocol`` witness --
and ``check`` deliberately does not expose it, because a one-line interface
that silently judged a party other than the one the caller named would be the
wrong audit.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Optional, Tuple

from . import baseline as baseline_mod
from .git_witness import GitWitness, GitWitnessError
from .runner import ExecutionResult, SafeRunner, WITNESS_SCOPE_FULL
from .verdict import Verdict, VerdictEngine
from .witness_provenance import cap_verdict

__all__ = [
    "CheckOutcome",
    "LOCAL_TOPOLOGY",
    "PROVENANCE_TOPOLOGY",
    "check_command",
    "check_diff",
    "check_file",
    "NEXT_STEP_BASELINE_ADD",
    "NEXT_STEP_BASELINE_FILE",
    "NEXT_STEP_BASELINE_DIFF",
]

#: What the receipt says: this check ran on the caller's machine.
LOCAL_TOPOLOGY = "local"

#: What ``cap_verdict`` understands: an observer other than the agent was in
#: the path. This is a **semantic mapping for the cap**, not an assertion that
#: a sidecar process was involved -- see the module docstring. It exists as a
#: named constant so the mapping is one visible decision rather than a literal
#: buried in the call site.
PROVENANCE_TOPOLOGY = "sidecar"

#: The witness was produced by this process, not declared by the caller.
PRODUCER = "independent"

#: ``{path}`` is filled with the path the caller named, not left as ``<path>``:
#: a next step the operator can paste should not need editing first. The git
#: clause is there because a dirty file in ``git status`` invites exactly the
#: wrong question -- "git says it changed, why INVALID?" -- and the answer is
#: that "changed vs HEAD" is a different declaration from "changed since your
#: recording".
NEXT_STEP_BASELINE_FILE = (
    "run `harness-audit baseline --file {path}` first, then re-run this check "
    "(git HEAD is a different claim: 'changed vs HEAD' is not 'changed since "
    "your recording')"
)
NEXT_STEP_BASELINE_ADD = (
    "the recording does not name `{path}`: run "
    "`harness-audit baseline --file {path}` to add it, then re-run this check"
)
NEXT_STEP_BASELINE_DIFF = (
    "run `harness-audit baseline` first, then re-run this check"
)


def _is_recording_artefact(path: str) -> bool:
    """Whether ``path`` is the ``--local`` baseline file itself.

    ``--local`` writes ``.harness-baseline.json`` into the tree it records, so
    the file is untracked and dirty by construction. Counting it would make
    ``--diff`` report "1 path changed" immediately after a recording, i.e. a
    guaranteed PASS that says nothing about the code. The recording is the
    instrument, not the effect, so it is excluded from the change set.
    """
    normalised = str(path).replace("\\", "/")
    while normalised.startswith("./"):
        # ``lstrip("./")`` would eat the dot of ``.harness-baseline.json``
        # itself, because it takes a character set rather than a prefix.
        normalised = normalised[2:]
    normalised = normalised.lstrip("/")
    return normalised == baseline_mod.BASELINE_FILENAME or normalised.endswith(
        "/" + baseline_mod.BASELINE_FILENAME
    )

#: How much of stdout to keep in the receipt. Enough to diagnose a failure,
#: short enough that a chatty test suite does not become the trace.
_STDOUT_HEAD = 2000


@dataclass(frozen=True)
class CheckOutcome:
    """One declaration, judged. The ``verbose`` fields are the trace's own.

    The split matters: ``verdict`` / ``reason`` / ``next_step`` are the whole
    public answer, and everything below ``declaration`` is what ``--verbose``
    reveals. Nothing is deleted from the internal record -- it is wrapped.
    """

    verdict: Verdict
    reason: str
    next_step: Optional[str] = None
    declaration: str = ""
    witness_type: str = ""
    witness_scope: str = WITNESS_SCOPE_FULL
    producer: str = PRODUCER
    topology: str = LOCAL_TOPOLOGY
    tier: str = "tier2"
    dropped_assertions: Tuple[str, ...] = ()
    cap_fired: bool = False
    renegotiation_class: Optional[str] = None
    evidence: Mapping[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.verdict is Verdict.PASS

    def to_dict(self) -> Dict[str, Any]:
        """The full record, for ``--json`` and for the tests to assert on."""
        return {
            "verdict": self.verdict.value,
            "reason": self.reason,
            "next_step": self.next_step,
            "declaration": self.declaration,
            "witness_type": self.witness_type,
            "witness_scope": self.witness_scope,
            "producer": self.producer,
            "topology": self.topology,
            # The value the cap was actually given. Recorded next to the
            # receipt's `local` so the semantic mapping is visible in the trace
            # and does not have to be reconstructed from the source.
            "cap_topology": PROVENANCE_TOPOLOGY,
            "tier": self.tier,
            "dropped_assertions": list(self.dropped_assertions),
            "cap_fired": self.cap_fired,
            "renegotiation_class": self.renegotiation_class,
            "evidence": dict(self.evidence),
        }


def _judge(intent: Any, effect: Any, check: Any) -> Tuple[Verdict, str]:
    """The existing lattice, and nothing else."""
    return VerdictEngine().explain(intent=intent, effect=effect, check=check)


def _cap(verdict: Verdict, reason: str) -> Tuple[Verdict, str, bool]:
    """Apply the provenance cap and report whether it moved anything.

    ``independent`` + ``sidecar`` fires nothing by design; computing it rather
    than hard-coding ``False`` means the receipt would change if that ever
    stopped being true.
    """
    capped, capped_reason = cap_verdict(
        verdict,
        reason,
        producer=PRODUCER,
        topology=PROVENANCE_TOPOLOGY,
    )
    return capped, capped_reason, capped is not verdict


def _renegotiation_class(verdict: Verdict) -> Optional[str]:
    """The same coarse mapping ``pre_pr_audit.classify`` uses.

    Duplicated rather than imported because that module lives in the product
    repo and this package must not depend on it. ``PASS`` has no downstream
    renegotiation, so it is ``None`` rather than a string people would filter
    on.
    """
    if verdict is Verdict.PASS:
        return None
    if verdict is Verdict.FAIL:
        return "ci_debug"
    return "manual_review"


def _outcome(
    *,
    verdict: Verdict,
    reason: str,
    next_step: Optional[str],
    declaration: str,
    witness_type: str,
    evidence: Mapping[str, Any],
    execution: Optional[ExecutionResult] = None,
) -> CheckOutcome:
    capped, capped_reason, cap_fired = _cap(verdict, reason)
    return CheckOutcome(
        verdict=capped,
        reason=capped_reason,
        next_step=next_step,
        declaration=declaration,
        witness_type=witness_type,
        witness_scope=WITNESS_SCOPE_FULL,
        tier=execution.tier if execution is not None else "tier2",
        dropped_assertions=execution.dropped_assertions if execution is not None else (),
        cap_fired=cap_fired,
        renegotiation_class=_renegotiation_class(capped),
        evidence=dict(evidence),
    )


def check_file(
    path: str,
    *,
    cwd: str,
    timeout: float = 30.0,
    recorded: Optional[baseline_mod.RepoBaseline] = None,
) -> CheckOutcome:
    """Judge "this file changed" against a recorded digest.

    Order of the three answers, and why:

    1. the file exists and there is a recorded digest -> content comparison is
       the strongest witness available, so it decides;
    2. the file is gone but the recording had it -> a deletion *is* a change;
    3. there is no recording for this path -> ``INVALID``. "It exists" is not
       "it changed", and saying so would be the decoration this package is
       built to refuse.

A dirty path in ``git status`` is deliberately **not** used as an implicit
baseline. "Changed relative to HEAD" is a different declaration than "changed
since your recording", and answering the second with the first would borrow a
witness the caller never asked for. That is why the third case is ``INVALID``
with a next step that says so, rather than a convenient ``PASS``. The receipt
still records ``git_dirty`` in its evidence -- visible under ``--verbose``, and
never allowed to decide the verdict.
    """
    if not isinstance(path, str) or not path.strip():
        raise ValueError("path must be a non-empty string")
    cwd_abs = os.path.abspath(cwd)
    absolute = path if os.path.isabs(path) else os.path.join(cwd_abs, path)
    absolute = os.path.normpath(absolute)
    declaration = "file {}".format(path)

    repo_root = baseline_mod.resolve_repo(cwd_abs)
    key = baseline_mod._key_for(absolute, cwd_abs, repo_root)
    baseline = (
        recorded
        if recorded is not None
        else baseline_mod.load(cwd_abs, repo_root=repo_root)
    )

    exists = os.path.isfile(absolute)
    digest = baseline_mod.sha256_file(absolute) if exists else None
    git_dirty: Optional[bool] = None
    if repo_root is not None:
        try:
            git_dirty = key in GitWitness(repo_root, timeout=timeout).get_changed_files()
        except (GitWitnessError, OSError):
            git_dirty = None

    evidence: Dict[str, Any] = {
        "path": key,
        "exists": exists,
        "sha256": digest,
        "recording": baseline.source if baseline else None,
    }
    if git_dirty is not None:
        evidence["git_dirty"] = git_dirty

    recorded_digest = (
        baseline.file_digest(key) if baseline is not None else None
    )
    if baseline is not None:
        evidence["recorded_sha256"] = recorded_digest
        evidence["recorded_at"] = baseline.created_at

    if baseline is not None and key in baseline.files and recorded_digest is None:
        # Recorded, but unreadable at recording time. There is a slot and no
        # fact in it; saying "changed" or "unchanged" would both be invented.
        return _outcome(
            verdict=Verdict.INVALID,
            reason=(
                "the recording for {} could not be read, so there is no digest "
                "to compare".format(key)
            ),
            next_step="check the file permissions, then re-run `harness-audit baseline --file {}`".format(
                path
            ),
            declaration=declaration,
            witness_type="file",
            evidence=evidence,
        )

    if baseline is None or recorded_digest is None:
        if not exists:
            return _outcome(
                verdict=Verdict.FAIL,
                reason="{} does not exist, so it was not created".format(path),
                next_step=(
                    "check the path; if another step should have created it, "
                    "that step did not run"
                ),
                declaration=declaration,
                witness_type="file",
                evidence=evidence,
            )
        # Exists, but nothing to compare it with. The next step names this
        # path, not a generic `<path>`: a command the operator has to edit
        # before running is a worse answer than the INVALID it came with.
        named = key or path
        if baseline is None:
            reason = (
                "{} exists, but no recording was found, so 'changed' cannot be "
                "judged".format(path)
            )
            next_step = NEXT_STEP_BASELINE_FILE.format(path=named)
            evidence["baseline_searched"] = baseline_mod.candidate_paths(
                cwd_abs, repo_root=repo_root
            )
        else:
            # A recording exists; it simply has no entry for this path. Saying
            # "no recording" would send the operator to re-take a full
            # recording they already have.
            reason = (
                "{} exists, but the recording at {} does not name it, so "
                "'changed' cannot be judged".format(path, baseline.source)
            )
            next_step = NEXT_STEP_BASELINE_ADD.format(path=named)
        return _outcome(
            verdict=Verdict.INVALID,
            reason=reason,
            next_step=next_step,
            declaration=declaration,
            witness_type="file",
            evidence=evidence,
        )

    if not exists:
        # The recording had a digest and the file is gone: deletion is a change.
        # Checked before the unreadable branch, because an absent file is a
        # fact, not a read failure -- reporting it as "could not be read" would
        # turn the strongest answer available into an excuse.
        return _outcome(
            verdict=Verdict.PASS,
            reason="{} was removed since the recording, which is a change".format(path),
            next_step=None,
            declaration=declaration,
            witness_type="file",
            evidence=evidence,
        )

    if digest is None:
        return _outcome(
            verdict=Verdict.INVALID,
            reason="{} exists but could not be read, so it was not witnessed".format(path),
            next_step="check the file permissions, then re-run this check",
            declaration=declaration,
            witness_type="file",
            evidence=evidence,
        )

    if digest != recorded_digest:
        verdict, _reason = _judge(intent=True, effect=True, check=True)
        return _outcome(
            verdict=verdict,
            reason="{} changed since the recording (sha256 differs)".format(key),
            next_step=None,
            declaration=declaration,
            witness_type="file",
            evidence=evidence,
        )

    verdict, _reason = _judge(intent=True, effect=False, check=True)
    # The generic lattice reason says "false completion", which is accurate but
    # reads as an accusation about a report; here the claim was the operator's,
    # so the receipt names the comparison instead.
    return _outcome(
        verdict=verdict,
        reason=(
            "{} is byte-identical to the recording, so the claimed change did "
            "not happen".format(key)
        ),
        next_step="run the step that was supposed to change it, then re-run this check",
        declaration=declaration,
        witness_type="file",
        evidence=evidence,
    )


def check_command(
    command: str, *, cwd: str, timeout: float = 30.0
) -> CheckOutcome:
    """Judge "this command succeeded" against its own exit status.

    The claim is about the *program*, and an exit status is the right witness
    for a program fact -- so this form can PASS, unlike a claim about the world
    resting on the same signal. That distinction is the provenance cap's whole
    subject, and it is why ``program_fact`` is deliberately not set here: the
    intent really is "the program exits 0".

    Scope, stated because it is easy to over-read: this observes *this
    process's* exit status for a command the operator named. It does **not**
    observe an agent's tool layer, so it cannot catch a tool that swallowed an
    exit code and reported success -- ``SafeRunner(reported_success=...)`` is
    the hook for that (a ``protocol`` witness), and exposing it here would
    require a field the one-line interface does not have.
    """
    if not isinstance(command, str) or not command.strip():
        raise ValueError("command must be a non-empty string")
    declaration = "command {}".format(command)

    runner = SafeRunner(timeout=timeout)
    execution = runner.run_shell(
        command,
        cwd=cwd,
        intent_desc="check: the command exits successfully",
        # No artefact is claimed: the declaration is about the exit status, so
        # demanding a file would be a stronger claim than the caller made.
        reported_success=None,
    )

    stdout = execution.stdout or ""
    stderr = execution.stderr or ""
    evidence: Dict[str, Any] = {
        "command": command,
        "exit_code": execution.exit_code,
        "pipe_status": list(execution.pipe_status),
        "check_source": execution.check_source,
        "stdout_head": stdout[:_STDOUT_HEAD],
        "_stdout_truncated": len(stdout) > _STDOUT_HEAD,
        "stderr_head": stderr[:_STDOUT_HEAD],
        "_stderr_truncated": len(stderr) > _STDOUT_HEAD,
    }

    if execution.error is not None and "timed out" in execution.error:
        return _outcome(
            verdict=Verdict.INVALID,
            reason="the command did not finish within {:.0f}s, so nothing was witnessed".format(
                timeout
            ),
            next_step="raise --timeout, or run a smaller command, then re-run this check",
            declaration=declaration,
            witness_type="exit_status",
            evidence=evidence,
            execution=execution,
        )
    if execution.exit_code is None:
        return _outcome(
            verdict=Verdict.INVALID,
            reason="the command could not be started, so its exit status is not a witness",
            next_step="check that the command exists and is executable, then re-run this check",
            declaration=declaration,
            witness_type="exit_status",
            evidence=evidence,
            execution=execution,
        )

    succeeded = execution.exit_code == 0
    verdict, _reason = _judge(intent=True, effect=succeeded, check=True)
    if succeeded:
        reason = "the command exited 0"
    else:
        reason = (
            "the command exited {}, so the claim that it succeeded is "
            "contradicted".format(execution.exit_code)
        )
    return _outcome(
        verdict=verdict,
        reason=reason,
        next_step=(
            None
            if succeeded
            else "re-run the command by hand to see why it failed, then re-run this check"
        ),
        declaration=declaration,
        witness_type="exit_status",
        evidence=evidence,
        execution=execution,
    )


def check_diff(
    *,
    cwd: str,
    timeout: float = 30.0,
    recorded: Optional[baseline_mod.RepoBaseline] = None,
) -> CheckOutcome:
    """Judge "the working tree changed" against the recorded dirty set.

    Without a recording this can still answer one direction: an *empty* change
    set contradicts the claim ("nothing changed"), while a non-empty one
    proves only that *something* is dirty -- possibly since before the claim.
    So empty is ``FAIL`` and non-empty is ``INVALID``: the same asymmetry as
    ``check_file``, for the same reason.
    """
    cwd_abs = os.path.abspath(cwd)
    declaration = "diff"

    try:
        witness = GitWitness(cwd_abs, timeout=timeout)
    except (GitWitnessError, OSError) as problem:
        return _outcome(
            verdict=Verdict.INVALID,
            reason="no change set can be read: {}".format(problem),
            next_step="run this inside a git repository, then re-run this check",
            declaration=declaration,
            witness_type="git_diff",
            evidence={"repo_root": None},
        )

    repo_root = witness.repo_root
    baseline = (
        recorded
        if recorded is not None
        else baseline_mod.load(cwd_abs, repo_root=repo_root)
    )

    try:
        changed = witness.get_changed_files()
    except (GitWitnessError, OSError) as problem:
        return _outcome(
            verdict=Verdict.INVALID,
            reason="the change set could not be read: {}".format(problem),
            next_step="check `git status` by hand, then re-run this check",
            declaration=declaration,
            witness_type="git_diff",
            evidence={"repo_root": repo_root},
        )

    ignored = [path for path in changed if _is_recording_artefact(path)]
    changed = [path for path in changed if not _is_recording_artefact(path)]

    evidence: Dict[str, Any] = {
        "repo_root": repo_root,
        "changed": list(changed),
        "recording": baseline.source if baseline else None,
    }
    if ignored:
        evidence["ignored_recording_paths"] = ignored

    if baseline is None:
        if not changed:
            _verdict, _reason = _judge(intent=True, effect=False, check=True)
            return _outcome(
                verdict=_verdict,
                reason="the working tree has no changes, so the claimed change did not happen",
                next_step="run the step that was supposed to change it, then re-run this check",
                declaration=declaration,
                witness_type="git_diff",
                evidence=evidence,
            )
        evidence["baseline_searched"] = baseline_mod.candidate_paths(
            cwd_abs, repo_root=repo_root
        )
        return _outcome(
            verdict=Verdict.INVALID,
            reason=(
                "the working tree has {} changed path(s), but no recording says "
                "which changes came before this claim".format(len(changed))
            ),
            next_step=NEXT_STEP_BASELINE_DIFF,
            declaration=declaration,
            witness_type="git_diff",
            evidence=evidence,
        )

    evidence["recorded_dirty"] = len(
        [path for path in baseline.dirty if not _is_recording_artefact(path)]
    )
    evidence["recorded_at"] = baseline.created_at
    try:
        moved = witness.changed_since(dict(baseline.dirty))
    except (GitWitnessError, OSError) as problem:
        return _outcome(
            verdict=Verdict.INVALID,
            reason="the change set could not be compared: {}".format(problem),
            next_step="remove the unreadable path from the tree, then re-run this check",
            declaration=declaration,
            witness_type="git_diff",
            evidence=evidence,
        )

    ignored_moved = [path for path in moved if _is_recording_artefact(path)]
    moved = [path for path in moved if not _is_recording_artefact(path)]
    if ignored_moved:
        evidence["ignored_recording_paths"] = ignored_moved

    evidence["changed_since_recording"] = list(moved)
    if moved:
        return _outcome(
            verdict=Verdict.PASS,
            reason="{} path(s) changed since the recording".format(len(moved)),
            next_step=None,
            declaration=declaration,
            witness_type="git_diff",
            evidence=evidence,
        )

    _verdict, _reason = _judge(intent=True, effect=False, check=True)
    return _outcome(
        verdict=_verdict,
        reason="nothing changed since the recording, so the claimed change did not happen",
        next_step="run the step that was supposed to change it, then re-run this check",
        declaration=declaration,
        witness_type="git_diff",
        evidence=evidence,
    )
