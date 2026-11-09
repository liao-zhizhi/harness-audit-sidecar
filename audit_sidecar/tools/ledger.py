"""A hash-chained, append-only ledger, and an honest account of what it proves.

The audit trace was plain JSONL. A verdict is only worth what its record is
worth, and a record anyone can edit is worth nothing; so each entry here commits
to the one before it, and editing history in the middle breaks the chain:

    entry_hash[n] = sha256(prev_hash[n] || canonical(entry[n]))

with ``prev_hash[0]`` set to 64 zeros. Rewriting entry *k* changes its hash,
which breaks the link that entry *k+1* holds, which breaks every link after it.
``verify_chain()`` reports the first index where the arithmetic stops working.

**What this does not do, stated plainly, because the alternative is a false
sense of safety.** The threat named in the request was "delete the last line to
clean oneself". A hash chain does not defend against that: the remaining prefix
is still a perfectly valid chain, and ``verify_chain()`` will happily return
PASS for it. A chain proves *that entries were not altered*, not *that none were
removed*. Three things are needed for the second claim, and this module supplies
the first two while marking the limit of the third:

1. **An anchor outside the file.** ``seal()`` writes ``{"seq", "head_hash"}`` to
   a separate path. Verification then checks that the entry at ``anchor.seq``
   still hashes to ``anchor.head_hash``; a truncated file has no such entry and
   fails with ``truncated``. Without an anchor, tail truncation is invisible --
   which is why ``MembraneGateway`` always seals, and why constructing a bare
   ledger without an anchor is documented as insecure rather than defaulted to.

2. **Storage the attacker cannot write.** The anchor is only as strong as where
   it lives. If an adversary can write the ledger *and* the anchor, they can
   rebuild both consistently. A local-only scheme cannot prevent a wholesale
   rewrite; publishing the head (a commit, another host, a read-only medium) is
   what turns the anchor into evidence.

3. **Append-only as an API, not as a filesystem property.** ``append`` never
   updates or deletes, and refuses a payload carrying reserved fields. Any
   process with write permission can still truncate the file with ordinary I/O;
   the chain detects the result, it does not prevent the act.

Zero dependencies, plain files, no database.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from typing import Any, Dict, Iterator, List, Mapping, Optional

__all__ = [
    "AppendOnlyLedger",
    "GENESIS_HASH",
    "LedgerCorruptError",
    "LedgerError",
    "RESERVED_FIELDS",
]

#: ``prev_hash`` of the first entry. Sixty-four zeros make the genesis link an
#: ordinary string rather than ``None``, so the hashing code has one case.
GENESIS_HASH = "0" * 64

#: Fields the ledger owns. A payload carrying one of these is rejected rather
#: than merged: a caller able to set ``prev_hash`` could splice in a subtrace.
RESERVED_FIELDS = frozenset({"seq", "prev_hash", "entry_hash"})

_TAIL_CHUNK = 8192


class LedgerError(Exception):
    """Base class for ledger failures."""


class LedgerCorruptError(LedgerError):
    """The existing file cannot be extended without pretending it is sound."""


def _canonical(payload: Mapping[str, Any]) -> bytes:
    """Deterministic JSON bytes for hashing.

    ``sort_keys`` and fixed separators make the digest independent of dict
    insertion order; ``ensure_ascii=False`` keeps non-ASCII text readable in the
    file; ``allow_nan=False`` refuses NaN and Infinity, which are not JSON and
    would otherwise hash to a value no other reader could reproduce.
    """
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _hash_entry(prev_hash: str, body: Mapping[str, Any]) -> str:
    digest = hashlib.sha256()
    digest.update(prev_hash.encode("ascii"))
    digest.update(b"\x00")
    digest.update(_canonical(body))
    return digest.hexdigest()


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


class AppendOnlyLedger:
    """Append-only JSONL whose entries are linked by SHA256.

    Args:
        path: the ledger file. Created eagerly, so a reader never mistakes
            "not yet written" for "nothing happened".
        anchor_path: where :meth:`seal` records the head. When set,
            :meth:`verify_chain` also checks for truncation. Leaving it ``None``
            is supported and insecure: see the module docstring.
        fsync: flush each append to the platter. Off by default because the
            tests do not need durability and fsync dominates their runtime; the
            membrane turns it on.
    """

    def __init__(
        self,
        path: Any,
        *,
        anchor_path: Any = None,
        fsync: bool = False,
    ) -> None:
        self._path = self._normalise(path, "path", required=True)
        self._anchor_path = self._normalise(anchor_path, "anchor_path", required=False)
        self._fsync = bool(fsync)
        directory = os.path.dirname(self._path) or "."
        # Create the directory *before* opening: otherwise a nested path fails
        # with ENOENT even though the caller asked for it and the constructor
        # is the only place that can create it.
        os.makedirs(directory, exist_ok=True)
        # Create eagerly and empty if absent, so "no file" and "no entries" can
        # never be confused for one another later.
        with open(self._path, "a", encoding="utf-8"):
            pass

    @staticmethod
    def _normalise(value: Any, name: str, *, required: bool) -> Optional[str]:
        if value is None:
            if required:
                raise ValueError("{} must be a path, got None".format(name))
            return None
        if isinstance(value, os.PathLike):
            value = os.fspath(value)
        if isinstance(value, bytes):
            value = value.decode()
        if not isinstance(value, str) or not value.strip():
            raise ValueError("{} must be a non-empty path, got {!r}".format(name, value))
        return os.path.abspath(value)

    # -- read-only view ---------------------------------------------------

    @property
    def path(self) -> str:
        return self._path

    @property
    def anchor_path(self) -> Optional[str]:
        return self._anchor_path

    def entries(self) -> Iterator[Dict[str, Any]]:
        """Yield each entry in order.

        Raises:
            LedgerCorruptError: a line is not JSON, or not a JSON object. A
                reader that skipped such a line would report a clean ledger for
                a damaged file, so the damage is surfaced instead.
        """
        with open(self._path, "r", encoding="utf-8") as handle:
            for number, line in enumerate(handle, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except ValueError as problem:
                    raise LedgerCorruptError(
                        "line {} is not JSON: {}".format(number, problem)
                    ) from None
                if not isinstance(entry, dict):
                    raise LedgerCorruptError(
                        "line {} is not a JSON object".format(number)
                    )
                yield entry

    def __len__(self) -> int:
        return sum(1 for _ in self.entries())

    def head(self) -> Optional[Dict[str, Any]]:
        """The last entry, or ``None`` when the ledger has none.

        Read from the tail of the file rather than from a cached value: another
        process may have appended since this object was built, and a stale head
        would be a fork in the chain.
        """
        line = self._tail_line()
        if line is None:
            return None
        try:
            entry = json.loads(line)
        except ValueError:
            raise LedgerCorruptError(
                "the last line of {} is not JSON; refusing to extend a damaged "
                "ledger".format(self._path)
            ) from None
        if not isinstance(entry, dict):
            raise LedgerCorruptError("the last line of {} is not an object".format(self._path))
        return entry

    def _tail_line(self) -> Optional[str]:
        """The last non-empty line, read without scanning the whole file."""
        try:
            size = os.path.getsize(self._path)
        except OSError:
            return None
        if size == 0:
            return None
        window = min(size, _TAIL_CHUNK)
        while True:
            with open(self._path, "rb") as handle:
                handle.seek(size - window)
                chunk = handle.read(window)
            lines = [line for line in chunk.split(b"\n") if line.strip()]
            if lines:
                last = lines[-1]
                if len(lines) > 1 or window >= size:
                    return last.decode("utf-8")
            if window >= size:
                return None
            window = min(size, window * 2)

    def entry_at(self, seq: int) -> Optional[Dict[str, Any]]:
        """The entry with this ``seq``, or ``None``. Linear, and rarely used."""
        for entry in self.entries():
            if entry.get("seq") == seq:
                return entry
        return None

    # -- writing ----------------------------------------------------------

    def append(self, payload: Mapping[str, Any], *, record_type: str = "call") -> Dict[str, Any]:
        """Append one entry and return it as written.

        Args:
            payload: the entry body. Must be JSON-serialisable and must not
                carry a reserved field.
            record_type: what kind of line this is. ``"call"`` (the default)
                means "one audited call", which is the shape ``eco_api`` reads
                as a trace; bookkeeping lines use their own type so a consumer
                can tell them apart from a verdict.

        Raises:
            TypeError: the payload is not a mapping, or is not serialisable.
            ValueError: reserved field present, or empty ``record_type``.
            LedgerCorruptError: the file's tail is unreadable, in which case
                appending would build a chain on sand.
        """
        if not isinstance(payload, Mapping):
            raise TypeError(
                "payload must be a mapping, got {!r}".format(type(payload).__name__)
            )
        if not isinstance(record_type, str) or not record_type.strip():
            raise ValueError("record_type must be a non-empty str")

        reserved = RESERVED_FIELDS.intersection(payload)
        if reserved:
            raise ValueError(
                "payload may not set reserved field(s): {}".format(
                    ", ".join(sorted(reserved))
                )
            )

        previous = self.head()
        prev_hash = previous["entry_hash"] if previous is not None else GENESIS_HASH
        seq = (previous["seq"] + 1) if previous is not None else 1

        body: Dict[str, Any] = {
            "seq": seq,
            "prev_hash": prev_hash,
            "record_type": record_type,
        }
        body.update(payload)
        entry = dict(body)
        entry["entry_hash"] = _hash_entry(prev_hash, body)

        line = json.dumps(entry, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n"
        with open(self._path, "a", encoding="utf-8") as handle:
            handle.write(line)
            handle.flush()
            if self._fsync:
                os.fsync(handle.fileno())
        return entry

    def seal(self) -> Dict[str, Any]:
        """Record the current head as the anchor. Returns the anchor document.

        Call after a batch of appends, or after every append when the threat is
        tail truncation. An empty ledger seals ``seq: 0`` and the genesis hash,
        which is what makes "the file was emptied" detectable on the next
        verification.
        """
        if self._anchor_path is None:
            raise LedgerError(
                "this ledger has no anchor_path; construct it with one to record a head"
            )
        last = self.head()
        anchor = {
            "version": 1,
            "seq": last["seq"] if last is not None else 0,
            "head_hash": last["entry_hash"] if last is not None else GENESIS_HASH,
        }
        self._write_anchor(anchor)
        return anchor

    def _write_anchor(self, anchor: Mapping[str, Any]) -> None:
        assert self._anchor_path is not None
        directory = os.path.dirname(self._anchor_path) or "."
        os.makedirs(directory, exist_ok=True)
        descriptor, handle_name = tempfile.mkstemp(
            prefix=".anchor-", suffix=".tmp", dir=directory
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(anchor, handle, sort_keys=True)
                handle.write("\n")
                handle.flush()
                if self._fsync:
                    os.fsync(handle.fileno())
            os.replace(handle_name, self._anchor_path)
            handle_name = None
        finally:
            if handle_name is not None:
                try:
                    os.unlink(handle_name)
                except OSError:
                    pass

    def read_anchor(self) -> Optional[Dict[str, Any]]:
        """The recorded anchor, ``None`` when none exists or it is unreadable."""
        if self._anchor_path is None:
            return None
        try:
            with open(self._anchor_path, "r", encoding="utf-8") as handle:
                document = json.load(handle)
        except (OSError, ValueError):
            return None
        if not isinstance(document, dict):
            return None
        return document

    # -- verification -----------------------------------------------------

    def verify_chain(self) -> Dict[str, Any]:
        """Recompute the whole chain and compare it to the anchor.

        Returns ``status`` in the lattice's own vocabulary:

        ``PASS``
            Every ``prev_hash`` links, every ``entry_hash`` recomputes, ``seq``
            runs 1..n without a gap, and (when anchored) the anchored entry
            still hashes to the anchored value.
        ``FAIL``
            The arithmetic disagrees somewhere: ``first_bad_seq`` names where.
        ``INVALID``
            There is nothing to verify -- no file, or no entries.
        """
        if not os.path.exists(self._path):
            return self._result("INVALID", 0, None, None, "no ledger file at {!r}".format(self._path))

        count = 0
        expected_prev = GENESIS_HASH
        expected_seq = 1
        head_hash: Optional[str] = None

        try:
            for entry in self.entries():
                count += 1
                seq = entry.get("seq")
                prev = entry.get("prev_hash")
                claimed = entry.get("entry_hash")

                if seq != expected_seq:
                    return self._result(
                        "FAIL",
                        count - 1,
                        head_hash,
                        expected_seq,
                        "sequence broken: expected seq {}, found {!r}".format(
                            expected_seq, seq
                        ),
                    )
                if prev != expected_prev:
                    return self._result(
                        "FAIL",
                        count - 1,
                        head_hash,
                        seq,
                        "entry {} points at prev_hash {!r}, but the entry before it "
                        "hashes to {!r}".format(seq, prev, expected_prev),
                    )
                if not isinstance(claimed, str):
                    return self._result(
                        "FAIL", count - 1, head_hash, seq,
                        "entry {} carries no entry_hash".format(seq),
                    )

                body = {key: value for key, value in entry.items() if key != "entry_hash"}
                recomputed = _hash_entry(prev, body)
                if recomputed != claimed:
                    return self._result(
                        "FAIL",
                        count - 1,
                        head_hash,
                        seq,
                        "entry {} was altered: it claims {}, but its contents "
                        "hash to {}".format(seq, claimed[:16], recomputed[:16]),
                    )

                expected_prev = claimed
                expected_seq = seq + 1
                head_hash = claimed
        except LedgerCorruptError as problem:
            return self._result("FAIL", count, head_hash, None, str(problem))

        if count == 0:
            # An anchored empty ledger is a legitimate state (nothing has
            # happened yet); an unanchored empty one is simply unverifiable.
            anchor = self.read_anchor()
            if anchor is not None and anchor.get("seq") not in (None, 0):
                return self._result(
                    "FAIL", 0, None, None,
                    "the anchor records {} entries but the ledger is empty".format(
                        anchor.get("seq")
                    ),
                )
            return self._result(
                "INVALID", 0, None, None, "the ledger has no entries to verify"
            )

        anchor = self.read_anchor()
        if self._anchor_path is not None:
            if anchor is None:
                return self._result(
                    "FAIL", count, head_hash, None,
                    "an anchor path was configured but no anchor could be read; "
                    "truncation cannot be ruled out",
                )
            anchored_seq = anchor.get("seq")
            anchored_hash = anchor.get("head_hash")
            if (
                isinstance(anchored_seq, bool)
                or not isinstance(anchored_seq, int)
                or anchored_seq < 0
                or not isinstance(anchored_hash, str)
            ):
                return self._result(
                    "FAIL", count, head_hash, None, "the anchor document is malformed"
                )
            if count < anchored_seq:
                return self._result(
                    "FAIL", count, head_hash, anchored_seq + 1,
                    "truncated: the anchor records {} entries, the file holds {}".format(
                        anchored_seq, count
                    ),
                )
            if anchored_seq > 0:
                target = self.entry_at(anchored_seq)
                if target is None or target.get("entry_hash") != anchored_hash:
                    return self._result(
                        "FAIL", anchored_seq, head_hash, anchored_seq,
                        "entry {} no longer matches the anchored head; history was "
                        "rewritten".format(anchored_seq),
                    )

        return self._result("PASS", count, head_hash, None, "chain intact")

    @staticmethod
    def _result(
        status: str,
        entries: int,
        head_hash: Optional[str],
        first_bad_seq: Optional[int],
        detail: str,
    ) -> Dict[str, Any]:
        return {
            "status": status,
            "entries": entries,
            "head_hash": head_hash,
            "first_bad_seq": first_bad_seq,
            "detail": detail,
        }

    def __repr__(self) -> str:
        return "AppendOnlyLedger({!r}, anchored={})".format(
            self._path, self._anchor_path is not None
        )
