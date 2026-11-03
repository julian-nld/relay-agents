// Usage limits' part of Relay's page: a button with how many windows run low, a page with each window's history,
// and a tab in Settings (docs/WRITING-A-PLUGIN.md).

const SVG = "http://www.w3.org/2000/svg";
const RANGES = [[1, "1 day"], [7, "7 days"], [14, "14 days"]];
const HEIGHT = 128, LEFT = 40, RIGHT = 10, TOP = 10, BOTTOM = 22;  // the chart's frame, in px

function svg(tag, attrs = {}) {
  const el = document.createElementNS(SVG, tag);
  for (const [key, value] of Object.entries(attrs)) el.setAttribute(key, value);
  return el;
}

// What's left of a window at its latest point, 0–1 (one that reset since then has all of it again)
function leftNow(series, now = Date.now() / 1000) {
  const [, used, resets] = series.points.at(-1);
  return resets && resets <= now ? 1 : Math.max(0, Math.min(1, 1 - used));
}

// The times the x-axis names, by how long it is: every hour up to 6 hours, every 6 up to a day and a half, then every
// day, or every other one beyond a week
function step(from, to) {
  const hours = (to - from) / 3600;
  return hours <= 6 ? 1 : hours <= 36 ? 6 : hours <= 8 * 24 ? 24 : 48;
}

function ticks(from, to) {
  const hours = step(from, to);
  const first = new Date(from * 1000);
  first.setMinutes(0, 0, 0);
  first.setHours(hours >= 24 ? 24 : Math.ceil((first.getHours() + 1) / hours) * hours);  // (the next midnight, or hour)
  const found = [];
  for (let t = first.getTime() / 1000; t <= to; t += hours * 3600) found.push(t);
  return found;
}

function tickLabel(t, hours) {
  const when = new Date(t * 1000);
  return hours < 24 ? when.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })
    : hours === 24 ? when.toLocaleDateString([], { weekday: "short" })
      : when.toLocaleDateString([], { day: "numeric", month: "short" });
}

