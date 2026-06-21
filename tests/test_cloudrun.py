"""Phase 3 — per-run mechanics: the run topology builders + the guarded create/delete control path.

Everything here runs with no cluster and no tokens. The `build_*` functions are pure (manifest
dicts), so the whole topology is asserted directly — parallelism, the sentinel-drain command, the
per-run keyspace, emptyDir scratch, least-privilege SA, default-deny network. `create_run`/
`delete_run` are exercised through an injected applier/deleter with the context guard monkeypatched
to a pinned context, proving the guard runs *before* any object is touched (and refuses the wrong
context). The sentinel shutdown mechanic itself is tested in test_fleet.py.
"""
from __future__ import annotations

import types

import pytest

from loopkit.extensions import cloud, cloudrun


# --------------------------------------------------------------------------------------------
# RunSpec — identity, keyspace, parallelism, validation.
# --------------------------------------------------------------------------------------------
def test_run_id_is_sanitized_to_a_dns_label():
    spec = cloudrun.RunSpec(run_id="Nightly Issues!", image="img", target="t", goal="g")
    assert spec.run_id == "nightly-issues"
    assert spec.namespace == "run-nightly-issues"
    assert spec.redis_namespace == spec.namespace          # per-run keyspace = the run namespace


def test_sanitize_run_id_rejects_empty_after_cleaning():
    with pytest.raises(ValueError):
        cloudrun.sanitize_run_id("!!!")


def test_parallelism_is_workers_for_fanout_and_population_for_evolve():
    fan = cloudrun.RunSpec(run_id="a", image="i", target="t", goal="g", workers=3)
    assert fan.parallelism == 3
    evo = cloudrun.RunSpec(run_id="b", image="i", target="t", mode="evolve", population=5)
    assert evo.parallelism == 5                            # population, not workers


def test_fanout_requires_goal_or_issues():
    with pytest.raises(ValueError, match="goal or --from-issues"):
        cloudrun.RunSpec(run_id="a", image="i", target="t")     # no goal, no issues
    with pytest.raises(ValueError, match="fanout' or 'evolve"):
        cloudrun.RunSpec(run_id="a", image="i", target="t", goal="g", mode="bogus")


# --------------------------------------------------------------------------------------------
# Command builders — the sentinel-drain coordinator + the per-run-keyspace worker.
# --------------------------------------------------------------------------------------------
def test_coordinator_drains_exactly_parallelism_workers_for_fanout():
    spec = cloudrun.RunSpec(run_id="a", image="i", target="t", goal="fix it", workers=4)
    cmd = cloudrun.coordinator_command(spec)
    assert cmd[:2] == ["fleet", "run"]
    assert "--drain-workers" in cmd and cmd[cmd.index("--drain-workers") + 1] == "4"
    assert cmd[cmd.index("--tasks") + 1] == "4"
    assert cmd[cmd.index("--goal") + 1] == "fix it"
    assert cmd[cmd.index("--redis-namespace") + 1] == spec.namespace


def test_coordinator_from_issues_passes_target_and_label():
    spec = cloudrun.RunSpec(run_id="a", image="i", target="https://x/repo",
                            from_issues=True, label="loopkit", workers=2)
    cmd = cloudrun.coordinator_command(spec)
    assert "--from-issues" in cmd
    assert cmd[cmd.index("--target") + 1] == "https://x/repo"
    assert cmd[cmd.index("--label") + 1] == "loopkit"
    assert "--provider" not in cmd                          # auto (default) is left to detect_provider


def test_coordinator_from_issues_forces_a_non_auto_provider():
    spec = cloudrun.RunSpec(run_id="a", image="i", target="https://git.acme.internal/repo",
                            from_issues=True, provider="gitlab")
    cmd = cloudrun.coordinator_command(spec)
    assert cmd[cmd.index("--provider") + 1] == "gitlab"     # self-hosted GitLab the URL can't auto-detect


def test_coordinator_evolve_uses_population_for_drain_and_g_p_k():
    spec = cloudrun.RunSpec(run_id="a", image="i", target="t", mode="evolve",
                            generations=3, population=5, keep=2)
    cmd = cloudrun.coordinator_command(spec)
    assert cmd[:2] == ["fleet", "evolve"]
    assert cmd[cmd.index("--drain-workers") + 1] == "5"    # population, the worker pod count
    assert cmd[cmd.index("-g") + 1] == "3" and cmd[cmd.index("-p") + 1] == "5"


