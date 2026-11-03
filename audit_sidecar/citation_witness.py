"""Physical witnesses for bibliographic claims.

A bibliography is a set of claims about the world: *this* identifier names
*this* paper, by *these* authors. Until someone resolves the identifier, every
one of those claims is exactly the kind of assertion this sidecar exists to
distrust -- consistent with the manuscript, plausible in shape, and never
checked against anything outside itself. The `FCR` metric measures the same
defect one layer down: an agent says "done", and nothing independently confirms
it. Here the agent is a `.bib` file and the claim is a citation.

So this module collects a **physical witness**: it asks a registration agency
what record an identifier actually resolves to, and compares that record with
what the entry says it should be.

Three agencies are consulted, in this order:

    arXiv      https://export.arxiv.org/api/query?id_list=<id>   (Atom XML)
    CrossRef   https://api.crossref.org/works/<doi>              (JSON)

The division of labour matters. arXiv knows preprints; CrossRef knows anything
with a registered DOI, which is where published venue papers land. A DOI that
CrossRef has never heard of is not thereby fake -- it may be a DataCite DOI, or
registered with a different agency -- so "not found" is reported as INVALID, not
as FAIL. Only a *record that exists and disagrees* is a FAIL. That distinction
is the whole point: absence of a witness is not evidence of fabrication, and a
contradicting witness is not merely an absence.

What this cannot do
-------------------
A witness is only as good as the identifier. A live web page with no DOI and no
arXiv id has no witness available at all, so entries like a vendor blog post or
a GitHub repository come back INVALID. That is not a bug to be worked around:
it is the paper's own conclusion, applied to the paper's own bibliography. A
URL is not a witness unless someone can re-resolve it and get the same record.

Verdict vocabulary
------------------
`verify` returns the three-tuple ``(ok, verdict, message)``:

    (True,  "PASS",    "witness matched")
    (False, "FAIL",    "title mismatch")
    (False, "INVALID", "no witness found")

The strings are part of the contract; callers grep them. Richer diagnostics --
which agency answered, what it actually said, whether the network was the
problem -- are attached to ``last_detail`` after every call, because a bare
"INVALID" cannot tell an operator whether to fix the entry or retry later.

Network failure is deliberately NOT funnelled into FAIL. A timeout means we did
not look, and reporting "no witness" as "wrong" would be the same false
certainty this audit is meant to catch, pointed the other way.
"""

from __future__ import annotations

import json
import logging
import re
import socket
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ElementTree
from difflib import SequenceMatcher
from typing import Callable, Dict, List, Optional, Sequence, Tuple

__all__ = [
    "CitationWitness",
    "normalise_title",
    "normalise_name",
    "normalise_doi",
    "titles_match",
    "authors_match",
]

_LOG = logging.getLogger(__name__)

ARXIV_API = "https://export.arxiv.org/api/query"
CROSSREF_API = "https://api.crossref.org/works"

#: CrossRef's polite pool: requests carrying a contact address are served from a
#: better-behaved queue. It is also simply honest to say who is asking.
CONTACT = "mailto:liaozhizhi@foxmail.com"
USER_AGENT = "citation-audit/1.0 (+https://github.com/liao-zhizhi/harness-audit-sidecar; {})".format(
    CONTACT
)

#: Minimum seconds between two requests to the same host. arXiv throttles
#: aggressively -- a burst of entries in one `.bib` will otherwise return
#: 429/503 for the later ones, and every one of those would be recorded as
#: INVALID for a reason that has nothing to do with the entry.
DEFAULT_MIN_INTERVAL = {"export.arxiv.org": 3.0, "api.crossref.org": 1.0}

#: A 5xx or a throttle is retried; a 404 is not, because it is an answer.
RETRY_STATUS = frozenset({429, 500, 502, 503, 504})
MAX_ATTEMPTS = 3

#: Fuzzy match threshold on normalised titles. 0.90 accepts the differences that
#: genuinely occur in registration records -- subtitle punctuation, capitalised
#: prepositions, "&" for "and" -- while still rejecting a different paper.
TITLE_MATCH_RATIO = 0.90

