---
name: rights-and-ownership
description: "Decide whether the IP can be spun off: who owns it, what licenses govern it, and whether it can be open-sourced or sold as a hosted service."
---

# Rights and ownership

Set `asset.ownership` (`owned`, `third_party_oss`, `mixed`, `unknown`) from the
brief and evidence; never assume ownership the brief does not state.

For third-party open source: quote LICENSE, NOTICE and per-package license
fields; check network-use and field-of-use clauses (AGPL §13, SSPL, BSL/BUSL
change dates, Elastic, Commons Clause) and differently licensed directories
(`ee/`, `enterprise/`); read the CLA, trademark and commercial-use policies.

For owned IP the founder wants to open-source: identify what blocks release —
third-party code under incompatible terms, secrets or credentials in history,
customer data, patents, employer or contractor ownership — and record each as a
`blocker` with `resolve_with` (`owner`, `counsel`, `code_cleanup`, `agent`).

Fill `rights`: `can_open_source`, `can_sell_hosted`, blockers and a
`license_recommendation` for the open and paid parts, citing claims. This is
interpretation, not legal advice; route genuine legal questions to `counsel`.
A permissive license does not grant trademark use. No license file means no
granted rights, not public domain.

`checklist.md` holds the preserved T6 guide.