def test_worker_command_targets_the_per_run_keyspace():
    spec = cloudrun.RunSpec(run_id="a", image="i", target="repo", adapter="claude-api", goal="g")
    cmd = cloudrun.worker_command(spec)
    assert cmd[:2] == ["fleet", "worker"]
    assert cmd[cmd.index("--redis-namespace") + 1] == spec.namespace
    assert cmd[cmd.index("--target") + 1] == "repo"
    assert cmd[cmd.index("--adapter") + 1] == "claude-api"


# --------------------------------------------------------------------------------------------
# Object builders — the worker Job is the work-queue pattern; the network is default-deny.
# --------------------------------------------------------------------------------------------
def test_worker_job_is_the_fine_grained_work_queue_pattern():
    spec = cloudrun.RunSpec(run_id="a", image="ghcr.io/me/w:1", target="t", goal="g", workers=3)
    job = cloudrun.build_worker_job(spec)
    jspec = job["spec"]
    assert jspec["parallelism"] == 3
    assert "completions" not in jspec                      # unset => drain-the-queue work-queue Job
    assert jspec["ttlSecondsAfterFinished"] == spec.ttl_seconds
    assert jspec["backoffLimit"] == spec.backoff_limit
    pod = jspec["template"]["spec"]
    assert pod["restartPolicy"] == "Never"
    assert pod["serviceAccountName"] == "loopkit-worker"
    assert pod["automountServiceAccountToken"] is False
    assert pod["imagePullSecrets"] == [{"name": "ghcr-pull"}]


def test_worker_pod_has_emptydir_scratch_with_a_size_limit():
    spec = cloudrun.RunSpec(run_id="a", image="i", target="t", goal="g", scratch_size="3Gi")
    pod = cloudrun.build_worker_job(spec)["spec"]["template"]["spec"]
    scratch = next(v for v in pod["volumes"] if v["name"] == "scratch")
    assert scratch == {"name": "scratch", "emptyDir": {"sizeLimit": "3Gi"}}   # no PVC — durability via git push


def test_coordinator_job_has_no_parallelism_and_no_scratch():
    spec = cloudrun.RunSpec(run_id="a", image="i", target="t", goal="g")
    job = cloudrun.build_coordinator_job(spec)
    pod = job["spec"]["template"]["spec"]
    assert "parallelism" not in job["spec"]                # a single coordinator completion
    assert not any(v["name"] == "scratch" for v in pod["volumes"])   # no clone scratch (talks to Redis/gh)


# --------------------------------------------------------------------------------------------
# Phase 6 — the worker is a two-container split: loopkit-core holds the key (envFrom), the agent's
# untrusted surface runs in a keyless, different-uid executor sidecar with no credential anywhere.
# --------------------------------------------------------------------------------------------
def test_loopkit_core_gets_creds_via_envfrom_and_dispatches_to_the_executor():
    spec = cloudrun.RunSpec(run_id="a", image="i", target="t", goal="g")
    pod = cloudrun.build_worker_job(spec)["spec"]["template"]["spec"]
    core = next(c for c in pod["containers"] if c["name"] == "loopkit-core")
    assert core["envFrom"] == [{"secretRef": {"name": "loopkit-creds", "optional": True}}]
    env = {e["name"]: e.get("value") for e in core["env"]}
    assert env[cloudrun.CREDS_EXPECTED_ENV] == "1"         # fail-closed marker (not a credential var)
    assert "--executor-socket" in core["args"]             # the untrusted surface is dispatched away


