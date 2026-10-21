'Self-evolution in the window: discover, plan and propose improvements from chat. Build adopted proposals and apply them automatically after tests.\n\nWork on one task at a time, prioritizing level-one tasks within the dollar budget. Use the existing self-update copy through myn_gui._start_self_fix. Passing tests marks the point as built; apply the copy when MYN is idle.\n'


from gui_common import (
    Path,
    agent_client,
    claude_client,
    local_actions,
    self_update,
    threading,
    time,
    tk,
)
import evolution
import interaction

TICK_MS = 20000
ANNOUNCE_GAP_S = 90      # At most one evolution announcement per interval.
MINING_MIN_LINES = 5     # new things the user said before reading them back is worth it
MINING_FIRST_DAYS = 14   # how far back the first reading goes
MINING_MAX_CHARS = 40000


class EvolutionMixin:
    evolution_run = None       # {"task_id", "point_id"} while a plan is being worked out
    evolution_mining = None    # "running" while reading back what the user said
    evolution_error = ""       # the last problem, for myn_status
    evolution_applying = False
    _evolution_said_at = 0.0

    def _evolution_config(self):
        return self._usage_config()

    def _evolution_boot(self):
        try:
            config = self._evolution_config()
            outcomes = self_update.evolution_results()
            for point in evolution.points(config):
                result = outcomes.get(point["id"], {})
                if point["status"] in ("updating", "updated", "built"):
                    if result.get("result") == "rolled_back" and result.get("at_utc", "") >= point.get("updated_at", ""):
                        evolution.update(config, point["id"], 'The new version failed to start; rolled back automatically', status="failed",
                                         reason='The new version failed to start; rolled back automatically', work=None)
                    elif point["status"] == "updating" and result.get("result") in ("trying", "kept"):
                        evolution.update(config, point["id"], 'New version installed', status="updated", work=None)
            evolution.recover(config)
        except Exception as error:
            self.evolution_error = 'Could not restore evolution point: %s' % type(error).__name__
        self.root.after(TICK_MS, self._evolution_tick)

    def _evolution_changed(self):
        self._evolution_cancel_obsolete()

    def _evolution_cancel_obsolete(self):
        check = getattr(self, "_agent_budget_check", None)
        if check:
            check()
        run = self.evolution_run or self.self_fix
        point_id = (run or {}).get("point_id") or (run or {}).get("point")
        if not point_id or self.agent_runner is None or not self.agent_runner.busy:
            return
        point = evolution.get(self._evolution_config(), point_id)
        expected = "planning" if self.evolution_run else "building"
        # A spoken budget reduction takes effect during work, not only next launch.
        settings = evolution.load_settings(self._evolution_config())
        if ((point and point.get("level") != 1 and "budget_limit" in run and settings.monthly_usd < run["budget_limit"])
                or ("task_limit" in run and settings.task_usd < run["task_limit"])):
            run["budget_cancelled"] = True
            self.agent_runner.cancel()
            return
        if point is None or point["status"] != expected or not local_actions.load_settings(self._evolution_config()).self_fix:
            self.agent_runner.cancel()

    # ---- the queue ---------------------------------------------------------------------------
    def _evolution_tick(self):
        try:
            self.root.after(TICK_MS, self._evolution_tick)
        except tk.TclError:
            return
        if self.session is None or getattr(self, "backup_working", False):
            return
        config = self._evolution_config()
        try:
            self._evolution_cancel_obsolete()
            self._background_notice_ready()
            self._evolution_announce(config)
            self._evolution_work(config)
        except Exception as error:  # never let this take MYN down
            self.evolution_error = "%s: %s" % (type(error).__name__, str(error)[:200])

    def _evolution_busy(self):
        return (self.self_fix is not None or self.evolution_run is not None or self.evolution_mining is not None or
                self.evolution_applying or
                (self.agent_runner is not None and self.agent_runner.busy))

    def _evolution_work(self, config):
        if self._evolution_auto_update(config):
            return
        actions = local_actions.load_settings(config)
        if not actions.self_fix and not actions.agent:
            return
        if self._evolution_busy() or not agent_client.installed():
            return
        key = self.session._key()
        if not key:
            return
        budget = evolution.quota(config)
        point, phase = evolution.next_work(config)
        actions = local_actions.load_settings(config)
        if phase == "background":
            if actions.agent:
                task = point.get("task_prompt") or point["title"]
                task += "\n" + "\n".join(n.get("text", "") for n in point.get("notes", []))
                try:
                    self._start_agent(task, point.get("task_folder") or "", point_id=point["id"])
                except Exception as error:
                    evolution.update(config, point["id"], 'Could not start: %s' % error, status="failed")
            return
        if not actions.self_fix:
            return
        if phase == "plan":
            self._evolution_plan(config, point, key)
        elif phase == "build":
            self._evolution_build(point)
        elif budget["stage"] == "normal" and self.evolution_mining is None:
            self._evolution_mine_maybe(config, key)

    # ---- working out a plan (read-only) -----------------------------------------------------------
    def _evolution_plan(self, config, point, key):
        if self.agent_runner is None:
            self.agent_runner = agent_client.AgentRunner(lambda message: self.events.put(("agent", message)))
        settings = evolution.runner_settings(config, agent_client.load_settings(config), point_id=point["id"])
        if settings is None:
            return
        rules = self_update._rules((self_update.ROOT / "AGENTS.md").read_text(encoding="utf-8"))
        task_id = "plan%d" % int(time.time() * 1000)
        work = self_update.prepare(task_id)
        evolution.update(config, point["id"], 'Starting planning', status="planning")
        self.evolution_run = {"task_id": task_id, "point_id": point["id"], "work": work,
                              "purpose": evolution.purpose("plan", point),
                              "budget_limit": evolution.load_settings(config).monthly_usd,
                              "task_limit": evolution.load_settings(config).task_usd}
        self.agent_log = [("task", 'Self-evolution: plan improvement "%s"' % point["title"])]
        try:
            self.agent_runner.run(task_id, evolution.plan_prompt(point, rules), key, work, [],
                                  settings, mode="plan")
        except Exception as error:
            self.evolution_run = None
            self_update.discard(work)
            evolution.update(config, point["id"], 'Could not start: %s' % error, status="new")
        self._evolution_changed()

    def _evolution_event(self, message):
        """Events of a planning run: questions are refused, the result becomes the plan."""
        kind = message.get("event")
        run = self.evolution_run
        if not run:
            return
        if kind == "ask":
            self.agent_runner.answer(message.get("request_id"), False, "Planning only reads; nothing may be changed.")
        elif kind == "tool":
            self._agent_log_add("tool", "%s %s" % (self.AGENT_TOOLS.get(message.get("name"), message.get("name") or ""),
                                                   message.get("summary") or ""))
        elif kind == "done":
            self.evolution_run = None
            config = self._evolution_config()
            point_id = run["point_id"]
            if run.get("work"):
                self_update.discard(run["work"])
            current = evolution.get(config, point_id)
            if current is None or current["status"] != "planning":
                self._evolution_changed()
                return  # the user rejected or adjusted it while the old plan was running
            if message.get("error") in ("evolution_quota", "error_max_budget_usd") or run.get("budget_cancelled"):
                if message.get("error") in ("evolution_quota", "error_max_budget_usd"):
                    evolution.pause_budget(config, point_id)
                evolution.update(config, point_id, 'Budget paused: raise the per-task limit if reached; for a monthly limit, you may wait until next month', status="new")
                self._evolution_changed()
                return
            if not message.get("ok"):
                reason = {"cancelled": 'Stopped'}.get(message.get("error"), message.get("error") or 'An error occurred')
                evolution.update(config, point_id, 'Planning failed: %s' % reason, status="failed", reason=reason)
                self._agent_log_add("done", 'No plan produced: ' + reason)
            else:
                try:
                    plan = evolution.parse_plan(message.get("result"))
                except ValueError as error:
                    evolution.update(config, point_id, 'Could not read the plan: %s' % error, status="failed",
                                     reason='Could not read the plan')
                else:
                    evolution.update(config, point_id, 'Plan ready' if not plan["exists"] else 'This feature already exists',
                                     status="exists" if plan["exists"] else "proposed", plan=plan, announced="")
                    self._agent_log_add("done", 'Plan: ' + plan["summary"])
            self._evolution_changed()

    # ---- building: the self-evolution run on a copy --------------------------------------------------
    def _evolution_build(self, point):
        config = self._evolution_config()
        evolution.update(config, point["id"], 'Starting development', status="building")
        try:
            self._start_self_fix(evolution.build_problem(point), point_id=point["id"])
        except Exception as error:
            evolution.update(config, point["id"], 'Could not start: %s' % error, status="adopted")
        self._evolution_changed()

    def _evolution_built(self, fix, changed):
        """Apply an adopted point after tests, once foreground work is idle."""
        config = self._evolution_config()
        point = evolution.get(config, fix["point"])
        if point is None or point["status"] != "building":
            self._self_fix_discard(fix)
            self.self_fix = None
            self._evolution_changed()
            return
        evolution.update(config, fix["point"], 'Implemented; all tests passed', status="built", work=str(fix["work"]),
                         result=fix["summary"][:2000], changed=changed[:40], announced="")
        self.self_fix = None
        self._evolution_changed()
        self._evolution_auto_update(config)

    def _evolution_auto_update(self, config):
        """Adoption authorizes application; merely proposed/unapproved work never does.

        Run before API/key/quota checks: applying an already tested copy costs no
        model tokens. Persisted built points are resumed by the same tick on restart.
        """
        if not local_actions.load_settings(config).self_fix:
            return False
        ready = [p for p in evolution.points(config) if p["status"] == "built"
                 and p.get("plan_approved") is True and p.get("work")]
        if not ready:
            return False
        if self._evolution_idle() and not self._evolution_busy() and not getattr(self, "pending_confirmation", None):
            self._evolution_update(sorted(ready, key=evolution._order)[0]["id"])
        return True

    def _evolution_build_failed(self, fix, reason):
        point = evolution.get(self._evolution_config(), fix["point"])
        if point is None or point["status"] != "building":
            return
        evolution.update(self._evolution_config(), fix["point"], 'Could not complete: %s' % reason[:120], status="failed",
                         reason=reason[:500])
        self._evolution_changed()

    # ---- telling the user ---------------------------------------------------------------------------
    def _evolution_idle(self):
        return (interaction.idle(self._evolution_config()) and
                not (self.busy or self.draft_active or self._speaking() or self.conversation_open or
                     getattr(self, "media_playing", False) or
                     getattr(self, "web_intro", False) or getattr(self, "pending_confirmation", None) or
                     getattr(self, "reader_open", False)))

    def _evolution_announce(self, config):
        import reporting
        if reporting.active(config) and reporting.context(config).get('state') == 'offered':
            return
        if (not local_actions.load_settings(config).self_fix or not self._evolution_idle() or
                time.time() - self._evolution_said_at < ANNOUNCE_GAP_S):
            return
        # Drop stale offers after completion, rejection, adjustment or a new plan.
        interaction.current_invitation(config)
        waiting = sorted((item for item in evolution.points(config)
                          if item["status"] in ("proposed", "exists", "built") and item.get("announced") != item["status"]
                          and not (item["status"] == "built" and item.get("plan_approved") is True)),
                         key=lambda item: (item.get("level", 3), item.get("updated_at", "")))
        if not waiting:
            return
        point = waiting[0]
        if not interaction.offer(config, point):
            return
        state = 'Plan completed' if point["status"] == "proposed" else (
            'Tests passed' if point["status"] == "built" else 'The explanation of the existing feature is ready')
        spoken = 'The improvement for "%s" is %s. Is now a good time to review it?' % (point["title"][:45], state)
        self._evolution_said_at = time.time()
        self.append("MYN", spoken, speakable=True, spoken_text=spoken)
        self.speak(spoken, purpose="proactive")
        self.conversation_open = True
        self.conversation_touch = self.last_answer_at = time.time()
        self._evolution_changed()

    def _evolution_ask_update(self, point):
        def answered(yes):
            if yes:
                self._evolution_update(point["id"])
        self._ask_user('MYN self-evolution', evolution.announcement(point)[0], answered, yes='Update', no='Later')

    # ---- putting a built point in place (only after the user's yes) -----------------------------------
    def evolution_update_request(self, point_id):
        """From the chat thread (evolution_points update, already confirmed by the user)."""
        self.events.put(("call", lambda: self._evolution_update(point_id)))
        return {"updating": True, "note": 'Back up first, install the new version, then restart.'}

    def _evolution_update(self, point_id):
        config = self._evolution_config()
        if not local_actions.load_settings(config).self_fix:
            self.append("MYN", 'Self-evolution is disabled. Enable it before applying updates.')
            return
        point = evolution.get(config, point_id)
        if point is None or point["status"] != "built" or not point.get("work"):
            self._set_voice_label('This evolution point cannot be applied right now', clear_after=3000)
            return
        if self._evolution_busy():
            self.append("MYN", 'A background task is running. Wait for it to finish before applying "%s".' % point["title"])
            return
        evolution.update(config, point_id, 'Starting update', status="updating")
        self.evolution_applying = True
        self._set_voice_label('Updating...', clear_after=20000)
        work = point["work"]

        def apply():
            try:
                clashes = self_update.live_changes(work)
                if clashes:
                    self.events.put(("call", lambda: self._evolution_redo(point_id, work, clashes)))
                    return
                ok, detail = self_update.apply(work, 'Evolution point: %s\n%s' % (point["title"], point.get("result") or ""),
                                               point_id=point_id)
            except Exception as error:
                ok, detail = False, 'Update check incomplete: %s' % type(error).__name__
            self.events.put(("call", lambda: self._evolution_applied(point_id, work, ok, detail)))
        threading.Thread(target=apply, daemon=True).start()

    def _evolution_redo(self, point_id, work, clashes):
        self.evolution_applying = False
        config = self._evolution_config()
        self._self_fix_discard({"work": work})
        point = evolution.update(config, point_id, 'The program changed during development (%s); rebuilding' % "、".join(clashes[:4]),
                                 status="adopted", work=None)
        self.append("MYN", 'The program changed after "%s" was built. To preserve those changes, I will rebuild it on the current version and '
                           'apply it automatically after tests pass.' % point["title"])
        self._evolution_changed()

    def _evolution_applied(self, point_id, work, ok, detail):
        self.evolution_applying = False
        config = self._evolution_config()
        self._self_fix_discard({"work": work})
        if not ok:
            evolution.update(config, point_id, 'Update failed: %s' % detail[:120], status="failed", reason=detail[:500],
                             work=None)
            self.append("MYN", 'Could not apply the evolution point: %s\n\nThe running version is unchanged.' % detail[:600])
            self._evolution_changed()
            return
        point = evolution.update(config, point_id, 'Updated', status="updated", work=None)
        self.append("MYN", 'Evolution point "%s" is installed. Restarting now. Previous version backup: %s' % (point["title"], detail))
        self.stop_speech()
        self.root.after(1500, self._restart_now)

    # ---- the chat's direct "change yourself" ------------------------------------------------------------
    def evolve_now(self, problem, level=2):
        """fix_myself from the chat thread: the user decided already, so it is a point that
        is adopted at once (no plan to approve); it is tested before automatic updating."""
        if not agent_client.installed():
            raise local_actions.ActionError(
                'Self-evolution requires background-task components. Run %s once in PowerShell, then restart MYN.' % agent_client.SETUP_SCRIPT)
        if not self.session._key():
            raise local_actions.ActionError('Configure a Claude API key first.')
        point, merged = evolution.add(self._evolution_config(), problem[:30], kind="improvement", request=problem,
                                      level=level, source="user", adopted_plan=problem)
        if merged and point["status"] in ("planning", "building", "updating", "built"):
            return {"started": False, "evolution_point": point["id"], "note": 'This evolution point is already being processed or waiting to be applied.'}
        if merged and point["status"] != "adopted":
            evolution.update(self._evolution_config(), point["id"], 'You directly requested this change', status="adopted",
                             plan=point.get("plan") or {"summary": problem[:300], "plan": problem, "exists": False,
                                                        "size": "", "risks": "", "files": []})
        busy = self._evolution_busy()
        waiting_quota = (not evolution.task_budget(self._evolution_config(), point["id"])["can_start"] or
                         (point.get("level") != 1 and not evolution.quota(self._evolution_config())["can_start"]))
        self.events.put(("call", lambda: (self._evolution_changed(), None if busy else self._evolution_tick_now())))
        return {"started": not busy and not waiting_quota, "evolution_point": point["id"],
                "note": ('The monthly evolution budget is near or at its limit. Saved for later; raise the budget or continue next month.' if waiting_quota else
                         'Starting now.' if not busy else 'I will start after the current task finishes.') +
                        'After implementation and successful tests, I will back up, update and restart automatically.'}

    def _evolution_tick_now(self):
        try:
            self._evolution_work(self._evolution_config())
        except Exception as error:
            self.evolution_error = "%s: %s" % (type(error).__name__, str(error)[:200])

    # ---- reading back what the user said (once a day, within the quota) ----------------------------------
    def _evolution_mine_maybe(self, config, key):
        idle = getattr(self, '_idle_work_allowed', lambda: True)
        if not idle() or getattr(self, 'processing', False):
            return
        settings = evolution.load_settings(config)
        today = evolution._now().astimezone(evolution.LOCAL_TIMEZONE).date().isoformat()
        state = evolution.state(config)
        if not settings.mining or state.get("mined_on") == today or evolution.quota(config)["stage"] != "normal":
            return
        lines = self._evolution_lines(state.get("mined_id"))
        if sum(1 for line in lines if line[1] == "user") < MINING_MIN_LINES and not any(line[0] < 0 for line in lines):
            evolution.set_state(config, mined_on=today)
            return
        self.evolution_mining = "running"

        def work():
            try:
                existing = [item for item in evolution.points(config) if item["status"] in evolution.OPEN]
                if evolution.quota(config)["stage"] != "normal" or not idle():
                    return
                import organized_memory
                organized = organized_memory.for_conversations(self.session.db_path,
                    [line[0] for line in lines if line[0] > 0])
                reply = claude_client.send(
                    claude_client.main_model(config), evolution.MINING_SYSTEM,
                    evolution.mining_prompt(lines, existing, organized), key, max_tokens=4000,
                    usage_path=Path(config) / "claude-usage.jsonl", purpose=evolution.purpose("mining"),
                    effort="medium", retry_truncated=False)
                found = evolution.parse_mining(reply, lines, {item["id"] for item in existing})
                added = 0
                for item in found:
                    if item["same_as"]:
                        target = evolution.get(config, item["same_as"])
                        if target is not None:
                            evolution.update(config, target["id"], 'Observed again in conversation',
                                             quotes=((target.get("quotes") or []) + item["quotes"])[-12:])
                        continue
                    _point, merged = evolution.add(config, item["title"], item["kind"], why=item["why"],
                                                   level=item["level"], source="history",
                                                   suggestion=item["suggestion"], quotes=item["quotes"],
                                                   deadline=item.get("deadline", ""))
                    added += 0 if merged else 1
                evolution.set_state(config, mined_on=today,
                                    mined_id=max((line[0] for line in lines if line[0] > 0), default=state.get("mined_id", 0)),
                                    mined_feedback_id=getattr(self, "_evolution_feedback_cursor", state.get("mined_feedback_id", 0)),
                                    mined_result='Added %d evolution points' % added, mining_error="")
            except Exception as error:
                evolution.set_state(config, mined_on=today, mining_error="%s: %s" % (type(error).__name__, str(error)[:120]))
            finally:
                self.evolution_mining = None
                self.events.put(("call", self._evolution_changed))
        threading.Thread(target=work, daemon=True).start()

    def _evolution_lines(self, after_id):
        """Only advance through evidence actually sent; feedback IDs use a negative namespace."""
        import evolution_sources
        config = self._evolution_config()
        lines, cursor = evolution_sources.read(
            self.session.db_path, config, after_id,
            evolution.state(config).get("mined_feedback_id", 0),
            max_chars=MINING_MAX_CHARS, first_days=MINING_FIRST_DAYS)
        self._evolution_feedback_cursor = cursor
        return lines

    # ---- for myn_status -----------------------------------------------------------------------------
    def _evolution_status(self):
        config = self._evolution_config()
        items = evolution.points(config)
        counts = {}
        for item in items:
            counts[evolution.STATUSES[item["status"]]] = counts.get(evolution.STATUSES[item["status"]], 0) + 1
        running = None
        if self.evolution_run:
            running = 'Planning: ' + str((evolution.get(config, self.evolution_run["point_id"]) or {}).get("title"))
        elif self.self_fix and self.self_fix.get("point"):
            running = 'Development: ' + str((evolution.get(config, self.self_fix["point"]) or {}).get("title"))
        return {"quota": evolution.quota(config), "points": counts, "running": running,
                "reading_history": evolution.state(config), "error": self.evolution_error}
