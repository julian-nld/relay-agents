// Restart Relay (POST /api/restart: refused while agents work) and reload once it answers again.
import { api } from "./api.js";
import { toast } from "./ui.js";

export async function restartRelay() {
  try {
    await api.post("/api/restart");
  } catch (error) {
    toast(error.message, "error");
    return;
  }
  toast("Restarting Relay…");
  await new Promise((resolve) => setTimeout(resolve, 1500));
  for (let i = 0; i < 60; i++) {
    try {
      await api.get("/api/state");
      location.reload();
      return;
    } catch {
      await new Promise((resolve) => setTimeout(resolve, 1000));
    }
  }
  toast("Relay didn't come back; check the machine it runs on", "error");
}
