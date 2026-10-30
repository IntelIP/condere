"""Deterministic repository index, run inside an offline SmolVM; never executes repository code.

Usage: python index_repository.py REPOSITORY_PATH OUTPUT_DIRECTORY

Writes facts.json, report.md and, when Graphify succeeds, graph.json. Uses only the standard
library and the image's Graphify CLI. YAML is pattern-matched rather than parsed, and every
extractor records its own failure as a limitation instead of stopping the index.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
from collections import Counter
from pathlib import Path

EXCLUDED = {
    ".git",
    "node_modules",
    ".venv",
    "venv",
    "vendor",
    "third_party",
    "build",
    "dist",
    "target",
    "__pycache__",
    ".next",
    ".tox",
    "graphify-out",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
}
LANGUAGES = {
    ".py": "Python",
    ".js": "JavaScript",
    ".jsx": "JavaScript",
    ".mjs": "JavaScript",
    ".ts": "TypeScript",
    ".tsx": "TypeScript",
    ".go": "Go",
    ".rs": "Rust",
    ".c": "C",
    ".h": "C/C++ header",
    ".cc": "C++",
    ".cpp": "C++",
    ".hpp": "C++",
    ".java": "Java",
    ".kt": "Kotlin",
    ".rb": "Ruby",
    ".php": "PHP",
    ".cs": "C#",
    ".swift": "Swift",
    ".scala": "Scala",
    ".ex": "Elixir",
    ".exs": "Elixir",
    ".sql": "SQL",
    ".sh": "Shell",
    ".proto": "Protocol Buffers",
}
GRAPH_EXTENSIONS = {".py", ".js", ".jsx", ".ts", ".tsx", ".go", ".rs", ".c", ".h", ".cc", ".cpp"}
MAX_GRAPH_FILES, MAX_GRAPH_BYTES, MAX_TEXT_BYTES = 3_000, 48_000_000, 1_000_000
PLATFORM_FILES = {
    "fly.toml",
    "render.yaml",
    "vercel.json",
    "netlify.toml",
    "app.yaml",
    "app.json",
    "Procfile",
    "serverless.yml",
    "serverless.yaml",
    "railway.json",
    "railway.toml",
    "cloudbuild.yaml",
    "skaffold.yaml",
    "kustomization.yaml",
    "Tiltfile",
    "nixpacks.toml",
    "Dockerrun.aws.json",
    ".platform.app.yaml",
    "wrangler.toml",
}
SIGNALS = {
    "PostgreSQL": r"^(pg|postgres|psycopg2?|psycopg|asyncpg|pgx|lib/pq|sqlx|diesel|gorm)",
    "MySQL": r"^(mysql|mysql2|pymysql|mysqlclient|go-sql-driver/mysql)",
    "SQLite": r"(sqlite)",
    "ORM": r"^(sqlalchemy|prisma|@prisma/client|typeorm|sequelize|knex|drizzle-orm|django)$",
    "Redis": r"^(redis|ioredis|go-redis|redis-py|aioredis)",
    "MongoDB": r"^(mongodb|mongoose|pymongo|motor|mongo-driver)",
    "Kafka": r"(kafka)",
    "RabbitMQ/AMQP": r"^(amqp|amqplib|pika|aio-pika|lapin)",
    "NATS": r"(nats)",
    "Elasticsearch/OpenSearch": r"(elasticsearch|opensearch)",
    "AWS SDK": r"^(boto3|botocore|aws-sdk|@aws-sdk/|aws-sdk-go|aws-config)",
    "Google Cloud SDK": r"^(google-cloud-|@google-cloud/|cloud.google.com/go)",
    "Azure SDK": r"^(azure-|@azure/)",
    "Model provider": r"^(openai|anthropic|@anthropic-ai/|@openai/|cohere|mistralai|google-genai)",
    "ML runtime": r"^(torch|tensorflow|transformers|onnxruntime|jax|vllm|llama-cpp)",
    "LLM framework": r"^(langchain|@langchain/|llama-index|llama_index|dspy|haystack)",
    "Payments": r"^(stripe|@stripe/|braintree|paddle)",
    "Authentication": r"(passport|next-auth|auth0|keycloak|oauth|oidc|jwt|clerk|supabase)",
    "Observability": r"(opentelemetry|prometheus|sentry|datadog|statsd)",
    "gRPC/Protobuf": r"(grpc|protobuf|tonic|prost)",
    "GraphQL": r"(graphql|apollo)",
    "Kubernetes client": r"^(kubernetes|@kubernetes/|k8s.io/client-go|kube)",
    "Object storage": r"(minio|s3fs|object_store|aws-sdk-s3)",
    "Search/vector": r"(faiss|qdrant|weaviate|pinecone|chromadb|milvus|pgvector|lancedb)",
}
LICENSES = [
    ("AGPL-3.0", r"GNU AFFERO GENERAL PUBLIC LICENSE"),
    ("LGPL", r"GNU LESSER GENERAL PUBLIC LICENSE"),
    ("GPL-3.0", r"GNU GENERAL PUBLIC LICENSE\s+Version 3"),
    ("GPL-2.0", r"GNU GENERAL PUBLIC LICENSE\s+Version 2"),
    ("SSPL-1.0", r"Server Side Public License"),
    ("BUSL-1.1", r"Business Source License"),
    ("Elastic-2.0", r"Elastic License"),
    ("Commons Clause", r"Commons Clause"),
    ("MPL-2.0", r"Mozilla Public License"),
    ("Apache-2.0", r"Apache License,?\s+Version 2\.0"),
    ("MIT", r"Permission is hereby granted, free of charge"),
    ("BSD", r"Redistribution and use in source and binary forms"),
    ("ISC", r"ISC License|Permission to use, copy, modify, and(/or)? distribute"),
    ("Unlicense", r"This is free and unencumbered software"),
]
ENV_PATTERNS = [
    r"os\.environ(?:\.get)?\(\s*['\"]([A-Z][A-Z0-9_]{2,})",
    r"os\.environ\[\s*['\"]([A-Z][A-Z0-9_]{2,})",
    r"os\.getenv\(\s*['\"]([A-Z][A-Z0-9_]{2,})",
    r"process\.env\.([A-Z][A-Z0-9_]{2,})",
    r"process\.env\[\s*['\"]([A-Z][A-Z0-9_]{2,})",
    r"os\.(?:Getenv|LookupEnv)\(\s*\"([A-Z][A-Z0-9_]{2,})",
    r"env::var(?:_os)?\(\s*\"([A-Z][A-Z0-9_]{2,})",
    r"System\.getenv\(\s*\"([A-Z][A-Z0-9_]{2,})",
    r"ENV\[\s*['\"]([A-Z][A-Z0-9_]{2,})",
]


def read(path: Path) -> str:
    if path.stat().st_size > MAX_TEXT_BYTES:
        return ""
    return path.read_text(errors="replace")


def walk(root: Path) -> list[Path]:
    files = []
    for parent, directories, names in os.walk(root, followlinks=False):
        directories[:] = sorted(d for d in directories if d not in EXCLUDED)
        for name in sorted(names):
            path = Path(parent) / name
            if path.is_file() and not path.is_symlink():
                files.append(path)
    return files


def rel(root: Path, path: Path) -> str:
    return str(path.relative_to(root))


def manifests(root: Path, files: list[Path], facts: dict) -> list[str]:
    found, names = [], []

    def add(path, ecosystem, **fields):
        found.append({"path": rel(root, path), "ecosystem": ecosystem, **fields})
        for key in ("dependencies", "dev_dependencies"):
            names.extend(fields.get(key) or [])

    for path in files:
        name = path.name
        try:
            if name == "package.json":
                data = json.loads(read(path) or "{}")
                add(
                    path,
                    "npm",
                    name=data.get("name"),
                    version=data.get("version"),
                    license=data.get("license"),
                    main=data.get("main"),
                    bin=data.get("bin"),
                    scripts=sorted(data.get("scripts") or {})[:30],
                    dependencies=sorted(data.get("dependencies") or {}),
                    dev_dependencies=sorted(data.get("devDependencies") or {})[:60],
                )
            elif name == "pyproject.toml":
                data = tomllib.loads(read(path))
                project, poetry = data.get("project", {}), data.get("tool", {}).get("poetry", {})
                dependencies = [
                    re.split(r"[ <>=!~;\[]", item, maxsplit=1)[0]
                    for item in project.get("dependencies", [])
                ] + [key for key in poetry.get("dependencies", {}) if key != "python"]
                license_value = project.get("license") or poetry.get("license")
                add(
                    path,
                    "python",
                    name=project.get("name") or poetry.get("name"),
                    version=project.get("version") or poetry.get("version"),
                    license=license_value
                    if isinstance(license_value, str)
                    else (license_value or {}).get("text"),
                    scripts=sorted(project.get("scripts", {}) | poetry.get("scripts", {})),
                    dependencies=dependencies,
                    optional_groups=sorted(project.get("optional-dependencies", {})),
                )
            elif re.fullmatch(r"requirements[\w.-]*\.txt", name):
                lines = [
                    re.split(r"[ <>=!~;\[]", line.strip(), maxsplit=1)[0]
                    for line in read(path).splitlines()
                    if line.strip() and not line.strip().startswith(("#", "-"))
                ]
                add(path, "python", dependencies=lines[:200])
            elif name == "go.mod":
                text = read(path)
                module = re.search(r"^module\s+(\S+)", text, re.M)
                requires = re.findall(r"^\s*([\w.\-/]+\.[\w.\-/]+)\s+v[\w.\-+]+", text, re.M)
                add(path, "go", name=module and module.group(1), dependencies=requires)
            elif name == "Cargo.toml":
                data = tomllib.loads(read(path))
                package = data.get("package", {})
                add(
                    path,
                    "cargo",
                    name=package.get("name"),
                    version=package.get("version"),
                    license=package.get("license"),
                    dependencies=sorted(data.get("dependencies", {})),
                    workspace_members=data.get("workspace", {}).get("members"),
                )
            elif name == "Gemfile":
                add(
                    path,
                    "ruby",
                    dependencies=re.findall(r"^\s*gem\s+['\"]([^'\"]+)", read(path), re.M),
                )
            elif name == "composer.json":
                data = json.loads(read(path) or "{}")
                add(
                    path,
                    "php",
                    name=data.get("name"),
                    license=data.get("license"),
                    dependencies=sorted(data.get("require") or {}),
                )
            elif name == "pom.xml":
                add(
                    path,
                    "maven",
                    dependencies=re.findall(r"<artifactId>([^<]+)</artifactId>", read(path))[:150],
                )
            elif name in {"build.gradle", "build.gradle.kts"}:
                deps = re.findall(
                    r"(?:implementation|api|runtimeOnly)\s*\(?\s*['\"]([^'\"]+)", read(path)
                )
                add(path, "gradle", dependencies=deps[:150])
            elif path.suffix == ".csproj":
                add(
                    path,
                    "nuget",
                    dependencies=re.findall(r'PackageReference Include="([^"]+)"', read(path)),
                )
        except Exception as error:  # noqa: BLE001 - one malformed manifest must not stop the index
            facts["limitations"].append(
                f"Could not parse {rel(root, path)}: {type(error).__name__}"
            )
    facts["manifests"] = found[:80]
    return names


def deployment(root: Path, files: list[Path], facts: dict) -> None:
    docker, compose, kubernetes, helm, terraform, platform = [], [], Counter(), [], Counter(), []
    for path in files:
        name, relative = path.name, rel(root, path)
        try:
            if (
                name in {"Dockerfile", "Containerfile"}
                or name.endswith(".Dockerfile")
                or name.startswith("Dockerfile.")
            ):
                text = read(path)
                docker.append(
                    {
                        "path": relative,
                        "base_images": re.findall(r"^\s*FROM\s+(\S+)", text, re.M | re.I),
                        "exposed_ports": re.findall(r"^\s*EXPOSE\s+(.+)$", text, re.M | re.I),
                        "entrypoint": re.findall(
                            r"^\s*(?:ENTRYPOINT|CMD)\s+(.+)$", text, re.M | re.I
                        )[-2:],
                        "volumes": re.findall(r"^\s*VOLUME\s+(.+)$", text, re.M | re.I),
                        "env": re.findall(r"^\s*ENV\s+([A-Za-z_][A-Za-z0-9_]*)", text, re.M | re.I),
                    }
                )
            elif re.fullmatch(r"(docker-)?compose[\w.-]*\.ya?ml", name):
                text = read(path)
                block = re.search(r"^services:\s*\n((?:[ \t]+.*\n|\s*\n)+)", text, re.M)
                services = (
                    re.findall(r"^  ([A-Za-z0-9_.-]+):\s*$", block.group(1), re.M) if block else []
                )
                compose.append(
                    {
                        "path": relative,
                        "services": services,
                        "images": re.findall(r"^\s+image:\s*['\"]?([^\s'\"]+)", text, re.M),
                    }
                )
            elif name == "Chart.yaml":
                text = read(path)
                helm.append(
                    {
                        "path": relative,
                        "name": (re.search(r"^name:\s*(\S+)", text, re.M) or [None, None])[1],
                        "app_version": (
                            re.search(r"^appVersion:\s*['\"]?([^\s'\"]+)", text, re.M)
                            or [None, None]
                        )[1],
                    }
                )
            elif path.suffix in {".yaml", ".yml"}:
                text = read(path)
                if re.search(r"^apiVersion:", text, re.M):
                    for kind in re.findall(r"^kind:\s*(\w+)", text, re.M):
                        kubernetes[kind] += 1
            elif path.suffix == ".tf":
                text = read(path)
                for provider in re.findall(r'^\s*provider\s+"([^"]+)"', text, re.M):
                    terraform["provider:" + provider] += 1
                for resource in re.findall(r'^\s*resource\s+"([^"]+)"', text, re.M):
                    terraform["resource:" + resource] += 1
            if name in PLATFORM_FILES:
                platform.append(relative)
        except Exception as error:  # noqa: BLE001
            facts["limitations"].append(f"Could not read {relative}: {type(error).__name__}")
    facts["deployment"] = {
        "dockerfiles": docker[:30],
        "compose": compose[:20],
        "kubernetes_kinds": dict(kubernetes.most_common(30)),
        "helm_charts": helm[:20],
        "terraform": dict(terraform.most_common(40)),
        "platform_configs": platform[:30],
    }


def licenses(root: Path, files: list[Path], facts: dict) -> None:
    found = []
    for path in files:
        if re.match(r"(LICEN[CS]E|COPYING|NOTICE)", path.name, re.I):
            text = read(path)[:20_000]
            identified = [label for label, pattern in LICENSES if re.search(pattern, text, re.I)]
            found.append({"path": rel(root, path), "identified": identified or ["unrecognized"]})
    spdx = Counter()
    for path in files:
        if path.suffix in LANGUAGES and path.stat().st_size < 200_000:
            match = re.search(r"SPDX-License-Identifier:\s*([\w.+-]+)", read(path)[:2_000])
            if match:
                spdx[match.group(1)] += 1
    facts["licenses"] = {"files": found[:30], "spdx_headers": dict(spdx.most_common(10))}


def environment(root: Path, files: list[Path], facts: dict) -> None:
    names = Counter()
    for path in files:
        if path.suffix in LANGUAGES or path.name.startswith(("Dockerfile", ".env.example")):
            text = read(path)
            for pattern in ENV_PATTERNS:
                names.update(re.findall(pattern, text))
            if path.name.startswith(".env.example"):
                names.update(re.findall(r"^([A-Z][A-Z0-9_]{2,})=", text, re.M))
    facts["environment_variables"] = [name for name, _ in names.most_common(150)]


def entry_points(root: Path, files: list[Path], facts: dict) -> None:
    points = []
    for path in files:
        relative = rel(root, path)
        try:
            if path.name in {
                "__main__.py",
                "main.go",
                "main.rs",
                "manage.py",
                "wsgi.py",
                "asgi.py",
            }:
                points.append(relative)
            elif (
                path.suffix == ".py"
                and "__main__" in read(path)
                and re.search(r"if\s+__name__\s*==\s*['\"]__main__['\"]", read(path))
            ):
                points.append(relative)
            elif path.name == "Procfile":
                points.extend(
                    f"Procfile: {line}" for line in read(path).splitlines() if line.strip()
                )
        except Exception:  # noqa: BLE001
            continue
    facts["entry_points"] = points[:60]


def ci(root: Path, files: list[Path], facts: dict) -> None:
    workflows = []
    for path in files:
        relative = rel(root, path)
        if relative.startswith(".github/workflows/") and path.suffix in {".yml", ".yaml"}:
            text = read(path)
            workflows.append(
                {
                    "path": relative,
                    "name": (re.search(r"^name:\s*(.+)$", text, re.M) or [None, None])[1],
                    "jobs": re.findall(
                        r"^  ([A-Za-z0-9_-]+):\s*$", text.split("jobs:", 1)[-1], re.M
                    )[:20],
                }
            )
        elif relative in {
            ".gitlab-ci.yml",
            ".circleci/config.yml",
            "Jenkinsfile",
            ".buildkite/pipeline.yml",
        }:
            workflows.append({"path": relative})
    facts["ci"] = workflows[:30]


def graph(root: Path, files: list[Path], output: Path, facts: dict) -> None:
    selected, skipped, total = [], 0, 0
    tests = re.compile(r"(^|/)(tests?|__tests__|spec|examples?|benchmarks?|fixtures)(/|$)")
    ordered = sorted(
        (path for path in files if path.suffix in GRAPH_EXTENSIONS),
        key=lambda path: (bool(tests.search(rel(root, path))), rel(root, path)),
    )
    for path in ordered:
        size = path.stat().st_size
        if (
            size > MAX_TEXT_BYTES
            or len(selected) >= MAX_GRAPH_FILES
            or total + size > MAX_GRAPH_BYTES
        ):
            skipped += 1
            continue
        selected.append(path)
        total += size
    facts["graph"] = {"files": len(selected), "skipped_files": skipped, "built": False}
    if skipped:
        facts["limitations"].append(
            f"Graph covers {len(selected)} source files; {skipped} were skipped by size bounds "
            "(tests and examples are skipped first)."
        )
    if not selected:
        facts["limitations"].append("No Graphify-supported source files; use exact text search.")
        return
    corpus, build = output / "corpus", output / "build"
    for path in selected:
        destination = corpus / rel(root, path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, destination)
    environment = {"PATH": os.environ["PATH"], "HOME": str(output), "PYTHONUTF8": "1"}
    try:
        result = subprocess.run(
            [
                "graphify",
                "extract",
                str(corpus),
                "--code-only",
                "--no-cluster",
                "--out",
                str(build),
            ],
            cwd=output,
            env=environment,
            capture_output=True,
            text=True,
            timeout=200,
        )
        built = build / "graphify-out/graph.json"
        if result.returncode or not built.exists():
            facts["limitations"].append(
                "Graphify extraction failed: " + (result.stderr + result.stdout)[-600:]
            )
            return
        shutil.copyfile(built, output / "graph.json")
        hubs = subprocess.run(
            [
                "graphify",
                "god-nodes",
                "--top",
                "25",
                "--json",
                "--graph",
                str(output / "graph.json"),
            ],
            cwd=output,
            env=environment,
            capture_output=True,
            text=True,
            timeout=60,
        )
        facts["graph"] |= {"built": True}
        try:
            facts["graph"]["hubs"] = json.loads(hubs.stdout)
        except json.JSONDecodeError:
            facts["graph"]["hubs_text"] = hubs.stdout[:4_000]
        data = json.loads((output / "graph.json").read_text())
        facts["graph"] |= {
            "nodes": len(data.get("nodes", [])),
            "edges": len(data.get("links", data.get("edges", []))),
        }
    except subprocess.TimeoutExpired:
        facts["limitations"].append(
            "Graphify extraction exceeded its time bound; no graph was saved."
        )
    finally:
        shutil.rmtree(corpus, ignore_errors=True)
        shutil.rmtree(build, ignore_errors=True)


def report(facts: dict) -> str:
    lines = [
        f"# Repository index: {facts['repository']}",
        "",
        f"Commit `{facts['commit']}`. Deterministic, offline index: pattern matching and static "
        "parsing only. Verify every claim in the cited source file before relying on it.",
        "",
        "## Languages",
        "",
    ]
    lines += [f"- {name}: {count} files" for name, count in facts["languages"].items()] or [
        "- none recognized"
    ]
    lines += ["", "## Top-level layout", ""]
    lines += [f"- `{name}`: {count} files" for name, count in facts["layout"].items()]
    lines += ["", "## Documentation", ""] + [f"- `{path}`" for path in facts["documentation"]]
    lines += ["", "## Entry points", ""] + [f"- `{path}`" for path in facts["entry_points"]]
    lines += ["", "## Dependency manifests", ""]
    for item in facts["manifests"]:
        header = ", ".join(
            f"{key} {item[key]}" for key in ("name", "version", "license") if item.get(key)
        )
        lines.append(f"- `{item['path']}` ({item['ecosystem']}{'; ' + header if header else ''})")
        if item.get("dependencies"):
            lines.append("  - dependencies: " + ", ".join(item["dependencies"][:80]))
        if item.get("scripts"):
            lines.append("  - scripts: " + ", ".join(map(str, item["scripts"][:30])))
    lines += ["", "## Dependency signals (unverified name matches)", ""]
    lines += [
        f"- {signal}: {', '.join(names)}" for signal, names in facts["dependency_signals"].items()
    ]
    deploy = facts["deployment"]
    lines += ["", "## Deployment artifacts", ""]
    for item in deploy["dockerfiles"]:
        lines.append(
            f"- Dockerfile `{item['path']}`: base {', '.join(item['base_images']) or 'unknown'}; "
            f"ports {', '.join(item['exposed_ports']) or 'none declared'}; "
            f"start {' / '.join(item['entrypoint']) or 'not declared'}; "
            f"volumes {', '.join(item['volumes']) or 'none'}"
        )
    for item in deploy["compose"]:
        lines.append(
            f"- Compose `{item['path']}`: services {', '.join(item['services']) or 'unparsed'}; "
            f"images {', '.join(item['images']) or 'built locally'}"
        )
    for item in deploy["helm_charts"]:
        lines.append(f"- Helm chart `{item['path']}`: {item['name']} (app {item['app_version']})")
    if deploy["kubernetes_kinds"]:
        lines.append(
            "- Kubernetes kinds: "
            + ", ".join(f"{k} ×{v}" for k, v in deploy["kubernetes_kinds"].items())
        )
    if deploy["terraform"]:
        lines.append(
            "- Terraform: " + ", ".join(f"{k} ×{v}" for k, v in deploy["terraform"].items())
        )
    if deploy["platform_configs"]:
        lines.append(
            "- Platform configs: " + ", ".join(f"`{p}`" for p in deploy["platform_configs"])
        )
    lines += ["", "## CI", ""] + [
        f"- `{item['path']}`"
        + (f": {item.get('name')}" if item.get("name") else "")
        + (f" (jobs: {', '.join(item['jobs'])})" if item.get("jobs") else "")
        for item in facts["ci"]
    ]
    lines += ["", "## Licenses", ""] + [
        f"- `{item['path']}`: {', '.join(item['identified'])}"
        for item in facts["licenses"]["files"]
    ]
    if facts["licenses"]["spdx_headers"]:
        lines.append(
            "- SPDX headers: "
            + ", ".join(f"{k} ×{v}" for k, v in facts["licenses"]["spdx_headers"].items())
        )
    lines += [
        "",
        "## Referenced environment variables",
        "",
        ", ".join(facts["environment_variables"]) or "none found",
    ]
    graph_facts = facts["graph"]
    lines += [
        "",
        "## Code graph",
        "",
        f"Built: {graph_facts['built']}; files {graph_facts['files']}; "
        f"skipped {graph_facts['skipped_files']}; nodes {graph_facts.get('nodes')}; "
        f"edges {graph_facts.get('edges')}.",
    ]
    for hub in (
        graph_facts.get("hubs", [])[:25] if isinstance(graph_facts.get("hubs"), list) else []
    ):
        lines.append(f"- {json.dumps(hub, sort_keys=True)[:300]}")
    lines += ["", "## Limitations", ""] + [f"- {item}" for item in facts["limitations"]]
    return "\n".join(lines) + "\n"


def main() -> None:
    root, output = Path(sys.argv[1]).resolve(strict=True), Path(sys.argv[2])
    output.mkdir(parents=True, exist_ok=True)
    files = walk(root)
    facts = {
        "repository": sys.argv[3] if len(sys.argv) > 3 else str(root),
        "commit": sys.argv[4] if len(sys.argv) > 4 else None,
        "file_count": len(files),
        "limitations": [],
    }
    languages = Counter(LANGUAGES[path.suffix] for path in files if path.suffix in LANGUAGES)
    facts["languages"] = dict(languages.most_common())
    layout = Counter(
        rel(root, path).split("/", 1)[0] if "/" in rel(root, path) else "." for path in files
    )
    facts["layout"] = dict(sorted(layout.items(), key=lambda item: -item[1])[:40])
    facts["documentation"] = [
        rel(root, path)
        for path in files
        if re.match(
            r"(README|CHANGELOG|CONTRIBUTING|SECURITY|GOVERNANCE|ARCHITECTURE)", path.name, re.I
        )
        and rel(root, path).count("/") <= 2
    ][:30]
    names = manifests(root, files, facts)
    facts["dependency_signals"] = {
        signal: sorted({name for name in names if re.search(pattern, name.lower())})[:15]
        for signal, pattern in SIGNALS.items()
        if any(re.search(pattern, name.lower()) for name in names)
    }
    facts |= {
        "deployment": {
            "dockerfiles": [],
            "compose": [],
            "kubernetes_kinds": {},
            "helm_charts": [],
            "terraform": {},
            "platform_configs": [],
        },
        "licenses": {"files": [], "spdx_headers": {}},
        "environment_variables": [],
        "entry_points": [],
        "ci": [],
    }
    for extractor in (deployment, licenses, environment, entry_points, ci):
        try:
            extractor(root, files, facts)
        except Exception as error:  # noqa: BLE001
            facts["limitations"].append(f"{extractor.__name__} failed: {type(error).__name__}")
    graph(root, files, output, facts)
    (output / "facts.json").write_text(json.dumps(facts, indent=2, sort_keys=True, default=str))
    (output / "report.md").write_text(report(facts))
    print(json.dumps({"graph": facts["graph"]["built"], "files": len(files)}))


if __name__ == "__main__":
    main()
