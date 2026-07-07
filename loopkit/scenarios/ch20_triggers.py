"""Ch 20 — triggers as infrastructure: a signed webhook is the trigger, fail-closed.

Chapter 12 ended on the trigger seam: *the worker is indifferent to what woke it*, so anything that
can submit a task drives the fleet. This chapter makes the cheapest real-world trigger concrete — a
forge **webhook** — and confronts what changes the moment the trigger is a public HTTP endpoint:
**authentication** (an unsigned POST must never start a paid run) and **idempotency** (forges
re-deliver, and one issue emits several matching events — `opened`, then `labeled` — so the loop must
fire **at most once per issue**).

The whole security-critical path is `triggers.WebhookApp.dispatch`, a *pure function over headers +
body* — no socket, no cluster, no tokens. This lab drives it through six deliveries with an injected
`create` recorder standing in for `cloudrun.create_run`, so you watch the run count change (or not):

    forged signature → 401, 0 runs   ·   signed issue → 202, 1 run   ·   re-deliver → 200 dup, still 1
    second event (labeled) → 200 dup, still 1   ·   unlabeled issue → 204 ignored
    changes-requested review → a REVISE event (per-round dedupe key; runs on the CI tier)   ·   tally

Same `create_run` seam the CLI and the CronJob call — a cron, a webhook, and a human are one code path.
"""
from __future__ import annotations

import json

from rich.table import Table

from ..extensions import triggers
from . import Scenario, Stage

SECRET = "the-shared-webhook-secret"          # GitHub HMAC-signs each body under this; never sent
IMAGE = "ghcr.io/acme/loopkit-worker:demo"
TRIGGER_LABEL = "loopkit"                      # only issues bearing this label dispatch a run


def _issue_payload(*, number: int, title: str, body: str, action: str = "opened",
                   labels: tuple[str, ...] = (TRIGGER_LABEL,), repo: str = "acme/widgets") -> dict:
    """A GitHub `issues` webhook payload — the shape the forge POSTs to the listener."""
    return {"action": action,
            "issue": {"number": number, "title": title, "body": body,
                      "user": {"login": "ada"},                       # the ISSUE AUTHOR (whose key a run spends)
                      "labels": [{"name": n} for n in labels]},
            "repository": {"full_name": repo, "clone_url": f"https://github.com/{repo}.git"}}


def _headers(body: bytes, *, secret: str, delivery: str) -> dict[str, str]:
    """GitHub delivery headers: the event type, a delivery id, and the HMAC signature of the body."""
    return {"X-GitHub-Event": "issues",
            "X-GitHub-Delivery": delivery,
            "X-Hub-Signature-256": triggers.sign(secret, body)}


