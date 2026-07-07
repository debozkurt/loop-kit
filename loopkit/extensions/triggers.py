"""Triggers — turn external events into runs through the one `create_run()` seam (Part III, Phase 4).

Chapter 12's insight is that a worker is indifferent to *what woke it*: anything that can submit a
run drives the fleet. Phase 3 built the single submission path (`cloudrun.create_run`); this module
adds the two *event* entry points that sit in front of it, so a scheduled firing, a signed webhook,
and a manual `loopkit cloud run` are the **same code path** — identical isolation, guard, and
topology no matter how a run starts:

    CronJob (loopkit cloud schedule)  ─┐
    Webhook listener (this module)    ─┼──▶  cloudrun.create_run()  ──▶  ns/run-<id> + Jobs
    CLI (loopkit cloud run)           ─┘

Design constraints carried from the rest of the project:

- **Thin stack, no new dependency.** The listener is stdlib `http.server` + `hmac`/`hashlib`; the
  CronJob is a pure manifest builder. HMAC verification, event parsing, and idempotency are pure
  functions, unit-testable with no socket, no cluster, and no tokens — the HTTP/k8s shells are thin.
- **Fail-closed security.** Authentication rejects anything unsigned/mis-signed/mis-tokened, and
  refuses outright when no secret is configured (an unauthenticated POST must never start a paid run).
  See [`docs/architecture/04-security.md`](../../docs/architecture/04-security.md) → *Webhook security*.
- **Two forges, one path.** GitHub and GitLab differ only in *how a delivery is authenticated* and
  *how its payload is shaped* — GitHub HMAC-signs the body, GitLab sends a static token; their issue
  JSON differs. Both are isolated behind a small `WebhookProvider`; everything downstream
  (idempotency, `event_to_run_spec`, `create_run`) is provider-neutral.
- **Exactly one run per issue.** Forges re-deliver, and one issue can emit several matching events
  (opened, then labeled). An idempotency store dedupes on the issue identity, so a re-delivery (or a
  second matching event) is a no-op.
- **Deferred client.** Only `create_schedule`/`list_schedules`/`delete_schedule` touch the
  `kubernetes` client, and they defer-import it (the `[cloud]` extra) and run the context guard
  **first** — same shape as `cloudrun`. The pure builders import nothing.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import threading
from dataclasses import dataclass, field
from typing import Callable, Mapping, Protocol, Sequence

from ..log import get_logger
from . import cloud
from .cloudrun import SYSTEM_NAMESPACE, RunSpec, sanitize_run_id
from .creds import ResolvedCreds

log = get_logger("triggers")

# Adapters that hold the model key inside their OWN (un-scrubbable) loop and persist credential-cache
# files under a shared $HOME — unsafe for untrusted-input (webhook/cron) runs. Prefer the in-process
# API adapter, where loopkit holds the key and the agent never sees it (Phase 5a C4).
CLI_ADAPTERS = ("claude-code", "codex")
DEFAULT_TRIGGER_ADAPTER = "claude-api"


def assert_trusted_adapter(adapter: str) -> None:
    """Refuse a CLI adapter on an untrusted-input run (the key can't be contained in the vendor loop)."""
    if adapter in CLI_ADAPTERS:
        raise ValueError(
            f"adapter {adapter!r} is refused on untrusted-input (webhook/cron) runs: a CLI adapter "
            f"holds the key in its own loop on attacker instructions. Use an API adapter "
            f"(e.g. 'claude-api'), where loopkit holds the key and the agent never sees it.")

# GitHub's HTTP headers for a webhook delivery (the listener reads these off each POST).
HEADER_EVENT = "X-GitHub-Event"
HEADER_DELIVERY = "X-GitHub-Delivery"
HEADER_SIGNATURE = "X-Hub-Signature-256"

# GitLab's equivalents. GitLab does NOT sign the body — it sends a static secret *token* verbatim in
# a header (see `verify_token`) — and names events differently ("Issue Hook", `object_kind: issue`).
GITLAB_HEADER_EVENT = "X-Gitlab-Event"
GITLAB_HEADER_TOKEN = "X-Gitlab-Token"
GITLAB_HEADER_DELIVERY = "X-Gitlab-Event-UUID"

# Issue actions worth turning into a run, in the GitHub vocabulary (GitLab actions are normalized to
# it below). "opened"/"reopened" start work on new/revived issues; "labeled" lets a label be the
# trigger (e.g. add `loopkit` to dispatch the agent at an issue).
TRIGGER_ACTIONS = ("opened", "reopened", "labeled")

# Revise runs (a reviewer requested changes on a loop-authored PR) only ever resume branches the loop
# itself created. The prefix is the containment: a crafted review event pointing at `main` or a
# human's feature branch is refused by policy (`should_trigger`) and by the CI glue — the loop
# follows through on ITS OWN work, it doesn't adopt someone else's branch on a reviewer's say-so.
REVISE_BRANCH_PREFIX = "loopkit/"

# GitLab issue-hook `object_attributes.action` → the GitHub vocabulary the rest of the module speaks,
# plus the candidate set worth parsing. "update" carries label changes (so the label-gate can fire on
# it) but is *not* opened/reopened, so it never triggers a run on its own without a configured label.
_GITLAB_ACTION = {"open": "opened", "reopen": "reopened", "update": "update", "close": "closed"}
_GITLAB_CANDIDATE_ACTIONS = ("opened", "reopened", "update")


# --------------------------------------------------------------------------------------------
# HMAC signature verification — pure stdlib, fail-closed (the listener's authentication).
# --------------------------------------------------------------------------------------------
def sign(secret: str, body: bytes) -> str:
    """Compute the GitHub-style `sha256=<hex>` HMAC of `body` under `secret` (used by tests too)."""
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def verify_signature(secret: str, body: bytes, signature: str | None) -> bool:
    """True iff `signature` is the valid HMAC-SHA256 of `body` under `secret`. Fail-closed.

    GitHub signs each delivery with the shared webhook secret and sends `X-Hub-Signature-256:
    sha256=<hex>`. We recompute and compare in **constant time** (`hmac.compare_digest`) so a timing
    side-channel can't leak the digest. Every failure mode returns False rather than raising: no
    secret configured (refuse to authenticate anything), no signature header, or a mismatch — so a
    forged or unsigned POST can never start a run.
    """
    if not secret or not signature:
        return False
    return hmac.compare_digest(sign(secret, body), signature)


def verify_token(secret: str, token: str | None) -> bool:
    """True iff `token` matches `secret` (GitLab's auth). Constant-time, fail-closed.

    GitLab does not HMAC the body; it sends the configured *secret token* verbatim in the
    `X-Gitlab-Token` header, and the receiver compares it against the shared secret. We still use
    `hmac.compare_digest` so the comparison is constant-time. Honest caveat vs GitHub: because the
    token isn't bound to the request body, a leaked token is directly replay/forge-able — that's
    GitLab-native behavior, not something this layer can strengthen (see 04-security.md).
    """
    if not secret or not token:
        return False
    return hmac.compare_digest(secret, token)


# --------------------------------------------------------------------------------------------
# Event model — normalize a GitHub webhook payload into the few fields a run needs (pure).
# --------------------------------------------------------------------------------------------
@dataclass
class WebhookEvent:
    """The normalized slice of a forge delivery that becomes a run. Plain data, no I/O.

    Two kinds share the shape: an **issue** event (the original Phase-4 trigger — the issue is the
    goal, a fresh branch is the workspace) and a **revise** event (a reviewer requested changes on a
    loop-authored PR — the review is the goal, the PR's *existing* head branch is the workspace).
    For a revise event `issue_number` holds the PR number and `body` the review's summary comment.
    """

    delivery_id: str
    event_type: str
    action: str
    repo: str                                    # owner/name (repository.full_name)
    clone_url: str                               # repository.clone_url — what the workers clone
    issue_number: int                            # the issue — or, for kind="revise", the PR number
    title: str
    body: str
    labels: list[str] = field(default_factory=list)
    submitter: str = ""                          # whose key a run spends: the issue AUTHOR / the REVIEWER
    kind: str = "issue"                          # "issue" | "revise"
    branch: str = ""                             # revise only: the PR's head ref the run must resume
    review_id: int = 0                           # revise only: the review round (the dedupe unit)

    @property
    def dedupe_key(self) -> str:
        """The idempotency key. Note the semantics INVERT between kinds:

        - **issue** → `repo#N`: one issue maps to *at most one run ever* — a re-delivery and a second
          matching event (opened then labeled) both dedupe (the Phase-4 acceptance).
        - **revise** → `repo#prN@rID`: one run *per review round*. A re-delivery of the same review
          dedupes, but a NEW round (new review id) is new work and must start a new run — an
          issue-style key would make the loop deaf to every review after the first.
        """
        if self.kind == "revise":
            return f"{self.repo}#pr{self.issue_number}@r{self.review_id}"
        return f"{self.repo}#{self.issue_number}"


def parse_event(event_type: str, payload: dict, delivery_id: str = "-") -> WebhookEvent | None:
    """Map a GitHub webhook payload into a `WebhookEvent`, or None for events we don't act on.

    Two event families become runs: `issues` with a trigger-worthy action (a new task), and
    `pull_request_review` with changes requested (a revise of the loop's own PR — see
    `parse_review_event`). Everything else (push, a plain PR event, a `closed`/`deleted` issue, a
    malformed payload) returns None so the listener replies "ignored". The goal-building lives in
    `event_to_run_spec`/`revise_goal`; this only extracts fields.
    """
    if event_type == "pull_request_review":
        return parse_review_event(payload, delivery_id)
    if event_type != "issues":
        return None
    action = payload.get("action", "")
    if action not in TRIGGER_ACTIONS:
        return None
    issue = payload.get("issue") or {}
    repo = payload.get("repository") or {}
    number = issue.get("number")
    if number is None:
        return None
    labels = [lbl.get("name", "") for lbl in (issue.get("labels") or []) if lbl.get("name")]
    # Bind to the ISSUE AUTHOR, NOT `sender.login` (the actor). On a `labeled` event the actor is the
    # maintainer who labelled it, but the run is *about* the author's issue — so it spends the author's
    # key, and an attacker can only ever spend a key registered to themselves (C3 confused-deputy fix).
    author = (issue.get("user") or {}).get("login", "")
    return WebhookEvent(
        delivery_id=delivery_id, event_type=event_type, action=action,
        repo=repo.get("full_name", ""), clone_url=repo.get("clone_url", ""),
        issue_number=int(number), title=(issue.get("title") or "").strip(),
        body=(issue.get("body") or "").strip(), labels=labels, submitter=author)


def parse_review_event(payload: dict, delivery_id: str = "-") -> WebhookEvent | None:
    """Map a GitHub `pull_request_review` payload into a revise `WebhookEvent`, or None.

    Only a **submitted, changes-requested** review becomes a run: it is the one review outcome that
    is an explicit human instruction to keep working ("this isn't done — fix these things"). An
    approval or a plain comment is not a work order, and acting on every comment would let the loop
    react to its own PR chatter. The event captures what a revise run needs beyond the issue shape:
    the PR's **head branch** (the workspace to resume) and the **review id** (the dedupe round).

    Identity (C3): the run is bound to the **reviewer** — the review body is the instruction being
    executed, so it spends the reviewer's key, never the PR author's. GitLab has no equivalent
    changes-requested primitive on MR hooks, so revise parsing is GitHub-only for now.
    """
    if payload.get("action") != "submitted":
        return None
    review = payload.get("review") or {}
    if (review.get("state") or "").lower() != "changes_requested":
        return None
    pr = payload.get("pull_request") or {}
    number = pr.get("number")
    branch = ((pr.get("head") or {}).get("ref") or "").strip()
    if number is None or not branch:
        return None
    repo = payload.get("repository") or {}
    labels = [lbl.get("name", "") for lbl in (pr.get("labels") or []) if lbl.get("name")]
    return WebhookEvent(
        delivery_id=delivery_id, event_type="pull_request_review", action="submitted",
        repo=repo.get("full_name", ""), clone_url=repo.get("clone_url", ""),
        issue_number=int(number), title=(pr.get("title") or "").strip(),
        body=(review.get("body") or "").strip(), labels=labels,
        submitter=(review.get("user") or {}).get("login", ""),
        kind="revise", branch=branch, review_id=int(review.get("id") or 0))


def revise_goal(event: WebhookEvent) -> str:
    """Build the goal for a revise run: address the review, on the PR's existing branch.

    The review body is untrusted input exactly like an issue body — the goal is untrusted by design
    (gates certify the result, not the request). A changes-requested review may carry no summary
    comment (inline-only reviews), so the fallback points the agent at the PR's inline comments
    rather than handing it an empty instruction.
    """
    feedback = event.body or (f"(The review has no summary comment — the feedback is in the inline "
                              f"review comments on PR #{event.issue_number}.)")
    return (f"A reviewer requested changes on PR #{event.issue_number} ({event.title!r}).\n\n"
            f"Address this review feedback:\n\n{feedback}\n\n"
            f"The branch already holds the PR's commits — continue from them; do not start over "
            f"and do not open a new PR (pushing the branch updates the existing one).")


def parse_gitlab_event(payload: dict, delivery_id: str = "-") -> WebhookEvent | None:
    """Map a GitLab *issue* webhook payload into a `WebhookEvent`, or None for ones we don't act on.

    GitLab's shape differs from GitHub's: the kind is `object_kind: "issue"`, the issue lives under
    `object_attributes` (per-project number = `iid`, body = `description`), the repo is `project`
    (`path_with_namespace` + `git_http_url`), and labels are a top-level array of `{title}`. The
    action vocabulary (`open`/`reopen`/`update`/`close`) is normalized to GitHub's so everything
    downstream (`should_trigger`, `event_to_run_spec`) is provider-agnostic.
    """
    if payload.get("object_kind") != "issue":
        return None
    oa = payload.get("object_attributes") or {}
    action = _GITLAB_ACTION.get(oa.get("action", ""), "")
    if action not in _GITLAB_CANDIDATE_ACTIONS:
        return None
    number = oa.get("iid")
    if number is None:
        return None
    project = payload.get("project") or {}
    labels = [lbl.get("title", "") for lbl in (payload.get("labels") or []) if lbl.get("title")]
    # `submitter` is deliberately left empty here: GitLab's static token isn't bound to the body, so a
    # payload `user.username` is forgeable. The GitLab path uses the listener's single pinned identity
    # (`WebhookApp.listener_submitter`) instead of trusting the delivery (S1).
    return WebhookEvent(
        delivery_id=delivery_id, event_type="issue", action=action,
        repo=project.get("path_with_namespace", ""), clone_url=project.get("git_http_url", ""),
        issue_number=int(number), title=(oa.get("title") or "").strip(),
        body=(oa.get("description") or "").strip(), labels=labels)


def parse_event_payload(payload: dict, delivery_id: str = "-") -> WebhookEvent | None:
    """Parse a raw forge issue-event payload, auto-detecting GitHub vs GitLab by its *shape*.

    The CI tier (`loopkit run --from-event`) hands us the event JSON straight off disk — Actions
    writes it to `$GITHUB_EVENT_PATH`, GitLab CI to a trigger variable — with **no HTTP headers** to
    read the event type from and **no signature** to verify (the forge already authenticated the
    trigger; the CI runner is the trust boundary, not loopkit). So unlike the webhook path, the forge
    has to be inferred from the body: a GitLab issue payload carries a top-level `object_kind`, a
    GitHub `pull_request_review` payload carries `review` + `pull_request`, and a GitHub `issues`
    payload has neither (it has `action` + `issue` + `repository`). Everything downstream is the
    same `WebhookEvent` the webhook path produces. Returns None for any non-actionable payload
    (a `workflow_dispatch`, a `closed` issue, an approving review), so the caller can report it cleanly.
    """
    if payload.get("object_kind"):                       # GitLab system-hook discriminator
        return parse_gitlab_event(payload, delivery_id)
    if "review" in payload and "pull_request" in payload:  # GitHub `pull_request_review` → revise
        return parse_review_event(payload, delivery_id)
    return parse_event("issues", payload, delivery_id)   # GitHub `issues` event


def should_trigger(event: WebhookEvent, trigger_label: str | None) -> bool:
    """Policy: does this event warrant a run, given an optional required label?

    With a `trigger_label` configured, only issues *carrying that label* dispatch a run (the label is
    the opt-in switch — works for `opened` already-labeled and for `labeled` adding it). Without one,
    only brand-new/revived issues (`opened`/`reopened`) trigger — a bare `labeled` won't, so editing
    labels doesn't spam runs.

    A **revise** event uses the branch prefix instead of the label gate: a loop-authored PR doesn't
    inherit the issue's labels, and the containment question is different — not "did a human opt this
    task in?" (the reviewer's changes-requested *is* the opt-in) but "is this PR the loop's own work?".
    """
    if event.kind == "revise":
        return event.branch.startswith(REVISE_BRANCH_PREFIX)
    if trigger_label:
        return trigger_label in event.labels
    return event.action in ("opened", "reopened")


def event_to_run_spec(event: WebhookEvent, *, image: str, adapter: str = DEFAULT_TRIGGER_ADAPTER,
                      submitter: str = "fleet", workers: int = 1, env_name: str = "prod",
                      image_pull_secret: str | None = "ghcr-pull") -> RunSpec:
    """Build the `RunSpec` for one issue: the issue's title+body *is* the goal, one run per issue.

    The run id encodes the issue (`<owner>-<repo>-issue-<n>`, sanitized) so `loopkit cloud ls` is
    legible and the namespace is traceable back to its trigger; the issue number + submitter ride in
    labels for the same reason. Defaults to (and only allows) an **API adapter** on this untrusted
    path (C4). A single-worker fan-out (the issue is one task) — wider fan-out is for blind
    multi-attempt runs, not a targeted issue fix.
    """
    if event.kind != "issue":
        # A revise run must resume the PR's existing branch, and RunSpec has no branch to carry —
        # the cloud tier can't express it yet. The CI tier handles revise (`loopkit run --from-event`
        # checks the branch out itself); this guard keeps a half-right cloud run from ever starting.
        raise ValueError(f"a {event.kind!r} event can't become a cloud run yet (RunSpec has no "
                         f"branch to resume) — revise runs are CI-tier only, via `loopkit run --from-event`.")
    assert_trusted_adapter(adapter)                          # no CLI adapter on an untrusted run
    goal = f"{event.title}\n\n{event.body}".strip() if event.body else event.title
    run_id = sanitize_run_id(f"{event.repo}-issue-{event.issue_number}")
    return RunSpec(
        run_id=run_id, image=image, target=event.clone_url or event.repo,
        goal=goal or f"Resolve issue #{event.issue_number}", workers=workers, adapter=adapter,
        submitter=submitter, env_name=env_name, image_pull_secret=image_pull_secret,
        extra_labels={"loopkit.dev/issue": str(event.issue_number),
                      "loopkit.dev/trigger": "webhook"})


# --------------------------------------------------------------------------------------------
# Idempotency — dedupe deliveries so one issue starts at most one run.
# --------------------------------------------------------------------------------------------
class IdempotencyStore(Protocol):
    """A first-writer-wins reservation: `reserve(key)` is True the first time, False thereafter.

    `release(key)` undoes a reservation — called when a reserved delivery does NOT start a run after
    all (a `create_run` exception), so the issue isn't permanently marked "seen" and dead (G6). The
    refuse-on-unusable path never reserves in the first place, so it needs no release.
    """

    def reserve(self, key: str) -> bool: ...
    def release(self, key: str) -> None: ...


class InMemoryIdempotencyStore:
    """Process-local dedupe (a `set` + lock). Correct for a single-replica listener; tests use it.

    Scale-out (multiple listener replicas) needs a *shared* store so a re-delivery routed to a
    different pod still dedupes — that's `RedisIdempotencyStore`, backed by the cluster Redis. This
    one has no TTL; a bounded `max_keys` (FIFO eviction) keeps memory flat over a long uptime, at the
    cost of possibly re-triggering a very old issue — acceptable for the in-memory tier.
    """

    def __init__(self, *, max_keys: int = 10_000) -> None:
        self._seen: dict[str, None] = {}
        self._max = max_keys
        self._lock = threading.Lock()

    def reserve(self, key: str) -> bool:
        with self._lock:
            if key in self._seen:
                return False
            self._seen[key] = None
            if len(self._seen) > self._max:
                self._seen.pop(next(iter(self._seen)))     # evict oldest (insertion order)
            return True

    def release(self, key: str) -> None:
        with self._lock:
            self._seen.pop(key, None)                      # undo a reservation that didn't start a run


class RedisIdempotencyStore:
    """Shared dedupe across listener replicas via Redis `SET key 1 NX EX ttl` (atomic first-writer).

    `SET ... NX` sets the key only if absent and returns truthy exactly for the writer that won the
    race — so concurrent re-deliveries across pods still yield one run. The TTL bounds memory and
    lets a long-idle issue be re-triggered later (a feature: re-running a stale issue after the
    window). Deferred redis import (the `[fleet]` extra the worker image already ships).
    """

    def __init__(self, client, *, prefix: str = "loopkit:webhook:seen:", ttl_seconds: int = 86_400) -> None:
        self._r = client
        self._prefix = prefix
        self._ttl = ttl_seconds

    @classmethod
    def from_url(cls, url: str, **kw) -> "RedisIdempotencyStore":
        import redis                                         # deferred — only the cloud listener needs it
        return cls(redis.Redis.from_url(url, decode_responses=True), **kw)

    def reserve(self, key: str) -> bool:
        return bool(self._r.set(self._prefix + key, "1", nx=True, ex=self._ttl))

    def release(self, key: str) -> None:
        self._r.delete(self._prefix + key)                 # free the key so a fixed re-delivery can run


# --------------------------------------------------------------------------------------------
# Providers — the per-forge front-end (auth scheme + payload shape). Everything downstream
# (idempotency, event_to_run_spec, create_run) is provider-neutral, so a provider is small.
# --------------------------------------------------------------------------------------------
class WebhookProvider(Protocol):
    """The forge-specific slice of a delivery: how to authenticate it and how to read its payload."""

    name: str

    def authenticate(self, secret: str, body: bytes, headers: Mapping[str, str]) -> bool: ...
    def event_type(self, headers: Mapping[str, str]) -> str: ...
    def delivery_id(self, headers: Mapping[str, str]) -> str: ...
    def is_ping(self, event_type: str) -> bool: ...
    def parse(self, event_type: str, payload: dict, delivery_id: str) -> WebhookEvent | None: ...


class GitHubProvider:
    """GitHub: HMAC-signed body (`X-Hub-Signature-256`), `issues` events, a `ping` on creation."""

    name = "github"

    def authenticate(self, secret, body, headers):
        return verify_signature(secret, body, headers.get(HEADER_SIGNATURE.lower()))

    def event_type(self, headers):
        return headers.get(HEADER_EVENT.lower(), "")

    def delivery_id(self, headers):
        return headers.get(HEADER_DELIVERY.lower(), "-")

    def is_ping(self, event_type):
        return event_type == "ping"

    def parse(self, event_type, payload, delivery_id):
        return parse_event(event_type, payload, delivery_id)


class GitLabProvider:
    """GitLab: static secret token (`X-Gitlab-Token`), `object_kind: issue` payload, no ping event."""

    name = "gitlab"

    def authenticate(self, secret, body, headers):
        return verify_token(secret, headers.get(GITLAB_HEADER_TOKEN.lower()))

    def event_type(self, headers):
        return headers.get(GITLAB_HEADER_EVENT.lower(), "")

    def delivery_id(self, headers):
        return headers.get(GITLAB_HEADER_DELIVERY.lower(), "-")

    def is_ping(self, event_type):
        return False                                         # GitLab has no ping; "Test" sends a real event

    def parse(self, event_type, payload, delivery_id):
        return parse_gitlab_event(payload, delivery_id)


def provider_for(name: str | None) -> WebhookProvider:
    """Resolve a provider by name (`github` default | `gitlab`). Raises on anything else."""
    key = (name or "github").lower()
    if key == "github":
        return GitHubProvider()
    if key == "gitlab":
        return GitLabProvider()
    raise ValueError(f"unknown webhook provider {name!r} (expected 'github' or 'gitlab')")


# --------------------------------------------------------------------------------------------
# The webhook application — pure dispatch over an injected `create_run`, then the HTTP shell.
# --------------------------------------------------------------------------------------------
@dataclass
class WebhookResponse:
    """An HTTP status + a short message; the handler writes it back, tests assert on it."""

    status: int
    message: str


# What turns a built RunSpec + its resolved creds into a created run. Injected so the dispatch logic
# is testable with a recorder; the real listener binds it to a guarded `cloudrun.create_run(...,
# in_cluster=True, creds=...)`.
RunStarter = Callable[[RunSpec, "dict[str, str]"], str]
# Resolves a spec's submitter to creds (a `ResolvedCreds`). Injected; the real listener binds it to
# `creds.resolve_for_run(Identity(...), allow_fleet_fallback=...)`. Default = permissive (tests).
CredsResolver = Callable[[RunSpec], ResolvedCreds]


def _default_resolve(_spec: RunSpec) -> ResolvedCreds:
    """The dispatch tests' default: treat every run as authorized (the CLI injects the real resolver)."""
    return ResolvedCreds({}, source="submitter")


@dataclass
class WebhookApp:
    """The listener's logic, independent of the socket: verify → parse → authorize → dedupe → create.

    Holds the configuration (the shared secret, the worker image, the idempotency store, the run
    factory + resolver, and the **provider** — GitHub or GitLab) and exposes `dispatch(...)` returning a
    `WebhookResponse`. The HTTP server is a thin shell (`serve`) that reads the request and calls this —
    so the whole security-critical path is unit-testable with no network: forged auth → 401, ping →
    200, ignored event → 204, unregistered submitter → 403 (no run, **no dedupe burned**), valid → 202
    (create called once), duplicate → 200. The provider supplies the two things that differ between
    forges (how to authenticate, how to read the payload); everything else is shared.

    **Identity (C3):** the submitter is the issue AUTHOR for GitHub; for GitLab (forgeable token) it's
    `listener_submitter`, the listener's single pinned identity (S1). Resolution runs **before** the
    idempotency reservation (G6) so a refused/unauthorized delivery can run later, once registered.
    """

    secret: str
    image: str
    create: RunStarter
    resolve: CredsResolver = _default_resolve
    store: IdempotencyStore = field(default_factory=InMemoryIdempotencyStore)
    provider: WebhookProvider = field(default_factory=GitHubProvider)
    adapter: str = DEFAULT_TRIGGER_ADAPTER
    workers: int = 1
    env_name: str = "prod"
    trigger_label: str | None = None
    image_pull_secret: str | None = "ghcr-pull"
    listener_submitter: str | None = None        # the pinned identity for GitLab / a fallback

    def dispatch(self, *, headers: Mapping[str, str], body: bytes) -> WebhookResponse:
        p = self.provider
        h = {k.lower(): v for k, v in headers.items()}       # case-insensitive lookups for providers
        event_type = p.event_type(h)
        delivery_id = p.delivery_id(h)
        dlog = log.bind(forge=p.name, delivery=delivery_id[:8] if delivery_id else "-",
                        event=event_type or "-")
        # 1. Authenticate first — a forged/unsigned POST never reaches the parser (fail-closed).
        if not p.authenticate(self.secret, body, h):
            dlog.warn("hook.unauthorized", reason="bad_or_missing_auth")
            return WebhookResponse(401, "invalid or missing authentication")
        # 2. A forge connectivity check (GitHub's `ping`) — ack it without doing work.
        if p.is_ping(event_type):
            dlog.info("hook.ping")
            return WebhookResponse(200, "pong")
        # 3. Parse the JSON body, then narrow to actionable issue events (provider-specific shape).
        try:
            payload = json.loads(body or b"{}")
        except json.JSONDecodeError:
            dlog.warn("hook.bad_json")
            return WebhookResponse(400, "malformed JSON body")
        event = p.parse(event_type, payload, delivery_id)
        if event is None or not should_trigger(event, self.trigger_label):
            dlog.info("hook.ignored")
            return WebhookResponse(204, "event ignored")
        ilog = dlog.bind(repo=event.repo, issue=event.issue_number)
        # A revise event is parsed (and would dedupe per review round) but the cloud tier can't
        # resume a PR branch yet — defer it explicitly rather than starting a wrong-workspace run.
        # The CI tier (`loopkit run --from-event`) is where revise runs execute today.
        if event.kind != "issue":
            ilog.info("hook.revise_deferred", branch=event.branch, review=event.review_id)
            return WebhookResponse(204, "revise events run on the CI tier for now; ignored here")
        # 4. Resolve the submitter's identity → key BEFORE reserving (G6). The issue author (GitHub) or
        #    the pinned listener identity (GitLab); a build/adapter error or an unregistered submitter
        #    refuses here, leaving the dedupe key untouched so a later registration + re-delivery works.
        submitter = event.submitter or self.listener_submitter or "fleet"
        try:
            spec = event_to_run_spec(event, image=self.image, adapter=self.adapter, submitter=submitter,
                                     workers=self.workers, env_name=self.env_name,
                                     image_pull_secret=self.image_pull_secret)
        except ValueError as exc:                            # e.g. a CLI adapter on an untrusted run
            ilog.warn("hook.refused", reason="bad_spec", detail=str(exc)[:120])
            return WebhookResponse(422, f"cannot start run: {exc}")
        resolved = self.resolve(spec)
        if not resolved.usable:
            ilog.warn("hook.unauthorized_submitter", submitter=submitter[:40], source=resolved.source)
            return WebhookResponse(403, f"no registered credentials for submitter {submitter!r}")
        # 5. Idempotency — first writer wins; a re-delivery (or second matching event) is a no-op.
        if not self.store.reserve(event.dedupe_key):
            ilog.info("hook.duplicate", key=event.dedupe_key)
            return WebhookResponse(200, f"duplicate delivery for {event.dedupe_key}; skipped")
        # 6. Submit through the one shared seam; on failure, RELEASE the dedupe key so the issue can
        #    retry once the transient cause clears (otherwise it would be marked seen-and-dead, G6).
        try:
            namespace = self.create(spec, resolved.data)
        except Exception as exc:                            # noqa: BLE001 — surface, don't crash the listener
            self.store.release(event.dedupe_key)
            ilog.error("hook.create_failed", error=type(exc).__name__, detail=str(exc)[:200])
            return WebhookResponse(500, f"failed to start run: {type(exc).__name__}")
        ilog.info("hook.started", run=spec.run_id, ns=namespace, submitter=submitter[:40],
                  creds=resolved.source)
        return WebhookResponse(202, f"started run {spec.run_id} in ns/{namespace}")


def serve(app: WebhookApp, *, host: str = "0.0.0.0", port: int = 8080):
    """Run the blocking HTTP listener (stdlib only). `GET /healthz` → 200; `POST /` → `app.dispatch`.

    A `ThreadingHTTPServer` so a slow `create_run` (a cluster round-trip) doesn't block the next
    delivery. This is the only place that binds a socket — everything security-relevant is in
    `WebhookApp.dispatch`, tested without it. Returns the server (call `.serve_forever()`); the CLI
    `loopkit cloud webhook` command does exactly that.
    """
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):                       # silence the default stderr access log
            pass                                            # we emit our own structured lines

        def _reply(self, resp: WebhookResponse) -> None:
            payload = resp.message.encode()
            self.send_response(resp.status)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self) -> None:                           # liveness/readiness probe
            self._reply(WebhookResponse(200, "ok") if self.path == "/healthz"
                        else WebhookResponse(404, "not found"))

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", 0) or 0)
            body = self.rfile.read(length) if length else b""
            # Hand the provider the raw headers; it reads whichever ones its forge uses.
            resp = app.dispatch(headers=dict(self.headers.items()), body=body)
            self._reply(resp)

    server = ThreadingHTTPServer((host, port), Handler)
    log.info("webhook.listen", forge=app.provider.name, host=host, port=port,
             label=app.trigger_label or "-")
    return server


