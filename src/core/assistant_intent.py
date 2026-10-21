"""Natural requests share model tools; no phrase-to-action lookup here."""
import json

NOTE = """
Interpret the user's intended outcome from the whole current utterance and recent
conversation, not from command keywords, exact phrases or a list of examples.
A short new request can change topic. A request such as 'play a song', 'some music please',
'play anything', 'put something on' in a music context means use music_control(play).
These examples are not an allowlist. Understand other Chinese, English and Japanese
paraphrases. Do not answer a request to perform an available action with instructions
for the user to do it themselves. Use the supplied tool and report its actual result.
Opening the music page and starting playback are different actions. A generic request
to hear music starts MYN's local shuffled music without asking for a song name. Use
music_control for MYN playback; OS media keys cannot reliably start MYN music.
Interpret short answers and pronouns using the latest applicable context; do not use
an unrelated old topic. Missing time, ambiguous targets or uncertain transcription
require one short specific clarification, not silent failure or an invented action.
For voice input, allow plausible ASR homophones in context (for example 放手歌 may
mean 放首歌). Do not rewrite the saved original or invent dates, amounts, file names,
deletion targets or permissions from a guess. Quotes, negatives, past events,
hypotheticals and requests to explain a command are not instructions to execute it.
Never say an action completed before a successful tool receipt. A receipt for opening
a window is not a playback receipt; no receipt means no action was performed.
For calendar, alarm, timer, expenses, evolution, task budgets, microphone, backups,
preferences and memory requests use their supplied tools regardless of wording.
Removed settings pages stay removed: their functions are available through tools.
"""

VOICE_NOTE = """
The utterance is speech recognition output, not a perfect transcript. Understand
plausible homophones, omissions and short colloquial requests using the conversation.
Do not require the exact feature name or action verb. A new assistant request can
change topic. When clearly addressed but the meaning is uncertain, allow MYN to ask
one brief clarification; uncertainty about content is not proof it was for someone
else. When the addressee itself is unclear, or speech is from another person/media,
stay silent. Never use guessed content as consent for risky actions.
"""

ACTIONS = ('agents', 'expenses', 'player', 'equalizer', 'input_open', 'input_close',
           'reader_open', 'reader_close', 'read_all', 'stop_speaking')
TOOL = dict(name='interface_control', description=(
    "Open or close MYN's own interface from a natural user request. agents contains voice "
    "and API settings; expenses shows spending; player only opens the media library, "
    "use music_control to play. equalizer opens the independent music/microphone audio console. "
    "input_open/input_close shows/hides typing. reader_open/"
    "reader_close shows/hides the reading panel. read_all reads the latest full reply. "
    "stop_speaking stops MYN's speech, not music. "
    "Never open deleted memory/backup/preferences/evolution pages: use their data tools."),
    input_schema=dict(type='object', additionalProperties=False,
        properties=dict(action=dict(type='string', enum=list(ACTIONS))), required=['action']))


def execute(args, callback, unattended=False):
    if (not isinstance(args, dict) or set(args) != {'action'} or
            args['action'] not in ACTIONS or unattended or not callable(callback)):
        return json.dumps(dict(ok=False, error='This interface action is currently unavailable.'), ensure_ascii=False), True
    try:
        result = callback(args['action'])
        if not isinstance(result, dict) or result.get('ok') is not True:
            raise ValueError((result or {}).get('error', 'The interface did not confirm success.')
                             if isinstance(result, dict) else 'The interface did not confirm success.')
        return json.dumps(result, ensure_ascii=False), False
    except Exception as error:
        return json.dumps(dict(ok=False, error=str(error)[:200]), ensure_ascii=False), True