def test_executor_sidecar_is_a_keyless_native_sidecar_with_a_different_uid():
    spec = cloudrun.RunSpec(run_id="a", image="i", target="t", goal="g")
    pod = cloudrun.build_worker_job(spec)["spec"]["template"]["spec"]
    execs = [c for c in pod.get("initContainers", []) if c["name"] == "executor"]
    assert len(execs) == 1
    ex = execs[0]
    assert ex["restartPolicy"] == "Always"                 # native sidecar (k8s >= 1.29)
    assert ex["args"] == cloudrun.executor_command()
    # No credential reaches the executor: no envFrom, no secret-backed env.
    assert "envFrom" not in ex
    assert not any(e.get("valueFrom", {}).get("secretKeyRef") for e in ex.get("env", []))
    assert ex["securityContext"]["runAsUser"] == cloudrun.EXECUTOR_UID
    assert ex["securityContext"]["runAsUser"] != pod["securityContext"]["runAsUser"]  # ≠ loopkit-core
    # Shares only the workspace + the socket dir (so dispatched paths resolve) — nothing else.
    mounts = {m["name"] for m in ex["volumeMounts"]}
    assert {"scratch", "exec-socket"} <= mounts


def test_no_secret_is_mounted_as_a_file_anywhere_in_the_worker_pod():
    # Belt and braces: the Secret is delivered ONLY via loopkit-core's envFrom, never as a volume —
    # so there is no readOnly mount for a hijacked agent to `cat` (and the executor has no env either).
    spec = cloudrun.RunSpec(run_id="a", image="i", target="t", goal="g")
    pod = cloudrun.build_worker_job(spec)["spec"]["template"]["spec"]
    assert not any("secret" in v for v in pod["volumes"])
    ex = next(c for c in pod["initContainers"] if c["name"] == "executor")
    assert "envFrom" not in ex


def test_pod_and_both_containers_are_hardened():
    spec = cloudrun.RunSpec(run_id="a", image="i", target="t", goal="g")
    pod = cloudrun.build_worker_job(spec)["spec"]["template"]["spec"]
    assert pod["securityContext"]["runAsNonRoot"] is True and pod["securityContext"]["runAsUser"] == 1000
    for c in [pod["containers"][0], pod["initContainers"][0]]:        # loopkit-core + the executor
        sc = c["securityContext"]
        assert sc["allowPrivilegeEscalation"] is False and sc["readOnlyRootFilesystem"] is True
        assert sc["capabilities"]["drop"] == ["ALL"]
    assert pod["automountServiceAccountToken"] is False    # workers get no cluster-API token


def test_coordinator_secret_is_git_only_worker_secret_is_full():
    spec = cloudrun.RunSpec(run_id="a", image="i", target="t", goal="g")
    objs = cloudrun.build_run_objects(spec, creds={"ANTHROPIC_API_KEY": "sk", "GITHUB_TOKEN": "ghp"})
    by_name = {o["metadata"]["name"]: o for o in objs if o["kind"] == "Secret"}
    assert set(by_name["loopkit-creds"]["stringData"]) == {"ANTHROPIC_API_KEY", "GITHUB_TOKEN"}
    assert set(by_name["loopkit-creds-coord"]["stringData"]) == {"GITHUB_TOKEN"}   # git only — no model key
    coord = cloudrun.build_coordinator_job(spec)["spec"]["template"]["spec"]
    core = coord["containers"][0]
    assert core["envFrom"] == [{"secretRef": {"name": "loopkit-creds-coord", "optional": True}}]
    assert "initContainers" not in coord                   # the coordinator runs no untrusted command


def test_network_policy_is_default_deny_with_a_tight_egress_allowlist():
    spec = cloudrun.RunSpec(run_id="a", image="i", target="t", goal="g")
    np = cloudrun.build_network_policy(spec)["spec"]
    assert np["podSelector"] == {} and set(np["policyTypes"]) == {"Ingress", "Egress"}
    assert "ingress" not in np                             # deny all inbound
    egress = np["egress"]
    # DNS (53), Redis (6379), and HTTPS (443) — and HTTPS blocks the metadata range.
    ports = {p["port"] for rule in egress for p in rule.get("ports", [])}
    assert {53, 6379, 443} <= ports
    https = next(r for r in egress if any(p["port"] == 443 for p in r["ports"]))
    assert https["to"][0]["ipBlock"]["except"] == ["169.254.0.0/16"]


