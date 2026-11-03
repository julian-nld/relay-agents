// The side column's views (Relay 2.11): which there are, which one a page belongs to, how wide the column is.

// [id, label, icon]: the activity bar's views, top to bottom
export const VIEWS = [
  ["chats", "Chats", "chat"],
  ["tasks", "Tasks", "list"],
  ["agentos", "AgentOS", "compass"],
  ["governor", "Governor", "bot"],
  ["scripts", "Scripts", "fileCode"],
  ["production", "Production boxes", "box"],
];

export const WIDTH = { min: 220, max: 480, standard: 288 };  // the column's, in px

// The view a page belongs to, when Relay starts on it (afterwards only the bar changes the view: the chat on the
// right stays as it is, whichever view the column shows)
export function viewForPath(path, governorId = null) {
  if (/^\/scripts(\/|$)/.test(path)) return "scripts";
  if (path === "/tasks") return "tasks";
  if (path === "/agentos" || path === "/scheduled") return "agentos";
  if (path.startsWith("/s/")) return "production";
  if (governorId && path === `/c/${governorId}`) return "governor";
  return "chats";
}

// A width as it was kept (or dragged): within bounds, the standard one when it's no number
export function clampWidth(value) {
  const width = Number(value);
  if (!Number.isFinite(width) || width <= 0) return WIDTH.standard;
  return Math.round(Math.min(WIDTH.max, Math.max(WIDTH.min, width)));
}

// A script's page: /scripts/<its name> (private/report.py); none: the scripts' own page
export function scriptOfPath(path) {
  const found = /^\/scripts\/(.+)$/.exec(path);
  try {
    return found ? decodeURIComponent(found[1]) : null;
  } catch {
    return null;  // (an address typed with a stray %: the scripts' own page)
  }
}
