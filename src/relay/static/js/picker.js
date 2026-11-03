// Folder picker: browse the workspace roots and choose a project folder.

import { api } from "./api.js";
import { clear, h, icon, modal, promptDialog, toast } from "./ui.js";

export function pickFolder(current) {
  return new Promise((resolve) => {
    let chosen = null;
    const m = modal("Choose a project folder", { onClose: () => resolve(chosen) });
    const crumbs = h("div.crumbs");
    const list = h("div.dir-list");
    const selectButton = h("button.btn.primary", { disabled: true }, "Use this folder");
    const newButton = h("button.btn", { disabled: true }, icon("folderPlus", "sm"), "New folder");
    m.body.append(h("p.muted.picker-note",
      "Agents run in this folder. Only folders inside your workspace roots can be used."),
      crumbs, list);
    m.foot.append(newButton, h("span.grow"), h("button.btn", { onclick: () => m.close() }, "Cancel"), selectButton);
    let path = null;

    async function show(target, { quiet = false, intoSingleRoot = false } = {}) {
      try {
        const data = await api.get(target ? `/api/workspaces?path=${encodeURIComponent(target)}` : "/api/workspaces");
        const [only, ...others] = data.path ? [] : data.entries;
        if (intoSingleRoot && only && !others.length && only.exists !== false) {
          show(only.path);  // one workspace root: nothing to choose there
          return;
        }
        path = data.path;
        clear(crumbs,
          data.path ? [
            data.parent
              ? h("button.icon-btn.sm", { title: "Up", onclick: () => show(data.parent) }, icon("arrowLeft", "sm"))
              : h("button.icon-btn.sm",
                  { title: "Workspace roots", onclick: () => show(null) }, icon("arrowLeft", "sm")),
            h("span", data.path),
          ] : h("span", "Workspace roots"));
        clear(list, data.entries.length ? data.entries.map((entry) =>
          h("button", { disabled: entry.exists === false, onclick: () => show(entry.path) },
            icon("folder", "sm"), h("span.grow.truncate", entry.name),
            entry.exists === false ? h("span.faint", "missing") : icon("chevronRight", "sm")))
          : h("div.empty", data.path ? "No subfolders." : "No workspace roots. Add one in Settings → Workspaces."));
        selectButton.disabled = !data.path;
        newButton.disabled = !data.path;
      } catch (error) {
        if (!quiet) toast(error.message, "error");
        if (target) show(null);
      }
    }

    selectButton.addEventListener("click", () => { chosen = path; m.close(); });
    newButton.addEventListener("click", async () => {
      const name = await promptDialog("New folder", { label: "Folder name", confirm: "Create" });
      if (!name || !name.trim()) return;
      try {
        const created = await api.post("/api/workspaces", { parent: path, name: name.trim() });
        show(created.path);
      } catch (error) {
        toast(error.message, "error");
      }
    });

    // Start in the current folder if it is still browsable, else in the only workspace root.
    show(current || null, { quiet: true, intoSingleRoot: true });
  });
}
