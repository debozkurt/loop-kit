"""Phase 5a — per-submitter credential resolution (`loopkit/extensions/creds.py`).

Token-free, no cluster. The resolve decision tree (submitter → fleet → default-deny), the projection
(only the adapter key + git), and the S4 injective check are pure; `set/delete` run the context guard
before any injected seam fires. One importorskip test covers the client writer's create→patch dispatch.
"""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
from typer.testing import CliRunner

from loopkit.cli import app
from loopkit.extensions import cloud, creds

PROD = "do-nyc1-loopkit-prod"
runner = CliRunner()


def _source(submitter, env_name, **keys):
    """A source-Secret data bag as the reader returns it (the reserved canonical keys + creds)."""
    return {**keys, creds._SUBMITTER_KEY: submitter, creds._ENV_KEY: env_name}


def _reader(store):
    """A dict-backed reader keyed by (secret_name, namespace)."""
    return lambda name, ns: store.get((name, ns))


# --------------------------------------------------------------------------------------------
# Naming + projection.
# --------------------------------------------------------------------------------------------
def test_secret_name_keyed_by_env_and_submitter_not_adapter():
    assert creds.secret_name("prod", "alice") == "loopkit-creds-prod-alice"
    assert creds.secret_name("prod", "alice") != creds.secret_name("dev", "alice")   # env matters
    assert creds.secret_name("prod", "Alice!") == creds.secret_name("prod", "alice")  # sanitized (lossy)


def test_project_keeps_only_adapter_key_and_git():
    bag = {"ANTHROPIC_API_KEY": "sk-ant", "OPENAI_API_KEY": "sk-oa", "GITHUB_TOKEN": "ghp", "JUNK": "x"}
    out = creds.project(bag, "claude-code")
    assert out == {"ANTHROPIC_API_KEY": "sk-ant", "GITHUB_TOKEN": "ghp"}   # no OpenAI key, no junk
    assert creds.project(bag, "openai-api") == {"OPENAI_API_KEY": "sk-oa", "GITHUB_TOKEN": "ghp"}


# --------------------------------------------------------------------------------------------
# Resolve decision tree.
# --------------------------------------------------------------------------------------------
def test_resolve_uses_the_submitters_own_secret():
    store = {("loopkit-creds-prod-alice", creds.SYSTEM_NAMESPACE):
             _source("alice", "prod", ANTHROPIC_API_KEY="sk-ant", OPENAI_API_KEY="sk-oa")}
    rc = creds.SecretResolver(reader=_reader(store)).resolve(
        creds.Identity("alice", "prod", "claude-code"))
    assert rc.source == "submitter" and rc.usable
    assert rc.data == {"ANTHROPIC_API_KEY": "sk-ant"}        # projected to the adapter key only


def test_resolve_is_default_deny_without_fallback():
    rc = creds.SecretResolver(reader=_reader({})).resolve(creds.Identity("nobody", "prod", "claude-code"))
    assert rc.source == "none" and not rc.usable             # unregistered → nothing, fail-closed


def test_resolve_falls_back_to_fleet_only_when_allowed():
    store = {("loopkit-creds-prod-fleet", creds.SYSTEM_NAMESPACE):
             _source("fleet", "prod", ANTHROPIC_API_KEY="sk-fleet")}
    ident = creds.Identity("newcontributor", "prod", "claude-code")
    denied = creds.SecretResolver(reader=_reader(store)).resolve(ident, allow_fleet_fallback=False)
    assert denied.source == "none" and not denied.usable     # fallback not allowed → no run
    allowed = creds.SecretResolver(reader=_reader(store)).resolve(ident, allow_fleet_fallback=True)
    assert allowed.source == "fleet" and allowed.data == {"ANTHROPIC_API_KEY": "sk-fleet"}


def test_resolve_fails_closed_on_an_identity_mismatch():
    # The fetched Secret records a DIFFERENT canonical submitter (a sanitize collision / tampering).
    store = {("loopkit-creds-prod-alice", creds.SYSTEM_NAMESPACE):
             _source("alice-two", "prod", ANTHROPIC_API_KEY="sk")}
    rc = creds.SecretResolver(reader=_reader(store)).resolve(creds.Identity("alice", "prod", "claude-code"))
    assert rc.source == "none" and not rc.usable             # recorded != requested → refuse (S4)


def test_resolve_for_run_threads_the_injected_reader():
    store = {("loopkit-creds-prod-bob", creds.SYSTEM_NAMESPACE):
             _source("bob", "prod", OPENAI_API_KEY="sk-oa", GITHUB_TOKEN="ghp")}
    rc = creds.resolve_for_run(creds.Identity("bob", "prod", "openai-api"), reader=_reader(store))
    assert rc.source == "submitter" and rc.data == {"OPENAI_API_KEY": "sk-oa", "GITHUB_TOKEN": "ghp"}


