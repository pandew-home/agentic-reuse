#!/bin/sh
set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

install_skill() {
    skill=$1
    destination=$2
    source=$script_dir/skills/$skill/SKILL.md
    target=$destination/$skill

    mkdir -p "$target"
    temp=$(mktemp "$target/.SKILL.md.XXXXXX")
    trap 'rm -f "$temp"' EXIT HUP INT TERM
    cp "$source" "$temp"
    chmod 0644 "$temp"
    mv "$temp" "$target/SKILL.md"
    trap - EXIT HUP INT TERM
}

for destination in "$HOME/.agents/skills" "$HOME/.claude/skills"; do
    install_skill agent-ops "$destination"
    install_skill agent-ops-author "$destination"
done

printf '%s\n' "Installed agent-ops and agent-ops-author skills."
