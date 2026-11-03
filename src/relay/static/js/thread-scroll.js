// Follow new output until the user reads back; preserve their anchor as content grows.

import { h, icon } from "./ui.js";

// What a reader's place is kept by, as the thread grows above or below it
const ANCHORS = ".bubble, .md > *, .tool-head, .activity-head, "
  + ".question-title, .run-error, .reasoning > summary, .relay-note "
  + "> summary, .switch-note > summary, .relay-note-details > *, .recovery-notice, .recovery-details > summary";

export class ThreadScroll {
  constructor(scroller, thread) {
    this.scroller = scroller;
    this.thread = thread;
    this.following = true;
    this.unread = false;
    this.appliedTop = null;
    this.label = h("span", "Latest");
    this.button = h("button.btn.small.latest-btn", { type: "button", hidden: true,
      title: "Go to the latest message and follow new output", onclick: () => this.jump() },
      this.label, icon("chevronDown", "sm"));
    this.onScroll = () => {
      // A delayed event from our own scroll may arrive after the next streamed layout change.
      if (this.appliedTop === this.scroller.scrollTop) {
        this.appliedTop = null;
        this.refresh();
        return;
      }
      this.appliedTop = null;
      this.following = this.atBottom();
      if (this.following) { this.unread = false; this.anchor = null; }
      else this.rememberAnchor();
      this.refresh();
    };
    this.onExpand = (event) => {
      if (event.target.closest("summary, button.tool-head")) {
        this.following = false;
        this.rememberAnchor();
      }
    };
    scroller.addEventListener("scroll", this.onScroll);
    thread.addEventListener("click", this.onExpand, true);
    this.observer = new ResizeObserver(() => this.schedule());
    this.observer.observe(thread);
    this.observer.observe(scroller);
  }

  atBottom() {
    return this.scroller.scrollHeight - this.scroller.scrollTop - this.scroller.clientHeight < 72;
  }

  rememberAnchor() {
    const { top, bottom } = this.scroller.getBoundingClientRect();
    const el = this.anchorAt(top, bottom) || this.scan(top, bottom);
    this.anchor = el ? { el, offset: el.getBoundingClientRect().top - top } : null;
  }

  // The anchor at the top of the view, asked of the page at a few points: measuring every element of a long thread
  // at each scroll was most of the time its earlier messages took to show (minutes, with tens of thousands of tool
  // calls).
  anchorAt(top, bottom) {
    if (typeof document === "undefined" || !document.elementsFromPoint
      || !this.thread.getBoundingClientRect) return null;
    const { left, width } = this.thread.getBoundingClientRect();
    for (let y = top + 1; y < bottom; y += 16) {
      for (const x of [left + width / 2, left + 32]) {
        const el = document.elementsFromPoint(x, y).find((e) => this.thread.contains(e))?.closest(ANCHORS);
        if (el && this.thread.contains(el)) return el;
      }
    }
    return null;
  }

  scan(top, bottom) {
    const candidates = [...this.thread.querySelectorAll(ANCHORS)];
    return candidates.find((el) => {
      const rect = el.getBoundingClientRect();
      return rect.top <= top && rect.bottom > top && rect.height > 0;
    }) || candidates.find((el) => {
      const rect = el.getBoundingClientRect();
      return rect.top >= top && rect.top < bottom && rect.height > 0;
    });
  }

  changed() {
    if (this.destroyed) return;
    if (!this.following) this.unread = true;
    this.schedule();
  }

  schedule() {
    if (this.destroyed || this.frame) return;
    this.frame = requestAnimationFrame(() => {
      this.frame = null;
      if (this.destroyed) return;
      if (this.following) {
        this.scroller.scrollTop = this.scroller.scrollHeight;
        this.appliedTop = this.scroller.scrollTop;
      }
      else if (this.anchor?.el.isConnected) {
        const offset = this.anchor.el.getBoundingClientRect().top - this.scroller.getBoundingClientRect().top;
        this.scroller.scrollTop += offset - this.anchor.offset;
        this.appliedTop = this.scroller.scrollTop;
      }
      this.refresh();
    });
  }

  refresh() {
    this.button.hidden = this.atBottom();
    this.label.textContent = this.unread ? "New activity · Latest" : "Latest";
  }

  jump() {
    this.following = true;
    this.unread = false;
    this.anchor = null;
    this.schedule();
  }

  destroy() {
    this.destroyed = true;
    cancelAnimationFrame(this.frame);
    this.observer.disconnect();
    this.scroller.removeEventListener("scroll", this.onScroll);
    this.thread.removeEventListener("click", this.onExpand, true);
  }
}
