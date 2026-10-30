---
name: project-health
description: Answer T7 — project activity, maintainer concentration, release cadence and adoption — from dated repository metadata and registry evidence.
---

# Project health (T7)

Read the T7 contract. Condere saves GitHub repository metadata (stars, forks,
open issues, recent releases, top contributors, recent commit activity) as a
dated `repository_metadata` source before workers start; find it with
`list_sources`.

Method:

1. Read the metadata source; report activity and releases with their dates.
2. Assess maintainer concentration from contributor counts and commit share.
3. Look for adoption: package-registry downloads and dependents, documented
   production users, case studies. Cite each source and its date.
4. Read governance, sponsorship or company-backing information where it exists.

Fill `activity`, `maintainers_and_contributors`, `release_cadence`,
`adoption_signals` and `maturity_risks`. Stars and forks measure attention, not
use. A quiet repository can be mature. Always state the observation date.
