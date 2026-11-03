# ERRATA

**False completions this project caught in itself.**

An audit tool that only audits other people is a decoration. This file records the
occasions during development and release when this project — its author and the AI
assistant working on it — claimed something that was not true, and the witness that
caught it.

Two rules govern this file:

1. **Every entry names the witness.** Not "we noticed", but *what* noticed: a registry
   readback, a test run, a resolver, a byte comparison. If an entry has no witness, it
   is a confession, not a record, and it says so.
2. **An entry whose artefact no longer exists says that too.** Provenance is stated per
   entry. Reconstructing a plausible detail to make an entry look verifiable would be
   the exact failure this file is about.

Entries are grouped by the layer of the stack where the false completion occurred.

---

## 1. Packaging — `0.4.0` inside a `0.5.0` wheel

**The Mistake.** The distribution was named `agent_harness_audit-0.5.0` while the package
it carried reported `__version__ = "0.4.0"`. `pip show` would have agreed with the
filename; `import audit_sidecar; print(__version__)` would not. Two numbers described one
artefact, and only one of them was being checked.

**How it was caught.** By reading the built artefact back instead of trusting the build.
The version in the wheel filename, the `Version:` field in its `METADATA`, and the
`__version__` string inside the shipped `__init__.py` were compared as three separate
values. The third disagreed.

**The Fix.** The three places that carry a version — `pyproject.toml`, the package's
`__version__`, and any prose that names it — are bumped together and re-read from the
built artefact, not from the working tree. The same check recurs in this project's
later releases; the `v0.8.1` bump touched all three, including the README status line,
for this reason.

**Evidence.** *No surviving artefact.* No `agent_harness_audit-0.4*` or `-0.5*` wheel or
sdist exists on this machine, and the initial `v0.5.0` commit (`c32b605`) already carries
`0.5.0` in both `pyproject.toml` and `__init__.py` — so the mismatch was corrected before
the first commit, and what remains is the practice it produced, not the bytes. This entry
rests on the development record; treat it as `INVALID` in the strict sense used by this
project — remembered, not witnessed.

---

## 2. Testing — `31 / 348` reported as `OK`

**The Mistake.** The documented command was `python3 -m unittest discover tests`. From the
repository root it runs the full suite. Run from anywhere else, it does **not** fail: it
collects **31 of 348** tests, reports 9 errors in that subset, and prints a summary. The
documented invocation depended on an *implied* start directory, so a reader in a different
working directory would believe the suite ran when four-fifths of it had not been loaded.

**How it was caught.** By running the documented command from a directory that was not the
repository root and reading the test count against the count the suite was known to have.
`Ran 31 tests` against `348` is arithmetically impossible to mistake for a pass — once
someone actually reads the number instead of the word `OK`.

**The Fix.** Both documented invocations now name the start directory and the pattern:

```bash
python3 -m unittest discover -s tests -p 'test_*.py'
```

`-s tests` states the start directory; `-p 'test_*.py'` keeps a future helper module in
`tests/` from being collected as a test. The README also states that the command must run
from the repository root, and records the `31 / 348` measurement as the reason rather than
as a footnote.

**Evidence.** Reproduced during the `v0.8.1` work: from `/tmp`, `Ran 31 tests` with 9
errors; from the repository root, `Ran 348 tests ... OK (skipped=4)`. Fixed in `577b6dd`,
released as `v0.8.1`.

---

## 3. Typesetting — the bibliography style silently drops identifiers

**The Mistake.** Entries carried `eprint` and `archivePrefix` fields, and were assumed to
therefore print an arXiv identifier to the reader. `IEEEtran.bst` 1.14 has no `eprint`
handler and no `doi` handler: both fields are read and silently discarded at typesetting
time. The `.bib` was correct, the PDF was byte-identical to a version that never contained
the identifier, and nobody reading the paper could resolve the reference.

**How it was caught.** By grepping the style file for the field it was assumed to print:

```console
$ grep -c 'doi' "$(kpsewhich IEEEtran.bst)"
0
```

Zero occurrences. A field the style never mentions cannot be printed by it. The second
witness was the PDF: after adding a printable `note`, the SHA256 changed from
`5724b84e…` to `85c22506…` — the bytes had been telling the truth the whole time.

**The Fix.** The identifier is written twice, deliberately, with the division of labour
stated in `references.bib`: `eprint` for tooling, and a `note = {{\url{…}}}` for readers.
The doubled braces are not decoration — `IEEEtran.bst`'s `format.note` lowercases the first
character of a note unless it begins with a brace, which would print `iSBN`, `hTTPS`.

**Evidence.** `grep -c 'doi' IEEEtran.bst` → `0`; the before/after PDF hashes above; the
mechanism documented in line at the head of `metabolism_paper/references.bib`.

---

## 4. References — a remembered arXiv number that pointed at the wrong paper

