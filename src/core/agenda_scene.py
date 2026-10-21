"""Conservative semantic lifecycle for the conversation calendar."""


def evaluate(context, decision, question):
    result = {"close": False, "revision": (context or {}).get("revision")}
    if not context or not context.get("open") or not isinstance(decision, dict):
        return result
    evidence, reason = decision.get("evidence"), decision.get("reason")
    result["close"] = bool(
        decision.get("action") == "close" and decision.get("certainty") == "clear"
        and isinstance(evidence, str) and 1 <= len(evidence.strip()) <= 500
        and evidence.strip() in question and isinstance(reason, str) and reason.strip())
    return result
