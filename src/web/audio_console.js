/* Audio processing lives outside the page, so navigation never interrupts playback. */
(() => {
  const defaults = () => ({music_eq:[0,0,0,0,0,0], mic_eq:[0,0,0,0,0,0],
    music_enabled:true, mic_clean:true, effect:'natural', pitch:0, mic_gain:1,
    music_send:.5, mic_send:1, monitor_gain:.3, input_device:'', listen_device:'', send_device:'',
    input_name:'',listen_name:'',send_name:''});
  let settings=defaults(), ready, context, initializing, localOutput, remoteOutput,
    localBus, remoteBus, musicEQ, musicLocal, musicRemote, micStream, micNodes,
    sending=false, monitoring=false, devices=[], mount, saveChain=Promise.resolve(),
    revision=0, captureGeneration=0, message='', localLevel=1, localMuted=false, workletReady, musicMeter, sendMeter;
  const sources = new Map();
  function timeout(promise) {
    let timer;
    return Promise.race([promise,new Promise((_,reject)=>{timer=setTimeout(()=>reject(new Error('Audio device startup timed out. Check the device and retry.')),8000);})]).finally(()=>clearTimeout(timer));
  }
  const virtual = label => /cable|voicemeeter|virtual/i.test(label);
  const api = (method,...args) => {
    const fn=window.pywebview?.api?.[method];
    return fn ? fn(...args) : Promise.reject(new Error('Audio controls are not connected yet. Try again shortly.'));
  };
  function status(text) {
    message=text;
    const node=mount?.querySelector('.audio-status');
    if(node) node.textContent=text;
  }
  async function load() {
    if(!ready) ready=api('audio_console_settings').then(data=>{settings=data;})
      .catch(error=>{ready=null; throw error;});
    return ready;
  }
  function persist() {
    const data=JSON.parse(JSON.stringify(settings)), current=++revision;
    saveChain=saveChain.catch(()=>{}).then(()=>api('audio_console_save',data))
      .then(()=>{if(current===revision) status('Saved');})
      .catch(error=>status('Adjusted, but save failed: '+error.message));
  }
  function gain(node,value) {
    if(node) node.gain.setTargetAtTime(value,context.currentTime,.025);
  }
  function apply() {
    if(!context) return;
    musicEQ.set(settings.music_eq,settings.music_enabled);
    gain(musicLocal,localMuted ? 0 : localLevel);
    gain(musicRemote,sending ? settings.music_send : 0);
    if(micNodes) {
      micNodes.eq.set(settings.mic_eq);
      gain(micNodes.level,settings.mic_gain);
      gain(micNodes.monitor,monitoring ? settings.monitor_gain : 0);
      gain(micNodes.remote,sending ? settings.mic_send : 0);
      micNodes.high.frequency.setTargetAtTime(settings.effect==='telephone' ? 350 : settings.mic_clean ? 80 : 10,context.currentTime,.025);
      micNodes.low.frequency.setTargetAtTime(settings.effect==='telephone' ? 3200 : 20000,context.currentTime,.025);
      micNodes.effect.port.postMessage({effect:settings.effect,pitch:settings.pitch});
    }
  }
  async function inventory() {
    devices=await navigator.mediaDevices.enumerateDevices();
    // Browser IDs can change across private-mode restarts. Match an exact, unique
    // saved device name; never silently choose a different microphone.
    for(const prefix of ['input','listen','send']) {
      const kind=prefix==='input'?'audioinput':'audiooutput', key=prefix+'_device';
      if(settings[key] && !devices.some(d=>d.kind===kind&&d.deviceId===settings[key])) {
        const matches=devices.filter(d=>d.kind===kind && d.deviceId!=='default' && d.deviceId!=='communications' && d.label===settings[prefix+'_name']);
        if(settings[prefix+'_name'] && matches.length===1) settings[key]=matches[0].deviceId;
      }
    }
    return devices;
  }
  async function sink(element,id,remote=false) {
    await inventory();
    if(id) {
      const item=devices.find(d=>d.kind==='audiooutput' && d.deviceId===id);
      if(!item) throw new Error('Selected output device disconnected. Select it again.');
      if(remote && !virtual(item.label)) throw new Error('Select the virtual audio output endpoint.');
      if(!remote && virtual(item.label)) throw new Error('Choose speakers or headphones for local monitoring.');
    } else if(remote) throw new Error('No virtual microphone device selected.');
    if(typeof element.setSinkId!=='function') throw new Error('This window cannot switch output devices.');
    await element.setSinkId(id);
  }
  async function restoreListen() {
    if(localOutput && settings.listen_device && localOutput.sinkId!==settings.listen_device &&
        devices.some(d=>d.kind==='audiooutput'&&d.deviceId===settings.listen_device))
      await sink(localOutput,settings.listen_device);
  }
  function output(bus) {
    const limiter=AudioEffects.limiter(context), destination=context.createMediaStreamDestination();
    bus.connect(limiter).connect(destination);
    const element=new Audio(); element.srcObject=destination.stream;
    return element;
  }
  async function init() {
    await load();
    if(initializing) return initializing;
    initializing=(async()=>{
      context=new AudioContext({latencyHint:'interactive'});
      localBus=context.createGain(); remoteBus=context.createGain();
      sendMeter=context.createAnalyser();sendMeter.fftSize=256;remoteBus.connect(sendMeter);
      musicEQ=AudioEffects.equalizer(context);
      musicMeter=context.createAnalyser(); musicMeter.fftSize=256; musicEQ.output.connect(musicMeter);
      musicLocal=context.createGain(); musicRemote=context.createGain();
      musicRemote.gain.value=0;
      musicEQ.output.connect(musicLocal).connect(localBus);
      musicEQ.output.connect(musicRemote).connect(remoteBus);
      localOutput=output(localBus); remoteOutput=output(remoteBus);
      if(settings.listen_device) {
        try {await sink(localOutput,settings.listen_device);}
        catch(error) {status(error.message+' Local playback is temporarily using the system default device.');}
      }
      await timeout(context.resume()); await timeout(localOutput.play()); apply();
    })().catch(error=>{initializing=null; context?.close(); context=null; throw error;});
    return initializing;
  }
  async function prepareMedia() {
    await init(); await timeout(context.resume()); await timeout(localOutput.play());
    for(const id of ['media-audio-output','media-secondary-audio']) {
      const element=document.getElementById(id);
      if(element && !sources.has(element)) {
        const source=context.createMediaElementSource(element);
        source.connect(musicEQ.input); sources.set(element,source);
      }
    }
  }
  function setVolume(level,muted) {localLevel=level; localMuted=muted; apply();}
  function stopMic() {
    captureGeneration++;
    const stream=micStream; micStream=null;
    if(stream) stream.getTracks().forEach(track=>track.stop());
    if(micNodes) {
      for(const node of [micNodes.source,micNodes.high,micNodes.low,micNodes.level,
        micNodes.effect,micNodes.monitor,micNodes.remote,micNodes.eq.input,...micNodes.eq.bands]) node.disconnect();
      micNodes.effect.port.close(); micNodes=null;
    }
    monitoring=false; refreshState();
  }
  async function startMic() {
    await init(); await inventory();
    const device=devices.find(d=>d.kind==='audioinput' && d.deviceId===settings.input_device);
    if(!device || !settings.input_device) throw new Error('Select a physical microphone for voice effects first.');
    if(virtual(device.label)) throw new Error('Do not route a virtual microphone into itself. Select a physical microphone.');
    const generation=++captureGeneration;
    await api('audio_console_authorize_capture');
    const stream=await navigator.mediaDevices.getUserMedia({audio:{deviceId:{exact:device.deviceId},
      echoCancellation:true, noiseSuppression:settings.mic_clean, autoGainControl:false},video:false});
    if(generation!==captureGeneration) {stream.getTracks().forEach(t=>t.stop()); return;}
    try {
      if(!workletReady)workletReady=context.audioWorklet.addModule('voice_effect_worklet.js').catch(error=>{workletReady=null;throw error;});
      await workletReady;
      if(generation!==captureGeneration) {stream.getTracks().forEach(t=>t.stop());return;}
      const source=context.createMediaStreamSource(stream), high=context.createBiquadFilter(),
        low=context.createBiquadFilter(), level=context.createGain(), eq=AudioEffects.equalizer(context),
        effect=new AudioWorkletNode(context,'myn-voice-effect',{numberOfInputs:1,numberOfOutputs:1,outputChannelCount:[1]}),
        monitor=context.createGain(), remote=context.createGain();
      monitor.gain.value=0; remote.gain.value=0;
      high.type='highpass'; low.type='lowpass';
      source.connect(high).connect(low).connect(eq.input);
      eq.output.connect(level).connect(effect);
      effect.connect(monitor).connect(localBus); effect.connect(remote).connect(remoteBus);
      micNodes={source,high,low,level,eq,effect,monitor,remote}; micStream=stream;
      stream.getTracks().forEach(track=>track.addEventListener('ended',()=>{stopMic();status('Microphone disconnected.');}));
      apply(); refreshState(); status('Microphone processing enabled. MYN speech recognition still uses the original input.');
    } catch(error) {stream.getTracks().forEach(t=>t.stop()); throw error;}
  }
  function stopSend() {
    sending=false; remoteOutput?.pause(); apply(); refreshState();
  }
  async function startSend() {
    await init();
    await sink(remoteOutput,settings.send_device,true);
    try {await timeout(remoteOutput.play());}
    catch(error){remoteOutput.pause();throw error;}
    sending=true; apply(); refreshState();
    status('Sending audio. Select the matching virtual recording endpoint as the microphone in your call app.');
  }
  function refreshState() {
    const shortcut=document.getElementById('equalizer-shortcut');
    if(shortcut) {
      shortcut.classList.toggle('audio-active',!!micStream||sending);
      shortcut.title=sending?'Equalizer · Sending audio':micStream?'Equalizer · Processing microphone':'Equalizer';
    }
    if(!mount) return;
    const states=[['mic-toggle',!!micStream,'Stop microphone processing','Start microphone processing'],
      ['monitor-toggle',monitoring,'Stop monitoring','Monitor your voice'],
      ['send-toggle',sending,'Stop sending','Send to virtual microphone']];
    for(const [id,on,yes,no] of states) {
      const button=mount.querySelector('#audio-'+id);
      if(button) {button.textContent=on?yes:no; button.setAttribute('aria-pressed',String(on));}
    }
  }
  function node(tag,text,cls) {
    const el=document.createElement(tag); if(text)el.textContent=text; if(cls)el.className=cls; return el;
  }
  function action(text,callback,id) {
    const button=node('button',text); button.type='button'; if(id) button.id='audio-'+id;
    button.onclick=async()=>{button.disabled=true;try{await callback();}catch(error){status(error.message||String(error));}finally{button.disabled=false;}};
    return button;
  }
  function slider(parent,key,label,min,max,step=1,index=null) {
    const row=node('label',null,'audio-slider'), name=node('span',label), range=node('input'), value=node('output');
    range.type='range';range.min=min;range.max=max;range.step=step;range.value=index===null?settings[key]:settings[key][index];
    range.setAttribute('aria-label',label);
    const display=()=>value.textContent=key.endsWith('_eq')?Number(range.value).toFixed(0)+' dB':key==='pitch'?range.value+' semitones':Math.round(range.value*100)+'%';
    display();range.oninput=()=>{if(index===null)settings[key]=Number(range.value);else settings[key][index]=Number(range.value);display();apply();};
    range.onchange=persist;row.append(name,range,value);parent.append(row);
  }
  function eq(parent,key) {
    const controls=node('div',null,'audio-eq');
    AudioEffects.frequencies.forEach((f,i)=>slider(controls,key,f>=1000?f/1000+' kHz':f+' Hz',-12,12,1,i));
    parent.append(controls);
    const presets=node('div',null,'audio-buttons');
    for(const [title,values] of [['Natural',[0,0,0,0,0,0]],['Bass',[5,3,0,0,0,0]],['Vocals',[-2,-1,1,3,2,0]],['Bright',[0,0,-1,0,3,4]]])
      presets.append(action(title,()=>{settings[key]=values.slice();apply();persist();render();}));
    parent.append(presets);
  }
  function deviceSelect(parent,key,label,kind,filter,callback) {
    const row=node('label',null,'audio-device');row.append(node('span',label));
    const select=node('select'); select.setAttribute('aria-label',label);
    const blank=node('option',key==='listen_device'?'System default output device':'Select a device'); blank.value='';select.append(blank);
    for(const device of devices.filter(d=>d.kind===kind && d.deviceId!=='communications' && filter(d))) {
      const option=node('option',device.label||'Device '+device.deviceId.slice(0,8));option.value=device.deviceId;select.append(option);
    }
    if(settings[key] && ![...select.options].some(o=>o.value===settings[key])) {
      const missing=node('option','Saved device disconnected');missing.value=settings[key];select.append(missing);
    }
    select.value=settings[key];
    select.onchange=async()=>{
      const previous=settings[key]; select.disabled=true;
      try{await callback(select.value);settings[key]=select.value;
        settings[key.replace('_device','_name')]=devices.find(d=>d.kind===kind&&d.deviceId===select.value)?.label||'';persist();}
      catch(error){select.value=previous;status(error.message);}finally{select.disabled=false;}
    };
    row.append(select);parent.append(row);
  }
  function render() {
    if(!mount || !document.body.contains(mount))return;
    mount.textContent='';
    const heading=node('div',null,'audio-console-heading');
    heading.append(node('h2','Equalizer'),node('span','Music · Microphone · Audio routing'));
    const columns=node('div',null,'audio-columns');
    const music=node('section'),mic=node('section'),routing=node('section');
    music.append(node('h3','Music'));eq(music,'music_eq');
    music.append(action(settings.music_enabled?'Disable music EQ':'Enable music EQ',()=>{settings.music_enabled=!settings.music_enabled;apply();persist();render();}));
    mic.append(node('h3','Microphone'));eq(mic,'mic_eq');
    mic.append(action(settings.mic_clean?'Noise reduction on':'Noise reduction off',async()=>{
      const enabled=!settings.mic_clean;
      if(micStream)await micStream.getAudioTracks()[0].applyConstraints({noiseSuppression:enabled});
      settings.mic_clean=enabled;apply();persist();render();
    }));
    const effects=node('label',null,'audio-device');effects.append(node('span','Voice effect'));
    const select=node('select');select.setAttribute('aria-label','Voice effect');
    for(const [key,label] of [['natural','Natural'],['pitch','Pitch shift'],['robot','Robot'],['telephone','Telephone']]) {
      const option=node('option',label);option.value=key;select.append(option);
    }
    select.value=settings.effect;select.onchange=()=>{settings.effect=select.value;apply();persist();};effects.append(select);mic.append(effects);
    slider(mic,'pitch','Pitch',-12,12);slider(mic,'mic_gain','Microphone gain',0,2,.01);
    const micButtons=node('div',null,'audio-buttons');
    micButtons.append(action('',()=>micStream?stopMic():startMic(),'mic-toggle'),action('',async()=>{
      if(!micStream)throw new Error('Enable microphone processing first. Use headphones to avoid feedback.');
      monitoring=!monitoring;apply();refreshState();},'monitor-toggle'));
    mic.append(micButtons);slider(mic,'monitor_gain','Monitor volume',0,1,.01);
    mic.append(node('p','Use headphones when monitoring to prevent speaker feedback.','audio-note'));
    routing.append(node('h3','Devices and routing'));
    deviceSelect(routing,'listen_device','Local playback','audiooutput',d=>!virtual(d.label),async id=>{await init();await sink(localOutput,id);});
    deviceSelect(routing,'input_device','Effects microphone','audioinput',d=>d.deviceId!=='default'&&!virtual(d.label),async()=>stopMic());
    deviceSelect(routing,'send_device','Virtual microphone output','audiooutput',d=>virtual(d.label),async()=>stopSend());
    routing.append(action('Refresh devices',async()=>{await inventory();await restoreListen();render();}));
    routing.append(action('Allow microphone device access',async()=>{
      await api('audio_console_authorize_capture');
      const temporary=await navigator.mediaDevices.getUserMedia({audio:true,video:false});
      temporary.getTracks().forEach(t=>t.stop());await inventory();await restoreListen();render();
    }));
    slider(routing,'music_send','Send music',0,1,.01);slider(routing,'mic_send','Send voice',0,1,.01);
    routing.append(action('',()=>sending?stopSend():startSend(),'send-toggle'));
    const note=node('p',null,'audio-note');
    note.textContent=devices.some(d=>d.kind==='audiooutput'&&virtual(d.label))?
      'Send volume is independent of player volume. Routing continues after closing this page. Re-enable microphone processing and sending after restart.':
      'No virtual audio device detected. Install one to route audio to other apps. Equalizer and local monitoring work independently.';
    routing.append(note);columns.append(music,mic,routing);
    const state=node('p',message,'audio-status');state.setAttribute('role','status');
    mount.append(heading,state,columns);refreshState();
  }
  async function open() {
    window.MediaPlayer?.back();window.TimerHUD?.closeAlarm();
    showSheet({key:'audio_console',title:'Equalizer',body:[]});
    mount=node('div',null,'audio-console');document.getElementById('sheet-body').replaceChildren(mount);
    mount.append(node('p','Loading audio settings…'));
    try {await load();await inventory();render();}catch(error){mount.textContent=error.message;}
  }
  navigator.mediaDevices?.addEventListener('devicechange',async()=>{
    try {
      await inventory();
      if(sending&&!devices.some(d=>d.deviceId===settings.send_device)){stopSend();status('Virtual audio device disconnected; sending stopped.');}
      if(micStream&&!devices.some(d=>d.deviceId===settings.input_device)){stopMic();status('Microphone disconnected.');}
      await restoreListen();
      if(mount&&document.body.contains(mount))render();
    } catch(error){status(error.message);}
  });
  window.addEventListener('pagehide',()=>{stopMic();stopSend();localOutput?.pause();context?.close();});
  window.AudioConsole={open,prepareMedia,setVolume,stopMic,stopSend,
    state:()=>{const data=new Float32Array(256),remote=new Float32Array(256);
      musicMeter?.getFloatTimeDomainData(data);sendMeter?.getFloatTimeDomainData(remote);
      return {settings:JSON.parse(JSON.stringify(settings)),capturing:!!micStream,sending,monitoring,context:context?.state,
        musicPeak:Math.max(...data.map(Math.abs)),localVolume:localMuted?0:localLevel,
        localGain:musicLocal?.gain.value,sendGain:musicRemote?.gain.value,
        sendPeak:Math.max(...remote.map(Math.abs)),sendVolume:sending?settings.music_send:0};}};
})();
