#!/usr/bin/env python3
"""Audit a BibTeX file against physical witnesses.

    python3 audit_bib.py path/to/references.bib [more.bib ...]

Every entry in a ``.bib`` file is a claim: *this identifier* names *this paper*.
This script resolves each identifier against arXiv or CrossRef and groups the
entries by what the witness said:

    PASS     the record exists and its title matches the entry
    FAIL     the record exists and says something else (张冠李戴)
    INVALID  no witness could be obtained -- no identifier, an identifier the
             agency does not know, or a network that would not answer

INVALID is not an accusation. It is the honest third answer, and for a live web
page or a GitHub repository it is the *correct* one: there is nothing to
re-resolve, so the entry cannot be trusted the way a DOI can. That is the same
distinction the paper draws between "the agent failed" and "nobody looked".

Exit status is 0 when no entry FAILs, 1 when at least one does, so the script
can be wired into CI as a gate. INVALID entries do not fail the build -- turning
"unverifiable" into "broken" would push authors to delete citations they cannot
prove, which is the opposite of what an audit should encourage.

Dependencies: standard library only. BibTeX is parsed by the small scanner at
the bottom of this file rather than by ``pybtex``, because the audit is meant to
run in a fresh checkout with nothing installed, and because an entry this script
cannot parse is itself worth seeing.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

try:  # Running from the source tree.
    from audit_sidecar.citation_witness import CitationWitness, normalise_doi
    from audit_sidecar.tools.citation_audit import CitationAuditProxy, CitationVerdict
except ImportError:  # pragma: no cover - only when the package is not importable
    sys.stderr.write(
        "error: cannot import audit_sidecar; run this script from the repository "
        "root (or pip install -e .)\n"
    )
    raise

#: Fields that can carry an arXiv identifier, in the order they are trusted.
#: ``eprint`` is only used when the entry says the archive is arXiv, because
#: ``eprint`` is also used for IACR and other archives whose ids look different.
_ARXIV_FIELDS = ("arxiv", "arxiv_id", "arxivid", "eprint")
_DOI_FIELDS = ("doi",)

_ARXIV_IN_URL = re.compile(
    r"arxiv\.org/(?:abs|pdf)/([A-Za-z\-]+(?:\.[A-Z]{2})?/\d{7}|\d{4}\.\d{4,5})"
)
_DOI_IN_URL = re.compile(r"doi\.org/(10\.[^\s{}]+)", re.IGNORECASE)


# ---------------------------------------------------------------------------
# BibTeX model
# ---------------------------------------------------------------------------

@dataclass
class BibEntry:
    """One parsed entry, with the fields needed for the audit already located."""

    entry_type: str
    key: str
    fields: Dict[str, str] = field(default_factory=dict)
    line: int = 0

    @property
    def title(self) -> str:
        return self.fields.get("title", "")

    @property
    def authors(self) -> List[str]:
        raw = self.fields.get("author", "")
        return [part.strip() for part in raw.split(" and ") if part.strip()]

    @property
    def first_author_surname(self) -> Optional[str]:
        """The first author's surname, best effort.

        Handles both ``Wolf, Clifford`` and ``Clifford Wolf``. Only used as an
        optional cross-check, so returning ``None`` when unsure is better than
        inventing a surname and failing the entry against it.
        """
        if not self.authors:
            return None
        first = self.authors[0]
        if "," in first:
            surname = first.split(",", 1)[0].strip()
        else:
            tokens = [token for token in re.split(r"\s+", first) if token]
            surname = tokens[-1] if tokens else ""
        surname = _strip_latex(surname)
        return surname or None

    @property
    def arxiv_id(self) -> Optional[str]:
        for name in _ARXIV_FIELDS:
            value = self.fields.get(name)
            if not value:
                continue
            if name == "eprint" and not _declares_arxiv(self.fields):
                # An eprint under an unstated or non-arXiv archive cannot be
                # handed to the arXiv API: doing so would query for an id that
                # means something else and report a fabricated mismatch.
                continue
            match = re.search(r"(\d{4}\.\d{4,5}|[a-z\-]+(?:\.[A-Z]{2})?/\d{7})", value)
            if match:
                return match.group(1)
        url = self.fields.get("url", "")
        if url:
            match = _ARXIV_IN_URL.search(url)
            if match:
                return match.group(1)
        return None

    @property
    def doi(self) -> Optional[str]:
        for name in _DOI_FIELDS:
            value = self.fields.get(name)
            if value:
                return normalise_doi(_strip_latex(value))
        url = self.fields.get("url", "")
        if url:
            match = _DOI_IN_URL.search(url)
            if match:
                return normalise_doi(match.group(1))
        return None


def _declares_arxiv(fields: Dict[str, str]) -> bool:
    for name in ("archiveprefix", "eprinttype", "archive"):
        value = fields.get(name, "")
        if value and "arxiv" in value.casefold():
            return True
    return False


def _strip_latex(text: str) -> str:
    """Turn ``{Yosys} \\emph{Open}`` into ``Yosys Open`` for display purposes."""
    text = re.sub(r"\\[~'\"`^=.]\s*", "", text)
    text = re.sub(r"\\[a-zA-Z]+\s*", " ", text)
    text = text.replace("{", "").replace("}", "").replace("$", "")
    return " ".join(text.split())


# ---------------------------------------------------------------------------
# BibTeX scanner
# ---------------------------------------------------------------------------

def parse_bib(text: str) -> List[BibEntry]:
    """Parse the entries of a BibTeX document.

    Deliberately a scanner rather than a full grammar: it understands nested
    braces, quoted values and ``#`` concatenation -- which is all a reference
    list uses -- and it skips ``@comment``/``@preamble``/``@string`` instead of
    pretending they are references.
    """
    entries: List[BibEntry] = []
    index = 0
    length = len(text)
    while index < length:
        at = text.find("@", index)
        if at < 0:
            break
        header = re.match(r"@\s*([A-Za-z]+)\s*([{(])", text[at:])
        if not header:
            index = at + 1
            continue
        entry_type = header.group(1).casefold()
        open_char = header.group(2)
        close_char = "}" if open_char == "{" else ")"
        body_start = at + header.end()
        body, index = _read_until_close(text, body_start, open_char, close_char)
        if body is None:
            break  # unbalanced file: stop rather than guess at the remainder

        if entry_type in ("comment", "preamble", "string"):
            continue

        key, fields_text = _split_key(body)
        entries.append(
            BibEntry(
                entry_type=entry_type,
                key=key,
                fields=_parse_fields(fields_text),
                line=text.count("\n", 0, at) + 1,
            )
        )
    return entries


def _read_until_close(
    text: str, start: int, open_char: str, close_char: str
) -> Tuple[Optional[str], int]:
    """Read the entry body up to the matching closing delimiter."""
    depth = 1
    index = start
    in_quote = False
    while index < len(text):
        char = text[index]
        if char == "\\":
            index += 2  # an escaped brace is content, not structure
            continue
        if char == '"' and not in_quote:
            in_quote = True
        elif char == '"' and in_quote:
            in_quote = False
        elif not in_quote:
            if char == open_char:
                depth += 1
            elif char == close_char:
                depth -= 1
                if depth == 0:
                    return text[start:index], index + 1
        index += 1
    return None, len(text)


def _split_key(body: str) -> Tuple[str, str]:
    """Split ``key, field = {...}`` into the key and the fields text."""
    depth = 0
    for index, char in enumerate(body):
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
        elif char == "," and depth == 0:
            return body[:index].strip(), body[index + 1 :]
    return body.strip(), ""


def _parse_fields(body: str) -> Dict[str, str]:
    fields: Dict[str, str] = {}
    index = 0
    length = len(body)
    while index < length:
        while index < length and body[index] in " \t\r\n,":
            index += 1
        if index >= length:
            break
        match = re.match(r"([A-Za-z][\w\-:.]*)\s*=", body[index:])
        if not match:
            index = _skip_to_comma(body, index)
            continue
        name = match.group(1).casefold()
        index += match.end()
        value, index = _read_value(body, index)
        fields[name] = value
    return fields


def _skip_to_comma(body: str, index: int) -> int:
    depth = 0
    while index < len(body):
        char = body[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
        elif char == "," and depth == 0:
            return index + 1
        index += 1
    return index


def _read_value(body: str, index: int) -> Tuple[str, int]:
    """Read a field value, including ``#``-concatenated pieces."""
    parts: List[str] = []
    length = len(body)
    while index < length:
        while index < length and body[index] in " \t\r\n":
            index += 1
        if index >= length:
            break
        char = body[index]
        if char == "{":
            piece, index = _read_braced(body, index)
            parts.append(piece)
        elif char == '"':
            piece, index = _read_quoted(body, index)
            parts.append(piece)
        else:
            start = index
            while index < length and body[index] not in ",#":
                index += 1
            token = body[start:index].strip()
            if token:
                parts.append(token)
        while index < length and body[index] in " \t\r\n":
            index += 1
        if index < length and body[index] == "#":
            index += 1
            continue
        break
    return " ".join("".join(parts).split()), index


