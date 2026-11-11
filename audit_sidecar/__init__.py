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
    analyze_chronic_drift  detect a "perfect loop" that is a decaying transient
    run_self_audit   known-answer gate that must pass before the tool runs

The audit logic is standard library; PyYAML is used only to read the rules.
"""

from __future__ import annotations

from .confidence import ConfidenceAccount
from .cloud_report import (
    MUTATION_TYPES,
    PROPAGATION_REPORTING,
    CloudReport,
    MutationReport,
    mutation_kind,
    propagation_capability,
    new_trace_id,
    report_execution,
    report_mutation,
    witness_digest,
)
from .drift import DriftReport, analyze_chronic_drift
from .citation_witness import CitationWitness
from .git_witness import GitWitness, GitWitnessError
from .immune_state import ImmuneState, select_tier
from .judgment_base import (
    JudgmentBase,
    RuleError,
    default_rules_dir,
    default_rules_dirs,
    packaged_rules_dir,
)
from .mcp_proxy import MCPProxy, UnknownToolError
from .rule_engine import RuleDecision, RuleEngine
from .runner import ExecutionResult, SafeRunner
from .self_audit import run_self_audit
from .verdict import Verdict, VerdictEngine
from .tools.citation_audit import CitationAuditProxy

__version__ = "0.10.2"

__all__ = [
    "CitationAuditProxy",
    "CitationWitness",
    "CloudReport",
    "ConfidenceAccount",
    "DriftReport",
    "ExecutionResult",
    "GitWitness",
    "GitWitnessError",
    "ImmuneState",
    "JudgmentBase",
    "MCPProxy",
    "MUTATION_TYPES",
    "MutationReport",
    "PROPAGATION_REPORTING",
    "RuleDecision",
    "RuleEngine",
    "RuleError",
    "SafeRunner",
    "UnknownToolError",
    "Verdict",
    "VerdictEngine",
    "analyze_chronic_drift",
    "mutation_kind",
    "default_rules_dir",
    "default_rules_dirs",
    "new_trace_id",
    "packaged_rules_dir",
    "propagation_capability",
    "report_execution",
    "report_mutation",
    "run_self_audit",
    "select_tier",
    "witness_digest",
    "__version__",
]
