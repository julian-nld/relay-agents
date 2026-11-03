"""Usage limits: each usage-limit window over time (Settings → Usage and the activity bar's ring tell only what's
left now), and a word when one runs low. Every 15 minutes it asks Relay what's left (relay_sdk.limits) and keeps
what each window said, for 14 days. Its page draws them (static/index.js); agents ask with usage_limits_now."""

import time

import relay_sdk
from relay.plugins import PluginError

EVERY = 900  # seconds between two looks
FRESH = 120  # seconds: an older look is taken anew when the page opens (Relay keeps an answer that long anyway)
KEEP_DAYS = 14
BACK = 0.5  # used: under this again, a window that ran low is back
DEFAULTS = {"threshold": 10, "notify": True}  # % left that's low; a notification when a window gets there
PACE = 3 * 3600  # seconds back a window's pace is taken from (since it last reset)
PACE_LEAST = 1200  # seconds of it, at least, for a pace to go by


def setup(plugin):
    store = plugin.store

    def settings():
        return {**DEFAULTS, **store.get("settings", {})}

    def sample():
        """What Relay says is left now, kept per window; the windows that just ran low or are back again. Relay's
        rows, as it said them."""
        rows = relay_sdk.limits(user=plugin.sdk)
        now = time.time()
        low = settings()["threshold"] / 100
        changed = []

        def keep(series):
            for row in rows:
                for window in row.get("windows") or []:
                    key = f"{row['provider_id']}|{window['label']}"
                    one = series.setdefault(key, {"points": [], "low": False})
                    one.update(provider_id=row["provider_id"], name=row["name"], also=row.get("also") or [],
                               plan=row.get("plan"), label=window["label"], left=window.get("left"))
                    at, used, resets = row.get("checked") or now, window.get("used") or 0, window.get("resets_at")
                    if resets and resets <= now and at < resets:  # (it reset since it was told: all of it is back)
                        at, used, resets = resets, 0, None
                    at = round(at)
                    if one["points"] and one["points"][-1][0] >= at:
                        continue  # (nothing new: a last run's word, told again)
                    one["points"].append([at, round(used, 4), resets])
                    if not one["low"] and 1 - used <= low:
                        one["low"] = True
                        changed.append(("low", one, used, resets))
                    elif one["low"] and used < BACK:
                        one["low"] = False
                        changed.append(("back", one, used, resets))
            cutoff = now - KEEP_DAYS * 86400
            for key in list(series):
                series[key]["points"] = [p for p in series[key]["points"] if p[0] >= cutoff]
                if not series[key]["points"]:
                    del series[key]
            return series

        store.update("series", keep, {})
        store.set("sampled", now)
        if settings()["notify"]:
            for what, one, used, resets in changed:
                left = f"{one['name']} · {one['label']}: {round((1 - used) * 100)}% left"
                when = f", resets {time.strftime('%a %H:%M', time.localtime(resets))}" if resets else ""
                relay_sdk.notify(left + when,
                                 title="A usage limit runs low" if what == "low" else "A usage limit is back",
                                 url="/p/usage_limits", user=plugin.sdk)
        return rows

    plugin.every(EVERY, sample)

    # Its page's API, at /api/plugins/usage_limits/…
    def history(call):
        try:
            days = max(1, min(KEEP_DAYS, int(call.query.get("days", 7))))
        except ValueError:
            raise PluginError("days is a number") from None
        problem = None
        if store.get("sampled", 0) < time.time() - FRESH:  # (the page shows now: a look first)
            try:
                sample()
            except relay_sdk.RelayError as exc:
                problem = str(exc)
        since = time.time() - days * 86400
        series = [{**one, "key": key, "points": [p for p in one["points"] if p[0] >= since] or one["points"][-1:]}
                  for key, one in store.get("series", {}).items()]
        return {"series": series, "days": days, "sampled": store.get("sampled"), "error": problem, **settings()}

    def low_now(call):
        return {"low": sum(1 for one in store.get("series", {}).values() if one["low"])}

    def change_settings(call):
        given = call.json or {}
        threshold, notify = given.get("threshold", settings()["threshold"]), given.get("notify", settings()["notify"])
        if not isinstance(threshold, int) or isinstance(threshold, bool) or not 1 <= threshold <= 50:
            raise PluginError("threshold is a whole number from 1 to 50 (% left)")
        if not isinstance(notify, bool):
            raise PluginError("notify is true or false")
        store.set("settings", {"threshold": threshold, "notify": notify})
        return settings()

    # For agents and the Governor: what's left now, when it resets, and where the pace leads
    def said(call):
        want = str(call.arguments.get("provider") or "").strip().lower()
        try:
            rows, problem = sample(), None
        except relay_sdk.RelayError as exc:
            rows, problem = [], str(exc)
        now, low = time.time(), settings()["threshold"]
        lines = []
        for one in store.get("series", {}).values():
            names = [one["provider_id"], one["name"], *one.get("also", [])]
            if want and not any(want in name.lower() for name in names):
                continue
            _, used, resets = one["points"][-1]
            left = f"{round((1 - used) * 100)}%"
            line = (f"- {one['name']} ({one['provider_id']}{', ' + one['plan'] if one.get('plan') else ''}) · "
                    f"{one['label']}: " + (f"{one['left']} left ({left})" if one.get("left") else f"{left} left")
                    + (" (LOW)" if 1 - used <= low / 100 else ""))
            if resets and resets > now:
                line += f", resets {_when(resets, now)}"
            out = _runs_out(one["points"], now)
            if out:
                line += f"; at the pace of the last hours it runs out {_when(out, now)}, before it resets"
            if one.get("also"):
                line += f". The same login as: {', '.join(one['also'])}"
            lines.append(line)
        lines += [f"- {row['name']} ({row['provider_id']}): {row['error']}"
                  for row in rows if row.get("error") and not row.get("windows")
                  and (not want or want in row["provider_id"].lower() or want in row["name"].lower())]
        if not lines:
            return (f"No usage limits known{' for ' + repr(want) if want else ''}: providers paid per use or free "
                    "have none." + (f" (Relay couldn't be asked: {problem})" if problem else ""))
        sampled = store.get("sampled")
        head = (f"Usage limits of the user's subscriptions, as of {time.strftime('%H:%M', time.localtime(sampled))} "
                f"(low is {low}% left or less; relay agents lists the providers)"
                if sampled else "Usage limits of the user's subscriptions")
        return "\n".join([head + (f". Relay couldn't be asked now ({problem}): these are the last known." if problem
                                  else ":"), *lines])

    plugin.tool("now", "What's left of the user's usage limits: their subscriptions' windows (Claude Code's 5-hour "
                "and weekly, ChatGPT's, Copilot's), per provider: % left, when each resets, and when one would run out "
                "at its recent pace. Look before long or costly work, or to pick a provider with room for a subagent.",
                {"type": "object", "properties": {"provider": {
                    "type": "string", "description": "only this provider's windows: its id (as relay agents lists "
                                                     "them) or name"}}},
                said, governor=True)

    plugin.route("history", history)
    plugin.route("low", low_now)
    plugin.route("settings", lambda call: settings())
    plugin.route("settings", change_settings, methods=["PUT"])


def _when(at, now):
    """A moment as a reader takes it: "Tue 21:30 (in 2 h 5 min)", a date when it's further than a week's days."""
    minutes = max(0, round((at - now) / 60))
    span = f"{minutes // 1440} d {minutes % 1440 // 60} h" if minutes >= 1440 else \
        f"{minutes // 60} h {minutes % 60} min" if minutes >= 60 else f"{minutes} min"
    return f"{time.strftime('%a %H:%M' if minutes < 6 * 1440 else '%d %b %H:%M', time.localtime(at))} (in {span})"


def _runs_out(points, now):
    """When a window is used up at the pace of its last hours (since it last reset), if that's before it resets."""
    at, used, resets = points[-1]
    start = len(points) - 1
    while start and points[start - 1][1] <= points[start][1] and points[start - 1][0] >= now - PACE:
        start -= 1
    since, before = points[start][0], points[start][1]
    if at - since < PACE_LEAST or used <= before:
        return None
    out = at + (1 - used) * (at - since) / (used - before)
    return out if out > now and (not resets or out < resets) else None
