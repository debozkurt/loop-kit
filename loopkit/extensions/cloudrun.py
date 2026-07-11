"""Per-run mechanics — `create_run()` and the ephemeral Job topology it builds (Part III, Phase 3).

This is the core integration: turning a submission (a target + goal-set + budget) into a running
pair of Kubernetes Jobs in a fresh, isolated namespace. The three submit paths (CLI, CronJob,
webhook — Phase 4) all converge on `create_run()`, so behaviour is identical no matter how a run
starts.

The shape, per [`docs/architecture/02-cloud-architecture.md`](../../docs/architecture/02-cloud-architecture.md):

    ns/run-<id>  (ephemeral, one per run, TTL-GC'd)
      ├─ Job coordinator   `fleet run|evolve --drain-workers N`  (enqueue → collect → sentinel → exit)
      ├─ Job worker        parallelism N, completions unset      (BRPOP → clone → run_loop → push → exit)
      │    ├─ loopkit-core (uid 1000)   HOLDS the key (envFrom); loop · LLM call · git clone/commit/push
      │    └─ executor    (uid 1001)    keyless native sidecar; run_bash/read/write + held-out gate
      ├─ ServiceAccount loopkit-worker   no cluster-API access (automount off)
      ├─ Secret loopkit-creds            git creds + the agent key (envFrom into loopkit-core ONLY)
      ├─ ResourceQuota + LimitRange      loose to start
      └─ NetworkPolicy                   default-deny + egress allowlist (DNS, Redis, HTTPS, no metadata)

**Why the two-container worker (Phase 6 agent isolation).** The agent's tool calls + the held-out gate
run attacker-influenced commands. Rather than mitigate (load-shred the key in time, Phase 5a), the
worker pod runs that untrusted surface in a **keyless executor sidecar** — a different uid in its own
PID namespace — and loopkit-core dispatches to it over a Unix socket. The credential is `envFrom`'d
into loopkit-core only; there is nothing to `ptrace` and nothing to shred in the executor. The
coordinator runs no untrusted command, so it stays a single trusted container.

**Why two Jobs and a shared Redis keyspace.** The worker Job is the canonical *fine-grained
work-queue* pattern — `parallelism: N`, `completions` unset, pods drain the queue and exit 0. The
coordinator owns "the run is over" and enqueues N **sentinels** (see `fleet.drain`) so the pods exit
cleanly; this is required for `evolve`, where the queue is empty *between* generations. Both Jobs
share one per-run Redis **keyspace** (`--redis-namespace <run-ns>` → `{ns}:tasks`/`{ns}:results`), so
one Redis StatefulSet in `loopkit-system` serves every run with no cross-talk.

Design constraints carried from the rest of the project:

- **Pure builders, deferred client.** Every `build_*` function returns a plain manifest dict and
  imports nothing — so the whole topology is unit-testable (parallelism, sentinel-drain command,
  per-run keyspace, emptyDir, least-priv SA, default-deny network) with no cluster and no client.
  Only `create_run`/`delete_run`/`list_runs` touch the `kubernetes` client, and they defer-import it
  (the `[cloud]` extra) and run the **context-safety guard first** ([`cloud.check_context`]).
- **emptyDir, not a PVC.** Durability is via the git push on DONE; worker scratch is node-local and
  free, with a `sizeLimit` so a runaway clone can't fill the node (DO block storage is RWO anyway).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, Sequence

from .. import secrets
from ..log import get_logger
from . import cloud

log = get_logger("cloudrun")

SYSTEM_NAMESPACE = "loopkit-system"
# In-cluster Redis Service DNS (set up by Phase-2 bootstrap). Note: NOT :16379 — that dev-only remap
# exists only to dodge a laptop's local redis-server; in-cluster there is no such collision.
DEFAULT_REDIS_URL = f"redis://redis.{SYSTEM_NAMESPACE}.svc.cluster.local:6379"

# Phase 6 — agent isolation. The worker pod is split in two: loopkit-core (uid 1000) holds the
# credential and runs only trusted code (the loop, the LLM API call, git clone/commit/push); the
# keyless executor sidecar (uid 1001, a separate PID namespace by default) runs the untrusted surface
# (the agent's run_bash/read/write + the held-out gate) against the shared workspace. They talk over a
# Unix socket on a shared emptyDir.
EXECUTOR_UID = 1001                                  # different uid → can't ptrace loopkit-core's heap
EXECUTOR_SOCKET_DIR = "/var/run/loopkit-exec"        # shared emptyDir; both containers mount it here
EXECUTOR_SOCKET = f"{EXECUTOR_SOCKET_DIR}/exec.sock"
# Set on the worker container (survives the load-shred — not a credential var) so the entrypoint can
# fail-closed when an API adapter resolves no key (a misconfiguration), instead of 401-ing deep in a tick.
CREDS_EXPECTED_ENV = "LOOPKIT_CREDS_EXPECTED"

# Phase-5a hardening (C2): a non-root, no-caps, read-only-rootfs pod so privilege escalation is blocked
# and file ownership is deterministic. The shared `fsGroup` makes the workspace + socket emptyDirs
# group-owned (gid 1000); both uids are in that group, so loopkit-core can clone/commit files the
# executor edited and vice-versa (the entrypoints `umask 002` for group-writable trees).
_POD_SECURITY_CONTEXT = {"runAsNonRoot": True, "runAsUser": 1000, "runAsGroup": 1000, "fsGroup": 1000,
                         "seccompProfile": {"type": "RuntimeDefault"}}
_CONTAINER_SECURITY_CONTEXT = {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                               "capabilities": {"drop": ["ALL"]}}
# The executor sidecar runs as a *different* uid than loopkit-core — so even within the pod it cannot
# `ptrace`/`process_vm_readv` loopkit-core's heap (where the SDK client holds the key), and in its own
# PID namespace (the k8s default — no `shareProcessNamespace`) it can't see loopkit-core's PIDs at all.
_EXECUTOR_SECURITY_CONTEXT = {**_CONTAINER_SECURITY_CONTEXT, "runAsUser": EXECUTOR_UID,
                              "runAsGroup": 1000}

# The only hosts a run legitimately reaches over 443: the agent API, the git forge, the image registry.
# A per-run Cilium FQDN policy narrows the broad `0.0.0.0/0:443` egress to these, so a hijacked run
# can't POST exfiltrated data to an arbitrary host. Cilium-specific (DOKS runs it); applied best-effort.
DEFAULT_EGRESS_FQDNS = ["api.anthropic.com", "api.openai.com", "github.com",
                        "*.githubusercontent.com", "codeload.github.com", "ghcr.io",
                        "*.pkg.github.com", "gitlab.com"]

# Custom resources (CRDs) the run topology stamps — `create_from_dict` only knows built-in kinds, so
# these go through `CustomObjectsApi` (and are applied best-effort: a cluster without the CRD is fine).
_CUSTOM_PLURALS = {"CiliumNetworkPolicy": "ciliumnetworkpolicies"}

_DNS_LABEL = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")


def sanitize_run_id(raw: str) -> str:
    """Coerce `raw` into a DNS-1123-label-safe run id (lowercase alphanumeric + '-', <= 50 chars).

    The run id becomes part of the namespace name (`run-<id>`) and many object names, so it must be a
    valid DNS label. We lowercase, replace illegal runs with '-', trim, and cap length — a value
    change, never a crash, so a human-friendly `--name "Nightly Issues!"` still yields a legal id.
    """
    cleaned = re.sub(r"[^a-z0-9-]+", "-", raw.lower()).strip("-")
    cleaned = cleaned[:50].strip("-")
    if not cleaned:
        raise ValueError(f"run id {raw!r} has no DNS-label-safe characters")
    return cleaned


@dataclass
class RunSpec:
    """Everything `create_run` needs to build one run's namespace + Jobs. Plain data, no I/O.

    `mode` selects the coordinator's job: `fanout` (N independent attempts at one goal, or one task
    per issue) or `evolve` (generational search). `parallelism` — the worker Job's pod count and the
    coordinator's sentinel count — is `population` for evolve, else `workers`.
    """

    run_id: str
    image: str                                   # ghcr.io/<owner>/loopkit-worker:<tag>
    target: str                                  # repo URL/path the workers clone + operate on
    workers: int = 1
    adapter: str = "claude-code"
    goal: str | None = None
    from_issues: bool = False
    label: str | None = None
    provider: str = "auto"                        # issue forge: auto | github | gitlab (--from-issues)
    mode: str = "fanout"                          # fanout | evolve
    generations: int = 2                         # evolve
    population: int = 4                          # evolve
    keep: int = 2                               # evolve
    max_iter: int = 8
    max_cost_usd: float = 5.0
    env_name: str = "prod"
    submitter: str = "fleet"                     # whose key this run spends (Phase 5a; a run label)
    skills_repo: str | None = None               # loopkit-skills git repo: cross-run flywheel (Phase 5b)
    skills_branch: str = "main"                  # branch of the skills repo to read/write
    redis_url: str = DEFAULT_REDIS_URL
    image_pull_secret: str | None = "ghcr-pull"  # None for a public GHCR package
    creds_secret: str = "loopkit-creds"          # worker Secret: adapter key + git creds
    coordinator_creds_secret: str = "loopkit-creds-coord"  # coordinator Secret: git creds only (G1)
    ttl_seconds: int = 3600                      # ttlSecondsAfterFinished — GC finished Jobs
    active_deadline_seconds: int = 10800         # Job-wide wall (3h): kills a wedged run (Finding D)
    backoff_limit: int = 2
    scratch_size: str = "2Gi"
    cpu_request: str = "250m"
    mem_request: str = "512Mi"
    cpu_limit: str = "2"
    mem_limit: str = "2Gi"
    node_pool: str | None = None                 # pin pods to a DOKS node pool (nodeSelector); None = any node
    namespace_prefix: str = "run"
    fqdn_egress: bool = True                      # stamp a per-run Cilium FQDN egress allowlist (DOKS)
    egress_fqdns: list[str] = field(default_factory=lambda: list(DEFAULT_EGRESS_FQDNS))
    extra_labels: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.run_id = sanitize_run_id(self.run_id)
        if self.mode not in ("fanout", "evolve"):
            raise ValueError(f"mode must be 'fanout' or 'evolve', got {self.mode!r}")
        if self.mode == "fanout" and not (self.goal or self.from_issues):
            raise ValueError("a fanout run needs --goal or --from-issues")

    @property
    def namespace(self) -> str:
        return f"{self.namespace_prefix}-{self.run_id}"

    @property
    def redis_namespace(self) -> str:
        # Per-run keyspace = the run namespace name, so one Redis serves every run with no cross-talk.
        return self.namespace

    @property
    def parallelism(self) -> int:
        """Worker pod count == coordinator sentinel count (population for evolve, else workers)."""
        return max(1, self.population if self.mode == "evolve" else self.workers)


# --------------------------------------------------------------------------------------------
# Command builders — what runs *inside* the coordinator / worker containers (pure, testable).
# --------------------------------------------------------------------------------------------
def coordinator_command(spec: RunSpec) -> list[str]:
    """The `loopkit …` argv the coordinator pod runs: enqueue, collect, then drain the workers.

    `--drain-workers N` is the load-bearing flag — it makes the coordinator enqueue N sentinels at
    true completion so the ephemeral worker pods exit 0 (and, for evolve, only after the final
    generation). N == `spec.parallelism`, so it always matches the worker Job's pod count.
    """
    n = spec.parallelism
    base = ["fleet", "run" if spec.mode == "fanout" else "evolve",
            "--redis-url", spec.redis_url, "--redis-namespace", spec.redis_namespace,
            "--drain-workers", str(n)]
    if spec.mode == "evolve":
        return base + ["-g", str(spec.generations), "-p", str(spec.population), "-k", str(spec.keep)]
    if spec.from_issues:
        cmd = base + ["--from-issues", "--target", spec.target]
        if spec.label:
            cmd += ["--label", spec.label]
        if spec.provider and spec.provider != "auto":      # force the forge (e.g. self-hosted GitLab)
            cmd += ["--provider", spec.provider]
        return cmd
    return base + ["--tasks", str(n), "--goal", spec.goal or ""]


def worker_command(spec: RunSpec) -> list[str]:
    """The `loopkit fleet worker …` argv loopkit-core runs (drains the per-run keyspace).

    `--executor-socket` is the Phase-6 seam: loopkit-core dispatches the agent's tool calls + the
    held-out gate to the keyless executor sidecar over this socket, so the model's chosen commands
    never run in the key-holding container. `--skills-repo` (Phase 5b) wires the cross-run flywheel:
    loopkit-core (which holds the git token) clones the shared skills repo per task and pushes a gated
    write-back on DONE — only the worker, never the coordinator (which does no write-back).
    """
    cmd = ["fleet", "worker",
           "--redis-url", spec.redis_url, "--redis-namespace", spec.redis_namespace,
           "--adapter", spec.adapter, "--target", spec.target, "--max-iter", str(spec.max_iter),
           "--executor-socket", EXECUTOR_SOCKET]
    if spec.skills_repo:
        cmd += ["--skills-repo", spec.skills_repo, "--skills-branch", spec.skills_branch]
    return cmd


def executor_command() -> list[str]:
    """The argv the keyless executor sidecar runs — serve the tool surface over the shared socket."""
    return ["executor", "--socket", EXECUTOR_SOCKET]


# --------------------------------------------------------------------------------------------
# Object builders — one Kubernetes manifest dict each (pure; no client, no I/O).
# --------------------------------------------------------------------------------------------
def _labels(spec: RunSpec) -> dict[str, str]:
    return {"app.kubernetes.io/part-of": "loopkit",
            "app.kubernetes.io/component": "run",
            "loopkit.dev/run-id": spec.run_id,
            "loopkit.dev/submitter": _label_safe(spec.submitter),
            **spec.extra_labels}


def _label_safe(value: str) -> str:
    """Coerce an arbitrary submitter into a valid label value (<=63 chars, alnum/-/_/. only)."""
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-_.")[:63]
    return cleaned or "unknown"


def build_namespace(spec: RunSpec) -> dict:
    return {"apiVersion": "v1", "kind": "Namespace",
            "metadata": {"name": spec.namespace,
                         "labels": {**_labels(spec),
                                    "kubernetes.io/metadata.name": spec.namespace}}}


def build_worker_sa(spec: RunSpec) -> dict:
    """The workers' ServiceAccount: no Role bound to it anywhere + token automount off = no cluster
    API access. A hijacked agent has nothing to escalate (containment over trust)."""
    return {"apiVersion": "v1", "kind": "ServiceAccount",
            "metadata": {"name": "loopkit-worker", "namespace": spec.namespace, "labels": _labels(spec)},
            "automountServiceAccountToken": False}


def build_resource_quota(spec: RunSpec) -> dict:
    """Loose per-run quota — separation now, tighten later (the structure is what matters)."""
    n = spec.parallelism + 1                      # workers + coordinator
    return {"apiVersion": "v1", "kind": "ResourceQuota",
            "metadata": {"name": "run-quota", "namespace": spec.namespace, "labels": _labels(spec)},
            "spec": {"hard": {"pods": str(n + 1),
                              "requests.cpu": str(n), "requests.memory": f"{n}Gi",
                              "limits.cpu": str(n * 2), "limits.memory": f"{n * 2}Gi"}}}


def build_limit_range(spec: RunSpec) -> dict:
    """Default container requests/limits so a pod with none still gets bounded (Ch 14 cost guard)."""
    return {"apiVersion": "v1", "kind": "LimitRange",
            "metadata": {"name": "run-limits", "namespace": spec.namespace, "labels": _labels(spec)},
            "spec": {"limits": [{"type": "Container",
                                 "default": {"cpu": spec.cpu_limit, "memory": spec.mem_limit},
                                 "defaultRequest": {"cpu": spec.cpu_request, "memory": spec.mem_request}}]}}


def build_network_policy(spec: RunSpec) -> dict:
    """Per-run default-deny ingress + a tight egress allowlist (the worker's only outbound paths).

    Deny all inbound (no `ingress` rules). Allow egress ONLY to: DNS (kube-system), the shared Redis
    in `loopkit-system` (6379), and HTTPS (443) to the internet for GitHub / the agent API / GHCR —
    with the link-local cloud metadata range (169.254.0.0/16) carved out (credential-theft target).
    DOKS runs Cilium, so this can be tightened to FQDN egress later.
    """
    return {"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy",
            "metadata": {"name": "run-egress", "namespace": spec.namespace, "labels": _labels(spec)},
            "spec": {"podSelector": {}, "policyTypes": ["Ingress", "Egress"],
                     "egress": [
                         {"to": [{"namespaceSelector": {"matchLabels": {
                             "kubernetes.io/metadata.name": "kube-system"}}}],
                          "ports": [{"protocol": "UDP", "port": 53}, {"protocol": "TCP", "port": 53}]},
                         {"to": [{"namespaceSelector": {"matchLabels": {
                             "kubernetes.io/metadata.name": SYSTEM_NAMESPACE}}}],
                          "ports": [{"protocol": "TCP", "port": 6379}]},
                         {"to": [{"ipBlock": {"cidr": "0.0.0.0/0", "except": ["169.254.0.0/16"]}}],
                          "ports": [{"protocol": "TCP", "port": 443}]},
                     ]}}


def build_fqdn_egress_policy(spec: RunSpec) -> dict:
    """A `CiliumNetworkPolicy` narrowing per-run HTTPS egress to an FQDN allowlist (defense in depth).

    The standard `build_network_policy` already default-denies and limits egress to DNS, Redis, and
    443; this restricts the 443 to *named hosts* (the agent API, the git forge, GHCR) so a hijacked
    run can't exfiltrate to an arbitrary host. Cilium-specific (DOKS runs Cilium) — `create_run`
    applies it best-effort, skipping it on a cluster whose API doesn't serve the CRD.
    """
    fqdn_rules = [{"matchPattern": f} if "*" in f else {"matchName": f} for f in spec.egress_fqdns]
    return {
        "apiVersion": "cilium.io/v2", "kind": "CiliumNetworkPolicy",
        "metadata": {"name": "run-fqdn-egress", "namespace": spec.namespace, "labels": _labels(spec)},
        "spec": {
            "endpointSelector": {},
            "egress": [
                # DNS to kube-dns (required so Cilium can resolve + enforce the toFQDNs rule).
                {"toEndpoints": [{"matchLabels": {"k8s:io.kubernetes.pod.namespace": "kube-system",
                                                  "k8s:k8s-app": "kube-dns"}}],
                 "toPorts": [{"ports": [{"port": "53", "protocol": "ANY"}],
                              "rules": {"dns": [{"matchPattern": "*"}]}}]},
                # HTTPS only to the allowlisted FQDNs.
                {"toFQDNs": fqdn_rules,
                 "toPorts": [{"ports": [{"port": "443", "protocol": "TCP"}]}]},
                # The shared Redis in loopkit-system.
                {"toEndpoints": [{"matchLabels": {
                    "k8s:io.kubernetes.pod.namespace": SYSTEM_NAMESPACE}}],
                 "toPorts": [{"ports": [{"port": "6379", "protocol": "TCP"}]}]},
            ],
        },
    }


def build_creds_secret(spec: RunSpec, data: dict[str, str], *, name: str | None = None) -> dict:
    """A per-run Secret holding `data` (resolved + projected creds), `name` defaulting to the worker
    Secret. Namespace-scoped and GC'd with the namespace.

    In the Phase-6 split this Secret is mounted (`envFrom`) **only into loopkit-core** — the trusted
    container that runs no model-chosen command. The untrusted surface (run_bash/read/write + the gate)
    runs in the *keyless* executor sidecar, which has no Secret reference at all, so there is nothing
    for a hijacked agent to `printenv`/`cat`. `stringData` is base64 (not encrypted) in the k8s API —
    at-rest protection depends on the cluster's etcd posture, which the operator must verify (see
    [`04-security.md`]). Empty `data` ⇒ caller omits the Secret entirely.
    """
    return {"apiVersion": "v1", "kind": "Secret", "type": "Opaque",
            "metadata": {"name": name or spec.creds_secret, "namespace": spec.namespace,
                         "labels": _labels(spec)},
            "stringData": dict(data)}


def _executor_sidecar(spec: RunSpec) -> dict:
    """The keyless tool-execution sidecar (Phase 6) — a *native* sidecar (an initContainer with
    `restartPolicy: Always`, stable since k8s 1.29) so it starts before loopkit-core and is terminated
    by the kubelet when loopkit-core exits (letting the Job complete).

    Runs as `EXECUTOR_UID` (≠ loopkit-core) with **no creds env and no Secret mount**, sharing only the
    workspace (the clone target) + the socket dir. `GIT_CONFIG_*` marks all dirs safe so an agent's
    `git` call doesn't trip dubious-ownership on a tree loopkit-core (a different uid) cloned.
    """
    return {
        "name": "executor", "image": spec.image,
        "command": ["loopkit"], "args": executor_command(),
        "restartPolicy": "Always",                            # native sidecar (k8s ≥ 1.29)
        "securityContext": _EXECUTOR_SECURITY_CONTEXT,
        "env": [{"name": "HOME", "value": "/home/executor"},
                {"name": "LOOPKIT_ENV", "value": spec.env_name},
                # Treat any working tree as safe — loopkit-core (uid 1000) owns the clone, the executor
                # runs as uid 1001, so an agent `git` command would otherwise warn dubious-ownership.
                {"name": "GIT_CONFIG_COUNT", "value": "1"},
                {"name": "GIT_CONFIG_KEY_0", "value": "safe.directory"},
                {"name": "GIT_CONFIG_VALUE_0", "value": "*"}],
        "resources": {"requests": {"cpu": spec.cpu_request, "memory": spec.mem_request},
                      "limits": {"cpu": spec.cpu_limit, "memory": spec.mem_limit}},
        "volumeMounts": [{"name": "scratch", "mountPath": "/scratch"},        # the shared workspace
                         {"name": "exec-socket", "mountPath": EXECUTOR_SOCKET_DIR},
                         {"name": "exec-home", "mountPath": "/home/executor"},
                         {"name": "exec-tmp", "mountPath": "/tmp"}],
    }


def _pod_spec(spec: RunSpec, *, command: list[str], scratch: bool, creds_secret: str,
              executor_sidecar: bool) -> dict:
    """Pod template for a Job. loopkit-core is a hardened (non-root, no-caps, read-only-rootfs)
    container that gets the per-role creds via `envFrom` (it runs only trusted code). When
    `executor_sidecar` is set (the worker), a keyless executor native-sidecar runs the untrusted
    surface in a different uid/PID-namespace, and loopkit-core dispatches to it over the shared socket
    (`command` already carries `--executor-socket`). `restartPolicy: Never` + `backoffLimit` own retries.

    Writable paths for the read-only rootfs are explicit emptyDirs (HOME, /tmp, the `/scratch` clone
    target). The creds Secret is referenced **only** by loopkit-core's `envFrom` — never by the
    executor — so the untrusted container has no credential in its env, files, or address space.
    """
    container: dict = {
        "name": "loopkit-core",
        "image": spec.image,
        "command": ["loopkit"],
        "args": command,
        "securityContext": _CONTAINER_SECURITY_CONTEXT,
        # Creds come in via envFrom (loopkit-core is trusted); the entrypoint load-shreds them out of
        # os.environ into the in-process store, and `child_env` re-injects only the git token for git.
        "envFrom": [{"secretRef": {"name": creds_secret, "optional": True}}],
        "env": [{"name": "WORKER_NAME",
                 "valueFrom": {"fieldRef": {"fieldPath": "metadata.name"}}},
                {"name": "LOOPKIT_ENV", "value": spec.env_name},
                {"name": CREDS_EXPECTED_ENV, "value": "1"},   # fail-closed if an API adapter resolves no key
                {"name": "HOME", "value": "/home/loopkit"}],
        "resources": {"requests": {"cpu": spec.cpu_request, "memory": spec.mem_request},
                      "limits": {"cpu": spec.cpu_limit, "memory": spec.mem_limit}},
        "volumeMounts": [{"name": "home", "mountPath": "/home/loopkit"},   # writable HOME (ro-rootfs)
                         {"name": "tmp", "mountPath": "/tmp"}],
    }
    pod: dict = {
        "serviceAccountName": "loopkit-worker",      # no cluster-API access
        "automountServiceAccountToken": False,
        "restartPolicy": "Never",
        "securityContext": _POD_SECURITY_CONTEXT,
        "containers": [container],
        "volumes": [{"name": "home", "emptyDir": {}},
                    {"name": "tmp", "emptyDir": {}}],
    }
    if spec.node_pool:
        # Pin to a DOKS node pool (e.g. an autoscaling worker pool). DOKS labels every node
        # `doks.digitalocean.com/node-pool: <name>`; the pool name is operator config (passed at run
        # time / via $LOOPKIT_NODE_POOL), never hardcoded — same masking posture as cluster/context.
        pod["nodeSelector"] = {"doks.digitalocean.com/node-pool": spec.node_pool}
    if spec.image_pull_secret:
        pod["imagePullSecrets"] = [{"name": spec.image_pull_secret}]
    if scratch:
        container["volumeMounts"].append({"name": "scratch", "mountPath": "/scratch"})
        container["env"].append({"name": "TMPDIR", "value": "/scratch"})
        pod["volumes"].append({"name": "scratch", "emptyDir": {"sizeLimit": spec.scratch_size}})
    if executor_sidecar:
        # The shared socket dir + the executor's own writable HOME/tmp (it runs as a different uid).
        pod["volumes"] += [{"name": "exec-socket", "emptyDir": {}},
                           {"name": "exec-home", "emptyDir": {}},
                           {"name": "exec-tmp", "emptyDir": {}}]
        container["volumeMounts"].append({"name": "exec-socket", "mountPath": EXECUTOR_SOCKET_DIR})
        # Native sidecars are initContainers with restartPolicy: Always (start first, stay running).
        pod["initContainers"] = [_executor_sidecar(spec)]
    return pod


def _job(spec: RunSpec, *, name: str, role: str, command: list[str],
         parallelism: int | None, scratch: bool, creds_secret: str,
         executor_sidecar: bool) -> dict:
    """A Job wrapping the pod template, with TTL GC + backoff. `parallelism=None` ⇒ a single
    completion (the coordinator); a worker Job sets `parallelism: N` with **completions unset** —
    the fine-grained work-queue pattern (pods drain the queue and exit 0 on a sentinel). `creds_secret`
    is the per-role Secret: the worker gets adapter-key+git, the coordinator only git (G1).
    `executor_sidecar` adds the keyless tool-execution sidecar (the worker; not the coordinator)."""
    labels = {**_labels(spec), "app.kubernetes.io/component": role}
    job_spec: dict = {
        "ttlSecondsAfterFinished": spec.ttl_seconds,
        # Liveness bound (Finding D): a hard wall on the whole Job so a wedged tick (a tool/gate the
        # per-call subprocess timeout somehow outlived, or a stuck pod) can't run until the node reaps
        # it — the kubelet terminates the Job's pods past this deadline. Sits above the per-call
        # tool/gate deadlines (executor.TOOL_TIMEOUT/GATE_TIMEOUT) that bound a single step.
        "activeDeadlineSeconds": spec.active_deadline_seconds,
        "backoffLimit": spec.backoff_limit,
        "template": {"metadata": {"labels": labels},
                     "spec": _pod_spec(spec, command=command, scratch=scratch,
                                       creds_secret=creds_secret,
                                       executor_sidecar=executor_sidecar)},
    }
    if parallelism is not None:
        job_spec["parallelism"] = parallelism        # completions left unset on purpose (work queue)
    return {"apiVersion": "batch/v1", "kind": "Job",
            "metadata": {"name": name, "namespace": spec.namespace, "labels": labels},
            "spec": job_spec}


def build_coordinator_job(spec: RunSpec) -> dict:
    # The coordinator runs no agent — it only enqueues/collects and (for --from-issues) lists issues
    # via `gh`, so it needs the git token but NOT the model key (which it would never use). It runs no
    # untrusted command, so it needs no executor sidecar (single trusted container).
    return _job(spec, name="coordinator", role="coordinator",
                command=coordinator_command(spec), parallelism=None, scratch=False,
                creds_secret=spec.coordinator_creds_secret, executor_sidecar=False)


def build_worker_job(spec: RunSpec) -> dict:
    return _job(spec, name="worker", role="worker",
                command=worker_command(spec), parallelism=spec.parallelism, scratch=True,
                creds_secret=spec.creds_secret, executor_sidecar=True)


def build_run_objects(spec: RunSpec, *, creds: dict[str, str] | None = None) -> list[dict]:
    """Every manifest for one run, in apply order (namespace first, workloads last).

    When `creds` is non-empty (a token-free mock run needs none), TWO per-run Secrets are built: the
    worker Secret (the full projected set: adapter key + git) and the coordinator Secret (the git
    subset only — the coordinator never uses the model key, G1). Apply order matters: the namespace
    must exist before anything in it, and the SA/Secrets/policies before the Jobs that reference them.
    """
    objects = [build_namespace(spec), build_worker_sa(spec),
               build_resource_quota(spec), build_limit_range(spec), build_network_policy(spec)]
    if spec.fqdn_egress:
        objects.append(build_fqdn_egress_policy(spec))       # best-effort (skipped if Cilium absent)
    if creds:
        objects.append(build_creds_secret(spec, creds, name=spec.creds_secret))
        git_only = {k: v for k, v in creds.items() if k in secrets.GIT_ENV}
        if git_only:
            objects.append(build_creds_secret(spec, git_only, name=spec.coordinator_creds_secret))
    objects += [build_coordinator_job(spec), build_worker_job(spec)]
    return objects


# --------------------------------------------------------------------------------------------
# Cluster operations — these touch the kubernetes client (deferred), and run the guard FIRST.
# Each mutating/reading op takes an injectable seam (applier/deleter/lister/...) so the whole
# control path is unit-testable with no cluster: tests inject a recorder and assert the objects.
# --------------------------------------------------------------------------------------------
Applier = Callable[[Sequence[dict]], None]


@dataclass
class RunSummary:
    """One run's at-a-glance state for `loopkit cloud ls` (derived from Job status)."""

    run_id: str
    namespace: str
    phase: str                                   # pending | running | complete | failed | unknown
    workers_active: int = 0
    workers_succeeded: int = 0
    workers_failed: int = 0


