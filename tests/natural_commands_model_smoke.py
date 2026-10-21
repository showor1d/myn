"""Opt-in real model check; synthetic conversations and stub tools, no real actions.

Usage goes to MYN's normal ledger. Never print credentials or personal context.
Run explicitly with --live; not part of unittest discovery.
"""
import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

BASE = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(BASE/'src'), str(BASE/'src/core')]
import assistant_intent
import chat
import claude_client
import credentials
import data_location
import interaction


def main():
    if '--live' not in sys.argv:
        raise SystemExit('Explicit --live required')
    config = data_location.user_data_dir()
    key = credentials.get_key(config)
    if not key:
        raise SystemExit('No configured key; live verification unavailable')
    original = claude_client.coordinated_reply
    cases = [
        ('放首歌', [], ('music_control', 'play')),
        ('放手歌', [], ('music_control', 'play')),
        ('随便来一首吧', [('想听音乐吗？', 'user')], ('music_control', 'play')),
        ('Could you put some music on?', [], ('music_control', 'play')),
        ('何か曲をかけて', [], ('music_control', 'play')),
        ('让我看看最近的开销', [], ('interface_control', 'expenses')),
        ('我想用键盘跟你说', [], ('interface_control', 'input_open')),
        ('把声音弄小一点', [], ('set_volume', None)),
        ('明天八点叫我起床', [], ('add_alarm', None)),
        ('先别放歌', [], None),
        ('解释一下“放首歌”是什么意思，不要播放', [], None),
    ]
    results = []
    for question, history, expected in cases:
        with tempfile.TemporaryDirectory(prefix='myn-semantic-live-') as folder:
            session = chat.ChatSession(Path(folder)/'database/jarvis.db')
            session.interface_control = lambda action: {'ok':True}
            session._heard_turn = True
            session._addressing_confirmed = True
            session._optional_key = lambda: key
            calls = []
            def run(system, message, secret, **kwargs):
                def simulated_tool(name, args):
                    calls.append((name, args))
                    if name == 'music_control':
                        result = dict(ok=True, playing=True, name='Test Song', paused=False)
                    elif name == 'interface_control':
                        result = dict(ok=True, action=args.get('action'), dispatched=True)
                    else:
                        result = dict(ok=True, verified=True, id='test-only', result=args)
                    return json.dumps(result, ensure_ascii=False), False
                kwargs.update(tool_executor=simulated_tool, server_tools=None,
                              usage_path=config/'claude-usage.jsonl', purpose='command_verification',
                              max_tokens=1200, max_tool_rounds=3)
                if history:
                    kwargs['history'] = [dict(question='我想听点音乐', answer='想听哪首？')]
                return original(system, message, secret, **kwargs)
            with patch.object(claude_client, 'coordinated_reply', side_effect=run):
                answer = session._general(question)
            mutation = [c for c in calls if c[0] not in ('myn_status','list_alarms','music_control')
                        or (c[0]=='music_control' and c[1].get('action')!='status')]
            passed = (any(n==expected[0] and (expected[1] is None or a.get('action')==expected[1])
                          for n,a in calls) if expected else not mutation)
            result = dict(question=question, passed=passed, tools=calls,
                          unavailable=bool(answer.get('unavailable')))
            results.append(result)
            print(json.dumps(result, ensure_ascii=False), flush=True)
    # Real semantic addressing tolerates ASR homophones only in directed conversation.
    verdict = interaction.classify(config, dict(mode='idle',revision=0), '放手隔。',
        [dict(question='想听歌', answer='好，你想听什么？')], None, key,
        addressing=dict(recent=[['you','想听歌'],['myn','好，你想听什么？']],heard_elsewhere=[]))
    addressed = (verdict.get('addressing') or {}).get('to_myn') is True
    print(json.dumps(dict(addressing_homophone=addressed), ensure_ascii=False), flush=True)
    raise SystemExit(0 if all(r['passed'] and not r['unavailable'] for r in results) and addressed else 1)


if __name__ == '__main__':
    main()
