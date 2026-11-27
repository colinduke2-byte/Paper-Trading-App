// Exact arithmetic for lot quantities (at most 4 decimal places, as the server accepts).
// Floats would turn 0.1 + 0.2 into 0.30000000000000004, which the server refuses.

const SCALE = 10000;

// A quantity in ten-thousandths, or NaN when it is not a number.
export function toUnits(q: number | string): number {
  const n = typeof q === "number" ? q : Number(String(q).trim());
  return Number.isFinite(n) && String(q).trim() !== "" ? Math.round(n * SCALE) : NaN;
}

// The sum of the quantities, written as the server expects ("0.3", "12", "1.0001").
export function sumQty(qs: (number | string)[]): string {
  const units = qs.reduce<number>((a, q) => a + (toUnits(q) || 0), 0);
  return String(units / SCALE);
}

// True when two quantities are equal to 4 decimal places.
export function sameQty(a: number | string, b: number | string): boolean {
  return toUnits(a) === toUnits(b);
}
