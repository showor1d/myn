"""A read-only cost receipt tied to a persisted evolution point and startup result."""
import usage_detail
import usage_stats


def outcome(point, result):
    if result.get("at_utc") and result["at_utc"] < point.get("receipt_requested_at", ""):
        result = {}  # an earlier attempt does not establish this attempt's result
    if result.get("result") == "rolled_back":
        return "Failed — previous version restored"
    if point.get("status") == "failed":
        return "Failed — update not completed"
    if point.get("status") == "updated" and result.get("result") == "kept":
        return "Succeeded — restart confirmed"
    return None


def build(config, point, result):
    status = outcome(point, result)
    if status is None:
        return None
    calls = [c for c in usage_stats.load_calls(config) if c.task_key == "evolution:" + point["id"]]
    stages = usage_detail.entries(calls, config, expand=False)
    details = usage_detail.entries(calls, config)
    known = sum(r["calls"] for r in stages)
    complete = bool(stages) and all(r["calls_complete"] for r in stages)
    count = str(known) if complete else "At least %d (incomplete history)" % known if known else "Unavailable in history"
    total = sum(c.cost for c in calls)
    total_text = usage_detail.usd(total) if calls else "Unavailable in history"
    if any(not c.priced for c in calls):
        total_text += " + unpriced usage"
    rows = [{"key": str(i), "cells": ["%d. %s" % (i + 1, r["phase"]), r["model"],
             str(r["calls"]) if r["calls_complete"] else ">=%d" % r["calls"] if r["calls"] else "Unknown",
             format(r["tokens"], ","), usage_detail.usd(r["cost_usd"]) if r["priced"] else "Unpriced"]}
            for i, r in enumerate(stages)]
    verification = point.get("verification") or {}
    test_text = ("%s; %s tests, %s skipped, %s failed" % (
        "Passed" if verification.get("accepted") else "Failed", verification.get("ran", 0),
        verification.get("skipped", 0), verification.get("failed", 0))) if verification.get("executed") else "No execution record"
    rows += [{"key": "verify", "cells": ["Verification", test_text, "0", "0", "$0.0000"]},
             {"key": "restart", "cells": ["Install / restart", status, "0", "0", "$0.0000"]}]
    body = [{"t": "h", "text": status},
            {"t": "p", "text": point["title"]},
            {"t": "p", "text": "Total: %s USD  |  Model calls: %s  |  Tokens: %s" % (
                total_text, count, format(sum(c.tokens for c in calls), ","))},
            {"t": "table", "id": "evolution-cost-stages", "height": 220,
             "columns": ["Stage / attempt", "Model / result", "Calls", "Tokens", "USD"], "rows": rows}]
    if any(r["cost_basis"] == "Allocated estimate" for r in details):
        body += [{"t": "p", "text": "Per-call amounts below are allocated estimates from each task total, using token-price weights."},
                 {"t": "table", "id": "evolution-cost-calls", "height": 170,
                  "columns": ["Step", "Model", "Tokens", "USD"],
                  "rows": [{"key": str(i), "cells": [r["phase"], r["model"], format(r["tokens"], ","),
                       usage_detail.usd(r["cost_usd"]) if r["priced"] else "Unpriced"]} for i, r in enumerate(details)]}]
    if point.get("reason") and status.startswith("Failed"):
        body.append({"t": "p", "text": str(point["reason"])[:500], "tone": "bad"})
    body.append({"t": "p", "text": "Includes planning and all recorded retries. Verification and restart run locally with no model charge. "
                 "Missing historical calls are not guessed. Startup confirmation does not replace live feature testing."})
    return dict(title="Evolution cost receipt", body=body, status=status, total_usd=total,
                calls=known, calls_complete=complete, stages=rows)


def show_tk(root, receipt, closed):
    import tkinter as tk
    from tkinter import ttk
    from tkinter.scrolledtext import ScrolledText
    window = tk.Toplevel(root)
    window.title(receipt["title"])
    window.geometry("790x540")
    window.minsize(600, 380)
    content = ScrolledText(window, wrap=tk.WORD, font=("Segoe UI", 10), padx=16, pady=12)
    content.pack(fill=tk.BOTH, expand=True)
    for node in receipt["body"]:
        if node["t"] == "table":
            content.insert(tk.END, " | ".join(node["columns"]) + "\n")
            for row in node["rows"]:
                content.insert(tk.END, " | ".join(row["cells"]) + "\n")
        else:
            content.insert(tk.END, node["text"] + "\n")
        content.insert(tk.END, "\n")
    content.configure(state=tk.DISABLED)
    def close():
        window.destroy()
        closed()
    ttk.Button(window, text="Close", command=close).pack(pady=10)
    window.protocol("WM_DELETE_WINDOW", close)
    window.bind("<Escape>", lambda event: close())
    window.lift()
    return window