def _client_applier(kubeconfig, *, in_cluster: bool = False) -> Applier:
    """The real applier: create each manifest via the client, tolerating already-exists (409).

    Built-in kinds go through `utils.create_from_dict`. A `CiliumNetworkPolicy` is a **custom
    resource** — `create_from_dict` can't construct an API for `cilium.io/v2` (it builds
    `CiliumIoV2Api`, which doesn't exist) and would raise — so it's applied via `CustomObjectsApi`,
    **best-effort**: a cluster without the Cilium CRD (or any error on this optional hardening object)
    is logged and skipped, never failing the run behind the standard default-deny NetworkPolicy.
    """
    from kubernetes import client, utils                         # deferred ([cloud] extra)
    from kubernetes.client.exceptions import ApiException

    api = cloud.api_client(kubeconfig, in_cluster=in_cluster)

    def _apply_custom(obj: dict) -> None:
        group, _, version = obj["apiVersion"].partition("/")
        try:
            client.CustomObjectsApi(api).create_namespaced_custom_object(
                group, version, obj["metadata"]["namespace"], _CUSTOM_PLURALS[obj["kind"]], obj)
        except ApiException as exc:
            if exc.status != 409:                                # 409 = already exists (idempotent)
                log.warn("apply.custom_skipped", kind=obj["kind"], status=exc.status)
        except Exception as exc:                                 # noqa: BLE001 — CRD absent / mapping: skip
            log.warn("apply.custom_skipped", kind=obj["kind"], err=type(exc).__name__)

    def apply(objects: Sequence[dict]) -> None:
        for obj in objects:
            if obj.get("kind") in _CUSTOM_PLURALS:               # custom resource (the Cilium FQDN policy)
                _apply_custom(dict(obj))
                continue
            try:
                utils.create_from_dict(api, dict(obj))
            except utils.FailToCreateError as exc:               # multi-doc/list failures
                non_conflict = [e for e in exc.api_exceptions if getattr(e, "status", None) != 409]
                if non_conflict:
                    raise
            except ApiException as exc:                          # single-object already exists
                if exc.status != 409:
                    raise

    return apply


