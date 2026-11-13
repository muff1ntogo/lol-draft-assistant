// The app's one toast: a short confirmation of something just done, with an optional way to undo it
// (ui/Toast.svelte draws it; App.svelte mounts that once). A new toast replaces the old one.
//   toast('Swapped Ana in for Ben', { action: { label: 'Undo', onclick: swapBack }, seconds: 10 })
let current = $state(null);
let timer;

export function toast(text, { action = null, seconds = 6 } = {}) {
  clearTimeout(timer);
  current = { id: Date.now() + Math.random(), text, action };
  timer = setTimeout(dismiss, seconds * 1000);
}
export function dismiss() {
  clearTimeout(timer);
  current = null;
}
export const toastState = { get current() { return current; } };
