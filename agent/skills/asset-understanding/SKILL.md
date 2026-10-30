---
name: asset-understanding
description: Understand the digital IP itself: what it does, how it is built and run, what it depends on, what is hard to operate, how healthy it is, and where it applies.
---

# Asset understanding

Use first. The asset is the software (or other digital IP) the founder brought or
the brief points to. Everything later depends on knowing what it actually is.

Delegate code reading to the `code-analyst` subagent. Give it the repository, the
question, and the paths the saved index suggests. It works in the sandbox with the
Graphify graph, grep and files, and returns claims with exact quotes. Do not read
large files yourself.

Establish, with claims:
- components and what each does (map each to a path as a `component` with
  value `core`, `commodity` or `glue`): core is what is hard to rebuild;
- how it is built, packaged, deployed and configured, and what running it needs;
- dependencies, external services and their licenses;
- integrations and extension points;
- the operating burden a customer carries today (upgrades, scaling, backup,
  recovery, security) — this is what a hosted or supported offering could remove;
- project health and maturity from dated repository metadata;
- where it applies and where it is unsuitable.

A successful sandbox command is not a performance measurement. Documentation
claims are `reported`; behaviour seen in code is `observed`; your inference is
`modelled`. Record unknowns as `unknown` claims instead of guessing.

`checklist.md` holds the preserved Technical question guides (T1–T5, T7, T8):
evidence needed, search routes, weak signals and completion. Read the part you
need.
