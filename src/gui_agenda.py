"""Conversation-requested calendar cards on the main window."""
import textwrap

import agenda_display
from gui_common import config_for_db


class AgendaMixin:
    _agenda_payload = None
    _agenda_offset = 0
    _agenda_revision = 0
    _agenda_close_job = None
    _agenda_close_token = None

    def _agenda_context(self):
        payload = self._agenda_payload
        return {"open": bool(payload), "revision": self._agenda_revision,
                "from_date": payload["from_date"] if payload else None,
                "to_date": payload["to_date"] if payload else None}

    def _apply_agenda_scene(self, decision):
        if "input_revision" in decision:
            import interaction
            if interaction.load(self._usage_config())["revision"] != decision["input_revision"]:
                return
        if decision.get("close") and decision.get("revision") == self._agenda_revision:
            self._close_agenda_display()

    def _show_agenda_display(self, payload):
        self._cancel_agenda_auto_close()
        self._agenda_revision += 1
        self._agenda_payload = payload
        self._agenda_offset = 0
        self._draw_agenda_display()
        token = self._agenda_close_token = object()

        def expire():
            if self._agenda_close_token is token:
                self._agenda_close_job = None
                self._close_agenda_display()

        self._agenda_close_job = self.root.after(120_000, expire)

    def _cancel_agenda_auto_close(self):
        self._agenda_close_token = None
        if self._agenda_close_job is not None:
            self.root.after_cancel(self._agenda_close_job)
            self._agenda_close_job = None

    def _close_agenda_display(self):
        self._cancel_agenda_auto_close()
        self._agenda_revision += 1
        self._agenda_payload = None
        self.core_canvas.delete("agenda_display")
        if self.web_active:
            self.web.js({"cmd": "agenda_close"})

    def _agenda_changed(self):
        """Refresh an already requested date range after a chat edit."""
        if not self._agenda_payload or self.session is None:
            return
        old = self._agenda_payload
        try:
            self._agenda_payload = agenda_display.snapshot(
                config_for_db(self.session.db_path), old["from_date"], old["to_date"])
            if old != self._agenda_payload:
                self._agenda_revision += 1
        except (OSError, ValueError) as error:
            # Never leave stale cards looking like a successful read.
            self._close_agenda_display()
            self.append('Error', 'Could not read the calendar: ' + str(error))
            return
        self._draw_agenda_display()

    def _draw_agenda_display(self):
        payload = self._agenda_payload
        if not payload:
            return
        if self.web_active:
            self.web.js(payload)
            return
        # Classic fallback: text drawn on the main canvas with pagination.
        canvas = self.core_canvas
        canvas.delete("agenda_display")
        width, height = canvas.winfo_width(), canvas.winfo_height()
        x = max(22, width * .35)
        lines = []
        for day in payload["days"]:
            lines.append(day["weekday"] + "  " + day["label"])
            if not day["events"]:
                lines.append('  No events')
            for event in day["events"]:
                text = "%s  %s%s" % (event["time"], event["title"],
                                     " · " + event["location"] if event["location"] else "")
                lines.extend("  " + line for line in textwrap.wrap(text, max(15, int((width-x-35)/14))))
                if event["note"]:
                    lines.extend("  " + line for line in textwrap.wrap(event["note"], max(15, int((width-x-35)/14))))
            lines.append("")
        page = max(3, int((height - 220) / 24))
        self._agenda_offset = min(self._agenda_offset, max(0, len(lines)-page))
        for index, line in enumerate(lines[self._agenda_offset:self._agenda_offset+page]):
            canvas.create_text(x, 115 + index*24, anchor="nw", text=line, fill="#e5f8fc",
                               font=(self.font_family, 11), tags="agenda_display")
        for label, offset, callback in (
                ("↑", 0, lambda _e: self._scroll_agenda(-page)),
                ("↓", 45, lambda _e: self._scroll_agenda(page)),
                ('Collapse calendar', 95, lambda _e: self._close_agenda_display())):
            item = canvas.create_text(x+offset, 85, anchor="nw", text=label, fill="#8de8fa",
                                      font=(self.font_family, 11), tags="agenda_display")
            canvas.tag_bind(item, "<Button-1>", callback)
        canvas.tag_raise("agenda_display")

    def _scroll_agenda(self, amount):
        self._agenda_offset = max(0, self._agenda_offset+amount)
        self._draw_agenda_display()
