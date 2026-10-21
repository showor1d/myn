'Continue a spoken conversation without repeating the wake word. Judge whether each later sentence addresses MYN or someone else. Only sentences addressed to MYN receive a reply; retain other speech quietly for memory. Close the conversation for clear other-party speech, a quiet interval, or an explicit farewell. Ambiguous fragments and stray sounds neither receive replies nor close the conversation.'

import json
import re

import claude_client

IDLE_SECONDS = 120  # the conversation closes after this long without an exchange
RECENT_TURNS = 6
from assistant_intent import VOICE_NOTE

JUDGE_SYSTEM = (
    'You decide whether speech is addressed to the voice assistant MYN.\n'
    'The user opened a conversation with the wake word and need not repeat it each time. The microphone stays on, '
    'so it hears requests to MYN, nearby conversations, phone calls, self-talk, '
    'reading aloud and television or video audio.\n'
    "Singing, humming, lyrics and quoted dialogue are not addressed to MYN, even if they contain thanks, questions or the assistant's name. "
    'Use context; lyrics after a reply are not automatically an answer. Handle explicit requests to change volume, switch songs or evaluate singing.\n'
    'Use recent conversation and the new sentence to judge whether it addresses MYN.\n'
    'Return true for answers to MYN, follow-up questions or details, feedback or responses '
    '(such as agreement, corrections or requests for more detail), and clear new requests to the assistant.\n'
    'Speech recognition may render MYN as Maya, Maia or a Chinese phonetic equivalent. A request addressed to such a name counts as addressing MYN.\n'
    'When to_myn is false, determine elsewhere: is this clearly addressed to another person, a friend or a phone call, '
    'or clearly dialogue from video, television or games? If so, elsewhere is true. '
    'For fragments, unclear sounds, self-directed exclamations or uncertainty, elsewhere is false.\n'
    "'Recently overheard' contains recent microphone speech already judged not addressed to MYN. If the new sentence continues that "
    'conversation with another person, it is not addressed to MYN and elsewhere is true.\n'
    'ends: whether the user is ending the conversation with MYN, for example by saying goodbye or that no more help is needed.\n'
    + VOICE_NOTE + 'Output only one JSON object: '
    '{"to_myn": true or false, "elsewhere": true or false, "ends": true or false}'
)


def judge_prompt(recent, utterance, heard=(), pending=None):
    """recent: [(speaker tag, text)] oldest first, speaker "you" or "myn"; heard: sentences
    heard in the last few minutes that were not for MYN (the user chatting with a friend)."""
    lines = []
    for tag, text in recent[-RECENT_TURNS:]:
        text = " ".join(str(text).split())
        if len(text) > 300:
            text = text[:180] + "…" + text[-120:]
        lines.append("%s：%s" % ('User' if tag == "you" else "MYN", text))
    aside = [" ".join(str(said).split())[:120] for said in list(heard)[-5:] if str(said).strip()]
    context = ""
    if pending:
        context = ("Awaiting the user's decision on this plan (not yet adopted): %s.\n"
                   'Interpret brief replies in that context; do not mistake rejecting the plan for addressing someone else. '
                   'Clear conversations with others still count as elsewhere; stay quiet when uncertain.\n' %
                   json.dumps(pending, ensure_ascii=False))
    return 'Recent conversation:\n%s\n\n%s%sNew sentence: %s' % (
        "\n".join(lines) if lines else '(Just started)',
        context,
        ('Recently overheard (not addressed to MYN):\n%s\n\n' % "\n".join(aside)) if aside else "",
        " ".join(str(utterance).split()))


def parse_verdict(reply):
    """(to_myn, ends, elsewhere) from the judge's reply; unreadable replies raise."""
    match = re.search(r"\{.*\}", str(reply or ""), re.S)
    if not match:
        raise ValueError("no verdict")
    data = json.loads(match.group(0))
    to_myn, ends, elsewhere = data.get("to_myn"), data.get("ends", False), data.get("elsewhere", False)
    if not all(isinstance(value, bool) for value in (to_myn, ends, elsewhere)):
        raise ValueError("bad verdict")
    return to_myn, ends, (elsewhere and not to_myn)


def judge(key, recent, utterance, usage_path=None, sender=None, heard=(), pending=None):
    """Ask Claude whether the utterance continues the conversation with MYN.

    Returns (to_myn, ends, elsewhere). On any failure it returns (False, False,
    False): no reply (safer than answering speech meant for someone else), but
    the conversation stays open.
    """
    send = sender or claude_client.send
    prompt = judge_prompt(recent, utterance, heard, pending)
    # Keep the short verdict prompt, using the user's fixed Opus 5.5 model.
    attempts = ((claude_client.OPUS, {"type": "disabled"}),)
    for model, thinking in attempts:
        try:
            reply = send(model, JUDGE_SYSTEM, prompt, key, max_tokens=60, usage_path=usage_path,
                         purpose="conversation_judge", effort="low", thinking=thinking)
            return parse_verdict(reply)
        except claude_client.ClaudeUnavailable as error:
            if not error.fallback_allowed:
                break
        except Exception:  # never lose the sentence: on any failure it is just noted
            break
    return False, False, False
