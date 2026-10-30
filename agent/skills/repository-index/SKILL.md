---
name: repository-index
description: Start Technical work from the saved deterministic repository index and its Graphify graph, then verify claims in cited source files.
---

# Repository index

Before any worker starts, Condere acquires each admitted repository at an exact
commit and indexes it in an offline sandbox, without a model. `read_assessment`
lists each index under `repository_indexes`: repository, commit, saved `source_id`,
and the `workspace_path` where its files are copied into your sandbox.

The saved index source is a readable report. Use `read_source` or
`search_saved_sources` on it for:

- languages and file counts; top-level layout; documentation files
- entry points (CLIs, `main` modules, package `bin`/scripts, server start commands)
- parsed dependency manifests with declared versions, and dependency signals for
  databases, queues, caches, cloud SDKs and model providers
- deployment artifacts: Dockerfiles (base images, ports, entrypoints), Compose
  services, Helm charts, Kubernetes kinds, Terraform providers, platform configs
- CI workflows, license files and license fields, referenced environment variables
- Graphify hubs: the most connected code symbols, and what was skipped

In `workspace_path` you will find `facts.json` (the same facts as JSON) and
`graph.json` (Graphify's AST graph). Query the graph instead of rebuilding it:

```sh
graphify query "how are requests authenticated" --graph WORKSPACE_PATH/graph.json --budget 800
graphify explain "SymbolName" --graph WORKSPACE_PATH/graph.json
graphify path "EntryPoint" "Storage" --graph WORKSPACE_PATH/graph.json
graphify affected "SymbolName" --graph WORKSPACE_PATH/graph.json
graphify god-nodes --top 15 --graph WORKSPACE_PATH/graph.json
```

The index is navigation, produced by pattern matching and static parsing. YAML is
pattern-matched, not fully parsed; dynamic imports and generated code can be
missing. Before submitting a code claim, call `inspect_repository` with the exact
file paths to save a citable copy, then cite its exact text. The index source itself
is citable for inventory facts (for example, which deployment files exist).
A missing entry is never evidence that a capability is absent.
