# Preserved question guides: T1, T2, T3, T4, T5, T7, T8
Ported verbatim from the 26-question method (commit e26d4b59). Use them as checklists, not output fields.

## T1. How does the technology work?
- Decision: Establish what the system is made of and how work moves through it, as the basis for every hosting and product judgment.
- Evidence needed: Repository index facts and graph hubs, entry points, main modules and the files that implement them; official architecture documentation where code is unavailable.
- Search routes:
  - Start from the repository index: languages, entry points, graph hubs and directory layout.
  - Follow callers and imports from each entry point to storage, network and background-work code; read the implementing files.
- Weak signals: File names, README marketing language and graph edges alone describe intent, not behavior.
- Completion: Name each major component, its responsibility and the path between them with file-level citations; label any inferred link.
- Points to cover: components, data flow, core technology, entry points, state and storage
- Contract rule: Describe the actual components and request/data path from cited code or official documentation; separate observed structure from inferred design.

## T2. How is it built, packaged and deployed?
- Decision: Determine what it takes to run the technology, which is what a hosted offering would take over.
- Evidence needed: Dockerfiles, Compose files, Helm charts, Kubernetes manifests, Terraform, platform configs, build scripts, release artifacts and installation documentation.
- Search routes:
  - Read every deployment artifact the repository index lists; confirm images, ports, volumes and required services.
  - Read installation and self-hosting documentation and compare it with the deployment files.
- Weak signals: A Dockerfile proves a container build exists, not that it is production-ready; absent files do not prove a method is unsupported.
- Completion: List each deployment method with its requirements, configuration surface and stateful services, citing the files; state which methods are documented versus only present.
- Points to cover: packaging, runtime requirements, deployment methods, configuration, stateful services
- Contract rule: Report each supported way to run it, its required runtime and services, and its configuration from cited build and deployment files.

## T3. What does it depend on?
- Decision: Identify what the technology needs to function and what that implies for hosting cost, licensing and risk.
- Evidence needed: Dependency manifests and lockfiles, imported service SDKs, required databases/queues/caches, and license metadata for material dependencies.
- Search routes:
  - Start from the index's parsed manifests and dependency signals; read the manifests for versions.
  - Check material dependencies' license and maintenance status from their official repositories or registries.
- Weak signals: A dependency listed in a manifest may be optional, development-only or unused; package popularity is not suitability.
- Completion: Name the material runtime dependencies and external services, with licenses and risks cited; mark optional and development-only dependencies.
- Points to cover: runtime dependencies, external services, dependency licenses, supply chain risks
- Contract rule: Separate bundled libraries from required external services; tie license and risk statements to cited manifests or sources.

## T4. What does it integrate with, and how can it be extended?
- Decision: Determine how customers would connect the technology to their systems and where a product could add value.
- Evidence needed: Public API routes, CLI commands, SDKs, plugin or extension mechanisms, supported protocols and file formats, and integration code for third-party services.
- Search routes:
  - Locate route definitions, CLI entry points and plugin registries through the index and graph; read their implementations.
  - Read official integration documentation and compare it with the implemented code.
- Weak signals: Roadmap items, issues and README badges are not implemented integrations.
- Completion: List interfaces, integrations and extension points with citations, separating implemented from planned.
- Points to cover: interfaces, integrations, extension points, protocols and formats
- Contract rule: Cite the actual APIs, SDKs, plugins, protocols and data formats; distinguish implemented integrations from documented plans.

## T5. What is hard about operating it?
- Decision: Find the operational work customers would pay a host to remove: this is the core of an infrastructure-and-hosting business.
- Evidence needed: State and storage handling, upgrade and migration procedures, backup and restore, scaling and concurrency limits, configuration complexity, security patching, and maintainers' operational guidance or user-reported operating problems.
- Search routes:
  - Read migration, backup, scaling and upgrade code and documentation identified by the index.
  - Search public issues and discussions for operators' reported problems with running it in production.
- Weak signals: Isolated complaints, unmeasured performance claims and generic 'ops is hard' statements are not evidence of a specific burden.
- Completion: Name each concrete operating burden with evidence and state which ones a hosted offering would credibly remove.
- Points to cover: operational tasks, scaling characteristics, failure and recovery, security operations, hosting value
- Contract rule: Ground each operational burden in cited code, configuration or documentation; the hosting value must follow from burdens the evidence shows.

## T7. How healthy and mature is the project?
- Decision: Judge whether the project is a stable foundation for a business and how dependent it is on a few maintainers.
- Evidence needed: Repository metadata (commits, contributors, releases, issues), package registry downloads and dependents, documented production users, and governance or funding information.
- Search routes:
  - Start from the saved GitHub metadata source; read release history and contributor concentration.
  - Search registries and public adopter lists for dependents and production use.
- Weak signals: Stars, forks and social mentions measure attention, not adoption; a quiet repository can be mature rather than abandoned.
- Completion: Report dated activity, maintainer concentration, release cadence and adoption evidence, naming the maturity risks.
- Points to cover: activity, maintainers and contributors, release cadence, adoption signals, maturity risks
- Contract rule: Use dated, cited repository metadata and registry evidence; separate attention (stars) from use (downloads, dependents, production users).

## T8. Where could it be applied?
- Decision: Generate the candidate uses and workloads a hosted product could serve, from what the technology demonstrably does.
- Evidence needed: Implemented capabilities from the Technical findings, documented use cases, examples and demos, adopters' descriptions of their use, and limits that rule uses out.
- Search routes:
  - Map implemented capabilities and integrations to workloads; read examples and adopter accounts.
  - Search for how existing users apply the technology and where they hit its limits.
- Weak signals: Vendor use-case pages and a founder's ideas are hypotheses, not demand.
- Completion: List applications and workloads tied to cited capabilities, plus uses the evidence shows it is unsuitable for.
- Points to cover: applications, target workloads, adjacent uses, unsuitable uses
- Contract rule: Ground each proposed application in a cited capability; label proposed uses as hypotheses until customer evidence supports them.