def run(stage: Stage) -> None:
    stage.beat("Chapter 12 left us with the [bold]trigger seam[/]: a worker is indifferent to what "
               "woke it, so anything that can submit a run drives the fleet. The cheapest real "
               "trigger is a forge [bold]webhook[/] — but it's a [bold]public HTTP endpoint[/], so "
               "before the loop does anything it must answer two questions an in-cluster queue never "
               "had to: [italic]is this delivery real[/], and [italic]have I already handled this "
               "issue[/]?")

    created: list = []                          # the injected create_run recorder — one entry per started run

    def record_create(spec, creds) -> str:      # stands in for cloudrun.create_run(..., creds=...)
        created.append(spec)
        return f"ns/run-{spec.run_id}"

    app = triggers.WebhookApp(secret=SECRET, image=IMAGE, create=record_create,
                              store=triggers.InMemoryIdempotencyStore(), trigger_label=TRIGGER_LABEL)
    stage.beat(f"The listener is up with a shared secret and a label gate ([bold]{TRIGGER_LABEL}[/]). "
               "`dispatch(headers, body)` is a pure function — every decision below runs with no "
               "socket, no tokens. The injected [bold]create[/] recorder is our stand-in for "
               "`cloudrun.create_run`, so the [bold]runs[/] column is ground truth.")

    rows: list[tuple[str, str, int]] = []

    # 1. A forged delivery: a valid-looking labeled issue, but signed with the WRONG secret.
    body = json.dumps(_issue_payload(number=42, title="Add retry to the uploader",
                                     body="Retry 5xx 3x with backoff.")).encode()
    forged = {"X-GitHub-Event": "issues", "X-GitHub-Delivery": "d-forged",
              "X-Hub-Signature-256": triggers.sign("attacker-guess", body)}     # wrong secret
    resp = app.dispatch(headers=forged, body=body)
    rows.append(("forged signature", f"{resp.status} {resp.message[:24]}", len(created)))
    stage.beat(f"[bold]Forged[/] (signed with the wrong secret) → [red]{resp.status}[/], and "
               "crucially [bold]0 runs[/]. `verify_signature` recomputes the HMAC and compares in "
               "constant time; every failure mode returns False, so a forged or unsigned POST can "
               "never reach the parser, let alone start a paid run. [italic]Fail-closed[/].")

    # 2. The same issue, correctly signed → exactly one run.
    resp = app.dispatch(headers=_headers(body, secret=SECRET, delivery="d-1"), body=body)
    rows.append(("signed issue #42 (opened)", f"{resp.status} {resp.message[:24]}", len(created)))
    spec = created[-1]
    stage.beat(f"[bold]Correctly signed[/] → [green]{resp.status}[/], [bold]1 run[/]: "
               f"[bold]{spec.run_id}[/]. The issue's title+body [italic]is[/] the goal, and the run "
               "is bound to the issue [bold]author[/] (ada) — not whoever clicked, so an attacker can "
               "only ever spend a key registered to themselves.")

    # 3. Re-delivery of the identical event (forges retry on any non-2xx / timeout).
    resp = app.dispatch(headers=_headers(body, secret=SECRET, delivery="d-1-retry"), body=body)
    rows.append(("re-delivery (retry)", f"{resp.status} {resp.message[:24]}", len(created)))

    # 4. A SECOND matching event for the same issue: opened, now labeled. Different event, same issue.
    body2 = json.dumps(_issue_payload(number=42, title="Add retry to the uploader",
                                      body="Retry 5xx 3x with backoff.", action="labeled")).encode()
    resp = app.dispatch(headers=_headers(body2, secret=SECRET, delivery="d-2"), body=body2)
    rows.append(("second event (labeled)", f"{resp.status} {resp.message[:24]}", len(created)))
    stage.beat("A retry [italic]and[/] a genuinely different event (`labeled` after `opened`) for the "
               "same issue both come back [yellow]200 duplicate[/] — [bold]still 1 run[/]. The "
               "idempotency key is the [bold]issue identity[/] (`repo#number`), not the delivery id, "
               "so one issue maps to at most one run no matter how many events it emits.")

    # 5. An issue WITHOUT the trigger label → ignored (the opt-in switch).
    body3 = json.dumps(_issue_payload(number=43, title="Typo in the README", body="s/teh/the/",
                                      labels=())).encode()
    resp = app.dispatch(headers=_headers(body3, secret=SECRET, delivery="d-3"), body=body3)
    rows.append(("unlabeled issue #43", f"{resp.status} {resp.message[:24]}", len(created)))
    stage.beat(f"An issue without the [bold]{TRIGGER_LABEL}[/] label → [dim]204 ignored[/], no run. "
               "The label is the opt-in switch: a backlog stays quiet until someone deliberately "
               "hands it to the loop.")

    # 6. The loop's PR gets a changes-requested review — the trigger for the POST-PR follow-through.
    review = {"action": "submitted",
              "review": {"id": 901, "state": "changes_requested",
                         "body": "Retry only idempotent requests; add a test.",
                         "user": {"login": "grace"}},
              "pull_request": {"number": 88, "title": "loopkit: Add retry to the uploader",
                               "head": {"ref": "loopkit/issue-42"}, "labels": []},
              "repository": {"full_name": "acme/widgets",
                             "clone_url": "https://github.com/acme/widgets.git"}}
    body4 = json.dumps(review).encode()
    headers4 = dict(_headers(body4, secret=SECRET, delivery="d-4"))
    headers4["X-GitHub-Event"] = "pull_request_review"
    resp = app.dispatch(headers=headers4, body=body4)
    rows.append(("changes-requested review", f"{resp.status} {resp.message[:24]}", len(created)))
    event = triggers.parse_event("pull_request_review", review, "d-4")
    stage.beat(f"A reviewer [bold]requests changes[/] on the loop's own PR #88 → parsed as a "
               f"[bold]revise[/] event bound to the PR's branch ([bold]{event.branch}[/]) and the "
               f"[bold]reviewer's[/] key (grace). Note the dedupe key [bold]{event.dedupe_key}[/]: "
               "the idempotency semantics [italic]invert[/] — an issue runs [bold]at most once "
               "ever[/], but each new review [bold]round[/] is new work and gets its own key (only a "
               "re-delivery of the same review dedupes). The cloud listener defers it "
               f"([dim]{resp.status}[/]) — resuming a PR branch is the [bold]CI tier's[/] job "
               "(`loopkit run --from-event`, demo 21), where the loop follows through on its PR "
               "instead of stopping at 'opened'.")

    stage.console.print(_ledger(rows))
    stage.beat("Seven deliveries, [bold]one run[/]. And `dispatch` is the [bold]same create_run seam[/] "
               "the CLI (`loopkit cloud run`) and the CronJob (`loopkit cloud schedule`) call — a "
               "human, a cron, and a webhook are one code path, identical isolation. GitHub "
               "HMAC-signs the [italic]body[/]; GitLab sends a static [italic]token[/] (weaker — not "
               "bound to the body), so a GitLab listener pins its identity instead of trusting the "
               "payload. Next chapter: the tier that skips the listener entirely and lets the forge's "
               "own CI be the trigger.")


def _ledger(rows: list[tuple[str, str, int]]) -> Table:
    table = Table(title="webhook deliveries → runs", header_style="bold")
    table.add_column("delivery")
    table.add_column("response")
    table.add_column("runs so far", justify="right")
    for delivery, response, runs in rows:
        color = "green" if response.startswith("202") else (
            "red" if response.startswith("401") else "yellow")
        table.add_row(delivery, f"[{color}]{response}[/]", str(runs))
    return table


SCENARIO = Scenario(chapter=20, slug="triggers", title="Triggers as infrastructure",
                    teaches="A signed webhook is the trigger: fail-closed auth + idempotency turn a "
                            "public endpoint into exactly one run per issue (Ch 12 seam, productionized).",
                    live_supported=False, run=run)