# --------------------------------------------------------------------------------------------
# CronJob — `loopkit cloud schedule`: run `loopkit cloud run --in-cluster` on a timer (pure builder).
# --------------------------------------------------------------------------------------------
@dataclass
class ScheduleSpec:
    """A recurring run: a cron schedule + the `loopkit cloud run` arguments to fire. Plain data.

    The CronJob's container *is* the worker image (it ships the loopkit CLI), and it submits the run
    in-cluster as the `loopkit-control` ServiceAccount — the same identity the webhook listener uses,
    the same `create_run` the CLI calls. Exactly one of `from_issues` (a recurring issue sweep) or
    `goal` (a fixed recurring task) must be set.
    """

    name: str
    schedule: str                                # crontab expr, e.g. "0 9 * * *"
    target: str
    image: str
    from_issues: bool = False
    goal: str | None = None
    label: str | None = None
    provider: str = "auto"                       # issue forge: auto | github | gitlab (--from-issues)
    adapter: str = DEFAULT_TRIGGER_ADAPTER       # API adapter only on this untrusted path (C4)
    workers: int = 1
    env_name: str = "prod"
    submitter: str = "fleet"                     # whose key each firing spends (operator-authored)
    allow_fleet_fallback: bool = False           # cron is operator-authored, so fleet fallback is legit
    namespace: str = SYSTEM_NAMESPACE
    service_account: str = "loopkit-control"     # the only SA permitted to create runs (see 20-rbac)
    image_pull_secret: str | None = "ghcr-pull"
    concurrency_policy: str = "Forbid"           # don't overlap a slow run with the next firing
    history_limit: int = 3
    starting_deadline_seconds: int = 300

    def __post_init__(self) -> None:
        self.name = sanitize_run_id(self.name)
        if self.from_issues == bool(self.goal):
            raise ValueError("a schedule needs exactly one of --from-issues or --goal")
        assert_trusted_adapter(self.adapter)     # no CLI adapter on a scheduled (untrusted) run


