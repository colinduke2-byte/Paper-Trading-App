import { CSSProperties, ReactNode, useEffect, useRef } from "react";

// Open dialogs, innermost last: Escape and the focus trap act on the top one only.
const stack: HTMLElement[] = [];

const FOCUSABLE = 'a[href], button:not([disabled]), input:not([disabled]):not([type="hidden"]), select:not([disabled]), ' +
  'textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

/** Modal behaviour every dialog shares: Escape closes, focus stays inside while open and goes back to the
 * element that opened it on close. Clicking the backdrop closes it unless `destructive` is set (a dialog
 * that erases something closes only with its own buttons or Escape). Each dialog keeps its own look through
 * className and style. Give it a name with labelledBy (the id of its heading) or label. */
export function Dialog({ onClose, labelledBy, label, destructive = false, className = "modal", bgClassName = "modal-bg",
  style, children }: {
  onClose: () => void; labelledBy?: string; label?: string; destructive?: boolean; className?: string;
  bgClassName?: string; style?: CSSProperties; children: ReactNode;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const close = useRef(onClose);
  close.current = onClose;
  const downOnBackdrop = useRef(false);
  // The opener is read during the first render: by the time effects run, an autoFocus field inside has focus.
  const opener = useRef<HTMLElement | null | undefined>(undefined);
  if (opener.current === undefined) opener.current = document.activeElement as HTMLElement | null;

  useEffect(() => {
    const el = ref.current!;
    const back = opener.current;
    stack.push(el);
    // keep an autoFocus field's focus; otherwise focus the first control (or the dialog itself)
    if (!el.contains(document.activeElement)) (el.querySelector<HTMLElement>(FOCUSABLE) || el).focus();
    const onKey = (e: KeyboardEvent) => {
      if (stack[stack.length - 1] !== el) return;
      if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); close.current(); return; }
      if (e.key !== "Tab") return;
      const items = Array.from(el.querySelectorAll<HTMLElement>(FOCUSABLE)).filter((x) => x.offsetParent !== null || x === document.activeElement);
      if (!items.length) { e.preventDefault(); el.focus(); return; }
      const first = items[0], last = items[items.length - 1];
      const inside = el.contains(document.activeElement);
      if (e.shiftKey && (document.activeElement === first || !inside)) { e.preventDefault(); last.focus(); }
      else if (!e.shiftKey && (document.activeElement === last || !inside)) { e.preventDefault(); first.focus(); }
    };
    document.addEventListener("keydown", onKey, true);
    return () => {
      document.removeEventListener("keydown", onKey, true);
      const i = stack.lastIndexOf(el);
      if (i >= 0) stack.splice(i, 1);
      if (back && back !== document.body && back.isConnected && typeof back.focus === "function") back.focus();
    };
  }, []);

  return (
    <div className={bgClassName}
      onMouseDown={(e) => { downOnBackdrop.current = e.target === e.currentTarget; }}
      onClick={(e) => { if (!destructive && downOnBackdrop.current && e.target === e.currentTarget) onClose(); }}>
      <div ref={ref} className={className} style={style} role="dialog" aria-modal="true" tabIndex={-1}
        aria-labelledby={labelledBy} aria-label={labelledBy ? undefined : label}>
        {children}
      </div>
    </div>
  );
}
