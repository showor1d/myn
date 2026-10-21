"""Quiet report preparation and invitations, using the existing conversation/reader."""
import threading
import time

import reporting
import interaction


class ReportsMixin:
    _report_working = False
    _report_text_waiting = None

    def _reports_boot(self):
        self.session.show_report = lambda text: self.events.put(('call',lambda: self._report_display(text)))
        self.root.after(12000,self._reports_tick)

    def _report_display(self, text):
        # The reply can still be streaming. Present details only when it has finished,
        # so an ordinary chat render cannot replace the report while opening it.
        self._report_text_waiting = text
        self.root.after(200,self._report_flush)

    def _report_flush(self):
        if not self._report_text_waiting:
            return
        if self.busy or self.draft_active:
            self.root.after(200,self._report_flush)
            return
        text, self._report_text_waiting = self._report_text_waiting,None
        if self.web_active and self.web is not None:
            self.web.js({'cmd':'reader','action':'report','text':text,'key':'report-' + str(time.time_ns())})
        else:
            self.append('MYN',text)

    def _reports_tick(self):
        self.root.after(15000,self._reports_tick)
        if self.session is None or getattr(self,'backup_working',False):
            return
        config = self._evolution_config()
        if not self._report_working:
            self._report_working = True
            def prepare():
                try:
                    reporting.due(config,self.session.db_path)
                    self.report_error = ''
                except Exception as error:
                    self.report_error = '%s: %s' % (type(error).__name__,str(error)[:150])
                finally:
                    self._report_working = False
            threading.Thread(target=prepare,daemon=True).start()
        self._report_announce(config)

    def _report_announce(self, config):
        if not self._evolution_idle() or not self._speech_allowed('proactive'):
            return
        try:
            invitation = interaction.load(config).get('invitation')
            if invitation and time.time()-invitation.get('at',0) < 300:
                return  # don't ask two different acceptance questions together
            report = reporting.offer(config)
        except Exception as error:
            self.report_error = str(error)[:150]
            return
        if not report:
            return
        spoken = 'The report on "%s" is ready. Is now a good time to view it?' % report['title'][:45]
        self.append('MYN',spoken,speakable=True,spoken_text=spoken)
        self.speak(spoken,purpose='proactive')
        self.conversation_open = True
        self.conversation_touch = self.last_answer_at = time.time()
        self._evolution_said_at = time.time()
