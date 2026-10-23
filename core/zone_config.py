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
        return {k: int(v) for k, v in (self._section().get("zone_volumes") or {}).items()
                if isinstance(v, (int, float))}

    @_zone_volumes.setter
    def _zone_volumes(self, value) -> None:
        self._pins()["zone_volumes"] = value

    @property
    def _volume(self) -> int:
        pins = self._pins()
        if "volume" in pins:
            return pins["volume"]
        from config import DEFAULT_CLIP_VOLUME

        return int(self._section().get("clip_volume", DEFAULT_CLIP_VOLUME))

    @_volume.setter
    def _volume(self, value) -> None:
        self._pins()["volume"] = value
