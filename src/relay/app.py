"""The Relay web application (Starlette)."""

from __future__ import annotations

import asyncio
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response
from starlette.routing import Mount, Route, WebSocketRoute
from starlette.staticfiles import StaticFiles

from relay import config, forks, relocation, skills, tunables, webdav
from relay.agentos import AgentOS
from relay.auth import Accounts, LoginThrottle
from relay.boxes import Boxes
from relay.computers import Computers
from relay.containers import ContainerError, Containers
from relay.creations import Creations
from relay.db import Database
from relay.delegation import Delegation
from relay.fetch import Fetcher
from relay.forward import Forwarding
from relay.notify import Notifier
from relay.plugins import Plugins
from relay.production import Production
from relay.providers import Registry
from relay.providers.base import Provider
from relay.repair import Repairs
from relay.routes.accounts import (
    add_app_password,
    app_passwords,
    change_password,
    index,
    login,
    logout,
    remove_app_password,
    set_new_chat,
    setup,
    state,
)
from relay.routes.conversations import (
    clear_conversation,
    commit_task,
    continue_now,
    conversation_commands,
    create_conversation,
    delete_conversation,
    delete_share,
    discard_task,
    export_conversation,
    fork_conversation,
    get_conversation,
    list_conversations,
    list_shares,
    list_tasks,
    mark_reviewed,
    merge_task,
    move_to_code,
    queue_message,
    resume_later,
    retry_message,
    rewind_files,
    send_message,
    send_queued_now,
    set_timed,
    share_conversation,
    subagent_copy,
    task_changes,
    unqueue_message,
    update_conversation,
)
from relay.routes.extras import (
    add_computer,
    add_mode,
    add_schedule,
    add_service,
    add_skill,
    computer_poll,
    computer_result,
    computer_script,
    delete_computer,
    delete_mode,
    delete_schedule,
    delete_service,
    delete_skill,
    demote_service,
    get_agentos,
    get_fallback_loops,
    get_schedule,
    get_service,
    get_skill,
    get_subagent_policy,
    guess_service,
    list_computers,
    list_modes,
    list_schedules,
    list_services,
    list_skills,
    promote_service,
    put_agentos,
    put_fallback_loops,
    put_subagent_policy,
    reset_schedule,
    resolve_intervention,
    restart_service,
    restore_service,
    rollback_service,
    run_schedule,
    sdk_page,
    sdk_wheel,
    service_logs,
    service_status,
    set_mode_icon,
    skill_file,
    start_service,
    stop_service,
    update_computer,
    update_mode,
    update_schedule,
    update_service,
    update_workbox,
    wake_agentos,
)
from relay.routes.memory import add_memory, change_memory, clear_memory, forget_memory, get_memory, put_memory
from relay.routes.plugins import add_plugin, list_plugins, plugin_route, plugin_static, update_plugin
from relay.routes.providers import (
    add_provider,
    check_conformance,
    check_provider,
    creation_action,
    creations,
    delete_provider,
    forget_ranking,
    get_creation,
    get_repair,
    list_providers,
    model_ranking,
    model_statuses,
    new_creation,
    new_repair,
    provider_efforts,
    provider_guide,
    provider_kinds,
    provider_models,
    provider_trouble,
    start_draft,
    test_draft,
    test_model,
    update_provider,
)
from relay.routes.runs import (
    answer_form,
    answer_permission,
    get_upload,
    mcp_endpoint,
    run_events,
    stop_run,
    tools_for,
    upload,
)
from relay.routes.scripts import (
    copy_script,
    create_script,
    delete_script,
    get_script,
    get_script_run,
    list_scripts,
    move_script,
    rename_script,
    run_script,
    save_script,
    script_docs,
    script_output,
    script_templates,
    set_scripts_folder,
    share_scripts,
    stop_script_run,
    write_script,
)
from relay.routes.search import get_search, put_search, try_search
from relay.routes.settings import (
    browse_workspaces,
    create_workspace,
    get_limits,
    get_settings,
    put_limits,
    put_settings,
    restart,
    send_notification,
    test_notifications,
    usage_limits,
    usage_overview,
)
from relay.routes.system import get_storage, get_system, remove_box, remove_image, remove_stopped, stop_box
from relay.routes.terminal import (
    creation_terminal_socket,
    provider_draft_terminal_socket,
    provider_terminal_socket,
    service_terminal_socket,
    terminal_socket,
)
from relay.routes.vpn import (
    add_profile,
    add_tunnel,
    delete_profile,
    delete_tunnel,
    get_vpn,
    reconnect_tunnel,
    tunnel_log,
    update_profile,
    update_tunnel,
    vpn_servers,
)
from relay.runs import RunManager
from relay.scheduler import Scheduler
from relay.schedules import Schedules
from relay.scripts import Scripts
from relay.search import Search
from relay.terminal import Shells
from relay.uploads import Uploads
from relay.vpn import Vpn
from relay.web import (
    COOKIE,
    CSP,
    CSRF_HEADER,
    DAV_CSP,
    PUBLIC_API,
    api,
    app_credentials,
    error_page,
    local_url,
    proxy_sign_in,
)


