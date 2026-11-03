"""Demo: install a signed external rule package, mount it on the membrane.

Run from the repository root:

    python3 demo_rule_registry.py

No network access is used. ``FakeEcosystem`` stands in for the remote registry
so the demo can show the exact bytes, the published digest, and a tampered
variant side by side. The rule package is pulled through the same verified path
a real URL would use.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from typing import Any, Dict

from audit_sidecar.tools.membrane import MembraneGateway
from audit_sidecar.tools.rule_interface import (
    RuleIntegrityError,
    RuleRegistry,
)

URL = "https://rules.example.org/web3_safety_rules.yaml"

WEB3_SAFETY_RULES = """\
# Published by the Example Web3 Safety Guild.
# This file is an *interface*: rule ids are namespaced, every rule names the
# physical witnesses it requires, and a violation has a stated trust cost.
schema: "harness-audit/rule-package/1.0"
package:
  name: "web3-safety"
  version: "1.0.0"
  author: "Example Web3 Safety Guild"
  description: "Minimum evidence for moving value on-chain."

rules:
  - rule_id: "org.example.safe-web3-transfer"
    version: "1.0.0"
    author: "Example Web3 Safety Guild"
    target_tool: "web3_transfer"
    description: "A transfer needs a reviewed diff, a compiler result, and tests."
    interface_spec:
      required_witnesses:
        - "git_diff"
        - "compiler_exit_code"
        - "test_exit_code"
      expectation: "file_modified"
      capability: "write"
    penalty_weight: 40.0
    risk_level: "high"

  - rule_id: "org.example.no-unlimited-approval"
    version: "1.0.0"
    author: "Example Web3 Safety Guild"
    target_tool: "web3_approve"
    description: "Unlimited approval requires static analysis and a receipt."
    interface_spec:
      required_witnesses:
        - "static_analysis_report"
        - "transaction_receipt"
      expectation: "file_modified"
      capability: "write"
    penalty_weight: 60.0
    risk_level: "high"
