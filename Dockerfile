# loopkit — a reproducible, sandboxed loop environment (Chapter 16: blast-radius containment).
# This one image is both the sandbox runtime and the fleet worker (Chapter 12).
#
#   docker build -t loopkit .
#   docker run --rm loopkit demo 13                                  # a scenario, fully isolated
#   docker run --rm -v "$PWD":/work -w /work loopkit run --dry-run   # rehearse against your repo
#   docker run --rm -e REDIS_URL=... loopkit fleet worker            # a fleet worker pod
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
# Editable install with [fleet]+[cloud]: editable so Tilt's live_update (sync ./loopkit) reloads
# worker code without a rebuild; [fleet] pulls the thin redis client the coordinator/worker need,
# [cloud] the kubernetes client the in-cluster triggers use (the CronJob + webhook listener run
# `loopkit cloud run --in-cluster`, Part III Phase 4). pytest is the demo-repo gates' runner.
RUN pip install --no-cache-dir -e '.[fleet,cloud]' pytest

# examples/ ships at the repo root (not inside the package), so point the scenarios at it.
ENV LOOPKIT_DEMO_REPO=/opt/loopkit/examples/demo-repo

# Run as a non-root user so the blast radius stays small even inside the container. Pin uid 1000 so
# the cloud pod's securityContext (runAsUser/fsGroup 1000, Phase 5a) lands on this exact user.
RUN useradd --create-home --uid 1000 runner \
 && git config --system --add safe.directory '*'
USER runner
WORKDIR /work

ENTRYPOINT ["loopkit"]
CMD ["--help"]
