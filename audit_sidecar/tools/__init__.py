"""Tool specifications that collect their own witnesses.

The default registry in :mod:`audit_sidecar.mcp_proxy` maps a tool name to a
shell command, because the sidecar's original witnesses are filesystem effects.
Some claims have no shell command at all -- a citation is checked by resolving
an identifier over HTTP -- and this package holds those tools.

The point of keeping them here rather than in the proxy is reviewability: a
witness that reaches the network is a different trust boundary from one that
reads ``git status``, and a reader should be able to see that difference in the
import graph.
"""

from __future__ import annotations

__all__ = ["citation_audit", "eco_api", "ledger", "membrane"]
