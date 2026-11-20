"""env_store — reads, private backups and atomic 0600 writes for the
.env keys file.

The one home for secrets handling: every route that shows, saves,
tests or drops an API key goes through here. Reads mirror the bot's
own dotenv load (what the next restart loads), and a replace always
backs the file up first — a bad paste stays recoverable.
"""

import os


def path():
    import paths as _paths
    return _paths.env_file()


def file_env() -> dict:
    """What .env holds NOW, parsed exactly the way the bot loads it
    (python-dotenv: quotes, `export`, inline comments). Read-only."""
    try:
        from dotenv import dotenv_values
    except ImportError:  # pragma: no cover — dotenv ships with the bot
        dotenv_values = None
    env_path = path()
    if not env_path.is_file():
        return {}
    if dotenv_values is not None:
        return {k: v for k, v in dotenv_values(env_path).items() if v is not None}
    view = {}
    for ln in env_path.read_text().splitlines():
        k, sep, v = ln.partition("=")
        if sep and k and not k.lstrip().startswith("#"):
            view[k.strip()] = v.strip()
    return view


def saved_env() -> dict:
    """Process env overlaid with what .env holds NOW, so key status and
    hints show a key the moment it is saved (the voice agent itself still
    reads the process env, at the next restart). Read-only."""
    view = dict(os.environ)
    try:
        view.update(file_env())
    except OSError:
        pass
    return view


def key_set(var: str | None) -> bool:
    """A key is set: the live process env OR the saved .env (a key saved on
    the Keys card counts before the restart that loads it). Presence only —
    never the value."""
    return bool(var and (os.getenv(var) or saved_env().get(var)))


_BACKUPS_KEPT = 3


def _backup(env_path) -> None:
    """Before a key is REPLACED: copy .env to .env.bak-<time> (0600) and
    keep the newest few — a bad paste stays recoverable. A key line is
    never deleted, and neither is its last copy."""
    import os as _os
    import re as _re
    from datetime import datetime as _dt
    from datetime import timezone as _tz

    # UTC + 'Z': names sort in time order all year (no DST repeat hour).
    stamp = _dt.now(_tz.utc).strftime("%Y%m%d-%H%M%S-%fZ")
    bak = env_path.with_name(f"{env_path.name}.bak-{stamp}")
    fd = _os.open(bak, _os.O_WRONLY | _os.O_CREAT | _os.O_EXCL, 0o600)
    with _os.fdopen(fd, "w") as f:
        f.write(env_path.read_text())
    # Prune only OUR backups; a hand-made .env.bak-whatever is never touched.
    ours = _re.compile(_re.escape(env_path.name) + r"\.bak-\d{8}-\d{6}-\d{6}Z")
    mine = sorted(p for p in env_path.parent.glob(f"{env_path.name}.bak-*") if ours.fullmatch(p.name))
    for old in mine[:-_BACKUPS_KEPT]:
        old.unlink(missing_ok=True)


async def write_key(var: str, value: str) -> None:
    """Replace-or-append `var=value` in .env.

    Atomic write + 0600: write_text created a
    missing .env as 0644, and a crash mid-write could truncate it.
    TTSKeyPayload's character-class pattern is the upstream defense
    against newline smuggling — this is the belt to that suspenders.
    """
    env_path = path()
    lines = env_path.read_text().splitlines() if env_path.is_file() else []
    out, replaced, changed = [], False, False
    for ln in lines:
        # Both line forms, like remove_key: an `export VAR=` line
        # missed here was kept AND a duplicate appended. It keeps its prefix.
        if ln.startswith(var + "="):
            new = var + "=" + value
        elif ln.lstrip().startswith(f"export {var}="):
            new = ln[:ln.index("export ")] + f"export {var}=" + value
        else:
            out.append(ln)
            continue
        changed = changed or ln != new
        out.append(new)
        replaced = True
    if not replaced:
        out.append(var + "=" + value)
    if changed:
        _backup(env_path)
    _write_private(env_path, "\n".join(out) + "\n")


def _write_private(env_path, text: str) -> None:
    """Atomic 0600 write of .env. The temp file is 0600 from creation —
    created under the umask and chmod'ed after, it held every secret
    world-readable for a moment."""
    tmp = env_path.parent / (env_path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)  # a leftover temp file keeps its old mode through O_CREAT
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.replace(tmp, env_path)  # atomic on the same filesystem


def saved_value(var: str) -> str:
    """The key the NEXT restart loads: .env is the store (systemd loads it
    as EnvironmentFile), so a removed line means removed even while the
    running process still holds the old value."""
    if path().is_file():
        return file_env().get(var) or ""
    return os.environ.get(var) or ""


async def remove_key(var: str) -> bool:
    """Drop `var=` from .env (backup first, atomic, 0600). False if absent."""
    env_path = path()
    if not env_path.is_file():
        return False
    lines = env_path.read_text().splitlines()
    keep = [ln for ln in lines
            if not (ln.startswith(var + "=") or ln.lstrip().startswith(f"export {var}="))]
    if len(keep) == len(lines):
        return False
    _backup(env_path)
    _write_private(env_path, "\n".join(keep) + ("\n" if keep else ""))
    return True
