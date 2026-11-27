/**
 * Data-driven links. A URL that came from a provider, a file or the server is used as an href or src only
 * when it is an absolute http or https URL; anything else (javascript:, data:, relative paths, junk) gives
 * undefined, and the caller shows plain text instead. React does not block javascript: URLs by itself.
 */
export function safeUrl(raw: unknown): string | undefined {
  if (typeof raw !== "string" || !raw || raw.length > 2048) return undefined;
  let u: URL;
  try {
    u = new URL(raw.trim());
  } catch {
    return undefined;
  }
  return u.protocol === "http:" || u.protocol === "https:" ? u.href : undefined;
}
