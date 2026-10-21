/* Read-only timer animation. The Python scheduler alone decides when to remind. */
(() => {
  const root = document.getElementById("timer-hud");
  let items = [], count = 0, syncedAt = 0, notice = "", noticeUntil = 0, lastText = "";
  let alarmUpcoming = false;
  let hoverTimer = null, travelTimer = null, alarmRows = [], alarmSignature = "";
  const pending = new Map();
  const dock = document.createElement("div");
  dock.id = "status-dock";
  let coreGeometry = null;
  function layoutDock(geometry) {
    if (geometry) coreGeometry = geometry;
    const {cx, cy, radius} = coreGeometry || {
      cx: innerWidth / 2, cy: innerHeight / 2 - 3,
      radius: Math.min(innerWidth * .21, innerHeight * .27, 158)
    };
    // Stay outside the membrane, without following its breathing or rotation.
    const ry = radius * 1.22 + 42, rx = Math.min(ry, cx - 30);
    const entries = Array.from(dock.children);
    entries.forEach((node, i) => {
      const angle = (-36 + 72 * i / Math.max(1, entries.length - 1)) * Math.PI / 180;
      const left = `${(cx - rx * Math.cos(angle) - 14).toFixed(2)}px`;
      const top = `${(cy + ry * Math.sin(angle) - 17).toFixed(2)}px`;
      if (node.style.left !== left) node.style.left = left;
      if (node.style.top !== top) node.style.top = top;
    });
    positionBox();
    window.MediaPlayer?.positionMini();
  }
  function placePopup(panel, anchor, preferredWidth) {
    if (panel.hidden) return;
    const r = anchor.getBoundingClientRect();
    const available = r.left - 20;
    panel.style.boxSizing = "border-box";
    panel.style.width = Math.min(preferredWidth, available >= 180 ? available : innerWidth - 16) + "px";
    panel.style.maxHeight = (innerHeight - 16) + "px";
    panel.style.overflowY = "auto";
    panel.style.overflowX = "hidden";
    panel.scrollLeft = 0;
    let left = r.left - panel.offsetWidth - 10, top = r.top;
    if (available < 180) {
      // In a narrow window, use the outer upper/lower corner instead of covering the core.
      const g = coreGeometry || {cy:innerHeight / 2 - 3, radius:Math.min(innerWidth * .21, innerHeight * .27, 158)};
      left = 8;
      top = r.top < g.cy ? g.cy - g.radius * 1.22 - panel.offsetHeight - 12 : g.cy + g.radius * 1.22 + 12;
    }
    panel.style.left = Math.max(8, left) + "px";
    panel.style.top = Math.max(8, Math.min(top, innerHeight - panel.offsetHeight - 8)) + "px";
  }
  const agents = document.createElement("button");
  agents.id = "agents-shortcut"; agents.type = "button";
  agents.title = "Agents"; agents.setAttribute("aria-label", "Agents");
  agents.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true">' +
    '<rect x="4" y="6" width="16" height="14" rx="4"/>' +
    '<path d="M12 6V3M10 2h4M1 11v5M23 11v5M8 15h8"/>' +
    '<circle cx="8" cy="11" r="1"/><circle cx="16" cy="11" r="1"/></svg>';
  agents.onclick = () => {
    expandAlarm(false); window.MediaPlayer?.closeMini();
    report({event:"menu", item:"agents"});
  };
  const expenses = document.createElement("button");
  expenses.id = "expenses-shortcut"; expenses.type = "button";
  expenses.title = "Expenses";
  expenses.setAttribute("aria-label", "Expenses");
  expenses.innerHTML = '<svg viewBox="0 0 28 28" aria-hidden="true">' +
    '<path d="M3 18v3l21-2v-3M5 21v3l21-2v-3"/>' +
    '<rect x="2" y="5" width="23" height="13" rx="2"/>' +
    '<path d="M6 8H5v7h1M21 8h1v7h-1M13.5 7v9M16 9c-1-1.5-5-1.5-5 .5s5 1.5 5 3.5-4 2-5 .5"/></svg>';
  expenses.onclick = () => {
    expandAlarm(false); window.MediaPlayer?.closeMini();
    report({event:"menu", item:"usage"});
  };
  const equalizer = document.createElement("button");
  equalizer.id = "equalizer-shortcut"; equalizer.type = "button";
  equalizer.title = "Equalizer"; equalizer.setAttribute("aria-label", "Equalizer");
  equalizer.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 3v5m0 4v9M12 3v11m0 4v3M19 3v3m0 4v11"/><rect x="2" y="8" width="6" height="4" rx="2"/><rect x="9" y="14" width="6" height="4" rx="2"/><rect x="16" y="6" width="6" height="4" rx="2"/></svg>';
  equalizer.onclick = () => {expandAlarm(false);window.MediaPlayer?.closeMini();window.AudioConsole?.open();};
  const keyboard = document.createElement("button");
  keyboard.id = "keyboard-shortcut"; keyboard.type = "button";
  keyboard.setAttribute("aria-label", "Open input bar");
  keyboard.setAttribute("aria-pressed", "false");
  keyboard.setAttribute("aria-controls", "bar");
  keyboard.title = "Open input bar";
  keyboard.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="2" y="5" width="20" height="14" rx="2"/>' +
    '<path d="M6 9h.01M10 9h.01M14 9h.01M18 9h.01M6 12h.01M10 12h.01M14 12h.01M18 12h.01M7 16h10"/></svg>';
  keyboard.onclick = () => { expandAlarm(false); window.MediaPlayer?.closeMini(); report({event:"toggle_input"}); };
  const icon = document.createElement("button");
  icon.type = "button"; icon.className = "alarm-indicator";
  icon.setAttribute("aria-label", "Alarm scheduled within the next 24 hours");
  icon.setAttribute("aria-expanded", "false");
  icon.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true">' +
    '<path d="M18 8a6 6 0 0 0-12 0c0 7-3 7-3 9h18c0-2-3-2-3-9Z"/>' +
    '<path d="M10 21h4M12 2V1"/></svg>';
  const box = document.createElement("section");
  box.id = "alarm-mini-box"; box.hidden = true;
  box.setAttribute("aria-label", "Alarm");
  box.innerHTML = '<strong>Alarm</strong><div class="alarm-mini-list"></div>' +
    '<p id="alarm-default-sound">Default alarm sound: loading</p>' +
    '<div class="alarm-volume-controls"><label for="alarm-volume">Alarm volume</label>' +
    '<input id="alarm-volume" type="range" min="0" max="1" step="0.01" value="0.8" disabled>' +
    '<output id="alarm-volume-value" for="alarm-volume">80%</output></div>' +
    '<p class="alarm-mini-error" role="status"></p>';
  document.body.appendChild(box);
  const alarmVolume = box.querySelector('#alarm-volume');
  function volumeLabel() {
    box.querySelector('#alarm-volume-value').textContent = Math.round(Number(alarmVolume.value)*100) + '%';
    alarmVolume.style.setProperty('--fill', (Number(alarmVolume.value)*100)+'%');
  }
  alarmVolume.oninput = volumeLabel;
  alarmVolume.onchange = async () => {
    try {
      await window.MediaPlayer.saveAlarmSettings({volume:Number(alarmVolume.value)});
      box.querySelector('.alarm-mini-error').textContent = '';
    } catch(e) {
      box.querySelector('.alarm-mini-error').textContent = String(e.message || e);
      window.MediaPlayer.loadAlarmSettings();
    }
  };
  const report = message => window.pywebview?.api?.emit?.(message);
  function positionBox() {
    placePopup(box, icon, 310);
  }
  function expandAlarm(open) {
    clearTimeout(hoverTimer); clearTimeout(travelTimer);
    if (open && document.body.classList.contains("media-open")) return;
    if (open) window.MediaPlayer?.closeMini();
    const changed = box.hidden === open;
    box.hidden = !open; icon.setAttribute("aria-expanded", String(open));
    if (changed) report({event: open ? "alarm_mini_open" : "alarm_mini_close"});
    if (changed && open) window.MediaPlayer?.loadAlarmSettings();
    if (!open && !alarmUpcoming) render();
    positionBox();
  }
  icon.onpointerenter = () => {
    window.MediaPlayer?.closeMini();
    clearTimeout(hoverTimer); clearTimeout(travelTimer);
    if (box.hidden) hoverTimer = setTimeout(() => expandAlarm(true), 300);
  };
  icon.onpointerleave = () => {
    clearTimeout(hoverTimer);
    if (!box.hidden) travelTimer = setTimeout(() => expandAlarm(false), 1000);
  };
  icon.onclick = () => expandAlarm(box.hidden);
  box.onpointerenter = () => clearTimeout(travelTimer);
  box.onpointerleave = () => expandAlarm(false);
  document.addEventListener("pointerdown", e => {
    if (!box.contains(e.target) && !icon.contains(e.target)) expandAlarm(false);
  });
  document.addEventListener("keydown", e => { if (e.key === "Escape") expandAlarm(false); });
  function renderAlarms() {
    const list = box.querySelector(".alarm-mini-list");
    list.textContent = "";
    for (const alarm of alarmRows) {
      const row = document.createElement("div"); row.className = "alarm-mini-row";
      const info = document.createElement("div");
      const title = document.createElement("div"); title.textContent = alarm.name;
      const when = document.createElement("small");
      when.textContent = alarm.when + (alarm.ringtone_name ? ' · ' + alarm.ringtone_name : '');
      const next = document.createElement("small");
      next.textContent = alarm.enabled ? `Next ${alarm.next_run || "Awaiting execution"}` :
        alarm.finished || alarm.status === "cancelled" ? "Finished" : "Disabled";
      info.append(title, when, next);
      const toggle = document.createElement("button");
      toggle.type = "button"; toggle.className = "alarm-mini-toggle";
      toggle.setAttribute("role", "switch");
      toggle.setAttribute("aria-checked", String(!!alarm.enabled));
      toggle.setAttribute("aria-label", `${alarm.enabled ? "Close" : "Enabled"} ${alarm.name} (entire alarm)`);
      toggle.title = "Pause or resume the entire alarm; ask MYN to skip a single occurrence";
      toggle.disabled = pending.has(alarm.id) || !!alarm.finished || alarm.status === "cancelled";
      toggle.onclick = () => {
        const request = `${Date.now()}-${Math.random()}`;
        const timer = setTimeout(() => {
          if (pending.get(alarm.id)?.request !== request) return;
          pending.delete(alarm.id);
          box.querySelector(".alarm-mini-error").textContent = "Save result not received. Reopen to verify.";
          renderAlarms();
        }, 10000);
        pending.set(alarm.id, {request, timer});
        toggle.disabled = true;
        report({event:"alarm_toggle", id:alarm.id, enabled:!alarm.enabled, request});
      };
      row.append(info, toggle); list.appendChild(row);
    }
    if (!alarmRows.length) list.textContent = "No alarms";
    positionBox();
  }
  renderAlarms();
  const nodes = new Map();
  function clock(value) {
    const seconds = Math.max(0, Math.ceil(value));
    const days = Math.floor(seconds / 86400);
    const h = Math.floor(seconds / 3600) % 24, m = Math.floor(seconds / 60) % 60, s = seconds % 60;
    const hh = days || h ? `${String(h).padStart(2, "0")}:` : "";
    return `${days ? days + " days " : ""}${hh}${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
  }
  function tick() {
    if (!root.children.length) render();
    const now = performance.now(), elapsed = (now - syncedAt) / 1000;
    if (notice && now >= noticeUntil) notice = "";
    root.hidden = false; // Expenses and keyboard remain available even with no upcoming alarm.
    root.style.top = "54px";
    layoutDock();
    if (document.body.classList.contains("media-open") && !box.hidden) expandAlarm(false);
    positionBox();
    for (const item of items) {
      const row = nodes.get(item.id);
      const left = Math.max(0, item.remaining - (item.paused ? 0 : elapsed));
      row.classList.toggle("paused", item.paused);
      row.classList.toggle("scheduled", item.kind === "scheduled");
      row.querySelector(".timer-value").textContent = (item.paused ? "Paused: " : "") + clock(left);
      const progress = item.total > 0 ? Math.min(1, left / item.total) : .24;
      row.style.setProperty("--timer-progress", String(progress));
      row.querySelector(".timer-subtitle").textContent = item.when || (item.paused ? "Paused" : left > 0 ? "Running" : "Awaiting spoken reminder");
    }
    const banner = root.querySelector(".timer-finished");
    if (banner && lastText !== notice) {
      banner.textContent = notice ? "Time is up · " + notice : "";
      banner.hidden = !notice;
      lastText = notice;
    }
    window.MediaPlayer?.positionMini();
  }
  function render() {
    root.textContent = "";
    dock.textContent = "";
    root.appendChild(dock);
    dock.appendChild(agents);
    nodes.clear();
    if (alarmUpcoming || !box.hidden) {
      dock.appendChild(icon);
    }
    dock.appendChild(expenses);
    const mediaAnchor = document.createElement("div");
    mediaAnchor.id = "media-mini-anchor";
    dock.appendChild(mediaAnchor);
    dock.appendChild(equalizer);
    dock.appendChild(keyboard);
    for (const item of items) {
      const row = document.createElement("div");
      row.className = "timer-chip";
      // Only static markup; model/user labels are assigned through textContent.
      row.innerHTML = '<div class="timer-orbit"><svg viewBox="0 0 44 44" aria-hidden="true">' +
        '<circle class="timer-track" cx="22" cy="22" r="18"/><circle class="timer-arc" cx="22" cy="22" r="18" pathLength="1"/>' +
        '</svg><span class="timer-dot"></span></div><div class="timer-info">' +
        '<div class="timer-label"></div><div class="timer-value"></div><div class="timer-subtitle"></div></div>';
      row.querySelector(".timer-label").textContent = item.label;
      root.appendChild(row);
      nodes.set(item.id, row);
    }
    if (count > items.length) {
      const more = document.createElement("div");
      more.className = "timer-more";
      more.textContent = `${count - items.length} more timers · Ask for details`;
      root.appendChild(more);
    }
    const banner = document.createElement("div");
    banner.className = "timer-finished"; banner.hidden = !notice; banner.textContent = notice ? "Time is up · " + notice : "";
    root.appendChild(banner); lastText = notice;
  }
  window.TimerHUD = {
    layoutDock, placePopup,
    setInput(visible) {
      keyboard.setAttribute("aria-pressed", String(visible));
      keyboard.setAttribute("aria-label", visible ? "Hide input bar" : "Open input bar");
      keyboard.title = visible ? "Hide input bar" : "Open input bar";
    },
    closeAlarm: () => expandAlarm(false),
    alarmSettingsError(error) { box.querySelector('.alarm-mini-error').textContent = error; },
    alarmSettings(data) {
      box.querySelector('#alarm-default-sound').textContent = 'Default alarm sound: ' + (data.ringtone_name || 'Spoken reminder');
      alarmVolume.value = data.volume; alarmVolume.disabled = false; volumeLabel();
    },
    alarms(data) {
      for (const [id, entry] of pending) {
        if (entry.request === data.request) {
          clearTimeout(entry.timer); pending.delete(id);
        }
      }
      box.querySelector(".alarm-mini-error").textContent = String(data.error || "");
      if (Array.isArray(data.alarms)) {
        const signature = JSON.stringify(data.alarms);
        alarmRows = data.alarms;
        if (signature !== alarmSignature || data.request) {
          alarmSignature = signature; renderAlarms();
        }
      }
    },
    update(data) {
      const next = (data.items || []).filter(i => i && typeof i.id === "string" &&
        Number.isFinite(i.remaining) && Number.isFinite(i.total)).slice(0, 3);
      const nextAlarm = data.alarm_upcoming === true;
      const changed = JSON.stringify(next.map(i => [i.id, i.label])) !== JSON.stringify(items.map(i => [i.id, i.label])) ||
        count !== data.count || alarmUpcoming !== nextAlarm;
      items = next; count = data.count || 0; syncedAt = performance.now();
      alarmUpcoming = nextAlarm;
      if (changed || !root.children.length) render();
      tick();
    },
    finish(text) {
      notice = String(text || "Timer finished").slice(0, 80); noticeUntil = performance.now() + 4000;
      if (!root.children.length) render();
      tick();
    }
  };
  tick();
  setInterval(tick, 200);
})();
