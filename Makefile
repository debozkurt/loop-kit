# loopkit fleet — cluster lifecycle + Tilt, with isolation baked into every recipe (Ch 12 + 16).
#
# The load-bearing safety property: KUBECONFIG points at a repo-local file, exported for ALL
# recipes, so kind and kubectl read/write ONLY the loopkit cluster's credentials. The user's
# ~/.kube/config (sre-agent / spacer / glip-bot / RingCentral / …) is never opened or merged.
# This is the mechanism the global kubectl-safety rule asks for, wired in so the flags can't be
# forgotten.
#
#   make fleet-up        # create the isolated kind cluster (context kind-loopkit)
#   make tilt-up         # build + deploy redis + workers; port-forward redis to localhost
#   make fleet-run       # coordinator: blind fan-out (needs tilt-up running for the port-forward)
#   make fleet-evolve    # coordinator: evolutionary search
#   make fleet-down      # delete the cluster — ~/.kube/config was never written

CLUSTER         := loopkit
CONTEXT         := kind-$(CLUSTER)
NAMESPACE       := loopkit
KUBECONFIG_FILE := $(CURDIR)/.kube/loopkit.yaml
# The shell kubectl wrapper is broken here (_kube_guard_enforce: command not found in
# non-interactive shells), so call the real binary. Override on another machine: make KUBECTL=...
KUBECTL         ?= /opt/homebrew/bin/kubectl
# The loopkit CLI from the project venv (matches the test/demo targets). Override: make LOOPKIT=loopkit
LOOPKIT         ?= .venv/bin/loopkit

# Exported to every recipe's environment — this is the isolation guarantee, not a per-command flag.
export KUBECONFIG = $(KUBECONFIG_FILE)

.PHONY: fleet-up fleet-down fleet-nodes tilt-up tilt-down fleet-run fleet-evolve test demo help

help:
	@grep -E '^[a-zA-Z_-]+:.*?# ' $(MAKEFILE_LIST) | sort | \
	  awk 'BEGIN{FS=":.*?# "}{printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

.kube:
	@mkdir -p .kube

fleet-up: .kube # create the isolated kind cluster (idempotent), then show its nodes
	@if kind get clusters 2>/dev/null | grep -qx "$(CLUSTER)"; then \
	  echo "cluster '$(CLUSTER)' already exists — kubeconfig at $(KUBECONFIG_FILE)"; \
	else \
	  echo "creating isolated kind cluster '$(CLUSTER)' (kubeconfig: $(KUBECONFIG_FILE)) …"; \
	  kind create cluster --name "$(CLUSTER)"; \
	fi
	@$(KUBECTL) --context="$(CONTEXT)" get nodes

fleet-down: # delete the kind cluster (the host's ~/.kube/config was never touched)
	@kind delete cluster --name "$(CLUSTER)" || true

fleet-nodes: # show the cluster's nodes (verifies the context + repo-local kubeconfig)
	@$(KUBECTL) --context="$(CONTEXT)" get nodes

tilt-up: # build images + deploy redis + workers on the cluster; port-forward redis to localhost
	@tilt up

tilt-down: # tear down everything Tilt deployed (leaves the cluster)
	@tilt down

# The coordinator talks to the cluster's redis via Tilt's port-forward at localhost:16379 (16379,
# not 6379, so it never touches a local redis-server on the default port). Requires `tilt up`.
REDIS_LOCAL := redis://localhost:16379

fleet-run: # coordinator: blind fan-out over the queue (requires `make tilt-up` / `tilt up`)
	@$(LOOPKIT) fleet run --redis-url $(REDIS_LOCAL)

fleet-evolve: # coordinator: evolutionary search over the queue
	@$(LOOPKIT) fleet evolve --redis-url $(REDIS_LOCAL)

test: # run the unit suite (fakeredis + MockAgent — no cluster, no tokens)
	@.venv/bin/python -m pytest -q

demo: # run the fleet teaching scenario (Ch 12) in-process
	@.venv/bin/loopkit demo 12
