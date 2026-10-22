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

Langfuse configuration and image pins are prepared, but its launch is blocked by
the current Docker disk limit. Assessment persistence, spending controls, research
tools, final review/report delivery, API/MCP, and actual client use remain
unfinished. No paid assessment has run. See the [handoff](roadmap.md#current-handoff)
for the active goal and next actions.

`codex/condere-mda` has independent Git history. The original Agno implementation
and its uncommitted work remain in their existing branches/worktrees. Port useful
methods and contracts deliberately; do not merge the legacy tree wholesale.
