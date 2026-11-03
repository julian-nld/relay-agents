// Thin JSON client for Relay's API.
import { relayUrl } from "./base.js";

export class ApiError extends Error {
  constructor(message, status, data = {}) {
    super(message);
    this.status = status;
    this.data = data;  // the whole answer, for errors that say more (e.g. which commits)
  }
}

async function request(method, path, body) {
  // (X-Relay-Client: what Relay's pages start is theirs; a conversation made without it is marked external)
  const options = { method, headers: { "X-Relay-Request": "1", "X-Relay-Client": "web" }, credentials: "same-origin" };
  if (body !== undefined) {
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(body);
  }
  let response;
  try {
    response = await fetch(relayUrl(path), options);
  } catch {
    throw new ApiError("Can't reach Relay. Is the server running?", 0);
  }
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    if (response.status === 401 && !path.startsWith("/api/login")) {
      window.dispatchEvent(new CustomEvent("relay:signed-out"));
    }
    throw new ApiError(data.error || `Request failed (${response.status})`, response.status, data);
  }
  return data;
}

export const api = {
  get: (path) => request("GET", path),
  post: (path, body = {}) => request("POST", path, body),
  put: (path, body) => request("PUT", path, body),
  patch: (path, body) => request("PATCH", path, body),
  del: (path) => request("DELETE", path),
};