def _read_braced(body: str, index: int) -> Tuple[str, int]:
    """Read a ``{...}`` value and return its inner text."""
    depth = 1
    start = index + 1
    index = start
    while index < len(body):
        char = body[index]
        if char == "\\":
            index += 2
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return body[start:index], index + 1
        index += 1
    return body[start:], len(body)


def _read_quoted(body: str, index: int) -> Tuple[str, int]:
    start = index + 1
    index = start
    while index < len(body):
        if body[index] == "\\":
            index += 2
            continue
        if body[index] == '"':
            return body[start:index], index + 1
        index += 1
    return body[start:], len(body)


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------

@dataclass
class AuditedEntry:
    entry: BibEntry
    identifier: str
    identifier_kind: str  # "arxiv", "doi" or ""
    record: CitationVerdict


def audit_entries(
    entries: Sequence[BibEntry],
    proxy: CitationAuditProxy,
) -> List[AuditedEntry]:
    """Audit every entry, in file order, without ever raising for a bad claim."""
    results: List[AuditedEntry] = []
    for entry in entries:
        arxiv_id = entry.arxiv_id
        doi = entry.doi
        kind = "arxiv" if arxiv_id else ("doi" if doi else "")
        identifier = arxiv_id or doi or ""
        record = proxy.validate_citation(
            doi=doi,
            arxiv_id=arxiv_id,
            expected_title=entry.title or None,
            expected_author=entry.first_author_surname,
        )
        results.append(
            AuditedEntry(entry=entry, identifier=identifier, identifier_kind=kind, record=record)
        )
    return results


