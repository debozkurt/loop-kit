"""Phase 4 — triggers: the webhook listener + the CronJob, both → the shared create_run() seam.

Everything security-critical runs with no socket, no cluster, no tokens. HMAC verification, event
parsing, idempotency, and the dispatch decision tree are pure and asserted directly; the CronJob is
a pure manifest builder; `create_schedule` is exercised through an injected applier with the context
guard monkeypatched (proving it runs the guard *before* touching anything). One integration test
drives the real stdlib HTTP shell over a loopback socket to prove headers→dispatch wiring.
"""
from __future__ import annotations

import json
import textwrap
import threading
import urllib.request
from pathlib import Path

import pytest
from typer.testing import CliRunner

from loopkit.cli import app
from loopkit.extensions import cloud, triggers

runner = CliRunner()
SECRET = "s3cr3t"
PROD = "do-nyc1-loopkit-prod"


def _issue_payload(*, action="opened", number=42, title="Fix the bug",
                   body="It crashes on empty input.", labels=None, repo="acme/widget",
                   author="octocat", sender="maintainer"):
    return {
        "action": action,
        "issue": {"number": number, "title": title, "body": body,
                  "user": {"login": author},                  # the ISSUE AUTHOR (whose key a run spends)
                  "labels": [{"name": n} for n in (labels or [])]},
        "repository": {"full_name": repo, "clone_url": f"https://github.com/{repo}.git"},
        "sender": {"login": sender},                          # the actor — NOT used for key selection
    }


def _body(payload) -> bytes:
    return json.dumps(payload).encode()


# --------------------------------------------------------------------------------------------
# HMAC signature verification — the authentication, fail-closed.
# --------------------------------------------------------------------------------------------
def test_verify_signature_accepts_a_correct_hmac():
    body = b'{"hello":"world"}'
    assert triggers.verify_signature(SECRET, body, triggers.sign(SECRET, body)) is True


def test_verify_signature_rejects_a_forged_or_mismatched_signature():
    body = b'{"hello":"world"}'
    assert triggers.verify_signature(SECRET, body, "sha256=deadbeef") is False
    assert triggers.verify_signature(SECRET, body, triggers.sign("other-secret", body)) is False


def test_verify_signature_is_fail_closed_on_missing_secret_or_header():
    body = b"{}"
    assert triggers.verify_signature("", body, triggers.sign(SECRET, body)) is False  # no secret => refuse
    assert triggers.verify_signature(SECRET, body, None) is False                      # no signature => refuse


# --------------------------------------------------------------------------------------------
# Event parsing + trigger policy — only actionable issue events become a run.
# --------------------------------------------------------------------------------------------
def test_parse_event_extracts_the_issue_fields():
    ev = triggers.parse_event("issues", _issue_payload(labels=["bug", "loopkit"]), "deliv-1")
    assert ev is not None
    assert ev.repo == "acme/widget" and ev.issue_number == 42
    assert ev.clone_url == "https://github.com/acme/widget.git"
    assert ev.title == "Fix the bug" and "crashes" in ev.body
    assert ev.labels == ["bug", "loopkit"]
    assert ev.dedupe_key == "acme/widget#42"


def test_parse_event_ignores_non_issue_events_and_dead_actions():
    assert triggers.parse_event("push", {"ref": "main"}, "d") is None
    assert triggers.parse_event("issues", _issue_payload(action="closed"), "d") is None
    assert triggers.parse_event("issues", {"action": "opened", "issue": {}}, "d") is None  # no number


def test_should_trigger_respects_the_optional_label_gate():
    opened = triggers.parse_event("issues", _issue_payload(action="opened"), "d")
    labeled = triggers.parse_event("issues", _issue_payload(action="labeled"), "d")
    # No label configured: only opened/reopened trigger, a bare relabel does not.
    assert triggers.should_trigger(opened, None) is True
    assert triggers.should_trigger(labeled, None) is False
    # Label configured: only issues carrying it trigger, regardless of action.
    tagged = triggers.parse_event("issues", _issue_payload(action="labeled", labels=["loopkit"]), "d")
    assert triggers.should_trigger(tagged, "loopkit") is True
    assert triggers.should_trigger(opened, "loopkit") is False


