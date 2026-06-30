"""Per-submitter credential resolution — identity → Secret, projected into a run (Part III, Phase 5a).

Each engineer registers their keys once (`loopkit cloud creds set --as <eng>`) into a Secret in
`loopkit-system`. At run creation the submitter's identity selects their Secret and **only the run's
adapter key + git creds** are projected into the per-run Secret — so a `claude-code` run never carries
an OpenAI key, and a leak is bounded to the submitter's *own* key/budget. This is the single resolution
seam the three submit paths (CLI / CronJob / webhook) share; the Vault/ESO migration later swaps only
this resolver, nothing downstream.

Security properties baked in here:

- **Default-deny on triggers.** The registered Secret set *is* the admin-curated allowlist. An
  unregistered submitter resolves to nothing usable and (unless `allow_fleet_fallback` is explicitly
  set, never on the untrusted webhook path) gets no run — the fail-closed posture (C3).
- **Injective identity check (S4).** The Secret name is a lossy DNS-label of `(env, submitter)`, so two
  distinct submitters could collide on one name. Each Secret records its exact canonical `(submitter,
  env)`; `resolve` verifies recorded == requested before projecting, and refuses on a mismatch.
- **Guard-first mutations.** `set/delete_credential` run the context-safety guard before touching the
  cluster (same shape as `cloudrun`), with injectable seams so the whole path is token-free testable.

Deferred client: only the `_client_*` seams import `kubernetes` (the `[cloud]` extra); the pure logic
(naming, projection, the resolve decision tree) imports nothing heavy and is fully unit-testable.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Mapping, Protocol

from .. import secrets
from ..log import get_logger
from . import cloud
from .cloudrun import SYSTEM_NAMESPACE, _label_safe, sanitize_run_id

log = get_logger("creds")

DEFAULT_SUBMITTER = "fleet"               # the shared team key; used only via explicit fallback
SECRET_PREFIX = "loopkit-creds"

# Reserved Secret-data keys recording the exact canonical identity (valid k8s data keys, never an env
# var name, and filtered out by projection so they never reach a run). Used for the S4 injective check.
_SUBMITTER_KEY = "loopkit.submitter"
_ENV_KEY = "loopkit.env"
_RESERVED = (_SUBMITTER_KEY, _ENV_KEY)


# --------------------------------------------------------------------------------------------
# Identity + naming.
# --------------------------------------------------------------------------------------------
@dataclass
class Identity:
    """Who a run is for + what it needs: selects the source Secret and which key to project."""

    submitter: str
    env_name: str = "prod"
    adapter: str = "claude-code"


def secret_name(env_name: str, submitter: str) -> str:
    """The source Secret's name in `loopkit-system`: a DNS-label of `loopkit-creds-<env>-<submitter>`.

    Lossy (lowercase/collapse/truncate), so two submitters could collide — the recorded canonical
    identity + the resolve-time injective check (S4) make a collision fail-closed, not silently shared.
    """
    return sanitize_run_id(f"{SECRET_PREFIX}-{env_name}-{submitter}")


# --------------------------------------------------------------------------------------------
# Projection — only the adapter's key(s) + git creds reach a run (the blast-radius property).
# --------------------------------------------------------------------------------------------
def project(data: dict[str, str], adapter: str) -> dict[str, str]:
    """Keep only the keys `adapter` needs + git creds from a source bag; drop everything else.

    A `claude-code` run gets ANTHROPIC/OAUTH + git, never an OpenAI key — so a hijacked run can leak
    at most the one key it was issued, and the coordinator's git-only subset (built downstream) keeps
    the model key off the pod that handles untrusted issue text.
    """
    out: dict[str, str] = {}
    for name in (*secrets.ADAPTER_KEYS.get(adapter, ()), *secrets.GIT_ENV):
        value = data.get(name)
        if value:
            out[name] = value
    return out


# --------------------------------------------------------------------------------------------
# Resolver — the seam Vault later replaces. `reader(name, namespace) -> data|None` is injectable.
# --------------------------------------------------------------------------------------------
SecretReader = Callable[[str, str], "dict[str, str] | None"]


@dataclass
class ResolvedCreds:
    """The outcome of a resolution: the projected creds + where they came from (for policy + logging)."""

    data: dict[str, str]
    source: str                              # submitter | fleet | none

    @property
    def usable(self) -> bool:
        # "A registration was found" (the submitter's own, or an allowed fleet fallback). The webhook
        # authorizes on this; whether the projected key actually fits the run's adapter is the worker's
        # fail-closed check at load (G7) — a clearer, attributable failure than refusing here.
        return self.source != "none"


@dataclass
class SecretResolver:
    reader: SecretReader | None = None
    namespace: str = SYSTEM_NAMESPACE
    default_submitter: str = DEFAULT_SUBMITTER

    def resolve(self, identity: Identity, *, allow_fleet_fallback: bool = False) -> ResolvedCreds:
        """Resolve `identity` → projected creds. The submitter's registered Secret is authoritative
        (the allowlist); absent it, fall back to the `fleet` default **only if explicitly allowed**
        (never on the untrusted webhook path), else return nothing usable (default-deny)."""
        own = self._fetch(identity.submitter, identity.env_name)
        if own is not None:
            log.info("creds.resolved", submitter=_label_safe(identity.submitter),
                     env=identity.env_name, adapter=identity.adapter, source="submitter")
            return ResolvedCreds(project(own, identity.adapter), source="submitter")
        if not allow_fleet_fallback:
            log.warn("creds.unresolved", submitter=_label_safe(identity.submitter),
                     env=identity.env_name, reason="no_registered_key")
            return ResolvedCreds({}, source="none")
        fleet = self._fetch(self.default_submitter, identity.env_name)
        if fleet is not None:
            log.warn("creds.fleet_fallback", submitter=_label_safe(identity.submitter),
                     env=identity.env_name, adapter=identity.adapter)
            return ResolvedCreds(project(fleet, identity.adapter), source="fleet")
        log.warn("creds.unresolved", submitter=_label_safe(identity.submitter),
                 env=identity.env_name, reason="no_fleet_default")
        return ResolvedCreds({}, source="none")

    def _fetch(self, submitter: str, env_name: str) -> dict[str, str] | None:
        read = self.reader or _client_secret_reader(None)
        data = read(secret_name(env_name, submitter), self.namespace)
        if data is None:
            return None
        # S4: the recorded canonical identity must match what we asked for (a sanitize collision, or a
        # tampered Secret, resolves to a *different* engineer's name — refuse rather than mis-spend).
        if data.get(_SUBMITTER_KEY) != submitter or data.get(_ENV_KEY) != env_name:
            log.warn("creds.identity_mismatch", requested=_label_safe(submitter), env=env_name,
                     recorded=_label_safe(data.get(_SUBMITTER_KEY) or "-"))
            return None
        return data


def resolve_for_run(identity: Identity, *, allow_fleet_fallback: bool = False,
                    kubeconfig=None, in_cluster: bool = False,
                    reader: SecretReader | None = None) -> ResolvedCreds:
    """Convenience seam the CLI/webhook/cron call: build a `SecretResolver` (real or injected) + resolve."""
    resolver = SecretResolver(reader=reader or _client_secret_reader(kubeconfig, in_cluster=in_cluster))
    return resolver.resolve(identity, allow_fleet_fallback=allow_fleet_fallback)


# --------------------------------------------------------------------------------------------
# Run-credential decision — the Phase-5a fallback POLICY, typer-free so the submit path just renders
# it (no CLI dependency here, so the whole decision tree is unit-testable with an injected reader).
# --------------------------------------------------------------------------------------------
def creds_from_env(env: Mapping[str, str]) -> dict[str, str]:
    """The agent + git credentials present in a process environment (the `--from-env` escape hatch)."""
    return {var: env[var] for var in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GITHUB_TOKEN", "GH_TOKEN")
            if env.get(var)}


def resolve_submitter(explicit: str | None, env: Mapping[str, str]) -> str:
    """Who a run is for: explicit `--as` → `$LOOPKIT_SUBMITTER` → the shared `fleet` default."""
    return explicit or env.get("LOOPKIT_SUBMITTER") or DEFAULT_SUBMITTER


@dataclass(frozen=True)
class RunCredsDecision:
    """How a run's credentials resolve, for the submit path (CLI/cron/webhook) to render.

    `outcome`:
      - `resolved`            — `.data`/`.source` are ready to use.
      - `needs_fleet_consent` — only the shared `fleet` key is available; the caller must obtain
                                operator consent before using `.fleet_data` (a shared key isn't
                                attributable, so this layer never grants it silently).
      - `refused`             — no submitter key and no fleet default; exit with `.message`.
    """

    outcome: str
    data: dict[str, str]
    source: str                              # mock | from-env | submitter | (empty until consented)
    submitter: str = ""
    fleet_data: dict[str, str] = field(default_factory=dict)
    message: str = ""


def decide_run_creds(adapter: str, submitter: str, env_name: str, *, from_env: bool,
                     env: Mapping[str, str], kubeconfig=None, in_cluster: bool = False,
                     reader: SecretReader | None = None) -> RunCredsDecision:
    """Classify how a run's credentials resolve — the Phase-5a policy, with no CLI/typer dependency.

    `mock` needs none; `--from-env` takes (projected) keys from `env`; otherwise the submitter's own
    registered key wins, and only if it's absent does the shared `fleet` key come into play — but as a
    `needs_fleet_consent` outcome the caller must explicitly grant (the fail-closed posture). Inject
    `reader` to unit-test the whole tree without a cluster.
    """
    if adapter == "mock":
        return RunCredsDecision("resolved", {}, "mock")
    if from_env:
        return RunCredsDecision("resolved", project(creds_from_env(env), adapter), "from-env")
    ident = Identity(submitter, env_name, adapter)
    own = resolve_for_run(ident, allow_fleet_fallback=False, kubeconfig=kubeconfig,
                          in_cluster=in_cluster, reader=reader)
    if own.source == "submitter":
        return RunCredsDecision("resolved", own.data, "submitter")
    fleet = resolve_for_run(ident, allow_fleet_fallback=True, kubeconfig=kubeconfig,
                            in_cluster=in_cluster, reader=reader)
    if fleet.source == "fleet":
        return RunCredsDecision("needs_fleet_consent", {}, "", submitter=submitter, fleet_data=fleet.data)
    return RunCredsDecision("refused", {}, "", submitter=submitter,
                            message=f"no credentials for submitter '{submitter}' and no fleet default. "
                                    "Register one: loopkit cloud creds set --as <you> --adapter <adapter>.")


# --------------------------------------------------------------------------------------------
# Registration — `loopkit cloud creds set/ls/rm`. Guard-first, injectable seams.
# --------------------------------------------------------------------------------------------
def build_credential_secret(submitter: str, env_name: str, data: dict[str, str],
                            *, namespace: str = SYSTEM_NAMESPACE) -> dict:
    """A source Secret in `loopkit-system` holding `data` + the reserved canonical-identity keys."""
    payload = {**data, _SUBMITTER_KEY: submitter, _ENV_KEY: env_name}
    return {"apiVersion": "v1", "kind": "Secret", "type": "Opaque",
            "metadata": {"name": secret_name(env_name, submitter), "namespace": namespace,
                         "labels": {"app.kubernetes.io/part-of": "loopkit",
                                    "app.kubernetes.io/component": "creds",
                                    "loopkit.dev/submitter": _label_safe(submitter),
                                    "loopkit.dev/env": _label_safe(env_name)}},
            "stringData": payload}


SecretWriter = Callable[[dict], None]


def set_credential(submitter: str, data: dict[str, str], *, env_name: str = "prod",
                   expected=None, kubeconfig=None, writer: SecretWriter | None = None) -> str:
    """Create-or-merge a submitter's source Secret — **after** the context guard. Returns its name.

    Merge semantics (patch on conflict) let `creds set --adapter claude` then `--adapter openai`
    accumulate keys into one Secret, and a re-set rotate a key. Never accepts a key as an argv (the CLI
    reads env/stdin), so a value never lands in shell history / `ps`.
    """
    cloud.check_context(cloud.current_context(kubeconfig), expected)   # guard FIRST — fail-closed
    obj = build_credential_secret(submitter, env_name, data)
    write = writer or _client_secret_writer(kubeconfig, SYSTEM_NAMESPACE)
    log.info("creds.set", submitter=_label_safe(submitter), env=env_name,
             keys=",".join(sorted(data)) or "-")          # NAMES only — never values
    write(obj)
    return obj["metadata"]["name"]


@dataclass
class CredentialSummary:
    """One registered submitter's at-a-glance state for `loopkit cloud creds ls` (no values, ever)."""

    submitter: str
    env_name: str
    keys: list[str] = field(default_factory=list)         # key NAMES present, never the secret values


