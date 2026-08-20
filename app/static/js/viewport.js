// What the on-screen keyboard does to a fixed bottom bar.
//
// The bottom navigation and the mini player are `position: fixed; bottom: 0`,
// which pins them to the *layout* viewport. Opening the keyboard doesn't
// shrink that viewport — it shrinks the *visual* one and leaves the layout
// viewport alone, so the bar keeps sitting at a bottom edge that is now
// somewhere behind the keyboard. iOS resolves the contradiction by dragging
// the bar up into the middle of the screen, right above the keys, where it
// covers the content you were typing towards.
//
// So the keyboard is measured here instead: how much of the layout viewport
// it covers goes into --keyboard-inset, and the CSS uses that both to get the
// bottom furniture out of the way and to give the page that much extra room
// to scroll into, so every element can still be brought above the keys.
//
// Deliberately not paired with `interactive-widget=resizes-content` in the
// viewport meta: that flag makes Android's browser resize the layout viewport
// itself, which fixes the drag but leaves the bar parked directly on top of
// the keyboard — and makes the measurement below read zero, so this code
// would stop running on exactly one platform. One behaviour on both is worth
// more than a native-feeling half.

// An on-screen keyboard is always taller than this. Safari's collapsing URL
// bar is not, and neither is the accessory strip on its own — without a floor
// the class would flicker on every scroll.
const KEYBOARD_MIN_HEIGHT = 120;

export function installKeyboardInset() {
  const viewport = window.visualViewport;
  if (!viewport) return;

  function update() {
    // offsetTop counts the part scrolled off the top of the visual viewport,
    // which is how much of the gap is *not* the keyboard.
    const covered = window.innerHeight - viewport.height - viewport.offsetTop;
    const inset = Math.max(0, Math.round(covered));
    document.documentElement.style.setProperty("--keyboard-inset", `${inset}px`);
    document.body.classList.toggle("is-keyboard-open", inset >= KEYBOARD_MIN_HEIGHT);
  }

  viewport.addEventListener("resize", update);
  viewport.addEventListener("scroll", update);
  update();
}