def test_build_run_objects_order_and_conditional_secret():
    spec = cloudrun.RunSpec(run_id="a", image="i", target="t", goal="g")
    without = [o["kind"] for o in cloudrun.build_run_objects(spec)]
    assert without == ["Namespace", "ServiceAccount", "ResourceQuota", "LimitRange",
                       "NetworkPolicy", "CiliumNetworkPolicy", "Job", "Job"]   # secret omitted, no creds
    withc = [o["kind"] for o in cloudrun.build_run_objects(spec, creds={"ANTHROPIC_API_KEY": "x"})]
    assert "Secret" in withc and withc.index("Secret") < withc.index("Job")
    no_fqdn = cloudrun.RunSpec(run_id="a", image="i", target="t", goal="g", fqdn_egress=False)
    assert "CiliumNetworkPolicy" not in [o["kind"] for o in cloudrun.build_run_objects(no_fqdn)]


def test_fqdn_egress_policy_allowlists_named_hosts_on_443():
    pol = cloudrun.build_fqdn_egress_policy(cloudrun.RunSpec(run_id="a", image="i", target="t", goal="g"))
    assert pol["kind"] == "CiliumNetworkPolicy"
    fqdn_rule = next(e for e in pol["spec"]["egress"] if "toFQDNs" in e)
    names = {r.get("matchName") or r.get("matchPattern") for r in fqdn_rule["toFQDNs"]}
    assert {"api.anthropic.com", "github.com", "ghcr.io"} <= names    # the agent API, the forge, the registry
    assert {p["port"] for p in fqdn_rule["toPorts"][0]["ports"]} == {"443"}


def test_worker_and_executor_share_the_same_socket_path():
    spec = cloudrun.RunSpec(run_id="a", image="i", target="t", goal="g")
    pod = cloudrun.build_worker_job(spec)["spec"]["template"]["spec"]
    core = next(c for c in pod["containers"] if c["name"] == "loopkit-core")
    assert core["args"][core["args"].index("--executor-socket") + 1] == cloudrun.EXECUTOR_SOCKET
    assert cloudrun.executor_command() == ["executor", "--socket", cloudrun.EXECUTOR_SOCKET]
    ex = pod["initContainers"][0]
    core_dir = next(m for m in core["volumeMounts"] if m["name"] == "exec-socket")["mountPath"]
    ex_dir = next(m for m in ex["volumeMounts"] if m["name"] == "exec-socket")["mountPath"]
    assert core_dir == ex_dir == cloudrun.EXECUTOR_SOCKET_DIR   # the bound socket is reachable across the split


def test_client_applier_routes_cilium_to_custom_objects_and_tolerates_absent_crd(monkeypatch):
    kubernetes = pytest.importorskip("kubernetes")
    from kubernetes.client.exceptions import ApiException
    builtins_created: list[str] = []
    custom_created: list[tuple] = []

    def fake_create_from_dict(api, obj):
        builtins_created.append(obj["kind"])      # CiliumNetworkPolicy here would AttributeError live

    class FakeCustom:
        def __init__(self, api):
            pass

        def create_namespaced_custom_object(self, group, version, ns, plural, obj):
            custom_created.append((obj["kind"], plural))
            raise ApiException(status=404)         # CRD not served — must NOT fail the run

    monkeypatch.setattr(cloudrun.cloud, "api_client", lambda *a, **k: object())
    monkeypatch.setattr("kubernetes.utils.create_from_dict", fake_create_from_dict)
    monkeypatch.setattr(kubernetes.client, "CustomObjectsApi", FakeCustom)

    spec = cloudrun.RunSpec(run_id="r", image="i", target="t", goal="g")
    cloudrun._client_applier(None)(cloudrun.build_run_objects(spec))   # must not raise despite the 404
    assert "CiliumNetworkPolicy" not in builtins_created               # never routed to create_from_dict
    assert ("CiliumNetworkPolicy", "ciliumnetworkpolicies") in custom_created
    assert "Job" in builtins_created                                  # built-ins still via create_from_dict


# --------------------------------------------------------------------------------------------
# create_run / delete_run — the guard runs first, then the injected applier/deleter.
# --------------------------------------------------------------------------------------------
@pytest.fixture
def pinned(monkeypatch):
    """Pin the active context to PROD without a cluster (monkeypatch the guard's lookup)."""
    monkeypatch.setattr(cloud, "current_context",
                        lambda kubeconfig=None, in_cluster=False: "do-nyc1-loopkit-prod")
    return "do-nyc1-loopkit-prod"


