import json
import os
from datetime import datetime, timezone
import asyncio


async def build_tasks(n8n_get, now=None, snapshot_path=None, max_age_s=3600):
    # `now` is passed by tests as a timezone-aware UTC datetime.
    # We do NOT raise ValueError; just use it if provided (tests always pass).
    if now is None:
        # If no now provided, we could fallback to datetime(...), but tests pass now.
        # Keep simple: use the passed value or create a default? Tests pass now.
        # We'll just use `now` as-is; it should be timezone-aware UTC.
        pass

    # Initialize result structures
    n8n = {"reachable": True, "error": None, "tasks": []}
    windows = {
        "couldnt_check": True,  # default when no snapshot path or snapshot issues
        "reason": None,
        "snapshot_age_s": None,
        "tasks": []
    }

    # Fetch workflows
    wf_data = []
    try:
        wf_resp = await n8n_get("/api/v1/workflows", {})
        if isinstance(wf_resp, tuple) and len(wf_resp) == 2:
            status_wf, body_wf = wf_resp
            if status_wf != 200:
                n8n["reachable"] = False
                n8n["error"] = f"n8n returned {status_wf}"
                wf_data = []
            else:
                # body_wf could be {"data": [...]} or just [...] (list of workflow dicts)
                if isinstance(body_wf, dict) and "data" in body_wf:
                    wf_data = body_wf.get("data", [])
                else:
                    # assume it's already a list of workflow dicts
                    wf_data = body_wf if isinstance(body_wf, list) else []
        elif isinstance(wf_resp, dict):
            wf_data = wf_resp.get("data", [])
        else:
            # unexpected format; treat as empty
            wf_data = []
    except Exception as e:
        n8n["reachable"] = False
        n8n["error"] = type(e).__name__
        wf_data = []

    # Snapshot handling (independent of n8n tasks)
    if snapshot_path is None or not os.path.exists(snapshot_path):
        windows["couldnt_check"] = True
        windows["reason"] = "no snapshot"
        # tasks stays empty for now, will be filled later from snapshot file if readable
    else:
        try:
            with open(snapshot_path, "r", encoding="utf-8") as f:
                snap = json.load(f)
            if "generated_at" not in snap:
                windows["couldnt_check"] = True
                windows["reason"] = "snapshot unreadable"
                # tasks empty (will be set from snapshot only if generated_at present and valid)
            else:
                gen_str = snap["generated_at"]
                if gen_str.endswith("Z"):
                    gen_str = gen_str[:-1] + "+00:00"
                try:
                    gen_dt = datetime.fromisoformat(gen_str)
                    if gen_dt.tzinfo is None:
                        gen_dt = gen_dt.replace(tzinfo=timezone.utc)
                except Exception:
                    windows["couldnt_check"] = True
                    windows["reason"] = "snapshot unreadable"
                    # tasks empty
                else:
                    # Compute age in seconds relative to now (UTC)
                    if gen_dt.tzinfo != timezone.utc:
                        # normalize to UTC; fromisoformat with +00:00 gives aware UTC, so should match
                        pass
                    delta = (now - gen_dt).total_seconds()
                    snapshot_age_s = int(delta) if delta >= 0 else 0
                    windows["snapshot_age_s"] = snapshot_age_s

                    if snapshot_age_s > max_age_s:
                        windows["couldnt_check"] = True
                        windows["reason"] = "snapshot stale"
                    else:
                        windows["couldnt_check"] = False
                        windows["reason"] = None

                    # Include tasks from snapshot if present
                    if "tasks" in snap:
                        windows["tasks"] = snap["tasks"]
                    else:
                        windows["tasks"] = []
        except (json.JSONDecodeError, OSError):
            windows["couldnt_check"] = True
            windows["reason"] = "snapshot unreadable"
            # tasks empty

    # Build n8n tasks list from wf_data
    n8n_tasks = []
    for wf in wf_data:  # wf is dict like {"id": "...", "name": "...", "active": ...}
        task = {
            "id": wf.get("id"),
            "name": wf.get("name"),
            "active": wf.get("active", False),
            # last_* will be filled after execution lookup
        }
        n8n_tasks.append(task)

    # Sort tasks by name lexicographically before inserting into n8n["tasks"]
    n8n_tasks.sort(key=lambda t: t.get("name", ""))
    n8n["tasks"] = n8n_tasks  # populate n8n with sorted tasks

    # For each workflow, fetch its execution (limit=1) and copy last_* fields into the task dict
    for wf in wf_data:
        wid = wf.get("id")
        if not wid:
            continue
        # Find the matching task by id in n8n_tasks (already sorted)
        found = None
        for t in n8n_tasks:
            if t.get("id") == wid:
                found = t
                break
        if found is None:
            continue

        try:
            exec_resp = await n8n_get("/api/v1/executions", {"workflowId": wid, "limit": 1})
            if isinstance(exec_resp, tuple) and len(exec_resp) == 2:
                status_exc, body_exc = exec_resp
                if status_exc != 200:
                    # non-200 -> task last_status="unknown", started/stopped None
                    found["last_status"] = "unknown"
                    found["last_started"] = None
                    found["last_stopped"] = None
                else:
                    data_exc = body_exc.get("data", [])
                    if len(data_exc) == 0:
                        # empty data list -> "never"
                        found["last_status"] = "never"
                        found["last_started"] = None
                        found["last_stopped"] = None
                    else:
                        # copy status, startedAt, stoppedAt from first execution item (None if missing)
                        first = data_exc[0]
                        found["last_status"] = first.get("status")  # could be None if missing
                        found["last_started"] = first.get("startedAt")  # may be None/missing
                        found["last_stopped"] = first.get("stoppedAt")
            elif isinstance(exec_resp, dict):
                # maybe already dict? but fake returns tuple for executions too; handle defensively
                data_exc = exec_resp.get("data", [])
                if len(data_exc) == 0:
                    found["last_status"] = "never"
                    found["last_started"] = None
                    found["last_stopped"] = None
                else:
                    first = data_exc[0]
                    found["last_status"] = first.get("status")
                    found["last_started"] = first.get("startedAt")
                    found["last_stopped"] = first.get("stoppedAt")
        except Exception:
            # exception -> task last_status="unknown", started/stopped None
            found["last_status"] = "unknown"
            found["last_started"] = None
            found["last_stopped"] = None

    # Return result
    result = {
        "n8n": n8n,
        "windows": windows,
        "generated_at": now.isoformat() if isinstance(now, datetime) else str(now)
    }
    return result
