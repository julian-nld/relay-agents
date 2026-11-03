# Keep the normal interactive shell and its customisations; only the disaster guard is added.
[[ -f ~/.bashrc ]] && source ~/.bashrc

_relay_terminal_guard() {
    python3 "$RELAY_TERMINAL_GUARD" --command "$1"
}
# A failing DEBUG trap skips that command, without ending the interactive shell.
shopt -s extdebug
trap '_relay_terminal_guard "$BASH_COMMAND"' DEBUG
