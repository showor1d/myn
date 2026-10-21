/* Real PCM spectra supplied by the microphone / WASAPI workers. No phase animation. */
(() => {
  const count=80, history=new Float32Array(count);
  const bandCount=128, spectrum=new Float32Array(bandCount);
  let micBands=null, outputBands=null;
  let mic=0, micAt=-10000, output=0, outputAt=-10000, smooth=0,
    head=0, elapsed=0, active=true, pollTimer, bounds=null;
  const finite=value=>Number.isFinite(value)?Math.min(1,Math.max(0,value)):0;
  const validBands=bands=>Array.isArray(bands)&&bands.length===bandCount?bands.map(finite):null;
  function input(value, now=performance.now(), bands=null) {
    mic=finite(value);micAt=now;micBands=validBands(bands);
  }
  function playback(value, now=performance.now(), bands=null) {
    output=finite(value);outputAt=now;outputBands=validBands(bands);
  }
  function levels(now=performance.now()) {
    return {input:now-micAt<400?mic:0,output:now-outputAt<400?output:0};
  }
  async function poll() {
    if(!active)return;
    try {
      const api=window.pywebview?.api;
      if(api?.core_audio_levels) {
        const data=await api.core_audio_levels();
        if(active)playback(data.available?data.output:0,performance.now(),data.available?data.bands:null);
      }
    } catch(_) {playback(0);}
    if(active)pollTimer=setTimeout(poll,document.hidden?250:40);
  }
  function sample(dt,now=performance.now()) {
    const current=levels(now),a=current.input,b=current.output;
    // Bring quiet real audio into view, with a small floor to suppress idle noise.
    const drive=Math.max(a,b), target=drive>.0015?Math.min(1,Math.sqrt(drive)*1.45):0;
    smooth+=(target-smooth)*(1-Math.exp(-dt*(target>smooth?24:10)));
    if(smooth<.001)smooth=0;
    const fromMic=now-micAt<400&&micBands, fromOutput=now-outputAt<400&&outputBands;
    for(let i=0;i<bandCount;i++) {
      const targetBand=Math.max(fromMic&&a>.0015?micBands[i]:0,
                                fromOutput&&b>.0015?outputBands[i]:0);
      spectrum[i]+=(targetBand-spectrum[i])*(1-Math.exp(-dt*(targetBand>spectrum[i]?32:18)));
      if(spectrum[i]<.001)spectrum[i]=0;
    }
    elapsed+=dt;
    while(elapsed>=.025) {elapsed-=.025;head=(head+1)%count;history[head]=smooth;}
    return {input:a,output:b,level:smooth,spectral:!!(fromMic||fromOutput)};
  }
  function draw(g,x,y,radius,scale,dt,now=performance.now()) {
    const signal=sample(dt,now);
    const strength=Math.min(1,signal.level*3);
    const base=radius*1.19+Math.max(.6,scale), amplitude=Math.max(9,radius*.72);
    bounds={x,y,radius:base+amplitude,level:signal.level,scale};
    if(strength<.003)return;
    g.save();g.globalCompositeOperation='source-over';g.filter='none';g.shadowBlur=0;
    g.lineCap='round';g.lineJoin='round';
    const mix=signal.input/(signal.input+signal.output+.0001);
    // Each angle is a fixed frequency band. Same audio => same contour, regardless
    // of wall clock. Smooth only incoming measurements; no synthesized crests.
    for(let layer=2;layer>=0;layer--) {
      const rim=[], n=bandCount;
      for(let i=0;i<n;i++) {
        const a=i/n*Math.PI*2;
        // If loopback is unavailable, show just a uniform loudness pulse, never
        // invent a frequency distribution from a single scalar volume reading.
        const wave=signal.spectral?Math.pow(spectrum[i],1.5):signal.level*.12;
        const r=base+amplitude*wave*(1-layer*.18)+layer*radius*.025;
        rim.push([x+Math.cos(a-Math.PI/2)*r,y+Math.sin(a-Math.PI/2)*r]);
      }
      g.strokeStyle=`rgba(${Math.round(91+35*mix)},${Math.round(183+48*mix)},255,${((layer? .16:.88)*strength).toFixed(3)})`;
      g.lineWidth=layer?Math.max(.7,scale):Math.max(1.1,2.1*scale);
      g.beginPath();
      const last=rim[n-1],first=rim[0];
      g.moveTo((last[0]+first[0])/2,(last[1]+first[1])/2);
      for(let i=0;i<n;i++) {
        const p=rim[i],next=rim[(i+1)%n];
        g.quadraticCurveTo(p[0],p[1],(p[0]+next[0])/2,(p[1]+next[1])/2);
      }
      g.closePath();g.stroke();
    }
    g.restore();
  }
  window.addEventListener('pagehide',()=>{active=false;clearTimeout(pollTimer);});
  window.CoreAudioRing={input,playback,draw,sample,levels,
    state:()=>({...bounds,history:Array.from(history),bands:Array.from(spectrum)})};
  poll();
})();
