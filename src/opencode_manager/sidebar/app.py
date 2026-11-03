"""The sidebar: a Textual app running in the left tmux pane."""

from __future__ import annotations

from pathlib import Path

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import Label, ListItem, ListView, Static

from opencode_manager import configfile, opencode, paths
from opencode_manager.profiles import Profile, ProfileError, ProfileStore
from opencode_manager.settings import Settings
from opencode_manager.sidebar.modals import ConfirmScreen, NewProfileRequest, NewProfileScreen
from opencode_manager.tmux import TmuxError
from opencode_manager.workspace import Workspace

HELP = """\
[b]enter[/] switch   [b]n[/] new
[b]a[/] login        [b]d[/] delete
[b]r[/] restart      [b]o[/] focus
[b]e[/] edit config  [b]p[/] project
[b]q[/] quit"""


class ProfileItem(ListItem):
    def __init__(self, profile: Profile, active: bool) -> None:
        marker = "●" if active else "○"
        super().__init__(Label(f"{marker} {profile.name}"), classes="active" if active else "")
        self.profile = profile


class SidebarApp(App):
    CSS_PATH = "sidebar.tcss"
    TITLE = "opencode-manager"
    BINDINGS = [
        Binding("enter", "switch", "Switch", show=False),
        Binding("n", "new", "New"),
        Binding("d", "delete", "Delete"),
        Binding("a", "login", "Login"),
        Binding("r", "restart", "Restart"),
        Binding("e", "edit", "Edit config"),
        Binding("p", "pick_project", "Open project"),
        Binding("o", "focus_opencode", "Focus opencode"),
        Binding("q", "quit_workspace", "Quit"),
    ]

    def __init__(self, settings: Settings, store: ProfileStore, workspace: Workspace) -> None:
        super().__init__()
        self.settings = settings
        self.store = store
        self.workspace = workspace
        # (active profile, project, profile names) as currently displayed.
        self._shown: tuple[str, Path, tuple[str, ...]] | None = None

    def compose(self) -> ComposeResult:
        yield Static("opencode-manager", id="title")
        yield Label("PROFILES", classes="section")
        yield ListView(id="profiles")
        yield Label("PROJECT", classes="section")
        yield Static(self._project_label(self.workspace.directory), id="project")
        yield Label("MODE", classes="section")
        yield Static(self.settings.launch_mode, id="mode")
        yield Static("", id="status")
        yield Static(HELP, id="help")

    async def on_mount(self) -> None:
        await self.refresh_profiles()
        self.query_one(ListView).focus()
        # Scripts (`opencode-manager switch/open/profile ...`) change the
        # workspace behind our back; follow along.
        self.set_interval(1.0, self._poll_workspace)

    def _poll_workspace(self) -> None:
        self._read_workspace()

    @work(thread=True, exclusive=True, group="poll")
    def _read_workspace(self) -> None:
        try:
            current = (
                self.workspace.active_profile,
                self.workspace.directory,
                tuple(p.name for p in self.store.list()),
            )
        except (TmuxError, OSError):
            return  # e.g. the session is closing
        if current != self._shown:
            self.call_from_thread(self._show_external_change, current)

    async def _show_external_change(self, current: tuple[str, Path, tuple[str, ...]]) -> None:
        selected = self._selected()
        await self.refresh_profiles(highlight=selected.name if selected else None)
        self.query_one("#project", Static).update(self._project_label(current[1]))

    async def refresh_profiles(self, highlight: str | None = None) -> None:
        active = self.workspace.active_profile
        highlight = highlight or active
        view = self.query_one(ListView)
        await view.clear()
        profiles = self.store.list()
        self._shown = (active, self.workspace.directory, tuple(p.name for p in profiles))
        await view.extend(ProfileItem(p, p.name == active) for p in profiles)
        view.index = next((i for i, p in enumerate(profiles) if p.name == highlight), 0)

    def _project_label(self, directory: Path) -> str:
        # The screen's side padding takes 2 columns.
        return _fit(_short_path(directory), self.settings.sidebar_width - 2)

    def _selected(self) -> Profile | None:
        item = self.query_one(ListView).highlighted_child
        return item.profile if isinstance(item, ProfileItem) else None

    def _status(self, message: str, error: bool = False) -> None:
        status = self.query_one("#status", Static)
        status.update(message)
        status.set_class(error, "error")

    # Actions

    def on_list_view_selected(self) -> None:
        self.action_switch()

    def action_switch(self) -> None:
        if profile := self._selected():
            self._launch(profile, f"Switched to {profile.name}")

    def action_restart(self) -> None:
        try:
            profile = self.store.get(self.workspace.active_profile)
        except ProfileError as exc:
            self._status(str(exc), error=True)
            return
        self._launch(profile, "Restarted opencode")

    def action_login(self) -> None:
        if profile := self._selected():
            self._launch(profile, f"Log in for {profile.name} →", login_first=True)

    def action_pick_project(self) -> None:
        self._pick_project()

    @work(thread=True, exclusive=True)
    def _pick_project(self) -> None:
        try:
            directory = self.workspace.pick_project()
            if directory is None or directory == self.workspace.directory:
                return
            self.call_from_thread(self._status, f"Opening {directory.name}…")
            self.workspace.open_project(directory)
        except (TmuxError, OSError, ProfileError, opencode.OpencodeError) as exc:
            self.call_from_thread(self._status, str(exc), True)
            return
        self.call_from_thread(
            self.query_one("#project", Static).update, self._project_label(directory)
        )
        self.call_from_thread(self._status, f"Opened {directory.name}")
        self.workspace.focus_opencode()

    def action_edit(self) -> None:
        if profile := self._selected():
            self._edit(profile)

    @work(thread=True, exclusive=True)
    def _edit(self, profile: Profile) -> None:
        path = configfile.config_file(profile)
        before = configfile.read(path)
        try:
            warning = self.workspace.edit_config(profile)
        except TmuxError as exc:
            self.call_from_thread(self._status, str(exc), True)
            return
        after = configfile.read(path)
        if after == before:
            self.call_from_thread(self._status, warning or "No changes")
            return
        # An external editor may have saved invalid JSON; opencode would fail.
        if after is not None and (error := configfile.validate(after)):
            self.call_from_thread(self._status, f"{path.name} is invalid. {error}", True)
            return
        self.call_from_thread(self._offer_apply, profile)

    def _offer_apply(self, profile: Profile) -> None:
        active = profile.name == self.workspace.active_profile
        uses_service = self.settings.launch_mode == "service"
        if not active and not uses_service:
            self._status(f"Saved. Applies next time {profile.name} starts.")
            return
        if profile.is_default and uses_service:
            question = "Apply now? This restarts this machine's opencode service."
        else:
            question = f"Apply now? This restarts {profile.name}."

        def confirmed(yes: bool | None) -> None:
            if yes:
                self._apply(profile, active)
            else:
                self._status("Saved. Restart the profile to apply.")

        self.push_screen(ConfirmScreen(question), confirmed)

    @work(thread=True, exclusive=True)
    def _apply(self, profile: Profile, active: bool) -> None:
        self.call_from_thread(self._status, f"Restarting {profile.name}…")
        try:
            opencode.restart_service(self.settings, profile)
            if active:
                self.workspace.launch(profile)
        except (TmuxError, OSError, opencode.OpencodeError) as exc:
            self.call_from_thread(self._status, str(exc), True)
            return
        self.call_from_thread(self._status, f"Applied config for {profile.name}")

    def action_focus_opencode(self) -> None:
        self.workspace.focus_opencode()

    def action_new(self) -> None:
        existing = {p.name for p in self.store.list()}
        self.push_screen(NewProfileScreen(existing), self._create_profile)

    async def _create_profile(self, request: NewProfileRequest | None) -> None:
        if request is None:
            return
        source = paths.default_opencode_config_dir() if request.copy_config else None
        try:
            profile = self.store.create(request.name, copy_config_from=source)
        except (ProfileError, OSError) as exc:
            self._status(str(exc), error=True)
            return
        await self.refresh_profiles(highlight=profile.name)
        self._status(f"Created {profile.name}. Press a to log in.")

    def action_delete(self) -> None:
        profile = self._selected()
        if profile is None:
            return
        if profile.is_default:
            self._status("The default profile can't be deleted", error=True)
            return
        if profile.name == self.workspace.active_profile:
            self._status("Switch to another profile first", error=True)
            return

        async def confirmed(yes: bool | None) -> None:
            if yes:
                self._delete_profile(profile)

        self.push_screen(
            ConfirmScreen(f"Delete {profile.name}? Its auth and sessions are removed."),
            confirmed,
        )

    @work(thread=True, exclusive=True)
    def _delete_profile(self, profile: Profile) -> None:
        self.call_from_thread(self._status, f"Deleting {profile.name}…")
        try:
            opencode.stop_service(self.settings, profile)
            self.store.delete(profile.name)
        except (ProfileError, OSError, opencode.OpencodeError) as exc:
            self.call_from_thread(self._status, str(exc), True)
            return
        self.call_from_thread(self.refresh_profiles)
        self.call_from_thread(self._status, f"Deleted {profile.name}")

    def action_quit_workspace(self) -> None:
        def confirmed(yes: bool | None) -> None:
            if yes:
                self._quit()

        self.push_screen(ConfirmScreen("Quit opencode-manager?"), confirmed)

    @work(thread=True, exclusive=True)
    def _quit(self) -> None:
        self.call_from_thread(self._status, "Stopping opencode…")
        try:
            self.workspace.quit()
        except (TmuxError, OSError, opencode.OpencodeError) as exc:
            self.call_from_thread(self._status, str(exc), True)

    @work(thread=True, exclusive=True)
    def _launch(self, profile: Profile, message: str, login_first: bool = False) -> None:
        # Runs in a thread: preparing a profile's service can take a moment.
        self.call_from_thread(self._status, f"Starting {profile.name}…")
        try:
            self.workspace.launch(profile, login_first=login_first)
        except (TmuxError, OSError, opencode.OpencodeError) as exc:
            self.call_from_thread(self._status, str(exc), True)
            return
        self.call_from_thread(self.refresh_profiles, profile.name)
        self.call_from_thread(self._status, message)
        self.workspace.focus_opencode()


def _fit(text: str, width: int) -> str:
    """Shorten `text` from the left so it fits on one line of `width` columns."""
    return text if len(text) <= width else "…" + text[len(text) - width + 1 :]


def _short_path(path: Path) -> str:
    try:
        return "~/" + str(path.relative_to(Path.home()))
    except ValueError:
        return str(path)
