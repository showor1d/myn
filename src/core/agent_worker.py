"""MYN's background task worker: the Claude Agent SDK (the agent loop and tools behind
Claude Code), in its own Python 3.10+ environment (local-agent\\.venv).

MYN starts it and talks JSON lines over stdin/stdout (UTF-8):

  in   {"command": "run", "task_id", "prompt", "cwd", "add_dirs", "model",
        "max_turns", "max_budget_usd", "mode", "data_dir"?}
        max_budget_usd 0/None: no cost cap (the user's choice, 2026-10-18).
        mode "plan": working out a plan for a evolution point — Read / Glob / Grep only, every
        other request is refused without asking (nothing is changed, run or fetched).
        mode "self_fix": MYN editing a copy of itself — edits inside cwd are fine
        (acceptEdits), commands run inside the copy (MYN's data paths point at the
        throw-away data_dir), no web; anything else comes back as "ask" (MYN declines).
       {"command": "permission", "request_id", "allow": bool, "message"?}
       {"command": "cancel"}
  out  {"event": "ready", "sdk": version}           once, after the SDK imported
       {"event": "unavailable", "error"}           the SDK is not installed
       {"event": "text", "task_id", "text"}         what the agent says while working
       {"event": "tool", "task_id", "name", "summary"}
       {"event": "ask", "task_id", "request_id", "tool", "summary", "detail"}
       {"event": "done", "task_id", "ok", "result", "cost_usd", "turns", "error",
        "usage" (token counts only), "model"}

Permissions (the user's levels, 2026-10-17; commands 2026-10-18 "the same as you"):
looking (Read, Glob, Grep, WebSearch, WebFetch, the to-do list) runs directly; commands
(Bash) run directly too, the way Claude Code runs them, except risky ones (risky_command:
deleting, system/registry/service/power changes, force-pushing, running downloaded code,
touching MYN's personal data or keys) which come back as "ask" with the reason. Writing
and editing files (Write, Edit, ...) and any read outside the granted folders come back to
MYN as "ask" and wait for the user's yes. Claude Code's own settings files are not
loaded (setting_sources=[]), so only these rules apply. The API key arrives in the
environment from MYN and is never written anywhere.

Run with --self-test to check the SDK imports.
"""

import asyncio
import json
import os
import re
import sys
import threading
import uuid
from pathlib import Path

READ_ONLY = ["Read", "Glob", "Grep", "WebSearch", "WebFetch", "TodoWrite"]
NEVER = ["AskUserQuestion"]  # questions go in the final message instead
# Plan mode reads MYN source without editing it.
PLAN_TOOLS = ["Read", "Glob", "Grep"]
PLAN_NEVER = ["Write", "Edit", "MultiEdit", "NotebookEdit", "Bash", "WebFetch", "WebSearch", "TodoWrite"]
APPEND = (
    "You are working for MYN, the user's personal assistant on their Windows computer, as a "
    "background task. The user speaks Chinese; reply in their language. Work only inside the "
    "working directory and the added directories. Reading, searching and running commands "
    "(tests, builds, scripts, git status...) run directly; writing or editing files and risky "
    "commands (deleting, system settings, force-pushing...) are shown to the user first and may "
    "be declined: then find another way or stop and explain. Verify your work by running it "
    "when you can. Never delete files unless the task explicitly asks for it. If you need a "
    "decision you cannot make, stop and ask it in your final message. End with a short summary "
    "of what you did and the result, in plain sentences that can be read aloud (put file paths "
    "and details after the summary)."
)

