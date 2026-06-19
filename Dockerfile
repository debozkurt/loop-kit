# loopkit — a reproducible, sandboxed loop environment (Chapter 16: blast-radius containment).
#
#   docker build -t loopkit .
#   docker run --rm loopkit demo 13                                  # a scenario, fully isolated
#   docker run --rm -v "$PWD":/work -w /work loopkit run --dry-run   # rehearse against your repo
#
# The gate runs the TARGET project's toolchain, so a real run needs that toolchain (and, for a
# live agent, the agent binary + credentials) present in the container — extend this image for
# your stack. v1 ships the Python toolchain so the demo-repo gates work out of the box.
FROM python:3.13-slim

# git is required for the loop's durability (commit every tick); pytest for the demo-repo gates.
RUN apt-get update \
 && apt-get install -y --no-install-recommends git \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/loopkit
COPY pyproject.toml README.md ./
COPY loopkit ./loopkit
COPY examples ./examples
RUN pip install --no-cache-dir . pytest

# examples/ ships at the repo root (not inside the package), so point the scenarios at it.
ENV LOOPKIT_DEMO_REPO=/opt/loopkit/examples/demo-repo

# Run as a non-root user so the blast radius stays small even inside the container.
RUN useradd --create-home runner \
 && git config --system --add safe.directory '*'
USER runner
WORKDIR /work

ENTRYPOINT ["loopkit"]
CMD ["--help"]
