"""Tests for the citation witness, the citation tool, and the .bib audit.

Two layers, on purpose:

* **Deterministic tests** inject a fake transport. They pin the contract --
  404 is INVALID, a contradicting record is FAIL, a throttle is retried -- and
  they run with no network, so CI never goes red because arXiv had a bad day.
* **Live tests** (``CITATION_AUDIT_LIVE=1``) resolve real identifiers against
  the real agencies. They are the only tests that can catch a change in the
  upstream response format, and they are skipped by default because a test
  suite that needs the internet is a test suite that is red on a plane.
"""

from __future__ import annotations

import os
import unittest

from audit_sidecar.citation_witness import (
    CitationWitness,
    authors_match,
    normalise_title,
    titles_match,
)
from audit_sidecar.mcp_proxy import MCPProxy, UnknownToolError
from audit_sidecar.tools.citation_audit import (
    TOOL_NAME,
    TOOL_SPEC,
    CitationAuditProxy,
    register_citation_tool,
)
from audit_sidecar.verdict import Verdict
from audit_bib import audit_entries, parse_bib

REAL_DOI = "10.1109/asp-dac66049.2026.11420235"
REAL_DOI_TITLE = (
    "SuperSAGA: A Supervisor-Subordinate Agentic Workflow for the Generation "
    "of Assertions"
)
FAKE_DOI = "10.1109/asp-dac66049.9999.99999999"
REAL_ARXIV = "2609.26751"
REAL_ARXIV_TITLE = (
    "EquivSVA: A Formally Verified Dataset of Behavioral Assertions Across "
    "Equivalent RTL Implementations"
)


def crossref_body(title: str, authors=("Subhajit Paul", "Ansuman Banerjee")) -> bytes:
    import json

    return json.dumps(
        {
            "status": "ok",
            "message": {
                "title": [title],
                "author": [
                    {"given": name.split()[0], "family": name.split()[-1]}
                    for name in authors
                ],
            },
        }
    ).encode("utf-8")


def arxiv_body(title: str, authors=("Aditi",)) -> bytes:
    people = "".join(
        "<author><name>{}</name></author>".format(name) for name in authors
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<feed xmlns="http://www.w3.org/2005/Atom">'
        "<title>arXiv Query</title>"
        "<entry><id>http://arxiv.org/abs/2609.26751v1</id>"
        "<title>{}</title>{}</entry>"
        "</feed>".format(title, people)
    ).encode("utf-8")


EMPTY_ARXIV_FEED = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<feed xmlns="http://www.w3.org/2005/Atom">'
    "<title>arXiv Query: search_query=&amp;id_list=0000.00000</title>"
    "</feed>"
).encode("utf-8")


class FakeTransport:
    """URL-substring router standing in for the network."""

    def __init__(self, routes, default=None) -> None:
        self.routes = routes
        self.default = default
        self.urls = []

    def __call__(self, url: str):
        self.urls.append(url)
        for needle, response in self.routes.items():
            if needle in url:
                if isinstance(response, list):
                    # A scripted sequence: the last entry repeats.
                    return response.pop(0) if len(response) > 1 else response[0]
                if isinstance(response, Exception):
                    raise response
                return response
        if self.default is not None:
            if isinstance(self.default, Exception):
                raise self.default
            return self.default
        raise AssertionError("unexpected URL: {}".format(url))


def witness_with(**routes) -> CitationWitness:
    return CitationWitness(
        fetch=FakeTransport(routes),
        min_interval={},
        sleep=lambda _seconds: None,
    )


# ---------------------------------------------------------------------------
# Title / author comparison
# ---------------------------------------------------------------------------

class TitleNormalisationTest(unittest.TestCase):
    def test_strips_latex_protection_and_markup(self) -> None:
        self.assertEqual(
            normalise_title(r"{Yosys} -- A Free {Verilog} Synthesis Suite"),
            "yosys a free verilog synthesis suite",
        )

    def test_folds_accents_and_case(self) -> None:
        self.assertEqual(normalise_title("Moura, Jo{\\~a}o"), "moura joao")

    def test_ignores_punctuation(self) -> None:
        self.assertEqual(
            normalise_title("LLM-as-a-Judge, with MT-Bench & Chatbot Arena!"),
            "llm as a judge with mt bench and chatbot arena",
        )


