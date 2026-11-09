"""``harness-audit setup``: the install step, applied instead of printed.

``init`` prints an MCP stanza and writes ``ref_roots.json``. That split exists
because a CLI that silently rewrites a client's global settings has to be
trusted more than this one is. ``setup`` is the opt-in version of that trust:
it writes, and every write is preceded by a backup and reported by path.

What it touches, and why each one is necessary:

* ``.cursor/mcp.json`` -- so Cursor launches the audit server at all. Project
  scope is preferred; an existing global file is used when there is no project
  one, rather than creating a second file that shadows it.
* ``ref_roots.json`` -- the ``@alias`` map the case library resolves through.
  Merged, never replaced: overwriting would repoint every other project's
  ``@alias`` cases at this checkout, which is a silent loss of the same kind as
  overwriting ``mcp.json``.
* one ``baseline`` -- because "this file changed" is a comparison. Without a
  recording, ``--file`` can only answer "exists", so a freshly set-up project
  would look audited while every check came back ``INVALID``.

``--print-only`` performs the same detection and the same merge, prints the
result, and writes nothing. It is the same code path with the writes withheld,
not a second description that can drift from the real one.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import baseline as baseline_mod
from . import usage
from .self_audit import run_self_audit

#: MCP server entry written into the client config. ``command`` is the CLI the
#: user already has on PATH, and ``mcp`` is its stdio subcommand -- one name to
#: install, rather than a second console script that can be missing.
MCP_SERVER_NAME = "harness-audit"
MCP_ENTRY: Dict[str, Any] = {"command": "harness-audit", "args": ["mcp"]}

#: Resolved at import time so the tests can point HOME somewhere disposable.
GLOBAL_MCP_PATH = Path.home() / ".cursor" / "mcp.json"
PROJECT_MCP_RELPATH = Path(".cursor") / "mcp.json"

#: The case library reads exactly this path; writing elsewhere would produce a
#: file that looks right and is never read.
REF_ROOTS_PATH = Path.home() / ".gradia" / "case_library" / "ref_roots.json"

#: Extensions worth recording a digest for. A recording is per path, so the
#: baseline has to name the files a check will later ask about; recording every
#: tracked file in a large repository would spend minutes hashing vendored data
#: no claim will mention.
COMMON_SUFFIXES = frozenset(
    {
        ".py", ".pyi", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs",
        ".go", ".rs", ".java", ".kt", ".rb", ".php", ".c", ".h", ".cc",
        ".cpp", ".hpp", ".cs", ".swift", ".scala", ".sh", ".bash", ".zsh",
        ".sql", ".toml", ".yaml", ".yml", ".json", ".md", ".txt", ".cfg",
        ".ini", ".lean",
    }
)

#: Default cap on files recorded by one ``setup``. A cap that is reported is
#: honest; an unbounded walk over a monorepo is a setup command that appears to
#: hang. Unrecorded paths still fail closed -- ``--file`` returns INVALID with
#: the exact ``baseline --file`` command, never a quiet PASS.
DEFAULT_MAX_FILES = 200


def choose_mcp_path(project_root: str) -> Tuple[Path, str]:
    """``(path, scope)``. Project scope preferred, existing global respected.

    The project path hangs off the *repository root*, not the invocation
    directory: Cursor reads ``.cursor/mcp.json`` from the workspace it opened,
    so a setup run in ``repo/sub/`` that wrote ``repo/sub/.cursor/mcp.json``
    would produce a config the client never loads.

    Preference order is deliberate: a project file affects one repository and
    is reviewable in a diff, while the global file affects every project on the
    machine. But when a project file does not exist and a global one does,
    creating a project file would silently shadow the global server the user
    already configured (same server name), so the existing one is used.
    """
    project = Path(os.path.abspath(project_root)) / PROJECT_MCP_RELPATH
    if project.exists():
        return project, "project"
    if GLOBAL_MCP_PATH.exists():
        return GLOBAL_MCP_PATH, "global"
    return project, "project"


def merge_mcp(document: Any) -> Tuple[bool, Dict[str, Any]]:
    """``(changed, merged)``. Adds only our own server entry.

    Other servers are preserved byte-for-byte in value, because a hand-written
    entry may carry fields this tool has no opinion about. A document whose
    ``mcpServers`` is not an object is refused rather than replaced: replacing
    it would delete every other server, and "the file was backed up" is not a
    substitute for not destroying it.
    """
    if document is None or document == {}:
        merged: Dict[str, Any] = {}
    elif not isinstance(document, dict):
        raise ValueError("the existing config is not a JSON object")
    else:
        merged = dict(document)

    servers = merged.get("mcpServers", {})
    if servers is None:
        servers = {}
    if not isinstance(servers, dict):
        raise ValueError("mcpServers is not a JSON object")

    current = servers.get(MCP_SERVER_NAME)
    if current == MCP_ENTRY:
        return False, merged

    servers = dict(servers)
    servers[MCP_SERVER_NAME] = dict(MCP_ENTRY)
    merged["mcpServers"] = servers
    return True, merged


def merge_ref_roots(document: Any, alias: str, project_root: str) -> Tuple[bool, Dict[str, str]]:
    """``(changed, mapping)``. Upserts one alias, keeps the rest.

    ``prepare_ref_roots`` never overwrites; that is right for ``init``, which
    must not repoint an existing alias as a side effect of printing. ``setup``
    is the explicit "make this checkout work" command, so it updates *this*
    alias and leaves every other one alone.
    """
    if document is None:
        mapping: Dict[str, str] = {}
    elif not isinstance(document, dict):
        raise ValueError("the existing ref_roots file is not a JSON object")
    else:
        mapping = {str(key): str(value) for key, value in document.items()}

    target = os.path.abspath(project_root)
    if mapping.get(alias) == target:
        return False, mapping
    mapping = dict(mapping)
    mapping[alias] = target
    return True, mapping


def backup_path(path: Path) -> Path:
    """``<file>.bak``, then ``.bak.1``, ``.bak.2`` ... rather than clobbering.

    Overwriting ``.bak`` on the second run would replace the *original* content
    with the already-modified content, which is a backup that exists and cannot
    restore anything -- the failure mode of a backup, not of a write.
    """
    candidate = Path(str(path) + ".bak")
    index = 1
    while candidate.exists():
        candidate = Path("{}.bak.{}".format(path, index))
        index += 1
    return candidate


def _write_json(path: Path, payload: Any, *, backup: bool) -> Optional[Path]:
    """Write atomically. Returns the backup path, if one was made.

    ``os.replace`` from a sibling temp file: a write interrupted half way leaves
    the client config intact instead of truncated, and a truncated ``mcp.json``
    is a client that will not start.
    """
    made: Optional[Path] = None
    if backup and path.exists():
        made = backup_path(path)
        shutil.copy2(path, made)

    path.parent.mkdir(parents=True, exist_ok=True)
    handle, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=2, ensure_ascii=False, sort_keys=True)
            stream.write("\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return made


def _read_json(path: Path) -> Any:
    """Parsed document, ``None`` when absent. Raises ``ValueError`` when broken.

    A broken file is not silently treated as empty: that would let ``setup``
    rewrite a config it could not read, and the operator would lose whatever
    the file actually contained.
    """
    if not path.exists():
        return None
    raw = path.read_text(encoding="utf-8")
    if not raw.strip():
        return None
    try:
        return json.loads(raw)
    except ValueError as problem:
        raise ValueError("{} is not valid JSON ({})".format(path, problem))


def collect_common_files(cwd: str, *, limit: int = DEFAULT_MAX_FILES) -> Tuple[List[str], int, bool]:
    """``(files, matched, truncated)`` for the recording.

    Inside a repository the tracked files are the set a claim can be about: a
    file git does not track is usually generated, and recording it would produce
    a digest that changes without a change.
    """
    root = baseline_mod.resolve_repo(cwd)
    candidates: List[str] = []
    if root is not None and shutil.which("git") is not None:
        try:
            completed = subprocess.run(
                ["git", "-C", root, "ls-files", "-z"],
                capture_output=True,
                stdin=subprocess.DEVNULL,
                timeout=30,
            )
        except (OSError, subprocess.SubprocessError):
            completed = None
        if completed is not None and completed.returncode == 0:
            for raw in completed.stdout.split(b"\0"):
                if not raw:
                    continue
                name = raw.decode("utf-8", "replace")
                if os.path.splitext(name)[1].lower() in COMMON_SUFFIXES:
                    candidates.append(os.path.join(root, name))
    if not candidates:
        # No repository (or nothing tracked): the top level of the directory the
        # operator pointed at. Deliberately shallow -- a recursive walk outside
        # git is how a setup command discovers node_modules.
        base = Path(os.path.abspath(cwd))
        try:
            for entry in sorted(base.iterdir()):
                if entry.is_file() and entry.suffix.lower() in COMMON_SUFFIXES:
                    candidates.append(str(entry))
        except OSError:
            pass

    candidates.sort()
    matched = len(candidates)
    return candidates[:limit], matched, matched > limit


def _parse_args(argv: List[str]):
    import argparse

    parser = argparse.ArgumentParser(
        prog="harness-audit setup",
        description=(
            "Write the Cursor MCP config, merge ref_roots.json, and record a "
            "baseline. Backs up every file it replaces; --print-only writes "
            "nothing."
        ),
    )
    parser.add_argument("--cwd", default=os.getcwd(), help="project directory (default: current)")
    parser.add_argument(
        "--print-only",
        action="store_true",
        help="show what would be written; write nothing",
    )
    parser.add_argument(
        "--local",
        action="store_true",
        help="record the baseline as ./.harness-baseline.json instead of the global file",
    )
    parser.add_argument(
        "--no-baseline",
        action="store_true",
        help=(
            "write the config only; leave an existing recording untouched "
            "(used by `up`, which must not overwrite the memory it is about to judge)"
        ),
    )
    parser.add_argument(
        "--max-files",
        type=int,
        default=DEFAULT_MAX_FILES,
        help="cap on files recorded in the baseline (default: %d)" % DEFAULT_MAX_FILES,
    )
    # Overrides exist so the command is testable without touching a real home
    # directory, and so a machine with a non-standard Cursor layout can still be
    # set up. They are paths, not behaviour toggles.
    parser.add_argument("--mcp-config", default=None, help="override the mcp.json path")
    parser.add_argument("--ref-roots", default=None, help="override the ref_roots.json path")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    return parser.parse_args(argv)


def run_setup(argv: List[str]) -> int:
    """Returns a process exit code. ``2`` means "refused to write; fix this"."""
    options = _parse_args(argv)
    cwd = os.path.abspath(options.cwd)
    if not os.path.isdir(cwd):
        print("setup: directory does not exist: {}".format(cwd), file=sys.stderr)
        return 2

    if not run_self_audit():
        # Writing configuration for a tool that no longer rejects its own
        # known-bad cases would be setting up decoration. Refuse, and say which
        # diagnostic to run.
        print(
            "setup: self-audit failed, so no configuration was written. Run "
            "`harness-audit` (the demo) to see which fixture failed.",
            file=sys.stderr,
        )
        return 3

    project_root = baseline_mod.resolve_repo(cwd) or cwd

    mcp_path, scope = (
        (Path(options.mcp_config), "explicit")
        if options.mcp_config
        else choose_mcp_path(project_root)
    )
    ref_path = Path(options.ref_roots) if options.ref_roots else REF_ROOTS_PATH

    alias = _alias_for(project_root)
    try:
        mcp_changed, mcp_document = merge_mcp(_read_json(mcp_path))
        roots_changed, roots_mapping = merge_ref_roots(
            _read_json(ref_path), alias, project_root
        )
    except ValueError as problem:
        print("setup: refusing to write: {}".format(problem), file=sys.stderr)
        print(
            "       fix the file by hand (or move it aside) and re-run; this "
            "command will not replace a file it cannot read.",
            file=sys.stderr,
        )
        return 2

    files, matched, truncated = collect_common_files(cwd, limit=options.max_files)

    report: Dict[str, Any] = {
        "project_root": project_root,
        "print_only": bool(options.print_only),
        "mcp": {
            "path": str(mcp_path),
            "scope": scope,
            "changed": mcp_changed,
            "written": False,
            "backup": None,
            "server": MCP_ENTRY,
        },
        "ref_roots": {
            "path": str(ref_path),
            "alias": alias,
            "target": os.path.abspath(project_root),
            "changed": roots_changed,
            "written": False,
            "backup": None,
            # Whether this is the file `@alias` resolution reads by default.
            # Recorded at write time because "which file did I just edit" is
            # the question an operator asks when a lookup still fails, and a
            # custom `--ref-roots` is invisible in the path alone.
            "default": os.path.abspath(str(ref_path)) == os.path.abspath(str(REF_ROOTS_PATH)),
        },
        "baseline": {
            "files": len(files),
            "matched": matched,
            "truncated": truncated,
            "local": bool(options.local),
            "written": False,
        },
    }

    if not options.print_only:
        if mcp_changed:
            report["mcp"]["backup"] = _as_str(_write_json(mcp_path, mcp_document, backup=True))
            report["mcp"]["written"] = True
        if roots_changed:
            report["ref_roots"]["backup"] = _as_str(
                _write_json(ref_path, roots_mapping, backup=True)
            )
            report["ref_roots"]["written"] = True
        if not options.no_baseline:
            baseline_mod.record(cwd, files=files, local=options.local)
            report["baseline"]["written"] = True
            # Setup takes a recording as one of its three steps. Logging it as a
            # `baseline` event keeps the usage log's count of recordings true even
            # when the operator never typed `harness-audit baseline`.
            usage.record_baseline(
                files=len(files), local=bool(options.local), cwd=cwd
            )

    usage.record_setup(
        mcp_scope=report["mcp"]["scope"],
        mcp_changed=bool(report["mcp"]["changed"]),
        ref_roots_changed=bool(report["ref_roots"]["changed"]),
        baseline_files=int(report["baseline"]["files"]),
        baseline_written=bool(report["baseline"]["written"]),
        print_only=bool(options.print_only),
    )

    if options.json:
        print(json.dumps(report, indent=2, ensure_ascii=False, sort_keys=True))
        return 0

    _print_report(report)
    return 0


def _as_str(value: Optional[Path]) -> Optional[str]:
    return str(value) if value is not None else None


def _alias_for(project_root: str) -> str:
    name = os.path.basename(os.path.abspath(project_root.rstrip(os.sep))) or "repo"
    cleaned = "".join(c if (c.isalnum() or c in "-_.") else "-" for c in name).strip("-")
    return (cleaned or "repo").lower()


def _display_path(path: str) -> str:
    """``/home/me/.gradia/x.json`` -> ``~/.gradia/x.json``, for eye-readable output.

    Only cosmetic: every path in the JSON report is still absolute, because a
    machine consumer wants the real location and a human wants the short one.
    """
    home = os.path.expanduser("~")
    try:
        absolute = os.path.abspath(path)
        if home and home != "/" and (absolute == home or absolute.startswith(home + os.sep)):
            return "~" + absolute[len(home):]
    except (OSError, ValueError):  # pragma: no cover - defensive
        pass
    return path


def _print_report(report: Dict[str, Any]) -> None:
    verb = "would write" if report["print_only"] else "wrote"
    print("project root : {}".format(report["project_root"]))
    print("")

    mcp = report["mcp"]
    state = "unchanged" if not mcp["changed"] else verb
    print("mcp config   : {} ({}) -- {}".format(mcp["path"], mcp["scope"], state))
    if mcp["changed"]:
        print(
            "  server     : {} -> {}".format(
                MCP_SERVER_NAME, " ".join([MCP_ENTRY["command"], *MCP_ENTRY["args"]])
            )
        )
    if mcp["backup"]:
        print("  backup     : {}".format(mcp["backup"]))

    roots = report["ref_roots"]
    state = "unchanged" if not roots["changed"] else verb
    line = "ref_roots    : {} -- {}".format(_display_path(roots["path"]), state)
    if roots.get("backup"):
        line += " (backup: {})".format(os.path.basename(roots["backup"]))
    if not roots.get("default", True):
        # The one fact a failed `@alias` lookup needs: this run edited a file
        # the resolver does not read, so the alias will not appear to work even
        # though the write succeeded.
        line += " (custom path, not the default)"
    print(line)
    print("  alias      : {} -> {}".format(roots["alias"], roots["target"]))

    baseline = report["baseline"]
    recorded = "would record" if report["print_only"] else "recorded"
    where = "./.harness-baseline.json" if baseline["local"] else "~/.gradia/baseline.json"
    if baseline["truncated"]:
        print(
            "baseline     : {} {} of {} files matching common extensions (capped) -> {}".format(
                recorded, baseline["files"], baseline["matched"], where
            )
        )
        print(
            "               note: paths past the cap stay INVALID for --file; "
            "run `harness-audit baseline --file <path>` for those"
        )
    else:
        print(
            "baseline     : {} {} file(s) -> {}".format(
                recorded, baseline["files"], where
            )
        )

    print("")
    if report["print_only"]:
        print("next         : nothing was written (--print-only); drop the flag to apply")
    else:
        print(
            "next         : restart Cursor so it reloads MCP servers; then run "
            "`harness-audit check --file <path>`"
        )
