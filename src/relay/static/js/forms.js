// Questions and other keyed forms, independent of the provider that asks them.

import { h } from "./ui.js";

let sequence = 0;

export class FormCard {
  constructor(reply) {
    this.reply = reply;
    this.el = h("div.question-card");
    this.event = {};
    this.group = `question-${++sequence}`;
    this.busy = false;
  }

  update(event, active) {
    const previous = this.event.status;
    this.event = { ...this.event, ...event };
    this.active = active;
    // A duplicate pending event must not erase the answers being entered.
    if (previous !== "pending" || this.event.status !== "pending" || !this.controls) this.render();
    this.refresh();
  }

  render() {
    const e = this.event;
    this.controls = (e.fields || []).map((field, index) => this.field(field, index));
    this.error = h("div.error-text", { role: "alert" });
    this.status = h("div.faint");
    this.buttons = h("div.question-actions",
      h("button.btn.small", { type: "button", onclick: () => this.submit(null) }, "Dismiss"),
      h("button.btn.small.primary", { type: "submit" }, "Send answers"));
    this.form = h("form", { oninput: () => this.refresh(), onsubmit: (event) => {
      event.preventDefault();
      const answer = this.answers();
      for (const c of this.controls.filter((c) => !c.el.hidden)) {
        const value = answer[c.field.key];
        const count = Array.isArray(value) ? value.length : value === "" || value === undefined ? 0 : 1;
        if ((c.field.required && !count) || count < (c.field.minItems || 0)
            || (c.field.maxItems !== undefined && count > c.field.maxItems)) {
          this.error.textContent = `Check your answer to “${c.field.title || c.field.key}”.`;
          return;
        }
      }
      this.submit(answer);
    } }, ...this.controls.map((c) => c.el), this.error, this.status, this.buttons);
    this.el.replaceChildren(h("div.question-title",
      e.subagent ? `${e.subagent}: ${e.title}` : e.title || "Questions"), this.form);
  }

  field(field, index) {
    const value = this.event.answer?.[field.key] ?? field.default;
    const el = h("fieldset.question-field", h("legend", field.title || field.key),
      field.description ? h("div.faint", field.description) : null);
    let read;
    if (field.options) {
      const multiple = field.type === "multiselect" && field.maxItems !== 1;
      const selected = Array.isArray(value) ? value : value === undefined ? [] : [value];
      const choices = field.options.map((option) => {
        const input = h("input", { type: multiple ? "checkbox" : "radio", name: `${this.group}-${index}`,
          value: option.value, checked: selected.includes(option.value) });
        el.append(h("label.question-choice", input, h("span", h("span", option.label),
          option.description ? h("small.faint", option.description) : null)));
        return input;
      });
      let custom, customChoice;
      if (field.custom) {
        const other = selected.filter((v) => !field.options.some((o) => o.value === v));
        custom = h("input.input", { placeholder: "Type your own answer", value: other.join(", "),
          "aria-label": `${field.title || field.key}: your own answer` });
        customChoice = h("input", { type: multiple ? "checkbox" : "radio", name: `${this.group}-${index}`,
          checked: other.length > 0 });
        custom.addEventListener("input", () => { customChoice.checked = !!custom.value.trim(); });
        el.append(h("label.question-choice", customChoice, h("span", "Your own answer", custom)));
      }
      read = () => {
        const picked = choices.filter((c) => c.checked).map((c) => c.value);
        if (customChoice?.checked && custom.value.trim()) picked.push(custom.value.trim());
        return field.type === "multiselect" ? picked : picked[0] ?? "";
      };
    } else if (field.type === "external") {
      el.append(h("a", { href: field.url, target: "_blank", rel: "noopener" }, "Open form"));
      read = () => undefined;
    } else {
      const numeric = ["number", "integer"].includes(field.type);
      const type = field.type === "boolean" ? "checkbox" : numeric ? "number"
        : { email: "email", uri: "url", date: "date", "date-time": "datetime-local" }[field.format] || "text";
      const input = h("input.input", { type, value: field.type === "boolean" ? undefined : value ?? "",
        checked: field.type === "boolean" && value === true, placeholder: field.placeholder,
        min: field.minimum, max: field.maximum, step: field.type === "integer" ? 1 : numeric ? "any" : undefined,
        minLength: field.minLength, maxLength: field.maxLength, pattern: field.pattern,
        required: field.type !== "boolean" && field.required, "aria-label": field.title || field.key });
      el.append(input);
      read = () => field.type === "boolean" ? input.checked
        : numeric ? (input.value === "" ? undefined : Number(input.value)) : input.value;
    }
    return { field, el, read };
  }

  answers() {
    return Object.fromEntries(this.controls.filter((c) => !c.el.hidden)
      .map((c) => [c.field.key, c.read()]).filter(([, value]) => value !== undefined));
  }

  refresh() {
    clearTimeout(this.countdown);
    const values = Object.fromEntries(this.controls.map((c) => [c.field.key, c.read()]));
    const pending = this.event.status === "pending";
    this.el.classList.toggle("pending", pending && this.active);
    for (const c of this.controls) {
      c.el.hidden = !!c.field.hidden || (c.field.when || []).some((condition) =>
        condition.op === "eq" ? values[condition.key] !== condition.value : values[condition.key] === condition.value);
      c.el.disabled = c.el.hidden || !pending || !this.active || this.busy;
    }
    this.buttons.hidden = !pending || !this.active;
    this.buttons.querySelectorAll("button").forEach((b) => { b.disabled = this.busy; });
    this.status.textContent = pending ? (this.active ? "Waiting for your answers" : "Not answered")
      : this.event.status === "answered" ? "Answers sent" : this.event.message || "Question dismissed";
    if (pending && this.active && this.event.expires) {
      const seconds = Math.max(0, Math.ceil(this.event.expires - Date.now() / 1000));
      this.status.textContent = seconds
        ? `Waiting for your answers · dismiss in ${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`
        : "Dismissing unanswered question…";
      if (this.el.isConnected) this.countdown = setTimeout(() => this.refresh(), 1000);
    }
  }

  async submit(answer) {
    if (this.busy || !this.active || this.event.status !== "pending") return;
    this.busy = true;
    this.error.textContent = "";
    this.refresh();
    try {
      await this.reply(this.event, answer);
      this.update({ status: answer === null ? "cancelled" : "answered", answer }, this.active);
    } catch (error) {
      this.error.textContent = error.message;
    } finally {
      this.busy = false;
      this.refresh();
    }
  }
}