def test_event_to_run_spec_uses_the_issue_as_the_goal_and_is_traceable():
    ev = triggers.parse_event("issues", _issue_payload(), "d")
    spec = triggers.event_to_run_spec(ev, image="ghcr.io/me/w:1")
    assert spec.goal.startswith("Fix the bug") and "crashes" in spec.goal
    assert spec.run_id == "acme-widget-issue-42"          # sanitized, traceable to the issue
    assert spec.target == "https://github.com/acme/widget.git"
    assert spec.extra_labels["loopkit.dev/issue"] == "42"
    assert spec.extra_labels["loopkit.dev/trigger"] == "webhook"


# --------------------------------------------------------------------------------------------
# Idempotency — first writer wins; a re-delivery is a no-op.
# --------------------------------------------------------------------------------------------
def test_in_memory_idempotency_reserves_once():
    store = triggers.InMemoryIdempotencyStore()
    assert store.reserve("acme/widget#42") is True
    assert store.reserve("acme/widget#42") is False       # second time => already seen
    assert store.reserve("acme/widget#43") is True        # a different issue is independent


def test_in_memory_idempotency_evicts_oldest_past_the_cap():
    store = triggers.InMemoryIdempotencyStore(max_keys=2)
    store.reserve("a"); store.reserve("b"); store.reserve("c")   # evicts "a"
    assert store.reserve("a") is True                     # re-triggerable after eviction
    assert store.reserve("c") is False                    # still remembered


def test_redis_idempotency_dedupes_via_set_nx():
    fakeredis = pytest.importorskip("fakeredis")
    store = triggers.RedisIdempotencyStore(fakeredis.FakeStrictRedis(decode_responses=True))
    assert store.reserve("acme/widget#42") is True
    assert store.reserve("acme/widget#42") is False


# --------------------------------------------------------------------------------------------
# WebhookApp.dispatch — the full decision tree, with an injected create recorder.
# --------------------------------------------------------------------------------------------
def _app(created, **kw):
    return triggers.WebhookApp(secret=SECRET, image="ghcr.io/me/w:1",
                               create=lambda spec, data: created.append(spec) or spec.namespace, **kw)


def _post(app_obj, payload, *, event="issues", secret=SECRET, delivery="d1"):
    body = _body(payload)
    headers = {triggers.HEADER_EVENT: event, triggers.HEADER_DELIVERY: delivery,
               triggers.HEADER_SIGNATURE: triggers.sign(secret, body)}
    return app_obj.dispatch(headers=headers, body=body)


def test_dispatch_rejects_a_forged_signature_before_any_work():
    created = []
    body = _body(_issue_payload())
    headers = {triggers.HEADER_EVENT: "issues", triggers.HEADER_SIGNATURE: "sha256=bad"}
    resp = _app(created).dispatch(headers=headers, body=body)
    assert resp.status == 401 and created == []           # never parsed, never created


def test_dispatch_acks_a_ping_without_creating():
    created = []
    resp = _post(_app(created), {"zen": "Keep it logically awesome."}, event="ping")
    assert resp.status == 200 and "pong" in resp.message and created == []


def test_dispatch_ignores_unactionable_events():
    created = []
    resp = _post(_app(created), _issue_payload(action="closed"))
    assert resp.status == 204 and created == []


def test_dispatch_starts_exactly_one_run_for_a_valid_issue():
    created = []
    resp = _post(_app(created), _issue_payload())
    assert resp.status == 202
    assert len(created) == 1 and created[0].run_id == "acme-widget-issue-42"


def test_dispatch_dedupes_redelivery_to_one_run_per_issue():
    created = []
    app_obj = _app(created)
    first = _post(app_obj, _issue_payload(), delivery="d1")
    second = _post(app_obj, _issue_payload(), delivery="d2")   # re-delivery / second matching event
    assert first.status == 202 and second.status == 200       # second is acked but skipped
    assert len(created) == 1                                   # exactly one run for the issue


def test_dispatch_honors_the_trigger_label_filter():
    created = []
    app_obj = _app(created, trigger_label="loopkit")
    unlabeled = _post(app_obj, _issue_payload())                       # no label => ignored
    labeled = _post(app_obj, _issue_payload(number=7, labels=["loopkit"]))
    assert unlabeled.status == 204
    assert labeled.status == 202 and len(created) == 1


def test_dispatch_handles_bad_json_and_create_failure():
    created = []
    bad = b"not json{"
    headers = {triggers.HEADER_EVENT: "issues", triggers.HEADER_SIGNATURE: triggers.sign(SECRET, bad)}
    resp = _app(created).dispatch(headers=headers, body=bad)
    assert resp.status == 400
    # create_run raising must surface as 500, not crash the listener.
    boom = triggers.WebhookApp(secret=SECRET, image="i",
                               create=lambda spec, data: (_ for _ in ()).throw(RuntimeError("k8s down")))
    resp2 = _post(boom, _issue_payload())
    assert resp2.status == 500


