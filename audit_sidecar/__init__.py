"""agent-harness-audit: an execution-layer audit sidecar for agent harnesses.

MVP reference implementation of the audit method described in the paper
"From Formal Verification to Agents: A Method-Transfer Audit of False
Completion in AI Agent Harnesses".

Public surface::

    Verdict          four-valued verdict (PASS / FAIL / INVALID / NOT RUN)
    VerdictEngine    judge one (intent, effect, check) case
    ConfidenceAccount sliding-window trust balance
    run_self_audit   known-answer gate that must pass before the tool runs

Only the Python standard library is used.
"""

from __future__ import annotations

from .confidence import ConfidenceAccount
from .git_witness import GitWitness, GitWitnessError
from .mcp_proxy import MCPProxy, UnknownToolError
from .runner import ExecutionResult, SafeRunner
from .self_audit import run_self_audit
from .verdict import Verdict, VerdictEngine

__version__ = "0.5.0"

__all__ = [
    "ConfidenceAccount",
    "ExecutionResult",
    "GitWitness",
    "GitWitnessError",
    "MCPProxy",
    "SafeRunner",
    "UnknownToolError",
    "Verdict",
    "VerdictEngine",
    "run_self_audit",
    "__version__",
]
