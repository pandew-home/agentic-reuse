#!/bin/sh
set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

SCRIPT_DIR=$script_dir python3 - <<'PY'
import os
import pathlib
import shutil
import tempfile

root = pathlib.Path(os.environ["SCRIPT_DIR"])
home = pathlib.Path.home()
skills = ("agent-ops", "agent-ops-author")
destinations = (home / ".agents" / "skills", home / ".claude" / "skills")
staged = []
committed = []

for skill in skills:
    source = root / "skills" / skill / "SKILL.md"
    if not source.is_file():
        raise SystemExit(f"Missing skill source: {source}")

try:
    for destination in destinations:
        destination.mkdir(mode=0o755, parents=True, exist_ok=True)
        for skill in skills:
            target_dir = destination / skill
            target_dir.mkdir(mode=0o755, exist_ok=True)
            fd, temporary = tempfile.mkstemp(prefix=".SKILL.md.", dir=target_dir)
            os.close(fd)
            temporary = pathlib.Path(temporary)
            shutil.copyfile(root / "skills" / skill / "SKILL.md", temporary)
            temporary.chmod(0o644)
            staged.append((temporary, target_dir / "SKILL.md"))

    for temporary, target in staged:
        backup = None
        if target.exists():
            fd, backup_name = tempfile.mkstemp(prefix=".SKILL.md.backup.", dir=target.parent)
            os.close(fd)
            backup = pathlib.Path(backup_name)
            os.replace(target, backup)
        try:
            os.replace(temporary, target)
        except Exception:
            if backup is not None:
                os.replace(backup, target)
            raise
        committed.append((target, backup))
except Exception:
    for target, backup in reversed(committed):
        target.unlink(missing_ok=True)
        if backup is not None:
            os.replace(backup, target)
    raise
finally:
    for temporary, _ in staged:
        temporary.unlink(missing_ok=True)

for _, backup in committed:
    if backup is not None:
        backup.unlink(missing_ok=True)
PY

printf '%s\n' "Installed agent-ops and agent-ops-author skills."
