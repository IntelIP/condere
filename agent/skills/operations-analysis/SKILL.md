---
name: operations-analysis
description: Answer T5 — what is hard about operating the technology — the burdens a hosted offering would remove.
---

# Operations analysis (T5)

This is the core of an infrastructure-and-hosting business: customers pay to stop
doing operational work. Read the T5 contract and the accepted architecture and
deployment context when available.

Method:

1. From the index and code, read how the system handles state, migrations,
   upgrades, backups and restore, concurrency and scaling limits, and security
   patching. Cite the files and documentation.
2. Read operating guidance from maintainers (production checklists, scaling
   guides, known limitations).
3. Search public issues and discussions for operators' reported production
   problems. Attribute each report; one complaint is an anecdote, not a pattern.
4. Derive `hosting_value` only from burdens the evidence shows.

Fill `operational_tasks`, `scaling_characteristics`, `failure_and_recovery`,
`security_operations` and `hosting_value`. Do not claim performance without a
measurement receipt; "ops is hard" is not a finding. If evidence is missing,
say which observation would establish the burden.
