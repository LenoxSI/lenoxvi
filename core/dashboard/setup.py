"""setup — the first-run setup wizard's server side.

The installer drops an empty `setup.pending` marker in the state dir when it
writes a fresh config.yaml; the dashboard opens the wizard while the marker
exists, and Finish or Skip removes it. An install without the marker (an
upgrade, a hand-made box, a dev clone) never opens the wizard by itself.

step_states() is a pure function of what the existing cards already compute,
so a step's "done" always agrees with what its card shows. No framework
imports here: the routes call in, and the rules unit-test without a client.
"""
import os
import platform
import shutil

import paths


def pending() -> bool:
    """The first-run marker is present."""
    return paths.setup_marker().is_file()


def clear() -> bool:
    """Remove the marker. True if a file was removed; a missing one is fine."""
    try:
        paths.setup_marker().unlink()
    except FileNotFoundError:
        return False
    return True


def _ram_mb() -> int | None:
    # Linux only; anything else (or an unreadable file) reports unknown.
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) // 1024
    except (OSError, ValueError, IndexError):
        pass
    return None


def _disk_free_gb() -> int | None:
    try:
        return shutil.disk_usage(paths.state_root()).free // (1024 ** 3)
    except OSError:
        return None


def host_facts() -> dict:
    """What the welcome step shows about this machine. No paths, no names.
    `slow` (an ARM board or fewer than 4 cores) makes the speech steps
    suggest Local or Cloud."""
    arch = platform.machine()
    cpus = os.cpu_count()
    return {
        "arch": arch,
        "cpus": cpus,
        "ram_mb": _ram_mb(),
        "disk_free_gb": _disk_free_gb(),
        "python": platform.python_version(),
        "slow": arch == "aarch64" or (cpus is not None and cpus < 4),
    }


def _provider_step(sid: str, name: str, providers: list) -> dict:
    row = next((p for p in providers if p.get("name") == name), None)
    if row is None:
        return {"id": sid, "done": False, "detail": str(name or ""), "tier": None}
    return {"id": sid, "done": bool(row.get("available")), "detail": str(row.get("label") or name),
            "tier": row.get("tier")}


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" + ("" if n == 1 else "s")


def step_states(*, stt_current: dict, stt_providers: list, tts_provider: str, tts_providers: list,
                intelligence: dict, keys_set: dict, wake_file_ok: bool, zones: dict,
                capable_rooms: list, rooms: list, pending: bool, restart_pending: list,
                wake_custom: str = "") -> list:
    """The eight wizard steps, in order, each {id, done, detail} (listening
    and voice add their tier). A step not done never blocks the wizard.

    Inputs are what the cards' own helpers return: the STT pick and provider
    list, the resolved TTS provider and its list, the intelligence {kind,
    url, label} with keys_set per kind, whether the active wake model file
    resolves (and its file name when it is a custom model, "" for a stock
    one), every zone, the speaker-capable rooms, the known rooms, the marker
    state and the restart-pending labels. Details never carry a wake phrase.
    """
    kind = str(intelligence.get("kind") or "")
    brain_done = (kind.lower() != "stub" and bool(intelligence.get("url"))
                  and bool(keys_set.get(kind)))
    online = [r for r in rooms if r.get("online")]
    speaker_rooms = set(capable_rooms) & {r.get("room") for r in online}
    if zones:
        speakers_detail = _plural(len(zones), "zone")
    elif speaker_rooms:
        speakers_detail = _plural(len(speaker_rooms), "node speaker")
    else:
        speakers_detail = "no zones"
    if not wake_file_ok:
        wake_detail = "model missing"
    else:
        wake_detail = wake_custom or "stock model"
    return [
        {"id": "welcome", "done": True, "detail": ""},
        _provider_step("listening", str(stt_current.get("provider") or ""), stt_providers),
        _provider_step("voice", tts_provider, tts_providers),
        {"id": "brain", "done": brain_done,
         "detail": str(intelligence.get("label") or ("echo" if kind.lower() == "stub" else kind))},
        {"id": "wake", "done": bool(wake_file_ok), "detail": wake_detail},
        {"id": "speakers", "done": bool(zones) or bool(speaker_rooms), "detail": speakers_detail},
        {"id": "nodes", "done": bool(online), "detail": f"{len(online)} online"},
        {"id": "finish", "done": not pending, "detail": "restart needed" if restart_pending else "ready"},
    ]
