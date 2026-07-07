"""Phase 5a — credential hygiene (`loopkit/secrets.py`): scrub, load-then-shred, redaction.

Token-free, no cluster. The load-then-shred property is the load-bearing one: after `load()`, no
credential file or env var survives, so agent-driven `printenv`/`cat` finds nothing. The full
"install runs before the first subprocess" ordering is asserted at the spawn sites in
test_agent/test_gate; here we prove the unit invariants the whole scheme rests on.
"""
from __future__ import annotations

import json

import pytest

from loopkit import secrets


@pytest.fixture(autouse=True)
def _clean_state():
    """Each test starts with an empty registry + default (no-op) store, and restores after."""
    secrets.clear_registry()
    secrets.install(secrets.CredentialStore())
    yield
    secrets.clear_registry()
    secrets.install(secrets.CredentialStore())


# --------------------------------------------------------------------------------------------
# Credential-var detection + env scrubbing.
# --------------------------------------------------------------------------------------------
def test_is_credential_var_known_and_by_suffix():
    assert secrets.is_credential_var("ANTHROPIC_API_KEY")
    assert secrets.is_credential_var("GITHUB_TOKEN")
    assert secrets.is_credential_var("SOME_VENDOR_API_KEY")     # suffix sweep
    assert secrets.is_credential_var("DB_PASSWORD")
    assert not secrets.is_credential_var("PATH")
    assert not secrets.is_credential_var("LOOPKIT_ENV")


def test_child_env_strips_all_creds_and_does_not_mutate_base():
    base = {"PATH": "/bin", "ANTHROPIC_API_KEY": "sk-ant-secret", "GITHUB_TOKEN": "ghp_tok",
            "LOOPKIT_ENV": "prod"}
    store = secrets.CredentialStore()
    env = store.child_env(base=base)
    assert env == {"PATH": "/bin", "LOOPKIT_ENV": "prod"}       # both creds stripped
    assert base["ANTHROPIC_API_KEY"] == "sk-ant-secret"         # base untouched


def test_child_env_reinjects_only_the_allowlisted_var():
    base = {"PATH": "/bin", "GITHUB_TOKEN": "ghp_tok", "ANTHROPIC_API_KEY": "sk-ant-secret"}
    env = secrets.CredentialStore().child_env(base=base, add=secrets.GIT_ENV)
    assert env["GITHUB_TOKEN"] == "ghp_tok"                     # git re-injected for loopkit's own git
    assert "ANTHROPIC_API_KEY" not in env                       # the model key still withheld


def test_harden_keeps_infra_key_in_parent_env_but_child_env_still_strips_it(monkeypatch):
    # loopkit's own tracer reads LANGSMITH_API_KEY from os.environ in THIS process, so _harden must
    # NOT pop it — while child_env() must still withhold it from agent subprocesses. Contrast with a
    # model key, which is popped from the parent AND withheld from the child.
    monkeypatch.setenv("LANGSMITH_API_KEY", "ls-secret")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secret")
    store = secrets.CredentialStore.load()                      # scans env, hardens (pops), registers
    import os
    assert os.environ.get("LANGSMITH_API_KEY") == "ls-secret"   # kept for loopkit's own tracer
    assert "ANTHROPIC_API_KEY" not in os.environ                # model key scrubbed from the parent
    child = store.child_env()
    assert "LANGSMITH_API_KEY" not in child                     # but still withheld from the agent
    assert "ANTHROPIC_API_KEY" not in child


def test_child_env_prefers_store_value_for_reinjected_var():
    base = {"GITHUB_TOKEN": "from-env"}
    store = secrets.CredentialStore({"GITHUB_TOKEN": "from-store"})
    env = store.child_env(base=base, add=secrets.GIT_ENV)
    assert env["GITHUB_TOKEN"] == "from-store"                  # the loaded store wins over ambient


def test_child_env_reinjects_the_gitlab_token_for_loopkits_own_forge_calls():
    # glab authenticates via GITLAB_TOKEN — it must reach loopkit's own forge subprocess (GIT_ENV),
    # but the agent's bare child_env() must still scrub it (it matches the *_TOKEN credential rule).
    base = {"PATH": "/bin", "GITLAB_TOKEN": "glpat-xyz", "ANTHROPIC_API_KEY": "sk-ant-secret"}
    assert "GITLAB_TOKEN" in secrets.GIT_ENV
    forge_env = secrets.CredentialStore().child_env(base=base, add=secrets.GIT_ENV)
    assert forge_env["GITLAB_TOKEN"] == "glpat-xyz"            # re-injected for glab / git push
    assert "ANTHROPIC_API_KEY" not in forge_env                # model key still withheld
    agent_env = secrets.CredentialStore().child_env(base=base)
    assert "GITLAB_TOKEN" not in agent_env                     # the agent's shell gets none


# --------------------------------------------------------------------------------------------
# load() — from tmpfs files (cloud) and from env (laptop), both shred after.
# --------------------------------------------------------------------------------------------
def test_load_from_tmpfs_reads_then_shreds_the_files(tmp_path):
    (tmp_path / "ANTHROPIC_API_KEY").write_text("sk-ant-filevalue\n")
    (tmp_path / "GITHUB_TOKEN").write_text("ghp_filetoken")
    store = secrets.CredentialStore.load(tmp_path)
    assert store.get("ANTHROPIC_API_KEY") == "sk-ant-filevalue"   # trailing newline stripped
    assert store.get("GITHUB_TOKEN") == "ghp_filetoken"
    assert list(tmp_path.iterdir()) == []                         # files shredded — no readable copy