# --------------------------------------------------------------------------------------------
# Run-credential decision (the typer-free policy the CLI/cron/webhook render) — no CliRunner.
# --------------------------------------------------------------------------------------------
def test_resolve_submitter_precedence():
    assert creds.resolve_submitter("alice", {"LOOPKIT_SUBMITTER": "envsub"}) == "alice"   # explicit wins
    assert creds.resolve_submitter(None, {"LOOPKIT_SUBMITTER": "envsub"}) == "envsub"      # then the env
    assert creds.resolve_submitter(None, {}) == creds.DEFAULT_SUBMITTER                    # else the fleet default


def test_creds_from_env_keeps_only_the_cred_vars():
    env = {"ANTHROPIC_API_KEY": "a", "GH_TOKEN": "g", "PATH": "/bin", "JUNK": "j"}
    assert creds.creds_from_env(env) == {"ANTHROPIC_API_KEY": "a", "GH_TOKEN": "g"}


def test_creds_from_env_carries_the_subscription_oauth_and_gitlab_tokens():
    # Regression: the allow-list is derived from the adapter-key registry, so newly-added credentials
    # (CLAUDE_CODE_OAUTH_TOKEN for the subscription path; GITLAB_TOKEN) are forwarded, not dropped by a
    # stale literal. Without this, --from-env on the cloud fleet silently strips the subscription token.
    env = {"CLAUDE_CODE_OAUTH_TOKEN": "sk-oauth", "GITLAB_TOKEN": "glpat", "PATH": "/bin"}
    assert creds.creds_from_env(env) == {"CLAUDE_CODE_OAUTH_TOKEN": "sk-oauth", "GITLAB_TOKEN": "glpat"}


def test_decide_from_env_carries_the_subscription_token_for_claude_code():
    # The real subscription shell: an OAuth token + a git token, NO metered key. Both must survive
    # --from-env for the claude-code CLI adapter so the cloud pod can run on the Claude Code sub.
    env = {"CLAUDE_CODE_OAUTH_TOKEN": "sk-oauth", "GITHUB_TOKEN": "ghp_x"}
    d = creds.decide_run_creds("claude-code", "alice", "prod", from_env=True, env=env)
    assert d.outcome == "resolved" and d.source == "from-env"
    assert d.data == {"CLAUDE_CODE_OAUTH_TOKEN": "sk-oauth", "GITHUB_TOKEN": "ghp_x"}


def test_decide_mock_needs_no_creds():
    d = creds.decide_run_creds("mock", "alice", "prod", from_env=False, env={})
    assert d.outcome == "resolved" and d.source == "mock" and d.data == {}


def test_decide_from_env_projects_to_the_adapter_key():
    env = {"ANTHROPIC_API_KEY": "sk-ant", "OPENAI_API_KEY": "sk-oa", "JUNK": "x"}
    d = creds.decide_run_creds("claude-code", "alice", "prod", from_env=True, env=env)
    assert d.outcome == "resolved" and d.source == "from-env"
    assert d.data == {"ANTHROPIC_API_KEY": "sk-ant"}        # the OpenAI key is dropped (blast radius)


def test_decide_resolves_the_submitters_own_key():
    store = {("loopkit-creds-prod-alice", creds.SYSTEM_NAMESPACE):
             _source("alice", "prod", ANTHROPIC_API_KEY="sk-ant")}
    d = creds.decide_run_creds("claude-code", "alice", "prod", from_env=False, env={}, reader=_reader(store))
    assert d.outcome == "resolved" and d.source == "submitter"
    assert d.data == {"ANTHROPIC_API_KEY": "sk-ant"}


def test_decide_surfaces_fleet_as_needing_consent_not_silent_use():
    # Only the shared fleet key exists → the policy must NOT grant it; it returns it for the caller to consent.
    store = {("loopkit-creds-prod-fleet", creds.SYSTEM_NAMESPACE):
             _source("fleet", "prod", ANTHROPIC_API_KEY="sk-fleet")}
    d = creds.decide_run_creds("claude-code", "newbie", "prod", from_env=False, env={}, reader=_reader(store))
    assert d.outcome == "needs_fleet_consent" and d.data == {}      # not granted here
    assert d.submitter == "newbie" and d.fleet_data == {"ANTHROPIC_API_KEY": "sk-fleet"}


def test_decide_refuses_with_no_key_and_no_fleet():
    d = creds.decide_run_creds("claude-code", "nobody", "prod", from_env=False, env={}, reader=_reader({}))
    assert d.outcome == "refused" and d.submitter == "nobody"
    assert "no credentials" in d.message and "fleet default" in d.message


# --------------------------------------------------------------------------------------------
# Registration — guard-first set/delete with injected seams; reserved-key recording.
# --------------------------------------------------------------------------------------------
@pytest.fixture
def pinned(monkeypatch):
    monkeypatch.setattr(cloud, "current_context", lambda kubeconfig=None, in_cluster=False: PROD)
    return PROD