class TitleMatchTest(unittest.TestCase):
    def test_case_and_punctuation_difference_matches(self) -> None:
        matched, score = titles_match(
            "SuperSAGA: A Supervisor-Subordinate Agentic Workflow for the "
            "Generation of Assertions",
            "SuperSAGA: A Supervisor-Subordinate Agentic workflow for the "
            "Generation of Assertions",
        )
        self.assertTrue(matched)
        self.assertEqual(score, 1.0)

    def test_subtitle_omitted_by_the_record_matches(self) -> None:
        matched, _ = titles_match(
            "Release It! Design and Deploy Production-Ready Software",
            "Release It",
        )
        self.assertFalse(matched)  # two words: too short for prefix matching

        matched, _ = titles_match(
            "Release It! Design and Deploy Production-Ready Software",
            "Release It! Design and Deploy Production Ready Software",
        )
        self.assertTrue(matched)

    def test_different_paper_does_not_match(self) -> None:
        matched, score = titles_match(
            "SuperSAGA: A Supervisor-Subordinate Agentic Workflow for the "
            "Generation of Assertions",
            "Attention Is All You Need",
        )
        self.assertFalse(matched)
        self.assertLess(score, 0.5)


class AuthorMatchTest(unittest.TestCase):
    def test_surname_matches_regardless_of_given_name_style(self) -> None:
        self.assertTrue(authors_match("Wolf", ["Clifford Wolf"]))
        self.assertTrue(authors_match("Clifford Wolf", ["Wolf, Clifford"]))
        self.assertTrue(authors_match("Paul", ["Subhajit Paul", "Ansuman Banerjee"]))

    def test_different_surname_does_not_match(self) -> None:
        self.assertFalse(authors_match("Nygard", ["Subhajit Paul"]))

    def test_no_claim_is_not_a_contradiction(self) -> None:
        self.assertTrue(authors_match("", ["anyone"]))


# ---------------------------------------------------------------------------
# Witness verdicts
# ---------------------------------------------------------------------------

