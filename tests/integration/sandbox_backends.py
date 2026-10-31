"""The sandbox backend native tests run against: CONDERE_TEST_SANDBOX_BACKEND=smol or modal."""

import json
import os
from pathlib import Path

import pytest

from condere.runtime.sandbox import SmolSandbox
from condere.runtime.sandbox_modal import ModalSandbox

BACKEND = os.environ.get("CONDERE_TEST_SANDBOX_BACKEND", "smol")
Sandbox = ModalSandbox if BACKEND == "modal" else SmolSandbox
smol_only = pytest.mark.skipif(BACKEND != "smol", reason="SmolVM machine lifecycle")
modal_only = pytest.mark.skipif(BACKEND != "modal", reason="Modal sandbox lifecycle")


def image() -> str:
    if BACKEND == "modal":
        manifest = Path(__file__).resolve().parents[2] / "infra/smolvm/inspection-image.json"
        return json.loads(manifest.read_text())["base_image"]
    return os.environ["CONDERE_SMOL_IMAGE"]
