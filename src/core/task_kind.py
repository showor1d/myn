"""Is this message chat (answer fast) or work (think it through)? 2026-10-18.

The user's rule: only chat needs to be quick; anything that needs real thought is work
and gets deep thinking, more context and more tool rounds. Deciding must not slow chat
down, so it is decided here, locally, from the message itself (no extra model call).
When the rules can't tell, it is chat — and the chat model can still call
think_deeper (chat.py) to move the turn to work when it sees the question is harder.
"""

import re

CHAT, WORK = "chat", "work"
FOLLOW_ON_S = 15 * 60  # a short follow-up to work within this time stays work

_WORK_WORDS = re.compile(
    r"分析|对比|比较|评估|权衡|利弊|方案|规划|计划书|设计|架构|实现|开发|编写|写(?:一|个|篇|份|段|封)|"
    r"代码|程序|脚本|函数|接口|算法|bug|报错|错误信息|异常|调试|排查|修复|修一下|优化|重构|部署|"
    r"总结|归纳|整理一下|报告|文档|论文|大纲|简历|翻译(?:这|一下|成)|润色|改写|校对|"
    r"证明|推导|计算|算一下|公式|方程|原理|机制|区别|怎么(?:做|实现|设计|写|解决|安排)|"
    r"如何(?:做|实现|设计|写|解决|安排|才能|提高|学)|"
    r"步骤|流程|详细|深入|具体说|研究|调研|查一下.*(?:资料|论文)|帮我(?:做|写|改|想|查|算|看看这|分析|规划|整理)|"
    r"改进|审查|检查一下|review|explain|analy[sz]|implement|debug|refactor|design|compare|"
    r"write (?:a|an|the|me)|plan\b|how (?:do|does|can|should)|step[- ]by[- ]step|"
    r"traceback|exception|stack ?trace|error:", re.I)
# "Why" questions need explaining only when there is something to explain.
_WHY = re.compile(r"为什么|为啥|\bwhy\b", re.I)
# Quick things that are chat even when long-ish: greetings, feelings, single actions.
_CHAT_ONLY = re.compile(
    r"^(?:你好|嗨|哈喽|早|早上好|中午好|晚上好|晚安|在吗|在不在|谢谢|多谢|好的|好吧|嗯+|哦+|行|可以|没事|哈+|"
    r"hi|hello|hey|thanks?|thank you|ok(?:ay)?|good (?:morning|night))[\s!！。.,，~～?？]*$", re.I)
_QUICK_ACTION = re.compile(
    r"^(?:把|帮我)?(?:音量|声音)|^(?:打开|关掉|关闭|暂停|播放|下一首|上一首|静音|锁屏|几点|现在几点|"
    r"今天几号|今天星期|天气|计时|定(?:个|一个)?(?:闹钟|提醒)|提醒我)", re.I)
_CODE = re.compile(r"```|^\s{4,}\S|^\s*(?:def |class |import |from \S+ import|function |const |let |var |#include)|"
                   r"[{};]\s*$|[A-Za-z]:\\[^\s]+\.\w{1,5}\b", re.M)


def classify(text, previous=None, seconds_since=None, heard=False, images=False):
    """(kind, reason). previous is the last turn's kind; seconds_since the time since it."""
    words = (text or "").strip()
    if not words:
        return CHAT, 'Empty'
    if _CHAT_ONLY.match(words):
        return CHAT, 'Greeting or brief response'
    if _CODE.search(words):
        return WORK, 'Contains code, an error or a file path'
    lines = words.count("\n") + 1
    if lines >= 4 or len(words) >= (220 if heard else 120):
        return WORK, 'Long or multiline content'
    work_word = _WORK_WORDS.search(words) or (_WHY.search(words) if len(words) >= 15 else None)
    if _QUICK_ACTION.search(words) and not work_word:
        return CHAT, 'Simple action or query'
    if work_word:
        # Brief spoken questions stay in casual chat unless they contain a substantive task.
        if heard and len(words) < 12:
            return CHAT, 'Very short spoken phrase'
        return WORK, 'Requires analysis, writing, programming or reasoning (%s)' % work_word.group(0)
    if images and len(words) >= 8:
        return WORK, 'Requires image analysis'
    if previous == WORK and seconds_since is not None and seconds_since <= FOLLOW_ON_S \
            and len(words) <= 60:
        return WORK, 'Continues previous work'
    return CHAT, 'Everyday chat'


# Per-kind budgets: (effort, max_tokens, tool rounds, history turns, history chars,
# gap minutes that start a new conversation). Chat stays as before (fast); work gets
# what Claude Code gets: deep thinking, room to write, many tool rounds, long memory.
BUDGETS = {
    CHAT: {"effort": "low", "max_tokens": 4000, "tool_rounds": 8,
           "turns": 8, "history_chars": 20000, "gap_minutes": 30, "question_chars": 2000, "answer_chars": 4000},
    WORK: {"effort": "high", "max_tokens": 32000, "tool_rounds": 40,
           "turns": 40, "history_chars": 160000, "gap_minutes": 360, "question_chars": 20000, "answer_chars": 40000},
}
