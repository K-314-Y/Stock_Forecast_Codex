from __future__ import annotations
import json
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "state" / "runtime"

def load(name: str):
    return json.loads((RUNTIME / name).read_text(encoding="utf-8"))

def save(name: str, obj):
    (RUNTIME / name).write_text(
        json.dumps(obj, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )

def refresh():
    current = load("current_state.json")
    recent = load("recent_sessions.json")
    check = load("run_checklist.json")

    # Rolling 31 calendar days. Audit/source files are never deleted.
    as_of = date.fromisoformat(recent["as_of"])
    cutoff = as_of - timedelta(days=30)
    sessions = recent.get("sessions", {})
    old = {d: s for d, s in sessions.items() if date.fromisoformat(d) < cutoff}
    keep = {d: s for d, s in sessions.items() if date.fromisoformat(d) >= cutoff}
    if old:
        archive_dir = RUNTIME / "archive"
        archive_dir.mkdir(parents=True, exist_ok=True)
        for d, status in old.items():
            month = d[:7]
            p = archive_dir / f"{month}.json"
            data = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
            data[d] = status
            p.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

    recent["sessions"] = keep
    recent["missing"] = [d for d, s in keep.items() if s == "MISSING"]
    save("recent_sessions.json", recent)

    official = [d for d, s in keep.items() if s == "OFFICIAL_VALIDATED"]
    fresh = [d for d, s in keep.items() if s == "FRESH_CLOSE_VALIDATED"]
    snapshot = load("runtime_snapshot.json")
    snapshot.update({
        "v": "compact-v1",
        "as_of": recent["as_of"],
        "mode": current["mode"],
        "scope": current["scope"],
        "core6": current["core6"],
        "deadline": current["deadline_jst"],
        "gates": current["gates"],
        "features": current["features"],
        "disabled": current["disabled_features"],
        "recent": {"official": official, "fresh": fresh, "missing": recent["missing"]},
        "latest_attempt": current["latest_live_attempt"],
        "next": check["next_phase"],
        "forecast_allowed": check["forecast_allowed_now"],
        "catchup": check["catchup"],
        "core6_freshness_acquisition_fallback": check.get("core6_freshness_acquisition_fallback", {}),
        "late_research": current.get("late_research"),
        "research_diagnostic_allowed": check.get("research_diagnostic_allowed_now", False),
        "research_diagnostic_path": check.get("research_diagnostic_path"),
        "terms": current["terms"],
        "engine_alignment": current.get("engine_alignment"),
    })
    from engine.execution_policy import execution_gates
    scope = current.get("execution_scope")
    target = current.get("target_session")
    gates = execution_gates(scope, target)
    snapshot.update(gates)
    snapshot["execution_scope"] = scope
    snapshot["gates"].update({key: gates[key] for key in (
        "forecast_execution_enabled", "forward_trial_enabled", "formal_forecast_enabled",
        "schedules_enabled", "trial_enabled")})
    snapshot["forecast_generated"] = current.get("forecast_generated", False)
    snapshot["forecast_count"] = current.get("forecast_count", 0)
    snapshot["scoring_run"] = current.get("scoring_run", False)
    snapshot["fresh_strategy"] = "EPHEMERAL_RETRIEVAL"
    snapshot["raw_provider_persistence"] = "DISABLED"
    snapshot["derived_feature_persistence"] = "ENABLED"
    snapshot["execution_review"] = current.get("execution_review")
    save("runtime_snapshot.json", snapshot)
    print(RUNTIME / "runtime_snapshot.json")


if __name__ == "__main__":
    refresh()
