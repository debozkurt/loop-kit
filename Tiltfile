# loopkit fleet — Tilt orchestration of the deployable fleet (Chapter 12).
#
# Tilt is where a multi-service dev loop earns its keep: one `tilt up` builds the worker image,
# loads it into kind, deploys Redis + the worker pool, port-forwards Redis to localhost, and
# live-reloads the workers when the code changes. The coordinator (`loopkit fleet run|evolve`)
# then runs on the host against the forwarded Redis.

# --- Cluster isolation: refuse to run anywhere but the dedicated loopkit cluster. -------------
# This is the FIRST thing the Tiltfile does. allow_k8s_contexts whitelists exactly one context;
# the explicit fail() turns "wrong context" into a hard stop with a clear message instead of a
# silent deploy to whatever kubectl happens to point at. Combined with the repo-local KUBECONFIG
# (see the Makefile), the only cluster this can ever touch is kind-loopkit.
allow_k8s_contexts('kind-loopkit')
if k8s_context() != 'kind-loopkit':
    fail("refusing to run: expected context 'kind-loopkit', got '%s'. "
         "Run `make fleet-up` and `export KUBECONFIG=$PWD/.kube/loopkit.yaml` first." % k8s_context())

# --- Worker image -----------------------------------------------------------------------------
# One image is both the sandbox runtime and the worker: it bundles the demo-repo (via
# LOOPKIT_DEMO_REPO in the Dockerfile) and installs the [fleet] extra (the redis client). The
# editable install means a code change is picked up by syncing the source — see live_update.
docker_build(
    'loopkit-worker', '.',
    dockerfile='Dockerfile',
    live_update=[sync('./loopkit', '/opt/loopkit/loopkit')],
)

# --- Manifests --------------------------------------------------------------------------------
k8s_yaml(['k8s/redis.yaml', 'k8s/worker.yaml'])

# Redis: the queue + results store. Port-forward 6379 so the host coordinator can reach it.
k8s_resource(
    'redis',
    port_forwards='6379:6379',
    labels=['fleet'],
)

# The worker pool: long-lived pods, each running `loopkit fleet worker`, draining the queue.
# resource_deps ensures Redis is up first, so a worker's first BRPOP has something to connect to.
k8s_resource(
    'loopkit-worker',
    resource_deps=['redis'],
    labels=['fleet'],
)

print("loopkit fleet: context OK (kind-loopkit). `tilt up` → redis + workers; "
      "then `make fleet-run` / `make fleet-evolve` on the host.")
