"""One Backup screen: complete transfers, personal history and learned preferences."""
from web_panels3 import PreferencesPanel
from web_panels2 import LibraryPanel


def _prefix(nodes):
    if isinstance(nodes, list):
        return [_prefix(n) for n in nodes]
    if isinstance(nodes, dict):
        return {k: 'library_' + v if k == 'action' else _prefix(v) for k, v in nodes.items()}
    return nodes


class BackupPanel(PreferencesPanel):
    key = 'personal'
    title = 'Backup'

    def __init__(self, win, engine):
        super().__init__(win, engine)
        self.library = LibraryPanel(win)
        self.library.redraw = self.redraw

    def body(self):
        content = super().body()
        self.library.host = self.host
        self.library.values = self.values
        library = [
            {'t': 'p', 'text': 'Full backups include conversations, memories, communication preferences, settings, expenses, calendar, alarms, music, lyrics and encrypted API keys.'},
            {'t': 'buttons', 'items': [
                {'action': 'complete_export', 'label': 'Export full backup', 'kind': 'primary'},
                {'action': 'complete_import', 'label': 'Import full backup'},
                {'action': 'kept_folder', 'label': 'Data preserved before restore'}]},
            {'t': 'p', 'tone': 'muted', 'text': 'On another computer, install MYN and required speech components, import the .mynbackup file and enter its password. External work files and hardware are not included.'},
        ] + _prefix(self.library.body())
        tabs = next((n for n in content if n.get('t') == 'tabs'), None)
        if tabs is None:
            return library + content
        tabs['tabs'].insert(0, {'key': 'complete', 'label': 'Backup and restore', 'children': library})
        for tab in tabs['tabs']:
            if tab['key'] == 'rules':
                tab['children'] = content[:-1] + tab['children']
            if tab['key'] == 'data':
                tab['label'] = 'Import/export preferences'
        return [tabs]

    def act(self, action, values):
        import backup_ui
        if action in ('complete_export', 'complete_import'):
            backup_ui.transfer(self.win, importing=action == 'complete_import')
        elif action == 'kept_folder':
            import os
            import portable_backup
            from data_location import root_for_db
            os.startfile(str(portable_backup.transfer_dir(root_for_db(self.win.session.db_path))))
        elif action.startswith('library_'):
            self.library.host = self.host
            self.library.act(action[len('library_'):], values)
            self.redraw()
        else:
            super().act(action, values)
