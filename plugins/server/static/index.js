// Server's part of Relay's page: a button with how many warnings hold, and a page with this machine's CPU, memory,
// swap and disks (each with its last 24 hours) and what each container uses (docs/WRITING-A-PLUGIN.md).

const SVG = "http://www.w3.org/2000/svg";
const SPARK = 40;  // the sparklines' height, in px

function svg(tag, attrs = {}) {
  const el = document.createElementNS(SVG, tag);
  for (const [key, value] of Object.entries(attrs)) el.setAttribute(key, value);
  return el;
}

const percent = (fraction) => (fraction === null || fraction === undefined ? "—" : `${Math.round(fraction * 100)}%`);
const size = (bytes) => (bytes >= 1e9 ? `${(bytes / 1e9).toFixed(1)} GB` : `${Math.round(bytes / 1e6)} MB`);
const tone = (fraction) => (fraction >= 0.9 ? "bad" : fraction >= 0.7 ? "warn" : "ok");

function since(seconds) {
  const hours = Math.floor(seconds / 3600);
  return hours >= 48 ? `${Math.floor(hours / 24)} days` : hours ? `${hours} h` : `${Math.floor(seconds / 60)} min`;
}

export default function setup(relay) {
  const { h, icon, clear, clock } = relay.ui;
  const button = relay.barItem({ title: "Server", icon: "monitor", onClick: () => relay.navigate("") });

  async function warned() {
    button.setBadge((await relay.api.get("warnings")).length);
  }
  warned().catch(() => {});
  setInterval(() => warned().catch(() => {}), 60 * 1000);

  const watching = [];  // (the sparklines' ResizeObservers: let go when they're drawn anew)

  // The last 24 hours of one measure (0–1), quiet, its latest point in the accent; the pointer's value in a tooltip
  function sparkline(points, label) {
    const box = h("div.sv-spark");
    const tip = h("div.sv-tip", { hidden: true });
    const shown = points.filter((p) => p.value !== null && p.value !== undefined);
    const graphic = svg("svg", { height: SPARK, role: "img", tabindex: "0",
      "aria-label": shown.length
        ? `${label}, the last ${since(shown.at(-1).t - shown[0].t)}: from ${percent(shown[0].value)} `
        + `to ${percent(shown.at(-1).value)}, at most ${percent(Math.max(...shown.map((p) => p.value)))}`
        : `${label}: no history yet` });
    box.append(graphic, tip);
    if (shown.length < 2) {
      box.append(h("span.sv-spark-empty", "its history starts with Relay"));
      return box;
    }
    let index = shown.length - 1, show = null;
    function render() {
      const width = box.clientWidth;
      if (!width) return;
      graphic.setAttribute("width", width);
      graphic.setAttribute("viewBox", `0 0 ${width} ${SPARK}`);
      graphic.replaceChildren();
      const from = shown[0].t, to = shown.at(-1).t;
      const x = (t) => 3 + (t - from) / Math.max(1, to - from) * (width - 6);
      const y = (v) => 3 + (1 - v) * (SPARK - 6);
      const line = shown.map((p, i) => `${i ? "L" : "M"}${x(p.t).toFixed(1)} ${y(p.value).toFixed(1)}`).join(" ");
      const cross = svg("line", { class: "sv-cross", y1: 0, y2: SPARK, visibility: "hidden" });
      const dot = svg("circle", { class: "sv-dot", r: 3.5, cx: x(to), cy: y(shown.at(-1).value) });
      const hit = svg("rect", { class: "sv-hit", x: 0, y: 0, width, height: SPARK });
      graphic.append(svg("line", { class: "sv-base", x1: 3, x2: width - 3, y1: y(0), y2: y(0) }),
        svg("path", { class: "sv-line", d: line }), cross, dot, hit);
      show = (i) => {
        index = Math.max(0, Math.min(shown.length - 1, i));
        const p = shown[index];
        cross.setAttribute("x1", x(p.t));
        cross.setAttribute("x2", x(p.t));
        cross.setAttribute("visibility", "visible");
        dot.setAttribute("cx", x(p.t));
        dot.setAttribute("cy", y(p.value));
        clear(tip, h("strong", percent(p.value)), h("span", clock(p.t)));
        tip.hidden = false;
        tip.style.left = `${Math.min(Math.max(x(p.t) - 40, 0), width - 90)}px`;  // (CSSOM: allowed by the CSP)
      };
      hit.addEventListener("pointermove", (e) => {
        const at = from + (e.clientX - graphic.getBoundingClientRect().left - 3) / Math.max(1, width - 6) * (to - from);
        let nearest = 0;
        shown.forEach((p, i) => { if (Math.abs(p.t - at) < Math.abs(shown[nearest].t - at)) nearest = i; });
        show(nearest);
      });
      hit.addEventListener("pointerleave", () => { if (document.activeElement !== graphic) reset(); });
    }
    function reset() {
      tip.hidden = true;
      graphic.querySelector(".sv-cross")?.setAttribute("visibility", "hidden");
      const dot = graphic.querySelector(".sv-dot");
      if (dot) {  // (back on the latest point)
        index = shown.length - 1;
        render();
      }
    }
    graphic.addEventListener("focus", () => show?.(index));
    graphic.addEventListener("blur", reset);
    graphic.addEventListener("keydown", (e) => {
      const step = { ArrowLeft: -1, ArrowRight: 1, Home: -shown.length, End: shown.length }[e.key];
      if (step === undefined) return;
      e.preventDefault();
      show?.(index + step);
    });
    const observer = new ResizeObserver(() => render());
    observer.observe(box);
    watching.push(observer);
    return box;
  }

  function tile(label, value, sub, fraction, points) {
    const fill = h("span");
    fill.style.width = `${Math.round(Math.max(0, Math.min(1, fraction ?? 0)) * 100)}%`;
    return h("section.sv-tile",
      h("div.sv-label", label),
      h("div.sv-value", value),
      h("div.sv-sub", sub),
      h(`div.sv-meter.${tone(fraction ?? 0)}`, { role: "meter", "aria-valuemin": "0", "aria-valuemax": "100",
        "aria-valuenow": String(Math.round((fraction ?? 0) * 100)),
        "aria-label": `${label}: ${percent(fraction)}` }, fill),
      sparkline(points, label));
  }

  // Its page, /p/server: warnings, the machine's tiles, the containers
  relay.page("", async (el) => {
    let past = [];
    const head = h("p.lead.sv-lead");
    const warnings = h("div.sv-warnings");
    const tiles = h("div.sv-tiles");
    const rows = h("tbody");
    clear(el, h("div.sv-page", h("h1", "Server"), head, warnings, tiles,
      h("h2", "Containers"),
      h("p.muted.sv-note",
        "What each running container uses: CPU as a share of the whole machine, since the last look."),
      h("table.sv-table", h("thead", h("tr", h("th", "Container"), h("th.num", "CPU"), h("th.num", "Memory"))), rows)));

    const series = (pick) => past.map((p) => ({ t: p.t, value: pick(p) }));
    async function draw() {
      let now;
      try {
        now = await relay.api.get("status");
      } catch (error) {
        clear(warnings, h("p.error-text", error.message));
        return;
      }
      head.textContent = `${now.cores} cores · up ${since(now.uptime)} · load ${now.load.join(" ")}`;
      button.setBadge(now.warnings.length);
      clear(warnings, now.warnings.map((text) => h("div.sv-warning", icon("alert", "sm"), h("span", text))));
      const memory = now.memory, swap = now.swap;
      // (not while the pointer or the keyboard is on them: their tooltip stays)
      if (!tiles.matches(":hover") && !tiles.contains(document.activeElement)) {
        watching.splice(0).forEach((observer) => observer.disconnect());
        clear(tiles,
          tile("CPU", percent(now.cpu), `of ${now.cores} cores`, now.cpu, series((p) => p.cpu)),
          tile("Memory", percent(memory.used), `${size(memory.total - memory.available)} of ${size(memory.total)} used`,
            memory.used, series((p) => p.memory)),
          swap.total
            ? tile("Swap", percent(swap.used), `of ${size(swap.total)}`, swap.used, series((p) => p.swap)) : null,
          now.disks.map((d) => tile(`Disk ${d.path}`, percent(d.used),
            `${size(d.free)} free · ${[d.label, ...d.also].join(", ")}`, d.used, series((p) => p.disks?.[d.path]))));
      }
      clear(rows, now.containers.length ? now.containers.map((c) => h("tr",
        h("td", h("span.sv-kind", c.kind), " ",
          c.chat ? h("a",
            { href: "#", onclick: (e) => { e.preventDefault(); relay.navigate(`/c/${c.chat}`); } }, c.what)
            : h("span", c.what),
          c.what !== c.name ? h("span.sv-name", c.name) : null),
        h("td.num", c.cpu === null ? "—" : `${(c.cpu * 100).toFixed(1)}%`),
        h("td.num", size(c.memory))))
        : h("tr", h("td.muted", { colspan: "3" }, "No containers are running.")));
    }
    async function remember() {
      try {
        past = await relay.api.get("history");
      } catch {
        past = [];
      }
    }
    await remember();
    await draw();
    const often = setInterval(draw, 10 * 1000);
    const minutely = setInterval(remember, 60 * 1000);
    return () => {
      clearInterval(often);
      clearInterval(minutely);
      watching.splice(0).forEach((observer) => observer.disconnect());
    };
  });
}