class Relay:
    """Everything a request handler needs."""

    setting_up = False  # (while its start checks the containers and builds what's missing: the page says so)

    def __init__(self, settings: config.Settings, db: Database, registry: Registry) -> None:
        self.settings = settings
        tunables.use(settings.limits)  # Settings → Limits
        self.db = db
        self.registry = registry
        self.accounts = Accounts(db)
        self.uploads = Uploads(db)
        self.notifier = Notifier(settings)
        self.containers = Containers(settings.container_image or None)
        # Some folders' WorkBoxes behind a VPN (relay.vpn: gluetun, which Relay keeps)
        self.vpn = Vpn(settings)
        self.vpn.notifier = self.notifier  # (its problems, and that it's connected again)
        self.containers.network = self.vpn.network
        self.containers.via_vpn = self.vpn.covers
        self.containers.vpn_url = self.vpn.relay_url()
        self.terminals = set()
        self.shells = Shells(self.terminals)  # chats' shells, kept while their pages come and go
        self.limits_seen: dict[str, tuple[float, dict | None]] = {}  # provider -> (when, its limits()), a while
        # Providers' programs run in boxes of their own, their commands in the WorkBoxes.
        self.boxes = Boxes(self.containers, settings) if self.containers is not None else None
        Provider.boxes = self.boxes
        self.runs = RunManager(db, registry, self.notifier, self.containers, self.boxes)
        self.vpn.busy = self.runs.busy_folders  # (a tunnel doesn't rotate under an agent's work)
        # Relay's own web search for every agent (relay.search: SearXNG, on this machine's connection or a tunnel's)
        self.search = Search(settings, self.vpn)
        self.runs.search = self.search
        # …and its own web fetch (relay.fetch: in a browser when it must), on the same route
        self.fetch = Fetcher(settings, self.vpn, self.containers)
        self.runs.fetch = self.fetch
        self.throttle = LoginThrottle()
        # Subagents: agents reach Relay's MCP tools on this machine.
        self.computers = Computers(db)
        self.delegation = Delegation(self)
        self.runs.delegation = self.delegation
        self.runs.local_url = local_url(settings)
        self.schedules = Schedules(db)
        self.agentos = AgentOS(self)
        self.production = Production(self)
        self.repairs = Repairs(db, registry)
        self.repairs.boxes = self.boxes  # (its tests run in a box of theirs)
        self.creations = Creations(self)
        self.scripts = Scripts(self)  # the user's own SDK scripts (the Scripts page)
        self.plugins = Plugins(self)  # what's new since the core was wrapped up (RELAY_HOME/plugins), loaded at start
        self.scheduler = Scheduler(self)

    def prepare(self) -> None:
        # Its data or code moved to another folder: fix the stored paths.
        notes = relocation.relocate(self.db)
        for note in notes:
            print(f"relay: {note}", flush=True)
        if notes:  # it rewrote settings.json (workspace roots, the chat folder): use those from now
            moved = config.load_settings()
            for name in ("workspace_roots", "chat_root"):
                setattr(self.settings, name, getattr(moved, name))
        relocation.remove_old_tools_scripts()
        # Subagents' copies of folders: those with changes still to hand over stay for the user.
        forks.tidy(lambda subagent: self.db.one("SELECT 1 FROM conversations WHERE id = ?", (subagent,)) is not None,
                   self.containers)
        # Leftovers in the machine's cache from conversations that are gone.
        pruned = self.delegation.prune_scripts() + skills.prune_claude_folders()
        if pruned:
            print(f"relay: removed {pruned} unused tools scripts and skill folders from the cache", flush=True)
        self.registry.ensure_defaults()
        self.plugins.folder.mkdir(parents=True, exist_ok=True)  # (where the user puts them: Settings → Plugins)
        self.plugins.load(self.settings.plugins_off)
        left = self.db.fail_unfinished_runs()
        # AgentOS tasks whose wake-up a stop broke off; what the Governor followed, as it was kept
        self.agentos.reconcile(time.time(), {run["id"] for run in left})
        for user in self.agentos.enabled_users():  # Relay's own tasks (system tasks), those not there yet
            self.schedules.ensure_system(user)
        if left:
            # An agent that isn't Relay's own child process (opencode's server) may still be
            # working on them, and using a usage limit: stop it.
            threading.Thread(target=self._abandon, args=(left,), daemon=True).start()
        self.scripts.reconcile()  # (runs a stop left going)
        self.scripts.show()  # (the boxes: the private scripts out of sight, the public ones in view)
        self.accounts.purge_expired()
        self.uploads.purge_orphans()
        self.containers.forget_routes()
        self.containers.set_url(self.runs.local_url)  # for `relay` in the containers (container/bin/relay)
        # Agents work in containers: whether podman works, with the image pulled, before the first answer.
        self.setting_up = True
        threading.Thread(target=self._check_containers, daemon=True).start()

    def _check_containers(self) -> None:
        try:
            self.containers.check()
            if self.boxes is not None:
                self.boxes.check()
                # No run starts meanwhile (start() takes this lock too); a box in use stays.
                with self.runs.start_lock:
                    stale = [] if self.runs.active else self.boxes.drop_stale()
                for name in stale:
                    print(f"relay: {name} saw a removed folder of Relay's; it's made anew", flush=True)
        except ContainerError as exc:
            print(f"relay: {exc}", flush=True)
        finally:
            self.setting_up = False

    def _abandon(self, runs: list[dict]) -> None:
        for run in runs:
            if not run["session"]:
                continue
            try:
                self.registry.provider(run["provider_id"]).abandon(run["session"], Path(run["directory"]))
            except Exception as exc:  # noqa: BLE001 - removed meanwhile, or its server is gone: nothing to stop
                print(f"relay: couldn't stop {run['provider_id']} session {run['session']}: {exc}", flush=True)

    @property
    def login_required(self) -> bool:
        return self.settings.auth == "password"

    def needs_setup(self) -> bool:
        return self.login_required and not self.accounts.has_users()


