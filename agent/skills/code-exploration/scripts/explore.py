"""Bounded, offline Graphify navigation; never execute repository code."""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
from importlib.metadata import version
from pathlib import Path

EXTENSIONS = {
    ".py",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".go",
    ".rs",
    ".c",
    ".h",
    ".cpp",
    ".cc",
}
EXCLUDED = {".git", "node_modules", ".venv", "venv", "vendor", "build", "dist", "graphify-out"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repository", type=Path)
    parser.add_argument("--scope", default=".")
    parser.add_argument("--question", required=True)
    parser.add_argument("--output", type=Path, default=Path("/workspace/code-index"))
    args = parser.parse_args()
    root = args.repository.resolve(strict=True)
    scope = (root / args.scope).resolve(strict=True)
    if not scope.is_relative_to(root) or not scope.is_dir():
        raise ValueError("Choose a directory within the staged repository.")
    files, skipped, total = {}, [], 0
    for parent, directories, names in os.walk(scope, followlinks=False):
        directories[:] = sorted(
            d for d in directories if d not in EXCLUDED and not (Path(parent) / d).is_symlink()
        )
        for name in sorted(names):
            path = Path(parent) / name
            relative = str(path.relative_to(root))
            if path.is_symlink() or not path.is_file():
                skipped.append(relative)
                continue
            if path.suffix not in EXTENSIONS:
                continue
            size = path.stat().st_size
            if size > 1_000_000:
                skipped.append(relative)
                continue
            total += size
            if len(files) >= 400 or total > 16_000_000:
                raise ValueError("Scope exceeds 400 files or 16 MB; select a narrower directory.")
            files[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    if not files:
        raise ValueError("No supported source files in this scope; use exact text retrieval.")
    identity = {"files": files, "graphify_version": version("graphifyy")}
    digest = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    output = args.output.resolve() / digest
    output.mkdir(parents=True, exist_ok=True)
    graph = output / "graphify-out/graph.json"
    manifest = output / "manifest.json"
    expected = identity | {"repository": str(root), "scope": args.scope, "skipped": skipped}
    reused = graph.exists() and manifest.exists() and json.loads(manifest.read_text()) == expected
    environment = {"PATH": os.environ["PATH"], "HOME": str(output), "PYTHONUTF8": "1"}
    if not reused:
        corpus = output / "source"
        for relative in files:
            destination = corpus / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(root / relative, destination)
        result = subprocess.run(
            [
                "graphify",
                "extract",
                str(corpus),
                "--code-only",
                "--no-cluster",
                "--out",
                str(output),
            ],
            cwd=output,
            env=environment,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode or not graph.exists():
            raise RuntimeError(
                "Graphify extraction failed: " + (result.stderr + result.stdout)[-1500:]
            )
        manifest.write_text(json.dumps(expected, sort_keys=True, indent=2) + "\n")
    result = subprocess.run(
        ["graphify", "query", args.question, "--budget", "600", "--graph", str(graph)],
        cwd=output,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode:
        raise RuntimeError("Graph query failed: " + result.stderr[-1000:])
    print(
        json.dumps(
            {
                "graph": str(graph),
                "manifest": str(manifest),
                "reused": reused,
                "files": len(files),
                "skipped": skipped,
                "navigation": result.stdout[:6000],
                "limitation": "Static navigation only; verify claims in original source.",
            }
        )
    )


if __name__ == "__main__":
    main()
