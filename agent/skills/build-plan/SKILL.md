---
name: build-plan
description: "After the decision, turn the chosen option into a build plan a coding agent can execute, tied to real code paths."
---

# Build plan

Only after the decision. Target the chosen option.

- `keep_open`: component IDs that stay open.
- `build_paid`: capabilities to build for the paid part, each with the claims
  that justify it and a rough estimate.
- `architecture_delta`: what must change to sell it (for example tenancy, auth,
  metering, billing, upgrades, isolation, observability).
- `work_packages`: small, ordered packages with the paths they touch,
  dependencies and an acceptance check a coding agent can verify.
- `not_authorized`: building, deploying, publishing, outreach and spending are
  not authorized by this plan.

When `asset.ownership` is `none` (no existing software, model or dataset), the plan
is a product scope as well: `architecture_delta` names the minimum product's core
components, the first version and its first milestone, even when the chosen first
step is a test or a service, so the opportunity is fully described. For an
`ai_native_service`, it names the agent workflow: which steps agents perform, where a
human reviews or signs, which systems and records it touches, and what is measured
per outcome.

Distinguish verified code behaviour from proposed design. The plan is a
proposal for the founder, not an instruction to build.
