---
name: managed-offering-design
description: Answer T9 — what a hosted open-core product would include — derived from accepted T1–T8 findings.
---

# Managed offering design (T9)

T9 runs after T1–T8 are accepted. Read them with `read_assessment` and their
cited sources before proposing anything. This answer is a proposal that Market and
Business work will test.

Method:

1. From T2 and T5, list the operational work a managed service would take over.
2. From T1, T3 and T4, identify the control-plane, multi-tenancy, isolation,
   metering, identity and upgrade work the codebase lacks for a hosted service.
3. From T6, confirm the license permits the proposed boundary.
4. Propose what stays open source and what is paid (hosting, scale, security,
   compliance, collaboration, support); tie each paid item to a cited burden.
5. Search for existing hosted offerings of the same technology, including the
   maintainers' own; they are direct competition.
6. Model build effort with explicit assumptions using `calculate` where numbers help.

Fill `managed_service_scope`, `open_core_boundary`, `multi_tenancy_gaps`,
`build_effort` and `differentiation_from_self_hosting`. Use `modelled` basis with
stated assumptions for the proposal; cite T-findings' sources for its inputs.
