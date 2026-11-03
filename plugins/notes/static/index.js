// Notes' part of Relay's page: a button on the activity bar with how many notes there are, their page, and a tab in
// Settings. setup(relay) gets what it may add (docs/WRITING-A-PLUGIN.md).

export default function setup(relay) {
  const { h, icon, clear, toast, timeAgo, confirmDialog } = relay.ui;
  const button = relay.barItem({ title: "Notes", icon: "book", onClick: () => relay.navigate("") });

  async function notes() {
    const all = await relay.api.get("notes");
    button.setBadge(all.length);
    return all;
  }
  notes().catch(() => {});

  // Its page, /p/notes: the notes, newest first, and one of your own to add
  relay.page("", async (el) => {
    const list = h("div.notes-list");
    const text = h("textarea.notes-new", { rows: 2, placeholder: "A note…", "aria-label": "New note" });
    const remove = async (note) => {
      await relay.api.del(`notes/${note.id}`);
      draw();
    };
    const item = (note) => h("div.notes-item",
      h("p", note.text),
      h("div.notes-meta", h("span", `${note.by} · ${timeAgo(note.at)}`),
        note.conversation
          ? h("a", { href: "#", onclick: (e) => { e.preventDefault(); relay.navigate(`/c/${note.conversation}`); } },
            "Open its chat") : null,
        h("button.icon-btn.sm",
          { title: "Remove", "aria-label": "Remove", onclick: () => remove(note) }, icon("trash", "sm"))));
    async function draw() {
      const all = await notes();
      clear(list, all.length ? all.slice().reverse().map(item)
        : h("p.muted", "No notes yet. Agents add them with their notes_add tool."));
    }
    const add = h("button.btn.primary.small", { onclick: async () => {
      try {
        await relay.api.post("notes", { text: text.value });
        text.value = "";
        draw();
      } catch (error) {
        toast(error.message, "error");
      }
    } }, "Add");
    clear(el, h("div.notes", h("h1", "Notes"), h("div.notes-add", text, add), list));
    await draw();
    const timer = setInterval(draw, 30000);
    return () => clearInterval(timer);  // (another page opened)
  });

  // A tab in Settings
  relay.settingsTab({ label: "Notes", icon: "book", render: async (pane) => {
    const all = await notes();
    clear(pane, h("h3", "Notes"),
      h("p.lead", `${all.length} note${all.length === 1 ? "" : "s"}; those older than 30 days go by themselves.`),
      h("button.btn.danger.small", { onclick: async () => {
        if (!await confirmDialog("Remove all notes?", "They can't be brought back.",
          { confirm: "Remove all", danger: true })) return;
        await relay.api.del("notes");
        toast("All notes removed");
        notes();
      } }, "Remove all"));
  } });
}
