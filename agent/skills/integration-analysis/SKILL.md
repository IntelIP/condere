---
name: integration-analysis
description: Answer T4 — interfaces, integrations, extension points, protocols and formats — distinguishing implemented code from plans.
---

# Integration analysis (T4)

Integrations decide how customers connect the technology to their systems and
where a product could add value. Read the T4 contract.

Method:

1. Find route definitions, RPC/GraphQL schemas, CLI commands and SDK packages
   through the index and `graphify query`; read their implementations.
2. Find plugin, extension, webhook or adapter mechanisms and the registry that
   loads them.
3. Find integration code for third-party services and the protocols and data
   formats it supports (for example S3, Postgres wire, OpenTelemetry, OpenAPI).
4. Compare official integration documentation with what the code implements.

Fill `interfaces`, `integrations`, `extension_points` and `protocols_and_formats`,
separating implemented integrations from documented plans. Issues, roadmaps and
badges are not implementations.
