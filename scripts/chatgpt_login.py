"""Sign Condere in with ChatGPT so model calls use the operator's ChatGPT plan.

Usage: uv run python scripts/chatgpt_login.py [login|status|logout]

Credentials stay in ~/.local/share/condere-mda/chatgpt with owner-only permissions.
They are never written to Doppler, the repository, logs, or traces.
"""

import argparse
import functools
import json
import sys

from condere.runtime.chatgpt_plan import USAGE_URL, PlanCredentials, SignInRequired, sign_in

print = functools.partial(print, flush=True)  # noqa: A001 - show the sign-in link immediately


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", nargs="?", default="login", choices=("login", "status", "logout"))
    args = parser.parse_args()
    credentials = PlanCredentials()
    if args.action == "status":
        print(json.dumps(credentials.status(), indent=2))
        return
    if args.action == "logout":
        revoked = credentials.sign_out()
        print(
            "Signed out of ChatGPT for Condere."
            if revoked
            else "Signed out locally; remote revocation was not confirmed. "
            "You can disconnect Condere in ChatGPT settings."
        )
        return
    try:
        record = sign_in(credentials, announce=print)
    except SignInRequired as error:
        sys.exit(str(error))
    if not record.get("welcomed"):
        print(
            "\nYou're using your ChatGPT plan.\n"
            "Eligible Condere model requests now use your ChatGPT plan. "
            f"Manage usage and this app's limit at {USAGE_URL}"
        )
        credentials.save(record | {"welcomed": True})
    print(f"Signed in as {record.get('email') or 'your ChatGPT account'}. Using ChatGPT plan.")


if __name__ == "__main__":
    main()