def create_run(spec: RunSpec, *, expected=None, kubeconfig=None, in_cluster: bool = False,
               creds: dict[str, str] | None = None, applier: Applier | None = None,
               deleter: Callable[[str], None] | None = None) -> str:
    """Build `ns/run-<id>` + Jobs and apply them — **after** the context-safety guard passes.

    The guard runs first and unconditionally (a wrong/unpinned context raises before a single object
    is created), so a run can never land on the wrong cluster. Returns the run namespace. Pass
    `applier` to record objects in a test; the default creates them via the client. `in_cluster=True`
    is the trigger path (CronJob/webhook pods authenticate with their ServiceAccount); the guard still
    runs, with the synthetic `in-cluster` context the manifests pin.

    **Partial-failure cleanup (G3):** a mid-apply error can leave a real credential Secret in a
    namespace with no Job to TTL-GC it (orphaned at rest). On any apply failure the run namespace is
    deleted (best-effort) before re-raising, so a failed submit never leaves a key behind.
    """
    cloud.check_context(cloud.current_context(kubeconfig, in_cluster=in_cluster),
                        expected)                                  # guard FIRST — fail-closed
    objects = build_run_objects(spec, creds=creds)
    apply = applier or _client_applier(kubeconfig, in_cluster=in_cluster)
    log.info("run.create", run=spec.run_id, ns=spec.namespace,
             mode=spec.mode, workers=spec.parallelism, adapter=spec.adapter, submitter=spec.submitter)
    try:
        apply(objects)
    except Exception:                                            # noqa: BLE001 — clean up, then re-raise
        log.error("run.create_failed", run=spec.run_id, ns=spec.namespace)
        try:
            (deleter or _client_deleter(kubeconfig, in_cluster=in_cluster))(spec.namespace)
        except Exception:                                        # noqa: BLE001 — best-effort teardown
            pass
        raise
    log.info("run.created", run=spec.run_id, ns=spec.namespace, objects=len(objects))
    return spec.namespace