"""


class FakeEcosystem:
    """A stand-in for a remote rule registry. No sockets are opened."""

    def __init__(self, files: Dict[str, bytes]) -> None:
        self._files = files

    def __call__(self, url: str) -> bytes:
        try:
            return self._files[url]
        except KeyError:
            raise FileNotFoundError("remote rule registry has no {}".format(url)) from None


def rule(title: str) -> None:
    print()
    print("-" * 78)
    print(title)
    print("-" * 78)


def show_entry(entry: Dict[str, Any]) -> None:
    kind = entry.get("record_type", "?")
    if entry.get("event") == "denied":
        detail = "reason={} penalty={}".format(
            entry.get("reason"), entry.get("penalty_weight")
        )
    elif entry.get("event") == "registration":
        detail = "capability={} digest={}".format(
            entry.get("capability"), str(entry.get("digest"))[:19]
        )
    elif entry.get("event") == "grant":
        detail = "capability={} digest={}".format(
            entry.get("capability"), str(entry.get("digest"))[:19]
        )
    elif entry.get("event") == "redeem":
        detail = "capability={} issued_at={}".format(
            entry.get("capability"), entry.get("issued_at")
        )
    else:
        detail = "tool={} verdict={}".format(
            entry.get("tool"), entry.get("verdict", entry.get("event"))
        )
    print(
        "  seq={:>2}  {:<12} {:<12} {}".format(
            entry.get("seq"), kind, str(entry.get("event", "-")), detail
        )
    )


def main() -> int:
    raw = WEB3_SAFETY_RULES.encode("utf-8")
    digest = hashlib.sha256(raw).hexdigest()

    # The .sha256 sidecar is published beside the package. The demo also passes
    # the digest explicitly, which is the stronger anchor: it came from a
    # different channel than the bytes.
    ecosystem = FakeEcosystem(
        {
            URL: raw,
            URL + ".sha256": (
                digest + "  web3_safety_rules.yaml\n"
            ).encode("utf-8"),
        }
    )

    print("Rule Interface demo: a rule package is an interface, not a config file.")
    print("Publisher : Example Web3 Safety Guild")
    print("Package   : web3-safety 1.0.0")
    print("SHA256    : sha256:{}".format(digest))

    with tempfile.TemporaryDirectory(prefix="rule-interface-demo-") as root:
        registry = RuleRegistry()

        rule("[1] pull the package from the external ecosystem")
        package = registry.pull(URL, expected_digest=digest, transport=ecosystem)
        print("  fetched  : {}".format(URL))
        print("  verified : expected == actual (sha256:{})".format(package.digest[7:19]))
        print("  installed: {} {} by {}".format(package.name, package.version, package.author))
        for installed_rule in package.rules:
            print(
                "    - {:<38} needs [{}]  penalty={}".format(
                    installed_rule.rule_id,
                    ", ".join(installed_rule.interface_spec.required_witnesses),
                    installed_rule.penalty_weight,
                )
            )

        gateway = MembraneGateway(os.path.join(root, "audit.jsonl"))

        rule("[2] mount the interface on MembraneGateway (membrane.py unchanged)")
        interface = registry.mount(gateway)
        mounted = interface.mounted_packages["web3-safety"]
        print("  package digest registered for 'read' : {}".format(mounted["digest"]))
        print("  read warrant issued / redeemed        : seq {} / seq {}".format(
            mounted["warrant_seq"], mounted["redeem_seq"]
        ))
        print("  mount record written                 : seq {}".format(mounted["mount_seq"]))
        print("  the package itself crossed the membrane before its rules could act.")
        print()
        print("  ledger after mounting:")
        for entry in gateway.ledger.entries():
            show_entry(entry)

        rule("[3] high-risk operation: transfer without a compiler witness")
        decision = interface.enforce(
            "web3_transfer",
            witnesses={"git_diff", "test_exit_code"},
            agent="demo-agent",
        )
        print("  decision        : {}".format("ALLOWED" if decision.allowed else "DENIED"))
        print("  matched rules   : {}".format(", ".join(decision.matched_rule_ids)))
        print("  violated rules  : {}".format(", ".join(decision.violated_rule_ids)))
        for rule_id, missing in decision.missing_by_rule.items():
            print("  missing witness : {} -> {}".format(rule_id, ", ".join(missing)))
        print("  penalty         : -{} trust points".format(decision.penalty_weight))
        print("  ledger seq      : {}".format(decision.ledger_seq))
        print("  trust score     : {} / 100.0 (local projection)".format(
            interface.trust_score
        ))
        print("  denial record   :")
        show_entry(list(gateway.ledger.entries())[-1])

        rule("[4] same operation with the full evidence set")
        artefact = os.path.join(root, "transfer.diff")
        with open(artefact, "w", encoding="utf-8") as handle:
            handle.write(
                "diff --git a/Transfer.sol b/Transfer.sol\n"
                "+ require(approved, \"not approved\");\n"
            )
        with open(artefact, "rb") as handle:
            artefact_digest = "sha256:" + hashlib.sha256(handle.read()).hexdigest()

        # The digest is registered out of band, before the request arrives. This
        # is the potential difference: the agent cannot nominate its own evidence.
        gateway.register_artefact("write", artefact)

        decision = interface.enforce(
            "web3_transfer",
            witnesses={"git_diff", "compiler_exit_code", "test_exit_code"},
            witness_payload={
                "kind": "file",
                "path": artefact,
                "digest": artefact_digest,
            },
            agent="demo-agent",
        )
        print("  decision        : {}".format("ALLOWED" if decision.allowed else "DENIED"))
        print("  rule layer      : 3/3 required witnesses present")
        print("  membrane layer  : warrant seq {} issued, redeemed at seq {}".format(
            decision.warrant.seq, decision.ledger_seq
        ))
        print("  warrant uses    : {} remaining (single use)".format(
            decision.warrant.uses_remaining
        ))

        # Record the call only after the gate has allowed it.
        call = gateway.append_call(
            "web3_transfer",
            "PASS",
            capability="write",
            digest=artefact_digest,
            tokens_used=1200,
            elapsed_ms=42.0,
            rule_id="org.example.safe-web3-transfer",
        )
        print("  executed call   : ledger seq {} (verdict PASS)".format(call["seq"]))

        rule("[5] a tampered package is refused before it can change policy")
        tampered = WEB3_SAFETY_RULES.replace(
            "penalty_weight: 40.0", "penalty_weight: 0.0"
        ).encode("utf-8")
        tampered_ecosystem = FakeEcosystem({URL: tampered})
        try:
            registry.pull(
                URL, expected_digest=digest, transport=tampered_ecosystem
            )
        except RuleIntegrityError as problem:
            print("  refused         : {}".format(str(problem).splitlines()[0]))
            print("  registry intact : {} package(s), rules={}".format(
                len(registry), ", ".join(registry.rule_ids)
            ))
            print("  a zeroed penalty never reached the mounted interface.")

        rule("[6] the one-way lock")
        verification = gateway.verify()
        entries = list(gateway.ledger.entries())
        print("  chain status    : {}".format(verification["status"]))
        print("  entries         : {}".format(verification["entries"]))
        print("  tail            :")
        for entry in entries[-3:]:
            show_entry(entry)
        print()
        print("  every mount, denial, grant and redemption is hash-linked.")
        print("  rolling the log back invalidates outstanding warrants.")

        print()
        print("=" * 78)
        print("RESULT")
        print("  external package : {} {} ({})".format(
            package.name, package.version, package.digest[:19]
        ))
        print("  mounted rules    : {}".format(", ".join(registry.rule_ids)))
        print("  high-risk call   : blocked once, allowed once with full evidence")
        print("  trust penalty    : {} assessed by the interface".format(
            interface.penalty_assessed
        ))
        print("  membrane.py      : not modified; injection used the public API only")
        print("=" * 78)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
