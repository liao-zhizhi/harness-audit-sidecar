#!/usr/bin/env bash
# Build, verify, and publish the sidecar to PyPI -- in that order, with the
# verification refusing to be skipped.
#
# Written because the two ways this goes wrong are both silent:
#
#   1. `twine upload dist/*` uploads whatever is in `dist/`, and `dist/` here
#      still held wheels built under the *old* distribution name. A stale
#      filename uploads fine and no one notices that `pip install
#      harness-audit-sidecar` still installs an older build -- or that the
#      release page has two names in it.
#   2. The build succeeds while the *artifact* is broken: package data missing,
#      entry point not registered, `requires-python` resolved to something the
#      caller's interpreter is not. `python -m build` says nothing about any of
#      that. Only installing the wheel in an empty venv does.
#
# Usage:
#   scripts/publish_pypi.sh                 # build + verify, print the upload cmd
#   scripts/publish_pypi.sh --upload        # ...and upload to PyPI
#   scripts/publish_pypi.sh --upload --test # ...to TestPyPI instead
#
# Credentials come from the environment, never from this file:
#   export TWINE_USERNAME=__token__
#   export TWINE_PASSWORD=pypi-...        # a scoped API token, not the password

set -euo pipefail

NAME="harness-audit-sidecar"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$REPO/.pypi-build"
DO_UPLOAD=0
TEST_INDEX=0
ALLOW_DIRTY=0

for arg in "$@"; do
  case "$arg" in
    --upload) DO_UPLOAD=1 ;;
    --test)   TEST_INDEX=1 ;;
    --allow-dirty) ALLOW_DIRTY=1 ;;
    *) echo "unknown argument: $arg" >&2; exit 2 ;;
  esac
done

cd "$REPO"

#: Which index this run is talking about. The existence check and every message
#: have to name the same one: telling a TestPyPI rehearsal "already on PyPI" is
#: the kind of wrong sentence that sends someone off to bump a version that was
#: never published.
if [[ $TEST_INDEX -eq 1 ]]; then
  INDEX_NAME="TestPyPI"
  INDEX_ROOT="https://test.pypi.org"
else
  INDEX_NAME="PyPI"
  INDEX_ROOT="https://pypi.org"
fi

# 1. Version, read from the one place that decides it. The pyproject value and
#    `audit_sidecar.__version__` disagreeing is how a wheel gets published as
#    0.10.1 while `audit_sidecar.__version__` still reports 0.10.0.
VERSION=$(python3 - <<'PY'
import re, pathlib
text = pathlib.Path("pyproject.toml").read_text(encoding="utf-8")
print(re.search(r'^version\s*=\s*"([^"]+)"', text, re.M).group(1))
PY
)
MODULE_VERSION=$(python3 -c "import audit_sidecar; print(audit_sidecar.__version__)")
if [[ "$VERSION" != "$MODULE_VERSION" ]]; then
  echo "FATAL: pyproject says $VERSION, audit_sidecar.__version__ says $MODULE_VERSION" >&2
  exit 1
fi
echo "== $NAME $VERSION =="

# 2. Refuse to publish a version that already exists. PyPI rejects a duplicate
#    upload anyway, but with a wall of HTTP that reads like an auth failure --
#    and the real problem is that the caller forgot to bump.
if curl -sf --max-time 10 "$INDEX_ROOT/pypi/$NAME/$VERSION/json" >/dev/null; then
  echo "FATAL: $NAME $VERSION is already on $INDEX_NAME; bump the version first" >&2
  exit 1
fi

# 2b. An artifact built from a dirty tree corresponds to no commit, so the sdist
#     cannot be reproduced from the tag it claims to be. That is the same defect
#     as a verdict with no witness -- a claim about source that nobody can check
#     -- so an upload from a dirty tree is refused rather than warned about.
#     Build/verify on a dirty tree is fine: it happens while iterating.
if [[ $DO_UPLOAD -eq 1 && $ALLOW_DIRTY -eq 0 ]]; then
  if [[ -n "$(git status --porcelain 2>/dev/null)" ]]; then
    echo "FATAL: working tree is dirty; the sdist will not match any commit." >&2
    echo "       commit first, or pass --allow-dirty if you mean it." >&2
    exit 1
  fi
