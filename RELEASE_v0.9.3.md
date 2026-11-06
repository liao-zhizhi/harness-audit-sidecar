# v0.9.3 — Final Metadata Patch

This is the final metadata patch. No further changes to ERRATA.md will be made without a new version.

No code changed. No prose in `ERRATA.md` changed. Three version strings moved.

## Why this release exists

`v0.9.2` was cut, then `ERRATA.md` entry 8 was written and pushed to `master`. That entry
is documentation, and the rule it records is that any change produces a version — but the
entry itself did not, so the same defect appeared a third time, in the file that names it:

| Source | artefact name | `ERRATA.md` inside |
| --- | --- | --- |
| tag `v0.9.2` | `agent_harness_audit-0.9.2.tar.gz` | 13,346 bytes |
| `master` | `agent_harness_audit-0.9.2.tar.gz` | 16,736 bytes |

Same file name, two different source distributions. The wheel is unaffected — `ERRATA.md`
does not ship in it — but the sdist is exactly the path `MANIFEST.in` was written to
serve: `pip download --no-binary :all:` is supposed to yield a buildable, inspectable
source tree, and it yielded two different trees under one version number.

An entry about metadata drift, causing metadata drift, is not an irony worth keeping. It
gets a version.

## What changed

```
pyproject.toml              version = "0.9.3"
audit_sidecar/__init__.py   __version__ = "0.9.3"
README.md                   status line v0.9.2 -> v0.9.3
```

`ERRATA.md` is untouched in this commit, deliberately. Editing it here — even a typo —
would recreate the condition being fixed.

## Verified: tag and branch produce the same sdist

```
git clone --branch v0.9.3 https://github.com/liao-zhizhi/harness-audit-sidecar
git clone            https://github.com/liao-zhizhi/harness-audit-sidecar   # master
python -c "from setuptools import build_meta as b; b.build_sdist(out)"      # in each
sha256sum agent_harness_audit-0.9.3.tar.gz                                  # compare
```

Both are built from separate clones of the remote, not from the working tree that produced
the tag. Byte-level comparison is the whole point: a content comparison would pass while
the archives still differed in member order or timestamps, which is the class of difference
that makes two "identical" builds disagree later.

## Tests

| Suite | Before | After |
| --- | --- | --- |
| `audit_sidecar` | 514 | 514 |

## What "final" means here

Not that the project is finished. It means the version is now the only thing that will move
without a substantive change: `ERRATA.md` is frozen at this commit, and any future edit to
it — correction, typo, or new entry — ships as a new version, because it is included in the
sdist and therefore in what `pip download --no-binary :all:` hands a reviewer.
