// Light, dark or system theme, remembered per browser (for every Relay on its site alike: a trial Relay's too).

export function applyTheme(choice = storedTheme()) {
  const dark = choice === "dark" || (choice === "system" && matchMedia("(prefers-color-scheme: dark)").matches);
  document.documentElement.dataset.theme = dark ? "dark" : "light";
  for (const [id, off] of [["hljs-dark", !dark], ["hljs-light", dark]]) {
    const sheet = document.getElementById(id);  // (the app's code colours; an error page has none)
    if (sheet) sheet.disabled = off;
  }
  // The phone's status bar follows the chosen theme, not only the system's.
  document.querySelectorAll('meta[name="theme-color"]').forEach((meta) => {
    meta.content = dark ? "#212121" : "#ffffff";
  });
}

export function storedTheme() {
  try {
    return localStorage.getItem("relay.theme") || "system";
  } catch {
    return "system";
  }
}

export function setTheme(choice) {
  try { localStorage.setItem("relay.theme", choice); } catch { /* private mode */ }
  applyTheme(choice);
}

matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => applyTheme());
applyTheme();