def list_credentials(*, kubeconfig=None, namespace: str = SYSTEM_NAMESPACE,
                     lister: Callable[[], list[CredentialSummary]] | None = None) -> list[CredentialSummary]:
    """List registered submitters (read-only — no guard needed). Shows key *names*, never values."""
    fetch = lister or _client_creds_lister(kubeconfig, namespace)
    return fetch()


def delete_credential(submitter: str, *, env_name: str = "prod", expected=None, kubeconfig=None,
                      namespace: str = SYSTEM_NAMESPACE,
                      deleter: Callable[[str], None] | None = None) -> str:
    """Delete a submitter's source Secret — guard first. Returns the Secret name."""
    cloud.check_context(cloud.current_context(kubeconfig), expected)   # guard FIRST
    name = secret_name(env_name, submitter)
    remove = deleter or _client_secret_deleter(kubeconfig, namespace)
    log.info("creds.delete", submitter=_label_safe(submitter), env=env_name)
    remove(name)
    return name


def visible_keys(data: dict[str, str]) -> list[str]:
    """The non-reserved key names in a source Secret (what `creds ls` shows)."""
    return sorted(k for k in data if k not in _RESERVED)


# --------------------------------------------------------------------------------------------
# Client seams (deferred `kubernetes` import — the `[cloud]` extra).
# --------------------------------------------------------------------------------------------
def _client_secret_reader(kubeconfig, *, in_cluster: bool = False) -> SecretReader:
    import base64

    from kubernetes import client
    from kubernetes.client.exceptions import ApiException

    core = client.CoreV1Api(cloud.api_client(kubeconfig, in_cluster=in_cluster))

    def read(name: str, namespace: str) -> dict[str, str] | None:
        try:
            sec = core.read_namespaced_secret(name, namespace)
        except ApiException as exc:
            if exc.status == 404:
                return None
            raise
        return {k: base64.b64decode(v).decode() for k, v in (sec.data or {}).items()}

    return read


