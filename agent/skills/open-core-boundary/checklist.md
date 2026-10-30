# Preserved question guides: T9, B2, B7
Ported verbatim from the 26-question method (commit e26d4b59). Use them as checklists, not output fields.

## T9. What would a hosted open-core product include?
- Decision: Propose the managed service and open-core boundary that the technical evidence supports, as input to Market and Business work.
- Evidence needed: Accepted T1–T8 findings: architecture, deployment, dependencies, integrations, operating burdens, license terms, project health and applications; existing hosted offerings for the same technology.
- Search routes:
  - Reread the accepted Technical findings and their citations before proposing anything.
  - Search for existing managed or hosted offerings of the same technology, including the maintainers' own.
- Weak signals: A feature list without a cited operating burden or buyer need is not a product boundary.
- Completion: Describe the managed scope, what stays open source, missing multi-tenant and control-plane work, a modelled build effort with assumptions, and why a customer would pay instead of self-hosting.
- Points to cover: managed service scope, open core boundary, multi tenancy gaps, build effort, differentiation from self hosting
- Contract rule: Derive the proposal from accepted T1–T8 findings and cite them; keep it a proposal with explicit assumptions and unknowns.

## B2. What would be free, paid and open source?
- Decision: Propose what is open source, free and paid, and the charging unit.
- Evidence needed: Buyer needs, reviewed pricing conditions, relevant license terms and constraints on the proposed open-core model.
- Search routes:
  - Reuse buyer and pricing evidence; inspect official feature tiers, billing terms and licenses where relevant.
  - Look for how the paid portion creates value for the identified buyer rather than copying a competitor's packaging.
- Weak signals: An existing open-core business does not prove our packaging will sell or that a license is suitable.
- Completion: Explain a provisional free/paid boundary and billing unit, with unresolved purchasing and license assumptions explicit.
- Points to cover: open core, paid value, billing unit, license approach
- Contract rule: Connect paid value to a buyer need; keep pricing and license suitability provisional where unverified.

## B7. Why could we win, and what would competitors find difficult to copy?
- Decision: Explain a plausible technical or business advantage and how easily it could be copied.
- Evidence needed: Reviewed Technical comparisons and Market evidence for the same customer task, plus counterevidence and explicit comparison conditions.
- Search routes:
  - Reuse source-backed Technical findings for architecture and Market findings for customer or distribution advantages.
  - Seek comparable measurements only when needed for a specific performance claim and allowed by scope.
- Weak signals: Code inspection cannot establish measured speed, cost or commercial superiority. Feature differences alone do not establish a durable advantage.
- Completion: Separate supported differences from advantage hypotheses and unknown comparisons; do not produce unsupported rankings or scores.
- Points to cover: technical advantage, business advantage, copyability, counterevidence
- Contract rule: Keep technical and business evidence separate; no unsupported scores, unmeasured performance wins or invented commercial superiority.