# Commands that are hard to undo or reach beyond the task: the user is asked first, as
# Claude Code confirms before destructive actions. (reason, pattern), case-insensitive.
RISKY = [
    ('Deletes files or folders', r"(?:^|[\s;&|(`])(?:rm|del|erase|rd|rmdir|shred|unlink)(?=\s|$)|"
                     r"remove-item|\bri\s+-|clear-content|clear-recyclebin|\bsdelete\b|cipher\s+/w"),
    ('Changes the system, disks or registry', r"\bformat(?:\.com)?\s+[a-z]:|\bdiskpart\b|\bbcdedit\b|\breg(?:\.exe)?\s+(?:add|delete|import)\b|"
                          r"set-itemproperty\s+.*(?:hklm|hkcu)|new-itemproperty|remove-itemproperty|"
                          r"\bvssadmin\b|\bwmic\b|set-executionpolicy|\bicacls\b|\btakeown\b|\bnet\s+(?:user|localgroup)\b|"
                          r"\bsc(?:\.exe)?\s+(?:delete|config|create|stop)\b|\bschtasks\b|"
                          r"(?:enable|disable)-(?:windowsoptionalfeature|netadapter)|set-mppreference|netsh\s+advfirewall"),
    ('Shuts down, restarts or terminates programs', r"\bshutdown\b|restart-computer|stop-computer|\blogoff\b|stop-process|\btaskkill\b|\bkill\s+-9\b"),
    ('Rewrites a remote repository or discards changes', r"\bgit\s+push\b|\bgit\s+reset\s+--hard|\bgit\s+clean\b|\bgit\s+checkout\s+--\s|"
                            r"\bgit\s+branch\s+-D\b|\bgit\s+stash\s+(?:drop|clear)\b|\bgit\b.*\s--force(?:-with-lease)?(?=\s|$)|\bnpm\s+publish\b|\btwine\s+upload\b"),
    ('Downloads and runs code, or runs as administrator', r"(?:iwr|invoke-webrequest|curl|wget|irm|invoke-restmethod)\b.*\|\s*(?:iex|invoke-expression|sh|bash|python)|"
                                    r"\b(?:iex|invoke-expression)\b|-verb\s+runas|\brunas\b|\bsudo\b"),
    ('Accesses MYN personal data or credentials', r"personal_data|jarvis\.db|\.dpapi\b|claude-key|elevenlabs-key|file-access\.json|"
                              r"anthropic_api_key|elevenlabs_api_key"),
]
_RISKY = [(reason, re.compile(pattern, re.I)) for reason, pattern in RISKY]
_ABSOLUTE = re.compile(r"(?<![\w.])(?:[a-z]:[\\/]|\\\\)[^\s'\"|;&<>]*", re.I)


def risky_command(command, inside=None):
    """Why a command needs the user's yes, or "" when it can just run.

    inside: when given (self-fix), any absolute path not under it, or a '..' step out,
    is also risky — the command must stay inside MYN's copy."""
    if not isinstance(command, str) or not command.strip():
        return 'Invalid command'
    for reason, pattern in _RISKY:
        if pattern.search(command):
            return reason
    if inside:
        root = os.path.normcase(os.path.normpath(str(inside)))
        # Running a program by its full path (python.exe, git.exe) is fine; what it is
        # pointed at must stay inside the copy.
        rest = re.sub(r"\"[^\"]*\.exe\"|'[^']*\.exe'|\S+\.exe\b", " ", command, flags=re.I)
        for match in _ABSOLUTE.findall(rest):
            path = os.path.normcase(os.path.normpath(match.rstrip(".,)")))
            if path != root and not path.startswith(root + os.sep):
                return 'Uses a path outside the working copy'
        if re.search(r"(?:^|[\s\\/'\"])\.\.(?:[\\/]|\s|$)", command):
            return 'Uses a path outside the working copy'
    return ""
_OUT = threading.Lock()


def emit(message):
    with _OUT:
        sys.stdout.write(json.dumps(message, ensure_ascii=False) + "\n")
        sys.stdout.flush()


def _usage_numbers(usage):
    """Token counts from the SDK's result, numbers only (for MYN's usage statistics)."""
    if not isinstance(usage, dict):
        return {}
    numbers = {name: usage[name] for name in ("input_tokens", "output_tokens", "cache_creation_input_tokens",
                                              "cache_read_input_tokens")
               if type(usage.get(name)) is int and usage[name] >= 0}
    server = usage.get("server_tool_use")
    if isinstance(server, dict) and type(server.get("web_search_requests")) is int:
        numbers["server_tool_use"] = {"web_search_requests": server["web_search_requests"]}
    return numbers


def _inside(path, root):
    try:
        Path(path).resolve().relative_to(Path(root).resolve())
        return True
    except (OSError, ValueError):
        return False


def planning_read_allowed(tool, data, cwd):
    """The plan agent only sees the program copy, even for auto-approved SDK tools."""
    if tool not in PLAN_TOOLS or not cwd or not isinstance(data, dict):
        return False
    raw = data.get("file_path") if tool == "Read" else data.get("path", ".")
    if not isinstance(raw, str) or not raw:
        return False
    candidate = Path(raw) if Path(raw).is_absolute() else Path(cwd) / raw
    if not _inside(candidate, cwd):
        return False
    # A Glob pattern may carry its own path instead of the separate path argument.
    pattern = data.get("pattern", "") if tool == "Glob" else ""
    return not (Path(pattern).is_absolute() or ".." in Path(pattern).parts)


def summarize(tool, data):
    """One line about a tool call, for MYN's task log and the approval dialog."""
    data = data if isinstance(data, dict) else {}
    for key in ("command", "file_path", "path", "pattern", "query", "url", "notebook_path"):
        value = data.get(key)
        if isinstance(value, str) and value:
            return value if len(value) <= 300 else value[:297] + "..."
    return ""