def create_app(settings: config.Settings | None = None, db: Database | None = None,
               registry: Registry | None = None) -> Starlette:
    settings = settings or config.load_settings()
    db = db or Database(config.database_path())
    registry = registry or Registry(config.providers_dir())
    relay = Relay(settings, db, registry)

    @asynccontextmanager
    async def lifespan(app):
        relay.prepare()
        await relay.production.start()  # AgentOS's live services: their proxies, their boxes running
        ticking = asyncio.create_task(relay.scheduler.loop())
        yield
        # (the scheduler, its VPN keeping and services' daily snapshots, cancelled and waited for: they start
        # nothing new while Relay stops. A step already in a thread, a VPN check say, finishes by itself.)
        ending = [task for task in (ticking, relay.scheduler.vpn_keeping, relay.scheduler.snapshotting)
                  if task is not None]
        for task in ending:
            task.cancel()
        await asyncio.gather(*ending, return_exceptions=True)
        relay.production.stop()
        relay.creations.stop()
        await relay.shells.close_all()
        await asyncio.to_thread(relay.runs.shutdown)

    routes = [
        Route("/", index),
        # WebDAV clients (Windows) ask the server's root what it speaks
        Route("/", webdav.root, methods=["OPTIONS", "PROPFIND"]),
        Route("/dav", webdav.root, methods=["OPTIONS", "PROPFIND"]),
        Route("/dav/", webdav.root, methods=["OPTIONS", "PROPFIND"]),
        Route("/dav/{token}", webdav.handle, methods=webdav.METHODS),
        Route("/dav/{token}/{path:path}", webdav.handle, methods=webdav.METHODS),
        Route("/c/{conversation_id}", index),
        WebSocketRoute("/api/conversations/{conversation_id}/terminal", terminal_socket),
        WebSocketRoute("/api/services/{service_id}/terminal", service_terminal_socket),
        WebSocketRoute("/api/provider-creations/{creation_id}/terminal", creation_terminal_socket),
        WebSocketRoute("/api/provider-repairs/{creation_id}/terminal", creation_terminal_socket),
        WebSocketRoute("/api/providers/{provider_id}/terminal", provider_terminal_socket),
        WebSocketRoute("/api/provider-kinds/{kind}/terminal", provider_draft_terminal_socket),
        Route("/tasks", index),
        Route("/scheduled", index),
        Route("/agentos", index),
        Route("/s/{service_id}", index),  # (a service's page: its shell in the ProductionBox)
        Route("/scripts", index),  # (the user's own SDK scripts)
        Route("/scripts/{name:path}", index),  # (one of them, open)
        Route("/p/{name}", index),  # (a plugin's page: relay.plugins, static/js/plugins.js)
        Route("/p/{name}/{path:path}", index),
        Route("/providers/new", index),  # (a provider Relay doesn't know, added with an agent: relay.creations)
        Route("/providers/new/{creation_id}", index),
        Route("/providers/repair/new", index),  # (repairing one: the same page's way)
        Route("/providers/repair/{creation_id}", index),
        Route("/sdk", sdk_page),  # Relay's Python SDK, for pip (no session: pip has none)
        Route("/sdk/", sdk_page),
        Route("/sdk/{name}", sdk_wheel),
        Route("/api/state", api(state)),
        Route("/api/new-chat", api(set_new_chat), methods=["PUT"]),
        Route("/api/setup", api(setup), methods=["POST"]),
        Route("/api/login", api(login), methods=["POST"]),
        Route("/api/logout", api(logout), methods=["POST"]),
        Route("/api/account/password", api(change_password), methods=["POST"]),
        Route("/api/app-passwords", api(app_passwords), methods=["GET"]),
        Route("/api/app-passwords", api(add_app_password), methods=["POST"]),
        Route("/api/app-passwords/{password_id}", api(remove_app_password), methods=["DELETE"]),
        Route("/api/settings", api(get_settings)),
        Route("/api/settings", api(put_settings), methods=["PUT"]),
        Route("/api/memory", api(get_memory)),
        Route("/api/memory", api(put_memory), methods=["PUT"]),
        Route("/api/memory", api(add_memory), methods=["POST"]),
        Route("/api/memory", api(clear_memory), methods=["DELETE"]),
        Route("/api/memory/{memory_id}", api(change_memory), methods=["PATCH"]),
        Route("/api/memory/{memory_id}", api(forget_memory), methods=["DELETE"]),
        Route("/api/notifications", api(send_notification), methods=["POST"]),
        Route("/api/notifications/test", api(test_notifications), methods=["POST"]),
        Route("/api/limits", api(get_limits)),
        Route("/api/limits", api(put_limits), methods=["PUT"]),
        Route("/api/provider-kinds", api(provider_kinds)),
        Route("/api/provider-kinds/{kind}/draft", api(start_draft), methods=["POST"]),
        Route("/api/provider-kinds/{kind}/test", api(test_draft), methods=["POST"]),
        Route("/api/providers/guide", api(provider_guide)),
        Route("/api/providers", api(list_providers)),
        Route("/api/providers", api(add_provider), methods=["POST"]),
        Route("/api/providers/{provider_id}", api(update_provider), methods=["PUT"]),
        Route("/api/providers/{provider_id}", api(delete_provider), methods=["DELETE"]),
        Route("/api/providers/{provider_id}/check", api(check_provider), methods=["POST"]),
        Route("/api/providers/{provider_id}/conformance", api(check_conformance), methods=["POST"]),
        Route("/api/providers/{provider_id}/models", api(provider_models)),
        Route("/api/providers/{provider_id}/test", api(test_model), methods=["POST"]),
        Route("/api/providers/{provider_id}/trouble", api(provider_trouble)),
        Route("/api/provider-creations", api(creations)),
        Route("/api/provider-creations", api(new_creation), methods=["POST"]),
        Route("/api/provider-creations/{creation_id}", api(get_creation)),
        Route("/api/provider-creations/{creation_id}/{action}", api(creation_action), methods=["POST"]),
        # (a repair's page: the same actions, relay.creations)
        Route("/api/provider-repairs", api(new_repair), methods=["POST"]),
        Route("/api/provider-repairs/{creation_id}", api(get_creation)),
        Route("/api/provider-repairs/{creation_id}/{action}", api(creation_action), methods=["POST"]),
        Route("/api/repairs/{repair_id}", api(get_repair)),
        Route("/api/restart", api(restart), methods=["POST"]),
        Route("/api/models/status", api(model_statuses)),
        Route("/api/models/ranking", api(model_ranking)),
        Route("/api/models/ranking", api(forget_ranking), methods=["DELETE"]),
        Route("/api/providers/{provider_id}/efforts", api(provider_efforts)),
        Route("/api/usage", api(usage_overview)),
        Route("/api/usage/limits", api(usage_limits)),
        Route("/api/skills", api(list_skills)),
        Route("/api/skills", api(add_skill), methods=["POST"]),
        Route("/api/skills/{name}", api(get_skill)),
        Route("/api/skills/{name}", api(delete_skill), methods=["DELETE"]),
        Route("/api/skills/{name}/file", api(skill_file), methods=["GET", "PUT"]),
        Route("/api/schedules", api(list_schedules)),
        Route("/api/schedules/{schedule_id}", api(get_schedule)),
        Route("/api/schedules", api(add_schedule), methods=["POST"]),
        Route("/api/schedules/{schedule_id}", api(update_schedule), methods=["PUT"]),
        Route("/api/schedules/{schedule_id}", api(delete_schedule), methods=["DELETE"]),
        Route("/api/schedules/{schedule_id}/run", api(run_schedule), methods=["POST"]),
        Route("/api/schedules/{schedule_id}/reset", api(reset_schedule), methods=["POST"]),
        Route("/api/agentos", api(get_agentos)),
        Route("/api/agentos", api(put_agentos), methods=["PUT"]),
        Route("/api/agentos/wake", api(wake_agentos), methods=["POST"]),
        Route("/api/agentos/interventions/{intervention_id}/resolve", api(resolve_intervention), methods=["POST"]),
        Route("/api/system", api(get_system)),
        Route("/api/system/storage", api(get_storage)),
        Route("/api/system/boxes/{name}/stop", api(stop_box), methods=["POST"]),
        Route("/api/system/boxes/{name}/remove", api(remove_box), methods=["POST"]),
        Route("/api/system/workboxes/remove-stopped", api(remove_stopped), methods=["POST"]),
        Route("/api/system/images/{image_id}/remove", api(remove_image), methods=["POST"]),
        Route("/api/search", api(get_search)),
        Route("/api/search", api(put_search), methods=["PUT"]),
        Route("/api/search/try", api(try_search), methods=["POST"]),
        Route("/api/plugins", api(list_plugins)),
        Route("/api/plugins/{name}", api(update_plugin), methods=["PATCH"]),
        Route("/api/plugins/{name}", api(add_plugin), methods=["POST"]),
        Route("/api/plugins/{name}/{path:path}", plugin_route, methods=["GET", "POST", "PUT", "PATCH", "DELETE"]),
        Route("/plugins/{name}/static/{path:path}", plugin_static),
        Route("/api/vpn", api(get_vpn)),
        Route("/api/vpn/servers", api(vpn_servers)),
        Route("/api/vpn/profiles", api(add_profile), methods=["POST"]),
        Route("/api/vpn/profiles/{profile_id}", api(update_profile), methods=["PUT"]),
        Route("/api/vpn/profiles/{profile_id}", api(delete_profile), methods=["DELETE"]),
        Route("/api/vpn/tunnels", api(add_tunnel), methods=["POST"]),
        Route("/api/vpn/tunnels/{tunnel_id}", api(update_tunnel), methods=["PUT"]),
        Route("/api/vpn/tunnels/{tunnel_id}", api(delete_tunnel), methods=["DELETE"]),
        Route("/api/vpn/tunnels/{tunnel_id}/reconnect", api(reconnect_tunnel), methods=["POST"]),
        Route("/api/vpn/tunnels/{tunnel_id}/log", api(tunnel_log)),
        Route("/api/services", api(list_services)),
        Route("/api/services", api(add_service), methods=["POST"]),
        Route("/api/services/guess", api(guess_service)),  # (before {service_id})
        Route("/api/services/{service_id}", api(get_service)),
        Route("/api/services/{service_id}", api(delete_service), methods=["DELETE"]),
        Route("/api/services/{service_id}", api(update_service), methods=["PATCH"]),
        Route("/api/services/{service_id}/rollback", api(rollback_service), methods=["POST"]),
        Route("/api/services/{service_id}/restore", api(restore_service), methods=["POST"]),
        Route("/api/services/{service_id}/restart", api(restart_service), methods=["POST"]),
        Route("/api/services/{service_id}/stop", api(stop_service), methods=["POST"]),
        Route("/api/services/{service_id}/start", api(start_service), methods=["POST"]),
        Route("/api/services/{service_id}/demote", api(demote_service), methods=["POST"]),
        Route("/api/services/{service_id}/promote", api(promote_service), methods=["POST"]),
        Route("/api/services/{service_id}/update-workbox", api(update_workbox), methods=["POST"]),
        Route("/api/services/{service_id}/logs", api(service_logs)),
        Route("/api/services/{service_id}/status", api(service_status)),
        Route("/api/fallback-loops", api(get_fallback_loops)),
        Route("/api/fallback-loops", api(put_fallback_loops), methods=["PUT"]),
        Route("/api/subagents", api(get_subagent_policy)),
        Route("/api/subagents", api(put_subagent_policy), methods=["PUT"]),
        Route("/api/modes", api(list_modes)),
        Route("/api/modes", api(add_mode), methods=["POST"]),
        Route("/api/modes/{mode_id}", api(update_mode), methods=["PUT"]),
        Route("/api/modes/{mode_id}", api(delete_mode), methods=["DELETE"]),
        Route("/api/modes/{mode_id}/icon", api(set_mode_icon), methods=["PUT"]),
        Route("/api/uploads", api(upload), methods=["POST"]),
        Route("/api/uploads/{upload_id}", get_upload),
        Route("/api/workspaces", api(browse_workspaces)),
        Route("/api/workspaces", api(create_workspace), methods=["POST"]),
        Route("/api/tasks", api(list_tasks)),
        Route("/api/scripts", api(list_scripts)),
        Route("/api/scripts", api(set_scripts_folder), methods=["PUT"]),
        Route("/api/scripts/templates", api(script_templates)),
        Route("/api/scripts/docs", api(script_docs)),
        Route("/api/scripts/share", api(share_scripts), methods=["POST"]),
        Route("/api/scripts/write", api(write_script), methods=["POST"]),
        Route("/api/scripts/runs/{run_id}", api(get_script_run)),
        Route("/api/scripts/runs/{run_id}/output", script_output),
        Route("/api/scripts/runs/{run_id}/stop", api(stop_script_run), methods=["POST"]),
        # (a name may be examples/…: Relay's examples)
        Route("/api/scripts/{name:path}", api(get_script)),
        Route("/api/scripts/{name:path}", api(save_script), methods=["PUT"]),
        Route("/api/scripts/{name:path}", api(delete_script), methods=["DELETE"]),
        Route("/api/scripts/{name:path}/run", api(run_script), methods=["POST"]),
        Route("/api/scripts/{name:path}/rename", api(rename_script), methods=["POST"]),
        Route("/api/scripts/{name:path}/copy", api(copy_script), methods=["POST"]),
        Route("/api/scripts/{name:path}/move", api(move_script), methods=["POST"]),
        # (after those: {name:path} takes their …/run and the like too)
        Route("/api/scripts/{name:path}", api(create_script), methods=["POST"]),
        Route("/api/conversations", api(list_conversations)),
        Route("/api/conversations", api(create_conversation), methods=["POST"]),
        Route("/api/conversations/{conversation_id}", api(get_conversation)),
        Route("/api/conversations/{conversation_id}", api(update_conversation), methods=["PATCH"]),
        Route("/api/conversations/{conversation_id}", api(delete_conversation), methods=["DELETE"]),
        Route("/api/conversations/{conversation_id}/messages", api(send_message), methods=["POST"]),
        Route("/api/conversations/{conversation_id}/retry", api(retry_message), methods=["POST"]),
        Route("/api/conversations/{conversation_id}/queue", api(queue_message), methods=["POST"]),
        Route("/api/conversations/{conversation_id}/queue/{queued_id:int}", api(unqueue_message),
              methods=["DELETE"]),
        Route("/api/conversations/{conversation_id}/queue/{queued_id:int}/send", api(send_queued_now),
              methods=["POST"]),
        Route("/api/conversations/{conversation_id}/fork", api(fork_conversation), methods=["POST"]),
        Route("/api/conversations/{conversation_id}/clear", api(clear_conversation), methods=["POST"]),
        Route("/api/conversations/{conversation_id}/to-code", api(move_to_code), methods=["POST"]),
        Route("/api/conversations/{conversation_id}/resume", api(resume_later), methods=["POST"]),
        Route("/api/conversations/{conversation_id}/timed", api(set_timed), methods=["POST"]),
        Route("/api/conversations/{conversation_id}/continue", api(continue_now), methods=["POST"]),
        Route("/api/conversations/{conversation_id}/share", api(share_conversation), methods=["POST"]),
        Route("/api/shares", api(list_shares)),
        Route("/api/shares/{token}", api(delete_share), methods=["DELETE"]),
        Route("/api/conversations/{conversation_id}/rewind", api(rewind_files), methods=["POST"]),
        Route("/api/conversations/{conversation_id}/commands", api(conversation_commands)),
        Route("/api/conversations/{conversation_id}/changes", api(task_changes)),
        Route("/api/conversations/{conversation_id}/export.md", export_conversation),
        Route("/api/conversations/{conversation_id}/merge", api(merge_task), methods=["POST"]),
        Route("/api/conversations/{conversation_id}/reviewed", api(mark_reviewed), methods=["POST"]),
        Route("/api/conversations/{conversation_id}/commit", api(commit_task), methods=["POST"]),
        Route("/api/conversations/{conversation_id}/discard", api(discard_task), methods=["POST"]),
        Route("/api/conversations/{conversation_id}/copy", api(subagent_copy), methods=["POST"]),
        Route("/api/runs/{run_id}/events", run_events),
        Route("/mcp/{token}", mcp_endpoint, methods=["GET", "POST", "DELETE"]),
        Route("/tools-for", tools_for),
        Route("/computer/{token}/poll", computer_poll, methods=["POST"]),
        Route("/computer/{token}/result", computer_result, methods=["POST"]),
        Route("/api/computers", api(list_computers)),
        Route("/api/computers", api(add_computer), methods=["POST"]),
        Route("/api/computers/{computer_id}", api(update_computer), methods=["PATCH"]),
        Route("/api/computers/{computer_id}", api(delete_computer), methods=["DELETE"]),
        Route("/api/computers/{computer_id}/script", computer_script),
        Route("/api/runs/{run_id}/stop", api(stop_run), methods=["POST"]),
        Route("/api/runs/{run_id}/permission", api(answer_permission), methods=["POST"]),
        Route("/api/runs/{run_id}/form", api(answer_form), methods=["POST"]),
        Mount("/static", StaticFiles(directory=config.static_dir()), name="static"),
    ]
    # (what plugins can't forward to a server of their own: the first segments of Relay's paths)
    relay.plugins.taken = {r.path.strip("/").split("/")[0] for r in routes} | {"port", "trial"}
    app = Starlette(routes=routes, lifespan=lifespan, exception_handlers={HTTPException: http_error},
                    middleware=[Middleware(Forwarding), Middleware(SecurityMiddleware)])
    app.state.relay = relay
    return app