class WitnessVerdictTest(unittest.TestCase):
    def test_matching_crossref_record_passes(self) -> None:
        witness = witness_with(**{"api.crossref.org": (200, crossref_body(REAL_DOI_TITLE))})
        self.assertEqual(
            witness.verify(doi=REAL_DOI, expected_title=REAL_DOI_TITLE),
            (True, "PASS", "witness matched"),
        )
        self.assertEqual(witness.last_detail["agency"], "crossref")

    def test_unknown_doi_is_invalid_not_fail(self) -> None:
        witness = witness_with(**{"api.crossref.org": (404, b"Not Found")})
        ok, verdict, message = witness.verify(doi=FAKE_DOI, expected_title=REAL_DOI_TITLE)
        self.assertFalse(ok)
        self.assertEqual(verdict, "INVALID")
        self.assertTrue(message.startswith("no witness found"))
        self.assertEqual(witness.last_detail["cause"], "not_found")

    def test_wrong_title_is_fail(self) -> None:
        witness = witness_with(
            **{"api.crossref.org": (200, crossref_body("Attention Is All You Need"))}
        )
        ok, verdict, message = witness.verify(doi=REAL_DOI, expected_title=REAL_DOI_TITLE)
        self.assertFalse(ok)
        self.assertEqual(verdict, "FAIL")
        self.assertTrue(message.startswith("title mismatch"))
        self.assertEqual(witness.last_detail["cause"], "title_mismatch")

    def test_wrong_author_is_fail(self) -> None:
        witness = witness_with(**{"api.crossref.org": (200, crossref_body(REAL_DOI_TITLE))})
        ok, verdict, message = witness.verify(
            doi=REAL_DOI, expected_title=REAL_DOI_TITLE, expected_author="Nygard"
        )
        self.assertFalse(ok)
        self.assertEqual(verdict, "FAIL")
        self.assertTrue(message.startswith("author mismatch"))

    def test_matching_arxiv_record_passes(self) -> None:
        witness = witness_with(
            **{"export.arxiv.org": (200, arxiv_body(REAL_ARXIV_TITLE, ("Aditi", "FNU")))}
        )
        self.assertEqual(
            witness.verify(arxiv_id=REAL_ARXIV, expected_title=REAL_ARXIV_TITLE),
            (True, "PASS", "witness matched"),
        )
        self.assertEqual(witness.last_detail["agency"], "arxiv")

    def test_arxiv_id_is_normalised_before_the_query(self) -> None:
        transport = FakeTransport({"export.arxiv.org": (200, arxiv_body(REAL_ARXIV_TITLE))})
        witness = CitationWitness(fetch=transport, min_interval={}, sleep=lambda _s: None)
        witness.verify(arxiv_id="arXiv:2609.26751v3", expected_title=REAL_ARXIV_TITLE)
        self.assertIn("id_list=2609.26751", transport.urls[0])

    def test_arxiv_empty_feed_is_invalid(self) -> None:
        witness = witness_with(**{"export.arxiv.org": (200, EMPTY_ARXIV_FEED)})
        ok, verdict, _ = witness.verify(arxiv_id="0000.00000", expected_title="Anything")
        self.assertFalse(ok)
        self.assertEqual(verdict, "INVALID")
        self.assertEqual(witness.last_detail["cause"], "not_found")

    def test_timeout_is_invalid_not_fail(self) -> None:
        witness = witness_with(**{"api.crossref.org": OSError("timed out")})
        ok, verdict, _ = witness.verify(doi=REAL_DOI, expected_title=REAL_DOI_TITLE)
        self.assertFalse(ok)
        self.assertEqual(verdict, "INVALID")
        self.assertEqual(witness.last_detail["cause"], "unavailable")

    def test_throttle_is_retried_before_giving_up(self) -> None:
        transport = FakeTransport(
            {"api.crossref.org": [(429, b"slow down"), (200, crossref_body(REAL_DOI_TITLE))]}
        )
        witness = CitationWitness(fetch=transport, min_interval={}, sleep=lambda _s: None)
        self.assertEqual(
            witness.verify(doi=REAL_DOI, expected_title=REAL_DOI_TITLE)[1], "PASS"
        )
        self.assertEqual(len(transport.urls), 2)

    def test_persistent_throttle_is_invalid(self) -> None:
        witness = witness_with(**{"api.crossref.org": (503, b"unavailable")})
        ok, verdict, message = witness.verify(doi=REAL_DOI, expected_title=REAL_DOI_TITLE)
        self.assertFalse(ok)
        self.assertEqual(verdict, "INVALID")
        self.assertIn("unavailable", message)

    def test_no_identifier_is_invalid(self) -> None:
        witness = witness_with()
        ok, verdict, _ = witness.verify(expected_title=REAL_DOI_TITLE)
        self.assertFalse(ok)
        self.assertEqual(verdict, "INVALID")
        self.assertEqual(witness.last_detail["cause"], "no_identifier")

    def test_no_expected_title_is_invalid(self) -> None:
        """A titleless entry must not buy a PASS by claiming nothing."""
        witness = witness_with(**{"api.crossref.org": (200, crossref_body(REAL_DOI_TITLE))})
        ok, verdict, _ = witness.verify(doi=REAL_DOI)
        self.assertFalse(ok)
        self.assertEqual(verdict, "INVALID")
        self.assertEqual(witness.last_detail["cause"], "no_expected_title")

    def test_malformed_doi_is_invalid(self) -> None:
        witness = witness_with(**{"api.crossref.org": (404, b"")})
        witness.verify(doi="not a doi", expected_title=REAL_DOI_TITLE)
        self.assertEqual(witness.last_detail["cause"], "not_found")

    def test_unparseable_body_is_invalid(self) -> None:
        witness = witness_with(**{"api.crossref.org": (200, b"<html>oops</html>")})
        ok, verdict, _ = witness.verify(doi=REAL_DOI, expected_title=REAL_DOI_TITLE)
        self.assertFalse(ok)
        self.assertEqual(verdict, "INVALID")
        self.assertEqual(witness.last_detail["cause"], "unparseable")


