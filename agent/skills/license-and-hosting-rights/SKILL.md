---
name: license-and-hosting-rights
description: Answer T6 — whether licensing, contributor terms and trademarks permit a hosted or open-core product — as cited interpretation, not legal advice.
---

# License and hosting rights (T6)

Read the T6 contract. The repository index lists license files and per-package
license fields.

Method:

1. Read LICENSE, NOTICE and per-package license fields with `inspect_repository`;
   quote the governing terms.
2. Check for network-use or field-of-use clauses (AGPL §13, SSPL, BSL/BUSL change
   dates, Elastic, Commons Clause) and for differently licensed directories
   (for example an `ee/` or `enterprise/` folder).
3. Read contributor license agreements, the trademark policy and any commercial-use
   policy from official sources.
4. Note bundled components whose licenses differ from the top-level license.

Fill `license`, `hosting_rights`, `contributor_terms`, `trademark_and_branding`
and `commercial_constraints`. Present implications as interpretation and list
the questions that need legal review. A permissive license does not grant
trademark use; no license file means no granted rights, not public domain.
