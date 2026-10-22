"Settings rendered inside MYN's main view.\n\nA WebView2 Panel describes a screen using nodes understood by web/app.js and receives sheet events. SheetHost sends descriptions, routes actions and provides inline confirmations, forms and notices. No separate window is opened. Tk windows provide the fallback.\n"

import itertools
import threading
import traceback


class Panel:
    """One settings screen. Subclasses set key, title (and width) and implement body() and
    act(action, values); self.host is the SheetHost, self.win the MYN window."""

    key = "panel"
    title = ""
    width = 720
    back_panel = None

    def __init__(self, win):
        self.win = win
        self.host = None
        self.values = {}

    def v(self, name, default=None):
        """What you last entered in a field (kept across redraws)."""
        value = self.values.get(name)
        return default if value is None else value

    def body(self):
        return []

    def act(self, action, values):
        pass

    def modal_act(self, action, values):
        """An action from this panel's small form (modal)."""
        self.act(action, values)

    def closed(self):
        pass

    # helpers for subclasses
    def redraw(self):
        if self.host is not None and self.host.panel is self:
            self.host.draw()

    def toast(self, text, tone=None):
        if self.host is not None:
            self.host.toast(text, tone)

    def confirm(self, text, then, title=None, yes='OK', no='No', danger=False):
        if self.host is not None:
            self.host.confirm(text, then, title=title or self.title, yes=yes, no=no, danger=danger)

    def modal(self, title, body, width=460):
        if self.host is not None:
            self.host.modal(title, body, width)

    def close_modal(self):
        if self.host is not None:
            self.host.close_modal()


class SheetHost:
    def __init__(self, send, schedule=None):
        """send(cmd) passes a command to the page; schedule(ms, fn) runs fn later on the main
        thread (for confirmations asked from other threads)."""
        self.send = send
        self.schedule = schedule
        self.panel = None
        self._ids = itertools.count(1)
        self._answers = {}
        self.last_error = ""

    # ---- panels -----------------------------------------------------------------------------
    def open(self, panel):
        from settings_navigation import AGENTS_SECTIONS
        if self.panel is not None and panel.key in {key for key, _, _ in AGENTS_SECTIONS}:
            panel.back_panel = self.panel if self.panel.key == "agents" else self.panel.back_panel
        if self.panel is not None and self.panel is not panel:
            self._closed()
        panel.host = self
        self.panel = panel
        self.draw()
        return panel

    def draw(self):
        panel = self.panel
        if panel is None:
            return
        try:
            body = panel.body()
        except Exception as error:  # a broken panel must not take MYN down
            self.last_error = traceback.format_exc(limit=4)
            body = [{"t": "p", "tone": "bad", "text": 'This settings page encountered an error: %s' % (error or type(error).__name__)}]
        if panel.back_panel is not None:
            body = [{"t": "buttons", "items": [
                {"action": "panel_back", "label": 'Back to Agents'}]}] + body
        self.send({"cmd": "sheet", "spec": {"key": panel.key, "title": panel.title, "width": panel.width,
                                             "body": body}})

    def close(self):
        if self.panel is not None:
            self.send({"cmd": "sheet_close"})
            self._closed()

    def _closed(self):
        panel, self.panel = self.panel, None
        if panel is not None:
            try:
                panel.closed()
            except Exception:
                pass

    def is_open(self, key=None):
        return self.panel is not None and (key is None or self.panel.key == key)

    # ---- what the page reports ------------------------------------------------------------------
    def event(self, message):
        """A page event about panels; True if it was one."""
        kind = message.get("event")
        if kind == "sheet_closed":
            if self.panel is not None and message.get("key") == self.panel.key:
                self._closed()
            return True
        if kind == "sheet_answer":
            then = self._answers.pop(message.get("id"), None)
            if then is not None:
                then(bool(message.get("yes")))
            return True
        if kind != "sheet":
            return False
        panel = self.panel
        if panel is None or message.get("key") != panel.key:
            return True
        values = message.get("values") if isinstance(message.get("values"), dict) else {}
        panel.values.update(values)
        action = str(message.get("action") or "")
        try:
            if message.get("from") == "modal":
                if action == "modal_cancel":
                    return True
                panel.modal_act(action, values)
            else:
                if action == "panel_back" and panel.back_panel is not None:
                    self.open(panel.back_panel)
                else:
                    panel.act(action, values)
        except Exception as error:
            self.last_error = traceback.format_exc(limit=4)
            self.toast('An error occurred: %s' % (str(error) or type(error).__name__), "bad")
        return True

    # ---- in-panel feedback ------------------------------------------------------------------------
    def toast(self, text, tone=None):
        self.send({"cmd": "sheet_toast", "text": text, "tone": tone})

    def confirm(self, text, then, title="MYN", yes='OK', no='No', danger=False):
        """Ask inside the page; then(bool) is called with the answer (no = the default)."""
        ident = next(self._ids)
        self._answers[ident] = then
        self.send({"cmd": "sheet_confirm", "id": ident, "title": title, "text": text, "yes": yes, "no": no,
                   "danger": danger})
        return ident

    def notice(self, title, body, closed):
        """One dismissible, read-only receipt; no approval or action is requested."""
        ident = next(self._ids)
        self._answers[ident] = lambda _value: closed()
        self.send({"cmd": "sheet_confirm", "id": ident, "title": title,
                   "body": body, "notice": True, "yes": "Close", "width": 760})
        return ident

    def ask_and_wait(self, text, title='MYN needs your approval', yes='Allow', no='Decline', timeout=None):
        """From another thread: ask in the page and wait for the answer (False if none)."""
        done = threading.Event()
        answer = {"yes": False}

        def then(value):
            answer["yes"] = value
            done.set()

        self.schedule(0, lambda: self.confirm(text, then, title=title, yes=yes, no=no))
        done.wait(timeout)
        return answer["yes"]

    def modal(self, title, body, width=460):
        self.send({"cmd": "sheet_modal", "spec": {"title": title, "body": body, "width": width}})

    def close_modal(self):
        self.send({"cmd": "sheet_modal_close"})


def options(pairs):
    return [[str(value), str(label)] for value, label in pairs]