ERRORS = {404: ("Page not found", "Relay has no page at this address. The link may be old, or the address mistyped."),
          405: ("Not here", "This address doesn't take this kind of request.")}


async def http_error(request: Request, exc: HTTPException) -> Response:
    """Relay's page (web.error_page) for a browser that asks for one; as Starlette answers for the rest:
    the API's callers, tools, a script or a stylesheet that isn't there."""
    if exc.status_code in (204, 304):
        return Response(status_code=exc.status_code, headers=exc.headers)
    if "text/html" in request.headers.get("accept", "") and not request.url.path.startswith(
            ("/api/", "/mcp/", "/dav/", "/computer/")):
        title, text = ERRORS.get(exc.status_code, (exc.detail, ""))
        return HTMLResponse(error_page(exc.status_code, title, text, request.url.path,
                                       request.app.state.relay.settings.base_path),
                            status_code=exc.status_code, headers=exc.headers)
    return PlainTextResponse(exc.detail, status_code=exc.status_code, headers=exc.headers)


class SecurityMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        relay: Relay = request.app.state.relay
        path = request.url.path
        if path.startswith("/api/"):
            # An app password (a script, the SDK) comes in the Authorization header, which another site's page can't
            # send without a permission Relay never gives (no CORS): it needs no CSRF header, and no cookie counts then.
            # Unless it's a proxy's own sign-in in front of Relay: then the session counts, as without it.
            given = app_credentials(request)
            found = None
            if given is not None and relay.login_required:
                found = relay.accounts.user_for_app_password(given[1], given[0])
                if found is None and proxy_sign_in(request):
                    given = None
            if given is None and request.method not in ("GET", "HEAD") and request.headers.get(CSRF_HEADER) != "1":
                return JSONResponse({"error": "Missing request header"}, status_code=403)
            app_password = None
            if not relay.login_required:
                user = relay.accounts.local_user()
            elif given is not None:
                user = found and {"id": found["id"], "username": found["username"]}
                app_password = found and found["app_password"]
            else:
                user = relay.accounts.user_for_token(request.cookies.get(COOKIE))
            if user is None and path not in PUBLIC_API:
                return JSONResponse({"error": "Not signed in" if given is None else
                                     "That app password isn't Relay's (any more): see Settings → Account"},
                                    status_code=401)
            request.state.user = user
            request.state.app_password = app_password  # (its id: what it may not do, routes.accounts says)
        response = await call_next(request)
        # A network drive serves the project's own files from Relay's origin: an HTML
        # file there must not run scripts with the user's session (relay.webdav).
        if path.startswith("/dav/"):
            response.headers["Content-Security-Policy"] = DAV_CSP
        else:
            response.headers.setdefault("Content-Security-Policy", CSP)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        if path.startswith("/api/"):
            response.headers.setdefault("Cache-Control", "no-store")
        elif not path.startswith("/dav/"):
            # The app's modules must match each other: a browser checks each time (a 304 when
            # unchanged) instead of mixing a new main.js with an old ui.js from its cache.
            response.headers.setdefault("Cache-Control", "no-cache")
        return response
