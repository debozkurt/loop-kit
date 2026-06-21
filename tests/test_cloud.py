"""Phase 2 — the cloud control-plane foundation: the context-safety guard + the system manifests.

The guard's pure logic (`check_context` / `resolve_expected`) is exhaustively unit-tested with no
cluster, no client, and no network — that's the whole point of keeping it dependency-free. The
kubeconfig-reading + CLI tests need the `kubernetes` client (the `[cloud]` extra); they
`importorskip` so the base `loopkit[dev]` suite stays green without it. The manifest tests assert the
two Phase-2 acceptance properties that live in YAML: Redis is durable (AOF + PVC) and the network is
default-deny.
"""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
from typer.testing import CliRunner

from loopkit.cli import app
from loopkit.extensions import cloud

runner = CliRunner()
REPO = Path(__file__).resolve().parents[1]
CLOUD_MANIFESTS = REPO / "k8s" / "cloud"
PROD = "do-nyc1-loopkit-prod"


# --------------------------------------------------------------------------------------------
# The guard — pure logic, no kubernetes import, no cluster. The heart of Phase 2's acceptance.
# --------------------------------------------------------------------------------------------
def test_check_context_passes_on_exact_match():
    assert cloud.check_context(PROD, PROD) == PROD


def test_check_context_refuses_a_different_context():
    with pytest.raises(cloud.ContextError, match="refusing to act"):
        cloud.check_context("kind-loopkit", PROD)


def test_check_context_is_fail_closed_when_nothing_is_pinned():
    # The dangerous default: no pin must NEVER mean "allow the ambient context".
    with pytest.raises(cloud.ContextError, match="no expected cloud context"):
        cloud.check_context(PROD, None)


def test_check_context_refuses_when_there_is_no_active_context():
    with pytest.raises(cloud.ContextError, match="no active kube context"):
        cloud.check_context(None, PROD)


def test_check_context_honors_an_allowlist():
    assert cloud.check_context("b", "a,b,c") == "b"
    with pytest.raises(cloud.ContextError):
        cloud.check_context("d", "a,b,c")


def test_resolve_expected_splits_and_trims_an_allowlist():
    assert cloud.resolve_expected("a, b ,c") == ["a", "b", "c"]


def test_resolve_expected_reads_env_but_explicit_wins(monkeypatch):
    monkeypatch.setenv(cloud.ENV_CONTEXT, "from-env")
    assert cloud.resolve_expected() == ["from-env"]
    assert cloud.resolve_expected("explicit") == ["explicit"]


def test_resolve_expected_empty_is_unpinned(monkeypatch):
    monkeypatch.delenv(cloud.ENV_CONTEXT, raising=False)
    assert cloud.resolve_expected() == []
    assert cloud.resolve_expected("") == []   # an empty flag value is not a pin


# --------------------------------------------------------------------------------------------
# Reading the active context from a kubeconfig (needs the kubernetes client; no cluster).
# --------------------------------------------------------------------------------------------
def _write_kubeconfig(tmp_path: Path, current: str) -> Path:
    cfg = tmp_path / "kubeconfig.yaml"
    cfg.write_text(textwrap.dedent(f"""\
        apiVersion: v1
        kind: Config
        current-context: {current}
        clusters:
        - name: c
          cluster:
            server: https://example.invalid:443
        contexts:
        - name: {PROD}
          context: {{cluster: c, user: u}}
        - name: kind-loopkit
          context: {{cluster: c, user: u}}
        users:
        - name: u
          user: {{}}
        """))
    return cfg


def test_current_context_reads_the_active_context(tmp_path):
    pytest.importorskip("kubernetes")
    cfg = _write_kubeconfig(tmp_path, PROD)
    assert cloud.current_context(cfg) == PROD


# --------------------------------------------------------------------------------------------
# The CLI surface — the guard enforced through `loopkit cloud …` (Phase-2 acceptance).
# --------------------------------------------------------------------------------------------
def test_cli_bootstrap_refuses_the_wrong_context(tmp_path):
    pytest.importorskip("kubernetes")
    cfg = _write_kubeconfig(tmp_path, PROD)            # active = prod
    result = runner.invoke(app, ["cloud", "bootstrap", "--kubeconfig", str(cfg),
                                 "--context", "kind-loopkit", "--yes"])  # but we pin a different one
    assert result.exit_code == 1
    assert "refus" in result.output.lower()             # guard fired before any apply


def test_cli_context_reports_allowed_when_pinned_matches(tmp_path):
    pytest.importorskip("kubernetes")
    cfg = _write_kubeconfig(tmp_path, PROD)
    result = runner.invoke(app, ["cloud", "context", "--kubeconfig", str(cfg), "--context", PROD])
    assert result.exit_code == 0
    assert "allowed" in result.output


