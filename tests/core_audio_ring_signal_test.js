/* Verify the rendered contour is driven by measurements, never elapsed time. */
const fs=require('fs'),vm=require('vm'),assert=require('assert');
function create() {
  const scope={window:{addEventListener(){}},performance:{now:()=>0},
    document:{hidden:false},setTimeout:()=>0,clearTimeout(){}};
  vm.runInNewContext(fs.readFileSync('src/web/core_audio_ring.js','utf8'),scope);
  return scope.window.CoreAudioRing;
}
function contour(api,now,dt=0) {
  const points=[],g={save(){},restore(){},beginPath(){},moveTo(){},stroke(){},closePath(){},
    quadraticCurveTo(...p){points.push(p)}};
  api.draw(g,0,0,100,1,dt,now);
  return points;
}
const a=create(),b=create(),bands=Array(128).fill(0);
bands[20]=.9;bands[21]=.7;bands[90]=.4;
for(let i=0;i<160;i++) {
  a.playback(.3,i*16,bands);contour(a,i*16,.016);
  b.playback(.3,100000+i*16,bands);contour(b,100000+i*16,.016);
}
assert.deepStrictEqual(contour(a,2544),contour(b,102544),'Clock must not move peaks');
const previous=contour(a,2544);
const shifted=Array(128).fill(0);shifted[65]=.9;
for(let i=160;i<240;i++) {a.playback(.3,i*16,shifted);contour(a,i*16,.016);}
assert.notDeepStrictEqual(previous,contour(a,3824),'Changed spectrum must change contour');
assert(a.state().bands[65]>.85 && a.state().bands[20]<.01);
for(let i=240;i<400;i++) contour(a,i*16,.016);
assert.equal(a.state().level,0);assert(a.state().bands.every(v=>v===0));
const fallback=create();fallback.playback(.3,0);
const flat=contour(fallback,0,1).slice(256);
const radii=flat.map(p=>Math.hypot(p[0],p[1]));
assert(Math.max(...radii)-Math.min(...radii)<1e-8,'Meter fallback must not invent waves');
console.log('PASS: clock independence, frequency response, silence, truthful meter fallback');