def test_set_credential_records_canonical_identity_and_keys(pinned):
    written: list[dict] = []
    name = creds.set_credential("alice", {"ANTHROPIC_API_KEY": "sk"}, env_name="prod",
                                expected=pinned, writer=written.append)
    assert name == "loopkit-creds-prod-alice"
    obj = written[0]
    assert obj["stringData"]["ANTHROPIC_API_KEY"] == "sk"
    assert obj["stringData"][creds._SUBMITTER_KEY] == "alice"     # canonical identity recorded (S4)
    assert obj["metadata"]["labels"]["app.kubernetes.io/component"] == "creds"


def test_set_credential_refuses_wrong_context_before_writing(pinned):
    written: list[dict] = []
    with pytest.raises(cloud.ContextError):
        creds.set_credential("alice", {"ANTHROPIC_API_KEY": "sk"}, expected="kind-loopkit",
                             writer=written.append)
    assert written == []                                     # guard ran first — nothing written


def test_delete_credential_guard_first(pinned):
    deleted: list[str] = []
    name = creds.delete_credential("alice", expected=pinned, deleter=deleted.append)
    assert name == "loopkit-creds-prod-alice" and deleted == ["loopkit-creds-prod-alice"]
    with pytest.raises(cloud.ContextError):
        creds.delete_credential("alice", expected="kind-loopkit", deleter=deleted.append)
    assert deleted == ["loopkit-creds-prod-alice"]           # the refused delete did nothing


def test_list_credentials_shows_key_names_never_values():
    summary = creds.CredentialSummary(submitter="alice", env_name="prod",
                                      keys=creds.visible_keys(_source("alice", "prod",
                                                                      ANTHROPIC_API_KEY="sk", GITHUB_TOKEN="g")))
    out = creds.list_credentials(lister=lambda: [summary])
    assert out[0].submitter == "alice"
    assert out[0].keys == ["ANTHROPIC_API_KEY", "GITHUB_TOKEN"]   # names, sorted; reserved keys excluded
    assert "sk" not in str(out[0])                            # never a value


def test_client_writer_creates_then_patches_on_conflict(monkeypatch):
    kubernetes = pytest.importorskip("kubernetes")
    from kubernetes.client.exceptions import ApiException

    class Core:
        def __init__(self, conflict):
            self.conflict = conflict
            self.created: list[str] = []
            self.patched: list[str] = []

        def create_namespaced_secret(self, ns, obj):
            if self.conflict:
                raise ApiException(status=409)
            self.created.append(obj["metadata"]["name"])

        def patch_namespaced_secret(self, name, ns, obj):
            self.patched.append(name)

    monkeypatch.setattr(creds.cloud, "api_client", lambda *a, **k: object())
    obj = creds.build_credential_secret("alice", "prod", {"OPENAI_API_KEY": "sk"})

    fresh = Core(conflict=False)
    monkeypatch.setattr(kubernetes.client, "CoreV1Api", lambda api: fresh)
    creds._client_secret_writer(None, creds.SYSTEM_NAMESPACE)(obj)
    assert fresh.created == ["loopkit-creds-prod-alice"] and fresh.patched == []

    existing = Core(conflict=True)
    monkeypatch.setattr(kubernetes.client, "CoreV1Api", lambda api: existing)
    creds._client_secret_writer(None, creds.SYSTEM_NAMESPACE)(obj)
    assert existing.patched == ["loopkit-creds-prod-alice"]   # conflict → merge (accumulate/rotate)


# --------------------------------------------------------------------------------------------
# CLI surface — `loopkit cloud creds set` guard + env-only validation (no cluster needed).
# --------------------------------------------------------------------------------------------
def _kubeconfig(tmp_path: Path, current: str) -> Path:
    cfg = tmp_path / "kubeconfig.yaml"
    cfg.write_text(textwrap.dedent(f"""\
        apiVersion: v1
        kind: Config
        current-context: {current}
        clusters:
        - {{name: c, cluster: {{server: https://example.invalid:443}}}}
        contexts:
        - {{name: {PROD}, context: {{cluster: c, user: u}}}}
        - {{name: kind-loopkit, context: {{cluster: c, user: u}}}}
        users:
        - {{name: u, user: {{}}}}
        """))
    return cfg


def test_cli_creds_set_refuses_wrong_context(tmp_path, monkeypatch):
    pytest.importorskip("kubernetes")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-x")
    cfg = _kubeconfig(tmp_path, PROD)                         # active = prod
    result = runner.invoke(app, ["cloud", "creds", "set", "--as", "alice", "--adapter", "claude-api",
                                 "--kubeconfig", str(cfg), "--context", "kind-loopkit", "--yes"])
    assert result.exit_code == 1 and "refus" in result.output.lower()   # guard before any write


def test_cli_creds_set_needs_keys_in_the_environment(monkeypatch):
    for var in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GITHUB_TOKEN", "GH_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    result = runner.invoke(app, ["cloud", "creds", "set", "--as", "alice", "--adapter", "claude-api"])
    assert result.exit_code == 1 and "no credentials in the environment" in result.output.lower()
