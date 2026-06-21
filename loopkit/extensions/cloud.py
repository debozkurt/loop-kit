"""The cloud control plane — `loopkit cloud` talking to a managed Kubernetes cluster (Part III).

This is the laptop/CI-side client and the in-cluster control path for the DOKS target described in
[`docs/architecture/02-cloud-architecture.md`]. Phase 2 lands its *foundation*: the
**context-safety guard** (the non-negotiable that keeps a production cluster from being mutated by
accident) and `bootstrap` (apply the `ns/loopkit-system` infra: Redis, RBAC, NetworkPolicy). The
per-run mechanics (`create_run()`, ls/status/logs/kill) arrive in Phase 3 and attach here.

Three load-bearing properties, each a project invariant:

- **Deferred, optional dependency.** The official `kubernetes` client is behind the `[cloud]` extra.
  Importing *this module* must never import `kubernetes` (so `pip install loopkit` and the core CLI
  load without it) — every `import kubernetes` lives **inside** a function. The pure context-check
  logic (`check_context`, `resolve_expected`) has no dependency at all and is unit-testable with no
  cluster and no client installed.
- **Fail-closed context safety.** A managed cloud context is production-sensitive (the global
  kubectl-safety rule). Mirroring the dev `Tiltfile`'s `allow_k8s_contexts(...)` + `fail()`, every
  *mutating* entry point pins an expected context and **refuses** to act on any other — and refuses
  if *no* context is pinned at all. The expected context is never inferred from the ambient
  current-context; it must be declared (flag or `LOOPKIT_CLOUD_CONTEXT`), so "wrong cluster" is a
  hard stop, not a silent success.
- **Host kubeconfig stays untouched.** Like the `Makefile`'s repo-local `KUBECONFIG`, the cloud
  flow reads a kubeconfig you point it at (a repo-local `.kube/loopkit-cloud.yaml`), never writing
  to or merging into the user's `~/.kube/config`.

See [`docs/architecture/04-security.md`](../../docs/architecture/04-security.md) → *Control-plane /
kubectl safety* for why this is the first control, not the last.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from ..log import get_logger

log = get_logger("cloud")

# The env var that pins the expected cloud context (a single name, or a comma-separated allowlist).
# Fail-closed: with nothing here and no explicit --context, the guard refuses to mutate anything.
ENV_CONTEXT = "LOOPKIT_CLOUD_CONTEXT"

# The synthetic "context" name for the in-cluster control path (Phase-4 CronJob + webhook listener).
# A pod has no kubeconfig context; instead it authenticates with its ServiceAccount via
# `load_incluster_config()`, which only succeeds *inside* a real pod. So we treat that success as the
# in-cluster identity and report this name as the current context — the guard is then unchanged: it
# still refuses unless this name is explicitly pinned (manifests set `LOOPKIT_CLOUD_CONTEXT=in-cluster`).
# This keeps the guard fail-closed and impossible to spoof from a laptop (where load_incluster fails).
IN_CLUSTER_CONTEXT = "in-cluster"

# Where the system manifests live (applied by `bootstrap`). Resolved relative to the repo root so it
# works from a checkout; overridable by callers/tests.
DEFAULT_MANIFEST_DIR = Path(__file__).resolve().parents[2] / "k8s" / "cloud"


class ContextError(RuntimeError):
    """The active kube context is not the pinned cloud context (or none is pinned). Hard stop.

    Raised before any mutating cluster call. The message names the actual vs expected context so the
    operator can see exactly why the command refused — the cloud-scale analogue of the Tiltfile's
    `fail("refusing to run: expected context '…', got '…'")`.
    """


# --------------------------------------------------------------------------------------------
# The context guard — pure logic, no kubernetes dependency, fully unit-testable.
# --------------------------------------------------------------------------------------------
def resolve_expected(explicit: str | Sequence[str] | None = None) -> list[str]:
    """The allowlist of acceptable contexts: explicit arg wins, else `LOOPKIT_CLOUD_CONTEXT`.

    Returns a list (a single pin is just a one-element allowlist). An empty list means *nothing is
    pinned* — the fail-closed case the guard refuses on. Both sources accept a comma-separated list
    so a few sibling contexts (e.g. `do-nyc1-loopkit-prod,do-nyc1-loopkit-stg`) can be permitted.
    """
    if explicit is None:
        explicit = os.environ.get(ENV_CONTEXT, "")
    if isinstance(explicit, str):
        names = explicit.split(",")
    else:
        names = list(explicit)
    return [n.strip() for n in names if n and n.strip()]


def check_context(current: str | None, expected: str | Sequence[str] | None) -> str:
    """Raise `ContextError` unless `current` is one of the pinned `expected` contexts.

    Pure and side-effect-free (no cluster, no client) so the safety property is exhaustively
    unit-testable. Fail-closed twice over: an empty allowlist (nothing pinned) is refused, and a
    `current` outside the allowlist is refused. Returns the validated context name on success so
    callers can log/echo it.
    """
    allowed = resolve_expected(expected)
    if not allowed:
        raise ContextError(
            "no expected cloud context is pinned — refusing to act on the ambient context. "
            f"Set ${ENV_CONTEXT} or pass --context=<doks-context> (e.g. do-nyc1-loopkit-prod)."
        )
    if not current:
        raise ContextError(
            f"no active kube context; expected one of {allowed}. "
            "Point KUBECONFIG at the cloud kubeconfig (e.g. .kube/loopkit-cloud.yaml)."
        )
    if current not in allowed:
        raise ContextError(
            f"refusing to act on context {current!r}; expected one of {allowed}. "
            "This guard prevents a mutating command from landing on the wrong cluster "
            "(the global kubectl-safety rule). Switch context or pass --context explicitly."
        )
    return current


# --------------------------------------------------------------------------------------------
# Cluster access — these defer-import the kubernetes client (the `[cloud]` extra).
# --------------------------------------------------------------------------------------------
def current_context(kubeconfig: str | os.PathLike[str] | None = None, *,
                    in_cluster: bool = False) -> str | None:
    """The active context name from a kubeconfig file, or None if there is no active context.

    Deferred import: `kubernetes` is only needed here and in `api_client`/`bootstrap`, so importing
    this module stays dependency-free. We read the *named* contexts (never `load_incluster_config`)
    because the guard is about which laptop/CI kubeconfig context a human is pointed at.

    `in_cluster=True` (the Phase-4 CronJob + webhook path) reports `IN_CLUSTER_CONTEXT` *after*
    proving we really are in a pod — `load_incluster_config()` reads the mounted SA token and raises
    off-cluster. So a laptop can never claim the in-cluster identity, and the guard still refuses
    unless `in-cluster` is the pinned context.
    """
    from kubernetes import config  # deferred — only the cloud path needs the client

    if in_cluster:
        try:
            config.load_incluster_config()           # proves we're in a pod (SA token present)
        except config.config_exception.ConfigException as exc:
            raise ContextError(
                f"--in-cluster set but not running in a cluster (no ServiceAccount token): {exc}"
            ) from exc
        return IN_CLUSTER_CONTEXT
    try:
        _contexts, active = config.list_kube_config_contexts(
            config_file=str(kubeconfig) if kubeconfig else None
        )
    except config.config_exception.ConfigException as exc:
        raise ContextError(f"could not read kubeconfig: {exc}") from exc
    return active.get("name") if active else None


def api_client(kubeconfig: str | os.PathLike[str] | None = None, *, in_cluster: bool = False):
    """Build an `ApiClient` for the cluster — kubeconfig (laptop/CI) or in-cluster SA (trigger pods).

    Cloud-agnostic by construction — the client speaks the k8s API, identical across DOKS/EKS/GKE/
    kind. `in_cluster=True` is the Phase-4 control path: the webhook listener + CronJob pods load
    their ServiceAccount credentials with `load_incluster_config()` instead of a kubeconfig, so a
    submission never depends on one engineer's machine.
    """
    from kubernetes import client, config  # deferred

    if in_cluster:
        config.load_incluster_config()
    else:
        config.load_kube_config(config_file=str(kubeconfig) if kubeconfig else None)
    return client.ApiClient()


@dataclass
class BootstrapResult:
    """What `bootstrap` did: the validated context and the manifest files it applied."""

    context: str
    applied: list[str]


def bootstrap(
    *,
    expected: str | Sequence[str] | None = None,
    kubeconfig: str | os.PathLike[str] | None = None,
    manifest_dir: str | os.PathLike[str] | None = None,
) -> BootstrapResult:
    """Apply the `ns/loopkit-system` foundation manifests — **after** the context guard passes.

    The guard runs first and unconditionally, so a misconfigured invocation can never apply infra to
    the wrong cluster. Apply is idempotent: a manifest that already exists (HTTP 409 Conflict) is
    treated as success, so re-running `bootstrap` converges rather than erroring. Uses the Python
    client's `create_from_yaml` so no `kubectl` binary is required on the control plane.
    """
    from kubernetes import utils  # deferred
    from kubernetes.client.exceptions import ApiException

    ctx = check_context(current_context(kubeconfig), expected)
    directory = Path(manifest_dir or DEFAULT_MANIFEST_DIR)
    files = sorted(p for p in directory.glob("*.yaml"))
    if not files:
        raise FileNotFoundError(f"no manifests found in {directory}")

    log.info("bootstrap start", context=ctx, manifests=len(files))
    api = api_client(kubeconfig)
    applied: list[str] = []
    for path in files:
        try:
            utils.create_from_yaml(api, str(path))
            log.info("applied", file=path.name)
        except utils.FailToCreateError as exc:
            # create_from_yaml batches a multi-doc file; tolerate the already-exists case so
            # bootstrap is idempotent, but surface any other failure (RBAC denied, bad spec, …).
            non_conflict = [e for e in exc.api_exceptions if getattr(e, "status", None) != 409]
            if non_conflict:
                log.error("apply failed", file=path.name, errors=len(non_conflict))
                raise
            log.info("already present", file=path.name)
        except ApiException as exc:  # a single-object file that already exists
            if exc.status != 409:
                log.error("apply failed", file=path.name, status=exc.status)
                raise
            log.info("already present", file=path.name)
        applied.append(path.name)
    log.info("bootstrap done", context=ctx, applied=len(applied))
    return BootstrapResult(context=ctx, applied=applied)
