// Keyboard behaviour for every modal dialog (any element with role="dialog" and aria-modal="true"), added once
// from main.tsx so each dialog component does not have to repeat it:
//   * on open, focus moves into the dialog (unless a control in it already took focus with autoFocus);
//   * Tab and Shift+Tab stay inside the topmost dialog;
//   * Escape closes the topmost dialog by clicking its backdrop (every dialog here closes on a backdrop click);
//   * on close, focus returns to the control that opened it (or, when a menu item opened a second dialog, to
//     the control that opened the menu).

const FOCUSABLE = 'a[href], button:not([disabled]), input:not([disabled]):not([type=hidden]), select:not([disabled]), ' +
  'textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

const openers = new WeakMap<Element, HTMLElement | null>();
let lastRestore: HTMLElement | null = null;
// The last control focused outside any dialog: a control with autoFocus inside a new dialog takes focus before
// the dialog is noticed here, so the opener is remembered from before that.
let lastOutside: HTMLElement | null = null;

const dialogs = () => Array.from(document.querySelectorAll<HTMLElement>('[role="dialog"][aria-modal="true"]'));
const top = () => { const d = dialogs(); return d[d.length - 1] ?? null; };
const visible = (el: HTMLElement) => el.offsetParent !== null || el.getClientRects().length > 0;
const focusables = (root: HTMLElement) => Array.from(root.querySelectorAll<HTMLElement>(FOCUSABLE)).filter(visible);
const usable = (el: Element | null): el is HTMLElement => !!el && el instanceof HTMLElement && el.isConnected && el !== document.body;

function opened(d: HTMLElement) {
  const active = document.activeElement;
  const opener = usable(active) && !d.contains(active) ? active : usable(lastOutside) ? lastOutside : null;
  openers.set(d, opener ?? lastRestore);
  if (!d.contains(document.activeElement)) {
    const first = focusables(d)[0];
    if (first) first.focus();
    else { if (!d.hasAttribute("tabindex")) d.tabIndex = -1; d.focus(); }
  }
}

function closed(d: Element) {
  const back = openers.get(d) ?? null;
  lastRestore = back;
  // Let React finish the update that removed the dialog (it may open another one), then restore focus.
  queueMicrotask(() => {
    setTimeout(() => {
      if (lastRestore === back) lastRestore = null;
      const t = top();
      if (t) { if (!t.contains(document.activeElement)) (focusables(t)[0] ?? t).focus(); return; }
      if (usable(back) && (document.activeElement === document.body || !document.activeElement)) back.focus();
    }, 0);
  });
}

function each(nodes: NodeList, fn: (d: HTMLElement) => void) {
  nodes.forEach((n) => {
    if (!(n instanceof HTMLElement)) return;
    if (n.matches('[role="dialog"][aria-modal="true"]')) fn(n);
    n.querySelectorAll<HTMLElement>('[role="dialog"][aria-modal="true"]').forEach(fn);
  });
}

export function installDialogKeyboard() {
  new MutationObserver((records) => {
    for (const r of records) each(r.removedNodes, closed);
    for (const r of records) each(r.addedNodes, (d) => { if (d.isConnected) opened(d); });
  }).observe(document.body, { childList: true, subtree: true });

  document.addEventListener("focusin", (e) => {
    const t = e.target;
    if (t instanceof HTMLElement && !t.closest('[role="dialog"]')) lastOutside = t;
  });

  document.addEventListener("keydown", (e) => {
    const d = top();
    if (!d) return;
    if (e.key === "Escape") {
      const bg = d.parentElement;
      if (bg && bg !== document.body) { e.preventDefault(); bg.click(); }
      return;
    }
    if (e.key !== "Tab") return;
    const items = focusables(d);
    if (items.length === 0) { e.preventDefault(); d.focus(); return; }
    const first = items[0], last = items[items.length - 1];
    const active = document.activeElement as HTMLElement | null;
    if (!active || !d.contains(active)) { e.preventDefault(); (e.shiftKey ? last : first).focus(); }
    else if (e.shiftKey && active === first) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && active === last) { e.preventDefault(); first.focus(); }
  });
}
