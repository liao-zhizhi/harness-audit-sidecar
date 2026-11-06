"""agent-harness-audit: an execution-layer audit sidecar for agent harnesses.

MVP reference implementation of the audit method described in the paper
"From Formal Verification to Agents: A Method-Transfer Audit of False
Completion in AI Agent Harnesses".

Public surface::

    Verdict          four-valued verdict (PASS / FAIL / INVALID / NOT RUN)
    VerdictEngine    judge one (intent, effect, check) case
    CitationWitness  resolve a DOI/arXiv id and compare it with a claim
    CitationAuditProxy  audit citations with the same lattice
    JudgmentBase     load the external judgment base (rules/*.yaml)
    RuleEngine       judge with the lattice, then tighten by rule
    ImmuneState      inflammation state that scopes how much a call is worth
    select_tier      choose the innate or adaptive defence for one call
    ConfidenceAccount sliding-window trust balance
    run_self_audit   known-answer gate that must pass before the tool runs

The audit logic is standard library; PyYAML is used only to read the rules.
"""

from __future__ import annotations

from .confidence import ConfidenceAccount
from .citation_witness import CitationWitness
from .git_witness import GitWitness, GitWitnessError
from .immune_state import ImmuneState, select_tier
from .judgment_base import JudgmentBase, RuleError, default_rules_dir, packaged_rules_dir
from .mcp_proxy import MCPProxy, UnknownToolError
from .rule_engine import RuleDecision, RuleEngine
from .runner import ExecutionResult, SafeRunner
from .self_audit import run_self_audit
from .verdict import Verdict, VerdictEngine
from .tools.citation_audit import CitationAuditProxy

__version__ = "0.9.3"

__all__ = [
    "CitationAuditProxy",
    "CitationWitness",
    "ConfidenceAccount",
    "ExecutionResult",
    "GitWitness",
    "GitWitnessError",
    "ImmuneState",
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
    "select_tier",
    "__version__",
]