def delete_run(run_id: str, *, expected=None, kubeconfig=None,
               namespace_prefix: str = "run", deleter: Callable[[str], None] | None = None) -> str:
    """Delete a run's namespace (and everything in it) — guard first. Returns the namespace."""
    cloud.check_context(cloud.current_context(kubeconfig), expected)   # guard FIRST
    namespace = f"{namespace_prefix}-{sanitize_run_id(run_id)}"
    remove = deleter or _client_deleter(kubeconfig)
    log.info("run.delete", run=run_id, ns=namespace)
    remove(namespace)
    log.info("run.deleted", run=run_id, ns=namespace)
    return namespace


def _client_deleter(kubeconfig, *, in_cluster: bool = False) -> Callable[[str], None]:
    from kubernetes import client
    from kubernetes.client.exceptions import ApiException

    core = client.CoreV1Api(cloud.api_client(kubeconfig, in_cluster=in_cluster))

    def remove(namespace: str) -> None:
        try:
            core.delete_namespace(namespace)
        except ApiException as exc:
            if exc.status != 404:                                # already gone is success
                raise

    return remove


def list_runs(*, kubeconfig=None,
              lister: Callable[[], list[RunSummary]] | None = None) -> list[RunSummary]:
    """List runs across `run-*` namespaces with their phase (read-only — no guard needed)."""
    fetch = lister or _client_lister(kubeconfig)
    return fetch()


