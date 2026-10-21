"""Alarm classification; explicit tags win, old wake-up records remain compatible."""
import re


def is_alarm(item):
    if "category" in item:
        return item["category"] == "alarm"
    if item.get("kind") not in (None, "say"):
        return False
    # Compatibility for records made before alarms had their own tools.
    return bool(re.search(r"闹钟|起床|叫醒|alarm|wake.?up",
                          str(item.get("name", "")) + " " + str(item.get("text", "")), re.I))
