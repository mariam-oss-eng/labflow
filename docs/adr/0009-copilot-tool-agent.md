# ADR-0009 — AI Copilot as a small tool-using agent

**Status:** Accepted (v0.9, 2026-04-30)

## Context

Operators have been asking for a "chat with my LabFlow data" experience.
The tempting design is to dump the team's entire decision graph + open
tasks into an LLM context window and let the model freestyle. That
approach has three problems:

1. **It scales badly.** A team with 10 k decisions blows past any
   reasonable context window. We'd need RAG anyway.
2. **It's not auditable.** A free-form LLM response can't be tied back
   to specific rows in the database. Operators want to know exactly
   which tasks the assistant looked at when it gave an answer.
3. **It requires an LLM to do anything.** LabFlow has a strong "works
   offline by default" guarantee that we don't want to break.

## Decision

Build the Copilot as a **small tool-using agent** with seven explicit
tools (`search`, `get_task`, `list_open_tasks`, `list_decisions`,
`summarize_meeting`, `propose_task`, `analytics`). Each user turn:

1. Goes through a **rule-based intent classifier** that picks a tool +
   arguments. This is the deterministic offline planner — every test
   exercises it directly with no LLM in the loop.
2. **Executes the tool** against the team's data. Tool results are
   structured (lists of dicts) and bounded (`_MAX_RESULTS_PER_TOOL=10`).
3. **Composes a reply.** If `LABFLOW_COPILOT_LLM_CALLABLE` is set, the
   LLM phrases the answer; otherwise we render a structured Markdown
   synopsis directly from the tool result.
4. **Persists the entire turn** — user message, tool calls, assistant
   reply — to `copilot_sessions.transcript_json` and emits a
   `copilot.turn` audit event with the tool names used.

`propose_task` is intentionally side-effect-free (drafts only). The
agent never auto-creates tasks, decisions, or transitions; that
responsibility stays with explicit user actions.

## Consequences

* **Auditable by construction.** Every tool call is in the transcript
  and every turn is in the audit log. Compliance review is just SQL.
* **Works without an LLM.** The deterministic planner is the canonical
  implementation; the LLM is a phrasing layer. CI tests run with no
  network access.
* **Bounded blast radius.** No tool can write to the DB; the worst the
  agent can do on a misclassification is search for the wrong thing.
* **Easy to extend.** A new tool is a function + a row in `_TOOLS` + an
  entry in `TOOL_SCHEMA`. The schema is published at
  `/api/copilot/tools` so a real LLM can discover capabilities.
* **Single-turn by default.** The current loop runs one tool per turn.
  Multi-tool sequences are easy to enable when an LLM is wired in
  (the `_MAX_STEPS` cap is already in place).

## Alternatives considered

| Option | Why we passed |
|---|---|
| **Dump everything into the LLM context** | Doesn't scale; not auditable; locks us into an LLM dependency |
| **Pure RAG (retrieve-then-generate)** | Less flexible than tool use; can't compute `analytics` or fetch a specific task by id |
| **LangChain / LlamaIndex** | Hundreds of MB of transitive deps; opaque tool routing; we wanted a 400-line module we can audit end-to-end |
| **Function-call-only (no rule-based fallback)** | Would force every deployment to provide an LLM; breaks the offline guarantee |

## See also
* `labflow/copilot.py`
* `tests/test_v09.py::test_copilot_*`
* `docs/copilot.md`
