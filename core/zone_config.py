"""Speaker-zone settings read at use time, not when a session is built.

SonosOutput and WiiMOutput live as long as a node's session. They used to
copy `enabled`, `zones`, `zone_volumes` and `clip_volume` from config in
their constructors, so a zone added (or a volume reset) on the dashboard
after a node connected never reached that room: the mapping said "live",
the reply went nowhere until the node reconnected. Reading through the
cached cfg() costs a dict lookup; every dashboard write clears that cache.

Assigning one of these attributes pins it for this instance (test
fixtures do this). A constructor given an explicit config file keeps that
file's values (suite fixtures only).
"""

from __future__ import annotations

from loguru import logger

_warned_bad_values: set = set()  # (key, repr(value)): read on every reply, warned once


def _whole_number(key: str, value, instead: str) -> int | None:
    """`value` as an int, or None for a value a hand edit got wrong. A bad
    volume must never raise on the reply path or in the duck holder's loop."""
    if not isinstance(value, bool):  # YAML `on`/`yes` is not a volume
        try:
            return int(value)
        except (TypeError, ValueError, OverflowError):
            pass
    if (key, repr(value)) not in _warned_bad_values:
        _warned_bad_values.add((key, repr(value)))
        logger.warning(f"zone_config: {key} {value!r} is not a whole number — {instead}")
    return None


class LiveZoneConfig:
    _section_name = ""  # "sonos" | "wiim"
    _static_section: dict | None = None  # set when built from an explicit file

    def _section(self) -> dict:
        if self._static_section is not None:
            return self._static_section
        from config import cfg

        return cfg(self._section_name) or {}

    def _pins(self) -> dict:
        return self.__dict__.setdefault("_zone_cfg_pins", {})

    @property
    def _enabled(self) -> bool:
        pins = self._pins()
        return pins["enabled"] if "enabled" in pins else bool(self._section().get("enabled"))

    @_enabled.setter
    def _enabled(self, value) -> None:
        self._pins()["enabled"] = value

    @property
    def _zones(self) -> dict:
        pins = self._pins()
        return pins["zones"] if "zones" in pins else (self._section().get("zones") or {})

    @_zones.setter
    def _zones(self, value) -> None:
        self._pins()["zones"] = value

    @property
    def _zone_volumes(self) -> dict:
        pins = self._pins()
        if "zone_volumes" in pins:
            return pins["zone_volumes"]
        volumes = {}
        for k, v in (self._section().get("zone_volumes") or {}).items():
            vol = _whole_number(f"{self._section_name}.zone_volumes.{k}", v,
                                "zone skipped, the global clip_volume applies")
            if vol is not None:
                volumes[k] = vol
        return volumes

    @_zone_volumes.setter
    def _zone_volumes(self, value) -> None:
        self._pins()["zone_volumes"] = value

    @property
    def _volume(self) -> int:
        pins = self._pins()
        if "volume" in pins:
            return pins["volume"]
        from config import DEFAULT_CLIP_VOLUME

        vol = _whole_number(f"{self._section_name}.clip_volume",
                            self._section().get("clip_volume", DEFAULT_CLIP_VOLUME),
                            f"using {DEFAULT_CLIP_VOLUME}")
        return DEFAULT_CLIP_VOLUME if vol is None else vol

    @_volume.setter
    def _volume(self, value) -> None:
        self._pins()["volume"] = value
