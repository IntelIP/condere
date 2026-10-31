"""Daily health report: the improvement loop's daily check, run as a Cloud Run Job.

Reads the ledger and provider usage; starts no assessment and spends nothing. The report goes
to the evidence bucket at `health/<date>.json` and to the log as one JSON line, so Cloud
Logging and the coding agents read the same record. Each finding names the ceiling it crossed;
the ceilings are the owner-approved values in docs/production.md and change only by reviewed
pull request.
"""

import json
import os
from datetime import UTC, datetime, timedelta

import httpx

from condere.interfaces.server import configuration
from condere.runtime.coordinator import SMALL_CHARGE_MICROUSD
from condere.runtime.evidence import object_client
from condere.runtime.state import AssessmentStore

RUN_PAID_MICROUSD = 5_000_000
RUN_MINUTES = 30
MONTH_PAID_MICROUSD = 300_000_000
MONTH_WARNING_SHARE = 0.8
# The assessment job's task timeout is 60 minutes; an active run older than this was lost.
STRANDED_MINUTES = 70
# Costs this report cannot see yet: no usage API is wired for these.
UNMEASURED = ("exa balance", "modal compute", "cloud run", "neon", "r2", "chatgpt plan limits")


def dollars(microusd: int) -> float:
    return round(microusd / 1_000_000, 4)


def minutes(row: dict) -> float:
    return round((row["updated_at"] - row["created_at"]).total_seconds() / 60, 1)


def provider_usage(values: dict, http: httpx.Client) -> dict:
    """Account-level usage the ledger cannot see (other keys, other projects)."""
    usage = {}
    calls = {
        "openrouter": ("CONDERE_OPENROUTER_API_KEY", "https://openrouter.ai/api/v1/key"),
        "apify": ("APIFY_TOKEN", "https://api.apify.com/v2/users/me/limits"),
    }
    for name, (setting, url) in calls.items():
        if not values.get(setting):
            continue
        try:
            response = http.get(url, headers={"Authorization": f"Bearer {values[setting]}"})
            response.raise_for_status()
            data = response.json()["data"]
        except (httpx.HTTPError, KeyError, ValueError) as error:
            # The error type and status only; never the request, which carries the key.
            status = getattr(getattr(error, "response", None), "status_code", None)
            usage[name] = {"error": type(error).__name__, "status": status}
            continue
        if name == "openrouter":
            usage[name] = {
                "key_usage_usd": data["usage"],
                "key_usage_month_usd": data["usage_monthly"],
                "key_limit_remaining_usd": data["limit_remaining"],
            }
        else:
            usage[name] = {
                "month_usage_usd": data["current"]["monthlyUsageUsd"],
                "month_limit_usd": data["limits"]["maxMonthlyUsageUsd"],
                "cycle_ends": data["monthlyUsageCycle"]["endAt"],
            }
    return usage


def findings(snapshot: dict, usage: dict, now: datetime, since: datetime) -> list[dict]:
    found = []
    for row in snapshot["assessments"]:
        paid = row["settled_microusd"] + row["pending_microusd"]
        case = {"assessment": row["id"], "status": row["status"]}
        if row["status"] == "interrupted" and row["updated_at"] >= since:
            found.append({"kind": "interrupted", **case})
        if row["status"] in {"admitted", "running"} and now - row["updated_at"] > timedelta(
            minutes=STRANDED_MINUTES
        ):
            found.append(
                {
                    "kind": "stranded",
                    **case,
                    "idle_minutes": minutes({"created_at": row["updated_at"], "updated_at": now}),
                }
            )
        if paid > RUN_PAID_MICROUSD:
            found.append(
                {
                    "kind": "run_cost",
                    **case,
                    "paid_usd": dollars(paid),
                    "ceiling_usd": dollars(RUN_PAID_MICROUSD),
                }
            )
        if row["status"] == "completed" and minutes(row) > RUN_MINUTES:
            found.append(
                {
                    "kind": "run_duration",
                    **case,
                    "minutes": minutes(row),
                    "ceiling_minutes": RUN_MINUTES,
                }
            )
    for operation in snapshot["held"]:
        if operation["reserved_microusd"] > SMALL_CHARGE_MICROUSD:
            found.append(
                {
                    "kind": "held_charge",
                    "assessment": operation["assessment_id"],
                    "operation": operation["id"],
                    "provider": operation["kind"],
                    "held_usd": dollars(operation["reserved_microusd"]),
                }
            )
    month = sum(row["settled_microusd"] + row["pending_microusd"] for row in snapshot["month"])
    if month >= MONTH_PAID_MICROUSD * MONTH_WARNING_SHARE:
        found.append(
            {
                "kind": "month_cost",
                "paid_usd": dollars(month),
                "ceiling_usd": dollars(MONTH_PAID_MICROUSD),
            }
        )
    for name, values in usage.items():
        if "error" in values:
            found.append({"kind": "provider_unreadable", "provider": name, **values})
        elif name == "apify" and values["month_usage_usd"] >= (
            values["month_limit_usd"] * MONTH_WARNING_SHARE
        ):
            found.append({"kind": "provider_limit", "provider": name, **values})
    return found


def report(snapshot: dict, usage: dict, now: datetime, since: datetime) -> dict:
    rows = snapshot["assessments"]
    completed = [row for row in rows if row["status"] == "completed" and row["updated_at"] >= since]
    return {
        "generated_at": now.isoformat(),
        "window_start": since.isoformat(),
        "runs": {
            status: sum(1 for row in rows if row["status"] == status and row["updated_at"] >= since)
            for status in ("completed", "interrupted", "cancelled")
        }
        | {"active": sum(1 for row in rows if row["status"] in {"admitted", "running"})},
        "completed_runs": [
            {
                "assessment": row["id"],
                "minutes": minutes(row),
                "paid_usd": dollars(row["settled_microusd"] + row["pending_microusd"]),
                "pending_usd": dollars(row["pending_microusd"]),
                "failed_operations": row["failed_operations"],
            }
            for row in completed
        ],
        "month_by_provider": {
            row["kind"]: {
                "operations": row["operations"],
                "settled_usd": dollars(row["settled_microusd"]),
                "pending_usd": dollars(row["pending_microusd"]),
            }
            for row in snapshot["month"]
        },
        "provider_accounts": usage,
        "unmeasured": list(UNMEASURED),
        "findings": findings(snapshot, usage, now, since),
    }


def main() -> None:
    try:
        values = configuration(os.environ)
    except ValueError as error:
        raise SystemExit(str(error)) from None
    now = datetime.now(UTC)
    since = now - timedelta(days=1)
    state = AssessmentStore(values["CONDERE_DATABASE_URL"])
    snapshot = state.health(values["CONDERE_OWNER_ID"], since)
    with httpx.Client(timeout=20) as http:
        usage = provider_usage(values, http)
    result = report(snapshot, usage, now, since)
    body = json.dumps(result, sort_keys=True).encode()
    objects = object_client(values)
    try:
        objects.put_object(
            Bucket=values["CONDERE_EVIDENCE_BUCKET"],
            Key=f"health/{now:%Y-%m-%d}.json",
            Body=body,
            ContentType="application/json",
        )
    finally:
        objects.close()
    print(
        json.dumps(
            {
                "severity": "WARNING" if result["findings"] else "INFO",
                "message": "condere daily health",
                "report": result,
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