def cronjob_command(spec: ScheduleSpec) -> list[str]:
    """The `loopkit cloud run …` argv the CronJob fires (in-cluster, non-interactive).

    Reuses the exact CLI path a human runs — `--in-cluster` switches auth to the pod's SA and
    `--yes` skips the confirm. The same `create_run` runs whether a person typed it or cron did.
    """
    cmd = ["cloud", "run", "--target", spec.target, "--image", spec.image,
           "--adapter", spec.adapter, "--workers", str(spec.workers), "--env", spec.env_name,
           "--in-cluster", "--yes"]
    if spec.submitter and spec.submitter != "fleet":         # the schedule's operator-pinned identity
        cmd += ["--as", spec.submitter]
    if spec.allow_fleet_fallback:
        cmd.append("--allow-fleet-fallback")
    if spec.from_issues:
        cmd.append("--from-issues")
        if spec.label:
            cmd += ["--label", spec.label]
        if spec.provider and spec.provider != "auto":
            cmd += ["--provider", spec.provider]
    else:
        cmd += ["--goal", spec.goal or ""]
    return cmd


def _schedule_labels(spec: ScheduleSpec) -> dict[str, str]:
    return {"app.kubernetes.io/part-of": "loopkit",
            "app.kubernetes.io/component": "schedule",
            "loopkit.dev/schedule": spec.name}


