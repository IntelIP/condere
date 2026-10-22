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

Planning documents and infrastructure definitions are prepared. The Condere agent,
SmolVM adapter, assessment API, and MCP client are not implemented on this branch.
Langfuse and SmolVM have not been launched or demonstrated by this change. No paid
assessment has run. See the [handoff](roadmap.md#current-handoff) for checked scope
and the next action.

`codex/condere-mda` has an independent Git history. The existing Agno implementation
and its uncommitted work remain in their existing branches/worktrees. This branch
does not import or run that application. Selected research methods and contracts
will be ported through the backlog; do not merge the legacy tree wholesale.