export default function setup(relay) {
  const { h, clear, toast, timeAgo, clock } = relay.ui;
  const button = relay.barItem({ title: "Usage limits", icon: "clock", onClick: () => relay.navigate("") });

  async function low() {
    const { low: count } = await relay.api.get("low");
    button.setBadge(count);
  }
  low().catch(() => {});
  setInterval(() => low().catch(() => {}), 5 * 60 * 1000);

  const when = (t) => (Date.now() / 1000 - t < 6 * 86400 ? clock(t)
    : new Date(t * 1000).toLocaleString([], { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }));

  function nowText(series) {
    const left = leftNow(series);
    const [, , resets] = series.points.at(-1);
    return [series.left && left < 1 ? `${series.left} left` : `${Math.round(left * 100)}% left`,
      resets && resets > Date.now() / 1000 ? `resets ${when(resets)}` : null].filter(Boolean).join(" · ");
  }

  const watching = [];  // (the charts' ResizeObservers: let go when they're drawn anew)

  // One window's % left over the range: a 2px line with a faint wash, the threshold's hairline, and a crosshair
  // with the value at the pointer (or the arrow keys)
  function chart(series, { days, threshold, since }) {
    const box = h("div.ul-chart");
    const tip = h("div.ul-tip", { hidden: true });
    const to = Date.now() / 1000, from = since;
    const points = series.points.filter(([t]) => t >= from);
    if (!points.length) points.push(series.points.at(-1));
    const lowest = points.reduce((a, p) => (p[1] > a[1] ? p : a));
    const graphic = svg("svg", { role: "img", tabindex: "0", height: HEIGHT,
      "aria-label": `${series.label}: ${nowText(series)} now; least left in ${days} day${days > 1 ? "s" : ""}: `
        + `${Math.round((1 - lowest[1]) * 100)}% (${when(lowest[0])}). Arrow keys go through the values.` });
    box.append(graphic, tip);
    let index = points.length - 1, show = null;

    function render() {
      const width = box.clientWidth;
      if (!width) return;
      graphic.setAttribute("width", width);
      graphic.setAttribute("viewBox", `0 0 ${width} ${HEIGHT}`);
      graphic.replaceChildren();
      const x = (t) => LEFT + (Math.max(from, t) - from) / (to - from) * (width - LEFT - RIGHT);
      const y = (left) => TOP + (1 - left) * (HEIGHT - TOP - BOTTOM);
      const grid = svg("g", { class: "ul-grid" });
      for (const level of [0, 0.5, 1]) {
        grid.append(svg("line", { x1: LEFT, x2: width - RIGHT, y1: y(level), y2: y(level) }));
        const label = svg("text", { x: LEFT - 6, y: y(level) + 4, "text-anchor": "end" });
        label.textContent = `${level * 100}%`;
        grid.append(label);
      }
      for (const t of ticks(from, to)) {
        const label = svg("text", { x: x(t), y: HEIGHT - 6, "text-anchor": "middle" });
        label.textContent = tickLabel(t, step(from, to));
        if (x(t) > LEFT + 12 && x(t) < width - RIGHT - 12) grid.append(label);
      }
      const limit =
        svg("line", { class: "ul-low", x1: LEFT, x2: width - RIGHT, y1: y(threshold / 100), y2: y(threshold / 100) });
      const lowLabel =
        svg("text", { class: "ul-low-label", x: width - RIGHT, y: y(threshold / 100) - 4, "text-anchor": "end" });
      lowLabel.textContent = `low (${threshold}%)`;
      // (the line runs on to now: what was last told holds until the next look)
      const shown = [...points.map(([t, used]) => [x(t), y(1 - used)]), [x(to), y(leftNow(series, to))]];
      const line = shown.map(([px, py], i) => `${i ? "L" : "M"}${px.toFixed(1)} ${py.toFixed(1)}`).join(" ");
      const area = `${line} L${shown.at(-1)[0].toFixed(1)} ${y(0)} L${shown[0][0].toFixed(1)} ${y(0)} Z`;
      const cross = svg("line", { class: "ul-cross", y1: TOP, y2: HEIGHT - BOTTOM, visibility: "hidden" });
      const dot = svg("circle", { class: "ul-dot", r: 4, visibility: "hidden" });
      const hit =
        svg("rect", { class: "ul-hit", x: LEFT, y: 0, width: Math.max(0, width - LEFT - RIGHT), height: HEIGHT });
      graphic.append(grid, svg("path", { class: "ul-area", d: area }), limit,
        svg("path", { class: "ul-line", d: line }),
        lowLabel, cross, dot, hit);
      show = (i) => {
        index = Math.max(0, Math.min(points.length - 1, i));
        const [t, used] = points[index];
        const px = x(t), py = y(1 - used);
        for (const el of [cross, dot]) el.setAttribute("visibility", "visible");
        cross.setAttribute("x1", px);
        cross.setAttribute("x2", px);
        dot.setAttribute("cx", px);
        dot.setAttribute("cy", py);
        clear(tip, h("strong", `${Math.round((1 - used) * 100)}% left`), h("span", when(t)));
        tip.hidden = false;
        tip.style.left = `${Math.min(Math.max(px - 60, 0), width - 130)}px`;  // (CSSOM: allowed by the CSP)
      };
      hit.addEventListener("pointermove", (e) => {
        const t = from
          + (e.clientX - graphic.getBoundingClientRect().left - LEFT) / (width - LEFT - RIGHT) * (to - from);
        let nearest = 0;
        points.forEach(([pt], i) => { if (Math.abs(pt - t) < Math.abs(points[nearest][0] - t)) nearest = i; });
        show(nearest);
      });
      hit.addEventListener("pointerleave", () => hide());
    }
    function hide() {
      if (document.activeElement === graphic) return;
      tip.hidden = true;
      graphic.querySelectorAll(".ul-cross, .ul-dot").forEach((el) => el.setAttribute("visibility", "hidden"));
    }
    graphic.addEventListener("focus", () => show?.(index));
    graphic.addEventListener("blur", () => { tip.hidden = true; hide(); });
    graphic.addEventListener("keydown", (e) => {
      const step = { ArrowLeft: -1, ArrowRight: 1, Home: -points.length, End: points.length }[e.key];
      if (step === undefined) return;
      e.preventDefault();
      show?.(index + step);
    });
    const observer = new ResizeObserver(() => render());
    observer.observe(box);
    watching.push(observer);
    return box;
  }

  // The same values as a table, at most 48 rows of them (the latest always)
  function table(series, { days }) {
    const since = Date.now() / 1000 - days * 86400;
    const points = series.points.filter(([t]) => t >= since);
    const every = Math.max(1, Math.ceil(points.length / 48));
    const rows = points.filter((_, i) => (points.length - 1 - i) % every === 0);
    return h("table.ul-table", h("thead", h("tr", h("th", "When"), h("th", "Left"))),
      h("tbody",
        rows.reverse().map(([t, used]) => h("tr", h("td", when(t)), h("td", `${Math.round((1 - used) * 100)}%`)))));
  }

  // Its page, /p/usage_limits: per login a card, per window its chart
  relay.page("", async (el) => {
    let days = 7, asTable = false, data = null;
    const body = h("div.ul-body");
    const ranges =
      RANGES.map(([value, label]) => h("button.btn.small", { type: "button", "aria-pressed": String(value === days),
        onclick: () => { days = value; load(); } }, label));
    const tableButton = h("button.btn.small.ghost", { type: "button", "aria-pressed": "false",
      onclick: () => {
        asTable = !asTable;
        tableButton.setAttribute("aria-pressed", String(asTable));
        draw();
      } }, "Table");
    const status = h("span.muted.ul-status");
    clear(el, h("div.ul-page", h("h1", "Usage limits"),
      h("p.lead", "What's left of each usage window over time. Relay looks every 15 minutes; the ring on the bar "
        + "shows the tightest now."),
      h("div.ul-controls",
        h("div.ul-ranges", { role: "group", "aria-label": "Time range" }, ranges), tableButton, status),
      body));

    function draw() {
      ranges.forEach((b, i) => b.setAttribute("aria-pressed", String(RANGES[i][0] === days)));
      if (!data) return;
      watching.splice(0).forEach((observer) => observer.disconnect());
      status.textContent = data.sampled ? `looked ${timeAgo(data.sampled)}` : "";
      if (!data.series.length) {
        clear(body, data.error ? h("p.error-text", data.error)
          : h("p.muted",
              "No provider tells its usage limits yet. Claude Code, ChatGPT and Copilot do once they're signed in."));
        return;
      }
      // (one time axis for all: the range, or from the first look when there's less history than that, an hour at
      // least)
      const now = Date.now() / 1000;
      const first = Math.min(...data.series.map((series) => series.points[0][0]));
      const since = Math.max(now - days * 86400, Math.min(first, now - 3600));
      const logins = new Map();
      for (const series of data.series) {
        if (!logins.has(series.provider_id)) logins.set(series.provider_id, []);
        logins.get(series.provider_id).push(series);
      }
      clear(body, data.error ? h("p.error-text", `Couldn't look now: ${data.error}`) : null,
        [...logins.values()].map((windows) => {
          const first = windows[0];
          return h("section.ul-card",
            h("div.ul-head", h("strong", first.name), first.plan ? h("span.badge", first.plan) : null,
              first.also.length ? h("span.muted", `also ${first.also.join(", ")}`) : null),
            windows.map((series) => h("div.ul-window",
              h("div.ul-window-head", h("span", series.label),
                leftNow(series) * 100 <= data.threshold ? h("span.badge.danger", "low") : null,
                h("span.muted", nowText(series))),
              asTable ? table(series, { days }) : chart(series, { days, threshold: data.threshold, since }))));
        }));
    }
    async function load() {
      body.classList.add("ul-loading");  // (the charts stay while it asks)
      try {
        data = await relay.api.get(`history?days=${days}`);
      } catch (error) {
        clear(body, h("p.error-text", error.message));
        return;
      } finally {
        body.classList.remove("ul-loading");
      }
      draw();
      low().catch(() => {});
    }
    await load();
    const timer = setInterval(load, 5 * 60 * 1000);
    return () => {
      clearInterval(timer);
      watching.splice(0).forEach((observer) => observer.disconnect());
    };
  });

  // A tab in Settings: when a window is low, and whether that's said
  relay.settingsTab({ label: "Usage limits", icon: "clock", render: async (pane) => {
    const now = await relay.api.get("settings");
    const threshold =
      h("input.input", { type: "number", min: 1, max: 50, value: now.threshold, "aria-label": "Low at" });
    const notify = h("input", { type: "checkbox", checked: now.notify });
    clear(pane, h("h3", "Usage limits"),
      h("p.lead", "A window is low when this much of it is left; Relay looks every 15 minutes and keeps 14 days."),
      h("label.ul-field", h("span", "Low at (% left)"), threshold),
      h("label.ul-check", notify, h("span", "A notification when a window runs low, and when it's back")),
      h("button.btn.primary.small", { onclick: async () => {
        try {
          await relay.api.put("settings", { threshold: Number(threshold.value), notify: notify.checked });
          toast("Saved");
        } catch (error) {
          toast(error.message, "error");
        }
      } }, "Save"));
  } });
}