def _phase_from_jobs(coordinator, worker) -> str:
    """Derive a run phase from the coordinator + worker Job statuses."""
    def status(job):
        return getattr(job, "status", None) if job else None
    cs, ws = status(coordinator), status(worker)
    if cs and getattr(cs, "failed", 0):
        return "failed"
    if cs and getattr(cs, "succeeded", 0):
        return "complete"
    if (ws and getattr(ws, "active", 0)) or (cs and getattr(cs, "active", 0)):
        return "running"
    if coordinator or worker:
        return "pending"
    return "unknown"


def _client_lister(kubeconfig) -> Callable[[], list[RunSummary]]:
    from kubernetes import client

    core = client.CoreV1Api(cloud.api_client(kubeconfig))
    batch = client.BatchV1Api(cloud.api_client(kubeconfig))

    def fetch() -> list[RunSummary]:
        namespaces = core.list_namespace(
            label_selector="app.kubernetes.io/part-of=loopkit,app.kubernetes.io/component=run")
        summaries: list[RunSummary] = []
        for ns in namespaces.items:
            name = ns.metadata.name
            run_id = (ns.metadata.labels or {}).get("loopkit.dev/run-id", name)
            jobs = {j.metadata.name: j for j in batch.list_namespaced_job(name).items}
            worker = jobs.get("worker")
            wstatus = getattr(worker, "status", None)
            summaries.append(RunSummary(
                run_id=run_id, namespace=name,
                phase=_phase_from_jobs(jobs.get("coordinator"), worker),
                workers_active=getattr(wstatus, "active", 0) or 0,
                workers_succeeded=getattr(wstatus, "succeeded", 0) or 0,
                workers_failed=getattr(wstatus, "failed", 0) or 0))
        return summaries

    return fetch