def build_cronjob(spec: ScheduleSpec) -> dict:
    """A `batch/v1 CronJob` that fires `loopkit cloud run --in-cluster` on `spec.schedule`.

    Runs as `loopkit-control` (token automount ON — it *must* reach the API to create the run
    namespace/Jobs), `restartPolicy: Never` (a failed firing waits for the next tick, not a hot
    retry loop), and `concurrencyPolicy: Forbid` so a long run isn't lapped. It carries **no
    credentials** of its own (G14): the inner `loopkit cloud run --in-cluster` resolves the
    submitter's key from `loopkit-system` at run-creation time, so a static shared key never sits in
    this long-lived pod. `LOOPKIT_CLOUD_CONTEXT=in-cluster` pins the guard for the in-cluster path.
    """
    labels = _schedule_labels(spec)
    container: dict = {
        "name": "loopkit",
        "image": spec.image,
        "command": ["loopkit"],
        "args": cronjob_command(spec),
        "env": [{"name": "LOOPKIT_CLOUD_CONTEXT", "value": cloud.IN_CLUSTER_CONTEXT},
                {"name": "LOOPKIT_WORKER_IMAGE", "value": spec.image},
                {"name": "LOOPKIT_ENV", "value": spec.env_name}],
    }
    pod: dict = {
        "serviceAccountName": spec.service_account,   # loopkit-control — may create run ns/Jobs
        "restartPolicy": "Never",
        "containers": [container],
    }
    if spec.image_pull_secret:
        pod["imagePullSecrets"] = [{"name": spec.image_pull_secret}]
    return {
        "apiVersion": "batch/v1", "kind": "CronJob",
        "metadata": {"name": spec.name, "namespace": spec.namespace, "labels": labels},
        "spec": {
            "schedule": spec.schedule,
            "concurrencyPolicy": spec.concurrency_policy,
            "startingDeadlineSeconds": spec.starting_deadline_seconds,
            "successfulJobsHistoryLimit": spec.history_limit,
            "failedJobsHistoryLimit": spec.history_limit,
            "jobTemplate": {"metadata": {"labels": labels},
                            "spec": {"template": {"metadata": {"labels": labels}, "spec": pod}}},
        },
    }