def _client_secret_writer(kubeconfig, namespace: str, *, in_cluster: bool = False) -> SecretWriter:
    from kubernetes import client
    from kubernetes.client.exceptions import ApiException

    core = client.CoreV1Api(cloud.api_client(kubeconfig, in_cluster=in_cluster))

    def write(obj: dict) -> None:
        name = obj["metadata"]["name"]
        try:
            core.create_namespaced_secret(namespace, obj)
        except ApiException as exc:
            if exc.status != 409:                            # already exists → merge (accumulate/rotate)
                raise
            core.patch_namespaced_secret(name, namespace, obj)

    return write


def _client_secret_deleter(kubeconfig, namespace: str) -> Callable[[str], None]:
    from kubernetes import client
    from kubernetes.client.exceptions import ApiException

    core = client.CoreV1Api(cloud.api_client(kubeconfig))

    def remove(name: str) -> None:
        try:
            core.delete_namespaced_secret(name, namespace)
        except ApiException as exc:
            if exc.status != 404:                            # already gone is success
                raise

    return remove


def _client_creds_lister(kubeconfig, namespace: str) -> Callable[[], list[CredentialSummary]]:
    import base64

    from kubernetes import client

    core = client.CoreV1Api(cloud.api_client(kubeconfig))

    def fetch() -> list[CredentialSummary]:
        secs = core.list_namespaced_secret(
            namespace, label_selector="app.kubernetes.io/component=creds")
        out: list[CredentialSummary] = []
        for sec in secs.items:
            data = {k: base64.b64decode(v).decode() for k, v in (sec.data or {}).items()}
            out.append(CredentialSummary(
                submitter=data.get(_SUBMITTER_KEY, "-"),
                env_name=data.get(_ENV_KEY, "-"),
                keys=visible_keys(data)))
        return out

    return fetch
