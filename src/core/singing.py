"""Live performance guard. Numeric acoustic hints, never recordings or identity claims."""
import threading
from pathlib import Path

NOTE = """
Also return vocal:{activity:singing|humming|speech|unknown,
intent:request|question|answer|none,certainty:clear|uncertain,
evidence:exact current words,resume_conversation:boolean}.
Singing, humming, lyrics, recitation and dialogue in media are NOT conversation
with MYN even if the transcript contains thanks, a name or a question. Never reply
'you are welcome', 'keep singing', 'I will not interrupt'. A recent MYN reply does not make lyrics an answer.
The acoustic hint only indicates sustained pitched sound, NOT proof of singing or
speaker identity; music behind a clearly spoken request must not block that request.
Use current words and ongoing context to distinguish a genuine request/question or
answer to MYN from sung/quoted words. During performance, casual fragments/thanks
alone have intent=none; uncertainty about whom it addresses means to_myn=false.
Explicitly asking MYN to adjust volume, change music, or evaluate the singing can be
addressed without ending performance. resume_conversation=true ONLY if the current
directed speech clearly returns to normal conversation; no fixed command needed.
A deliberately SPOKEN Hey Mine asking for attention can have intent=request;
the same sounds sung as lyrics are not a wake request.
Silence, a pause between verses, an alarm, old/quoted/hypothetical singing descriptions
do not prove either entering or leaving performance. Classify the CURRENT utterance.
"""

_lock = threading.RLock()
_guards = {}


class Guard:
    def __init__(self):
        self.active = False
        self.epoch = 0
        self.last_signal = 0.
        self.permit = None

    def signal(self, at):
        with _lock:
            new_phrase = not self.last_signal or at-self.last_signal > 1.4
            # A confirmed spoken return can coexist with an uninterrupted musical
            # background. Don't override it with the same weak cue every 0.5 seconds.
            if not self.active and not new_phrase:
                self.last_signal = max(at, self.last_signal)
                return
            if not self.active or new_phrase:
                self.epoch += 1
                self.permit = None
            self.active = True
            self.last_signal = max(at, self.last_signal)

    def snapshot(self):
        with _lock:
            return dict(active=self.active, epoch=self.epoch,
                        acoustic_hint='sustained_pitched_sound' if self.last_signal else None)

    def allowed(self, purpose):
        with _lock:
            return purpose == 'alarm' or not self.active or (purpose == 'reply' and self.permit == self.epoch)

    def decide(self, result, question, snapshot):
        """Normalize an untrusted verdict; stale decisions cannot open the speech gate."""
        with _lock:
            vocal = result.get('vocal') or {}
            if not isinstance(vocal, dict): vocal = {}
            if 'vocal' in result:
                result['vocal'] = vocal
            addressing = result.get('addressing') or {}
            if not isinstance(addressing, dict): addressing = {}
            evidence = vocal.get('evidence')
            clear = (vocal.get('certainty') == 'clear' and isinstance(evidence, str)
                     and bool(evidence.strip()) and evidence in question)
            performance = clear and vocal.get('activity') in ('singing', 'humming')
            if performance and not self.active:
                self.active = True
                self.epoch += 1
                self.permit = None
            guarded = self.active or snapshot['active'] or performance
            if not guarded:
                return result
            direct = (clear and vocal.get('activity') == 'speech' and
                      vocal.get('intent') in ('request', 'question', 'answer') and
                      addressing.get('to_myn') is True and
                      snapshot['epoch'] == self.epoch)
            if direct:
                self.permit = self.epoch
                if vocal.get('resume_conversation') is True:
                    self.active = False
            else:
                self.permit = None
                result['addressing'] = dict(to_myn=False, ends=False, elsewhere=False)
            result['_vocal_epoch'] = self.epoch
            return result


def guard(config):
    key = str(Path(config).resolve()).casefold()
    with _lock:
        return _guards.setdefault(key, Guard())


class PitchHint:
    """Conservative sustained-pitch cue, not a speech/music classifier.

    Runs off the sound-card callback, on 2.4 seconds of ephemeral PCM, twice/sec.
    A candidate alone never authorizes an action or asserts who is singing.
    """
    def __init__(self, np):
        self.np = np
        self.blocks = []
        self.samples = 0
        self.elapsed = 0.
        self.rate = None

    def feed(self, block, rate):
        if rate != self.rate:
            self.blocks=[]; self.samples=0; self.elapsed=0.; self.rate=rate
        self.blocks.append(block)
        self.samples += len(block)
        self.elapsed += len(block)/rate
        while self.blocks and self.samples-len(self.blocks[0]) >= rate*2.4:
            self.samples -= len(self.blocks.pop(0))
        if self.elapsed < .5 or self.samples < rate*1.8:
            return False
        self.elapsed = 0.
        return self.analyze(self.np.concatenate(self.blocks), rate)

    def analyze(self, audio, rate):
        np = self.np
        step=max(1,round(rate/8000)); data=audio[::step]; sr=rate/step
        size=int(sr*.04); hop=int(sr*.02)
        pitches=[]
        for i in range(0,len(data)-size+1,hop):
            frame=np.asarray(data[i:i+size], dtype=np.float64)
            frame=frame-frame.mean()
            power=float(np.mean(frame*frame))
            if power < .000064:
                pitches.append(0.); continue
            fft=np.fft.rfft(frame, n=size*2)
            ac=np.fft.irfft(fft*fft.conjugate(),n=size*2)[:size]
            low,high=max(1,int(sr/900)),min(size-1,int(sr/75))
            lag=low+int(np.argmax(ac[low:high]))
            correlation=float(ac[lag]/max(ac[0]*(1-lag/size),1e-12))
            pitches.append(sr/lag if correlation>.82 else 0.)
        if not pitches: return False
        run=best=0; previous=0.
        for pitch in pitches:
            run=run+1 if pitch and previous and abs(pitch/previous-1)<.08 else int(bool(pitch))
            best=max(best,run); previous=pitch
        return (sum(bool(p) for p in pitches)/len(pitches)>.72 and
                best*.02>=.7 and sum(bool(p) for p in pitches)*.02>=1.5)
