const assert = require("node:assert/strict");
const MusicQueue = require("../src/web/music_queue.js");
const q = new MusicQueue(() => .23);
q.sync(["a", "b", "c", "d"]);
let last = null;
for (let cycle = 0; cycle < 30; cycle++) {
  const batch = Array.from({length:4}, () => q.next());
  assert.equal(new Set(batch).size, 4);
  assert.notEqual(batch[0], last);
  last = batch[3];
}
const previous = q.previous();
assert.notEqual(previous, last);
assert.equal(q.next(), last);
const manual = new MusicQueue(() => .8);
manual.sync(["a","b","c"]); manual.select("b");
assert.deepEqual(new Set([manual.next(), manual.next()]), new Set(["a","c"]));
manual.sync(["z"]); assert.equal(manual.next(), "z"); assert.equal(manual.next(), "z");
manual.sync([]); assert.equal(manual.next(), null);
const added = new MusicQueue(() => .1);
added.sync(["a","b"]); const first = added.next();
added.sync(["a","b","c"]);
assert.equal(new Set([first,added.next(),added.next()]).size,3);
console.log("Shuffle cycles, history, manual selection, additions/removals and single-file loops passed.");