# --------------------------------------------------------------------------------------------
# Schedule operations — guard-first, with injectable seams (same shape as cloudrun).
# --------------------------------------------------------------------------------------------
@dataclass
class ScheduleSummary:
    """One CronJob's at-a-glance state for `loopkit cloud schedules`."""

    name: str
    schedule: str
    suspended: bool = False
    last_run: str | None = None


def create_schedule(spec: ScheduleSpec, *, expected=None, kubeconfig=None,
                    applier: Callable[[Sequence[dict]], None] | None = None) -> str:
    """Apply the CronJob — **after** the context guard passes. Returns its name.

    Like `create_run`, the guard runs first and unconditionally, so a schedule can never be created
    on the wrong cluster. `applier` records objects in a test; the default creates via the client.
    """
    cloud.check_context(cloud.current_context(kubeconfig), expected)   # guard FIRST — fail-closed
    from .cloudrun import _client_applier                              # reuse the 409-tolerant applier

    apply = applier or _client_applier(kubeconfig)
    obj = build_cronjob(spec)
    log.info("schedule.create", name=spec.name, schedule=spec.schedule,
             ns=spec.namespace, fromIssues=spec.from_issues)
    apply([obj])
    log.info("schedule.created", name=spec.name)
    return spec.name


def delete_schedule(name: str, *, expected=None, kubeconfig=None, namespace: str = SYSTEM_NAMESPACE,
                    deleter: Callable[[str], None] | None = None) -> str:
    """Delete a CronJob by name — guard first. Returns the (sanitized) name."""
    cloud.check_context(cloud.current_context(kubeconfig), expected)   # guard FIRST
    safe = sanitize_run_id(name)
    remove = deleter or _client_cronjob_deleter(kubeconfig, namespace)
    log.info("schedule.delete", name=safe, ns=namespace)
    remove(safe)
    log.info("schedule.deleted", name=safe)
    return safe


