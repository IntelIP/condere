---
name: dependency-analysis
description: Answer T3 — what the technology depends on — separating bundled libraries from required external services, with licenses and risks.
---

# Dependency analysis (T3)

Dependencies drive hosting cost, license obligations and supply-chain risk. Read
the T3 contract. The repository index lists parsed manifests and dependency signals.

Method:

1. Read the manifests (and lockfiles where present) for the material runtime
   dependencies and their versions. Ignore development-only tooling unless it ships.
2. Separate libraries bundled into the build from external services the system
   must reach (databases, queues, identity providers, model APIs, cloud services).
3. For material dependencies, check license and maintenance status in their
   official repositories or registries. Cite what you read.
4. Note concentration risks: unmaintained, single-maintainer, copyleft, or
   vendor-locked dependencies.

Fill `runtime_dependencies`, `external_services`, `dependency_licenses` and
`supply_chain_risks`. Mark optional and development-only dependencies. A manifest
entry can be unused; popularity is not suitability.