def _format_entry(result: AuditedEntry) -> str:
    entry = result.entry
    label = "{}:{}".format(result.identifier_kind or "none", result.identifier or "-")
    title = _strip_latex(entry.title) or "(no title)"
    if len(title) > 74:
        title = title[:71] + "..."
    detail = result.record.detail or result.record.reason
    return "  {:<32} {:<42} {:<13} {}\n      {}\n      {}".format(
        entry.key,
        label,
        entry.entry_type,
        title,
        result.record.verdict.value,
        detail,
    )


def _report(path: str, results: Sequence[AuditedEntry]) -> str:
    passed = [r for r in results if r.record.verdict.value == "PASS"]
    failed = [r for r in results if r.record.verdict.value == "FAIL"]
    invalid = [r for r in results if r.record.verdict.value == "INVALID"]

    lines: List[str] = []
    lines.append("=" * 78)
    lines.append("citation audit: {}".format(path))
    lines.append(
        "entries: {}   PASS: {}   FAIL: {}   INVALID: {}".format(
            len(results), len(passed), len(failed), len(invalid)
        )
    )
    lines.append("=" * 78)

    lines.append("")
    lines.append("PASS -- witness confirms the claim ({})".format(len(passed)))
    lines.extend(_format_entry(r) for r in passed)
    if not passed:
        lines.append("  (none)")

    lines.append("")
    lines.append("FAIL -- witness contradicts the claim ({})".format(len(failed)))
    lines.extend(_format_entry(r) for r in failed)
    if not failed:
        lines.append("  (none)")

    lines.append("")
    lines.append("INVALID -- no witness could be obtained ({})".format(len(invalid)))
    if not invalid:
        lines.append("  (none)")
    else:
        for result in invalid:
            cause = str(result.record.witness.get("cause") or "")
            explained = {
                "no_identifier": "no DOI and no arXiv id",
                "no_expected_title": "entry has no title to compare",
                "not_found": "identifier not known to the agency",
                "unavailable": "agency unreachable -- retry",
                "malformed_identifier": "identifier malformed",
                "titleless_record": "record carries no title",
            }.get(cause, cause or "unwitnessed")
            lines.append(
                "  {:<32} {:<42} {:<13} {}".format(
                    result.entry.key,
                    "{}:{}".format(
                        result.identifier_kind or "none", result.identifier or "-"
                    ),
                    result.entry.entry_type,
                    explained,
                )
            )
    return "\n".join(lines)