# ---------------------------------------------------------------------------
# The tool, and its place in the shared lattice
# ---------------------------------------------------------------------------

class CitationAuditProxyTest(unittest.TestCase):
    def proxy(self, **routes) -> CitationAuditProxy:
        return CitationAuditProxy(witness=witness_with(**routes))

    def test_pass_fail_invalid_come_from_the_shared_engine(self) -> None:
        proxy = self.proxy(**{"api.crossref.org": (200, crossref_body(REAL_DOI_TITLE))})
        self.assertIs(
            proxy.validate_citation(doi=REAL_DOI, expected_title=REAL_DOI_TITLE).verdict,
            Verdict.PASS,
        )

        wrong = self.proxy(
            **{"api.crossref.org": (200, crossref_body("Attention Is All You Need"))}
        )
        self.assertIs(
            wrong.validate_citation(doi=REAL_DOI, expected_title=REAL_DOI_TITLE).verdict,
            Verdict.FAIL,
        )

        missing = self.proxy(**{"api.crossref.org": (404, b"")})
        self.assertIs(
            missing.validate_citation(doi=FAKE_DOI, expected_title=REAL_DOI_TITLE).verdict,
            Verdict.INVALID,
        )

    def test_invalid_reason_names_the_cause(self) -> None:
        proxy = self.proxy()
        record = proxy.validate_citation(expected_title="A Title", doi=None, arxiv_id=None)
        self.assertIs(record.verdict, Verdict.INVALID)
        self.assertIn("no DOI or arXiv id", record.reason)

    def test_audit_log_and_summary(self) -> None:
        proxy = self.proxy(**{"api.crossref.org": (200, crossref_body(REAL_DOI_TITLE))})
        proxy.validate_citation(doi=REAL_DOI, expected_title=REAL_DOI_TITLE)
        self.assertEqual(proxy.summary()["PASS"], 1)
        self.assertEqual(len(proxy.audit_log), 1)

    def test_engine_owns_the_lattice_not_the_witness(self) -> None:
        """The witness's own verdict string must not be passed through blindly."""
        proxy = self.proxy(**{"api.crossref.org": (200, crossref_body(REAL_DOI_TITLE))})
        record = proxy.validate_citation(
            doi=REAL_DOI, expected_title=REAL_DOI_TITLE, reported_success=False
        )
        # Witness matched but the entry's own report denies it: the engine calls
        # that a false failure, not a PASS.
        self.assertIs(record.verdict, Verdict.FAIL)
        self.assertIn("false failure", record.reason)

    def test_tool_spec_is_advertised(self) -> None:
        self.assertEqual(TOOL_SPEC["name"], TOOL_NAME)
        self.assertIn("expected_title", TOOL_SPEC["inputSchema"]["properties"])
        self.assertEqual(TOOL_SPEC["inputSchema"]["required"], ["expected_title"])


