"""Real offline KWS + Whisper on a supplied synthetic WAV; never opens the mic.

Run with local-tts/.venv Python; fixture must be generated speech, not personal
recordings. Verifies event ordering through Listener, no real GUI actions.
"""
import json
import sys
import time
import wave
from pathlib import Path
import numpy as np

BASE=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(BASE/'src/core'))
import stt
import stt_worker
import streaming_wake


def main(path):
    with wave.open(str(path),'rb') as source:
        assert source.getnchannels()==1 and source.getsampwidth()==2
        rate=source.getframerate()
        samples=np.frombuffer(source.readframes(source.getnframes()),np.int16).astype(np.float32)/32768
    events=[]
    transcribe,info=stt_worker.load_whisper(str(stt.MODEL))
    def recognize(audio,language):
        assert any(e['event']=='wake_detected' for e in events), 'Full ASR started before wake'
        return transcribe(audio,language)
    clock=[100.0]
    listener=stt_worker.Listener(None,np,recognize,events.append,threaded=False,clock=lambda:clock[0])
    listener.detector=streaming_wake.Detector('Hey Mine')
    listener.rate=rate
    listener.language='en'
    block=int(rate*.03)
    audio=np.concatenate([samples,np.zeros(rate,np.float32)])
    started=time.perf_counter()
    for pos in range(0,len(audio),block):
        clock[0]=100+pos/rate
        listener.feed(audio[pos:pos+block],clock[0])
    heard=[e for e in events if e['event']=='heard']
    assert heard and len(heard)<=2,events  # a natural pause may split wake and request
    assert heard[0]['streaming_wake'] and all(not e['record_only'] for e in heard)
    assert 'hear' in ' '.join(e['text'] for e in heard).lower(),heard
    assert len({e['capture_id'] for e in heard})==len(heard)
    assert stt._valid_template(heard[0].get('wake_template')), 'Isolated wake must yield portable features'
    print(json.dumps(dict(ok=True,model=info['gpu'],event_order=[e['event'] for e in events],
        synthetic_transcript=[e['text'] for e in heard],recognition_s=[e['transcribe_s'] for e in heard],
        wake_feature_frames=len(heard[0]['wake_template']),
        processing_s=round(time.perf_counter()-started,3)),ensure_ascii=False))


if __name__=='__main__':
    main(Path(sys.argv[1]))
