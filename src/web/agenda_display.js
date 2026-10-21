/* Read-only saved agenda. Dates/ranges come from the calendar tool, not prose. */
"use strict";
window.AgendaDisplay = (() => {
  const host = document.getElementById("agenda-display");
  let data = null, page = 0, signature = "";
  function node(tag, cls, text) {
    const el = document.createElement(tag);
    el.className = cls;
    if (text !== undefined) el.textContent = text;
    return el;
  }
  function close(notify = true) {
    host.hidden = true;
    document.body.classList.remove("agenda-showing");
    data = null; signature = "";
    if (notify && typeof emit === "function") emit({event: "agenda_close"});
  }
  function render() {
    host.replaceChildren();
    const bar = node("header", "agenda-bar");
    const title = data.from_date === data.to_date ? data.from_date : `${data.from_date} — ${data.to_date}`;
    bar.append(node("div", "agenda-heading", "Calendar"), node("span", "agenda-range", title));
    const exit = node("button", "agenda-close", "×");
    exit.type = "button"; exit.setAttribute("aria-label", "Collapse calendar");
    exit.onclick = () => close();
    bar.append(exit); host.append(bar);
    const days = data.days.slice(page * 7, page * 7 + 7);
    const grid = node("div", "agenda-grid" + (days.length === 1 ? " single" : ""));
    days.forEach((day, index) => {
      const card = node("article", "agenda-day");
      card.style.setProperty("--day-index", index);
      card.append(node("h3", "", day.weekday), node("div", "agenda-date", day.label));
      if (!day.events.length) card.append(node("p", "agenda-empty", "No events"));
      day.events.forEach(event => {
        const item = node("div", "agenda-event");
        item.append(node("div", "agenda-time", event.time), node("div", "agenda-title", event.title));
        if (event.location) item.append(node("div", "agenda-location", event.location));
        if (event.note) item.append(node("div", "agenda-note", event.note));
        card.append(item);
      });
      grid.append(card);
    });
    host.append(grid);
    if (data.days.length > 7) {
      const nav = node("nav", "agenda-nav");
      for (const [label, next] of [["Previous", page - 1], ["Next", page + 1]]) {
        const button = node("button", "", label);
        button.disabled = next < 0 || next * 7 >= data.days.length;
        button.onclick = () => { page = next; render(); };
        nav.append(button);
      }
      host.append(nav);
    }
  }
  function show(payload) {
    const next = JSON.stringify(payload);
    if (next === signature && !host.hidden) return; // Don't replay on every answer refresh.
    const sameRange = data && data.from_date === payload.from_date && data.to_date === payload.to_date;
    data = payload; signature = next;
    if (!sameRange) page = 0;
    render(); host.hidden = false;
    document.body.classList.add("agenda-showing");
  }
  host.addEventListener("wheel", e => e.stopPropagation(), {passive: true});
  host.addEventListener("pointerdown", e => e.stopPropagation());
  document.addEventListener("keydown", e => {
    if (e.key === "Escape" && !host.hidden) { close(); e.stopImmediatePropagation(); }
  }, true);
  return {show, close};
})();
