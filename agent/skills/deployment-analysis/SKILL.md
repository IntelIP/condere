---
name: deployment-analysis
description: Answer T2 — how the technology is built, packaged and deployed — from cited build and deployment artifacts.
---

# Deployment analysis (T2)

A hosted business takes over deployment, so this answer defines what customers
would stop doing. Read the T2 contract. The repository index lists every
Dockerfile, Compose file, Helm chart, Kubernetes manifest, Terraform module,
platform config and CI workflow it found.

Method:

1. Read each deployment artifact with `inspect_repository`. Record base images,
   build steps, exposed ports, volumes, required services and start commands.
2. Read installation and self-hosting documentation; compare it with the files.
   Note which methods are documented, which are only present, and which are absent.
3. Identify the configuration surface: environment variables, config files and
   secrets the deployment needs.
4. Name the stateful services (databases, queues, caches, object stores) a
   production deployment requires.

Fill `packaging`, `runtime_requirements`, `deployment_methods`, `configuration`
and `stateful_services` with file-level citations. A Dockerfile proves a container
build, not production readiness. Release artifacts and official documentation are
vendor-reported. Do not run the repository's build or setup scripts.
