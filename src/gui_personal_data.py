"""On-demand personal data reader and one-shot backup dialogs; no settings page."""
from pathlib import Path
import json
import os
from tkinter import filedialog, messagebox

import personal_data_tools


class PersonalDataMixin:
    def _personal_data_boot(self):
        self.session.personal_job = self._queue_personal_job

    def _open_personal_view(self, collection):
        if self.session is None:
            return
        try:
            value = personal_data_tools.read(self.session,collection)
            text = personal_data_tools.display(collection,value)
            import message_log
            if message_log.is_sensitive(text):
                raise ValueError('The content may contain sensitive credentials. Narrow the query.')
            self._report_display(text)
        except Exception as error:
            self._show_error('Personal data',str(error))

    def _queue_personal_job(self, action, payload):
        self.events.put(('call',lambda:self._personal_job_when_idle(action,payload)))

    def _personal_job_when_idle(self, action, payload):
        import backup_ui
        if backup_ui.busy(self):
            self.root.after(500,lambda:self._personal_job_when_idle(action,payload))
            return
        import backup
        import personal_profile
        import portable_backup
        from data_location import root_for_db
        db = self.session.db_path
        store = self.session.preferences.store
        try:
            if action in ('complete_export','complete_import'):
                backup_ui.transfer(self,importing=action=='complete_import')
                return
            if action=='understand':
                self.process_pending()
                text='Requested processing of pending conversations.'
            elif action=='reanalyse':
                self.reanalyse_history(self.root)
                return
            elif action=='database_backup':
                path=backup.backup_database(db,label='manual')
                store.backup('manual')
                text='Conversation and preference backup saved: '+str(path)
            elif action=='history_export':
                text='History exported: '+str(personal_profile.export_profile(db))
            elif action=='preferences_export': text='Preferences exported: '+str(store.export())
            elif action=='preferences_backup': text='Preferences backed up: '+str(store.backup('manual'))
            elif action in ('database_restore','preferences_restore','preferences_import'):
                folder = (Path(personal_profile.list_backups(db)[0]).parent if personal_profile.list_backups(db)
                          else root_for_db(db)/'backups') if action=='database_restore' else store.folder/(
                              'exports' if action=='preferences_import' else 'backups')
                chosen = payload.get('file')
                if chosen:
                    if Path(chosen).name != chosen or chosen in ('.','..'):
                        raise ValueError('Choose a filename from the backup list')
                    selected=(folder/chosen).resolve(strict=True)
                    if selected.parent != folder.resolve():
                        raise ValueError('Invalid backup path')
                else:
                    selected=filedialog.askopenfilename(parent=self.root,initialdir=str(folder),
                        title='Choose a preference export' if action=='preferences_import' else 'Choose backup',
                        filetypes=[('JSON','*.json')] if action=='preferences_import' else [('Database backup','*.db')])
                    if not selected: return
                if not messagebox.askyesno('Confirm import or restore','The following file will be used:\n%s\nYour current data will be backed up first. Continue?' % selected,
                                           parent=self.root,default=messagebox.NO):
                    return
                if backup_ui.busy(self):
                    raise ValueError('A new task started during confirmation. Wait for it to finish before restoring.')
                if action=='database_restore':
                    kept=personal_profile.restore_backup(db,selected)
                    self._reconnect_session()
                    text='Database restored; previous copy: '+str(kept)
                elif action=='preferences_restore':
                    text='Preferences restored; previous copy: '+str(store.restore(selected))
                else:
                    text='Preference merge completed: '+json.dumps(store.import_file(selected),ensure_ascii=False)
            elif action in ('open_folder','kept_folder'):
                folder=root_for_db(db) if action=='open_folder' else portable_backup.transfer_dir(root_for_db(db))
                os.startfile(str(folder))
                text='Data folder opened.'
            else:
                raise ValueError('Unknown data operation')
            self._report_display(text)
        except Exception as error:
            self._report_display('Data operation incomplete: '+str(error))