def detail(tool, data):
    """More of a change for the approval dialog (what will be written / run)."""
    data = data if isinstance(data, dict) else {}
    if tool == "Bash":
        reason = data.get("_myn_reason")
        return (('Reason: %s\n' % reason) if reason else "") + str(data.get("description") or "")[:300]
    if tool == "Write":
        return str(data.get("content") or "")[:600]
    if tool == "Edit":
        return ("- " + str(data.get("old_string") or "")[:250] + "\n+ " + str(data.get("new_string") or "")[:250])
    return ""


class Worker:
    def __init__(self, sdk, loop):
        self.sdk, self.loop = sdk, loop
        self.pending = {}
        self.client = None
        self.task = None

    async def ask(self, task_id, tool, data):
        request_id = uuid.uuid4().hex[:12]
        future = self.loop.create_future()
        self.pending[request_id] = future
        emit({"event": "ask", "task_id": task_id, "request_id": request_id, "tool": tool,
              "summary": summarize(tool, data), "detail": detail(tool, data)})
        return await future

    def answer(self, request_id, allow, message=""):
        future = self.pending.pop(request_id, None)
        if future is not None and not future.done():
            future.set_result((bool(allow), message))

    def deny_all(self, message):
        for request_id in list(self.pending):
            self.answer(request_id, False, message)

    async def run(self, request):
        sdk = self.sdk
        task_id = str(request.get("task_id") or "task")
        types = getattr(sdk, "types", sdk)

        self_fix = request.get("mode") == "self_fix"
        cwd = request.get("cwd") or None

        planning = request.get("mode") == "plan"
        token_budget = request.get("token_budget")
        token_budget = token_budget if type(token_budget) is int and token_budget >= 0 else None
        usage_by_message = {}
        observed_usage = {}
        quota_hit = False

        async def can_use_tool(tool_name, input_data, context):
            if planning:  # a plan is only thought out: nothing is changed, run or fetched
                return types.PermissionResultDeny(message="Only Read, Glob and Grep are available: "
                                                          "this is planning, nothing may be changed.")
            asked = input_data
            if tool_name == "Bash":
                command = input_data.get("command") if isinstance(input_data, dict) else None
                reason = risky_command(command, inside=cwd if self_fix else None)
                if not reason:  # an ordinary command runs directly, as in Claude Code
                    return types.PermissionResultAllow(updated_input=input_data)
                asked = dict(input_data, _myn_reason=reason)
            allow, message = await self.ask(task_id, tool_name, asked)
            if allow:
                return types.PermissionResultAllow(updated_input=input_data)
            return types.PermissionResultDeny(message=message or "The user declined this action.")

        async def keep_open(input_data, tool_use_id, context):
            # Documented workaround: a PreToolUse hook keeps the stream open for can_use_tool.
            if quota_hit:
                return {"continue_": False, "stopReason": "Monthly evolution token quota reached."}
            if planning and not planning_read_allowed(input_data.get("tool_name"),
                                                     input_data.get("tool_input"), cwd):
                return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                        "permissionDecisionReason": "Planning may only read files inside the program copy."}}
            return {"continue_": True}

        budget = request.get("max_budget_usd")
        extra = {}
        data_dir = request.get("data_dir")
        if self_fix and isinstance(data_dir, str) and data_dir:
            # Commands in the copy (running its tests, starting it) must never reach the
            # user's real data: point every MYN data path at a throw-away folder.
            extra["env"] = {"MYN_PERSONAL_DATA_DIR": data_dir,
                            "MYN_DATA_DIR": os.path.join(data_dir, "database"),
                            "MYN_CONFIG_DIR": os.path.join(data_dir, "config"),
                            "MYN_LEGACY_DATA_DIR": os.path.join(data_dir, "no-legacy")}
        options = sdk.ClaudeAgentOptions(
            system_prompt={"type": "preset", "preset": "claude_code", "append": APPEND},
            allowed_tools=list(PLAN_TOOLS if planning else READ_ONLY),
            disallowed_tools=list(NEVER) + (["WebFetch", "WebSearch"] if self_fix else []) +
            (list(PLAN_NEVER) if planning else []),
            permission_mode="acceptEdits" if self_fix else "default", cwd=cwd,
            add_dirs=[d for d in request.get("add_dirs") or [] if isinstance(d, str)],
            model="claude-opus-5-5", max_turns=int(request.get("max_turns") or 200),
            # 0 / None: no cost cap (the user turned it off, 2026-10-18)
            max_budget_usd=float(budget) if isinstance(budget, (int, float)) and budget > 0 else None,
            can_use_tool=can_use_tool, setting_sources=[],
            hooks={"PreToolUse": [types.HookMatcher(matcher=None, hooks=[keep_open])]}, **extra)
        done = {"event": "done", "task_id": task_id, "ok": False, "result": "", "cost_usd": None,
                "turns": None, "error": "", "usage": {}, "model": request.get("model") or ""}
        last_text = ""
        try:
            if token_budget == 0:
                done["error"] = "evolution_quota"
                emit(done)
                return
            async with sdk.ClaudeSDKClient(options=options) as client:
                self.client = client
                await client.query(str(request.get("prompt") or ""))
                async for message in client.receive_response():
                    kind = type(message).__name__
                    if kind == "AssistantMessage":
                        usage = _usage_numbers(getattr(message, "usage", None))
                        ident = getattr(message, "message_id", None) or getattr(message, "uuid", None)
                        if usage:
                            ident = ident or "anonymous-%d" % len(usage_by_message)
                            previous = usage_by_message.setdefault(ident, {})
                            for name, value in usage.items():
                                if type(value) is int:
                                    previous[name] = max(previous.get(name, 0), value)
                            observed_usage = {name: sum(part.get(name, 0) for part in usage_by_message.values())
                                              for name in ("input_tokens", "output_tokens", "cache_creation_input_tokens",
                                                           "cache_read_input_tokens")}
                        for block in getattr(message, "content", []) or []:
                            name = type(block).__name__
                            if name == "TextBlock" and getattr(block, "text", "").strip():
                                last_text = block.text[:8000]
                                emit({"event": "text", "task_id": task_id, "text": block.text[:4000]})
                            elif name == "ToolUseBlock":
                                emit({"event": "tool", "task_id": task_id, "name": getattr(block, "name", ""),
                                      "summary": summarize(getattr(block, "name", ""), getattr(block, "input", {}))})
                        if (token_budget is not None and sum(observed_usage.values()) >= token_budget and
                                any(type(block).__name__ == "ToolUseBlock" for block in getattr(message, "content", []) or [])):
                            quota_hit = True
                            await client.interrupt()
                    elif kind == "ResultMessage":
                        done.update(ok=not getattr(message, "is_error", False)
                                    and getattr(message, "subtype", "success") == "success",
                                    result=str(getattr(message, "result", "") or "")[:8000],
                                    cost_usd=getattr(message, "total_cost_usd", None),
                                    turns=getattr(message, "num_turns", None),
                                    usage=_usage_numbers(getattr(message, "usage", None)))
                        if not done["ok"]:
                            done["error"] = str(getattr(message, "subtype", "error"))
        except asyncio.CancelledError:
            done["error"] = "cancelled"
        except Exception as error:  # report, never die: MYN keeps the worker for the next task
            done["error"] = "%s: %s" % (type(error).__name__, str(error)[:300])
        finally:
            self.client = None
            self.deny_all("The task ended.")
        if not done["result"] and last_text:
            done["result"] = last_text  # e.g. stopped by the cost cap right after answering
        if not done["usage"]:
            done["usage"] = observed_usage
        if quota_hit:
            done.update(ok=False, error="evolution_quota")
        emit(done)

    async def cancel(self):
        self.deny_all("The user stopped the task.")
        client = self.client
        if client is not None:
            try:
                await client.interrupt()
            except Exception:
                pass
        if self.task is not None and not self.task.done():
            self.task.cancel()


