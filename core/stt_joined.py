"""Stretch-audio store for joint re-transcription of multi-stretch turns.

Segmented STT transcribes every VAD stretch on its own, so when a turn
closes over more than one stretch the tail stretch was transcribed with
no context and a short tail word is easily misheard. At such a close the
agent asks the STT service to transcribe the stretches' audio together —
one request over the concatenated audio. This module is the service side
of that: it keeps each stretch's audio under its transcript while the
service transcribes it, and turns the turn's stretch texts back into one
WAV clip under the retention bounds. It only listens: normal per-stretch
transcription flows exactly as before.
"""

import io
import wave

# A stretch's audio is kept for joining only when it is at most this long:
# short stretches are the misheard tails; a long stretch carries its own
# context.
STRETCH_KEEP_SECS = 8.0
# A multi-stretch turn is transcribed together only when the joined audio
# stays under this length.
JOIN_MAX_SECS = 30.0
# The store never holds more than this much audio in total; the oldest
# stretch is evicted first when a new one overflows it.
STORE_MAX_SECS = 60.0


def _wav_entry(audio: bytes):
    """(PCM payload, seconds, (channels, sample width, frame rate)) of a
    WAV clip, or None when it does not parse as one."""
    try:
        with wave.open(io.BytesIO(audio)) as w:
            params = (w.getnchannels(), w.getsampwidth(), w.getframerate())
            payload = w.readframes(w.getnframes())
    except Exception:  # noqa: BLE001 — an odd clip is skipped, never raised
        return None
    if min(params) <= 0:
        return None
    return payload, len(payload) / (params[0] * params[1] * params[2]), params


def _wrap_wav(payload: bytes, params: tuple[int, int, int]) -> bytes:
    """A WAV clip around raw PCM frames."""
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(params[0])
        w.setsampwidth(params[1])
        w.setframerate(params[2])
        w.writeframes(payload)
    return out.getvalue()


class StretchStore:
    """(stretch text -> stretch audio) for joint re-transcription: texts
    in, one clip out, bounded, the oldest evicted first."""

    def __init__(self) -> None:
        # Insertion-ordered: the front is the oldest, the first to evict.
        self._entries: dict[str, tuple[bytes, float, tuple[int, int, int]]] = {}

    def keep(self, text: str, audio: bytes) -> None:
        """Hold one stretch's audio under its transcript — the text the
        agent later asks to re-transcribe. Over the stretch bound, or not
        a parseable WAV, it is not kept."""
        entry = _wav_entry(audio)
        if not text or entry is None or entry[1] > STRETCH_KEEP_SECS:
            return
        self._entries.pop(text, None)  # a re-heard text: newest audio, newest slot
        self._entries[text] = entry
        total = sum(e[1] for e in self._entries.values())
        while total > STORE_MAX_SECS and self._entries:
            total -= self._entries.pop(next(iter(self._entries)))[1]

    def join(self, texts: list[str]) -> bytes | None:
        """One WAV clip with the stretches behind `texts`, in order — or
        None when any text was never kept (or has aged out), or the joined
        audio passes the join bound."""
        if not texts:
            return None
        entries = []
        for text in texts:
            entry = self._entries.get(text) if text else None
            if entry is None:
                return None
            entries.append(entry)
        if sum(e[1] for e in entries) > JOIN_MAX_SECS:
            return None
        params = entries[0][2]
        if any(e[2] != params for e in entries):
            return None
        return _wrap_wav(b"".join(e[0] for e in entries), params)
