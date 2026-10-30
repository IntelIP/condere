---
name: architecture-analysis
description: Answer T1 — what the system is made of and how work moves through it — from the repository index, graph navigation and cited source.
---

# Architecture analysis (T1)

Every hosting and product judgment depends on knowing what actually runs. Read
the T1 contract in `read_assessment`. Start with the repository index: languages,
layout, entry points and graph hubs.

Method:

1. For each entry point, follow calls and imports with `graphify query`/`path`
   toward request handling, storage, network clients and background work.
2. Read the implementing files with `inspect_repository`; confirm each component's
   responsibility in code, not from its name.
3. Identify where state lives (databases, files, caches, object storage) and how
   data reaches it.
4. Use official architecture documentation where code is unavailable, citing it
   as vendor-reported.

Fill `components`, `data_flow`, `core_technology`, `entry_points` and
`state_and_storage` concretely: name modules and files. Label any inferred link.
Stop when the main path from input to stored result is cited end to end; do not
survey every module. A graph edge, README claim or filename alone is not evidence
of behavior.