def list_schedules(*, kubeconfig=None, namespace: str = SYSTEM_NAMESPACE,
                   lister: Callable[[], list[ScheduleSummary]] | None = None) -> list[ScheduleSummary]:
    """List loopkit CronJobs in `loopkit-system` (read-only — no guard needed)."""
    fetch = lister or _client_cronjob_lister(kubeconfig, namespace)
    return fetch()


def _client_cronjob_deleter(kubeconfig, namespace: str) -> Callable[[str], None]:
    from kubernetes import client
    from kubernetes.client.exceptions import ApiException

    batch = client.BatchV1Api(cloud.api_client(kubeconfig))

    def remove(name: str) -> None:
        try:
            batch.delete_namespaced_cron_job(name, namespace)
        except ApiException as exc:
            if exc.status != 404:                            # already gone is success
                raise

    return remove


def _client_cronjob_lister(kubeconfig, namespace: str) -> Callable[[], list[ScheduleSummary]]:
    from kubernetes import client

    batch = client.BatchV1Api(cloud.api_client(kubeconfig))

    def fetch() -> list[ScheduleSummary]:
        cjs = batch.list_namespaced_cron_job(
            namespace, label_selector="app.kubernetes.io/component=schedule")
        out: list[ScheduleSummary] = []
        for cj in cjs.items:
            status = getattr(cj, "status", None)
            last = getattr(status, "last_schedule_time", None) if status else None
            out.append(ScheduleSummary(
                name=cj.metadata.name, schedule=cj.spec.schedule,
                suspended=bool(getattr(cj.spec, "suspend", False)),
                last_run=str(last) if last else None))
        return out

    return fetch
