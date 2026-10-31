"""Daily health findings and provider reads, with constructed ledger rows and no network."""

import json
from datetime import UTC, datetime, timedelta

import httpx

from condere.jobs.daily_health import findings, provider_usage, report, summary

NOW = datetime(2026, 10, 31, 13, 7, tzinfo=UTC)
SINCE = NOW - timedelta(days=1)


def run(name, status, *, started_minutes_ago, took=None, settled=0, pending=0):
    created = NOW - timedelta(minutes=started_minutes_ago)
    updated = created + timedelta(minutes=took if took is not None else 0)
    return {
        "id": name,
        "status": status,
        "created_at": created,
        "updated_at": updated,
        "settled_microusd": settled,
        "pending_microusd": pending,
        "failed_operations": 0,
    }


def snapshot(assessments=(), held=(), month=()):
    return {"assessments": list(assessments), "held": list(held), "month": list(month)}


def kinds(found):
    return sorted(item["kind"] for item in found)


def test_a_healthy_day_has_no_findings_and_counts_runs():
    rows = [run("ok", "completed", started_minutes_ago=120, took=10, settled=370_000)]
    result = report(snapshot(rows), {}, NOW, SINCE)
    assert result["findings"] == []
    assert result["runs"] == {"completed": 1, "interrupted": 0, "cancelled": 0, "active": 0}
    assert result["completed_runs"][0]["paid_usd"] == 0.37
    assert "modal compute" in result["unmeasured"]


def test_each_ceiling_produces_its_own_finding():
    rows = [
        run(
            "costly",
            "completed",
            started_minutes_ago=200,
            took=12,
            settled=4_000_000,
            pending=1_500_000,
        ),
        run("slow", "completed", started_minutes_ago=200, took=31),
        run("broke", "interrupted", started_minutes_ago=100, took=5),
        run("lost", "running", started_minutes_ago=90),
        run("young", "running", started_minutes_ago=20),
    ]
    held = [
        {"assessment_id": "broke", "id": "op1", "kind": "apify", "reserved_microusd": 300_000},
        {"assessment_id": "broke", "id": "op2", "kind": "exa", "reserved_microusd": 250_000},
    ]
    month = [
        {
            "kind": "exa",
            "operations": 9,
            "settled_microusd": 239_000_000,
            "pending_microusd": 1_000_000,
        }
    ]
    found = findings(snapshot(rows, held, month), {}, NOW, SINCE)
    assert kinds(found) == [
        "held_charge",
        "interrupted",
        "month_cost",
        "run_cost",
        "run_duration",
        "stranded",
    ]
    assert next(item for item in found if item["kind"] == "held_charge")["operation"] == "op1"
    assert next(item for item in found if item["kind"] == "stranded")["assessment"] == "lost"


def test_an_old_interruption_is_not_reported_again():
    old = run("old", "interrupted", started_minutes_ago=3000, took=5)
    assert findings(snapshot([old]), {}, NOW, SINCE) == []


def test_provider_usage_reads_accounts_and_never_reports_the_key():
    def handler(request):
        assert request.headers["Authorization"] in {"Bearer or-secret", "Bearer apify-secret"}
        if request.url.host == "openrouter.ai":
            body = {"data": {"usage": 12.5, "usage_monthly": 3.25, "limit_remaining": None}}
            return httpx.Response(200, json=body)
        return httpx.Response(401, json={"error": "token rejected"})

    values = {"CONDERE_OPENROUTER_API_KEY": "or-secret", "APIFY_TOKEN": "apify-secret"}
    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        usage = provider_usage(values, http)
    assert usage["openrouter"] == {
        "key_usage_usd": 12.5,
        "key_usage_month_usd": 3.25,
        "key_limit_remaining_usd": None,
    }
    assert usage["apify"] == {"error": "HTTPStatusError", "status": 401}
    assert "secret" not in json.dumps(usage)
    assert kinds(findings(snapshot(), usage, NOW, SINCE)) == ["provider_unreadable"]


def test_apify_near_its_monthly_limit_is_a_finding():
    usage = {"apify": {"month_usage_usd": 4.1, "month_limit_usd": 5.0, "cycle_ends": "x"}}
    assert kinds(findings(snapshot(), usage, NOW, SINCE)) == ["provider_limit"]


def test_summary_names_each_finding_for_the_alert_email():
    usage = {"apify": {"month_usage_usd": 30.16, "month_limit_usd": 30.0, "cycle_ends": "x"}}
    lost = run("lost", "running", started_minutes_ago=90)
    found = findings(snapshot([lost]), usage, NOW, SINCE)
    assert summary(found) == "2 finding(s): stranded (lost), provider_limit (apify)"
    assert summary([]) == "no findings"
