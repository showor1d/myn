/* Local mono voice effects. Two cross-faded delay heads change pitch without
   changing the capture clock. No audio leaves this processor except its output. */
class MYNVoiceEffect extends AudioWorkletProcessor {
  constructor(options) {
    super();
    this.ring = new Float32Array(32768); this.index = 0; this.phase = 0;
    this.carrier = 0; this.effect = 'natural'; this.pitch = 0;
    const configure = data => {
      this.effect = ['natural','pitch','robot','telephone'].includes(data.effect) ? data.effect : 'natural';
      this.pitch = Number.isFinite(data.pitch) ? Math.max(-12, Math.min(12, data.pitch)) : 0;
    };
    configure(options?.processorOptions || {});
    this.port.onmessage = ({data}) => configure(data);
  }
  process(inputs, outputs) {
    const source = inputs[0]?.[0], output = outputs[0][0];
    if (!source) { output.fill(0); return true; }
    const length = this.ring.length, span = sampleRate * .06;
    const ratio = 2 ** (this.pitch / 12), delta = (1-ratio) / span;
    const read = delay => {
      const p = (this.index - delay + length) % length, n = Math.floor(p), fraction = p-n;
      return this.ring[n]*(1-fraction) + this.ring[(n+1)%length]*fraction;
    };
    for (let i=0; i<output.length; i++) {
      this.ring[this.index] = source[i];
      let value = source[i];
      if (this.effect === 'pitch' && this.pitch !== 0) {
        const a = this.phase, b = (a+.5)%1;
        const weight = .5-.5*Math.cos(2*Math.PI*a);
        value = read(16+a*span)*weight + read(16+b*span)*(1-weight);
        this.phase = (this.phase+delta+1)%1;
      } else if (this.effect === 'robot') {
        value *= Math.sin(this.carrier);
        this.carrier = (this.carrier+2*Math.PI*45/sampleRate)%(2*Math.PI);
      }
      output[i] = value;
      this.index = (this.index+1)%length;
    }
    return true;
  }
}
registerProcessor('myn-voice-effect', MYNVoiceEffect);