_ATOM = "{http://www.w3.org/2005/Atom}"


# ---------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------

def normalise_title(text: str) -> str:
    """Reduce a title to a comparable form.

    Removes LaTeX markup (the braces that protect capitalisation in a `.bib`
    title are markup, not content), case, accents and every punctuation mark.
    What survives is the sequence of words, which is the only part two
    independent records are guaranteed to agree on.
    """
    if not text:
        return ""
    # \emph{x}, \&, and friends: drop the command, keep its argument.
    text = re.sub(r"\\[a-zA-Z]+\s*", " ", text)
    # Accent commands ({\~a}, \'e, \"o) are a backslash and a mark, not a word:
    # dropping the command folds "Jo{\~a}o" to "Joao" the same way NFKD folds
    # a precomposed "João".
    text = re.sub(r"\\[~'\"`^=.]\s*", "", text)
    text = text.replace("\\&", " and ").replace("&", " and ")
    text = text.replace("\\%", "%")
    text = text.replace("{", "").replace("}", "").replace("$", "")
    # NFKD splits accented letters; dropping combining marks then folds them.
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.casefold()
    # \W keeps letters and digits in any script, so a CJK title is not erased.
    text = re.sub(r"[^\w]+", " ", text, flags=re.UNICODE)
    text = text.replace("_", " ")
    return " ".join(text.split())


def normalise_name(text: str) -> str:
    """Reduce an author name to a comparable form, accent- and case-folded."""
    if not text:
        return ""
    text = re.sub(r"\\[~'\"`^=.]\s*", "", text)
    text = re.sub(r"\\[a-zA-Z]+\s*", " ", text)
    text = text.replace("{", "").replace("}", "")
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.casefold()
    text = re.sub(r"[^\w]+", " ", text, flags=re.UNICODE)
    return " ".join(text.split())


def titles_match(expected: str, actual: str) -> Tuple[bool, float]:
    """Do two titles name the same work?

    Returns ``(matched, score)`` so callers can report *how* close a rejection
    was. Three rules, in order:

    1. identical after normalisation -- the common case;
    2. one is a prefix of the other -- a record that omits a subtitle, which
       registration agencies do constantly;
    3. a character-level ratio above :data:`TITLE_MATCH_RATIO` -- punctuation
       and word-order noise inside an otherwise identical title.

    A short expected title is not prefix-matched: "Yosys" is a prefix of
    "Yosys Open Synthesis Suite" but also of a dozen unrelated papers, so the
    rule only applies when the shorter side has at least three words.
    """
    left, right = normalise_title(expected), normalise_title(actual)
    if not left or not right:
        return False, 0.0
    if left == right:
        return True, 1.0

    shorter, longer = sorted((left, right), key=len)
    if len(shorter.split()) >= 3 and longer.startswith(shorter):
        return True, 1.0

    ratio = SequenceMatcher(None, left, right).ratio()
    return ratio >= TITLE_MATCH_RATIO, ratio


def authors_match(expected: str, actual: Sequence[str]) -> bool:
    """Is ``expected`` one of the authors in ``actual``?

    Only the surname is compared, because given names are abbreviated
    inconsistently ("C." versus "Clifford"), reordered by some agencies, and
    transliterated by others. An entry that names the right surname is not a
    mismatch worth failing over; one that names a different person entirely is.
    """
    wanted = normalise_name(expected)
    if not wanted:
        return True  # nothing claimed, nothing to contradict
    surname = wanted.split()[-1]
    for name in actual:
        candidate = normalise_name(name)
        if not candidate:
            continue
        if candidate == wanted or surname in candidate.split():
            return True
    return False


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------

FetchResult = Tuple[int, bytes]


