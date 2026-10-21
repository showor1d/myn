"""Conversation tools for real, explicitly reported personal purchases."""
import re
from datetime import datetime

import expenses

TOOLS = [
    {"name": "record_expense",
     "description": ("Record an actual personal expense the user reports in this conversation. "
        "Understand the context and choose eat, entertainment, shop, study, transportation, house, utilities or health. "
        "Ask if amount, currency or category is unclear; NEVER guess currency, record plans, examples, negated purchases, "
        "someone else's spending, or API/development usage. User must state the currency (CNY/JPY/USD), including a "
        "clarification in this conversation. date is the actual spending day in Tokyo, omitted=today. evidence is a "
        "verbatim excerpt of the current message confirming this purchase or its clarification. "
        "item_index is a stable zero-based index for each purchase in this message, reused on retries. "
        "The program obtains official exchange rates, converts to USD and stores USD/JPY/CNY totals; "
        "never supply or invent rates. Confirm only after a successful saved receipt."),
     "input_schema": {"type": "object", "additionalProperties": False,
        "properties": {"amount": {"type": "number", "exclusiveMinimum": 0},
            "currency": {"type": "string", "enum": list(expenses.CURRENCIES)},
            "category": {"type": "string", "enum": list(expenses.PERSONAL_CATEGORIES)},
            "description": {"type": "string", "maxLength": 300},
            "date": {"type": "string", "description": "YYYY-MM-DD"},
            "evidence": {"type": "string"}, "item_index": {"type": "integer", "minimum": 0, "maximum": 19}},
        "required": ["amount", "currency", "category", "description", "evidence", "item_index"]}},
    {"name": "expense_report",
     "description": "Read spending totals and details, with category shares based on USD. Category token includes the existing AI usage ledger automatically; never record it again as personal spending.",
     "input_schema": {"type": "object", "additionalProperties": False,
        "properties": {"period": {"type": "string", "enum": ["day", "week", "month", "year"]},
                       "offset": {"type": "integer", "maximum": 0, "minimum": -1000},
                       "category": {"type": "string", "enum": list(expenses.CATEGORIES)}}}}
]


def execute(name, args, config, question, message_id, unattended=False):
    try:
        if name == "expense_report":
            return expenses.report(config, args.get("period", "day"), args.get("offset", 0),
                                   args.get("category")), False
        evidence = args.get("evidence", "")
        index = args.get("item_index")
        if unattended or not message_id or not isinstance(evidence, str) or not evidence.strip() or evidence not in question:
            raise ValueError("The user's current words must describe an actual expense; do not record expenses automatically from background tasks or older messages")
        if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < 20:
            raise ValueError('Invalid expense item ID')
        day = args.get("date") or datetime.now(expenses.TOKYO).date().isoformat()
        when = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=expenses.TOKYO)
        if when.date() > datetime.now(expenses.TOKYO).date():
            raise ValueError('Future spending plans cannot be recorded as actual expenses')
        item = expenses.add(config, args.get("amount"), args.get("currency"), args.get("category"),
                            args.get("description"), when.isoformat(), "chat:%s:%d" % (message_id, index))
        saved = next((r for r in expenses.records(config) if r["id"] == item["id"]), None)
        if saved != item:
            raise ValueError('Saved data could not be verified. Query the record first; do not create a duplicate')
        return {"ok": True, "verified": True, "id": item["id"], "say_this":
            'Expense recorded: %s, %s, equivalent to %s, categorized as %s.' % (item["description"],
                expenses.money(item["amount_minor"], item["currency"]),
                expenses.money(item["usd"], "USD"), item["category"])}, False
    except Exception as error:
        return {"ok": False, "error": str(error)[:200]}, True


def hold_stream(question):
    return bool(re.search(r"记[一笔账]|买了|付了|消费|开销|花销|花了|花费|支出|日元|人民币|美元|美金|\b(?:CNY|JPY|USD)\b|[¥￥$]", question, re.I))


def finish(answer, results, guarded=False):
    if results:
        return "\n".join(r["say_this"] if r.get("verified") else 'This expense was not confirmed as saved: ' + r.get("error", 'Please check the record')
                         for r in results)
    if guarded and re.search(r"(?:已|已经|帮你|给你).{0,5}(?:记好|记下|记账|记录了|记录好)", answer):
        return 'This expense has not been saved. Confirm the amount, currency and purpose; I will report the result after saving it.'
    return answer
