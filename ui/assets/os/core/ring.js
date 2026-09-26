/* A bounded list. Every list fed by events uses this so a long-running app
   cannot regrow the unbounded arrays the console had (finding #132).
   Oldest entries fall off the front once the cap is reached. */

export function ring(cap) {
  if (!Number.isInteger(cap) || cap < 1) {
    throw new RangeError('ring(cap): cap must be a positive integer');
  }
  const buf = [];
  return {
    cap,
    /* Adds one item and returns how many items were dropped to stay in cap. */
    push(item) {
      buf.push(item);
      const over = buf.length - cap;
      if (over > 0) {
        buf.splice(0, over);
        return over;
      }
      return 0;
    },
    /* Oldest first. A copy, so callers cannot grow the ring by mutating it. */
    items() {
      return buf.slice();
    },
    newestFirst() {
      return buf.slice().reverse();
    },
    last() {
      return buf.length ? buf[buf.length - 1] : undefined;
    },
    clear() {
      buf.length = 0;
    },
    get size() {
      return buf.length;
    },
  };
}
