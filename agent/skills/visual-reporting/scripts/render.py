"""Render accepted assessment data offline with Matplotlib; no model or network."""

import argparse
import csv
import hashlib
import json
import textwrap
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402


def render(dossier, directory):
    directory.mkdir(parents=True, exist_ok=True)
    figures, assets = [], []
    simulation = dossier["execution_mode"] == "simulation"
    plt.rcParams.update({"font.size": 10, "svg.fonttype": "none", "svg.hashsalt": "condere"})

    def save(identity, title, caption, rows, draw, source_ids=()):
        fig, axis = plt.subplots(figsize=(10, 6))
        draw(axis)
        fig.suptitle(title + (" — SIMULATION" if simulation else ""), fontsize=15)
        fig.text(0.02, 0.01, textwrap.fill(caption, 125), fontsize=8, va="bottom")
        fig.tight_layout(rect=(0, 0.15, 1, 0.94))
        paths = {}
        for extension in ("png", "svg"):
            path = directory / f"{identity}.{extension}"
            fig.savefig(
                path,
                dpi=150,
                bbox_inches="tight",
                metadata={"Date": None} if extension == "svg" else {},
            )
            paths[extension] = "figures/" + path.name
        plt.close(fig)
        data_path = directory / f"{identity}.json"
        data_path.write_text(json.dumps(rows, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
        paths["data"] = "figures/" + data_path.name
        csv_path = directory / f"{identity}.csv"
        with csv_path.open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
            writer.writeheader()
            for row in rows:
                writer.writerow(
                    {
                        key: json.dumps(value) if isinstance(value, (dict, list)) else value
                        for key, value in row.items()
                    }
                )
        figures.append(
            {
                "id": identity,
                "title": title,
                "caption": caption,
                "source_ids": sorted(set(source_ids)),
                "paths": paths,
            }
        )
        for path in (directory / f"{identity}.{ext}" for ext in ("png", "svg", "json", "csv")):
            assets.append(
                {
                    "path": "figures/" + path.name,
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    "bytes": path.stat().st_size,
                    "media_type": {
                        ".png": "image/png",
                        ".svg": "image/svg+xml",
                        ".json": "application/json",
                        ".csv": "text/csv",
                    }[path.suffix],
                }
            )

    review_rows = [
        {
            "target": item["target"],
            "status": item["status"],
            "record_hash": item["reviewed_hash"],
            "qualifications": item["qualifications"],
        }
        for item in dossier["review"]["items"]
    ]
    counts = {
        status: sum(row["status"] == status for row in review_rows)
        for status in ("supported", "qualified", "insufficient")
    }

    def coverage(axis):
        axis.barh(list(counts), list(counts.values()), color=["#196b55", "#b77c19", "#9b4141"])
        axis.set_xlabel("Reviewed sections (Technical plus 17 questions)")
        axis.set_xlim(0, max(counts.values()) + 1)
        for i, value in enumerate(counts.values()):
            axis.text(value + 0.05, i, str(value), va="center")

    save(
        "evidence-coverage",
        "Where the assessment needs further evidence",
        "Review status counts describe evidence coverage, "
        "not business quality or probability of success.",
        review_rows,
        coverage,
    )

    tasks = dossier["review"]["handoff"]["next_validation_tasks"][:12]
    if tasks:
        rows = [{"order": i + 1, "proposed_task": task} for i, task in enumerate(tasks)]

        def plan(axis):
            axis.axis("off")
            for index, row in enumerate(rows):
                axis.text(
                    0.02,
                    1 - index / len(rows),
                    textwrap.fill(f"{row['order']}. {row['proposed_task']}", 100),
                    transform=axis.transAxes,
                    va="top",
                    fontsize=max(7, 11 - len(rows) / 4),
                )

        save(
            "validation-plan",
            "Proposed next validation work",
            "Proposed order from the handoff; no dates, duration estimates, or execution approval "
            "are implied. Full tasks remain in the report.",
            rows,
            plan,
        )

    return {"figures": figures, "assets": assets}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("assessment", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    print(json.dumps(render(json.loads(args.assessment.read_text()), args.output)))
