// The simulation clock: "now", unless a time has been pinned.
//
// Everything on the page that needs an epoch - the live horizon, the sun and
// moon, alt/az readouts, the velocity-frame shift - asks this module instead
// of calling new Date() itself, so pinning the clock moves all of them
// together and un-pinning returns the whole page to live time. The 60 s
// redraw keeps running either way; with the clock pinned it simply redraws
// the same sky, which costs nothing and keeps one code path.
//
// The pinned value is what the operator typed in the time box, interpreted
// as UTC - the observatory's convention everywhere else (plots, filenames,
// the log). No timezone handling beyond that: a datetime-local input has no
// zone of its own, so the label on the box is the contract.

let fixed = null; // Date | null; null means live "now"

export function simDate() {
  return fixed ? new Date(fixed.getTime()) : new Date();
}

export function setFixedTime(date) {
  fixed = date instanceof Date && !isNaN(date) ? date : null;
}

export function isFixed() {
  return fixed !== null;
}