def _to_json(path: str, results: Sequence[AuditedEntry]) -> str:
    return json.dumps(
        {
            "file": path,
            "entries": [
                {
                    "key": r.entry.key,
                    "type": r.entry.entry_type,
                    "title": _strip_latex(r.entry.title),
                    "identifier": r.identifier,
                    "identifier_kind": r.identifier_kind,
                    "verdict": r.record.verdict.value,
                    "reason": r.record.reason,
                    "cause": r.record.witness.get("cause", ""),
                    "actual_title": r.record.witness.get("actual_title", ""),
                    "agency": r.record.agency,
                }
                for r in results
            ],
        },
        indent=2,
        ensure_ascii=False,
    )


def _parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Audit BibTeX entries against arXiv and CrossRef witnesses.",
        epilog=(
            "Exit status: 0 when no entry FAILs, 1 when at least one does. "
            "INVALID entries never fail the build -- see the module docstring."
        ),
    )
    parser.add_argument("bib", nargs="+", help="BibTeX file(s) to audit")
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit machine-readable JSON instead of the grouped report",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=20.0,
        help="per-request timeout in seconds (default: 20)",
    )
    parser.add_argument(
        "--min-interval",
        type=float,
        default=None,
        help=(
            "override the per-host request spacing in seconds; the default "
            "spaces arXiv at 3s and CrossRef at 1s to avoid throttling"
        ),
    )
    parser.add_argument(
        "--only",
        action="append",
        default=None,
        metavar="KEY",
        help="audit only the named entry (repeatable)",
    )
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    options = _parse_args(argv)

    interval: Optional[Dict[str, float]] = None
    if options.min_interval is not None:
        interval = {
            "export.arxiv.org": options.min_interval,
            "api.crossref.org": options.min_interval,
        }
    witness = CitationWitness(timeout=options.timeout, min_interval=interval)
    proxy = CitationAuditProxy(witness=witness)

    exit_code = 0
    for path in options.bib:
        try:
            with open(path, "rb") as handle:
                text = handle.read().decode("utf-8", errors="replace")
        except OSError as problem:
            sys.stderr.write("error: cannot read {}: {}\n".format(path, problem))
            exit_code = 2
            continue

        entries = parse_bib(text)
        if options.only:
            wanted = set(options.only)
            entries = [entry for entry in entries if entry.key in wanted]
        if not entries:
            sys.stderr.write("warning: {} contains no entries\n".format(path))
            continue

        results = audit_entries(entries, proxy)
        if options.json:
            print(_to_json(path, results))
        else:
            print(_report(path, results))
            print()
        if any(result.record.verdict.value == "FAIL" for result in results):
            exit_code = 1
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
