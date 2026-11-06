# v0.9.2 — Patch: Standardize the Copyright Holder to 'Zhizhi Liao' for Legal Clarity

Patch: Standardize the copyright holder to 'Zhizhi Liao' for legal clarity.

No code changed. The only functional difference between this tag and `v0.9.1` is
the author string in the built artefact's metadata.

## Why this tag exists at all

`v0.9.1` was tagged before the copyright holder was standardised, so building from
that tag produces:

```
Author-email: liaozhizhi <liaozhizhi@foxmail.com>
```

while building from `master` produces:

```
Author-email: Zhizhi Liao <liaozhizhi@foxmail.com>
```

Two names for one author, depending on which ref you check out. For a project whose
own `ERRATA.md` §1 records shipping `0.4.0` inside a `0.5.0` wheel — and whose rule
since then is "read the version out of the artefact, not the tree" — shipping a tag
whose metadata disagrees with its successor is the same defect in a different field.

A one-line metadata change is still a change to the artefact, so it gets a version.

## What changed

```
pyproject.toml              authors: "liaozhizhi" -> "Zhizhi Liao" (email kept)
audit_sidecar/__init__.py   __version__ = "0.9.2"
pyproject.toml              version = "0.9.2"
README.md                   status line v0.9.1 -> v0.9.2
```

The version is bumped in the three places `ERRATA.md` §1 requires, and read back
from a wheel built at this tag rather than from the working tree.

The email is unchanged: `liaozhizhi@foxmail.com`. A handle in an address is a
routing detail; a handle in a copyright line is an ambiguity about who holds the
right. Only the latter was the problem.

## Verified from a fresh clone at the tag

```
git clone --branch v0.9.2 https://github.com/liao-zhizhi/harness-audit-sidecar
cd harness-audit-sidecar && pip wheel --no-deps --no-build-isolation .
```

Read back from the built wheel:

```
Version:       0.9.2
Author-email:  Zhizhi Liao <liaozhizhi@foxmail.com>
License-Expression: MIT
License-File:  LICENSE
```

## Tests

| Suite | Before | After |
| --- | --- | --- |
| `audit_sidecar` | 514 | 514 |
