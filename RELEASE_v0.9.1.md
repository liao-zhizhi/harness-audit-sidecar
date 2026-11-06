# v0.9.1 — The Licence Was Already Right; the Copyright Line Was Not

No code changed. This release makes the licence explicit in prose, aligns the
copyright holder with the rest of the family, and re-reads the version from a built
wheel rather than the working tree. The suite is unchanged at 514 tests (7 skipped).

## What was actually missing

`LICENSE` and the PEP 639 metadata already existed — `license = "MIT"`,
`license-files`, `setuptools>=77`, no licence classifier. So the packaging half of
v1.0.9's lesson did not need applying here; it was applied when this project was
packaged, and a wheel build confirms it:

```
License-Expression: MIT
License-File: LICENSE
agent_harness_audit-0.9.1.dist-info/licenses/LICENSE
```

Two things were not right:

1. **The copyright line said `liaozhizhi`,** a handle, where the sibling repository
   names a person. One name for one author, or the authorship record is only as good
   as the least specific of the files claiming it.
2. **The README's licence section was one sentence,** `Code MIT; paper text CC BY
   4.0.`, with no statement of what each licence does or what neither of them grants.
   A reader could reasonably conclude the split was arbitrary.

## The version, read back rather than trusted

`ERRATA.md` §1 records this project shipping `0.4.0` inside a `0.5.0` wheel, and the
rule that came out of it: bump `pyproject.toml`, `__version__`, and any prose that
names the number, then read all three from the artefact. This release follows that
rule, which is why the check is three lines and not one:

```
filename : agent_harness_audit-0.9.1-py3-none-any.whl
metadata : Version: 0.9.1
__init__ : __version__ = "0.9.1"
```

The README status line moved from `v0.9.0` to `v0.9.1` for the same reason — prose
that names a version is one of the three places.

## Why MIT, when the business is auditing

MIT permits commercial use, so a company can wire this sidecar into its harness
without paying anything. That is deliberate: a sidecar that cannot be embedded in
someone else's stack is not a sidecar, and an audit standard that nobody runs
enforces nothing.

What MIT does not give them is a certification. Running the sidecar produces a record
of what it witnessed — a verdict, a trust adjustment, a hash chain — and that record
is not an accreditation. Presenting it as one is a claim this licence does not make
and this project cannot back. The audit service and the open-source tool share a
judgment base; they are not the same product.

## Tests

| Suite | Before | After |
| --- | --- | --- |
| `audit_sidecar` | 514 | 514 |

Re-run because the package metadata changed, not because the code did.
