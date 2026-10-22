"""Read-only daily and task detail projections; never add a second billed entry."""
from decimal import Decimal

import usage_stats


def category(purpose):
    text = str(purpose or "")
    if text.startswith(("evolution", "self_fix")):
        return "Self-improvement"
    if text.startswith(("memory", "save_classification", "scene_analysis", "profile", "preference")):
        return "Long-term memory"
    if text.startswith(("chat", "work")) and not text.startswith("chatgpt"):
        return "Daily chat"
    if text.startswith(("interaction_scene", "conversation_judge")):
        return "Speech understanding"
    if text.startswith("agent_task"):
        return "Background work"
    if text.startswith(("claude_code", "codex_development")):
        return "Development"
    if text.startswith("voice"):
        return "Speech synthesis"
    return "Other usage"


def phase(purpose):
    if purpose.startswith("evolution_"):
        step = purpose[len("evolution_"):].removesuffix("_l1")
        return {"plan": "Planning", "build": "Implementation", "mining": "Discovery",
                "background": "Background work"}.get(step, step.title())
    if purpose == "self_fix":
        return "Implementation"
    return usage_stats.purpose_name(purpose)


def usd(value):
    """Keep small per-call charges visible rather than rounding them to zero."""
    return "$%.6f" % value if 0 < abs(value) < .01 else "$%.4f" % value


def _steps(call):
    """Malformed optional detail must not hide an otherwise valid billed total."""
    steps = call.details.get("usage_steps") or []
    if not isinstance(steps, list):
        return []
    for step in steps:
        if (not isinstance(step, dict) or not isinstance(step.get("id"), str) or
                not isinstance(step.get("model"), str) or not isinstance(step.get("usage"), dict)):
            return []
        if any(type(step["usage"].get(k, 0)) is not int or step["usage"].get(k, 0) < 0
               for k in usage_stats.TOKEN_FIELDS):
            return []
    return steps


def call_count(call):
    steps = _steps(call)
    if call.details.get("status") == "agent_done":
        identified = {s.get("id") for s in steps if s.get("id") and s.get("identified", True)}
        return (len(identified), bool(call.details.get("calls_complete")) and len(identified) == len(steps)) if steps else (0, False)
    if call.details.get("source", "claude") == "claude":
        return 1, True
    return 0, False


def entries(calls, config=None, expand=True):
    """One row per call, or a clearly marked task total when call detail is missing."""
    titles = {}
    prices = usage_stats.load_settings(config).prices if config is not None else None
    if config is not None:
        import evolution
        titles = {"evolution:" + p["id"]: p["title"] for p in evolution.points(config)}
    result = []
    for index, call in enumerate(calls):
        task = titles.get(call.task_key) or phase(call.purpose)
        count, complete = call_count(call)
        model = str(call.details.get("model") or call.group)
        row = dict(name=model, model=model, category=category(call.purpose), task=task,
                   phase=phase(call.purpose), when=call.when.isoformat(), tokens=call.tokens,
                   cost_usd=call.cost, priced=call.priced, estimated=call.estimated or
                   (call.details.get("status") == "agent_done" and call.details.get("cost_source") != "sdk"),
                   calls=count, calls_complete=complete, task_key=call.task_key,
                   run_id=call.details.get("run_id") or "record-%d" % index,
                   cost_basis="SDK task total" if call.details.get("cost_source") == "sdk" else
                              "Historical task total / pricing estimate" if call.details.get("status") == "agent_done" else
                              "Estimated" if call.estimated else "Recorded usage")
        steps = _steps(call)
        if (expand and steps and complete and call.priced and
                sum(sum(s.get("usage", {}).get(k, 0) for k in usage_stats.TOKEN_FIELDS) for s in steps) == call.tokens):
            weights = [Decimal(str(usage_stats.cost_of(dict(s.get("usage", {}), model=s.get("model")), prices)[0])) for s in steps]
            total_weight = sum(weights)
            left = Decimal(str(call.cost))
            for n, step in enumerate(steps):
                value = left if n == len(steps) - 1 else Decimal(str(call.cost)) * (
                    weights[n] / total_weight if total_weight else Decimal(1) / len(steps))
                left -= value
                result.append(dict(row, name=step["model"], model=step["model"], calls=1,
                                   tokens=sum(step["usage"].get(k, 0) for k in usage_stats.TOKEN_FIELDS),
                                   cost_usd=float(value), estimated=True, cost_basis="Allocated estimate",
                                   phase=phase(call.purpose) + " / call %d" % (n + 1),
                                   tools=", ".join(t for t in (step.get("tools") or []) if isinstance(t, str))))
        else:
            result.append(row)
    return result


def description(row):
    count = str(row["calls"]) if row["calls_complete"] else (
        "at least %d" % row["calls"] if row["calls"] else "unknown")
    task = row["task"] if row["task"] == row["phase"] else row["task"] + " / " + row["phase"]
    return "%s · %s · %s tokens · %s %s · %s" % (
        row["when"][11:19], task, format(row["tokens"], ","), count,
        "call" if count == "1" else "calls",
        row["cost_basis"] if row["priced"] else "Cost unavailable")