async def serve(sdk, lines):
    """The command loop. `lines` yields raw stdin lines (a thread feeds them in)."""
    loop = asyncio.get_running_loop()
    inbox = asyncio.Queue()

    def feed():
        for line in lines:
            loop.call_soon_threadsafe(inbox.put_nowait, line)
        loop.call_soon_threadsafe(inbox.put_nowait, None)

    threading.Thread(target=feed, daemon=True).start()
    worker = Worker(sdk, loop)
    emit({"event": "ready", "sdk": getattr(sdk, "__version__", "")})
    while True:
        line = await inbox.get()
        if line is None:
            await worker.cancel()
            break
        try:
            request = json.loads(line)
        except ValueError:
            continue
        if not isinstance(request, dict):
            continue
        command = request.get("command")
        if command == "run":
            if worker.task is not None and not worker.task.done():
                emit({"event": "done", "task_id": request.get("task_id"), "ok": False, "result": "",
                      "error": "busy"})
                continue
            worker.task = asyncio.create_task(worker.run(request))
        elif command == "permission":
            worker.answer(request.get("request_id"), request.get("allow") is True, str(request.get("message") or ""))
        elif command == "cancel":
            await worker.cancel()
    if worker.task is not None:
        try:
            await asyncio.wait_for(worker.task, 5)
        except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
            pass


def main():
    for stream in (sys.stdin, sys.stdout):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    try:
        import claude_agent_sdk as sdk
    except Exception as error:
        emit({"event": "unavailable", "error": "claude-agent-sdk: %s" % str(error)[:200]})
        return 3
    if "--self-test" in sys.argv:
        print(json.dumps({"ok": True, "sdk": getattr(sdk, "__version__", ""), "python": sys.version.split()[0]}))
        return 0
    asyncio.run(serve(sdk, sys.stdin))
    return 0


if __name__ == "__main__":
    sys.exit(main())
