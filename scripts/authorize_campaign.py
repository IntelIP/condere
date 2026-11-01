"""Register explicit operator spending authority; does not start an assessment."""

import argparse
import os

from condere.runtime.state import AssessmentStore


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("campaign_id")
    parser.add_argument("--cap-microusd", type=int, required=True)
    parser.add_argument("--reason", required=True)
    parser.add_argument("--assessment-limit", type=int, default=1)
    args = parser.parse_args()
    if args.cap_microusd < 0 or args.assessment_limit < 1 or not args.reason.strip():
        parser.error("Use a nonnegative cap, a positive assessment limit and a reason.")
    missing = [
        key for key in ("CONDERE_OWNER_ID", "CONDERE_DATABASE_URL") if not os.environ.get(key)
    ]
    if missing:
        parser.error("Missing operator configuration: " + ", ".join(missing))
    store = AssessmentStore(
        os.environ["CONDERE_DATABASE_URL"],
        direct_dsn=os.environ.get("CONDERE_DATABASE_DIRECT_URL"),
    )
    store.setup()
    store.authorize_campaign(
        os.environ["CONDERE_OWNER_ID"],
        args.campaign_id,
        args.cap_microusd,
        {"basis": args.reason},
        assessment_limit=args.assessment_limit,
    )
    print(f"Campaign {args.campaign_id} registered. No assessment was started.")


if __name__ == "__main__":
    main()
