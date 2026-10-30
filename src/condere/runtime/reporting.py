"""Deterministic presentation from accepted records; plotting runs in an offline VM."""

import hashlib
import json
from pathlib import Path

from markdown_it import MarkdownIt

from condere.runtime.dossier import Dossier, ReportAsset, ReportFigure, render_json, render_markdown
from condere.runtime.sandbox import SmolSandbox

RENDERER = Path(__file__).resolve().parents[3] / "agent/skills/visual-reporting/scripts/render.py"


def render_figures(dossier: Dossier, sandbox: SmolSandbox) -> dict[str, bytes]:
    transfers = sandbox.upload_files(
        [
            ("/workspace/render_report.py", RENDERER.read_bytes()),
            ("/workspace/assessment.json", render_json(dossier).encode()),
        ]
    )
    if any(item.error for item in transfers):
        raise RuntimeError("The report inputs could not be staged in the offline sandbox.")
    result = sandbox.execute(
        "python /workspace/render_report.py /workspace/assessment.json /workspace/figures"
    )
    if result.exit_code:
        raise RuntimeError("The sandbox could not render the accepted assessment figures.")
    manifest = json.loads(result.output)
    assets = [ReportAsset.model_validate(item) for item in manifest["assets"]]
    figures = [ReportFigure.model_validate(item) for item in manifest["figures"]]
    if not 1 <= len(figures) <= 10 or len(assets) != 4 * len(figures):
        raise ValueError("The report exceeded its bounded figure contract.")
    paths = {asset.path for asset in assets}
    if len(paths) != len(assets):
        raise ValueError("Report artifact paths must be unique.")
    sources = {source.id for source in dossier.sources}
    for figure in figures:
        expected = {
            kind: f"figures/{figure.id}.{ext}"
            for kind, ext in (("png", "png"), ("svg", "svg"), ("data", "json"))
        }
        if figure.paths != expected or not set(expected.values()).issubset(paths):
            raise ValueError("Figure references must match their saved artifacts.")
        if not set(figure.source_ids).issubset(sources):
            raise ValueError("Figure sources must belong to this assessment.")
    downloaded = sandbox.download_files(["/workspace/" + item.path for item in assets])
    files = {}
    for asset, item in zip(assets, downloaded, strict=True):
        if (
            item.error
            or item.content is None
            or (
                len(item.content) != asset.bytes
                or hashlib.sha256(item.content).hexdigest() != asset.sha256
            )
        ):
            raise ValueError("Rendered artifact bytes do not match their manifest.")
        files[asset.path] = item.content
    dossier.figures, dossier.assets = figures, assets
    return files


def render_html(dossier: Dossier) -> str:
    # Raw HTML from briefs/sources is text, never executable markup. No scripts or remote assets.
    body = MarkdownIt("commonmark", {"html": False}).render(render_markdown(dossier))
    return (
        """<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy"
content="default-src 'none'; img-src 'self' data:; style-src 'unsafe-inline'">
<title>Condere opportunity assessment</title><style>
body{margin:0;background:#f4f6f8;color:#182632;font:17px/1.6 system-ui,sans-serif}
article{max-width:1000px;margin:auto;padding:44px;background:white}
h1,h2,h3{line-height:1.25}h2{margin-top:2.5em;border-top:1px solid #d8e0e5;padding-top:1em}
img{max-width:100%;height:auto}a{color:#1d588a}pre{overflow:auto}code{overflow-wrap:anywhere}
@media print{body,article{background:white}article{padding:0}img{break-inside:avoid}}
</style><article>"""
        + body
        + "</article></html>\n"
    )
