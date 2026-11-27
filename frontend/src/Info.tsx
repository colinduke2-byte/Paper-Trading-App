import { useEffect, useId, useRef, useState } from "react";
import { GLOSSARY } from "./glossary";

/** A small "i" button that explains a term in a popover. Opens on click or tap (not hover), so it works on phones. */
export function Info({ term, text, title }: { term?: string; text?: string; title?: string }) {
  const entry = term ? GLOSSARY[term] : undefined;
  const heading = title || entry?.title || "";
  const body = text || entry?.text || "";
  const [open, setOpen] = useState(false);
  const id = useId();
  const box = useRef<HTMLSpanElement>(null);
  useEffect(() => {
    if (!open) return;
    const away = (e: Event) => { if (!box.current?.contains(e.target as Node)) setOpen(false); };
    const esc = (e: KeyboardEvent) => { if (e.key === "Escape") { e.stopPropagation(); setOpen(false); } };
    document.addEventListener("pointerdown", away);
    window.addEventListener("keydown", esc, true);
    return () => { document.removeEventListener("pointerdown", away); window.removeEventListener("keydown", esc, true); };
  }, [open]);
  useEffect(() => { // keep the popover inside the window
    if (!open || !box.current) return;
    const pop = box.current.querySelector(".info-pop") as HTMLElement | null;
    if (!pop) return;
    pop.style.left = "";
    const r = pop.getBoundingClientRect();
    const over = r.right - (window.innerWidth - 8);
    if (over > 0) pop.style.left = `${-over}px`;
    else if (r.left < 8) pop.style.left = `${8 - r.left}px`;
  }, [open]);
  if (!body) return null;
  return (
    <span className="info" ref={box}>
      <button type="button" className="info-btn" aria-label={`What is ${heading || "this"}?`} aria-expanded={open} aria-controls={id}
        onClick={(e) => { e.stopPropagation(); e.preventDefault(); setOpen((o) => !o); }}>i</button>
      {open && (
        <span className="info-pop" role="dialog" id={id} aria-label={heading}>
          {heading && <b>{heading}</b>}
          <span>{body}</span>
        </span>
      )}
    </span>
  );
}
