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

for arg in "$@"; do
  case "$arg" in
    --upload) DO_UPLOAD=1 ;;
    --test)   TEST_INDEX=1 ;;
    *) echo "unknown argument: $arg" >&2; exit 2 ;;
  esac
done

cd "$REPO"

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
if curl -sf --max-time 10 "https://pypi.org/pypi/$NAME/$VERSION/json" >/dev/null; then
  echo "FATAL: $NAME $VERSION is already on PyPI; bump the version first" >&2
  exit 1
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
#    they want to watch the upload themselves.
if [[ $DO_UPLOAD -eq 1 ]]; then
  [[ -n "${TWINE_PASSWORD:-}" ]] \
    || { echo "FATAL: set TWINE_USERNAME=__token__ and TWINE_PASSWORD=<token>" >&2; exit 1; }
  TARGET=()
  [[ $TEST_INDEX -eq 1 ]] && TARGET=(--repository testpypi)
  "$OUT/env/bin/twine" upload "${TARGET[@]}" "$OUT/dist"/*
else
  echo
  echo "verified but NOT uploaded. To publish:"
  [[ $TEST_INDEX -eq 1 ]] && echo "  scripts/publish_pypi.sh --upload --test" \
                          || echo "  scripts/publish_pypi.sh --upload"
  echo "(needs TWINE_USERNAME=__token__ and TWINE_PASSWORD=pypi-...)"
fi
