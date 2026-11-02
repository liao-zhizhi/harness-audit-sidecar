"""agent-harness-audit: an execution-layer audit sidecar for agent harnesses.

MVP reference implementation of the audit method described in the paper
"From Formal Verification to Agents: A Method-Transfer Audit of False
Completion in AI Agent Harnesses".

Public surface::

    Verdict          four-valued verdict (PASS / FAIL / INVALID / NOT RUN)
    VerdictEngine    judge one (intent, effect, check) case
    JudgmentBase     load the external judgment base (rules/*.yaml)
    RuleEngine       judge with the lattice, then tighten by rule
    ConfidenceAccount sliding-window trust balance
    run_self_audit   known-answer gate that must pass before the tool runs

The audit logic is standard library; PyYAML is used only to read the rules.
"""

from __future__ import annotations

from .confidence import ConfidenceAccount
from .git_witness import GitWitness, GitWitnessError
from .judgment_base import JudgmentBase, RuleError, default_rules_dir, packaged_rules_dir
from .mcp_proxy import MCPProxy, UnknownToolError
from .rule_engine import RuleDecision, RuleEngine
from .runner import ExecutionResult, SafeRunner
from .self_audit import run_self_audit
from .verdict import Verdict, VerdictEngine

__version__ = "0.6.1"

__all__ = [
    "ConfidenceAccount",
    "ExecutionResult",
    "GitWitness",
    "GitWitnessError",
    "JudgmentBase",
    "MCPProxy",
    "RuleDecision",
    "RuleEngine",
    "RuleError",
    "SafeRunner",
    "UnknownToolError",
    "Verdict",
    "VerdictEngine",
    "default_rules_dir",
    "packaged_rules_dir",
    "run_self_audit",
    "__version__",
]