def run_status(run_id: str, *, kubeconfig=None, namespace_prefix: str = "run",
               getter: Callable[[str], RunSummary | None] | None = None) -> RunSummary | None:
    """One run's status (read-only). Returns None if the namespace is gone (GC'd / never created)."""
    namespace = f"{namespace_prefix}-{sanitize_run_id(run_id)}"
    fetch = getter or (lambda ns: next((s for s in _client_lister(kubeconfig)() if s.namespace == ns),
                                       None))
    return fetch(namespace)


def run_logs(run_id: str, *, kubeconfig=None, namespace_prefix: str = "run", role: str = "worker",
             tail_lines: int | None = None) -> str:
    """Concatenated logs of a run's pods (read-only). `role` = worker | coordinator."""
    from kubernetes import client

    namespace = f"{namespace_prefix}-{sanitize_run_id(run_id)}"
    core = client.CoreV1Api(cloud.api_client(kubeconfig))
    pods = core.list_namespaced_pod(namespace,
                                    label_selector=f"app.kubernetes.io/component={role}")
    chunks: list[str] = []
    for pod in pods.items:
        name = pod.metadata.name
        try:
            body = core.read_namespaced_pod_log(name, namespace, tail_lines=tail_lines)
        except Exception as exc:                                 # noqa: BLE001 — a pod with no logs yet
            body = f"(no logs: {type(exc).__name__})"
        chunks.append(f"===== {name} =====\n{body}")
    return "\n".join(chunks) if chunks else f"(no {role} pods in {namespace})"
