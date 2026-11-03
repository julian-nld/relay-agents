// Attachments: uploading files for a message, and showing them in the thread.

import { relayUrl } from "./base.js";
import { ApiError } from "./api.js";
import { h, icon, toast } from "./ui.js";

const MAX_FILES = 10;

export function formatSize(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

async function uploadFile(file) {
  let response;
  try {
    response = await fetch(relayUrl("/api/uploads"), {
      method: "POST",
      credentials: "same-origin",
      headers: {
        "X-Relay-Request": "1",
        "Content-Type": file.type || "application/octet-stream",
        "X-Filename": encodeURIComponent(file.name || "pasted-file"),
      },
      body: file,
    });
  } catch {
    throw new ApiError("Upload failed: Relay is unreachable", 0);
  }
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new ApiError(data.error || `Upload failed (${response.status})`, response.status);
  return data;
}

// The files waiting to be sent with the next message.
export class AttachmentTray {
  constructor({ onChange }) {
    this.items = [];
    this.onChange = onChange;
    this.el = h("div.attachment-tray.hidden");
  }

  add(files) {
    for (const file of files) {
      if (this.items.length >= MAX_FILES) {
        toast(`At most ${MAX_FILES} files per message`, "error");
        break;
      }
      const item = { file, name: file.name || "pasted image", size: file.size, uploading: true,
        preview: file.type?.startsWith("image/") ? URL.createObjectURL(file) : null };
      this.items.push(item);
      uploadFile(file).then((data) => {
        item.id = data.id;
        item.uploading = false;
        this.render();
      }).catch((error) => {
        toast(`${item.name}: ${error.message}`, "error");
        this.remove(item);
      });
    }
    this.render();
  }

  remove(item) {
    this.items = this.items.filter((i) => i !== item);
    if (item.preview) URL.revokeObjectURL(item.preview);
    this.render();
  }

  busy() {
    return this.items.some((i) => i.uploading);
  }

  ids() {
    return this.items.filter((i) => i.id).map((i) => i.id);
  }

  clear() {
    for (const item of this.items) if (item.preview) URL.revokeObjectURL(item.preview);
    this.items = [];
    this.render();
  }

  render() {
    this.el.replaceChildren(...this.items.map((item) => h("div.attachment", { title: item.name },
      item.preview ? h("img.thumb", { src: item.preview, alt: "" }) : h("span.file-icon", icon("file")),
      h("span.meta", h("span.name", item.name), h("span.size", item.uploading ? "Uploading…" : formatSize(item.size))),
      item.uploading ? h("div.spinner.sm")
        : h("button.icon-btn.sm.remove", { title: "Remove", onclick: () => this.remove(item) }, icon("x", "sm")))));
    this.el.classList.toggle("hidden", !this.items.length);
    this.onChange?.();
  }
}

// Attachments shown with a sent message.
export function attachmentList(attachments) {
  if (!attachments?.length) return null;
  return h("div.sent-attachments", attachments.map((a) => {
    const url = relayUrl(`/api/uploads/${a.id}`);
    if (a.mime?.startsWith("image/") && a.mime !== "image/svg+xml") {
      return h("a.sent-image", { href: url, target: "_blank", rel: "noopener", title: a.name },
        h("img", { src: url, alt: a.name, loading: "lazy" }));
    }
    return h("a.attachment.sent", { href: url, title: a.name },
      h("span.file-icon", icon("file")),
      h("span.meta", h("span.name", a.name), h("span.size", formatSize(a.size))));
  }));
}

// Lets `target` accept dropped and pasted files.
export function acceptFiles(target, dropZone, onFiles) {
  dropZone.addEventListener("dragover", (e) => {
    if (![...e.dataTransfer.types].includes("Files")) return;
    e.preventDefault();
    dropZone.classList.add("dragging");
  });
  dropZone.addEventListener("dragleave", (e) => {
    if (!dropZone.contains(e.relatedTarget)) dropZone.classList.remove("dragging");
  });
  dropZone.addEventListener("drop", (e) => {
    dropZone.classList.remove("dragging");
    if (!e.dataTransfer.files.length) return;
    e.preventDefault();
    onFiles([...e.dataTransfer.files]);
  });
  target.addEventListener("paste", (e) => {
    const files = [...(e.clipboardData?.files || [])];
    if (files.length) {
      e.preventDefault();
      onFiles(files);
    }
  });
}