# --------------------------------------------------------------------------------------------
# The HTTP shell — one loopback round-trip proving headers map into dispatch.
# --------------------------------------------------------------------------------------------
def test_serve_wires_headers_into_dispatch_over_a_socket():
    created = []
    server = triggers.serve(_app(created), host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        # healthz probe
        with urllib.request.urlopen(f"http://{host}:{port}/healthz", timeout=5) as r:
            assert r.status == 200
        # a signed issue delivery
        body = _body(_issue_payload(number=99))
        req = urllib.request.Request(
            f"http://{host}:{port}/", data=body, method="POST",
            headers={triggers.HEADER_EVENT: "issues",
                     triggers.HEADER_DELIVERY: "deliv-99",
                     triggers.HEADER_SIGNATURE: triggers.sign(SECRET, body)})
        with urllib.request.urlopen(req, timeout=5) as r:
            assert r.status == 202
        assert len(created) == 1 and created[0].run_id == "acme-widget-issue-99"
    finally:
        server.shutdown()
        thread.join(timeout=5)


# --------------------------------------------------------------------------------------------
# GitLab provider — token auth (not HMAC) + the object_attributes payload shape.
# --------------------------------------------------------------------------------------------
def _gl_issue_payload(*, action="open", iid=42, title="Fix the bug",
                      description="It crashes on empty input.", labels=None, repo="acme/widget"):
    return {
        "object_kind": "issue",
        "event_type": "issue",
        "object_attributes": {"iid": iid, "title": title, "description": description, "action": action},
        "project": {"path_with_namespace": repo, "git_http_url": f"https://gitlab.com/{repo}.git"},
        "labels": [{"title": n} for n in (labels or [])],   # GitLab label objects use `title`
    }


def _gl_app(created, **kw):
    kw.setdefault("listener_submitter", "gitlab-bot")        # GitLab pins one identity (S1)
    return triggers.WebhookApp(secret=SECRET, image="ghcr.io/me/w:1",
                               provider=triggers.GitLabProvider(),
                               create=lambda spec, data: created.append(spec) or spec.namespace, **kw)


def _gl_post(app_obj, payload, *, token=SECRET, event="Issue Hook", delivery="uuid-1"):
    body = _body(payload)
    headers = {triggers.GITLAB_HEADER_TOKEN: token, triggers.GITLAB_HEADER_EVENT: event,
               triggers.GITLAB_HEADER_DELIVERY: delivery}
    return app_obj.dispatch(headers=headers, body=body)


def test_verify_token_is_constant_time_and_fail_closed():
    assert triggers.verify_token(SECRET, SECRET) is True
    assert triggers.verify_token(SECRET, "nope") is False
    assert triggers.verify_token("", SECRET) is False      # no secret => refuse
    assert triggers.verify_token(SECRET, None) is False     # no token => refuse


def test_parse_gitlab_event_reads_object_attributes_and_project():
    ev = triggers.parse_gitlab_event(_gl_issue_payload(labels=["bug", "loopkit"]), "uuid-1")
    assert ev is not None
    assert ev.repo == "acme/widget" and ev.issue_number == 42   # iid, not the global id
    assert ev.clone_url == "https://gitlab.com/acme/widget.git"
    assert ev.title == "Fix the bug" and "crashes" in ev.body   # body from `description`
    assert ev.action == "opened"                                # normalized from GitLab "open"
    assert ev.labels == ["bug", "loopkit"]
    assert ev.dedupe_key == "acme/widget#42"                    # same dedupe key shape as GitHub


def test_parse_gitlab_event_ignores_non_issue_and_close():
    assert triggers.parse_gitlab_event({"object_kind": "push"}, "u") is None
    assert triggers.parse_gitlab_event(_gl_issue_payload(action="close"), "u") is None


def test_gitlab_dispatch_rejects_a_bad_token():
    created = []
    resp = _gl_post(_gl_app(created), _gl_issue_payload(), token="wrong")
    assert resp.status == 401 and created == []


def test_gitlab_dispatch_starts_one_run_and_dedupes():
    created = []
    app_obj = _gl_app(created)
    first = _gl_post(app_obj, _gl_issue_payload(), delivery="u1")
    second = _gl_post(app_obj, _gl_issue_payload(), delivery="u2")   # re-delivery
    assert first.status == 202 and second.status == 200
    assert len(created) == 1 and created[0].run_id == "acme-widget-issue-42"


def test_gitlab_update_triggers_only_with_the_label():
    created = []
    app_obj = _gl_app(created, trigger_label="loopkit")
    plain = _gl_post(app_obj, _gl_issue_payload(action="update"))            # update, no label => ignored
    tagged = _gl_post(app_obj, _gl_issue_payload(iid=7, action="update", labels=["loopkit"]))
    assert plain.status == 204
    assert tagged.status == 202 and len(created) == 1       # the label add (an update hook) fires once


def test_provider_for_resolves_known_forges_and_rejects_others():
    assert triggers.provider_for("github").name == "github"
    assert triggers.provider_for("gitlab").name == "gitlab"
    assert triggers.provider_for(None).name == "github"     # default
    with pytest.raises(ValueError, match="unknown webhook provider"):
        triggers.provider_for("bitbucket")


# --------------------------------------------------------------------------------------------
# In-cluster context — the guard stays intact for the CronJob/webhook path.
# --------------------------------------------------------------------------------------------
def test_in_cluster_context_guard_is_pure_and_fail_closed():
    assert cloud.check_context(cloud.IN_CLUSTER_CONTEXT, "in-cluster") == "in-cluster"
    with pytest.raises(cloud.ContextError):               # fail-closed: must be explicitly pinned
        cloud.check_context(cloud.IN_CLUSTER_CONTEXT, None)


def test_current_context_in_cluster_refuses_off_cluster():
    pytest.importorskip("kubernetes")
    # The test host is not a pod, so load_incluster_config fails => a clear ContextError, never a
    # silent "in-cluster" that a laptop could spoof.
    with pytest.raises(cloud.ContextError, match="not running in a cluster"):
        cloud.current_context(in_cluster=True)


# --------------------------------------------------------------------------------------------
# CronJob — the schedule builder + the guarded create path.
# --------------------------------------------------------------------------------------------
def test_schedule_spec_requires_exactly_one_of_issues_or_goal():
    with pytest.raises(ValueError, match="exactly one"):
        triggers.ScheduleSpec(name="nightly", schedule="0 9 * * *", target="t", image="i")
    with pytest.raises(ValueError, match="exactly one"):
        triggers.ScheduleSpec(name="n", schedule="0 9 * * *", target="t", image="i",
                              from_issues=True, goal="both")


def test_cronjob_command_is_cloud_run_in_cluster():
    spec = triggers.ScheduleSpec(name="nightly-issues", schedule="0 9 * * *", target="https://x/r",
                                 image="ghcr.io/me/w:1", from_issues=True, label="loopkit")
    cmd = triggers.cronjob_command(spec)
    assert cmd[:2] == ["cloud", "run"]
    assert "--in-cluster" in cmd and "--yes" in cmd and "--from-issues" in cmd
    assert cmd[cmd.index("--label") + 1] == "loopkit"
    assert cmd[cmd.index("--target") + 1] == "https://x/r"
    goal_spec = triggers.ScheduleSpec(name="daily", schedule="@daily", target="t", image="i",
                                      goal="run the audit")
    gcmd = triggers.cronjob_command(goal_spec)
    assert "--from-issues" not in gcmd and gcmd[gcmd.index("--goal") + 1] == "run the audit"


def test_cronjob_command_forces_a_non_auto_provider():
    spec = triggers.ScheduleSpec(name="gl-nightly", schedule="0 9 * * *",
                                 target="https://git.acme.internal/r", image="i",
                                 from_issues=True, provider="gitlab")
    cmd = triggers.cronjob_command(spec)
    assert cmd[cmd.index("--provider") + 1] == "gitlab"    # threads through to `cloud run --from-issues`


def test_build_cronjob_runs_as_control_sa_pinned_in_cluster():
    spec = triggers.ScheduleSpec(name="Nightly Issues!", schedule="0 9 * * *", target="t",
                                 image="ghcr.io/me/w:1", from_issues=True)
    cj = triggers.build_cronjob(spec)
    assert cj["kind"] == "CronJob"
    assert cj["metadata"]["name"] == "nightly-issues"     # sanitized
    assert cj["metadata"]["namespace"] == triggers.SYSTEM_NAMESPACE
    cjspec = cj["spec"]
    assert cjspec["schedule"] == "0 9 * * *"
    assert cjspec["concurrencyPolicy"] == "Forbid"        # don't lap a slow run
    pod = cjspec["jobTemplate"]["spec"]["template"]["spec"]
    assert pod["serviceAccountName"] == "loopkit-control"  # the only SA that may create runs
    assert pod["restartPolicy"] == "Never"
    env = {e["name"]: e["value"] for e in pod["containers"][0]["env"]}
    assert env["LOOPKIT_CLOUD_CONTEXT"] == "in-cluster"   # pins the guard for the in-cluster path
    assert pod["imagePullSecrets"] == [{"name": "ghcr-pull"}]


@pytest.fixture
def pinned(monkeypatch):
    """Pin the active context to PROD without a cluster (monkeypatch the guard's lookup)."""
    monkeypatch.setattr(cloud, "current_context",
                        lambda kubeconfig=None, in_cluster=False: PROD)
    return PROD


def test_create_schedule_applies_after_the_guard(pinned):
    spec = triggers.ScheduleSpec(name="nightly", schedule="0 9 * * *", target="t", image="i",
                                 from_issues=True)
    recorded: list[dict] = []
    name = triggers.create_schedule(spec, expected=pinned, applier=recorded.extend)
    assert name == "nightly"
    assert [o["kind"] for o in recorded] == ["CronJob"]


def test_create_schedule_refuses_wrong_context_before_applying(pinned):
    spec = triggers.ScheduleSpec(name="nightly", schedule="0 9 * * *", target="t", image="i",
                                 from_issues=True)
    recorded: list[dict] = []
    with pytest.raises(cloud.ContextError):
        triggers.create_schedule(spec, expected="kind-loopkit", applier=recorded.extend)
    assert recorded == []                                 # guard ran first — nothing applied


def test_delete_schedule_guard_first(pinned):
    deleted: list[str] = []
    name = triggers.delete_schedule("Nightly!", expected=pinned, deleter=deleted.append)
    assert name == "nightly" and deleted == ["nightly"]
    with pytest.raises(cloud.ContextError):
        triggers.delete_schedule("nightly", expected="kind-loopkit", deleter=deleted.append)
    assert deleted == ["nightly"]                         # the refused delete did nothing


# --------------------------------------------------------------------------------------------
# CLI surface — the guard + fail-closed validation through `loopkit cloud …`.
# --------------------------------------------------------------------------------------------
def _write_kubeconfig(tmp_path: Path, current: str) -> Path:
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


def test_cli_schedule_refuses_wrong_context(tmp_path):
    pytest.importorskip("kubernetes")
    cfg = _write_kubeconfig(tmp_path, PROD)               # active = prod
    result = runner.invoke(app, ["cloud", "schedule", "nightly", "--target", "t", "--cron", "0 9 * * *",
                                 "--from-issues", "--image", "img", "--kubeconfig", str(cfg),
                                 "--context", "kind-loopkit", "--yes"])  # pin a different one
    assert result.exit_code == 1
    assert "refus" in result.output.lower()              # guard fired before create_schedule


def test_cli_schedule_requires_image(tmp_path, monkeypatch):
    pytest.importorskip("kubernetes")
    monkeypatch.delenv("LOOPKIT_WORKER_IMAGE", raising=False)
    cfg = _write_kubeconfig(tmp_path, PROD)
    result = runner.invoke(app, ["cloud", "schedule", "nightly", "--target", "t", "--cron", "0 9 * * *",
                                 "--from-issues", "--kubeconfig", str(cfg), "--context", PROD, "--yes"])
    assert result.exit_code == 1
    assert "image" in result.output.lower()


def test_cli_webhook_is_fail_closed_without_a_secret(monkeypatch):
    pytest.importorskip("kubernetes")
    monkeypatch.delenv("LOOPKIT_WEBHOOK_SECRET", raising=False)
    result = runner.invoke(app, ["cloud", "webhook", "--image", "img"])
    assert result.exit_code == 1
    assert "secret" in result.output.lower()             # refuses to serve unauthenticated


def test_cli_webhook_rejects_an_unknown_provider():
    pytest.importorskip("kubernetes")
    result = runner.invoke(app, ["cloud", "webhook", "--image", "img", "--secret", "x",
                                 "--provider", "bitbucket"])
    assert result.exit_code == 1
    assert "provider" in result.output.lower()


# --------------------------------------------------------------------------------------------
# Phase 5a — per-submitter identity binding + the fail-closed / dedupe-safe authorization.
# --------------------------------------------------------------------------------------------
def test_parse_event_binds_submitter_to_issue_author_not_sender():
    ev = triggers.parse_event("issues", _issue_payload(author="alice", sender="maintainer"), "d")
    assert ev.submitter == "alice"                            # the issue author — never sender.login (C3)


def test_github_run_spends_the_issue_authors_key():
    created = []
    _post(_app(created), _issue_payload(author="alice"))
    assert created[0].submitter == "alice"


def _resolve_for(known: set[str]):
    def resolve(spec):
        ok = spec.submitter in known
        return triggers.ResolvedCreds({"ANTHROPIC_API_KEY": "k"} if ok else {},
                                      source="submitter" if ok else "none")
    return resolve


def test_dispatch_refuses_unregistered_submitter_without_burning_the_dedupe_key():
    created = []
    appobj = _app(created, resolve=_resolve_for({"alice"}))
    stranger = _post(appobj, _issue_payload(author="stranger"))    # unregistered → 403, no run
    assert stranger.status == 403 and created == []
    # The dedupe key was never reserved, so the SAME issue from a registered author still runs.
    ok = _post(appobj, _issue_payload(author="alice"))
    assert ok.status == 202 and len(created) == 1


def test_dispatch_releases_the_dedupe_key_on_a_create_failure():
    attempts = {"n": 0}

    def flaky_create(spec, data):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RuntimeError("transient k8s error")
        return spec.namespace

    appobj = triggers.WebhookApp(secret=SECRET, image="i", create=flaky_create)
    first = _post(appobj, _issue_payload())                  # create raises → 500, key released (G6)
    assert first.status == 500
    second = _post(appobj, _issue_payload())                 # same issue retried → succeeds
    assert second.status == 202 and attempts["n"] == 2


def test_event_to_run_spec_and_dispatch_refuse_a_cli_adapter():
    ev = triggers.parse_event("issues", _issue_payload(), "d")
    with pytest.raises(ValueError, match="refused"):
        triggers.event_to_run_spec(ev, image="i", adapter="claude-code")
    created = []
    appobj = _app(created, adapter="codex")                  # CLI adapter on an untrusted run
    assert _post(appobj, _issue_payload()).status == 422 and created == []


def test_gitlab_run_uses_the_pinned_listener_submitter_not_the_payload():
    created = []
    _gl_post(_gl_app(created), _gl_issue_payload())          # _gl_app pins listener_submitter='gitlab-bot'
    assert created[0].submitter == "gitlab-bot"              # S1 — never trusts the forgeable body


def test_cronjob_command_passes_submitter_and_fallback_only_when_non_default():
    spec = triggers.ScheduleSpec(name="n", schedule="@daily", target="t", image="i", goal="g",
                                 submitter="alice", allow_fleet_fallback=True)
    cmd = triggers.cronjob_command(spec)
    assert cmd[cmd.index("--as") + 1] == "alice" and "--allow-fleet-fallback" in cmd
    default = triggers.cronjob_command(
        triggers.ScheduleSpec(name="n", schedule="@daily", target="t", image="i", goal="g"))
    assert "--as" not in default and "--allow-fleet-fallback" not in default


def test_build_cronjob_carries_no_static_creds():
    spec = triggers.ScheduleSpec(name="n", schedule="@daily", target="t", image="i", goal="g")
    container = (triggers.build_cronjob(spec)["spec"]["jobTemplate"]["spec"]["template"]["spec"]
                 ["containers"][0])
    assert "envFrom" not in container                        # G14 — no shared key in the long-lived pod


def test_schedule_refuses_a_cli_adapter():
    with pytest.raises(ValueError, match="refused"):
        triggers.ScheduleSpec(name="n", schedule="@daily", target="t", image="i", goal="g",
                              adapter="claude-code")


def test_cli_webhook_gitlab_requires_a_pinned_identity():
    pytest.importorskip("kubernetes")
    result = runner.invoke(app, ["cloud", "webhook", "--image", "img", "--secret", "x",
                                 "--provider", "gitlab"])
    assert result.exit_code == 1 and "pinned identity" in result.output.lower()


def test_cli_webhook_refuses_a_cli_adapter():
    pytest.importorskip("kubernetes")
    result = runner.invoke(app, ["cloud", "webhook", "--image", "img", "--secret", "x",
                                 "--adapter", "claude-code"])
    assert result.exit_code == 1 and "refused" in result.output.lower()
