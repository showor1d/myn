/* MYN's main window, drawn by the browser engine (WebView2) on the GPU.
 *
 * The page only draws and reports what the user does. MYN's Python side decides
 * what is shown (messages, voice notices, the input bar) and acts on the events
 * this page emits (send, menu choices, window buttons, push-to-talk...).
 *
 *   Python -> page:  MYN.receive({cmd: "chat" | "fade" | "voice" | "input" | "entry", ...})
 *   page -> Python:  emit({event: "ready" | "send" | "menu" | "close" | ..., ...})
 */
"use strict";

// ---------------------------------------------------------------- bridge
const outbox = [];
function emit(message) {
  const api = window.pywebview && window.pywebview.api;
  if (api && api.emit) api.emit(message);
  else outbox.push(message);             // read by tests when there is no host
}
window.__mynOutbox = outbox;

// ---------------------------------------------------------------- helpers
function mulberry32(seed) {
  return function () {
    seed |= 0; seed = (seed + 0x6D2B79F5) | 0;
    let t = Math.imul(seed ^ (seed >>> 15), 1 | seed);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}
const TAU = Math.PI * 2;
const clamp = (v) => (v < 0 ? 0 : v > 1 ? 1 : v);
const DEEP_BLUE = [30, 85, 157], ELECTRIC = [29, 147, 232], CYAN = [56, 215, 246], ICE = [190, 247, 255];
const NAVY = [16, 42, 128], ROYAL = [36, 92, 228];  // the core's deep blues
const rgba = (c, a) => `rgba(${c[0]},${c[1]},${c[2]},${clamp(a).toFixed(3)})`;

function glowSprite(color, size = 64) {
  const c = document.createElement("canvas");
  c.width = c.height = size;
  const g = c.getContext("2d");
  const grad = g.createRadialGradient(size / 2, size / 2, 0, size / 2, size / 2, size / 2);
  grad.addColorStop(0, rgba(color, 1));
  grad.addColorStop(.18, rgba(color, .55));
  grad.addColorStop(.45, rgba(color, .14));
  grad.addColorStop(1, rgba(color, 0));
  g.fillStyle = grad;
  g.fillRect(0, 0, size, size);
  return c;
}
const SPRITES = { deep: glowSprite(DEEP_BLUE), electric: glowSprite(ELECTRIC), cyan: glowSprite(CYAN), ice: glowSprite(ICE),
  navy: glowSprite(NAVY), royal: glowSprite(ROYAL) };

// ---------------------------------------------------------------- the neural core
// A dense, living network: an outer shell and a deeper inner layer of neurons,
// curved fibres between neighbours, long "highways" across the ball, a fur of fine
// filaments, drifting dust, and chain reactions of firing that run along the fibres.
const rng = mulberry32(20261016);
const gauss = () => { let u = 0, v = 0; while (!u) u = rng(); while (!v) v = rng(); return Math.sqrt(-2 * Math.log(u)) * Math.cos(TAU * v); };
const dist2 = (p, o) => (p[0] - o[0]) ** 2 + (p[1] - o[1]) ** 2 + (p[2] - o[2]) ** 2;

const points = [];
{
  const shell = 92;  // evenly spaced outer shell keeps the silhouette round
  for (let i = 0; i < shell; i++) {
    const y = 1 - 2 * (i + .5) / shell, ring = Math.sqrt(1 - y * y);
    const a = i * 2.39996323 + (rng() * .3 - .15), r = .88 + rng() * .12;
    points.push([Math.cos(a) * ring * r, y * r, Math.sin(a) * ring * r]);
  }
  let guard = 0;
  while (points.length < 164 && guard++ < 20000) {
    const x = rng() * 2 - 1, y = rng() * 2 - 1, z = rng() * 2 - 1, l = Math.hypot(x, y, z);
    if (!(l > .05 && l <= 1)) continue;
    const r = .08 + .72 * Math.pow(rng(), .7), p = [x / l * r, y / l * r, z / l * r];
    if (points.every((o) => dist2(p, o) > .022)) points.push(p);
  }
}
const N = points.length;
const phases = points.map(() => rng() * TAU);
const neighbours = points.map(() => []);
const fibres = [];
{
  const seen = new Set();
  const link = (i, j, long) => {
    const key = Math.min(i, j) * 1000 + Math.max(i, j);
    if (i === j || seen.has(key)) return;
    seen.add(key);
    const f = { a: i, b: j, long, phase: rng() * TAU, period: 8 + rng() * 12,
      bend: [rng() * .32 - .16, rng() * .32 - .16], base: long ? .35 + .3 * rng() : .5 + .5 * rng(),
      dormant: rng() < (long ? .6 : .3), strength: 0, c: null };
    neighbours[i].push([j, fibres.length]); neighbours[j].push([i, fibres.length]);
    fibres.push(f);
  };
  points.forEach((p, i) => {
    const near = points.map((o, j) => [dist2(p, o), j]).sort((a, b) => a[0] - b[0]).slice(1, 6).map((e) => e[1]);
    for (const j of near.slice(0, 3 + (i % 2))) link(i, j, false);
  });
  for (let k = 0; k < 14; k++) {  // long highways right across the ball
    const i = Math.floor(rng() * 92), j = Math.floor(rng() * 92);
    if (dist2(points[i], points[j]) > 1.4) link(i, j, true);
  }
}
const filaments = [];  // fine short strands around every neuron
points.forEach(([px, py, pz], i) => {
  const outer = px * px + py * py + pz * pz > .6;
  for (let k = 0; k < (outer ? 5 : 3); k++) {
    const d = [px * (outer ? 1 : 0) + gauss() * .7, py * (outer ? 1 : 0) + gauss() * .7, pz * (outer ? 1 : 0) + gauss() * .7];
    const l = Math.hypot(...d) || 1;
    filaments.push({ n: i, dir: d.map((v) => v / l), length: (outer ? .07 : .05) + rng() * (outer ? .16 : .08),
      curl: rng() * 2 - 1, phase: rng() * TAU, fork: rng() < .35 });
  }
});
const dust = Array.from({ length: 220 }, () => {  // motes floating inside the ball
  let p; do { p = [rng() * 2 - 1, rng() * 2 - 1, rng() * 2 - 1]; } while (Math.hypot(...p) > 1.05);
  return { p, tw: rng() * TAU, size: .4 + rng() * .9 };
});
const haze = Array.from({ length: 7 }, () => ({ p: [gauss() * .3, gauss() * .3, gauss() * .3], size: .55 + rng() * .5, tw: rng() * TAU }));

// Firing: a neuron flares and sends impulses along its fibres; arriving impulses
// may set off the next neuron, so chain reactions run through the network.
const flash = new Float32Array(N);
const pulses = [];
let nextFire = 0;
function fire(i, generation, t) {
  flash[i] = 1;
  if (generation > 5) return;
  for (const [j, fi] of neighbours[i]) {
    if (rng() < Math.min(.95, (generation === 0 ? .9 : .55) * look.chain)) {
      const f = fibres[fi];
      pulses.push({ f: fi, from: i, to: j, start: t + rng() * .08,
        dur: ((f.long ? 1.3 : .55) + rng() * .3) * look.travel, gen: generation + 1 });
    }
  }
}

function view(t) {
  const yaw = t * .06, pitch = -.20 + .07 * Math.sin(t * .11);
  return [Math.cos(yaw), Math.sin(yaw), Math.cos(pitch), Math.sin(pitch)];
}
function turn(x, y, z, v) {
  const [cy, sy, cp, sp] = v;
  const x2 = x * cy + z * sy, z2 = -x * sy + z * cy;
  return [x2, y * cp - z2 * sp, y * sp + z2 * cp];
}
function bez(p0, p1, p2, p3, t) {
  const u = 1 - t;
  return u * u * u * p0 + 3 * u * u * t * p1 + 3 * u * t * t * p2 + t * t * t * p3;
}

// ---------------------------------------------------------------- flying stars
const starRng = mulberry32(19771214);
const TONES = [ICE, ICE, ICE, [170, 205, 255], [255, 236, 214]];
const stars = Array.from({ length: 170 }, () => respawn({}, .06 + starRng() * .94));
function respawn(s, depth = 1) {
  const a = starRng() * TAU, spread = .12 + 2.4 * Math.sqrt(starRng());
  s.x = Math.cos(a) * spread; s.y = Math.sin(a) * spread; s.z = depth;
  s.tone = TONES[Math.floor(starRng() * TONES.length)]; s.power = .55 + .45 * starRng();
  return s;
}

// ---------------------------------------------------------------- drawing
const canvas = document.getElementById("scene");
const ctx = canvas.getContext("2d");
const coreLayer = document.createElement("canvas");
const coreCtx = coreLayer.getContext("2d");
const bloom = document.createElement("canvas");
const bloomCtx = bloom.getContext("2d");
let W = 0, H = 0, DPR = 1;
function resize() {
  DPR = Math.min(window.devicePixelRatio || 1, 2.5);
  W = window.innerWidth; H = window.innerHeight;
  for (const c of [canvas, coreLayer]) { c.width = Math.round(W * DPR); c.height = Math.round(H * DPR); }
  bloom.width = Math.round(W * DPR / 2); bloom.height = Math.round(H * DPR / 2);
  layoutText();
}
window.addEventListener("resize", resize);

const plate = document.getElementById("plate");
let last = null;
const frameTimes = [];

let starGain = 1;
function drawStars(t, dt, cx, cy) {
  const speed = .05 * (1 + .3 * Math.sin(t * .06)), focal = 120;  // slower drift (2026-10-17; was .085)
  ctx.globalCompositeOperation = "lighter";
  ctx.lineCap = "round";
  for (const s of stars) {
    s.z -= dt * speed;
    const z = s.z, x = cx + s.x / z * focal, y = cy + s.y / z * focal;
    if (z < .035 || x < -40 || x > W + 40 || y < -40 || y > H + 40) { respawn(s); continue; }
    const tail = Math.min(1, z + .9 * speed * .2);
    let tx = cx + s.x / tail * focal, ty = cy + s.y / tail * focal;
    const len = Math.hypot(x - tx, y - ty);
    if (len > 30) { tx = x + (tx - x) * 30 / len; ty = y + (ty - y) * 30 / len; }
    const near = 1 - z;
    const a = s.power * near ** 1.6 * Math.min(1, near / .12) * .8 * starGain;
    if (a < .02) continue;
    const grad = ctx.createLinearGradient(tx, ty, x, y);
    grad.addColorStop(0, rgba(s.tone, 0));
    grad.addColorStop(1, rgba(s.tone, a));
    ctx.strokeStyle = grad;
    ctx.lineWidth = .5 + 1.3 * near * near;
    ctx.beginPath(); ctx.moveTo(tx, ty); ctx.lineTo(x, y); ctx.stroke();
    const r = 1.5 + 4 * near * near;
    ctx.globalAlpha = a * .7;
    ctx.drawImage(SPRITES.ice, x - r, y - r, 2 * r, 2 * r);
    ctx.globalAlpha = 1;
  }
}

// ---------------------------------------------------------------- the energy membrane
// A thin film of light around the core. It reacts to what MYN is doing (mode from
// Python: idle / listening / thinking / speaking): your voice makes it ripple and
// sets neurons twitching; actual playback amplitude drives the pulse and the
// heart sends impulses out through the network.
const coreState = { mode: "idle", level: 0, target: 0, levelAt: -10, waves: [], lastWave: 0,
  lastSyllable: 0, wakeAt: -10, glow: .45 };
const MODE_GLOW = { idle: .35, listening: .9, thinking: 1, speaking: .95 };
// How the whole core looks in each state (user, 2026-10-17): quietly noting things =
// fewer sparks, a little deeper and darker; listening = brighter, a few more sparks;
// thinking = the most sparks, most active and brightest; speaking sits in between.
//   light: brightness of the core layer   sat: colour depth   bloom: glow strength
//   spark: how often new chains start      chain: how far a chain spreads
//   travel: time an impulse takes along a fibre (larger = slower)
const MODE_LOOK = {
  idle:      { light: .72, sat: 1.22, bloom: .55, spark: .4,  chain: .7,  travel: 1.45 },
  listening: { light: 1.0, sat: 1.05, bloom: .9,  spark: 1.4, chain: 1.0, travel: 1.1 },
  speaking:  { light: 1.1, sat: 1.0,  bloom: 1.0, spark: 1.8, chain: 1.05, travel: 1.0 },
  thinking:  { light: 1.22, sat: 1.0, bloom: 1.25, spark: 3.0, chain: 1.3, travel: .75 },
};
const look = { ...MODE_LOOK.idle };
// Changing state (user, 2026-10-17): becoming more active happens quickly (about a
// quarter second), calming down happens slowly (a few seconds), each value on its own.
const LIVELIER = { light: 1, sat: -1, bloom: 1, spark: 1, chain: 1, travel: -1 };
const RISE_RATE = 4.5, SETTLE_RATE = .55;
function ease(current, goal, livelier, dt) {
  const rate = (goal - current) * livelier > 0 ? RISE_RATE : SETTLE_RATE;
  return current + (goal - current) * Math.min(1, dt * rate);
}
function updateLook(dt) {
  const goal = MODE_LOOK[coreState.mode] || MODE_LOOK.idle;
  for (const key in goal) look[key] = ease(look[key], goal[key], LIVELIER[key], dt);
}
const rings = [  // great circles on the membrane, each tilted and spinning on its own
  { tilt: .35, spin: .05, phase: 0 }, { tilt: 1.15, spin: -.04, phase: 1.7 }, { tilt: 2.2, spin: .03, phase: 3.1 },
];

function drawMembrane(g, t, dt, cx, cy, radius, v) {
  const st = coreState;
  if (t - st.levelAt > .35) st.target = 0;
  st.level += (st.target - st.level) * Math.min(1, dt * 14);
  st.glow = ease(st.glow, MODE_GLOW[st.mode] || .35, 1, dt);
  const speech = Math.sqrt(window.CoreAudioRing?.levels().output || 0);
  const drive = Math.max(st.level, speech * .8);
  if (drive > .32 && t - st.lastWave > .24) { st.waves.push({ t0: t, power: drive }); st.lastWave = t; }
  // Neurons answer: twitches to your voice, chains while thinking, impulses from the heart while speaking.
  if (st.mode === "listening" && st.level > .18 && rng() < st.level * dt * 10) fire(Math.floor(rng() * N), 4, t);
  if (st.mode === "thinking" && rng() < dt * 4) fire(Math.floor(rng() * N), 2, t);
  if (st.mode === "speaking" && speech > .62 && t - st.lastSyllable > .28 + rng() * .25) {
    fire(92 + Math.floor(rng() * (N - 92)), 2, t);
    st.lastSyllable = t;
  }
  const arrive = st.wakeAt + WAKE.gather + WAKE.open;  // the ring reaches the membrane
  const wake = t < arrive ? .25 * clamp((t - st.wakeAt) / (arrive - st.wakeAt)) : Math.exp(-(t - arrive) * 2.2);
  const glow = st.glow + .6 * wake;
  const R = radius * 1.2 * (1 + .03 * speech + .025 * st.level);

  // The film itself: faint inside, brighter towards the rim (light grazing a thin shell).
  const film = g.createRadialGradient(cx, cy, R * .62, cx, cy, R * 1.02);
  film.addColorStop(0, rgba(NAVY, 0));
  film.addColorStop(.78, rgba(NAVY, .05 * glow));
  film.addColorStop(.96, rgba(ROYAL, .14 * glow));
  film.addColorStop(1, rgba(NAVY, 0));
  g.fillStyle = film;
  g.beginPath(); g.arc(cx, cy, R * 1.02, 0, TAU); g.fill();

  // Great circles drifting over the surface; the near half is brighter.
  for (const ring of rings) {
    const front = new Path2D(), back = new Path2D();
    let prevFront = null;
    for (let k = 0; k <= 96; k++) {
      const a = k / 96 * TAU + t * ring.spin + ring.phase;
      let x = Math.cos(a), y = Math.sin(a) * Math.cos(ring.tilt), z = Math.sin(a) * Math.sin(ring.tilt);
      [x, y, z] = turn(x, y, z, v);
      const s = R * (1 + .04 * z);
      const px = cx + x * s, py = cy - y * s, isFront = z > 0;
      const path = isFront ? front : back;
      if (prevFront !== isFront) path.moveTo(px, py); else path.lineTo(px, py);
      prevFront = isFront;
    }
    g.lineWidth = .6;
    g.strokeStyle = rgba(ROYAL, .10 * glow); g.stroke(back);
    g.strokeStyle = rgba(ELECTRIC, .20 * glow); g.stroke(front);
  }

  // The rim, rippling with the voice (yours while listening, MYN's while speaking).
  const rim = new Path2D();
  for (let k = 0; k <= 180; k++) {
    const a = k / 180 * TAU;
    const ripple = drive * (.032 * Math.sin(6 * a - 9 * t) + .02 * Math.sin(11 * a + 7 * t + 1.3) + .012 * Math.sin(17 * a - 13 * t))
      + .006 * Math.sin(3 * a + t * .8);
    const r = R * (1 + ripple);
    const px = cx + Math.cos(a) * r, py = cy + Math.sin(a) * r;
    if (k === 0) rim.moveTo(px, py); else rim.lineTo(px, py);
  }
  g.strokeStyle = rgba(NAVY, .30 * glow); g.lineWidth = 7; g.stroke(rim);
  g.strokeStyle = rgba(ROYAL, .34 * glow); g.lineWidth = 2.2; g.stroke(rim);
  g.strokeStyle = rgba(CYAN, clamp(.22 * glow + .35 * drive + .4 * wake)); g.lineWidth = .8; g.stroke(rim);

  // Waves running outward from the film.
  for (let k = st.waves.length - 1; k >= 0; k--) {
    const w = st.waves[k], age = t - w.t0;
    if (age > 1.5) { st.waves.splice(k, 1); continue; }
    const a = w.power * (1 - age / 1.5) ** 2;
    const rr = R * (1 + age * .32);
    g.strokeStyle = rgba(ELECTRIC, .42 * a); g.lineWidth = 2.4 * (1 - age / 1.5) + .4;
    g.beginPath(); g.arc(cx, cy, rr, 0, TAU); g.stroke();
  }
  if (st.waves.length > 24) st.waves.splice(0, st.waves.length - 24);
}

const smoothstep = (a, b, x) => { const k = clamp((x - a) / (b - a)); return k * k * (3 - 2 * k); };
const easeOutCubic = (x) => 1 - (1 - x) ** 3;
const easeOutBack = (x) => { const c1 = 1.5, c3 = c1 + 1; return 1 + c3 * (x - 1) ** 3 + c1 * (x - 1) ** 2; };
// Outer neurons arrive a little later than inner ones: the core forms like an expanding wavefront.
// ---------------------------------------------------------------- waking up (wake word)
// About 1.6 s: the core draws in for a moment (gathering attention), then springs open;
// the heart flares, a ring of light runs from the centre out to the membrane and the
// neurons light up in order as it passes (inside first, like eyes opening); when it
// reaches the membrane two soft waves go out, and the core settles into listening.
const WAKE = { gather: .14, open: .5, total: 1.6 };
const centreDistance = points.map((p) => Math.hypot(...p));
const woken = new Uint8Array(N);
let wakeWaves = 0;
function wakeShape(t) {
  const u = t - coreState.wakeAt;
  if (u < 0 || u > WAKE.total) return { scale: 1, heart: 0, ring: -1, u: -1 };
  let scale;
  if (u < WAKE.gather) scale = 1 - .045 * Math.sin(Math.PI / 2 * u / WAKE.gather);   // draw in
  else {
    const w = u - WAKE.gather;
    scale = 1 - .045 * Math.cos(w * 13) * Math.exp(-w * 7) + .055 * Math.sin(w * 13) * Math.exp(-w * 5);
  }
  const heart = u < WAKE.gather ? .4 * u / WAKE.gather : Math.exp(-(u - WAKE.gather) * 3.2) * 1.3;
  const ring = u < WAKE.gather ? -1 : clamp((u - WAKE.gather) / WAKE.open);  // 0..1, centre → membrane
  return { scale, heart, ring, u };
}
function startWake(now) {
  coreState.wakeAt = now;
  woken.fill(0);
  wakeWaves = 0;
}

const growDelay = points.map((p, i) => .28 * Math.hypot(...p) + .1 * ((i * .618) % 1));
let formation = 1;  // 0 = not formed yet (start-up animation), 1 = the normal core

function drawCore(t, dt, cx, cy) {
  const g = coreCtx;
  g.setTransform(DPR, 0, 0, DPR, 0, 0);
  g.clearRect(0, 0, W, H);
  const F = formation;
  const aHaze = smoothstep(.15, .8, F), aFib = smoothstep(.22, .8, F), aFur = smoothstep(.5, 1, F);
  const aMem = smoothstep(.55, 1, F), aNeu = smoothstep(0, .2, F);
  g.globalCompositeOperation = "lighter";
  g.lineCap = "round"; g.lineJoin = "round";
  const breath = 1 + .05 * Math.sin(t * .8);
  const awake = F >= 1 ? wakeShape(t) : { scale: 1, heart: 0, ring: -1, u: -1 };
  const radius = Math.min(W * .21, H * .27, 158) * (1 + .012 * Math.sin(t * .8)) * awake.scale * (1 - .3 * readingShift);
  if (awake.ring >= 0) {  // neurons light up as the ring of light passes them
    const front = easeOutCubic(awake.ring) * 1.08;
    for (let i = 0; i < N; i++) {
      if (!woken[i] && centreDistance[i] <= front) { woken[i] = 1; flash[i] = Math.max(flash[i], .75 + .25 * (1 - centreDistance[i])); }
    }
    if (awake.ring >= 1 && wakeWaves === 0) { coreState.waves.push({ t0: t, power: 1 }); wakeWaves = 1; }
    if (awake.u > WAKE.gather + WAKE.open + .35 && wakeWaves === 1) { coreState.waves.push({ t0: t, power: .6 }); wakeWaves = 2; }
  }
  const v = view(t);
  const turned = [], proj = [];
  points.forEach((p, i) => {
    const ph = phases[i];
    const q = turn(p[0] + .03 * Math.sin(t * .27 + ph), p[1] + .026 * Math.cos(t * .23 + ph * 1.3), p[2] + .03 * Math.sin(t * .19 + ph * .7), v);
    const grow = F >= 1 ? 1 : Math.max(0, easeOutBack(clamp((F - growDelay[i]) / .55)));
    q[0] *= grow; q[1] *= grow; q[2] *= grow;
    turned.push(q);
    const s = radius * (1 + .16 * q[2]);
    proj.push([cx + q[0] * s, cy - q[1] * s, q[2]]);
  });
  const project = (q) => { const s = radius * (1 + .16 * q[2]); return [cx + q[0] * s, cy - q[1] * s, q[2]]; };
  const depthOf = (z) => .32 + .68 * clamp((z + 1) / 2);   // the far side recedes

  // Volume: a few slow, soft clouds of light inside the ball.
  for (const h of haze) {
    const [x, y, z] = project(turn(h.p[0], h.p[1], h.p[2], v));
    const r = h.size * radius * (1 + .08 * Math.sin(t * .3 + h.tw));
    g.globalAlpha = .17 * depthOf(z) * aHaze;
    g.drawImage(SPRITES.navy, x - r, y - r, 2 * r, 2 * r);
  }
  g.globalAlpha = 1;

  // Dust drifting with the ball (three brightness bands, one fill each).
  const dustPaths = [new Path2D(), new Path2D(), new Path2D()];
  for (const d of dust) {
    const [x, y, z] = project(turn(d.p[0], d.p[1], d.p[2], v));
    const a = (.4 + .6 * (.5 + .5 * Math.sin(t * .7 + d.tw))) * depthOf(z);
    dustPaths[Math.min(2, Math.floor(a * 3))].rect(x - d.size / 2, y - d.size / 2, d.size, d.size);
  }
  g.globalAlpha = aHaze;
  dustPaths.forEach((p, k) => { g.fillStyle = rgba(ICE, .07 + .07 * k); g.fill(p); });
  g.globalAlpha = 1;

  const onSphere = (a, b, share, swell) => {
    let x = a[0] + (b[0] - a[0]) * share, y = a[1] + (b[1] - a[1]) * share, z = a[2] + (b[2] - a[2]) * share;
    const l = Math.hypot(x, y, z) || 1;
    const reach = (Math.hypot(...a) * (1 - share) + Math.hypot(...b) * share) * swell / l;
    const q = [x * reach, y * reach, z * reach];
    const s = radius * (1 + .16 * q[2]);
    return [cx + q[0] * s, cy - q[1] * s];
  };

  // Fibres, grouped into brightness bands so each band is one GPU stroke.
  const BANDS = 8;
  const bandPaths = Array.from({ length: BANDS }, () => new Path2D());
  const longPaths = Array.from({ length: BANDS }, () => new Path2D());
  for (const f of fibres) {
    const [ax, ay, az] = proj[f.a], [bx, by, bz] = proj[f.b];
    const swell = f.long ? 1.0 : 1.08;
    const c1 = onSphere(turned[f.a], turned[f.b], .33, swell + f.bend[0] * .5 + .05 * Math.sin(t * .3 + f.phase));
    const c2 = onSphere(turned[f.a], turned[f.b], .67, swell + f.bend[1] * .5 + .05 * Math.cos(t * .25 + f.phase));
    let cycle = .5 + .5 * Math.sin(t * TAU / f.period + f.phase);
    cycle = cycle * cycle * (3 - 2 * cycle);
    const life = f.dormant ? cycle : .45 + .55 * cycle;
    const s = f.base * life * depthOf((az + bz) / 2) * breath;
    f.strength = s; f.c = [ax, ay, c1[0], c1[1], c2[0], c2[1], bx, by];
    if (s < .03) continue;
    const band = Math.min(BANDS - 1, Math.floor(s * BANDS));
    const path = (f.long ? longPaths : bandPaths)[band];
    path.moveTo(ax, ay); path.bezierCurveTo(c1[0], c1[1], c2[0], c2[1], bx, by);
  }
  g.globalAlpha = aFib;
  for (let b = 0; b < BANDS; b++) {
    const s = (b + .5) / BANDS;
    g.strokeStyle = rgba(NAVY, .42 * s); g.lineWidth = 3.4; g.stroke(bandPaths[b]);
    g.strokeStyle = rgba(ROYAL, .62 * s); g.lineWidth = .95; g.stroke(bandPaths[b]);
    g.strokeStyle = rgba(NAVY, .32 * s); g.lineWidth = 2.4; g.stroke(longPaths[b]);
    g.strokeStyle = rgba(ELECTRIC, .45 * s); g.lineWidth = .7; g.stroke(longPaths[b]);
  }

  // Fine filaments: a faint fur of curling strands.
  const furPaths = Array.from({ length: 4 }, () => new Path2D());
  for (const d of filaments) {
    const [nx, ny, nz] = proj[d.n];
    const [dx, dy, dz] = turn(d.dir[0], d.dir[1], d.dir[2], v);
    const reach = d.length * radius * (1 + .12 * Math.sin(t * .35 + d.phase));
    const curl = d.curl * (1 + .35 * Math.sin(t * .4 + d.phase));
    const ex = nx + dx * reach, ey = ny - dy * reach;
    const px = -dy * reach * curl * .6, py = -dx * reach * curl * .6;
    const c1x = nx + dx * reach * .35 + px * .4, c1y = ny - dy * reach * .35 + py * .4;
    const c2x = nx + dx * reach * .75 + px, c2y = ny - dy * reach * .75 + py;
    const s = (.45 + .25 * Math.sin(t * .6 + d.phase)) * depthOf(nz + dz * .3) * breath + .5 * flash[d.n];
    const path = furPaths[Math.min(3, Math.floor(clamp(s) * 4))];
    path.moveTo(nx, ny); path.bezierCurveTo(c1x, c1y, c2x, c2y, ex, ey);
    if (d.fork) {
      const mx = bez(nx, c1x, c2x, ex, .5), my = bez(ny, c1y, c2y, ey, .5);
      const fx = mx + (ex - nx) * .4 + py * .5, fy = my + (ey - ny) * .4 - px * .5;
      path.moveTo(mx, my); path.quadraticCurveTo((mx + fx) / 2 + px * .2, (my + fy) / 2 + py * .2, fx, fy);
    }
  }
  g.lineWidth = .55;
  g.globalAlpha = aFur;
  furPaths.forEach((p, k) => { g.strokeStyle = rgba(k < 2 ? ROYAL : ELECTRIC, .14 + .12 * k); g.stroke(p); });
  g.globalAlpha = 1;

  // Firing: start a new chain now and then; move impulses along their fibres.
  if (F >= 1 && t > nextFire) {
    fire(Math.floor(rng() * N), 0, t);
    nextFire = t + (.7 + rng() * 1.6) / look.spark;
  }
  for (let k = pulses.length - 1; k >= 0; k--) {
    const p = pulses[k], f = fibres[p.f];
    const u = (t - p.start) / p.dur;
    if (u < 0) continue;
    if (u >= 1 || !f.c) {
      pulses.splice(k, 1);
      if (u >= 1 && rng() < (.62 - p.gen * .07) * look.chain) fire(p.to, p.gen, t);
      else if (u >= 1) flash[p.to] = Math.max(flash[p.to], .6);
      continue;
    }
    const c = f.c, forward = p.from === f.a;
    for (let s = 0; s < 6; s++) {  // a comet: bright head, fading tail
      const w = clamp(u - s * .035), along = forward ? w : 1 - w;
      const x = bez(c[0], c[2], c[4], c[6], along), y = bez(c[1], c[3], c[5], c[7], along);
      const a = (1 - s / 6) * Math.sin(Math.PI * Math.min(1, u * 1.15));
      const r = (s === 0 ? 9 : 6) * (1 - s / 9);
      g.globalAlpha = .85 * a;
      g.drawImage(s === 0 ? SPRITES.ice : SPRITES.cyan, x - r, y - r, 2 * r, 2 * r);
    }
  }
  g.globalAlpha = 1;
  if (pulses.length > 260) pulses.splice(0, pulses.length - 260);

  // Neurons: glow, ring and body; a firing neuron flares white-blue.
  const ringPaths = [new Path2D(), new Path2D(), new Path2D(), new Path2D()];
  const bodyPaths = [new Path2D(), new Path2D(), new Path2D(), new Path2D()];
  proj.forEach(([x, y, z], i) => {
    const near = depthOf(z), pulse = .5 + .5 * Math.sin(t * 1.1 + phases[i] * 3), fl = flash[i];
    const r = (3.5 + 6 * near) * (1 + .15 * pulse) * (1 + 1.6 * fl);
    g.globalAlpha = clamp((.16 + .28 * near + .08 * pulse) * breath + .7 * fl) * aNeu;
    g.drawImage(fl > .3 ? SPRITES.cyan : SPRITES.royal, x - r, y - r, 2 * r, 2 * r);
    g.globalAlpha = 1;
    const band = Math.min(3, Math.floor(clamp(near + .5 * fl) * 4));
    const rr = (1.1 + 1.1 * near) * (1 + .5 * fl), rb = .55 + .65 * near;
    ringPaths[band].moveTo(x + rr, y); ringPaths[band].arc(x, y, rr, 0, TAU);
    bodyPaths[band].moveTo(x + rb, y); bodyPaths[band].arc(x, y, rb, 0, TAU);
    flash[i] *= Math.exp(-dt * 2.6);
  });
  g.globalAlpha = aNeu;
  ringPaths.forEach((p, k) => { g.fillStyle = rgba(CYAN, .30 + .18 * k); g.fill(p); });
  bodyPaths.forEach((p, k) => { g.fillStyle = rgba(ICE, .50 + .15 * k); g.fill(p); });
  g.globalAlpha = 1;

  // The heart breathes in the middle.
  const hx = cx + 3 * Math.sin(t * .23), hy = cy + 2 * Math.cos(t * .19);
  for (const [sprite, size, alpha] of [[SPRITES.navy, 80, .7], [SPRITES.royal, 34, .55], [SPRITES.cyan, 13, .65], [SPRITES.ice, 5, .95]]) {
    const r = size * breath * (1 + .9 * awake.heart);
    g.globalAlpha = clamp(alpha * (1 + .6 * awake.heart)) * aNeu; g.drawImage(sprite, hx - r / 2, hy - r / 2, r, r);
  }
  g.globalAlpha = 1;
  if (awake.ring >= 0 && awake.ring < 1) {  // the ring of light running out to the membrane
    const k = easeOutCubic(awake.ring), rr = radius * 1.2 * k, fade = Math.sin(Math.PI * Math.min(1, awake.ring * 1.05));
    g.strokeStyle = rgba(NAVY, .35 * fade); g.lineWidth = 14 * (1 - k) + 4; g.beginPath(); g.arc(hx, hy, rr, 0, TAU); g.stroke();
    g.strokeStyle = rgba(CYAN, .55 * fade); g.lineWidth = 3 * (1 - k) + 1.2; g.beginPath(); g.arc(hx, hy, rr, 0, TAU); g.stroke();
    g.strokeStyle = rgba(ICE, .5 * fade); g.lineWidth = .8; g.beginPath(); g.arc(hx, hy, rr, 0, TAU); g.stroke();
  }
  if (aMem > 0) {
    g.globalAlpha = aMem;
    drawMembrane(g, t, dt, cx, cy, radius, v);
    g.globalAlpha = 1;
  }
}

function makeBloom() {
  // A blurred copy of the core added on top, like light scattering in a lens.
  bloomCtx.setTransform(1, 0, 0, 1, 0, 0);
  bloomCtx.clearRect(0, 0, bloom.width, bloom.height);
  bloomCtx.filter = "blur(5px)";
  bloomCtx.drawImage(coreLayer, 0, 0, bloom.width, bloom.height);
  bloomCtx.filter = "none";
}

// ---------------------------------------------------------------- start-up: a neutron star is born into the core
// 0–1.8 s a quiet neutron star with a soft corona and two slow pulsar beams;
// 1.8–3 s it collapses: the corona is drawn in, light spirals inward, it brightens;
// 3 s a dense flash, rays and a shockwave; 3–6.6 s the debris becomes the core:
// neurons fly out and settle, fibres stretch between them, the shockwave slows and
// shrinks back into the energy membrane. A click skips it.
const INTRO = { collapse: 1.8, flash: 3.0, formed: 6.4, end: 7.0 };
const intro = { start: null, done: false };
const introRng = mulberry32(31415);
const infall = Array.from({ length: 150 }, () => ({ a: introRng() * TAU, r0: 140 + introRng() * 230,
  delay: introRng() * .5, tone: introRng() < .5 ? CYAN : ICE }));
const debris = Array.from({ length: 240 }, () => ({ a: introRng() * TAU, v: 220 + introRng() * 760,
  life: 1.2 + introRng() * 1.6, size: 2 + introRng() * 5, tone: introRng() < .3 ? ICE : introRng() < .6 ? CYAN : ROYAL }));
const rays = Array.from({ length: 30 }, (_, k) => ({ a: k / 30 * TAU + introRng() * .15, w: .004 + introRng() * .012,
  len: .6 + introRng() * .7, power: .4 + introRng() * .6 }));
document.body.classList.add("intro");

function introTime(t) {
  if (intro.done) return null;
  if (intro.start === null) intro.start = t;
  const ti = t - intro.start;
  plate.style.opacity = (.12 + .88 * smoothstep(INTRO.flash, INTRO.flash + 2.2, ti)).toFixed(3);
  if (ti > INTRO.formed - .6) document.body.classList.remove("intro");
  if (ti >= INTRO.end) {
    intro.done = true;
    plate.style.opacity = "1";
    emit({ event: "intro_done" });
    return null;
  }
  return ti;
}
document.addEventListener("pointerdown", () => {  // impatient: skip to the formed core
  if (!intro.done && intro.start !== null) intro.start -= INTRO.end;
}, { capture: true });

function wedge(g, cx, cy, angle, length, width, color, alpha) {
  const ex = cx + Math.cos(angle) * length, ey = cy + Math.sin(angle) * length;
  const nx = -Math.sin(angle) * width, ny = Math.cos(angle) * width;
  const grad = g.createLinearGradient(cx, cy, ex, ey);
  grad.addColorStop(0, rgba(color, alpha));
  grad.addColorStop(1, rgba(color, 0));
  g.fillStyle = grad;
  g.beginPath(); g.moveTo(cx, cy); g.lineTo(ex + nx, ey + ny); g.lineTo(ex - nx, ey - ny); g.closePath(); g.fill();
}

function drawIntro(g, ti, cx, cy) {
  g.setTransform(DPR, 0, 0, DPR, 0, 0);
  g.globalCompositeOperation = "lighter";
  const radius = Math.min(W * .21, H * .27, 158);
  const Rm = radius * 1.2;  // the membrane's radius: where the shockwave comes to rest
  if (ti < INTRO.flash) {
    // The neutron star, then its collapse.
    const c = smoothstep(INTRO.collapse, INTRO.flash, ti);
    const flicker = 1 + .05 * Math.sin(ti * 23) + .04 * Math.sin(ti * 37 + 1);
    const corona = 150 * (1 - .78 * c * c);
    g.globalAlpha = .55 * (1 - .2 * c); g.drawImage(SPRITES.navy, cx - corona * 1.7, cy - corona * 1.7, corona * 3.4, corona * 3.4);
    g.globalAlpha = .55 + .3 * c; g.drawImage(SPRITES.royal, cx - corona * .8, cy - corona * .8, corona * 1.6, corona * 1.6);
    // Pulsar beams: slow at first, whipping round as it collapses.
    const spin = ti * 1.4 + 9 * c * c * c;
    const beamLen = 340 * (1 - .65 * c), beamA = (.34 + .3 * c) * (.75 + .25 * Math.sin(ti * 5));
    g.globalAlpha = 1;
    for (const [w, k] of [[16, .35], [6, 1]]) {  // a soft sheath around a bright line
      wedge(g, cx, cy, spin, beamLen, w, k < 1 ? ROYAL : ICE, beamA * k);
      wedge(g, cx, cy, spin + Math.PI, beamLen, w, k < 1 ? ROYAL : ICE, beamA * k);
    }
    // Light falling inward along spirals.
    if (c > 0) {
      g.lineCap = "round";
      for (const p of infall) {
        const k = clamp((c - p.delay * .6) / (1 - p.delay * .6));
        if (k <= 0) continue;
        const r = p.r0 * (1 - k * k), a = p.a + 2.6 * k * k;
        const r2 = Math.min(p.r0, r * 1.25 + 6), a2 = a - .22;
        g.strokeStyle = rgba(p.tone, .55 * k * (1 - k * .3)); g.lineWidth = 1.1;
        g.beginPath(); g.moveTo(cx + Math.cos(a2) * r2, cy + Math.sin(a2) * r2); g.lineTo(cx + Math.cos(a) * r, cy + Math.sin(a) * r); g.stroke();
      }
    }
    // The star itself: tiny and fierce, ever brighter.
    const core = (16 - 7 * c) * flicker;
    g.globalAlpha = .95; g.drawImage(SPRITES.ice, cx - core, cy - core, 2 * core, 2 * core);
    const hot = 3 + 16 * c * c * c;
    g.globalAlpha = .6 + .4 * c; g.drawImage(SPRITES.ice, cx - hot, cy - hot, 2 * hot, 2 * hot);
    g.fillStyle = rgba(ICE, 1); g.globalAlpha = 1;
    g.beginPath(); g.arc(cx, cy, 1.6 + 1.2 * c, 0, TAU); g.fill();
    g.globalAlpha = 1;
    return;
  }
  const f = ti - INTRO.flash;
  // The flash: blinding at first, then a fading glare.
  if (f < .32) {  // one brief white-out, like a camera blinded for an instant
    g.fillStyle = rgba(ICE, .32 * (1 - f / .32) ** 2);
    g.fillRect(0, 0, W, H);
  }
  const glare = 40 + 480 * (1 - Math.exp(-f * 5));
  g.globalAlpha = clamp(1 - f / 1.1) ** 2.2; g.drawImage(SPRITES.ice, cx - glare, cy - glare, 2 * glare, 2 * glare);
  g.globalAlpha = clamp(.6 * (1 - f / 1.8)); g.drawImage(SPRITES.royal, cx - glare * .7, cy - glare * .7, glare * 1.4, glare * 1.4);
  g.globalAlpha = 1;
  // Rays of dense light shooting out.
  if (f < 1.6) {
    const reach = 1 - Math.exp(-f * 3.5);
    for (const r of rays) {
      wedge(g, cx, cy, r.a, Math.max(W, H) * r.len * reach, Math.max(W, H) * r.w * .8, ICE, .38 * r.power * (1 - f / 1.6) ** 2.4);
    }
  }
  // The shockwave: races out, slows, and comes back to rest as the membrane.
  const out = 1 - Math.exp(-f * 3.2);
  const back = smoothstep(.9, INTRO.formed - INTRO.flash, f);
  const ring = Rm * ((.15 + 2.3 * out) * (1 - back) + back);
  const ringA = (1 - back * .85) * clamp(f * 8);
  g.strokeStyle = rgba(ICE, .55 * ringA * clamp(1 - f / 1.2)); g.lineWidth = 6 * (1 - out) + 1.2;
  g.beginPath(); g.arc(cx, cy, ring, 0, TAU); g.stroke();
  g.strokeStyle = rgba(CYAN, .45 * ringA); g.lineWidth = 2;
  g.beginPath(); g.arc(cx, cy, ring, 0, TAU); g.stroke();
  g.strokeStyle = rgba(NAVY, .35 * ringA); g.lineWidth = 9;
  g.beginPath(); g.arc(cx, cy, ring * .985, 0, TAU); g.stroke();
  // Debris: sparks flung outward, slowing down and fading as the core takes shape.
  for (const d of debris) {
    if (f > d.life) continue;
    const dist = d.v / 2.4 * (1 - Math.exp(-2.4 * f));
    const a = (1 - f / d.life) ** 1.5;
    const x = cx + Math.cos(d.a) * dist, y = cy + Math.sin(d.a) * dist * .92;
    g.globalAlpha = .85 * a;
    const s = d.size * (1 - .5 * f / d.life);
    g.drawImage(d.tone === ICE ? SPRITES.ice : d.tone === CYAN ? SPRITES.cyan : SPRITES.royal, x - s, y - s, 2 * s, 2 * s);
  }
  g.globalAlpha = 1;
}

function frame(now) {
  const t = now / 1000;
  const dt = last === null ? 0 : Math.min(.25, t - last);
  last = t;
  frameTimes.push(now); if (frameTimes.length > 120) frameTimes.shift();
  ctx.setTransform(DPR, 0, 0, DPR, 0, 0);
  ctx.globalCompositeOperation = "source-over";
  ctx.clearRect(0, 0, W, H);
  const wantShift = document.body.classList.contains("reading") ? 1 : 0;
  readingShift += (wantShift - readingShift) * (1 - Math.exp(-dt * 4.2));
  if (Math.abs(wantShift - readingShift) < .001) readingShift = wantShift;
  const rw = Math.min(620, W * .58);
  const cx = W / 2 + readingShift * ((W - rw - 24) / 2 - W / 2), cy = H / 2 - 3;
  window.TimerHUD?.layoutDock({cx, cy, radius: Math.min(W * .21, H * .27, 158) * (1 - .3 * readingShift)});
  plate.style.transform = `translate(${(4 * Math.sin(t * .05)).toFixed(2)}px, ${(3 * Math.cos(t * .04)).toFixed(2)}px)`;
  const ti = introTime(t);
  if (ti !== null) {
    formation = ti < INTRO.flash ? 0 : smoothstep(INTRO.flash, INTRO.formed, ti);
    starGain = smoothstep(INTRO.flash + .2, INTRO.flash + 2.2, ti);
  } else { formation = 1; starGain = 1; }
  // Desktop-transparent theme: retain the neural core, without the star field.
  if (formation > 0) drawCore(t, dt, cx, cy);
  else { coreCtx.setTransform(1, 0, 0, 1, 0, 0); coreCtx.clearRect(0, 0, coreLayer.width, coreLayer.height); }
  if (ti !== null) drawIntro(coreCtx, ti, cx, cy);
  makeBloom();
  updateLook(dt);
  const settled = ti === null;  // the start-up animation keeps its own light
  const light = settled ? look.light : 1, sat = settled ? look.sat : 1, glowBoost = settled ? look.bloom : 1;
  ctx.setTransform(1, 0, 0, 1, 0, 0);
  ctx.globalCompositeOperation = "lighter";
  const musicGoal = document.body.classList.contains("media-open") ? 1 : 0;
  const openingShift = musicGoal ? window.MediaPlayer?.coreMotion(now, musicShift) : null;
  if (openingShift != null) musicShift = openingShift;
  else musicShift += (musicGoal - musicShift) * (1 - Math.exp(-dt * 9));
  if (Math.abs(musicShift - musicGoal) < .001) musicShift = musicGoal;
  let coreScale = 1, coreX = cx, coreY = cy;
  const radius = Math.min(W * .21, H * .27, 158) * (1 - .3 * readingShift);
  const slotScale = slot => Math.min(slot.width, slot.height) * .84 / (radius * 2.44);
  if (musicShift > 0 && window.MediaPlayer) {
    if (musicGoal) musicCoreSlot = window.MediaPlayer.coreTarget();
    const slot = musicCoreSlot;
    // Transform the same live core (including its glow), never a static duplicate.
    coreScale = 1 + (slotScale(slot) - 1) * musicShift;
    coreX = cx + musicShift * (slot.left + slot.width / 2 - cx);
    coreY = cy + musicShift * (slot.top + slot.height / 2 - cy);
  }
  // Expenses and Agents share the same live-core destination and return motion.
  const expensesGoal = document.body.classList.contains("core-sheet-open") ? 1 : 0;
  const reducedMotion = matchMedia("(prefers-reduced-motion: reduce)").matches;
  expensesShift = reducedMotion ? expensesGoal : expensesShift + Math.sign(expensesGoal - expensesShift) * Math.min(Math.abs(expensesGoal - expensesShift), dt / .55);
  document.body.classList.toggle("expenses-core-active", expensesShift > 0 || !!expensesGoal);
  if (expensesGoal) expensesCoreSlot = document.getElementById("expenses-core-slot").getBoundingClientRect();
  if (expensesShift > 0) {
    const slot = expensesCoreSlot;
    const targetScale = slotScale(slot);
    coreScale += (targetScale - coreScale) * expensesShift;
    coreX += (slot.left + slot.width / 2 - coreX) * expensesShift;
    coreY += (slot.top + slot.height / 2 - coreY) * expensesShift;
  }
  ctx.setTransform(coreScale, 0, 0, coreScale, (coreX - cx * coreScale) * DPR, (coreY - cy * coreScale) * DPR);
  const tinted = Math.abs(light - 1) > .01 || Math.abs(sat - 1) > .01;
  if (tinted) ctx.filter = `brightness(${light.toFixed(3)}) saturate(${sat.toFixed(3)})`;
  ctx.globalAlpha = Math.min(1, .85 * glowBoost); ctx.drawImage(bloom, 0, 0, canvas.width, canvas.height);
  if (glowBoost > 1.18) { ctx.globalAlpha = .85 * (glowBoost - 1.18); ctx.drawImage(bloom, 0, 0, canvas.width, canvas.height); }
  ctx.globalAlpha = 1; ctx.drawImage(coreLayer, 0, 0);
  if (tinted) ctx.filter = "none";
  // Draw at the same final centre/scale as the live core. Keep a readable minimum
  // stroke width in small panel slots, without adding blur or changing hit tests.
  ctx.save();ctx.setTransform(DPR,0,0,DPR,0,0);
  window.CoreAudioRing?.draw(ctx,coreX,coreY,radius*coreScale,coreScale,dt,now);
  ctx.restore();
  if (!fpsBox.hidden) updateFps();
  requestAnimationFrame(frame);
}

// ---------------------------------------------------------------- frame rate (F12)
const fpsBox = document.getElementById("fps");
let fpsShown = 0;
function updateFps() {
  const n = frameTimes.length;
  if (n < 2 || performance.now() - fpsShown < 400) return;
  fpsShown = performance.now();
  fpsBox.textContent = `${((n - 1) * 1000 / (frameTimes[n - 1] - frameTimes[0])).toFixed(0)} fps · GPU · ${W}×${H} @${DPR}x`;
}
window.addEventListener("keydown", (e) => {
  if (e.key === "F12") { fpsBox.hidden = !fpsBox.hidden; e.preventDefault(); }
});

// ---------------------------------------------------------------- subtitles and voice
const chat = document.getElementById("chat");
const voice = document.getElementById("voice");
const hint = document.getElementById("hint");
const bar = document.getElementById("bar");
const entry = document.getElementById("entry");
const TOP_GAP = 168;  // px below the window's centre: under the core's glow

function layoutText() {
  const reading = document.body.classList.contains("reading");
  const top = H / 2 + TOP_GAP * (reading ? .74 : 1);
  chat.style.top = top + "px";
  const bottom = bar.hidden ? 22 : bar.offsetHeight + 34;
  chat.style.maxHeight = Math.max(40, H - top - bottom) + "px";
  voice.style.top = (top - 24) + "px";
  hint.style.bottom = (bar.hidden ? 14 : bar.offsetHeight + 26) + "px";
  fitChat();
}

function fitChat() {
  // A shrunk earlier message longer than two lines is left out; a long reply shows its end.
  for (const el of chat.querySelectorAll(".msg.small")) {
    el.style.display = "";
    if (el.scrollHeight > el.clientHeight + 2) el.style.display = "none";
  }
  const over = chat.scrollHeight > chat.clientHeight + 1;
  chat.classList.toggle("overflow", over);
  if (over) chat.scrollTop = chat.scrollHeight;
}

// A quiet row under MYN's saved reply: 👍 / 👎 (clicking the chosen one again takes it
// back) and ✎ Teach me (why, and how it should have been said). Never interrupts the chat.
function feedbackRow(reply, rating) {
  const row = document.createElement("span");
  row.className = "fb";
  const marks = [[1, "👍", "Like this wording"], [-1, "👎", "Dislike this wording"], [null, "✎", "Teach MYN how to respond"]];
  for (const [value, mark, title] of marks) {
    const b = document.createElement("button");
    b.type = "button";
    b.textContent = mark;
    b.title = title;
    if (value !== null && value === rating) b.classList.add("on");
    b.addEventListener("click", (e) => {
      e.stopPropagation();  // not a click on the reply (which reads it aloud)
      if (value === null) emit({ event: "feedback_edit", reply });
      else emit({ event: "feedback", reply, rating: rating === value ? 0 : value });
    });
    row.appendChild(b);
  }
  return row;
}

function renderChat(items, showHint) {
  chat.classList.remove("fading");
  const keep = new Set(items.map((i) => String(i.key)));
  for (const el of [...chat.children]) if (!keep.has(el.dataset.key)) el.remove();
  items.forEach((item, index) => {
    let el = chat.querySelector(`[data-key="${item.key}"]`);
    const fresh = !el;
    if (fresh) {
      el = document.createElement("div");
      el.dataset.key = item.key;
      el.className = "msg enter";
      el.addEventListener("click", () => { if (el.dataset.link) emit({ event: "speak", link: el.dataset.link }); });
    }
    el.className = `msg ${item.size} ${item.tag}${fresh ? " enter" : ""}${item.link ? " clickable" : ""}`;
    el.dataset.link = item.link || "";
    el.textContent = "";
    if (item.name) { const n = document.createElement("span"); n.className = "name"; n.textContent = item.name; el.appendChild(n); }
    const newest = index === items.length - 1;
    if (newest && item.tag === "myn" && isLong(item.text)) {
      // The whole reply is in the panel; the subtitle only says where it is.
      el.appendChild(document.createTextNode(teaser(item.text)));
      const more = document.createElement("span");
      more.className = "more";
      more.textContent = "Read more";
      more.addEventListener("click", (e) => { e.stopPropagation(); dismissed.delete(String(item.key)); showReader(item.key, item.text, !!item.typing); });
      el.appendChild(more);
      if (!dismissed.has(String(item.key))) showReader(item.key, item.text, !!item.typing);
    } else el.appendChild(document.createTextNode(item.text));
    if (item.reply != null) el.appendChild(feedbackRow(item.reply, item.rating || 0));
    if (chat.children[index] !== el) chat.insertBefore(el, chat.children[index] || null);
    if (fresh) requestAnimationFrame(() => requestAnimationFrame(() => el.classList.remove("enter")));
  });
  hint.hidden = !showHint;
  fitChat();
}

window.MYN = {
  receive(cmd) {
    switch (cmd.cmd) {
      case "media_open": if (window.MediaPlayer) window.MediaPlayer.open(); break;
      case "alarm_music": window.MediaPlayer?.alarmMusic(cmd); break;
      case "alarm_settings_result": window.MediaPlayer?.alarmSettingsResult(cmd); break;
      case "music_control": if (window.MediaPlayer) window.MediaPlayer.command(cmd); break;
      case "agenda_display": if (window.AgendaDisplay) window.AgendaDisplay.show(cmd); break;
      case "agenda_close": if (window.AgendaDisplay) window.AgendaDisplay.close(false); break;
      case "timers": if (window.TimerHUD) window.TimerHUD.update(cmd); break;
      case "alarm_list": if (window.TimerHUD) window.TimerHUD.alarms(cmd); break;
      case "timer_finished": if (window.TimerHUD) window.TimerHUD.finish(cmd.text); break;
      case "chat": renderChat(cmd.items || [], !!cmd.hint); break;
      case "fade": chat.classList.add("fading"); break;
      case "reader":
        if (cmd.action === "report") { showReader(cmd.key, String(cmd.text || ""), false); break; }
        if (cmd.action === "close" && (cmd.key === undefined ||
            (String(cmd.key) === String(readerKey) && cmd.revision === readerRevision))) closeReader();
        else if (cmd.action === "open" && readerKey !== null) { dismissed.delete(String(readerKey)); openReader(); sizeInk(); }
        break;
      case "selection_used": clearLasso(true); setTool("read"); break;
      case "panel": document.body.classList.toggle("paneled", !!cmd.open); break;  // a settings panel is open
      case "sheet": showSheet(cmd.spec || {}); break;
      case "audio_console": window.AudioConsole?.open(); break;
      case "sheet_close": closeSheet(false); break;
      case "sheet_toast": toast(cmd.text || "", cmd.tone); break;
      case "sheet_modal": showModal(cmd.spec || {}); break;
      case "sheet_modal_close": sheetModal.hidden = true; break;
      case "sheet_confirm": showConfirm(cmd); break;
      case "attached": showAttached(+cmd.count || 0); break;
      case "voice":
        voice.textContent = cmd.text || "";
        voice.classList.toggle("on", !!cmd.text);
        break;
      case "input":
        bar.hidden = !cmd.visible;
        window.TimerHUD?.setInput(!!cmd.visible);
        if (typeof cmd.text === "string") entry.value = cmd.text;
        if (cmd.visible) setTimeout(() => entry.focus(), 30);
        layoutText();
        break;
      case "entry": entry.value = cmd.text || ""; break;
      case "mode": coreState.mode = cmd.mode || "idle"; break;
      case "level":
        coreState.target = clamp(+cmd.value || 0);
        window.CoreAudioRing?.input(+cmd.value || 0, performance.now(), cmd.bands);
        coreState.levelAt = performance.now() / 1000;
        break;
      case "wake": {
        startWake(performance.now() / 1000);  // the waking animation; listening follows
        coreState.mode = "listening";
        break;
      }
      case "config":
        // Old host configuration must not restore the removed space background.
        break;
    }
  },
};

// ---------------------------------------------------------------- controls, menu, input
document.getElementById("minimize").addEventListener("click", () => emit({ event: "minimize" }));
document.getElementById("close").addEventListener("click", () => emit({ event: "close" }));
document.getElementById("drag").addEventListener("dblclick", () => emit({ event: "maximize" }));

let attachedCount = 0;  // pictures waiting to go with the next message (told by MYN)
bar.addEventListener("submit", (e) => {
  e.preventDefault();
  const text = entry.value.trim();
  if (!text && !attachedCount) return;
  entry.value = "";
  emit({ event: "send", text });
});
const pic = document.getElementById("pic"), picCount = document.getElementById("pic-count");
pic.addEventListener("click", () => emit({ event: "attach_image" }));
entry.addEventListener("paste", (e) => {  // a copied picture: MYN takes it from the clipboard
  const items = (e.clipboardData && e.clipboardData.items) || [];
  if ([...items].some((item) => item.kind === "file" && item.type.startsWith("image/"))) {
    e.preventDefault();
    emit({ event: "paste_image" });
  }
});
function showAttached(count) {
  attachedCount = count;
  pic.classList.toggle("has", count > 0);
  picCount.hidden = !count;
  picCount.textContent = String(count);
}
entry.addEventListener("keydown", (e) => { if (e.key === "Escape") emit({ event: "hide_input" }); });
const mic = document.getElementById("mic");
mic.addEventListener("pointerdown", (e) => { mic.setPointerCapture(e.pointerId); mic.classList.add("held"); emit({ event: "voice_press" }); });
const release = () => { if (mic.classList.contains("held")) { mic.classList.remove("held"); emit({ event: "voice_release" }); } };
mic.addEventListener("pointerup", release);
mic.addEventListener("pointercancel", release);

let wheelAt = 0;
window.addEventListener("wheel", (e) => {
  if (reader.contains(e.target) || sheet.contains(e.target)) return;  // these scroll themselves
  const now = performance.now();
  if (now - wheelAt < 140 || !e.deltaY) return;
  wheelAt = now;
  emit({ event: "scroll", step: e.deltaY < 0 ? 1 : -1 });
}, { passive: true });

// ---------------------------------------------------------------- background video
// A looping video (for example rendered in UE5 or Blender) replaces the still plate
// when src/assets/background.mp4 exists.
const video = document.getElementById("bgvideo");
video.addEventListener("canplay", () => { video.hidden = false; plate.style.visibility = "hidden"; video.play().catch(() => {}); }, { once: true });
video.addEventListener("error", () => { video.hidden = true; }, { once: true });
// Python says whether the file exists (cmd "config"), so no failed load is logged.
function useVideo(url) { if (url) video.src = url; }

// ---------------------------------------------------------------- reader (2026-10-17)
// A long reply opens in a solid panel beside the core instead of filling the subtitles.
// You can read it, draw on it (pen, highlighter, eraser, undo), and circle a part of it
// for MYN: the circled text goes with your next question (typed, a chip, or spoken).
const reader = document.getElementById("reader");
const readerBody = document.getElementById("reader-body");
const readerPage = document.getElementById("reader-page");
const readerScroll = document.getElementById("reader-scroll");
const ink = document.getElementById("ink");
const inkCtx = ink.getContext("2d");
const ask = document.getElementById("ask");
const askQuote = document.getElementById("ask-quote");
const askEntry = document.getElementById("ask-entry");
const LONG_CHARS = 160;
let readerKey = null;            // which reply is in the panel
let readerRevision = 0;          // prevents late scene decisions closing newly opened text
let readerText = "";
let readerReportedText = "";
const dismissed = new Set();     // replies you closed: not opened again by themselves
const strokesByKey = new Map();  // your drawings, kept per reply
let tool = "read", inkColor = "#7fe3f5", drawingNow = null, lasso = null;
let readingShift = 0;            // 0 = core in the middle, 1 = core moved aside for the panel
let musicShift = 0;
let musicCoreSlot = {left:0, top:0, width:0, height:0};
let expensesShift = 0;
let expensesCoreSlot = {left:0, top:0, width:96, height:88};
let expensesEntrance = null;

function isLong(text) {
  return text.length > LONG_CHARS || (text.match(/\n/g) || []).length >= 4 || text.includes("```");
}

function teaser(text) {
  const first = text.replace(/```[\s\S]*?```/g, " ").replace(/[#*>`]/g, "").trim().split(/(?<=[。！？!?.])\s*|\n/)[0] || "";
  return first.length > 48 ? first.slice(0, 46) + "…" : first;
}

// A small, safe Markdown subset built with DOM nodes (never innerHTML).
function inline(parent, text) {
  const parts = text.split(/(\*\*[^*]+\*\*|`[^`]+`)/g);
  for (const part of parts) {
    if (!part) continue;
    if (part.startsWith("**") && part.endsWith("**") && part.length > 4) {
      const b = document.createElement("strong"); b.textContent = part.slice(2, -2); parent.appendChild(b);
    } else if (part.startsWith("`") && part.endsWith("`") && part.length > 2) {
      const c = document.createElement("code"); c.textContent = part.slice(1, -1); parent.appendChild(c);
    } else parent.appendChild(document.createTextNode(part));
  }
}

function renderMarkdown(text) {
  const out = document.createDocumentFragment();
  const lines = text.replace(/\r/g, "").split("\n");
  let para = null, list = null, quote = null;
  const close = () => { para = null; list = null; quote = null; };
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    if (/^\s*```/.test(line)) {
      close();
      const pre = document.createElement("pre"), code = document.createElement("code");
      const body = [];
      for (i++; i < lines.length && !/^\s*```/.test(lines[i]); i++) body.push(lines[i]);
      code.textContent = body.join("\n"); pre.appendChild(code); out.appendChild(pre);
      continue;
    }
    if (!line.trim()) { close(); continue; }
    const heading = line.match(/^\s*(#{1,4})\s+(.*)$/);
    if (heading) {
      close();
      const h = document.createElement(heading[1].length <= 2 ? "h3" : "h4"); inline(h, heading[2]); out.appendChild(h);
      continue;
    }
    const item = line.match(/^\s*(?:([-*•])|(\d+)[.)、])\s+(.*)$/);
    if (item) {
      const kind = item[1] ? "UL" : "OL";
      if (!list || list.tagName !== kind) { para = null; quote = null; list = document.createElement(kind.toLowerCase()); out.appendChild(list); }
      const li = document.createElement("li"); inline(li, item[3]); list.appendChild(li);
      continue;
    }
    const quoted = line.match(/^\s*>\s?(.*)$/);
    if (quoted) {
      if (!quote) { para = null; list = null; quote = document.createElement("blockquote"); out.appendChild(quote); }
      else quote.appendChild(document.createElement("br"));
      inline(quote, quoted[1]);
      continue;
    }
    if (!para) {
      list = null; quote = null;
      para = document.createElement("p");
      if (/^(Evidence[:：]|Source[:：])/.test(line.trim())) para.className = "src";
      out.appendChild(para);
    } else para.appendChild(document.createElement("br"));
    inline(para, line);
  }
  return out;
}

function showReader(key, text, typing) {
  const sameReply = key === readerKey;
  const wasOpen = !reader.hidden;
  if (!sameReply) readerRevision++;
  const atBottom = readerScroll.scrollTop + readerScroll.clientHeight >= readerScroll.scrollHeight - 30;
  if (!sameReply) { clearLasso(); readerScroll.scrollTop = 0; }
  if (!sameReply || text !== readerText) {
    readerBody.textContent = "";
    readerBody.appendChild(renderMarkdown(text));
    const last = readerBody.lastElementChild;
    if (typing && last) last.classList.add("typing");
  } else if (!typing) {
    for (const el of readerBody.querySelectorAll(".typing")) el.classList.remove("typing");
  }
  readerKey = key; readerText = text;
  openReader();
  if (wasOpen && (!sameReply || (!typing && readerReportedText !== text))) reportReader();
  sizeInk();
  if (typing && sameReply && atBottom) readerScroll.scrollTop = readerScroll.scrollHeight;
}

function openReader() {
  if (!reader.hidden) return;
  reader.hidden = false;
  document.body.classList.add("reading");
  layoutText();
  readerRevision++;
  reportReader();
}

function reportReader() {
  readerReportedText = readerText;
  emit({ event: "reader", open: !reader.hidden, key: readerKey,
         revision: readerRevision, text: readerText.slice(0, 6000) });
}

function closeReader() {
  if (reader.hidden) return;
  reader.hidden = true;
  document.body.classList.remove("reading");
  if (readerKey !== null) dismissed.add(String(readerKey));
  clearLasso(true);
  setTool("read");
  layoutText();
  readerRevision++;
  reportReader();
}

// ---- drawing -----------------------------------------------------------------------
function sizeInk() {
  const w = readerPage.clientWidth, h = Math.max(readerPage.scrollHeight, readerScroll.clientHeight);
  const d = Math.min(window.devicePixelRatio || 1, 2.5);
  if (ink.width !== Math.round(w * d) || ink.height !== Math.round(h * d)) {
    ink.width = Math.round(w * d); ink.height = Math.round(h * d);
    ink.style.width = w + "px"; ink.style.height = h + "px";
  }
  redrawInk();
}

function strokes() {
  const key = String(readerKey);
  if (!strokesByKey.has(key)) strokesByKey.set(key, []);
  return strokesByKey.get(key);
}

function paintStroke(g, s) {
  if (s.pts.length < 1) return;
  g.save();
  g.lineCap = "round"; g.lineJoin = "round";
  if (s.tool === "marker") { g.globalAlpha = .28; g.lineWidth = 15; g.strokeStyle = s.color; }
  else if (s.tool === "lasso") { g.setLineDash([7, 6]); g.lineWidth = 2; g.strokeStyle = "#38d7f6"; }
  else { g.lineWidth = 2.4; g.strokeStyle = s.color; }
  g.beginPath();
  g.moveTo(s.pts[0][0], s.pts[0][1]);
  if (s.pts.length === 1) g.lineTo(s.pts[0][0] + .1, s.pts[0][1]);
  for (let i = 1; i < s.pts.length - 1; i++) {  // smooth through the midpoints
    const [x0, y0] = s.pts[i], [x1, y1] = s.pts[i + 1];
    g.quadraticCurveTo(x0, y0, (x0 + x1) / 2, (y0 + y1) / 2);
  }
  if (s.pts.length > 1) { const [x, y] = s.pts[s.pts.length - 1]; g.lineTo(x, y); }
  if (s.tool === "lasso" && s.closed) {
    g.closePath(); g.stroke();
    g.setLineDash([]); g.globalAlpha = .07; g.fillStyle = "#38d7f6"; g.fill();
  } else g.stroke();
  g.restore();
}

function redrawInk() {
  const d = ink.width / Math.max(1, ink.clientWidth || readerPage.clientWidth);
  inkCtx.setTransform(1, 0, 0, 1, 0, 0);
  inkCtx.clearRect(0, 0, ink.width, ink.height);
  inkCtx.setTransform(d, 0, 0, d, 0, 0);
  for (const s of strokes()) paintStroke(inkCtx, s);
  if (lasso) paintStroke(inkCtx, lasso);
  if (drawingNow) paintStroke(inkCtx, drawingNow);
}

function setTool(name) {
  tool = name;
  for (const b of document.querySelectorAll("#reader-tools [data-tool]")) b.classList.toggle("on", b.dataset.tool === name);
  reader.classList.toggle("drawing", name !== "read");
  reader.classList.toggle("erasing", name === "eraser");
}

function pagePoint(e) {
  const r = readerPage.getBoundingClientRect();
  return [e.clientX - r.left, e.clientY - r.top];
}

function eraseAt(p) {
  const list = strokes();
  for (let i = list.length - 1; i >= 0; i--) {
    if (list[i].pts.some(([x, y]) => Math.hypot(x - p[0], y - p[1]) < (list[i].tool === "marker" ? 12 : 9))) {
      list.splice(i, 1); redrawInk(); return;
    }
  }
}

ink.addEventListener("pointerdown", (e) => {
  if (tool === "read") return;
  ink.setPointerCapture(e.pointerId);
  const p = pagePoint(e);
  if (tool === "eraser") { drawingNow = { tool: "eraser", pts: [] }; eraseAt(p); return; }
  if (tool === "lasso") clearLasso();
  drawingNow = { tool, color: inkColor, pts: [p] };
  redrawInk();
});
ink.addEventListener("pointermove", (e) => {
  if (!drawingNow) return;
  const p = pagePoint(e);
  if (drawingNow.tool === "eraser") { eraseAt(p); return; }
  const [lx, ly] = drawingNow.pts[drawingNow.pts.length - 1];
  if (Math.hypot(p[0] - lx, p[1] - ly) < 1.5) return;
  drawingNow.pts.push(p);
  redrawInk();
});
const endStroke = () => {
  if (!drawingNow) return;
  const s = drawingNow; drawingNow = null;
  if (s.tool === "eraser") return;
  if (s.tool === "lasso") { s.closed = true; lasso = s; redrawInk(); pickCircled(s.pts); return; }
  strokes().push(s);
  redrawInk();
};
ink.addEventListener("pointerup", endStroke);
ink.addEventListener("pointercancel", endStroke);

for (const b of document.querySelectorAll("#reader-tools [data-tool]")) b.addEventListener("click", () => setTool(b.dataset.tool));
for (const b of document.querySelectorAll("#reader-colors button")) {
  b.style.setProperty("--c", b.dataset.color);
  b.addEventListener("click", () => {
    inkColor = b.dataset.color;
    for (const o of document.querySelectorAll("#reader-colors button")) o.classList.toggle("on", o === b);
    if (tool === "read" || tool === "eraser" || tool === "lasso") setTool("pen");
  });
}
document.getElementById("reader-undo").addEventListener("click", () => { strokes().pop(); redrawInk(); });
document.getElementById("reader-clear").addEventListener("click", () => { strokes().length = 0; clearLasso(); redrawInk(); });
document.getElementById("reader-close").addEventListener("click", closeReader);
new ResizeObserver(() => { if (!reader.hidden) sizeInk(); }).observe(readerBody);

// ---- circling text for MYN ---------------------------------------------------------
function inside(poly, x, y) {
  let hit = false;
  for (let i = 0, j = poly.length - 1; i < poly.length; j = i++) {
    const [xi, yi] = poly[i], [xj, yj] = poly[j];
    if ((yi > y) !== (yj > y) && x < (xj - xi) * (y - yi) / (yj - yi) + xi) hit = !hit;
  }
  return hit;
}

function circledText(poly) {
  const xs = poly.map((p) => p[0]), ys = poly.map((p) => p[1]);
  const box = [Math.min(...xs), Math.min(...ys), Math.max(...xs), Math.max(...ys)];
  const origin = readerPage.getBoundingClientRect();
  const walker = document.createTreeWalker(readerBody, NodeFilter.SHOW_TEXT);
  const range = document.createRange();
  const blocks = [];  // [{el, chars: [[char, picked]]}] in reading order
  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
    const el = node.parentElement.closest("p, li, pre, h3, h4, blockquote") || readerBody;
    if (!blocks.length || blocks[blocks.length - 1].el !== el) blocks.push({ el, chars: [] });
    const chars = blocks[blocks.length - 1].chars;
    range.selectNodeContents(node);
    const r = range.getBoundingClientRect();
    const far = r.right - origin.left < box[0] || r.left - origin.left > box[2] ||
                r.bottom - origin.top < box[1] || r.top - origin.top > box[3];
    const text = node.textContent;
    for (let i = 0; i < text.length; i++) {
      let picked = false;
      if (!far) {
        range.setStart(node, i); range.setEnd(node, i + 1);
        const c = range.getClientRects()[0];
        if (c) picked = inside(poly, (c.left + c.right) / 2 - origin.left, (c.top + c.bottom) / 2 - origin.top);
      }
      chars.push([text[i], picked]);
    }
  }
  const parts = [];
  for (const { chars } of blocks) {
    const hits = chars.reduce((n, [, picked]) => n + picked, 0);
    if (!hits) continue;
    const visible = chars.filter(([ch]) => ch.trim()).length || 1;
    if (hits / visible >= .8) { parts.push(chars.map(([ch]) => ch).join("")); continue; }  // most of it: all of it
    const first = chars.findIndex(([, picked]) => picked);
    let last = chars.length - 1;
    while (!chars[last][1]) last--;
    parts.push(chars.slice(first, last + 1).map(([ch]) => ch).join(""));  // no holes in the middle
  }
  const out = parts.join("\n");
  return out.replace(/[ \t]+\n/g, "\n").trim();
}

let circled = "";
function pickCircled(poly) {
  const text = poly.length > 4 ? circledText(poly) : "";
  if (!text) {
    circled = "";
    askQuote.textContent = "No text selected. Circle the text again.";
    ask.hidden = false;
    document.getElementById("ask-chips").hidden = true;
    document.getElementById("ask-form").hidden = true;
    return;
  }
  circled = text.slice(0, 4000);
  askQuote.textContent = "";
  const b = document.createElement("b"); b.textContent = `Selected ${text.length} characters`;
  askQuote.append(b, document.createTextNode("：" + text.replace(/\s+/g, " ").slice(0, 60) + (text.length > 60 ? "…" : "")));
  document.getElementById("ask-chips").hidden = false;
  document.getElementById("ask-form").hidden = false;
  ask.hidden = false;
  askEntry.value = "";
  emit({ event: "selection", text: circled, key: readerKey });
}

function clearLasso(quiet) {
  const had = !!(lasso || circled);
  lasso = null; circled = ""; ask.hidden = true;
  redrawInk();
  if (had && !quiet) emit({ event: "selection", text: "" });
  if (had && quiet) emit({ event: "selection", text: "" });
}

function askAbout(request) {
  if (!circled || !request.trim()) return;
  emit({ event: "ask_about", text: circled, request: request.trim() });
}
for (const b of document.querySelectorAll("#ask-chips button")) b.addEventListener("click", () => askAbout(b.textContent));
document.getElementById("ask-form").addEventListener("submit", (e) => { e.preventDefault(); askAbout(askEntry.value); });
document.getElementById("ask-cancel").addEventListener("click", () => clearLasso());
askEntry.addEventListener("keydown", (e) => { if (e.key === "Escape") { e.stopPropagation(); clearLasso(); } });

document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape" || reader.hidden) return;
  if (document.activeElement === entry) return;
  if (lasso || circled) clearLasso(); else closeReader();
});

// ---------------------------------------------------------------- settings in the page (2026-10-17)
// "Keep floating panels inside the main window": MYN sends a description of a settings
// panel ({key, title, width, body: [nodes]}); the page draws it inside the main view and
// reports what you do: {event: "sheet", key, action, values}. Confirmations and small forms
// open inside the same box (sheet_confirm / sheet_modal), messages as a line at its foot.
const sheet = document.getElementById("sheet");
const sheetBody = document.getElementById("sheet-body");
const sheetTitle = document.getElementById("sheet-title");
const sheetToast = document.getElementById("sheet-toast");
const sheetModal = document.getElementById("sheet-modal");
const sheetModalBox = document.getElementById("sheet-modal-box");
let sheetKey = null, toastTimer = 0, confirmId = null;
const tabMemory = {};  // the tab you were on, per tabs id

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined && text !== null) e.textContent = String(text);
  return e;
}
function svgEl(tag, attrs) {
  const e = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const k in attrs || {}) e.setAttribute(k, attrs[k]);
  return e;
}

function sheetEmit(action, scope, extra) {
  emit(Object.assign({ event: "sheet", key: sheetKey, action, values: collect(scope || sheetBody),
                       from: scope === sheetModalBox ? "modal" : "main" }, extra || {}));
}

function collect(root) {
  const values = {};
  for (const input of root.querySelectorAll("[data-id]")) {
    const id = input.dataset.id;
    if (input.classList.contains("ui-radios")) {
      const on = input.querySelector("input:checked");
      values[id] = on ? on.value : null;
    } else if (input.type === "checkbox") values[id] = input.checked;
    else if (input.type === "range") values[id] = Number(input.value);
    else if (input.dataset.kind === "table" || input.dataset.kind === "cal" || input.dataset.kind === "tabs") values[id] = input.dataset.value || null;
    else values[id] = input.value;
  }
  if (root !== sheetBody) Object.assign(values, Object.assign(collect(sheetBody), values));
  return values;
}

const FMT = {
  usd: (v) => "$" + (Math.round(v * 100) / 100).toFixed(2),
  num: (v) => v >= 1e9 ? (v / 1e9).toFixed(1) + "B" : v >= 1e6 ? (v / 1e6).toFixed(1) + "M" : v >= 1e3 ? (v / 1e3).toFixed(1) + "K" : String(Math.round(v)),
};
let tip = null;
function showTip(text, x, y) {
  if (!tip) { tip = el("div", "ui-tip"); document.body.appendChild(tip); }
  tip.textContent = text; tip.hidden = false;
  tip.style.left = Math.min(x + 14, window.innerWidth - tip.offsetWidth - 8) + "px";
  tip.style.top = Math.max(8, y - tip.offsetHeight - 10) + "px";
}
function hideTip() { if (tip) tip.hidden = true; }

function barsChart(node) {
  const box = el("div", "ui-chart");
  if (node.title) box.appendChild(el("div", "ctitle", node.title));
  const W = 640, H = node.height || 220, L = 52, B = 26, T = 8, R = 6;
  const labels = node.labels || [], series = node.series || [];
  const totals = labels.map((_, i) => series.reduce((s, ser) => s + (ser.values[i] || 0), 0));
  let max = Math.max(node.min_max || 0, ...totals, 0);
  if (max <= 0) max = 1;
  const step = Math.pow(10, Math.floor(Math.log10(max))), nice = Math.ceil(max / step) * step;
  const fmt = FMT[node.fmt] || FMT.num;
  const svg = svgEl("svg", { viewBox: `0 0 ${W} ${H}`, preserveAspectRatio: "none", height: H });
  for (let k = 0; k <= 4; k++) {
    const y = T + (H - T - B) * (1 - k / 4);
    svg.appendChild(svgEl("line", { x1: L, x2: W - R, y1: y, y2: y, stroke: "#17303d", "stroke-dasharray": "3 4" }));
    const t = svgEl("text", { x: L - 6, y: y + 4, "text-anchor": "end", fill: "#6f8f9b", "font-size": 11 });
    t.textContent = fmt(nice * k / 4); svg.appendChild(t);
  }
  const n = Math.max(1, labels.length), slot = (W - L - R) / n, bw = Math.max(2, Math.min(34, slot * .62));
  labels.forEach((label, i) => {
    const x = L + slot * i + (slot - bw) / 2;
    let y = H - B;
    const dim = node.future_from != null && i >= node.future_from;
    series.forEach((ser) => {
      const v = ser.values[i] || 0;
      if (v <= 0) return;
      const h = (H - T - B) * v / nice;
      y -= h;
      svg.appendChild(svgEl("rect", { x, y, width: bw, height: Math.max(1, h), rx: 2, fill: ser.color, opacity: dim ? .35 : (node.highlight === i || node.highlight == null ? 1 : .55) }));
    });
    if (!totals[i]) svg.appendChild(svgEl("rect", { x, y: H - B - 2, width: bw, height: 2, fill: "#1d3443" }));
    const every = Math.ceil(n / 12);
    if (i % every === 0) {
      const t = svgEl("text", { x: x + bw / 2, y: H - 8, "text-anchor": "middle", fill: dim ? "#3f5c68" : (node.highlight === i ? "#5fd4e6" : "#8fb0bb"), "font-size": 11 });
      t.textContent = label; svg.appendChild(t);
    }
    const hit = svgEl("rect", { x: L + slot * i, y: T, width: slot, height: H - T - B, fill: "transparent" });
    hit.addEventListener("mousemove", (e) => {
      const lines = [label + "：" + fmt(totals[i])].concat(series.filter((s) => s.values[i]).map((s) => "  " + s.name + " " + fmt(s.values[i])));
      showTip(lines.join("\n"), e.clientX, e.clientY);
    });
    hit.addEventListener("mouseleave", hideTip);
    svg.appendChild(hit);
  });
  box.appendChild(svg);
  if (series.length > 1 || node.legend) {
    const lg = el("div", "legend");
    for (const s of series) { const item = el("span"); const sw = el("i"); sw.style.background = s.color; item.append(sw, document.createTextNode(s.name)); lg.appendChild(item); }
    box.appendChild(lg);
  }
  if (node.empty && !totals.some(Boolean)) {
    const t = svgEl("text", { x: W / 2, y: (H - B) / 2, "text-anchor": "middle", fill: "#6f8f9b", "font-size": 13 });
    t.textContent = node.empty; svg.appendChild(t);
  }
  return box;
}

function donutChart(node) {
  const box = el("div", "ui-chart");
  if (node.title) box.appendChild(el("div", "ctitle", node.title));
  const items = (node.items || []).filter((i) => i.value > 0);
  const total = items.reduce((s, i) => s + i.value, 0);
  const svg = svgEl("svg", { viewBox: "0 0 200 140", height: 140 });
  const cx = 70, cy = 70, r = 52, w = 18;
  if (!total) {
    svg.appendChild(svgEl("circle", { cx, cy, r, fill: "none", stroke: "#1d3443", "stroke-width": w }));
  } else {
    let a = -Math.PI / 2;
    for (const it of items) {
      const da = 2 * Math.PI * it.value / total;
      const large = da > Math.PI ? 1 : 0;
      const x1 = cx + r * Math.cos(a), y1 = cy + r * Math.sin(a), x2 = cx + r * Math.cos(a + da - 1e-4), y2 = cy + r * Math.sin(a + da - 1e-4);
      const path = svgEl("path", { d: `M${x1} ${y1} A${r} ${r} 0 ${large} 1 ${x2} ${y2}`, fill: "none", stroke: it.color, "stroke-width": w });
      path.addEventListener("mousemove", (e) => showTip(it.label + "：" + (it.text || it.value), e.clientX, e.clientY));
      path.addEventListener("mouseleave", hideTip);
      svg.appendChild(path);
      a += da;
    }
  }
  const c = svgEl("text", { x: cx, y: cy + 5, "text-anchor": "middle", fill: "#dff4f8", "font-size": 13 });
  c.textContent = node.center || ""; svg.appendChild(c);
  let ly = 22;
  for (const it of items.slice(0, 6)) {
    svg.appendChild(svgEl("rect", { x: 136, y: ly - 8, width: 8, height: 8, rx: 2, fill: it.color }));
    const t = svgEl("text", { x: 148, y: ly, fill: "#a9c6cf", "font-size": 10.5 });
    t.textContent = (it.label.length > 7 ? it.label.slice(0, 7) + "…" : it.label) + " " + (it.share || "");
    svg.appendChild(t); ly += 18;
  }
  if (!total) { const t = svgEl("text", { x: 150, y: 74, "text-anchor": "middle", fill: "#6f8f9b", "font-size": 12 }); t.textContent = node.empty || "No data"; svg.appendChild(t); }
  box.appendChild(svg);
  return box;
}

function hbarsChart(node) {
  const box = el("div", "ui-chart");
  if (node.title) box.appendChild(el("div", "ctitle", node.title));
  const items = node.items || [];
  const max = Math.max(...items.map((i) => i.value), 0) || 1;
  if (!items.length) box.appendChild(el("div", "ui-p muted", node.empty || "No data"));
  for (const it of items) {
    const row = el("div"); row.style.margin = "5px 0";
    const head = el("div"); head.style.display = "flex"; head.style.justifyContent = "space-between"; head.style.fontSize = "12px";
    head.append(el("span", null, it.label), el("span", null, it.text || ""));
    const track = el("div"); track.style.cssText = "height:6px;background:#132a37;border-radius:3px;margin-top:3px;overflow:hidden";
    const fill = el("div"); fill.style.cssText = `height:100%;width:${(100 * it.value / max).toFixed(1)}%;background:${it.color || "#38d7f6"};border-radius:3px`;
    track.appendChild(fill); row.append(head, track); box.appendChild(row);
  }
  return box;
}

function render(nodes, scope) {
  const frag = document.createDocumentFragment();
  for (const node of nodes || []) if (node) frag.appendChild(renderNode(node, scope));
  return frag;
}

function fieldWrap(node, control) {
  const wrap = el("div", "ui-field");
  if (node.label) wrap.appendChild(el("label", null, node.label));
  wrap.appendChild(control);
  if (node.hint) wrap.appendChild(el("div", "hint", node.hint));
  return wrap;
}

function onChange(node, control, scope, event) {
  if (node.on) control.addEventListener(event || "change", () => sheetEmit(node.on, scope));
}

function renderNode(node, scope) {
  switch (node.t) {
    case "usage_receipt": return window.usageReceipt(node, action => sheetEmit(action, scope));
    case "expense_nav": {
      const n = el("div", "expense-nav"); n.appendChild(render(node.children, scope)); return n;
    }
    case "h": return el("div", "ui-h", node.text);
    case "p": return el("div", "ui-p " + (node.tone || "") + (node.mono ? " mono" : ""), node.text);
    case "spacer": return el("div", "ui-spacer");
    case "group": {
      const g = el("div", "ui-group");
      if (node.title) g.appendChild(el("div", "ui-gtitle", node.title));
      g.appendChild(render(node.children, scope));
      return g;
    }
    case "row": { const r = el("div", "ui-row"); r.appendChild(render(node.children, scope)); return r; }
    case "split": {
      const s = el("div", "ui-split" + (node.wide_left ? " wide-left" : ""));
      for (const col of node.cols || []) { const c = el("div"); c.style.minWidth = "0"; c.appendChild(render(col, scope)); s.appendChild(c); }
      return s;
    }
    case "field": {
      let control;
      if (node.kind === "textarea") { control = el("textarea", "ui-area"); control.rows = node.rows || 4; }
      else { control = el("input", "ui-input"); control.type = node.kind === "password" ? "password" : node.kind === "number" ? "number" : "text"; }
      control.dataset.id = node.id;
      control.value = node.value == null ? "" : String(node.value);
      if (node.placeholder) control.placeholder = node.placeholder;
      if (node.width) control.style.width = node.width + "px";
      control.spellcheck = false;
      if (node.enter) control.addEventListener("keydown", (e) => { if (e.key === "Enter" && control.tagName === "INPUT") { e.preventDefault(); sheetEmit(node.enter, scope); } });
      const w = fieldWrap(node, control);
      if (node.width) w.style.flex = "0 0 auto";
      return w;
    }
    case "select": {
      const control = el("select", "ui-select");
      control.dataset.id = node.id;
      for (const [value, label] of node.options || []) { const o = el("option", null, label); o.value = value; control.appendChild(o); }
      control.value = node.value == null ? "" : String(node.value);
      onChange(node, control, scope);
      return fieldWrap(node, control);
    }
    case "check": {
      const label = el("label", "ui-check");
      const box = el("input"); box.type = "checkbox"; box.dataset.id = node.id; box.checked = !!node.value;
      onChange(node, box, scope);
      label.append(box, document.createTextNode(node.label || ""));
      return label;
    }
    case "radio": {
      const group = el("div", "ui-radios"); group.dataset.id = node.id;
      const name = "r" + Math.random().toString(36).slice(2);
      for (const [value, text] of node.options || []) {
        const label = el("label"); const r = el("input"); r.type = "radio"; r.name = name; r.value = value;
        r.checked = String(node.value) === String(value);
        onChange(node, r, scope);
        label.append(r, document.createTextNode(text)); group.appendChild(label);
      }
      return fieldWrap(node, group);
    }
    case "slider": {
      const wrap = el("div", "ui-slider");
      const range = el("input"); range.type = "range"; range.dataset.id = node.id;
      range.min = node.min; range.max = node.max; range.step = node.step || 1; range.value = node.value;
      const out = el("output", null, (node.value) + (node.unit || ""));
      range.addEventListener("input", () => { out.textContent = range.value + (node.unit || ""); });
      onChange(node, range, scope);
      wrap.append(range, out);
      return fieldWrap(node, wrap);
    }
    case "buttons": {
      const row = el("div", "ui-buttons" + (node.align === "right" ? " right" : ""));
      for (const b of node.items || []) {
        const button = el("button", "ui-btn " + (b.kind || ""), b.label);
        button.type = "button";
        if (b.disabled) button.disabled = true;
        if (b.title) button.title = b.title;
        if (b.press) {  // hold to talk (wake word training)
          button.addEventListener("pointerdown", (e) => { button.setPointerCapture(e.pointerId); button.classList.add("primary"); sheetEmit(b.press, scope); });
          const up = () => { if (button.classList.contains("primary")) { button.classList.remove("primary"); sheetEmit(b.release, scope); } };
          button.addEventListener("pointerup", up); button.addEventListener("pointercancel", up);
        } else button.addEventListener("click", () => sheetEmit(b.action, scope));
        row.appendChild(button);
      }
      return row;
    }
    case "kv": {
      const dl = el("dl", "ui-kv");
      for (const [k, v] of node.items || []) dl.append(el("dt", null, k), el("dd", null, v));
      return dl;
    }
    case "cards": {
      const grid = el("div", "ui-cards");
      for (const c of node.items || []) { const card = el("div", "ui-card"); card.append(el("div", "name", c.name), el("div", "value", c.value), el("div", "detail", c.detail || "")); grid.appendChild(card); }
      return grid;
    }
    case "charts": { const g = el("div", "ui-charts"); g.appendChild(render(node.children, scope)); return g; }
    case "bars": return barsChart(node);
    case "donut": return donutChart(node);
    case "hbars": return hbarsChart(node);
    case "tabs": {
      const wrap = el("div"); wrap.dataset.id = node.id; wrap.dataset.kind = "tabs";
      const bar = el("div", "ui-tabs"); wrap.appendChild(bar);
      const value = tabMemory[node.id] || node.value || (node.tabs[0] && node.tabs[0].key);
      wrap.dataset.value = value;
      const panes = [];
      for (const tab of node.tabs || []) {
        const b = el("button", "ui-tab" + (tab.key === value ? " on" : ""), tab.label); b.type = "button";
        const pane = el("div", "ui-pane"); pane.hidden = tab.key !== value; pane.appendChild(render(tab.children, scope));
        b.addEventListener("click", () => {
          for (const o of bar.children) o.classList.toggle("on", o === b);
          for (const p of panes) p.hidden = p !== pane;
          wrap.dataset.value = tab.key; tabMemory[node.id] = tab.key;
          if (node.on) sheetEmit(node.on, scope);
        });
        bar.appendChild(b); panes.push(pane); wrap.appendChild(pane);
      }
      return wrap;
    }
    case "table": {
      const wrap = el("div", "ui-table-wrap"); wrap.dataset.id = node.id; wrap.dataset.kind = "table";
      wrap.dataset.value = node.selected == null ? "" : String(node.selected);
      if (node.height) wrap.style.maxHeight = node.height + "px";
      const table = el("table", "ui-table");
      if (node.columns) { const tr = el("tr"); for (const c of node.columns) tr.appendChild(el("th", null, c)); const th = el("thead"); th.appendChild(tr); table.appendChild(th); }
      const body = el("tbody");
      if (!(node.rows || []).length) { const tr = el("tr"); const td = el("td", "empty", node.empty || "(Empty)"); td.colSpan = (node.columns || [1]).length; tr.appendChild(td); body.appendChild(tr); }
      for (const row of node.rows || []) {
        const tr = el("tr", "row" + (String(row.key) === wrap.dataset.value ? " on" : "") + (row.tone ? " " + row.tone : ""));
        for (const cell of row.cells) {
          if (cell && typeof cell === "object" && cell.cd != null) {  // a countdown, ticking in the page
            const td = el("td"); td.dataset.cd = cell.cd; td.textContent = clockText(cell.cd); tr.appendChild(td);
          } else tr.appendChild(el("td", null, cell));
        }
        tr.addEventListener("click", () => {
          for (const o of body.children) o.classList.toggle("on", o === tr);
          wrap.dataset.value = String(row.key);
          if (node.on) sheetEmit(node.on, scope);
        });
        if (node.open) tr.addEventListener("dblclick", () => { wrap.dataset.value = String(row.key); sheetEmit(node.open, scope); });
        body.appendChild(tr);
      }
      table.appendChild(body); wrap.appendChild(table);
      return wrap;
    }
    case "calendar": {
      const wrap = el("div"); wrap.dataset.id = node.id; wrap.dataset.kind = "cal"; wrap.dataset.value = node.chosen || "";
      const table = el("table", "ui-cal");
      const head = el("tr"); for (const w of ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]) head.appendChild(el("th", null, w));
      table.appendChild(head);
      for (const week of node.weeks || []) {
        const tr = el("tr");
        for (const day of week) {
          const td = el("td", day ? "" : "none");
          if (day) {
            td.classList.toggle("today", !!day.today); td.classList.toggle("on", day.date === node.chosen);
            td.appendChild(el("div", "d", day.day));
            if (day.dots) { const dots = el("div", "dots"); for (let k = 0; k < Math.min(day.dots, 4); k++) dots.appendChild(el("i")); td.appendChild(dots); }
            td.addEventListener("click", () => { wrap.dataset.value = day.date; if (node.on) sheetEmit(node.on, scope); });
            if (node.open) td.addEventListener("dblclick", () => { wrap.dataset.value = day.date; sheetEmit(node.open, scope); });
          }
          tr.appendChild(td);
        }
        table.appendChild(tr);
      }
      wrap.appendChild(table);
      return wrap;
    }
    default: return el("div", "ui-p muted", "");
  }
}

function clockText(endMs) {
  const left = Math.max(0, Math.ceil((Number(endMs) - Date.now()) / 1000));
  const h = Math.floor(left / 3600), m = Math.floor(left % 3600 / 60), sec = left % 60;
  const two = (n) => String(n).padStart(2, "0");
  return h ? `${h}:${two(m)}:${two(sec)}` : `${two(m)}:${two(sec)}`;
}
setInterval(() => { for (const c of document.querySelectorAll("[data-cd]")) c.textContent = clockText(c.dataset.cd); }, 500);

function showSheet(spec) {
  const same = spec.key === sheetKey && !sheet.hidden;
  const oldReceipt = same ? sheetBody.querySelector(".usage-receipt-layout") : null;
  const oldPrintKey = oldReceipt ? oldReceipt.dataset.printKey : null;
  const printStartedAt = oldReceipt?.printStartedAt;
  const metricsStartedAt = oldReceipt?.metricsStartedAt;
  const metricsSnapshot = oldReceipt?.metricsSnapshot?.();
  const metricsTarget = oldReceipt?.metricsTarget;
  const metricsFrom = oldReceipt?.metricsFrom;
  oldReceipt?.stopMetrics?.();
  oldReceipt?.stopPrint?.();
  const receiptScroll = oldReceipt?.querySelector(".receipt-feed")?.scrollTop || 0;
  const categoryScroll = oldReceipt?.querySelector(".expense-category-list")?.scrollTop || 0;
  const scroll = same ? sheetBody.scrollTop : 0;
  const tableScroll = same ? [...sheetBody.querySelectorAll(".ui-table-wrap")].map((t) => t.scrollTop) : [];
  sheetKey = spec.key;
  const coreSheet = ["usage", "agents", "audio_console"].includes(spec.key);
  sheet.classList.toggle("usage-sheet", spec.key === "usage");
  sheet.classList.toggle("agents-sheet", spec.key === "agents" || spec.key === "audio_console");
  document.body.classList.toggle("expenses-open", spec.key === "usage");
  document.body.classList.toggle("core-sheet-open", coreSheet);
  if (!same) expensesEntrance?.cancel();
  if (coreSheet && !same) {
    window.TimerHUD?.closeAlarm();
    window.MediaPlayer?.closeMini();
    if (!matchMedia("(prefers-reduced-motion: reduce)").matches)
      expensesEntrance = sheet.animate([{opacity:0}, {opacity:1}], {duration:400, easing:"linear"});
  }
  sheetTitle.textContent = spec.title || "";
  sheet.style.setProperty("--sheet-w", (spec.width || 720) + "px");
  sheetBody.textContent = "";
  sheetBody.style.minHeight = "";
  sheetBody.appendChild(render(spec.body, sheetBody));
  // Lay out the visible sheet before measuring/starting the first paper.
  if (sheet.hidden) {
    sheet.hidden = false;
    document.body.classList.add("paneled");
  }
  const receipt = sheetBody.querySelector(".usage-receipt-layout");
  if (!receipt || receipt.dataset.printKey !== oldPrintKey) window.ReceiptViewer?.close();
  if (receipt && receipt.dataset.printKey !== oldPrintKey) receipt.startPrint();
  else if (receipt) {
    if (printStartedAt != null) receipt.startPrint(printStartedAt, false);
    receipt.querySelector(".receipt-feed").scrollTop = receiptScroll;
    if (metricsSnapshot && metricsTarget !== receipt.metricsTarget)
      receipt.startMetrics?.(performance.now(), metricsSnapshot);
    else if (metricsStartedAt != null) receipt.startMetrics?.(metricsStartedAt, metricsFrom);
  }
  const categoryList = receipt?.querySelector(".expense-category-list");
  if (categoryList) {
    categoryList.scrollTop = categoryScroll;
    const selected = categoryList.querySelector(".selected");
    if (selected && receipt.dataset.printKey !== oldPrintKey) {
      const a = selected.getBoundingClientRect(), b = categoryList.getBoundingClientRect();
      if (a.bottom > b.bottom) categoryList.scrollTop += a.bottom - b.bottom;
      else if (a.top < b.top) categoryList.scrollTop -= b.top - a.top;
    }
  }
  if (same) {
    sheetBody.scrollTop = scroll;
    sheetBody.querySelectorAll(".ui-table-wrap").forEach((t, i) => { if (tableScroll[i] != null) t.scrollTop = tableScroll[i]; });
  }
}

function closeSheet(tell) {
  sheetBody.querySelector(".usage-receipt-layout")?.stopMetrics?.();
  sheetBody.querySelector(".usage-receipt-layout")?.stopPrint?.();
  window.ReceiptViewer?.close();
  if (sheet.hidden) return;
  sheet.hidden = true; sheetModal.hidden = true; hideTip(); sheetBody.style.minHeight = "";
  expensesEntrance?.cancel();
  document.body.classList.remove("expenses-open", "core-sheet-open");
  document.body.classList.remove("paneled");
  if (tell) emit({ event: "sheet_closed", key: sheetKey });
  sheetKey = null;
}

function toast(text, tone) {
  sheetToast.textContent = text; sheetToast.className = tone || ""; sheetToast.hidden = !text;
  clearTimeout(toastTimer);
  if (text) toastTimer = setTimeout(() => { sheetToast.hidden = true; }, tone === "bad" ? 9000 : 4500);
}

function showModal(spec) {
  sheetModalBox.style.setProperty("--modal-w", (spec.width || 460) + "px");
  sheetModal.style.setProperty("--modal-w", (spec.width || 460) + "px");
  sheetModalBox.textContent = "";
  if (spec.title) sheetModalBox.appendChild(el("div", "ui-modal-title", spec.title));
  sheetModalBox.appendChild(render(spec.body, sheetModalBox));
  sheetModal.hidden = false;
  const first = sheetModalBox.querySelector("input, textarea, select");
  if (first) setTimeout(() => first.focus(), 30);
}

function showConfirm(cmd) {
  confirmId = cmd.id;
  sheetModal.style.setProperty("--modal-w", "440px");
  sheetModalBox.textContent = "";
  if (cmd.title) sheetModalBox.appendChild(el("div", "ui-modal-title", cmd.title));
  sheetModalBox.appendChild(el("div", "ui-confirm-text", cmd.text));
  const row = el("div", "ui-buttons right");
  const no = el("button", "ui-btn", cmd.no || "No"); no.type = "button";
  const yes = el("button", "ui-btn " + (cmd.danger ? "danger" : "primary"), cmd.yes || "Yes"); yes.type = "button";
  const answer = (value) => { sheetModal.hidden = true; const id = confirmId; confirmId = null; emit({ event: "sheet_answer", id, yes: value }); if (cmd.standalone) closeSheet(false); };
  no.addEventListener("click", () => answer(false)); yes.addEventListener("click", () => answer(true));
  row.append(no, yes); sheetModalBox.appendChild(row);
  if (sheet.hidden) {  // asked while no panel is open: a small box of its own
    sheet.classList.remove("usage-sheet", "agents-sheet");
    document.body.classList.remove("expenses-open", "core-sheet-open");
    sheetKey = "__confirm"; sheetTitle.textContent = cmd.title || "MYN";
    sheet.style.setProperty("--sheet-w", "480px"); sheetBody.textContent = ""; sheetBody.style.minHeight = "120px";
    sheet.hidden = false; document.body.classList.add("paneled"); cmd.standalone = true;
  }
  sheetModal.hidden = false;
  setTimeout(() => no.focus(), 30);  // the safe answer is the default
}

document.getElementById("sheet-close").addEventListener("click", () => {
  if (confirmId !== null) { emit({ event: "sheet_answer", id: confirmId, yes: false }); confirmId = null; }
  closeSheet(true);
});
document.getElementById("expenses-core-slot").addEventListener("click", () => {
  document.getElementById("sheet-close").click();
});
document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape" || sheet.hidden) return;
  e.stopImmediatePropagation(); e.preventDefault();
  if (!sheetModal.hidden) {
    if (confirmId !== null) { const id = confirmId; confirmId = null; emit({ event: "sheet_answer", id, yes: false }); }
    else emit({ event: "sheet", key: sheetKey, action: "modal_cancel", values: {}, from: "modal" });
    sheetModal.hidden = true;
    if (sheetKey === "__confirm") closeSheet(false);
    return;
  }
  closeSheet(true);
}, true);
sheet.addEventListener("wheel", (e) => e.stopPropagation(), { passive: true });

// ---------------------------------------------------------------- start
resize();
requestAnimationFrame(frame);
function announce() { emit({ event: "ready" }); }
if (window.pywebview && window.pywebview.api) announce();
else window.addEventListener("pywebviewready", announce, { once: true });