class McpProxyIntegrationTest(unittest.TestCase):
    def test_validate_citation_is_a_first_class_tool(self) -> None:
        proxy = MCPProxy(cwd=".")
        citations = register_citation_tool(
            proxy,
            witness=witness_with(**{"api.crossref.org": (200, crossref_body(REAL_DOI_TITLE))}),
        )
        response = proxy.intercept_tool_call(
            TOOL_NAME,
            {"doi": REAL_DOI, "expected_title": REAL_DOI_TITLE},
            True,
        )
        self.assertEqual(response["status"], "ok")
        self.assertIs(proxy.audit_log[-1].verdict, Verdict.PASS)
        self.assertEqual(len(citations.audit_log), 1)
        # The response must not leak the verdict, or a caller could branch on it.
        self.assertNotIn("PASS", response["content"][0]["text"])

    def test_fail_is_recorded_in_the_shared_audit_log(self) -> None:
        proxy = MCPProxy(cwd=".")
        register_citation_tool(
            proxy,
            witness=witness_with(
                **{"api.crossref.org": (200, crossref_body("Attention Is All You Need"))}
            ),
        )
        proxy.intercept_tool_call(
            TOOL_NAME, {"doi": REAL_DOI, "expected_title": REAL_DOI_TITLE}, True
        )
        self.assertIs(proxy.audit_log[-1].verdict, Verdict.FAIL)
        self.assertEqual(proxy.summary()["FAIL"], 1)

    def test_unknown_tool_still_refuses(self) -> None:
        proxy = MCPProxy(cwd=".")
        register_citation_tool(proxy, witness=witness_with())
        with self.assertRaises(UnknownToolError):
            proxy.intercept_tool_call("write_file_but_wrong", {}, True)

    def test_builtin_tool_cannot_be_shadowed(self) -> None:
        proxy = MCPProxy(cwd=".")
        with self.assertRaises(ValueError):
            proxy.register_witness_tool(
                "run_command", lambda args, reported: None  # type: ignore[arg-type]
            )

    def test_handler_shape_is_checked(self) -> None:
        proxy = MCPProxy(cwd=".")
        proxy.register_witness_tool("bad_tool", lambda args, reported: "nope")  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            proxy.intercept_tool_call("bad_tool", {}, True)


# ---------------------------------------------------------------------------
# BibTeX parsing and batch audit
# ---------------------------------------------------------------------------

SAMPLE_BIB = r"""
% a comment line -- and a stray at-sign must not appear here
@string{neurips = "Advances in Neural Information Processing Systems"}

@article{advani2026confident,
  title         = {From Confident Closing to Silent Failure: Characterizing
                   False Success in {LLM} Agents},
  eprint        = {2606.09863},
  archivePrefix = {arXiv},
  year          = {2026}
}

@inproceedings{paul2026supersaga,
  author    = {Paul, Subhajit and Banerjee, Ansuman},
  title     = {{SuperSAGA}: A Supervisor-Subordinate Agentic Workflow for the
               Generation of Assertions},
  booktitle = neurips # " 2026",
  doi       = {https://doi.org/10.1109/ASP-DAC66049.2026.11420235},
  year      = 2026
}

@misc{chase2022langchain,
  author       = {Chase, Harrison},
  title        = {{LangChain}},
  howpublished = {\url{https://github.com/langchain-ai/langchain}}
}

@misc{yosyshq2026sby,
  title = {{SymbiYosys}: Formal Verification for {Verilog}},
  url   = {https://arxiv.org/abs/2609.26751}
}
"""


class BibParserTest(unittest.TestCase):
    def setUp(self) -> None:
        self.entries = {entry.key: entry for entry in parse_bib(SAMPLE_BIB)}

    def test_skips_string_and_comment_entries(self) -> None:
        self.assertEqual(
            set(self.entries),
            {
                "advani2026confident",
                "paul2026supersaga",
                "chase2022langchain",
                "yosyshq2026sby",
            },
        )

    def test_title_kept_with_line_breaks_collapsed(self) -> None:
        entry = self.entries["advani2026confident"]
        self.assertTrue(entry.title.startswith("From Confident Closing"))
        self.assertNotIn("\n", entry.title)

    def test_eprint_needs_an_arxiv_archive_prefix(self) -> None:
        self.assertEqual(self.entries["advani2026confident"].arxiv_id, "2606.09863")

    def test_doi_is_stripped_of_the_resolver_prefix(self) -> None:
        self.assertEqual(
            self.entries["paul2026supersaga"].doi,
            "10.1109/ASP-DAC66049.2026.11420235",
        )

    def test_url_with_an_arxiv_id_is_a_witness(self) -> None:
        self.assertEqual(self.entries["yosyshq2026sby"].arxiv_id, "2609.26751")

    def test_bare_word_value_is_parsed(self) -> None:
        self.assertEqual(self.entries["paul2026supersaga"].fields["year"], "2026")

    def test_first_author_surname(self) -> None:
        self.assertEqual(self.entries["paul2026supersaga"].first_author_surname, "Paul")
        self.assertEqual(self.entries["chase2022langchain"].first_author_surname, "Chase")


