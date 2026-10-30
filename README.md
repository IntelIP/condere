# Condere: spin-off analysis on Deep Agents

Condere takes a piece of digital intellectual property, usually software, and
works out how to spin a business off it: open-source it, or build an open-core
business on open-source capability. It returns a compact decision JSON that a
coding agent (Codex or Claude Code) reads first, with links to the reasoning,
evidence and build plan behind it.

Condere never builds, deploys, publishes, contacts anyone or makes the founder's
decision. `MDA` is a project label, not LangChain's hosted Managed Deep Agents
service.

## Start here

- [Execution goal](goal.md): the spin-off objective, work order, completion
  criteria and the owner's authority for unattended iteration.
- [System design](system_design.md): architecture, contracts, storage and
  security boundaries.
- [Roadmap handoff](roadmap.md#current-handoff): what is done, measured results
  and the next action.
- [Infrastructure](infra/README.md): services, configuration and test commands.

## How an assessment runs

1. **Repository index.** Each admitted repository is indexed offline in SmolVM
   (Graphify graph, manifests, deployment, license and CI facts) before any
   model call.
2. **Lead analysis.** A Deep Agent on GPT-6.1 Sol (the owner's ChatGPT plan)
   plans the work and writes an `Analysis`: claims with evidence status, the
   asset, rights, buyer, two to four spin-off options with the case for and
   against, a next test and open questions. It delegates to:
   - `code-analyst` (Claude Opus 5.5): reads code in the sandbox;
   - `extractor` (Claude Haiku 5.5): reads and searches sources.
   Both return structured claims with exact quotes.
3. **Decision.** Jev (TypeSafe, via OpenRouter) reads a compact decision brief
   and answers typed questions: per-option gates and ratings, then verdict,
   option and route. Code enforces the gates and verdict rules and records every
   adjustment.
4. **Build plan.** For a chosen option, a short planner turns it into work
   packages tied to real code paths.
5. **Publish.** The compact `condere.spinoff.v1` JSON and its artifacts are saved
   atomically and served by HTTP and MCP.

Research uses Exa (with recency, live fetch, category and domain routing), live
page crawling of admitted links, and Apify for X, Reddit and YouTube. Every model
and research call goes through a spending ledger; quotes are verified against
saved sources; traces go to LangSmith.

## Reading a result

- `read_report`: the compact JSON (decision with probabilities, thesis and
  summary, option cases, next test, build plan, open questions, links).
  `format=markdown` returns a README.
- `read_artifact`: linked detail by its `ref`, such as
  `artifacts/reasoning.md`, `artifacts/claims.json` or
  `artifacts/decision-brief.md`. Each link's hash matches the report.
- `export_assessment`: a zip with the README, the JSON, every artifact and the
  cited source texts.

`codex/condere-spinoff` builds on `codex/condere-mda`. The earlier 26-question
pipeline is preserved at commit `e26d4b59`; its code is retired from new plans.