class CitationWitness:
    """Resolve identifiers to records and compare them with an entry's claims.

    :param fetch: injection point for tests. Takes a URL and returns
        ``(status, body)``; raising :class:`OSError` means the request itself
        failed. Defaults to :meth:`_http_get`.
    :param timeout: per-request timeout in seconds.
    :param min_interval: override for the per-host spacing. Tests pass ``0``.
    :param sleep: injection point for the delay, so tests need not actually
        wait. Defaults to :func:`time.sleep`.
    """

    def __init__(
        self,
        fetch: Optional[Callable[[str], FetchResult]] = None,
        *,
        timeout: float = 20.0,
        min_interval: Optional[Dict[str, float]] = None,
        sleep: Optional[Callable[[float], None]] = None,
    ) -> None:
        self._fetch = fetch if fetch is not None else self._http_get
        self._timeout = timeout
        self._min_interval = (
            dict(DEFAULT_MIN_INTERVAL) if min_interval is None else dict(min_interval)
        )
        self._sleep = sleep if sleep is not None else time.sleep
        self._last_request: Dict[str, float] = {}
        #: Filled after every ``verify`` call. Never raises, always a dict.
        self.last_detail: Dict[str, object] = {}

    # -- public API ----------------------------------------------------

    def verify(
        self,
        doi: Optional[str] = None,
        arxiv_id: Optional[str] = None,
        expected_title: Optional[str] = None,
        expected_author: Optional[str] = None,
    ) -> Tuple[bool, str, str]:
        """Check one bibliographic claim against a registration agency.

        ``arxiv_id`` is tried first when both are given: an arXiv record carries
        a full author list and the preprint title, which is what an entry with
        an ``eprint`` field is actually claiming. A DOI alongside it is usually
        the published version of the same work.

        Returns ``(ok, verdict, message)`` -- see the module docstring.
        """
        self.last_detail = {
            "doi": doi or "",
            "arxiv_id": arxiv_id or "",
            "expected_title": expected_title or "",
            "agency": "",
            "actual_title": "",
            "actual_authors": [],
            "cause": "",
        }

        if not (doi or arxiv_id):
            self.last_detail["cause"] = "no_identifier"
            return False, "INVALID", "no witness found"

        if not expected_title:
            # Nothing to compare against means nothing was checked. Returning
            # PASS here would let an entry buy a green tick by omitting its own
            # title, which is precisely the loophole this module closes.
            self.last_detail["cause"] = "no_expected_title"
            return False, "INVALID", "no witness found"

        if arxiv_id:
            result = self._verify_arxiv(arxiv_id, expected_title, expected_author)
            if result is not None:
                return result
            # An arXiv id that did not resolve is recorded, but a DOI on the
            # same entry still deserves its chance: the preprint may have been
            # withdrawn while the published record stands.
            if not doi:
                return (
                    False,
                    "INVALID",
                    "no witness found: arXiv id did not resolve",
                )

        return self._verify_crossref(doi, expected_title, expected_author)

    # -- arXiv ---------------------------------------------------------

    def _verify_arxiv(
        self,
        arxiv_id: str,
        expected_title: str,
        expected_author: Optional[str],
    ) -> Optional[Tuple[bool, str, str]]:
        identifier = _normalise_arxiv_id(arxiv_id)
        if not identifier:
            self.last_detail["cause"] = "malformed_identifier"
            return None

        url = "{}?id_list={}&max_results=1".format(
            ARXIV_API, urllib.parse.quote(identifier, safe=".")
        )
        status, body = self._request(url)
        self.last_detail["agency"] = "arxiv"

        if status != 200:
            # 404 from the arXiv API is meaningless (it answers 200 with an
            # empty feed), so anything non-200 is a transport problem.
            self.last_detail["cause"] = "unavailable" if status in (0, -1) else "http_{}".format(status)
            _LOG.warning("arXiv lookup for %s failed: status %s", identifier, status)
            return None

        try:
            title, authors = _parse_arxiv(body)
        except (ElementTree.ParseError, ValueError) as problem:
            self.last_detail["cause"] = "unparseable"
            _LOG.warning("arXiv response for %s unparseable: %s", identifier, problem)
            return None

        if not title:
            # A well-formed feed with no entry: the id does not name a paper.
            self.last_detail["cause"] = "not_found"
            return False, "INVALID", "no witness found"

        self.last_detail["actual_title"] = title
        self.last_detail["actual_authors"] = list(authors)
        return self._compare(title, authors, expected_title, expected_author, "arxiv")

    # -- CrossRef ------------------------------------------------------

    def _verify_crossref(
        self,
        doi: str,
        expected_title: str,
        expected_author: Optional[str],
    ) -> Tuple[bool, str, str]:
        identifier = _normalise_doi(doi)
        if not identifier:
            self.last_detail["cause"] = "malformed_identifier"
            return False, "INVALID", "no witness found: malformed DOI"

        url = "{}/{}?{}".format(
            CROSSREF_API,
            urllib.parse.quote(identifier, safe="/"),
            CONTACT,
        )
        status, body = self._request(url)
        self.last_detail["agency"] = "crossref"

        if status == 404:
            # CrossRef answered, and the answer is "no such DOI". Still INVALID
            # rather than FAIL: the DOI may belong to another registration
            # agency, and "this agency cannot witness it" is not "the claim is
            # false". A registered record that *disagrees* is the FAIL case.
            self.last_detail["cause"] = "not_found"
            return False, "INVALID", "no witness found"

        if status != 200:
            self.last_detail["cause"] = "unavailable" if status in (0, -1) else "http_{}".format(status)
            _LOG.warning("CrossRef lookup for %s failed: status %s", identifier, status)
            if status in (0, -1):
                return False, "INVALID", "no witness found: witness unavailable"
            return False, "INVALID", "no witness found: witness unavailable (HTTP {})".format(
                status
            )

        try:
            payload = json.loads(body.decode("utf-8"))
            message = payload["message"]
            titles = message.get("title") or []
            title = " ".join(str(titles[0]).split()) if titles else ""
            authors = [
                " ".join(
                    part for part in (entry.get("given"), entry.get("family")) if part
                )
                for entry in message.get("author") or []
            ]
        except (ValueError, KeyError, TypeError, UnicodeDecodeError) as problem:
            self.last_detail["cause"] = "unparseable"
            _LOG.warning("CrossRef response for %s unparseable: %s", identifier, problem)
            return False, "INVALID", "no witness found: unparseable record"

        if not title:
            # Registered but titleless: a dataset landing page, say. It cannot
            # witness a title claim, so it witnesses nothing here.
            self.last_detail["cause"] = "titleless_record"
            return False, "INVALID", "no witness found: record carries no title"

        self.last_detail["actual_title"] = title
        self.last_detail["actual_authors"] = authors
        return self._compare(title, authors, expected_title, expected_author, "crossref")

    # -- shared --------------------------------------------------------

    def _compare(
        self,
        actual_title: str,
        actual_authors: Sequence[str],
        expected_title: str,
        expected_author: Optional[str],
        agency: str,
    ) -> Tuple[bool, str, str]:
        matched, score = titles_match(expected_title, actual_title)
        self.last_detail["title_score"] = round(score, 4)
        if not matched:
            self.last_detail["cause"] = "title_mismatch"
            return (
                False,
                "FAIL",
                "title mismatch: {} says {!r}".format(agency, actual_title),
            )
        if expected_author and not authors_match(expected_author, actual_authors):
            self.last_detail["cause"] = "author_mismatch"
            return (
                False,
                "FAIL",
                "author mismatch: {} lists {}".format(
                    agency, ", ".join(actual_authors) or "no authors"
                ),
            )
        self.last_detail["cause"] = "matched"
        return True, "PASS", "witness matched"

    def _request(self, url: str) -> Tuple[int, bytes]:
        """Fetch with throttling and bounded retries.

        Returns ``(status, body)``. A request that never completes comes back as
        status ``0`` rather than raising: the caller's job is to record "we
        could not look", and an exception escaping into a batch audit would
        abort the whole run over one unreachable host.
        """
        host = urllib.parse.urlsplit(url).hostname or ""
        for attempt in range(1, MAX_ATTEMPTS + 1):
            self._respect_interval(host)
            try:
                status, body = self._fetch(url)
            except (urllib.error.URLError, socket.timeout, TimeoutError, OSError) as problem:
                _LOG.warning("request to %s failed (%d/%d): %s", host, attempt, MAX_ATTEMPTS, problem)
                if attempt == MAX_ATTEMPTS:
                    return 0, b""
                self._sleep(min(2.0 * attempt, 5.0))
                continue

            if status in RETRY_STATUS and attempt < MAX_ATTEMPTS:
                # arXiv's throttling is documented as temporary. Retrying once
                # or twice turns a spurious INVALID into a real verdict, which
                # matters because INVALID is indistinguishable from "no such
                # paper" in the summary counts.
                self._sleep(min(2.0 * attempt, 5.0))
                continue
            return status, body
        return 0, b""

    def _respect_interval(self, host: str) -> None:
        interval = self._min_interval.get(host, 0.0)
        if interval <= 0:
            return
        last = self._last_request.get(host)
        now = time.monotonic()
        if last is not None:
            wait = interval - (now - last)
            if wait > 0:
                self._sleep(wait)
        self._last_request[host] = time.monotonic()

    def _http_get(self, url: str) -> FetchResult:
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/atom+xml, application/json;q=0.9, */*;q=0.5",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as problem:
            # urlopen raises on 4xx/5xx. A 404 is an *answer* -- the agency has
            # nothing under this identifier -- so it must reach the caller as a
            # status, not as a transport failure. Conflating the two would turn
            # every invented DOI into "network problem" and hide the finding.
            return problem.code, problem.read() or b""


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def _parse_arxiv(body: bytes) -> Tuple[str, List[str]]:
    """Extract the title and author names from an arXiv Atom feed.

    Returns ``("", [])`` when the feed is well-formed but carries no entry,
    which is how the API reports an identifier it does not know.
    """
    root = ElementTree.fromstring(body)
    entries = root.findall("{}entry".format(_ATOM))
    if not entries:
        return "", []
    entry = entries[0]
    title_node = entry.find("{}title".format(_ATOM))
    title = " ".join((title_node.text or "").split()) if title_node is not None else ""
    authors = [
        " ".join((node.findtext("{}name".format(_ATOM)) or "").split())
        for node in entry.findall("{}author".format(_ATOM))
    ]
    return title, [name for name in authors if name]


def _normalise_arxiv_id(raw: str) -> str:
    """Accept ``2609.26751``, ``arXiv:2609.26751v2``, or a full abs URL.

    Returns ``""`` for anything that is not a plausible identifier. Guessing at
    a malformed id would send a query for the wrong paper and then report a
    title mismatch against it -- a fabricated FAIL, which is worse than an
    honest INVALID.
    """
    if not raw:
        return ""
    text = raw.strip()
    text = re.sub(r"^https?://(www\.)?arxiv\.org/(abs|pdf)/", "", text)
    text = re.sub(r"^arxiv:\s*", "", text, flags=re.IGNORECASE)
    text = text.replace(".pdf", "")
    text = re.sub(r"v\d+$", "", text)
    if re.match(r"^\d{4}\.\d{4,5}$", text):
        return text
    if re.match(r"^[a-z\-]+(\.[A-Z]{2})?/\d{7}$", text):
        return text
    return ""


def normalise_doi(raw: str) -> str:
    """Strip a resolver prefix, which is how DOIs appear in a `.bib` url field."""
    if not raw:
        return ""
    text = raw.strip()
    text = re.sub(r"^https?://(dx\.)?doi\.org/", "", text, flags=re.IGNORECASE)
    text = re.sub(r"^doi:\s*", "", text, flags=re.IGNORECASE)
    return text.strip()


#: Backwards-compatible private alias; the public name is the documented one.
_normalise_doi = normalise_doi