def test_load_shreds_subdirs_so_no_key_survives_in_k8s_metadata(tmp_path):
    # A k8s Secret mount keeps the real values under a `..data`/`..<timestamp>` dir; if one is ever
    # copied into the tmpfs, the shred must remove it too (else the agent could cat the subdir copy).
    (tmp_path / "ANTHROPIC_API_KEY").write_text("sk-ant-real-0123456789")
    meta = tmp_path / "..data"
    meta.mkdir()
    (meta / "ANTHROPIC_API_KEY").write_text("sk-ant-real-0123456789")
    store = secrets.CredentialStore.load(tmp_path)
    assert store.get("ANTHROPIC_API_KEY") == "sk-ant-real-0123456789"   # read the top-level file
    assert list(tmp_path.iterdir()) == []                               # EVERYTHING shredded, incl the subdir


def test_load_from_env_collects_and_deletes_cred_vars(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-envvalue")
    monkeypatch.setenv("GH_TOKEN", "ghp_envtoken")
    monkeypatch.setenv("PATH", "/bin")
    store = secrets.CredentialStore.load(None)
    assert store.get("ANTHROPIC_API_KEY") == "sk-ant-envvalue"
    import os
    assert "ANTHROPIC_API_KEY" not in os.environ                  # scrubbed from the process env
    assert "GH_TOKEN" not in os.environ
    assert os.environ.get("PATH") == "/bin"                       # non-creds untouched


def test_load_registers_values_for_redaction(tmp_path):
    (tmp_path / "ANTHROPIC_API_KEY").write_text("sk-ant-toredact-0123456789")
    secrets.CredentialStore.load(tmp_path)
    assert "‹redacted:ANTHROPIC_API_KEY›" in secrets.redact("key=sk-ant-toredact-0123456789 done")


def test_api_key_is_the_precise_sdk_key_never_an_oauth_token():
    store = secrets.CredentialStore({"ANTHROPIC_API_KEY": "sk-ant-x", "OPENAI_API_KEY": "sk-oa-y"})
    assert store.api_key("claude-api") == "sk-ant-x"
    assert store.api_key("openai-api") == "sk-oa-y"
    assert store.api_key("mock") is None
    assert store.api_key("claude-code") is None                     # CLI adapter has no SDK key
    # OAuth-only registration → no SDK key (the Anthropic SDK rejects an OAuth token), not a silent 401.
    oauth_only = secrets.CredentialStore({"CLAUDE_CODE_OAUTH_TOKEN": "sk-oauth-subscription"})
    assert oauth_only.api_key("claude-api") is None
    assert secrets.CredentialStore().api_key("claude-api") is None   # empty store → fall back to env


# --------------------------------------------------------------------------------------------
# Redaction registry — value scrub + the pre-push pattern scan.
# --------------------------------------------------------------------------------------------
def test_redact_replaces_registered_value_anywhere_including_json():
    secrets.register_secret("sk-ant-supersecretvalue", label="ANTHROPIC_API_KEY")
    blob = json.dumps({"out": "exit=0\nANTHROPIC_API_KEY=sk-ant-supersecretvalue"})
    redacted = secrets.redact(blob)
    assert "sk-ant-supersecretvalue" not in redacted
    assert "‹redacted:ANTHROPIC_API_KEY›" in redacted


def test_redact_ignores_short_values_and_empty_registry():
    secrets.register_secret("short", label="X")                  # below _MIN_SECRET_LEN → not stored
    assert secrets.redact("short string stays") == "short string stays"


def test_redact_obj_recurses_through_structures():
    secrets.register_secret("ghp_deadbeefdeadbeef00", label="GITHUB_TOKEN")
    obj = {"a": ["ghp_deadbeefdeadbeef00", {"b": "ghp_deadbeefdeadbeef00"}]}
    out = secrets.redact_obj(obj)
    assert out == {"a": ["‹redacted:GITHUB_TOKEN›", {"b": "‹redacted:GITHUB_TOKEN›"}]}


def test_redact_scrubs_a_secret_inside_an_exception_message():
    secrets.register_secret("sk-ant-leakedviaexcdetail123", label="ANTHROPIC_API_KEY")
    detail = str(RuntimeError("apiserver rejected stringData=sk-ant-leakedviaexcdetail123"))
    assert "sk-ant-leakedviaexcdetail123" not in secrets.redact(detail)


def test_scan_for_secrets_flags_registry_and_pattern_hits():
    secrets.register_secret("registeredsecretvalue123", label="MINE")
    found = secrets.scan_for_secrets("diff: registeredsecretvalue123 and token ghp_ABCDEFGHIJ0123456789")
    assert "registered:MINE" in found
    assert "github-pat" in found
    assert secrets.scan_for_secrets("clean diff, no secrets here") == []


# --------------------------------------------------------------------------------------------
# install / current.
# --------------------------------------------------------------------------------------------
def test_install_and_current_round_trip():
    store = secrets.CredentialStore({"GITHUB_TOKEN": "ghp_installed"})
    secrets.install(store)
    assert secrets.current() is store
    assert secrets.current().get("GITHUB_TOKEN") == "ghp_installed"
