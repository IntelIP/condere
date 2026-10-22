# Condere: self-hosted Deep Agents foundation

Condere helps a founder evaluate a technology as a possible software business,
then gives a person or coding agent a cited assessment and development handoff.

This branch starts a clean implementation using open-source **Deep Agents**,
self-hosted **Langfuse**, and self-hosted **SmolVM**. `MDA` is the branch's project
label; this is not LangChain's hosted Managed Deep Agents service.

## Start here

- [Execution goal](goal.md): reusable instruction to implement, integrate, and
  test the complete system through Roadmap Milestone 3.
- [System design](system_design.md): product scope, architecture, responsibilities,
  research coverage, storage, and security boundaries.
- [Backlog](backlog.md): bounded user outcomes, dependencies, and acceptance criteria.
- [Roadmap](roadmap.md): delivery sequence and launch milestones.
- [Demo](demo.md): the complete user journey and the observations needed to accept it.
- [Infrastructure](infra/README.md): Langfuse configuration and SmolVM startup plan.

## Current state

The local SmolVM adapter runs isolated commands and file operations through Deep
Agents. A deterministic test model discovers all six research skills and uses the
real VM; all 17 question-specific guides are preserved. Public repository
acquisition records an exact commit and verifies the same source inside the VM.
These checks demonstrate infrastructure behavior, not a real assessment.

Assessment persistence, bounded provider accounting, the four research/review
workers, reports and portable exports, and authenticated API/MCP adapters are
implemented. A complete fictional dossier now passes through the custom native
SmolVM image, PostgreSQL, actual local object storage, HTTP, and stdio MCP. The
sources and report remain retrievable after worker VM deletion; a separate storage
restart check retained saved source identities and a complete dossier. Research
models remain deterministic fixtures, so these results establish integration
behavior rather than business conclusions.

All six Langfuse services are healthy in a separate task-owned local VM. The
synthetic trace retrieval check still fails after restart. Integrated tracing,
remaining failure checks, the one live assessment, and retrieval inside both
coding clients remain unfinished. No paid assessment has run. See the
[installation guide](infra/README.md#api-and-coding-client-installation) and
[handoff](roadmap.md#current-handoff) for working commands and prerequisites.

`codex/condere-mda` has independent Git history. The original Agno implementation
and its uncommitted work remain in their existing branches/worktrees. Port useful
methods and contracts deliberately; do not merge the legacy tree wholesale.
