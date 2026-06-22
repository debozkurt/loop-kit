# Part III — prior art & lessons from the field

> **What this is.** A grounded survey of the canonical agentic-loop harnesses (Anthropic's own,
> the SWE coding agents, the framework/durability runtimes, the eval harnesses) mapped onto loopkit's
> design — what *validates* loopkit's bets, what loopkit was *under-weighting* (now adopted), and what
> remains worth doing. Companion to the [architecture wiki](architecture/README.md); the course (loops
> manual) carries the teaching form (`loops/prior-art.md`).

## Verdict

loopkit is unusually well-aligned with the field — most of this corpus reads as *external validation*
of its existing invariants (gates, three stops, git durability, keyless executor, best-of-N +
re-validation), not a list of things it's missing. The real lessons cluster in three places:
**tool/gate ergonomics (the ACI)**, the **measurement layer**, and **intra-run context**. Three cheap,
field-validated wins were adopted this pass; the rest are tracked below.

## Bets the field confirms (cite these in the wiki)

| loopkit bet | Confirmed by | The lesson, in their words |
|---|---|---|
| Gates + three hard stops are the loop, not features | [Building Effective Agents](https://www.anthropic.com/research/building-effective-agents) | An agent runs on *"ground truth from the environment at each step"* until *"stopping conditions"* — these are the two load-bearing parts of any loop. |
| Held-out acceptance gate the agent never sees | [SWE-bench](https://www.swebench.com), [inspect_ai](https://inspect.aisi.org.uk) (Target/grader separation), Claude Code best-practices (*"a fresh model refutes rather than grades its own work"*) | The moment an agent can read its grader, "solve the task" degenerates into "satisfy these asserts." |
| Best-of-N + **re-validate the winner** on a held-out check | [SWE-Gym](https://arxiv.org/abs/2412.21139), [R2E-Gym](https://arxiv.org/abs/2504.07164) | The large **Best@K-vs-Pass@K gap** is the empirical proof the re-validation step is load-bearing — the selector routinely mis-ranks; only the held-out gate certifies. |
| commit-every-tick git durability | [Aider](https://aider.chat), [Codex CLI](https://github.com/openai/codex) rollouts | git is the cheapest checkpoint/undo log; loopkit's is **host-portable** (better than the SDKs' machine-local JSONL sessions for a fleet). |
| Keyless-executor sidecar (gate runs off the key) | [OpenHands](https://github.com/All-Hands-AI/OpenHands) client/server runtime, inspect_ai (*"the harness sits outside the sandbox"*), [Berkeley RDI](https://rdi.berkeley.edu/blog/trustworthy-benchmarks-cont/) (*"the agent's code runs in the same environment the evaluator inspects"* is the flaw) | Run the verifier in a *different boundary* than the agent's tool calls. |
| Protected-paths / never-`main` / deny-by-default | Claude Agent SDK (*"deny rules always win; `bypassPermissions` ignores allow-lists"*), [Codex sandboxing](https://developers.openai.com/codex/concepts/sandboxing) (OS-sandbox × approval as orthogonal axes) | Allow-lists are convenience; **deny-lists are enforcement** — put it at the OS/tool boundary, not the prompt. |
| Fresh-context-each-tick from anchors | [Effective context engineering](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents) ("context rot"), JIT-retrieval endorsement | As tokens grow, recall decays (n² attention dilution + short-sequence training bias). Re-deriving a small high-signal context each tick is a *context-rot mitigation*, not just a durability trick. |
| Independent fan-out + validated reseed (not colliding multi-agents) | [Cognition, "Don't build multi-agents"](https://cognition.com/blog/dont-build-multi-agents) | *Parallelize reading/investigation, keep writing single-threaded.* loopkit's evolve already respects this (workers are independent attempts; only the validated winner reseeds). |

## Lessons adopted this pass (Built 🟢)

1. **Edit-time validation as a tool-boundary guardrail** ([SWE-agent ACI](https://swe-agent.com/latest/background/aci/) — their most-cited win). `executor.validate_syntax` + `_WorkspaceTools._write` now **refuse a syntactically-broken `.py`/`.json` edit** (returns a steering error, never writes the file), so the agent fixes it on the next turn instead of corrupting the workspace and spending ticks unwinding it. Best-effort (only what the stdlib can parse cheaply; empty content allowed).
2. **Shaped gate feedback, not a blind tail** ([Writing tools for agents](https://www.anthropic.com/engineering/writing-tools-for-agents)). `executor.shape_failure_output` (used by `run_gate`) keeps the runner's summary tail **and surfaces failure-marker lines** the tail would truncate, budget-bounded. A gate's feedback is the agent's primary signal *and* it spends the budget stop — a 10k-line dump is both context-rot and money. Test-runner-agnostic; short output is unchanged (the prior contract).
3. **The two-oracle gate** ([SWE-bench](https://arxiv.org/abs/2310.06770)'s FAIL_TO_PASS + PASS_TO_PASS). DONE now requires the held-out acceptance gate (the *fix works*) **AND** an optional held-out `gate.regression` (previously-passing behavior *preserved*). A fix that passes its target by breaking something else fails. `config.gate.regression` + `run_loop(regression_gate=…)`, **None-safe** (unconfigured ⇒ acceptance alone certifies — exact prior behavior); a regression failure feeds back a distinct "fix without regressing" message.
4. **`pass^k` as a first-class reliability metric — the open-measurement-layer's first brick** ([tau-bench](https://arxiv.org/abs/2406.12045)). New `extensions/measure.py` + **`loopkit measure`**: run a goal N times as independent isolated trials (the fleet's `TaskRunner` seam — each a full `run_loop` graded by the held-out gate, so a trial passes only on DONE), then report **`pass^k`** (reliability — *all* k trials pass, the unbiased `C(c,k)/C(n,k)`, **falls** with k) alongside **`pass@k`** (discovery — *any* of k, the Codex `1−C(n−c,k)/C(n,k)`, rises with k). `evolve` optimizes the latter; production lives or dies on the former, and the gap is what the field under-tools. Every `ReliabilityReport` is **harness-stamped** (loopkit version + a signature over the gates/adapter/model/iter-cap + a timestamp) and JSON-serializable — *a number without its harness isn't a measurement* (SWE-bench Verified was retired in 2026 over a 10–20pt cross-scaffold swing). Scenario `demo 24`. This is the first concrete piece of the [open measurement layer](../../README.md) roadmap candidate.

*(Tamper defense — "the diff must not touch the verifier" — is already enforced by loopkit's
protected-path guard reverting any tick that writes a protected path; keep the gate's own files in
`safety.protected_paths`, as the demo does with `tests/`.)*

## Lessons to adopt next (ranked)

> **`pass^k` (was #1) is now Built 🟢** — see *Lessons adopted this pass* above (`extensions/measure.py`
> + `loopkit measure`). The remaining open thread is the rest of the open-measurement-layer: persisting
> a corpus of harness-stamped reports and the `pass^k`-vs-cost / convergence axes (#5's trajectory log
> feeds this).

1. **A persistent agent scratchpad — the interesting tension with fresh-context.** [Manus](https://manus.im/blog/Context-Engineering-for-AI-Agents-Lessons-from-Building-Manus) + Anthropic both land on **structured note-taking** (a `NOTES.md`/todo the agent writes and re-reads across resets) and *"keep the wrong stuff in"* (failed attempts are in-loop signal). loopkit's fresh-context is a strength (context-rot mitigation) but discards *intra-run* learning except via the single last-gate-feedback string — the agent rediscovers dead-ends each tick. A durable, agent-authored note channel rendered like an anchor would keep the fresh-context purity while adding memory. *Design before building — it touches the fresh-context philosophy.*
2. **A general `PreToolUse` hook seam.** The Claude Agent SDK (deny-wins hooks) and [Goose](https://goose-docs.ai/)'s `smart_approve` (an LLM-as-permission-judge for the read-only/destructive middle ground) generalize loopkit's protected-path guard. A pluggable pre-tool-use hook lets users enforce policy without forking core (fits "extend at the seams").
3. **A ranked repo-map primer for large repos.** [Aider's repo-map](https://aider.chat/docs/repomap.html) (tree-sitter signatures + PageRank over the dep graph, token-budgeted) and [AutoCodeRover](https://arxiv.org/abs/2404.05427)'s AST `search_*` primitives give the agent the *shape* of a big repo cheaply — closing a discovery gap that anchors + ad-hoc reads leave open at scale.
4. **An append-only event log + an explicitly idempotent tick.** [LangGraph](https://docs.langchain.com/oss/python/langgraph/durable-execution): *determinism + idempotent re-run units are the price of resumability* (the resume unit is the node/tick). loopkit's commit-every-tick is a clean step boundary; the sharpening is to make the per-tick unit explicitly idempotent and add a replayable, **offline re-gradeable** trajectory log (feeds the measurement layer — a corpus of harness-stamped `pass^k` reports).

## Where loopkit is already ahead / deliberate non-gaps

- **Exact per-model cost → a budget stop that bites** — most harnesses discuss tokens only qualitatively.
- **Two-layer observability** (payload-free logs + full-tree LangSmith traces) has no direct analogue.
- **Deterministic execution gates beat LLM-judge gates** for self-grading bias — so "run the grader as an isolated sub-agent" mostly doesn't apply (it only matters if loopkit adds an LLM-judge gate).
- **Code-as-action** ([smolagents](https://github.com/huggingface/smolagents): ~30% fewer LLM calls) is partly free — the `run_bash` tool already lets the agent script multi-step actions; the vendor CLI adapters use it natively. The deeper bet (the agent writes Python that calls tools) would *raise* the sandbox bar, which the keyless executor already pays.
- **Agent Skills ≠ learned skills.** [Anthropic's Agent Skills](https://www.anthropic.com/engineering/equipping-agents-for-the-real-world-with-agent-skills) are *human-authored* progressive-disclosure packages; learned skills are *explicitly future work* there. loopkit's [skills flywheel](part-iii-skills-repo.md) (a lesson distilled from a past run) is genuinely net-new territory — don't conflate the two.

## Sources

Anthropic: [Building Effective Agents](https://www.anthropic.com/research/building-effective-agents) ·
[Agent SDK](https://platform.claude.com/docs/en/agent-sdk/overview) ·
[Claude Code best practices](https://code.claude.com/docs/en/best-practices) ·
[Agent Skills](https://www.anthropic.com/engineering/equipping-agents-for-the-real-world-with-agent-skills) ·
[Effective context engineering](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents) ·
[Writing tools for agents](https://www.anthropic.com/engineering/writing-tools-for-agents) ·
[Code execution with MCP](https://www.anthropic.com/engineering/code-execution-with-mcp).
SWE: [SWE-agent](https://swe-agent.com/latest/background/aci/) · [OpenHands](https://github.com/All-Hands-AI/OpenHands) ·
[mini-swe-agent](https://github.com/SWE-agent/mini-swe-agent) · [Aider](https://aider.chat/docs/repomap.html) ·
[AutoCodeRover](https://arxiv.org/abs/2404.05427).
Frameworks: [OpenAI Agents SDK](https://openai.github.io/openai-agents-python/) · [Codex CLI](https://github.com/openai/codex) ·
[LangGraph](https://docs.langchain.com/oss/python/langgraph/durable-execution) · [smolagents](https://github.com/huggingface/smolagents) ·
[Goose](https://goose-docs.ai/) · [Cognition](https://cognition.com/blog/dont-build-multi-agents) ·
[Manus](https://manus.im/blog/Context-Engineering-for-AI-Agents-Lessons-from-Building-Manus).
Eval: [SWE-bench](https://arxiv.org/abs/2310.06770) / [Verified](https://openai.com/index/introducing-swe-bench-verified/) ·
[tau-bench](https://arxiv.org/abs/2406.12045) · [Terminal-Bench](https://www.tbench.ai) · [inspect_ai](https://inspect.aisi.org.uk) ·
[SWE-Gym](https://arxiv.org/abs/2412.21139) · [reward tampering](https://www.anthropic.com/research/reward-tampering).
