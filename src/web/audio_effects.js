/* Independent DSP graphs: changing a preference never starts capture or routing. */
(() => {
  const frequencies = [60, 170, 350, 1000, 3500, 10000];
  function equalizer(context) {
    const input = context.createGain();
    const bands = frequencies.map((frequency, i) => {
      const node = context.createBiquadFilter();
      node.type = i === 0 ? 'lowshelf' : i === 5 ? 'highshelf' : 'peaking';
      node.frequency.value = frequency; node.Q.value = .8;
      return node;
    });
    let tail = input;
    for (const band of bands) { tail.connect(band); tail = band; }
    return {input, output:tail, bands, set(values, enabled=true) {
      bands.forEach((band,i) => band.gain.setTargetAtTime(enabled ? values[i] : 0, context.currentTime, .025));
    }};
  }
  function limiter(context) {
    const node = context.createDynamicsCompressor();
    node.threshold.value = -3; node.knee.value = 0; node.ratio.value = 20;
    node.attack.value = .003; node.release.value = .15;
    return node;
  }
  window.AudioEffects = {frequencies, equalizer, limiter};
})();