**The Mistake.** While adding citations, the assistant supplied arXiv `2307.03025` for
Panickssery et al., *LLM Evaluators Recognize and Favor Their Own Generations*, from
memory. The number was well-formed, plausible, and wrong. It resolves — to a different
paper.

**How it was caught.** By the witness written to catch exactly this: `CitationWitness`
resolved the identifier against the arXiv API and compared the returned title with the
title in the entry. The titles disagreed, the verdict was `FAIL`, and the resolution
returned *Style Over Substance* rather than the expected paper. A remembered identifier
that resolves is the most dangerous kind, because a resolver without a title comparison
would have marked it `PASS`.

**The Fix.** The correct identifier, `2404.13076`, was obtained by searching the arXiv API
by title rather than by recalling a number, and confirmed by the same comparison that had
rejected the first one. The rule this produced: **an identifier is looked up, never
recalled** — the tool that enforces it is the one that caught the error.

**Evidence.** The `FAIL` verdict and the returned title are recorded in this project's
development record; `2307.03025` resolves today to a different paper, so the contradiction
is re-checkable by anyone.

---

## 5. Typesetting — a Markdown link handed to `\url{}`

**The Mistake.** A URL was pasted from prose into a LaTeX argument without converting the
syntax: `\url{[text](https://example.com)}`. Markdown's link form is not a URL. The
argument contained brackets, parentheses and a label that is not part of any address, and
the build aborted rather than printing a malformed link — the failure was loud, which is
the only reason it is a footnote here rather than a hazard.

**How it was caught.** By the compiler, immediately: `xelatex` stopped on the argument.
A build that fails is a witness that cannot be argued with, which is why this entry is
included at all — the interesting failures in this file are the ones that *did not* stop
the build (entries 1, 3, 6).

**The Fix.** `\url{}` receives a bare address and nothing else. Where a label is wanted,
it goes outside the argument, and `xurl` is loaded so a long address can break at any
character — `\texttt` cannot break a 117-character URL and produces an overfull box
instead of an error.

**Evidence.** *No surviving artefact.* The failing source was corrected in place and no
copy of the broken line was kept; the compiler output that caught it is gone. What
survives is the invariant in `BUILD.md` and `fixes.lua`: code spans containing URLs are
rewritten to `\url`, and `\url` takes an address, never a link.

---

## 6. Verification — a rebuild that produced identical bytes, read as "not built"

**The Mistake.** A content witness was asked to confirm that a PDF had been rebuilt. Two
rebuilds 0.8 seconds apart produced **byte-identical** output, because pdfTeX stamps
`/CreationDate` at second resolution and nothing else in the file moved. The witness could
not distinguish "rebuilt, and the content happened to be unchanged" from "never built at
all". Both states produce the same observation; only one of them is the claim being made.

**How it was caught.** By asking what the witness would have reported if the build had not
run. When the answer is *the same evidence*, the witness does not support the claim — the
verdict is `INVALID`, not `PASS`. Two identical hashes are not proof of a rebuild; they
are the absence of a discriminating observation.

**The Fix.** The artefact is deleted **before** the build, outside the audited command:

```sh
rm -f main.pdf
latexmk -g -pdf -interaction=nonstopmode -halt-on-error main.tex
```

Deleting first changes what the witness must prove: not "the file changed", but "the file
was generated from nothing". The ambiguity is removed at its source rather than argued
with after the fact.

**Evidence.** Recorded in `metabolism_paper/DOGFOOD_AUDIT.md`, with the rebuild table
showing the `-g` flag and the pre-build deletion as a deliberate precondition.

---

## Addendum — an error made while writing this file

One entry above was initially misdiagnosed, and the mistake is worth keeping because it is
the same shape as the rest.

While preparing the `v0.8.1` fix, the assistant ran `unittest discover -s tests -t .`,
received `ImportError: Start directory is not importable`, and reported to the author that
**the README's documented command was broken**. It was not. The `ImportError` was caused by
the `-t .` flag the assistant had added itself; the README's `discover tests` works from the
repository root. The assistant had generalised from its own temporary flag to a claim about
the project, and the author was told a false thing with confidence.

It was caught by running the documented command exactly as documented. The real defect —
the `31 / 348` silent skip in entry 2 — was adjacent, real, and *not* the one being
reported. The near-miss is instructive: a plausible diagnosis reached from a partial
observation is indistinguishable, from the inside, from a correct one.

---

## What this file is for

Every entry above is a case where the artefact disagreed with the story told about it, and
the artefact won. None was caught by being careful. Each was caught by a mechanism that
compared a claim with something that could not be talked out of its answer:

| Entry | Witness |
|:--|:--|
| 1 | artefact readback (filename vs `METADATA` vs `__version__`) |
| 2 | test count vs known total |
| 3 | `grep` of the style file; PDF hash before/after |
| 4 | title comparison in `CitationWitness` |
| 5 | compiler abort |
| 6 | counterfactual: what would the witness show if the build never ran |

The pattern is the one the paper argues for. Care is not a mechanism. A witness is.
