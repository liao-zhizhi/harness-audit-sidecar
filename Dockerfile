# harness-audit-sidecar: the MCP stdio server, in the smallest box it fits in.
#
# Build:
#   docker build -t harness-audit-sidecar:0.10.1 .
#
# Run (the bind on 127.0.0.1 is not optional -- see "Why the loopback" below):
#   docker run --rm -i \
#     -v "$PWD:/work" -v harness-audit-state:/state \
#     harness-audit-sidecar:0.10.1
#
# The container is a *stdio* server: it reads JSON-RPC on stdin and writes on
# stdout, so it must run with `-i` (keep stdin open). Without `-i` the pipe
# closes immediately and the process exits after a handshake that never came.
FROM python:3.11-slim

# git is not optional. The change-set witness -- "which paths did this command
# actually touch" -- is read from git, and it is the only witness that catches
# an agent editing the wrong file. Without it every `run_command` with a
# declared expectation fails closed to INVALID, and the flagship verdict
# degrades to "we could not look" in a container that looks healthy.
#
# ca-certificates is needed by the citation witness (DOI/arXiv resolution).
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# PYTHONUTF8: the MCP transport is UTF-8 by specification, and a slim image
# has no locale set. Setting it explicitly keeps one non-ASCII tool argument
# from turning into a UnicodeEncodeError inside the protocol layer.
# PYTHONUNBUFFERED: stdout is a pipe; a buffered response is an invisible one.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUTF8=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Copy the metadata first so the dependency layer is cached independently of
# the source. README is required: pyproject.toml names it as the readme, and
# the build fails without it.
COPY pyproject.toml MANIFEST.in README.md LICENSE LICENSE-CC-BY ./
COPY audit_sidecar ./audit_sidecar

# Installed from the build context rather than from an index: the package is
# not published yet, and a Dockerfile that pip-installs a name that does not
# resolve is a Dockerfile that has never been built.
RUN pip install .

# The sidecar executes the audited commands, so it must be able to write the
# working tree; it must not be root while doing so. 10001 is arbitrary but
# fixed, so a host can match it with `--user` if it wants to.
RUN useradd --uid 10001 --user-group --create-home --home-dir /home/audit \
    --shell /usr/sbin/nologin audit \
    && mkdir -p /work /state \
    && chown -R audit:audit /work /state

USER audit

# /work  = the audited project (the witness sees this tree)
# /state = the trace, the anchor and the inflammation counters. Mount this
#          somewhere the *agent* cannot write: an agent that can edit its own
#          audit log is not being audited.
VOLUME ["/work", "/state"]

# Reserved for the local REST API (POST /api/v1/audit). Nothing listens on it
# yet -- the MCP stdio server uses the pipe, not the port -- so this EXPOSE is
# a reservation, not a claim that the endpoint exists.
EXPOSE 8080

# Refuse to serve unless the engine still rejects its own known-bad fixtures.
# This runs on every start, before the first byte of traffic.
ENTRYPOINT ["harness-audit-server"]
CMD ["--cwd", "/work", \
     "--trace", "/state/trace.jsonl", \
     "--immune-state", "/state/immune.json"]
