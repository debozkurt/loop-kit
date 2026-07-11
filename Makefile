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

# --- Part III cloud (DOKS) -------------------------------------------------------------------
# The cloud control plane gets its OWN repo-local kubeconfig (same isolation property as the kind
# fleet above): `doctl ... kubeconfig save --kubeconfig .kube/loopkit-cloud.yaml` writes there, NOT
# into ~/.kube/config, so the host's personal contexts are never touched or merged. Every cloud
# recipe overrides KUBECONFIG inline (the global export points at the kind cluster) and pins
# LOOPKIT_CLOUD_CONTEXT so loopkit's context-safety guard refuses any other cluster.
DO_CLUSTER       ?= loopkit-prod
DO_REGION        ?= nyc1
CLOUD_CONTEXT    ?= do-$(DO_REGION)-$(DO_CLUSTER)
CLOUD_KUBECONFIG := $(CURDIR)/.kube/loopkit-cloud.yaml
CLOUD_ENV         = KUBECONFIG=$(CLOUD_KUBECONFIG) LOOPKIT_CLOUD_CONTEXT=$(CLOUD_CONTEXT)

.PHONY: fleet-up fleet-down fleet-nodes tilt-up tilt-down fleet-run fleet-evolve \
        cloud-provision cloud-kubeconfig cloud-context cloud-doctor cloud-bootstrap cloud-webhook \
        cloud-ops cloud-ops-shell test demo help

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

cloud-provision: # print the DOKS provisioning recipe (system + autoscaling worker node pools)
	@echo "Provision a DOKS cluster (needs doctl + a DO token). Node pools per docs/architecture/02:"
	@echo "  doctl kubernetes cluster create $(DO_CLUSTER) --region $(DO_REGION) \\"
	@echo "    --node-pool 'name=system;size=s-2vcpu-4gb;count=1' \\"
	@echo "    --node-pool 'name=worker;size=s-4vcpu-8gb;auto-scale=true;min-nodes=0;max-nodes=8' \\"
	@echo "    --kubeconfig $(CLOUD_KUBECONFIG)        # repo-local — never ~/.kube/config"
	@echo "Then: make cloud-doctor && make cloud-bootstrap"

cloud-kubeconfig: .kube # fetch the DOKS kubeconfig into the repo-local file (host ~/.kube untouched)
	@doctl kubernetes cluster kubeconfig save $(DO_CLUSTER) --kubeconfig $(CLOUD_KUBECONFIG)
	@echo "wrote $(CLOUD_KUBECONFIG) (context $(CLOUD_CONTEXT)); host ~/.kube/config not modified"

cloud-context: # show the active cloud context + whether the guard allows mutations (read-only)
	@$(CLOUD_ENV) $(LOOPKIT) cloud context

cloud-doctor: # pre-flight the cloud control plane (extra, kubeconfig, pinned + matching context)
	@$(CLOUD_ENV) $(LOOPKIT) cloud doctor

cloud-bootstrap: # apply ns/loopkit-system (Redis, RBAC, NetworkPolicy) — guarded by the context pin
	@$(CLOUD_ENV) $(LOOPKIT) cloud bootstrap

# The webhook listener is OPT-IN (it provisions a PAID DO LoadBalancer), so it is NOT in the
# bootstrap glob. Apply ONLY the Deployment + Service here (never secret.example.yaml — that holds
# placeholders); create the real loopkit-webhook Secret out of band first. Explicit --context= per
# the global kubectl-safety rule, against the repo-local cloud kubeconfig.
cloud-webhook: # apply the opt-in webhook listener (Deployment + paid LoadBalancer); needs loopkit-webhook Secret
	@echo "Pre-req: create the loopkit-webhook Secret (see k8s/cloud/webhook/secret.example.yaml)."
	@KUBECONFIG=$(CLOUD_KUBECONFIG) kubectl --context=$(CLOUD_CONTEXT) \
	  apply -f k8s/cloud/webhook/deployment.yaml -f k8s/cloud/webhook/service.yaml

# The always-on ops/control pod — exec in to drive `loopkit cloud ... --in-cluster` (no LoadBalancer,
# no public endpoint). Explicit --context= per the global kubectl-safety rule. Needs the loopkit-ops
# Secret (never the placeholder secret.example.yaml). Replace OWNER in the Deployment image first.
cloud-ops: # apply the always-on ops pod (exec in to launch fleets/schedules); needs loopkit-ops Secret
	@echo "Pre-req: create the loopkit-ops Secret (see k8s/cloud/ops/secret.example.yaml)."
	@KUBECONFIG=$(CLOUD_KUBECONFIG) kubectl --context=$(CLOUD_CONTEXT) \
	  apply -f k8s/cloud/ops/deployment.yaml

cloud-ops-shell: # open an interactive shell in the ops pod
	@KUBECONFIG=$(CLOUD_KUBECONFIG) kubectl --context=$(CLOUD_CONTEXT) -n loopkit-system \
	  exec -it deploy/loopkit-ops -- bash

test: # run the unit suite (fakeredis + MockAgent — no cluster, no tokens)
	@.venv/bin/python -m pytest -q

demo: # run the fleet teaching scenario (Ch 12) in-process
	@.venv/bin/loopkit demo 12