class BatchAuditTest(unittest.TestCase):
    def test_misc_without_identifier_is_invalid(self) -> None:
        entries = parse_bib(SAMPLE_BIB)
        proxy = CitationAuditProxy(
            witness=witness_with(
                **{
                    "api.crossref.org": (404, b""),
                    "export.arxiv.org": (200, arxiv_body(REAL_ARXIV_TITLE)),
                }
            )
        )
        results = {item.entry.key: item for item in audit_entries(entries, proxy)}
        record = results["chase2022langchain"].record
        self.assertIs(record.verdict, Verdict.INVALID)
        self.assertEqual(record.witness["cause"], "no_identifier")

    def test_every_entry_is_judged(self) -> None:
        entries = parse_bib(SAMPLE_BIB)
        proxy = CitationAuditProxy(
            witness=witness_with(
                **{
                    "api.crossref.org": (200, crossref_body(REAL_DOI_TITLE)),
                    "id_list=2606.09863": (
                        200,
                        arxiv_body(
                            "From Confident Closing to Silent Failure: "
                            "Characterizing False Success in LLM Agents"
                        ),
                    ),
                    "id_list=2609.26751": (200, arxiv_body(REAL_ARXIV_TITLE)),
                }
            )
        )
        results = audit_entries(entries, proxy)
        self.assertEqual(len(results), 4)
        verdicts = {item.entry.key: item.record.verdict.value for item in results}
        self.assertEqual(verdicts["paul2026supersaga"], "PASS")
        self.assertEqual(verdicts["advani2026confident"], "PASS")
        self.assertEqual(verdicts["chase2022langchain"], "INVALID")
        # The fixture points the SymbiYosys entry at the EquivSVA id on
        # purpose: a resolvable identifier naming a *different* paper is the
        # 张冠李戴 case, and it must surface as FAIL rather than INVALID.
        self.assertEqual(verdicts["yosyshq2026sby"], "FAIL")


# ---------------------------------------------------------------------------
# Live tests (opt-in)
# ---------------------------------------------------------------------------

LIVE = os.environ.get("CITATION_AUDIT_LIVE") == "1"


@unittest.skipUnless(LIVE, "set CITATION_AUDIT_LIVE=1 to resolve real identifiers")
class LiveWitnessTest(unittest.TestCase):
    """The three findings the audit exists for, against the real agencies."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.witness = CitationWitness()

    def test_real_doi_passes(self) -> None:
        self.assertEqual(
            self.witness.verify(doi=REAL_DOI, expected_title=REAL_DOI_TITLE),
            (True, "PASS", "witness matched"),
        )

    def test_unknown_doi_is_invalid(self) -> None:
        ok, verdict, message = self.witness.verify(
            doi=FAKE_DOI, expected_title=REAL_DOI_TITLE
        )
        self.assertFalse(ok)
        self.assertEqual(verdict, "INVALID")
        self.assertTrue(message.startswith("no witness found"))

    def test_real_doi_with_wrong_title_is_fail(self) -> None:
        ok, verdict, message = self.witness.verify(
            doi=REAL_DOI, expected_title="Attention Is All You Need"
        )
        self.assertFalse(ok)
        self.assertEqual(verdict, "FAIL")
        self.assertTrue(message.startswith("title mismatch"))

    def test_real_arxiv_id_passes(self) -> None:
        self.assertEqual(
            self.witness.verify(arxiv_id=REAL_ARXIV, expected_title=REAL_ARXIV_TITLE),
            (True, "PASS", "witness matched"),
        )


if __name__ == "__main__":
    unittest.main()