fi

# 2c. Credentials, checked before the build rather than after it. Every mistake
#     here otherwise surfaces as a generic 403 that reads like "wrong account":
#     an account password, a token with a trailing newline, or -- the one that
#     actually happened -- a documentation placeholder pasted back verbatim
#     (`pypi-你的token`), which passes a bare `pypi-*` glob and is only rejected
#     90 seconds later by the upload endpoint.
#
#     PyPI tokens are macaroons serialised as base64. The first bytes are a
#     fixed header, so every genuine token starts `pypi-AgEIcHlwaS5vcmc`
#     (`b"\x02\x01\x08" + "pypi.org"` for index.pypi.org, and the same fixed
#     prefix for test.pypi.org). Checking that shape is not a guess about
#     validity -- it cannot tell a revoked token from a live one -- it only
#     rejects strings that were never tokens to begin with, which is exactly the
#     class of mistake that is invisible until the very end of a release.
if [[ $DO_UPLOAD -eq 1 ]]; then
  [[ "${TWINE_USERNAME:-}" == "__token__" ]] \
    || { echo "FATAL: TWINE_USERNAME must be __token__ (PyPI identifies API tokens by that literal user)" >&2; exit 1; }

  pw="${TWINE_PASSWORD:-}"
  if [[ "$pw" != pypi-* ]]; then
    echo "FATAL: TWINE_PASSWORD must be an API token (starts with 'pypi-'). An account password cannot upload, and a 2FA account cannot upload without a token at all." >&2
    exit 1
  fi
  if [[ "$pw" != pypi-AgEI* || ${#pw} -lt 60 ]]; then
    echo "FATAL: TWINE_PASSWORD does not look like a PyPI token." >&2
    echo "       Real tokens start 'pypi-AgEIcHlwaS5vcmc' and are 150+ chars." >&2
    echo "       What you pasted is ${#pw} chars and starts '${pw:0:16}...'." >&2
    echo "       If that is a placeholder from documentation ('pypi-你的token')," >&2
    echo "       no upload happened and no upload will happen: create a real token" >&2
    echo "       at https://pypi.org/manage/account/token/ and export it." >&2
    exit 1
  fi
  # The newline case gets its own sentence: the token is valid, the export is
  # not, and "invalid token" would send someone to regenerate a working one.
  if [[ "$pw" == *[[:space:]]* ]]; then
    echo "FATAL: TWINE_PASSWORD contains whitespace (a trailing newline from a copy-paste)." >&2
    echo "       Re-export it without the line break: the token itself is fine." >&2
    exit 1
  fi
fi

# 3. Build into a directory that is emptied first. Never `dist/`: it is
#    gitignored, so it survives across releases and across renames.
rm -rf "$OUT"
python3 -m venv "$OUT/env" >/dev/null
"$OUT/env/bin/pip" install -q --upgrade pip build twine >/dev/null
"$OUT/env/bin/python" -m build --sdist --wheel --outdir "$OUT/dist" . >/dev/null
echo "   built: $(ls "$OUT/dist" | tr '\n' ' ')"

# 4. Exactly two artifacts, and both named for this version. A leftover wheel
#    from another version in the same upload is the failure in the header.
ARTIFACTS=$(ls "$OUT/dist")
[[ $(wc -w <<<"$ARTIFACTS") -eq 2 ]] \
  || { echo "FATAL: expected 2 artifacts, got: $ARTIFACTS" >&2; exit 1; }
grep -q "${VERSION//./\\.}" <<<"$ARTIFACTS" \
  || { echo "FATAL: artifacts do not carry version $VERSION: $ARTIFACTS" >&2; exit 1; }

# 5. twine check: the README must render and the metadata must be legal, because
#    PyPI renders it once and never re-checks.
"$OUT/env/bin/twine" check "$OUT/dist"/*

# 6. The check that matters: install the wheel into an interpreter that has
#    nothing else, and use it the way the landing page tells people to.
WHEEL=$(ls "$OUT/dist"/*.whl)
python3 -m venv "$OUT/probe" >/dev/null
"$OUT/probe/bin/pip" install -q "$WHEEL"
"$OUT/probe/bin/harness-audit-server" --help >/dev/null \
  || { echo "FATAL: harness-audit-server does not run after a clean install" >&2; exit 1; }
"$OUT/probe/bin/python" - "$NAME" "$VERSION" <<'PY' \
  || { echo "FATAL: installed metadata or bundled rules are wrong" >&2; exit 1; }
import importlib.metadata as md, pathlib, sys, audit_sidecar
name, version = sys.argv[1], sys.argv[2]
dist = md.distribution(name)
assert dist.version == version, (dist.version, version)
rules = pathlib.Path(audit_sidecar.__file__).parent / "rules"
yamls = sorted(p.name for p in rules.glob("*.yaml"))
assert yamls, "no bundled rules: a pip install would enforce nothing"
print("   clean install: {} {} / rules {} / script ok".format(name, version, yamls))
PY

# 7. Upload, or print the command. The printed form is what a human runs when
#    they want to watch the upload themselves. Credentials were validated in 2c,
#    before the build, so this branch only has to name what it is about to do.
if [[ $DO_UPLOAD -eq 1 ]]; then
  # PyPI exposes no API to read a token's scope back, so this cannot be verified
  # from here -- only decided when the token is created. Say that out loud rather
  # than implying the check happened: a script that prints "scoped" without
  # looking is the exact class of claim this project is about.
  cat <<'NOTE'
   token: PyPI cannot report a token's scope back to a client, so this run
          cannot confirm it. Create it at pypi.org/manage/account/token/ with
          "Scope: Project" limited to harness-audit-sidecar, not "Entire
          account". An account-wide token can publish, delete, or add a
          collaborator to every project you own.
NOTE

  TARGET=()
  [[ $TEST_INDEX -eq 1 ]] && TARGET=(--repository testpypi)
  "$OUT/env/bin/twine" upload "${TARGET[@]}" "$OUT/dist"/*

  # The witness. Until this line existed, "the script exited 0" was the evidence
  # that a release happened -- and that is the exact failure this project is
  # about: a success claim with nothing independent behind it. It is not
  # hypothetical. A release was reported as done off the back of this script's
  # exit status while PyPI still answered 404 for the name, because the upload
  # had been rejected and the build output above the error looked like success.
  #
  # So the script now ends on the same check the landing page's deploy uses:
  # ask the index. A version that is not there is a failed release, whatever
  # twine's exit code said. Propagation is seconds; the poll is generous because
  # a false negative here would be a false alarm on a *finished* release.
  echo "   confirming with $INDEX_NAME (the script's own exit code is not evidence)..."
  CONFIRMED=0
  for attempt in $(seq 1 20); do
    if curl -sf --max-time 10 "$INDEX_ROOT/pypi/$NAME/$VERSION/json" >/dev/null 2>&1; then
      CONFIRMED=1; break
    fi
    sleep 3
  done
  if [[ $CONFIRMED -ne 1 ]]; then
    echo "FATAL: twine reported success but $INDEX_NAME still has no $NAME $VERSION." >&2
    echo "       Treat the release as NOT done. Check for a rejected upload" >&2
    echo "       (email unverified, token revoked mid-run, project name held by" >&2
    echo "       someone else) and look at the twine output above." >&2
    exit 1
  fi
  echo "   confirmed on $INDEX_NAME: $NAME $VERSION"
  echo "   uploaded to $INDEX_NAME: $NAME $VERSION"
else
  echo
  echo "verified but NOT uploaded. To publish to $INDEX_NAME:"
  [[ $TEST_INDEX -eq 1 ]] && echo "  scripts/publish_pypi.sh --upload --test" \
                          || echo "  scripts/publish_pypi.sh --upload"
  echo
  echo "Local token (works today):  TWINE_USERNAME=__token__ TWINE_PASSWORD=pypi-... (+ --test to rehearse)"
  echo "Trusted Publishing (recommended, no stored secret): push a v* tag, or run"
  echo "  the 'release' workflow from the Actions tab. Needs a trusted publisher on"
  echo "  $INDEX_NAME first -- for a first upload that is a *pending* publisher."
fi
