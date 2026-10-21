/* Local media only. Native drops stage paths until the user chooses a category. */
(() => {
  const labels = {music:"Music", audio:"Audio", video:"Video", movie:"Movies"};
  const panel = document.createElement("section");
  panel.id = "media-player"; panel.hidden = true; panel.setAttribute("aria-label", "Player");
  panel.innerHTML = `
    <div id="media-drag" class="pywebview-drag-region" aria-hidden="true"></div>
    <header>${Object.entries(labels).map(([id, name]) =>
      `<button id="media-${id}" aria-pressed="${id === "music"}">${name}</button>`).join("")}
      </header>
    <div id="media-layout"><aside id="media-sidebar">
      <button id="media-core-slot" aria-label="Back to main view" title="Back to main view"></button>
      <div id="media-list-title">Music library</div><div id="media-library"></div>
    </aside><div id="media-stage">
      <div id="media-art" aria-hidden="true"><span>♫</span></div>
      <div id="media-now">M Y N · M U S I C</div><p id="media-name" hidden>Choose a song</p>
      <div id="media-lyrics" role="region" aria-label="Lyrics" tabindex="0"><p class="lyrics-empty">Choose a song</p></div>
      <audio id="media-audio-output" preload="metadata"></audio>
      <audio id="media-secondary-audio" preload="metadata"></audio>
      <div id="media-audio-controls" role="group" aria-label="Audio playback controls">
        <div id="media-transport"><button id="media-alarm" aria-label="Set as default alarm sound" title="Set as default alarm sound" disabled></button><div id="media-track-nav">
          <button id="media-prev" aria-label="Previous track"></button>
          <button id="media-toggle" aria-label="Play" disabled></button>
          <button id="media-next" aria-label="Next track"></button>
        </div><div id="media-volume-controls"><button id="media-mute" aria-label="Mute"></button>
          <input id="media-volume" type="range" min="0" max="1" step="0.01" value="1" aria-label="Volume"></div>
        </div>
        <div id="media-timeline"><span id="media-elapsed">0:00</span>
          <input id="media-seek" type="range" min="0" max="1" step="0.1" value="0" disabled aria-label="Playback position">
          <span id="media-duration">0:00</span></div>
      </div>
      <video id="media-screen" controls playsinline preload="metadata" hidden></video>
    </div></div>
    <p id="media-status" role="status" aria-live="polite"></p>`;
  const drop = document.createElement("section");
  drop.id = "media-drop"; drop.hidden = true; drop.setAttribute("aria-label", "Select media category");
  drop.innerHTML = `<h2 id="media-drop-title">Drop onto the desired category</h2><div id="media-drop-options">
    ${Object.entries(labels).map(([id, name]) => `<button data-category="${id}">
      <span>${{music:"♫",audio:"≋",video:"▷",movie:"▤"}[id]}</span><strong>${name}</strong>
      <small>${id === "music" || id === "audio" ? "Audio files" : "Video files"}</small></button>`).join("")}
    </div><p id="media-drop-message"></p><button id="media-drop-cancel">Cancel</button>`;
  const mini = document.createElement("aside");
  mini.id = "media-mini"; mini.hidden = true; mini.setAttribute("aria-label", "Mini player");
  mini.innerHTML = `<button id="media-mini-note" aria-label="Open music player" aria-expanded="false"
      aria-controls="media-mini-box"><svg viewBox="0 0 24 24" aria-hidden="true">
      <path d="M9 17V5l11-2v12M9 8l11-2"/><ellipse cx="6" cy="18" rx="3" ry="2"/>
      <ellipse cx="17" cy="16" rx="3" ry="2"/></svg></button>
    <section id="media-mini-box" hidden>
      <button id="media-mini-info" aria-label="Open full player"><span id="media-mini-name">Choose music</span>
        <small id="media-mini-detail">MYN · MUSIC</small></button>
      <div id="media-mini-timeline"><span id="media-mini-elapsed">0:00</span>
        <input id="media-mini-seek" type="range" min="0" max="1" step="0.1" value="0" disabled aria-label="Mini player position">
        <span id="media-mini-duration">0:00</span></div>
      <div id="media-mini-controls"><button id="media-mini-prev" aria-label="Previous track"></button>
        <button id="media-mini-toggle" aria-label="Play"></button>
        <button id="media-mini-next" aria-label="Next track"></button></div>
      <p id="media-mini-error" role="status" hidden></p>
    </section>`;
  document.body.append(panel, drop, mini);
  const $ = id => document.getElementById(id);
  const audio = $("media-audio-output"), video = $("media-screen"), art = $("media-art");
  const secondaryAudio = $("media-secondary-audio");
  audio.crossOrigin = secondaryAudio.crossOrigin = 'anonymous';
  const playbackVolume = {volume:1, muted:false};
  const icon = path => `<svg viewBox="0 0 24 24" aria-hidden="true">${path}</svg>`;
  const playIcon = icon('<path d="M8 5l11 7-11 7z"/>');
  const pauseIcon = icon('<path d="M8 5v14M16 5v14"/>');
  const soundIcon = '<path d="M11 5L6 9H3v6h3l5 4z"/>';
  $("media-prev").innerHTML = icon('<path d="M6 5v14M18 5L8 12l10 7z"/>');
  $("media-next").innerHTML = icon('<path d="M18 5v14M6 5l10 7-10 7z"/>');
  $("media-alarm").innerHTML = icon('<circle cx="12" cy="13" r="8"/><path d="M12 8v5l3 2M3 5l3-3M18 2l3 3M6 20l-2 2M18 20l2 2"/>');
  $("media-mini-prev").innerHTML = $("media-prev").innerHTML;
  $("media-mini-next").innerHTML = $("media-next").innerHTML;
  const seek = $("media-seek"), volume = $("media-volume"), toggle = $("media-toggle");
  let mode = "music", items = [], loading = false, playGeneration = 0;
  const selections = {music:null, audio:null, video:null, movie:null};
  let playingMode = "music", musicInterrupted = false;
  let pending = null, dropChoice = null, depth = 0, awaitingDrop = false;
  let lyricData = {timed:false, lines:[]}, lyricIndex = -2, lyricRequest = 0, manualLyricsUntil = 0;
  let hoverTimer = null, travelTimer = null, settingsPromise = null, saveTimer = null;
  let settingsRevision = 0, saveChain = Promise.resolve();
  let startingMusic = false;
  const queue = new MusicQueue();
  let alarmSaving = false, alarmRevision = 0, alarmChain = Promise.resolve();
  const alarmRequests = new Map();
  function alarmSettingsResult(result) { alarmRequests.get(result.request)?.(result); }
  async function requestAlarmSettings(changes = {}) {
    const request = crypto.randomUUID();
    const result = await new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        alarmRequests.delete(request); reject(Error('No alarm settings result received. Reopen the player to verify.'));
      }, 15000);
      alarmRequests.set(request, value => { clearTimeout(timer); alarmRequests.delete(request); resolve(value); });
      window.pywebview?.api?.emit?.({event:'alarm_settings', request, ...changes});
    });
    if (!result?.ok) throw Error(result.error || 'Alarm settings have not been confirmed saved.');
    return result.settings;
  }
  function showAlarmSettings(data) {
    window.TimerHUD?.alarmSettings(data);
  }
  function saveAlarmSettings(changes) {
    const revision = ++alarmRevision;
    alarmChain = alarmChain.catch(() => {}).then(() => requestAlarmSettings(changes));
    return alarmChain.then(data => { if (revision === alarmRevision) showAlarmSettings(data); return data; });
  }
  async function loadAlarmSettings() {
    const revision = alarmRevision;
    try { const data = await requestAlarmSettings(); if (revision === alarmRevision) showAlarmSettings(data); }
    catch(e) { window.TimerHUD?.alarmSettingsError(String(e.message || e)); }
  }
  $("media-alarm").onclick = async () => {
    const song = items.find(r => r.id === selections.music && r.mode === 'music');
    if (!song || alarmSaving) return;
    alarmSaving = true; syncMini();
    try {
      const data = await saveAlarmSettings({ringtone_id:song.id});
      status('Default alarm sound set to: ' + data.ringtone_name);
    } catch(e) { status(String(e.message || e)); }
    finally { alarmSaving = false; syncMini(); }
  };
  const isAudio = () => mode === "music" || mode === "audio";
  const mediaFor = category => category === "music" ? audio : category === "audio" ? secondaryAudio : video;
  const viewAudio = () => mode === "audio" ? secondaryAudio : audio;
  // Browsing never owns playback. Decoded playback (not a load request) transfers sound.
  function takePlayback(element, category) {
    if (element !== audio && !audio.paused) musicInterrupted = true;
    if (element === audio) musicInterrupted = false;
    playingMode = category;
    for (const other of [audio, secondaryAudio, video]) if (other !== element) other.pause();
  }
  async function resumeMusic() {
    if (!musicInterrupted || !audio.getAttribute("src") || ringing) return;
    try { await audio.play(); }
    catch(e) { status("Click Play to resume from the previous position."); }
  }
  const status = text => {
    $("media-status").textContent = text;
    $("media-mini-error").textContent = text;
    $("media-mini-error").hidden = !text;
  };
  function positionMini() {
    if (mini.hidden) return;
    const anchor = document.getElementById("media-mini-anchor");
    const rect = anchor?.getBoundingClientRect();
    mini.style.top = (rect?.height ? rect.top : 54) + "px";
    mini.style.left = (rect?.height ? rect.left : 22) + "px";
    window.TimerHUD?.placePopup($("media-mini-box"), $("media-mini-note"), 298);
  }
  function expandMini(expanded) {
    clearTimeout(hoverTimer); clearTimeout(travelTimer);
    if (expanded) window.TimerHUD?.closeAlarm();
    $("media-mini-box").hidden = !expanded;
    $("media-mini-note").setAttribute("aria-expanded", String(expanded));
    positionMini();
  }
  function syncMini() {
    $("media-alarm").disabled = alarmSaving || mode !== 'music' || !items.some(r => r.id === selections.music && r.mode === 'music');
    mini.hidden = !panel.hidden;
    document.body.classList.toggle("media-docked", !mini.hidden);
    $("media-mini-toggle").innerHTML = audio.paused ? playIcon : pauseIcon;
    $("media-mini-toggle").setAttribute("aria-label", audio.paused ? "Play" : "Pause");
    $("media-mini-toggle").disabled = startingMusic;
    $("media-mini-prev").disabled = $("media-mini-next").disabled = !items.some(r => r.mode === "music");
    $("media-mini-name").textContent = items.find(r => r.id === selections.music)?.name || "Choose music";
    $("media-mini-detail").textContent = `Music · ${audio.paused ? "Paused" : "Playing"}`;
    const miniSeek = $("media-mini-seek");
    const duration = Number.isFinite(audio.duration) ? audio.duration : 0;
    miniSeek.max = duration || 1; miniSeek.value = audio.currentTime || 0; miniSeek.disabled = duration <= 0;
    miniSeek.setAttribute("aria-valuetext", `${timestamp(audio.currentTime)} / ${timestamp(duration)}`);
    $("media-mini-elapsed").textContent = timestamp(audio.currentTime);
    $("media-mini-duration").textContent = timestamp(duration);
    fillRange(miniSeek);
    mini.classList.toggle("playing", !audio.paused);
    positionMini();
  }
  const timestamp = value => {
    const n = Number.isFinite(value) ? Math.max(0, Math.floor(value)) : 0;
    return n >= 3600 ? `${Math.floor(n/3600)}:${String(Math.floor(n/60)%60).padStart(2,"0")}:${String(n%60).padStart(2,"0")}`
      : `${Math.floor(n/60)}:${String(n%60).padStart(2,"0")}`;
  };
  function fillRange(element) {
    const percent = 100 * Number(element.value) / Number(element.max);
    element.style.setProperty("--fill", `${Math.max(0, Math.min(100, percent))}%`);
  }
  function syncAudio() {
    const audio = viewAudio();
    const duration = Number.isFinite(audio.duration) ? audio.duration : 0;
    seek.max = duration || 1; seek.disabled = duration <= 0;
    seek.value = Math.min(audio.currentTime || 0, duration);
    seek.setAttribute("aria-valuetext", `${timestamp(audio.currentTime)} / ${timestamp(duration)}`);
    $("media-elapsed").textContent = timestamp(audio.currentTime);
    $("media-duration").textContent = timestamp(duration);
    toggle.disabled = startingMusic || (mode !== "music" && !audio.getAttribute("src"));
    toggle.innerHTML = audio.paused ? playIcon : pauseIcon;
    toggle.setAttribute("aria-label", audio.paused ? "Play" : "Pause");
    window.AudioConsole?.setVolume(playbackVolume.volume, playbackVolume.muted);
    volume.value = playbackVolume.muted ? 0 : playbackVolume.volume;
    volume.setAttribute("aria-valuetext", `${Math.round(Number(volume.value)*100)}%`);
    const silent = playbackVolume.muted || playbackVolume.volume === 0;
    $("media-mute").innerHTML = icon(soundIcon + (silent
      ? '<path d="M16 9l5 6m0-6l-5 6"/>' : '<path d="M15 8a6 6 0 0 1 0 8m3-11a10 10 0 0 1 0 14"/>'));
    $("media-mute").setAttribute("aria-label", silent ? "Unmute" : "Mute");
    fillRange(seek); fillRange(volume);
    syncMini();
    syncLyrics();
  }
  function syncLyrics() {
    if (!lyricData.timed) return;
    let low = 0, high = lyricData.lines.length;
    while (low < high) {
      const mid = (low + high) >> 1;
      if (lyricData.lines[mid].time <= audio.currentTime) low = mid + 1; else high = mid;
    }
    const index = low - 1;
    if (index === lyricIndex) return;
    lyricIndex = index;
    const host = $("media-lyrics");
    [...host.children].forEach((line, i) => {
      line.classList.toggle("current", i === index);
      if (i === index) line.setAttribute("aria-current", "true"); else line.removeAttribute("aria-current");
    });
    const line = host.children[index];
    if (line && performance.now() > manualLyricsUntil) {
      host.scrollTo({top:line.offsetTop - host.clientHeight/2 + line.offsetHeight/2,
        behavior:matchMedia("(prefers-reduced-motion: reduce)").matches ? "instant" : "smooth"});
    }
    curveLyrics();
  }
  let curveFrame = null;
  function curveLyrics() {
    if (curveFrame !== null) return;
    curveFrame = requestAnimationFrame(() => {
      curveFrame = null;
      const host = $("media-lyrics"), height = host.clientHeight;
      if (!height) return;
      for (const line of host.querySelectorAll(".lyrics-line")) {
        const distance = Math.max(-1, Math.min(1,
          (line.offsetTop + line.offsetHeight/2 - host.scrollTop - height/2)/(height/2)));
        line.style.setProperty("--lyric-tilt", `${-distance*48}deg`);
        line.style.setProperty("--lyric-depth", `${-Math.abs(distance)*48}px`);
      }
    });
  }
  function clearLyrics(text = "No lyrics · Add a matching LRC or TXT file to the music folder") {
    lyricData = {timed:false, lines:[]}; lyricIndex = -2; manualLyricsUntil = 0;
    const host = $("media-lyrics"); host.replaceChildren();
    const p = document.createElement("p"); p.className = "lyrics-empty"; p.textContent = text; host.append(p);
    panel.classList.remove("has-lyrics");
  }
  async function loadLyrics(row) {
    const request = ++lyricRequest; clearLyrics("Loading lyrics…");
    if (row.mode !== "music") { clearLyrics(""); return; }
    try {
      const data = await call("media_lyrics", row.id);
      if (request !== lyricRequest || selections.music !== row.id) return;
      if (!data.lines?.length) { clearLyrics(data.error || undefined); return; }
      lyricData = data; lyricIndex = -2;
      const host = $("media-lyrics"); host.replaceChildren(); host.scrollTop = 0;
      for (const entry of data.lines) {
        const p = document.createElement("p"); p.className = "lyrics-line"; p.textContent = entry.text || "♪";
        host.append(p);
      }
      panel.classList.add("has-lyrics"); syncLyrics();
    } catch (e) { if (request === lyricRequest) clearLyrics("Lyrics could not be loaded. Music playback remains available."); }
  }
  $("media-lyrics").addEventListener("wheel", () => { manualLyricsUntil = performance.now()+4000; }, {passive:true});
  $("media-lyrics").addEventListener("scroll", curveLyrics, {passive:true});
  new ResizeObserver(() => { lyricIndex = -2; syncLyrics(); curveLyrics(); }).observe($("media-lyrics"));
  async function toggleAudio(category) {
    if (startingMusic) return;
    const audio = mediaFor(category);
    if (category === "music") musicInterrupted = false;
    if (!audio.paused) { audio.pause(); return; }
    const generation = playGeneration;
    if (!audio.getAttribute("src") && category === "music") {
      startingMusic = true; syncAudio(); status("");
      try {
        if (!await refresh() || generation !== playGeneration) return;
        if (!items.some(row => row.mode === "music")) {
          status("The music folder is empty. Drop music files into MYN first."); return;
        }
        await next(1, true, "music", false);
      } finally { startingMusic = false; syncAudio(); }
      return;
    }
    try { await audio.play(); }
    catch (e) { if (generation === playGeneration) status("Playback unavailable. Select the file again."); }
  }
  toggle.onclick = () => toggleAudio(mode);
  seek.oninput = () => {
    if (!seek.disabled) { viewAudio().currentTime = Number(seek.value); syncAudio(); }
  };
  $("media-mini-seek").oninput = () => {
    if (!$("media-mini-seek").disabled) { audio.currentTime = Number($("media-mini-seek").value); syncAudio(); }
  };
  function saveVolume() {
    settingsRevision++; clearTimeout(saveTimer);
    saveTimer = setTimeout(() => {
      const level = playbackVolume.volume, muted = playbackVolume.muted;
      saveChain = saveChain.catch(() => {}).then(() => call("media_save_settings", level, muted))
        .catch(() => status("Volume changed, but saving failed. You may need to set it again after restart."));
    }, 150);
  }
  async function loadVolume() {
    if (!settingsPromise) {
      const revision = settingsRevision;
      settingsPromise = call("media_settings").then(data => {
        if (settingsRevision === revision) {
          playbackVolume.volume = data.volume;
          playbackVolume.muted = data.muted;
          syncAudio();
        }
      }).catch(() => { settingsPromise = null; status("Cannot read saved player volume."); });
    }
    await settingsPromise;
    await window.AudioConsole?.prepareMedia();
  }
  volume.oninput = () => {
    playbackVolume.volume = Number(volume.value);
    playbackVolume.muted = false; syncAudio(); saveVolume();
  };
  $("media-mute").onclick = () => {
    if (playbackVolume.volume === 0) { playbackVolume.volume = 1; playbackVolume.muted = false; }
    else playbackVolume.muted = !playbackVolume.muted;
    syncAudio(); saveVolume();
  };
  for (const element of [audio, secondaryAudio])
    for (const event of ["timeupdate", "durationchange", "loadedmetadata", "play", "pause", "ended", "emptied", "volumechange", "error"])
      element.addEventListener(event, syncAudio);
  syncAudio();
  async function call(method, ...args) {
    const api = window.pywebview?.api;
    if (!api?.[method]) throw Error("Player is not ready. Try again shortly.");
    return api[method](...args);
  }
  function immersive(value) {
    document.body.classList.toggle("music-immersive", value);
    panel.classList.toggle("immersive", value);
  }
  let entrance = null, entranceAnimation = null;
  const entranceDuration = 550;
  function visible() {
    const opening = panel.hidden;
    positionMini();
    panel.hidden = false; expandMini(false); window.TimerHUD?.closeAlarm();
    document.body.classList.add("media-open"); syncMini();
    if (!opening) return;
    entranceAnimation?.cancel();
    const reduced = matchMedia("(prefers-reduced-motion: reduce)").matches;
    entrance = {start: performance.now(), from: null, duration: reduced ? 0 : entranceDuration};
    if (reduced) return;
    entranceAnimation = panel.animate([
      {opacity:0}, {opacity:1}
    ], {duration:400, easing:"linear"});
  }
  function coreMotion(now, current) {
    if (!entrance) return null;
    if (entrance.from === null) entrance.from = current;
    const progress = entrance.duration ? Math.min(1, Math.max(0, (now-entrance.start)/entrance.duration)) : 1;
    return entrance.from + (1-entrance.from)*progress;
  }
  function halt() {
    ++playGeneration; musicInterrupted = false;
    audio.pause(); secondaryAudio.pause(); video.pause(); art.classList.remove("playing");
  }
  function list() {
    const host = $("media-library"); host.replaceChildren();
    const rows = items.filter(row => row.mode === mode);
    $("media-list-title").textContent = labels[mode] + " library · " + rows.length;
    if (!rows.length) {
      const p = document.createElement("p"); p.id = "media-empty";
      p.textContent = `No ${labels[mode]} yet. Drop files onto MYN and choose a category.`; host.append(p);
    }
    for (const [index, row] of rows.entries()) {
      const b = document.createElement("button");
      const number = document.createElement("span"); number.className = "media-track-number";
      number.textContent = String(index + 1).padStart(2, "0"); b.append(number);
      const name = document.createElement("span"); name.className = "media-track-name"; name.textContent = row.name;
      if (row.lyrics_file) b.title = "Lyrics: " + row.lyrics_file;
      b.append(name); b.setAttribute("aria-pressed", String(row.id === selections[mode]));
      b.onclick = () => play(row); host.append(b);
    }
    syncMini();
  }
  async function play(row, navigated = false, reveal = true) {
    const generation = ++playGeneration;
    const requestedView = mode;
    try { await loadVolume(); }
    catch(error) {status(error.message || 'Audio device is not ready.');return false;}
    if (generation !== playGeneration) return false;
    selections[row.mode] = row.id;
    if (reveal && mode === requestedView) mode = row.mode;
    if (row.mode === "music" && !navigated) { queue.sync(items.filter(r => r.mode === "music").map(r => r.id)); queue.select(row.id); }
    if (row.mode === "music") loadLyrics(row);
    const element = mediaFor(row.mode); element.dataset.mode = row.mode; element.src = row.url;
    status(""); modes(); list();
    try { await element.play(); return !element.paused; }
    catch (e) {
      if (generation !== playGeneration) return;
      status(e.name === "NotAllowedError" ? "Click Play to start playback." :
        "Cannot play this file. Check that it is complete and uses a supported format.");
      return false;
    }
  }
  function modes() {
    audio.hidden = true; secondaryAudio.hidden = true;
    video.hidden = isAudio() || video.dataset.mode !== mode; art.hidden = !isAudio();
    $("media-audio-controls").hidden = !isAudio();
    $("media-lyrics").hidden = mode !== "music";
    $("media-now").textContent = "M Y N · " + mode.toUpperCase();
    for (const id of Object.keys(labels)) $(`media-${id}`).setAttribute("aria-pressed", String(mode === id));
    $("media-prev").setAttribute("aria-label", mode === "music" ? "Previous track" : "Previous");
    $("media-next").setAttribute("aria-label", mode === "music" ? "Next track" : "Next");
    $("media-name").textContent = items.find(r => r.id === selections[mode])?.name || `Choose a ${labels[mode]} file`;
    immersive(mode === "music" && selections.music !== null && !panel.hidden);
    art.classList.toggle("playing", !viewAudio().paused);
    syncAudio(); curveLyrics();
  }
  function switchMode(next) {
    if (mode === next) return;
    mode = next; modes(); list(); status("");
    if (mode === "music") { lyricIndex = -2; syncLyrics(); resumeMusic(); }
  }
  async function refresh() {
    try {
      const data = await call("media_list"); items = data.items || [];
      queue.sync(items.filter(r => r.mode === "music").map(r => r.id)); list();
      return true;
    }
    catch (e) { status(String(e.message || e)); return false; }
  }
  async function open() {
    visible(); modes();
    try { await loadVolume(); }
    catch(error) {status(error.message || 'Audio device is not ready.');}
    await refresh();
  }
  function back() {
    // Audio continues on the main screen; hidden videos pause.
    entranceAnimation?.cancel(); entranceAnimation = null; entrance = null;
    video.pause(); immersive(false); panel.hidden = true; document.body.classList.remove("media-open");
    expandMini(false); syncMini();
  }
  function close() { halt(); back(); syncMini(); }

  // Separate from the music queue: looping alarms never advance to another song.
  const alarmAudio = document.createElement('audio');
  alarmAudio.id = 'alarm-music-output'; alarmAudio.loop = true;
  alarmAudio.volume = .8;
  document.body.append(alarmAudio);
  let ringing = null, ringGeneration = 0, ringPaused = false, ringLoading = false, ringFailed = false;
  let ringTimer = null;
  function clearRingTimer() { clearTimeout(ringTimer); ringTimer = null; }
  function armRingTimer(generation) {
    if (ringTimer !== null || ringPaused || ringFailed) return;
    ringTimer = setTimeout(() => {
      ringTimer = null;
      if (generation === ringGeneration && !ringPaused) ringFail();
    }, 15000);
  }
  function ringReport(ok) {
    if (ringing) window.pywebview?.api?.emit?.({event:'alarm_music_result', id:ringing.id, ok});
  }
  function ringFail() {
    if (!ringing || ringFailed) return;
    clearRingTimer();
    ringFailed = true; alarmAudio.pause(); ringReport(false);
  }
  async function ringPlay(generation) {
    if (ringPaused || ringFailed || ringLoading || !ringing || generation !== ringGeneration) return;
    armRingTimer(generation);
    musicInterrupted = false; audio.pause(); secondaryAudio.pause(); video.pause();
    try {
      await alarmAudio.play();
      if (generation === ringGeneration && ringPaused) alarmAudio.pause();
    } catch(e) { if (generation === ringGeneration && !ringPaused) ringFail(); }
  }
  async function alarmMusic(message) {
    if (typeof message.volume === 'number' && Number.isFinite(message.volume))
      alarmAudio.volume = Math.max(0, Math.min(1, message.volume));
    const next = (message.alarms || [])[0] || null;
    ringPaused = !!message.paused;
    if (next?.id === ringing?.id && next?.track === ringing?.track) {
      if (ringPaused) { clearRingTimer(); alarmAudio.pause(); }
      else if (ringLoading) armRingTimer(ringGeneration);
      else if (alarmAudio.paused) await ringPlay(ringGeneration);
      return;
    }
    const generation = ++ringGeneration;
    clearRingTimer();
    alarmAudio.pause(); alarmAudio.removeAttribute('src'); alarmAudio.load();
    ringing = next; ringFailed = false; ringLoading = false;
    if (!ringing) return;
    armRingTimer(generation);
    musicInterrupted = false; audio.pause(); secondaryAudio.pause(); video.pause(); ringLoading = true;
    try {
      const result = await call('media_list');
      const song = result.items.find(r => r.id === next.track && r.mode === 'music');
      if (!song) throw Error('Music alarm file no longer exists');
      if (generation !== ringGeneration) return;
      alarmAudio.muted = false; alarmAudio.src = song.url;
      ringLoading = false;
      await ringPlay(generation);
    } catch(e) { if (generation === ringGeneration) { ringLoading = false; ringFail(); } }
  }
  alarmAudio.addEventListener('error', () => { if (alarmAudio.error) ringFail(); });
  alarmAudio.addEventListener('playing', () => { clearRingTimer(); ringReport(true); });
  function imported(result) {
    loading = false; visible();
    if (!pending) resetDrop();
    if (result.items) items = result.items;
    if (result.added?.length) switchMode(result.added[0].mode);
    list();
    status([result.added?.length ? `Saved ${result.added.length} files` : "",
      ...(result.errors || [])].filter(Boolean).join("；"));
  }
  function next(delta, wrap = true, category = mode, reveal = true) {
    const rows = items.filter(r => r.mode === category);
    if (!rows.length) return;
    if (category === "music") {
      queue.sync(rows.map(r => r.id));
      const id = delta < 0 ? queue.previous() : queue.next();
      const row = rows.find(r => r.id === id);
      // The queue already recorded navigation, including forward history.
      if (row) return play(row, true, reveal);
      return;
    }
    const i = rows.findIndex(r => r.id === selections[category]);
    if (!wrap && i + delta >= rows.length) return;
    return play(rows[(Math.max(-1, i) + delta + rows.length) % rows.length], false, reveal);
  }
  for (const id of Object.keys(labels)) $(`media-${id}`).onclick = () => { switchMode(id); refresh(); };
  $("media-core-slot").onclick = back;
  $("media-prev").onclick = () => next(-1);
  $("media-next").onclick = () => next(1);
  const openMusic = async () => { switchMode("music"); await open(); };
  $("media-mini-note").onclick = openMusic;
  $("media-mini-note").onpointerenter = () => {
    window.TimerHUD?.closeAlarm();
    clearTimeout(hoverTimer); clearTimeout(travelTimer);
    if (!$("media-mini-box").hidden) return;
    hoverTimer = setTimeout(() => {
      if (mini.hidden) return;
      expandMini(true);
    }, 300);
  };
  $("media-mini-note").onpointerleave = () => {
    clearTimeout(hoverTimer);
    if (!$("media-mini-box").hidden) travelTimer = setTimeout(() => expandMini(false), 1000);
  };
  $("media-mini-box").onpointerenter = () => { clearTimeout(travelTimer); };
  $("media-mini-box").onpointerleave = () => expandMini(false);
  $("media-mini-prev").onclick = () => next(-1, true, "music", false);
  $("media-mini-next").onclick = () => next(1, true, "music", false);
  $("media-mini-toggle").onclick = () => toggleAudio("music");
  $("media-mini-info").onclick = openMusic;
  document.addEventListener("pointerdown", e => {
    if (!mini.contains(e.target)) expandMini(false);
  });
  window.addEventListener("resize", positionMini);
  async function command(message) {
    let result;
    try {
      if (message.expires && Date.now() > message.expires) throw Error("Playback request expired. Try again.");
      const action = message.action;
      if (action === "play") {
        await open(); switchMode("music");
        if (!items.some(r => r.mode === "music")) throw Error("The music folder is empty. Drop music files into MYN first.");
        if (!await next(1)) throw Error("Music could not start. Check the file format.");
      } else if (action === "pause") { musicInterrupted = false; audio.pause(); }
      else if (action === "resume") {
        if (!audio.getAttribute("src")) throw Error("No music selected. Play a song first.");
        await loadVolume(); await audio.play();
      } else if (action === "next" || action === "previous") {
        if (!await next(action === "next" ? 1 : -1, true, "music", false)) throw Error("No playable music.");
      } else if (action !== "status") throw Error("Invalid music action.");
      result = {ok:true, playing:!audio.paused, name:items.find(r => r.id === selections.music)?.name || null,
        volume:playbackVolume.volume, muted:playbackVolume.muted, order:"shuffle_cycle"};
    } catch (e) { result = {ok:false, error:String(e.message || e)}; status(result.error); }
    await window.pywebview?.api?.emit?.({event:"music_result", request:message.request, ...result});
    return result;
  }
  for (const element of [audio, secondaryAudio, video]) {
    element.addEventListener("playing", () => takePlayback(element,
      element === audio ? "music" : element === secondaryAudio ? "audio" : element.dataset.mode));
    const report = () => window.pywebview?.api?.emit?.({
      event: "media_state", playing: !audio.paused || !secondaryAudio.paused || !video.paused
    });
    for (const name of ["play", "pause", "ended", "error"]) element.addEventListener(name, report);
    element.addEventListener("error", () => status("Unsupported encoding or damaged file. The original was preserved; try MP3/WAV or H.264 MP4/WebM."));
    element.addEventListener("ended", () => {
      const category = element === audio ? "music" : element === secondaryAudio ? "audio" : element.dataset.mode;
      if (playingMode === category) next(1, false, category, false);
    });
  }
  for (const element of [audio, secondaryAudio]) {
    for (const event of ["playing", "pause"])
      element.addEventListener(event, () => art.classList.toggle("playing", !viewAudio().paused));
  }
  audio.addEventListener("playing", () => {
    if (mode === "music" && !panel.hidden) immersive(true);
  });
  function highlight(category) {
    dropChoice = category;
    for (const b of drop.querySelectorAll("[data-category]")) b.classList.toggle("target", b.dataset.category === category);
  }
  function resetDrop() {
    drop.hidden = true; pending = null; awaitingDrop = false; depth = 0; highlight(null);
  }
  async function choose(category) {
    if (!pending || loading) return;
    const token = pending; resetDrop(); loading = true; visible(); status("Saving to " + labels[category] + "…");
    try { imported(await call("media_import", token, category)); }
    catch (e) { loading = false; status(String(e.message || e)); }
  }
  function dropped(token, names) {
    pending = token; awaitingDrop = false; drop.hidden = false;
    $("media-drop-title").textContent = "Which category should contain these files?";
    $("media-drop-message").textContent = `${names.length} files · ${names.slice(0, 3).join("、")}`;
    if (dropChoice) choose(dropChoice);
  }
  for (const b of drop.querySelectorAll("[data-category]")) b.onclick = () => choose(b.dataset.category);
  function cancelDrop() {
    if (pending) call("media_cancel", pending).catch(() => {});
    resetDrop();
  }
  $("media-drop-cancel").onclick = cancelDrop;
  document.addEventListener("keydown", e => {
    if (e.key !== "Escape") return;
    if (!drop.hidden) { cancelDrop(); e.stopImmediatePropagation(); }
    else if (!panel.hidden) { back(); e.stopImmediatePropagation(); }
    else if (!$("media-mini-box").hidden) { expandMini(false); $("media-mini-note").focus(); e.stopImmediatePropagation(); }
  }, true);
  const files = e => Array.from(e.dataTransfer?.types || []).includes("Files");
  document.addEventListener("dragenter", e => {
    if (!files(e)) return;
    if (depth++ === 0 && !pending) {
      highlight(null); $("media-drop-title").textContent = "Drop onto the desired category";
      $("media-drop-message").textContent = "Original files are kept"; drop.hidden = false;
    }
  });
  document.addEventListener("dragover", e => {
    if (!files(e)) return;
    e.preventDefault(); e.dataTransfer.dropEffect = "copy";
    highlight(e.target.closest?.("[data-category]")?.dataset.category || null);
  });
  document.addEventListener("dragleave", e => {
    if (files(e) && --depth <= 0) {
      depth = 0; if (!pending && !awaitingDrop) { drop.hidden = true; highlight(null); }
    }
  });
  document.addEventListener("drop", e => {
    e.preventDefault(); depth = 0; awaitingDrop = true;
    highlight(e.target.closest?.("[data-category]")?.dataset.category || null);
    drop.hidden = false; $("media-drop-message").textContent = "Reading files…";
  });
  window.MediaPlayer = {open, close, back, command, alarmMusic, alarmSettingsResult, loadAlarmSettings, saveAlarmSettings, imported, dropped, positionMini, isDocked: () => !mini.hidden,
    closeMini: () => expandMini(false), coreMotion,
    coreTarget: () => $("media-core-slot").getBoundingClientRect()};
})();
