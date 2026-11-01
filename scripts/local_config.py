"""Configuration preparation through environment variables or explicit Doppler use."""

import fcntl
import json
import os
import re
import shlex
import subprocess
from pathlib import Path


def arguments(parser):
    parser.add_argument("--doppler", action="store_true", help="Use your local Doppler setup.")
    parser.add_argument(
        "--output", type=Path, default=Path(".env"), help="Write generated local bindings."
    )
    return parser.parse_args()


def doppler(*arguments, payload=None):
    result = subprocess.run(
        ["doppler", *arguments],
        input=payload,
        text=True,
        capture_output=True,
    )
    if result.returncode:
        raise SystemExit("Doppler failed; check your configured project and identity.")
    return result.stdout


def load(args):
    if args.doppler:
        return json.loads(doppler("secrets", "download", "--no-file", "--format", "json"))
    return dict(os.environ)


def save(args, original, values):
    if args.doppler:
        doppler("secrets", "upload", "/dev/stdin", payload=json.dumps(values))
        return
    changed = {key: value for key, value in values.items() if value != original.get(key)}
    if not changed:
        return
    # Edit bindings in place: uv uses the first duplicate assignment in an env file.
    # Lock the file across read/write and never follow a symlink.
    fd = os.open(args.output, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "r+") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        os.fchmod(stream.fileno(), 0o600)
        lines = stream.read().splitlines(keepends=True)
        remaining = dict(changed)
        updated = []
        for line in lines:
            match = re.match(r"^(?:export\s+)?([A-Za-z_][A-Za-z_0-9]*)=(.*)$", line)
            if not match or match[1] not in changed:
                updated.append(line)
                continue
            key = match[1]
            if not original.get(key) and match[2].strip() not in {"", "''", '""'}:
                raise SystemExit("Load the existing file with uv --env-file before updating it.")
            if key in remaining:
                updated.append(f"{key}={shlex.quote(str(remaining.pop(key)))}\n")
        if remaining:
            updated.append("\n# Generated local Condere configuration\n")
            updated.extend(f"{key}={shlex.quote(str(value))}\n" for key, value in remaining.items())
        stream.seek(0)
        stream.write("".join(updated))
        stream.truncate()
