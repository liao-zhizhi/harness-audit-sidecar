"""Witness provenance: who produced the witness, and which world it describes.

``verdict.py`` compares three observations (intent, effect, claim) and is
complete for that comparison. It does not ask two questions that decide whether
a PASS is worth anything:

1. **Who produced the witness?** A witness the agent reported about itself is
   not independent evidence. When that report arrives over a direct API path --
   no sidecar in between, nothing else observing -- the run has no independent
   witness at all, and the lattice's PASS would be a self-certification
   (case 3, direct topology).
2. **Which world does the witness describe?** A return value or an exit code
   proves something about the *program*. It does not prove the file changed or
   the experiment succeeded. Only a witness of the *world* can carry a PASS.

This module adds no verdict value and changes no precedence. It only **caps**
one: it may turn a PASS into INVALID, and may never touch FAIL, INVALID or
NOT RUN. That mirrors the monotonicity rule of ``rule_engine``: a policy may
tighten a verdict and may never loosen one. A cap that could soften a
contradiction would be a way to argue with the witness.

Both inputs default to ``None``, which means "not declared" and applies no cap.
That keeps every existing caller working unchanged; a deployment opts in by
declaring provenance at the call site.

**接线状态：未接电。** 当前生产路径无 direct_api 入口，cap 未接电。字段与约束
已就位，等真实 direct_api 路径出现时再接入；不为接电而造入口。

*Not wired: the production paths have no ``direct_api`` entry yet.* Every path
that reaches ``judge()`` runs inside ``SafeRunner`` -- ``runner.py`` (the engine
call), ``mcp_proxy.py:561`` and ``mcp_server.py:763`` all go through
``run_shell`` -- so ``topology`` is always ``sidecar`` and this cap cannot fire.
The two non-sidecar paths in the repo do not reach ``judge()`` at all:
``MembraneAgent.execute_tool_with_warrant`` runs the tool in-process but has its
own membrane that recomputes the digest from the artefact
(``tools/membrane.py::_resolve_witness``) and deliberately does not import the
auditor; ``metabolism_paper/dogfood_audit.py::audit_effect`` judges outside the
runner but calls ``VerdictEngine.explain`` (the bare lattice), never
``RuleEngine.judge``. Wiring either one would mean integrating another package's
verdict path or inventing an entry, so the cap stays dormant until a real
``direct_api`` caller exists.
"""

from __future__ import annotations

from typing import Optional, Tuple

from .verdict import Verdict

__all__ = [
    "PRODUCERS",
    "TOPOLOGIES",
    "CAP_REASON_PREFIX",
    "cap_verdict",
]

#: Who produced the witness. ``self_reported`` means the only account of the
#: effect is the agent's own claim; ``independent`` means something outside the
#: agent looked.
PRODUCERS = frozenset({"independent", "self_reported", "unknown"})

#: How the claim reached the judge. ``sidecar`` means it crossed the proxy and
#: at least one other observer is in the path; ``direct_api`` means the agent
#: called the target API itself with nothing in between.
TOPOLOGIES = frozenset({"sidecar", "direct_api", "other"})

#: Stable prefix so a capped verdict can be grepped out of a receipt without
#: re-loading the code that produced it.
CAP_REASON_PREFIX = "witness provenance cap"


def _check_enum(value: Optional[str], allowed: frozenset, field: str) -> None:
    if value is None:
        return
    if not isinstance(value, str) or value not in allowed:
        # Deliberately fatal rather than ignored. A typo such as
        # "self-reported" would otherwise silently disable the cap, which is
        # the one failure mode this module exists to prevent.
        raise ValueError(
            "{} must be one of {}, got {!r}".format(
                field, sorted(allowed), value
            )
        )


def cap_verdict(
    verdict: Verdict,
    reason: str = "",
    *,
    producer: Optional[str] = None,
    topology: Optional[str] = None,
    program_fact: Optional[bool] = None,
    world_fact: Optional[bool] = None,
) -> Tuple[Verdict, str]:
    """Return the verdict after applying the provenance caps.

    Only ``PASS`` can be capped. ``FAIL`` is a contradiction the witness already
    established and ``INVALID``/``NOT RUN`` are already below a green light, so
    a cap must not rewrite them -- softening a FAIL would be the exact
    "argue with the witness" move the audit forbids.

    Caps, in order (all that fire are named in the reason):

    * ``producer="self_reported"`` with ``topology="direct_api"`` -- the only
      account of the effect is the agent's own, over a path where nothing else
      could have observed. Ceiling: INVALID.
    * ``program_fact=True`` with ``world_fact`` not True -- the witness settles
      what the program did, not what changed in the world. Ceiling: INVALID.
    """
    _check_enum(producer, PRODUCERS, "producer")
    _check_enum(topology, TOPOLOGIES, "topology")

    if verdict is not Verdict.PASS:
        return verdict, reason

    fired = []

    if producer == "self_reported" and topology == "direct_api":
        fired.append(
            "producer=self_reported over topology=direct_api: "
            "the only account of the effect is the agent's own report"
        )

    if program_fact is True and world_fact is not True:
        fired.append(
            "program_fact witnessed without an independent world_fact: "
            "a return value does not prove the world changed"
        )

    if not fired:
        return verdict, reason

    detail = "{}: {}{}".format(
        CAP_REASON_PREFIX,
        "; ".join(fired),
        " ({})".format(reason) if reason else "",
    )
    return Verdict.INVALID, detail
