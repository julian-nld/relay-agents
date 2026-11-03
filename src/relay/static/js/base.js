// Where Relay is on its site: "" at the root, or the path it's served under (Settings' base_path: behind a proxy
// that serves it at /relay/, or a new provider's trial Relay at /trial/<id>/ of another). The server says it in
// the page (relay-base).

export const BASE =
  (globalThis.document?.querySelector?.('meta[name="relay-base"]')?.content || "").replace(/\/+$/, "");

// One of Relay's absolute paths (/api/…, /c/<id>) as the browser asks for it
export function relayUrl(path) {
  return BASE && path.startsWith("/") && !path.startsWith("//") ? BASE + path : path;
}

// A path as the browser has it, as Relay names it (without the base): "/" at Relay's root
export function stripBase(path) {
  if (!BASE) return path;
  if (path === BASE) return "/";
  return path.startsWith(`${BASE}/`) ? path.slice(BASE.length) : path;
}

// The page's own path, as Relay names it
export function relayPath() {
  return stripBase(location.pathname);
}

// A key in this browser's storage, for this Relay: two on one site (a trial's) keep their own drafts and views
export function storeKey(key) {
  return BASE ? `${BASE}|${key}` : key;
}