def test_create_run_applies_objects_when_context_matches(pinned):
    spec = cloudrun.RunSpec(run_id="r1", image="i", target="t", goal="g", workers=2)
    recorded: list[dict] = []
    ns = cloudrun.create_run(spec, expected=pinned, applier=recorded.extend)
    assert ns == "run-r1"
    assert [o["kind"] for o in recorded][:2] == ["Namespace", "ServiceAccount"]
    assert any(o["kind"] == "Job" and o["metadata"]["name"] == "worker" for o in recorded)


def test_create_run_refuses_wrong_context_before_applying_anything(pinned):
    spec = cloudrun.RunSpec(run_id="r1", image="i", target="t", goal="g")
    recorded: list[dict] = []
    with pytest.raises(cloud.ContextError):
        cloudrun.create_run(spec, expected="kind-loopkit", applier=recorded.extend)
    assert recorded == []                                  # guard ran first — nothing was applied


def test_create_run_includes_creds_secret_when_provided(pinned):
    spec = cloudrun.RunSpec(run_id="r1", image="i", target="t", goal="g")
    recorded: list[dict] = []
    cloudrun.create_run(spec, expected=pinned, creds={"ANTHROPIC_API_KEY": "sk"},
                        applier=recorded.extend)
    secret = next(o for o in recorded if o["kind"] == "Secret")
    assert secret["stringData"] == {"ANTHROPIC_API_KEY": "sk"}


def test_create_run_deletes_namespace_on_apply_failure(pinned):
    spec = cloudrun.RunSpec(run_id="r1", image="i", target="t", goal="g")
    deleted: list[str] = []

    def boom(_objects):
        raise RuntimeError("apiserver 500 mid-apply")

    with pytest.raises(RuntimeError):
        cloudrun.create_run(spec, expected=pinned, creds={"ANTHROPIC_API_KEY": "sk"},
                            applier=boom, deleter=deleted.append)
    assert deleted == ["run-r1"]              # the half-built ns (with its real Secret) is torn down (G3)


def test_run_spec_carries_submitter_label():
    spec = cloudrun.RunSpec(run_id="r1", image="i", target="t", goal="g", submitter="alice")
    assert spec.submitter == "alice"
    assert cloudrun.build_namespace(spec)["metadata"]["labels"]["loopkit.dev/submitter"] == "alice"


def test_delete_run_guard_first_and_targets_the_namespace(pinned):
    deleted: list[str] = []
    ns = cloudrun.delete_run("r1", expected=pinned, deleter=deleted.append)
    assert ns == "run-r1" and deleted == ["run-r1"]
    with pytest.raises(cloud.ContextError):
        cloudrun.delete_run("r1", expected="kind-loopkit", deleter=deleted.append)
    assert deleted == ["run-r1"]                           # the refused delete did nothing


# --------------------------------------------------------------------------------------------
# list_runs / phase derivation — the read path (injected lister + the pure phase rule).
# --------------------------------------------------------------------------------------------
def _job(active=0, succeeded=0, failed=0):
    return types.SimpleNamespace(status=types.SimpleNamespace(
        active=active, succeeded=succeeded, failed=failed))


def test_phase_from_jobs_covers_the_lifecycle():
    assert cloudrun._phase_from_jobs(None, None) == "unknown"
    assert cloudrun._phase_from_jobs(_job(), _job()) == "pending"
    assert cloudrun._phase_from_jobs(_job(active=1), _job(active=3)) == "running"
    assert cloudrun._phase_from_jobs(_job(succeeded=1), _job(succeeded=3)) == "complete"
    assert cloudrun._phase_from_jobs(_job(failed=1), _job()) == "failed"


def test_list_runs_uses_the_injected_lister():
    summaries = [cloudrun.RunSummary(run_id="r1", namespace="run-r1", phase="running",
                                     workers_active=2)]
    out = cloudrun.list_runs(lister=lambda: summaries)
    assert out[0].run_id == "r1" and out[0].phase == "running"