def test_cli_doctor_fails_when_unpinned(tmp_path, monkeypatch):
    pytest.importorskip("kubernetes")
    monkeypatch.delenv(cloud.ENV_CONTEXT, raising=False)
    cfg = _write_kubeconfig(tmp_path, PROD)
    result = runner.invoke(app, ["cloud", "doctor", "--kubeconfig", str(cfg)])  # no --context => unpinned
    assert result.exit_code == 1
    assert "unpinned" in result.output


def test_cli_run_refuses_wrong_context(tmp_path):
    pytest.importorskip("kubernetes")
    cfg = _write_kubeconfig(tmp_path, PROD)                 # active = prod
    result = runner.invoke(app, ["cloud", "run", "--target", "t", "--goal", "g", "--image", "img",
                                 "--kubeconfig", str(cfg), "--context", "kind-loopkit", "--yes"])
    assert result.exit_code == 1
    assert "refus" in result.output.lower()                # guard fired before create_run


def test_cli_run_requires_an_image(tmp_path, monkeypatch):
    pytest.importorskip("kubernetes")
    monkeypatch.delenv("LOOPKIT_WORKER_IMAGE", raising=False)
    cfg = _write_kubeconfig(tmp_path, PROD)
    result = runner.invoke(app, ["cloud", "run", "--target", "t", "--goal", "g",
                                 "--kubeconfig", str(cfg), "--context", PROD, "--yes"])
    assert result.exit_code == 1
    assert "image" in result.output.lower()


def test_cli_kill_refuses_wrong_context(tmp_path):
    pytest.importorskip("kubernetes")
    cfg = _write_kubeconfig(tmp_path, PROD)
    result = runner.invoke(app, ["cloud", "kill", "r1",
                                 "--kubeconfig", str(cfg), "--context", "kind-loopkit", "--yes"])
    assert result.exit_code == 1
    assert "refus" in result.output.lower()


# --------------------------------------------------------------------------------------------
# Manifest sanity — the two Phase-2 acceptance properties that live in YAML.
# --------------------------------------------------------------------------------------------
def _load_manifests() -> dict[tuple[str, str], dict]:
    yaml = pytest.importorskip("yaml")
    docs: dict[tuple[str, str], dict] = {}
    for f in sorted(CLOUD_MANIFESTS.glob("*.yaml")):
        for d in yaml.safe_load_all(f.read_text()):
            if d:
                docs[(d["kind"], d["metadata"]["name"])] = d
    return docs


def test_manifests_define_the_system_namespace_foundation():
    docs = _load_manifests()
    assert ("Namespace", "loopkit-system") in docs
    assert ("StatefulSet", "redis") in docs
    assert ("ClusterRole", "loopkit-control") in docs
    assert ("ClusterRoleBinding", "loopkit-control") in docs
    assert ("NetworkPolicy", "default-deny-all") in docs


def test_redis_is_durable_aof_plus_pvc():
    """Acceptance: 'Redis durable across pod restart' — AOF on + a PersistentVolumeClaim."""
    docs = _load_manifests()
    conf = docs[("ConfigMap", "redis-config")]["data"]["redis.conf"]
    assert "appendonly yes" in conf                      # write-ahead log => survives restart
    sts = docs[("StatefulSet", "redis")]["spec"]
    vct = sts["volumeClaimTemplates"]
    assert vct and vct[0]["metadata"]["name"] == "data"  # the AOF/data live on a PVC, not emptyDir


def test_default_deny_networkpolicy_has_no_allow_rules():
    """Default-deny means: select all pods, both directions, and define NO ingress/egress rules."""
    np = _load_manifests()[("NetworkPolicy", "default-deny-all")]["spec"]
    assert np["podSelector"] == {}
    assert set(np["policyTypes"]) == {"Ingress", "Egress"}
    assert "ingress" not in np and "egress" not in np


def test_worker_sa_has_no_cluster_api_access():
    """Workers get a no-API SA with token automounting OFF (containment over trust)."""
    sa = _load_manifests()[("ServiceAccount", "loopkit-worker")]
    assert sa["automountServiceAccountToken"] is False


def test_control_clusterrole_can_create_run_namespaces_and_jobs():
    rules = _load_manifests()[("ClusterRole", "loopkit-control")]["rules"]
    verbs_for = lambda res: {  # noqa: E731 — terse helper, test-local
        v for r in rules for res_list in [r.get("resources", [])]
        if res in res_list for v in r.get("verbs", [])}
    assert {"create", "delete"} <= verbs_for("namespaces")
    assert {"create", "delete"} <= verbs_for("jobs")
    # Secrets (Phase 5a least-privilege on the internet-facing control SA): create a run's Secret +
    # get a source key to project — but NEVER list (no enumerating all tenants) or update/patch (no
    # rewriting a victim's stored key on a listener RCE; registration uses the human kubeconfig).
    secret_verbs = verbs_for("secrets")
    assert {"create", "get"} <= secret_verbs
    assert "list" not in secret_verbs and "update" not in secret_verbs and "patch" not in secret_verbs
