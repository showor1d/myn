/* A shuffled bag per cycle, plus actual playback history for Previous. */
class MusicQueue {
  constructor(random = Math.random) { this.random = random; this.ids = []; this.bag = []; this.seen = new Set(); this.history = []; this.cursor = -1; }
  shuffle(ids) {
    const result = [...ids];
    for (let i = result.length - 1; i > 0; i--) {
      const j = Math.floor(this.random() * (i + 1));
      [result[i], result[j]] = [result[j], result[i]];
    }
    return result;
  }
  sync(ids) {
    this.ids = [...new Set(ids)];
    this.bag = this.bag.filter(id => this.ids.includes(id));
    this.bag.push(...this.shuffle(this.ids.filter(id => !this.seen.has(id) && !this.bag.includes(id))));
  }
  select(id) {
    this.seen.add(id); this.bag = this.bag.filter(value => value !== id);
    this.history = this.history.slice(0, this.cursor + 1);
    if (this.history[this.cursor] !== id) { this.history.push(id); this.cursor++; }
    // Bound history without changing the current cycle.
    if (this.history.length > 1000) { this.history.shift(); this.cursor--; }
    return id;
  }
  next() {
    while (this.cursor + 1 < this.history.length) {
      const id = this.history[++this.cursor];
      if (this.ids.includes(id)) return id;
    }
    if (!this.ids.length) return null;
    if (!this.bag.length) {
      this.seen.clear(); this.bag = this.shuffle(this.ids);
      if (this.bag.length > 1 && this.bag[0] === this.history[this.cursor])
        [this.bag[0], this.bag[1]] = [this.bag[1], this.bag[0]];
    }
    return this.select(this.bag.shift());
  }
  previous() {
    while (this.cursor > 0) {
      const id = this.history[--this.cursor];
      if (this.ids.includes(id)) return id;
    }
    return this.ids.includes(this.history[this.cursor]) ? this.history[this.cursor] : null;
  }
}
if (typeof module !== "undefined") module.exports = MusicQueue;
