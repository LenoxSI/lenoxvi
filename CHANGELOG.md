# Changelog

All notable changes to LenoxVI are documented here.

Server-level versioning only — firmware carries its own `project(VERSION)` in
its CMakeLists, bumped together with the server manifest on every OTA deploy.
The two version streams are independent.

## [Unreleased]

### Added
- Room recorder: save one room's raw mic audio to a WAV on the appliance
  (up to 10 minutes, one recording per room, owner-only files in
  `captures/`, deleted after 7 days) through the dashboard API — see
  "Recording a room" in the README. Off unless asked for, and only with
  `LVI_DASHBOARD_TOKEN` set: without a token the recorder routes refuse, so
  no one on the LAN can record a room or download a recording.
- STT: ElevenLabs Scribe v2 (realtime) is a third provider on the STT card (`stt.provider: elevenlabs`), next to
  local Whisper and Deepgram Nova-3. It uses the ElevenLabs key the voice already uses (the Keys card shows it
  used by TTS and STT), connects only while a room is awake (on the first audio after the wake word, holding
  that audio until the session is up; it closes after 10 s without audio, stays closed when ElevenLabs hangs up
  a quiet socket, and never sends keepalive silence, which ElevenLabs would bill), ends each turn on
  LenoxVI's own voice detection (a blip too short for Scribe to accept rides along with the next words
  instead of dropping the connection), and keeps local Whisper on standby: a bad key, a dead network or an
  account refusal (out of credits, terms not accepted) moves the room to Whisper at once, and it switches back
  once ElevenLabs answers with a working session again. The language is fixed when a session starts, so changing it needs a restart.
- TTS: Kokoro (82M, Apache-2.0, CPU) is a local provider on the TTS card (`tts.provider: kokoro`), with 28 English
  voices and a speed setting (`tts.kokoro_voice`, `tts.kokoro_speed`). Optional install:
  `requirements-kokoro.txt`, then `scripts/fetch_kokoro_models.sh` downloads the model files once and checks their
  sha256; the bot never downloads at run time. One model is shared by every room, replies are spoken a
  sentence at a time (the first words come quickly, even for long replies), and Pocket works as its
  fallback. The local voices (Pocket and Kokoro) now read times, money, percentages, degrees, ordinals, years and
  numbers as words ("9:06" -> "nine oh six"); the cloud providers' text is unchanged.

### Changed
- Dashboard: the Intelligence card, the Keys card, the rooms' agent pickers and the status chip build their lane
  options, labels, help text and brand colours from `GET /api/dashboard/intelligence/lanes` (each lane's own UI
  facts), so a new lane needs no dashboard edit. A built-in copy of the three lanes renders until the bot answers,
  and stays when an older bot has no lane list.
- Intelligence lanes: each intelligence system (Hermes, OpenClaw, Nanobot) now lives in its own module under
  `core/intelligence/`, behind one `Lane` contract and one registry (`intelligence.KINDS`). The voice agent, the
  dashboard routes, the key catalog and the bot's dispatch read the registry instead of keeping their own lists of
  kinds, so a new lane is one module plus one registry line. No behaviour change: every request, header, error text
  and dashboard answer is the same as before.
- Wake name: a bare "Hey <name>." is matched by sound-alike spelling, so the
  spellings list (`wake.heard_as`, "Also heard as" / "Add a spelling") and the
  "Heard right after a wake" list are gone from the Wake name card and the
  `/wake` API; an old `heard_as` in config.yaml is ignored.
- Dashboard: one unsaved-changes bar at the bottom of the page replaces the Save buttons on the TTS, STT, Voice
  tuning, Wake name, Wake word model ("Set active"), Intelligence and room mapping cards. It lists the cards with
  edits (each name links to its card), saves them all in one go with one summary message, and Discard puts
  them back. A field counts as changed only while it differs from the saved value: moving a slider and back
  is no change, and fields you haven't touched keep following the server. The browser warns before you leave
  with unsaved changes. Keys, volumes, node, zone and model actions still apply at once.
- Dashboard: Rooms, Voice and System are tabs instead of one long page. Each tab is a link (`#rooms`, `#voice`,
  `#system`), and a link to a card (`#keys`, `#stt`) opens its tab. Restart sits in the header and the status
  chips show above every tab; a tab switch keeps unsaved edits.
- Dashboard: a node's Mute button sits next to its status, so it's reachable on a phone without scrolling the table.
- Dashboard: sentence-case table headers, Restart is no longer red, a muted node's Mute button is amber instead
  of lime, "Wake sensitivity" is now "Wake threshold", the Nodes column reads "Since", and the wake name
  button reads "Save".
- Dashboard: saves no longer ask to restart the bot. A setting that needs a restart says "Saved — restart to
  apply." and the Overview button turns into "Restart to apply (N)", listing what is waiting; one restart
  applies them all.
- Dashboard Overview: the TTS and Intelligence chips warn "no key" (or "no url") when the configured service
  can't be reached after a restart, and the nodes chip warns "N unmapped" when an online room has no speaker;
  each warning links to the card that fixes it.
- Dashboard: the empty Rooms table reads "No mic nodes yet. A flashed node appears here when it connects."

### Fixed
- Dashboard: the Rooms, Nodes, Zones and wake model tables and the wake name card say "Loading…" while
  loading and show a failed load as "Couldn't load: …", instead of claiming there is nothing there.
- Speakers: a Sonos or WiiM zone added (or a room mapped to it) after a node
  connected now gets that room's replies right away, as the dashboard says;
  before, replies stayed silent until the node reconnected.
- Speakers: moving a zone's volume slider back to the global volume now plays
  at the global volume instead of the zone's old one.
- Intelligence card: saving Nanobot settings no longer pins nanobot's
  current model; "nanobot's own model" is a choice you can pick, and saving
  it clears a previously saved model (`agent: ""`), so a model change on the
  nanobot server can't make every turn fail.
- Intelligence card and room agent picker: when the agent list is refreshed
  twice in quick succession (for example after correcting a mistyped URL), a
  late reply to the older request no longer replaces the newer list.
- Zones card: a volume slider whose save fails moves back to the saved
  volume instead of showing the unsaved one.

## [0.6.8] — 2026-10-22 — Nanobot intelligence, friendlier key setup

A third agentic intelligence behind the same rooms and speakers: nanobot, set up
entirely from the dashboard, plus a Keys card that no longer asks for
needless restarts or calls an unfinished setup a failure.

- Companion firmware (separate version stream): unchanged —
  `array-v1.2.37` / `flex-v1.2.37` / `waveshare-v1.2.37`.

### Added
- Nanobot as a third intelligence: pick Nanobot on the Intelligence card,
  enter its URL, add its key on the Keys card (`LVI_NANOBOT_KEY`), optionally
  pick its model, Save and restart. Each room keeps its own conversation on
  the nanobot server, and per-room agent overrides work as for the other
  systems. A bare `http://host:port` URL works (`/v1` is added). Nanobot
  takes no system prompt from LenoxVI, so set its spoken style (short
  plain sentences, no markdown) in the nanobot agent's own instructions.

### Changed
- Keys card: saving or removing a key that nothing uses yet (for example a
  Nanobot key while Hermes is the intelligence) no longer offers a restart
  or shows "restart to apply"; switching to that provider offers the restart.
- Keys card: testing a key before its provider is set up (no URL saved, or
  no OpenClaw agent picked) shows what to do next as a note instead of a
  red "unreachable" failure.

## [0.6.7] — 2026-10-22 — Stability, pipecat 1.13, silent unmapped rooms

A stability release: fixes across turn taking, threads and the event loop,
speaker volume, renames, config writes, key routing and the dashboard;
pipecat moves to 1.13, ElevenLabs speaks clock times cleanly, and a room
with no speaker mapping now stays silent instead of borrowing another
room's speaker.

- Companion firmware (separate version stream): `array-v1.2.37` /
  `flex-v1.2.37` / `waveshare-v1.2.37` — nodes echo each command's request id
  so a retried command never gets the previous one's late answer; 1.2.35
  accepts a 64-character hex Wi-Fi key and reports a Wi-Fi rollback.

### Upgrade notes
- Map every room to a speaker zone: an unmapped room is now silent (see
  Changed).
- Reload open dashboard tabs after updating.

### Fixed
- A Pocket voice that can't load (for example a new voice picked while the
  internet is down) no longer stalls every new room session; Pocket uses the
  default voice and tries the chosen one again every few minutes.
- The Keys card's Add box saves only under a service you picked: it no
  longer starts on a default choice that a refresh could swap under a pasted
  key, and a pick that disappears clears the pasted key.
- Restarting the bot again within 30 seconds of its last start no longer
  reports "Bot is back" before the new process is up.
- After a room mapping save, the row no longer flicks back to the old zone,
  and saving a second row right away no longer undoes the first.
- A room mapping draft that still holds a zone deleted elsewhere no longer
  makes every row's Save fail.
- With a dashboard token set, nodes on older firmware now ask for the token
  to read their mute state instead of showing "Mute?" forever.
- Double-clicking Wi-Fi Apply or Save name no longer shows a false failure
  after the change succeeded.
- The wake-word model picker no longer shows a model as picked when none of
  the listed models is active.
- A refused bot restart request is reported at once instead of after a
  90-second wait.
- The dashboard token is now typed into a masked field.
- A WiiM speaker shared by two rooms, or a reply that starts just as the
  previous one finishes, no longer leaves the music at the voice volume.
- A WiiM reply that fails to start, or is cut off by a bot restart, now
  puts the music volume back.
- Two reply clips saved in the same second can no longer play each
  other's audio.
- Starting a session no longer reads config.yaml or prunes old clips on
  the voice pipeline's event loop.
- A question still being answered when a node disconnects is dropped: its
  reply no longer arrives late, and a sticky room's saved conversation is no
  longer overwritten by it.
- After a bot restart, every node that reconnects at the same moment now
  shows as online, not just the first.
- The `intelligence.supersede_ack_gap` setting in config.yaml now takes
  effect.
- Ring updates and node commands can no longer be lost mid-send.
- Saving reply clips and sticky conversations no longer briefly pauses the
  voice pipeline.
- The API key file is now written private from the start, and saving a key
  that sits on an `export` line no longer adds a duplicate line.
- The dashboard now refuses changes sent from a page on another port of the
  same address.
- Zone names containing `/` are refused when added (such zones could never
  be managed afterwards); a refused speech-to-text save no longer
  half-applies; saving a room mapping after a node was renamed no longer
  undoes the rename.
- Retrying a node command (mute, scan, info) right after it timed out no
  longer shows the first command's late answer. Nodes on firmware 1.2.37+
  match every reply to its request by id, so the retry goes out at once;
  older nodes wait a few seconds until the late answer has landed.
- Saving settings from the dashboard no longer strips the comments in
  config.yaml (section notes, inline notes, and the note above the next
  section).
- Deleting the default speaker zone now hands "default" to a remaining zone
  (or clears it) instead of leaving it pointing at a zone that no longer
  exists.
- Restarting the bot or uploading a wake-word model from the dashboard no
  longer freezes voice in every room while it runs.
- Moving a zone volume slider, adding a zone or deleting a zone no longer
  undoes hand edits made to the `sonos` or `wiim` sections of config.yaml
  since the last dashboard save.
- Uploading firmware from the dashboard no longer briefly pauses voice in
  every room while the image is saved and checked.
- Two firmware uploads to the same line at once can no longer mix their
  bytes into the served image, or serve one upload's image under the other's
  version.
- Renaming a node to a name another room already uses is now refused,
  instead of silently taking over that room's speaker, agent and settings.
- A renamed room keeps its conversation mode (sticky/fresh) and, if sticky,
  its saved conversation.
- WiiM rooms: the ring no longer flips back to listening before the reply has
  finished, and a filler followed quickly by the answer no longer leaves the
  music at the voice volume.
- Switching the TTS provider from the dashboard before restarting no longer
  knocks rooms offline: until the restart loads the new key, reconnecting
  rooms keep the voice the bot started with.
- Turning on the Pocket fallback (or picking a new Pocket voice) from the
  dashboard no longer freezes every room while the model loads.
- Interrupting with the wake word while a reply is still loading on a Sonos
  or WiiM speaker now stops it; before, the reply played anyway and the mic
  stayed closed, so the question you interrupted with was lost.
- Wake-word detection can no longer stall when the intelligence hangs: its
  calls and wake scoring each get their own threads.
- A short breath pause no longer ends your request early with local Whisper:
  the turn now waits out the full pause window from when you stopped
  talking (streaming STT keeps its instant close).
- Fillers ("Working on it…") no longer talk over you; they wait until you
  stop, and are skipped if the answer lands first.
- Whisper's made-up captions from background noise ("Thanks for watching!")
  are dropped instead of being answered or cancelling your real question.
- A long request no longer gets cut off: the listening window stays open while
  you are still talking and times out from when you stop (capped at 30 s of
  continuous speech, so a TV can't hold it open).
- Repeating a question right after "I can't reach my intelligence" now gets an
  answer instead of silence.
- Dashboard fits a phone in portrait: no sideways page scroll at 360–430 px;
  wide tables scroll inside their cards instead of hiding columns.
- ElevenLabs speed and stability sliders wrap with their labels on narrow
  screens.
- Wi-Fi change accepts a 64-character hex key (raw PSK); needs node firmware
  1.2.35.
- A node that rolls back to its previous network after a failed Wi-Fi change
  says so in the bot log (firmware 1.2.35).
- ElevenLabs Flash no longer garbles clock times ("9:11 PM" could come out as
  babble with seconds of silence): a new TTS setting turns on ElevenLabs' own
  number normalization, which is off by default on Flash. A bare `on` in
  config.yaml works (YAML reads it as a boolean; it is mapped back).
- Intelligence Save files only the URL on screen, under the provider on
  screen: a URL typed for the other provider and never saved there can no
  longer receive that provider's key.
- Switching provider on the Intelligence card with no URL for the new
  provider now asks for one, instead of saving the old provider's URL.
- A new OpenClaw setup can be saved: Save stores the URL first, then the
  agent list loads with the saved key so an agent can be picked.
- An Intelligence URL with an invalid port (such as `:70000`) is refused
  with a clear error instead of a server error.
- Renaming a sticky room no longer splits its conversation: turns after the
  rename are saved under the new name right away, not under the old name
  until the node reconnects.
- An empty answer from the intelligence is now treated as a failed turn: you
  hear the usual apology, nothing empty is saved to the conversation, and
  asking again works.
- A question that was interrupted or whose session ended is no longer sent
  to the intelligence later if it was still waiting for a free slot.
- A node can no longer be renamed `unknown` or `browser` (names the bot uses
  internally).
- Two nodes renamed to the same name at the same moment: the second rename
  is now refused instead of leaving both nodes in one room.

### Added
- TTS card: a normalization picker beside the ElevenLabs Flash speed and
  stability sliders (Auto / On / Off), saved
  as `tts.elevenlabs_text_normalization`. On spells out numbers, times and
  dates before speaking and adds about 0.15 s before the first word.

### Changed
- **A room with no speaker mapping is silent.** Its replies no longer play on
  the Sonos or WiiM default zone, and nothing is ducked for it; the bot logs
  one line per reply until the room is mapped. `default_zone` is still
  accepted in config.yaml but no longer routes replies. A room mapped to a
  node's built-in speaker plays once that node confirms its speaker (a few
  seconds after it connects); a speaker node with no mapping still uses its
  own speaker.
- Intelligence Save sends only the URL for the provider on screen (the
  per-provider `urls` map is no longer accepted in the request). Reload any
  dashboard tab left open from an older version before saving.
- Spoken replies are no longer stripped of `[bracketed]` text. The strip was a
  workaround for one agent's persona; the voice now speaks exactly what the
  agent writes.
- pipecat 1.11.0 -> 1.13.0 (pipecat-ai-prebuilt 1.1.0 -> 1.3.0): local Whisper
  decodes off the event loop, the ElevenLabs websocket keepalive no longer
  stops after one failure, a mid-turn TTS reconnect keeps its voice settings,
  and Silero VAD v6.2 (speech-stop events can land slightly later).
- The TTS revive (one bounded websocket retry when a dead voice service gets
  its next sentence) moved to pipecat 1.13's single synthesis path; on 1.13
  the old hook was never called, so a dead voice would have stayed silent.
- SmallWebRTC answers keep aiortc's default bundle policy: pipecat 1.13's
  max-bundle answers carried no ICE candidates, so the ESP32 nodes (no trickle
  ICE) could not connect. A new offline handshake test answers a node-shaped
  offer and requires candidates, a real address and short lines.

## [0.6.6] — 2026-10-20 — LenoxVI, a Keys card, cloud speech-to-text, per-room agents

The project's new name, one place for every API key, a cloud speech-to-text
option with local Whisper as its safety net, a different agent per room,
restarts that take seconds instead of a minute, and a dashboard whose status
reads at a glance.

- Companion firmware (separate version stream): `array-v1.2.34` /
  `flex-v1.2.34` / `waveshare-v1.2.34` — nodes reconnect as soon as the bot
  closes their connection on a stop, so a bot restart costs ~9 s of mic time
  instead of ~1 minute.

### Added
- **Keys card (System).** Every API key in one place: ElevenLabs,
  Deepgram, Hermes, OpenClaw and each Hermes agent key. Each row shows
  what uses the key, whether it's set (first 4 characters only),
  missing, or saved and waiting for a restart, with Replace and Test
  (checks the saved key with its provider before you restart). Rows show
  only the keys you have (plus any key in use but missing); one Add row
  picks a service or a Hermes agent without a key, saves, then tests it.
  Remove deletes a key after a confirmation that says what will stop
  working. Replacing or removing a key keeps a private backup of the old
  `.env` (newest 3). Keys are never shown. The TTS, STT and Intelligence
  cards no longer take keys; they link to the Keys card when one is
  missing.
- **Per-room agent.** Room -> zone mapping gets an Agent column: each room
  can talk to a different agent than the Intelligence card's default,
  per intelligence system. A room without a pick follows the default.
- **Cloud speech-to-text (Deepgram Nova-3).** The STT card gets a
  Provider choice: *Local — Whisper* (private, the default) or *Deepgram
  Nova-3 (cloud)* for top transcription quality on modest hardware. On
  Deepgram, your Whisper model becomes the **fallback**: if Deepgram
  rejects the key or can't be reached, the session switches to local
  Whisper on its own (under a second for a bad key, about 8 s for a dead
  network) and switches back when Deepgram answers again. The card shows
  which one is listening, with an amber "On Whisper fallback" chip and
  the reason. The Deepgram key is shared with Deepgram TTS (enter it on
  either card). Language *Auto-detect* uses Nova-3's multilingual mode.
  The wake word always stays local.
- Dashboard: **STT card** (Voice, under TTS). Pick one of four benchmarked
  faster-whisper models — Turbo (default), Medium, Small, Base — and a
  language (Auto-detect + 16 common ones). Device and compute type are
  written with the model (GPU → `cuda`/`float16`, no GPU → `cpu`/`int8`);
  Turbo and Medium need a GPU. A model that isn't downloaded yet is fetched
  first and config switches only once it's on disk, so a restart never
  downloads. Language changes apply to the next utterance, no restart.
  Hand-set values outside the lists show as "(set in config)" and are kept.
- Status chips across the top of the card: load state, model, GPU/CPU,
  compute type, and per-command speech-to-text time (median of the last 10
  since restart) — a live A/B meter when trying models.
- Download progress: a bar under the chips plus "43%" and "650 MB / 1.5 GB",
  from the bytes Hugging Face has written to disk (faster-whisper's own
  downloader hides progress, so the card downloads the same files itself).
  The card checks every 0.5 s while downloading, and a finished download
  fills the bar to 100% "Downloaded ✓" for a moment before the restart
  prompt, so even a few-second download visibly completes.
- Model table on the STT card (under the Model / Language row): the four
  models with their size on disk and a Delete button for downloaded models
  that aren't configured, loaded or downloading (removed via the Hugging
  Face cache API; a deleted model downloads again if picked). The "in use"
  flags refresh when a restart loads a different model.
- `GET`/`PUT /api/dashboard/stt`, `DELETE /api/dashboard/stt/model/{name}`;
  `/api/dashboard/status` gains an `stt` block.

### Changed
- **Renamed to LenoxVI.** Name, docs, dashboard logo and page title follow
  the LenoxVI brand kit (colours unchanged). Environment variables use the
  `LVI_` prefix (e.g. `LVI_HERMES_KEY`), the systemd user unit is
  `lenoxvi-bot` (`deploy/lenoxvi-bot.service.example`), and the dev
  server reads `LENOXVI_DEV_BOT_URL`. The firmware repo is
  `lenoxvi-firmware`.
- Dashboard: the Nodes table no longer shows a `speaker` chip (the built-in
  speaker lives in Zones and the mapping dropdown); the waveshare line chip's
  tooltip reads "Waveshare esp32-s3-audio board (built-in speaker option)".
- Dashboard: a node's built-in speaker row in Zones shows an `ESP32` backend
  chip (was `node`) and no "Mic pauses while it speaks." note; the
  "Not in use — replies go to …" note stays.
- **Status bar as chips.** The Overview status bar reads uptime ·
  intelligence · nodes online · last wake · version · stt · tts, each a chip.
  Nodes online is green when every node is up, amber when some are down, red
  when none are. The STT and TTS chips name the engine the rooms are actually
  using, amber on a fallback and red when it's down. The Nodes table shows
  online/offline as chips too. Every chip follows the brand's Chip rule
  (lowercase words, soft status tones).
- **Wake name card.** "Heard right after a wake" is plain text (spellings are
  added in the box), and the add-a-spelling box sits beside the wake name.
- Room → zone mapping and Wake name drop their explanatory notes; the Push
  firmware button sits at the far right of its row.
- **`deploy_firmware.sh` no longer restarts the bot.** The firmware server
  reads the track directory on every request, so a restart only dropped every
  room. The script stages the image and tells you to restart that line's
  node(s) from the dashboard, which is when a node picks it up.

### Fixed
- **Restarts no longer hang.** Stopping the bot waited for every node's
  session to end on its own, which never happens, until the service
  manager killed it 10 s later, so nodes never heard the bot leave and
  only noticed when their keepalive timed out. A stop now takes ~2 s and
  closes every node connection on the way out (nodes on firmware that
  listens for it reconnect right away).
- **Turns no longer split on a streaming STT.** With Deepgram, words
  finalized mid-sentence could close the turn while you were still
  talking, and the rest of the sentence was glued onto your next one. A
  turn now closes only after you stop speaking, and Deepgram's final
  transcript closes it immediately (faster than the old fixed pause).
  Local Whisper behaves exactly as before.
- Config writes are serialised behind one lock, so a write from a worker
  thread can't garble `config.yaml` or lose a section.
- The Keys card shows a **Missing** row for an agent in use (the configured
  default or a room's override) whose key isn't in `.env`; before, that case only
  showed up as a "No key" link elsewhere.
- The dashboard no longer allowlists two root files the build stopped
  shipping.

## [0.6.5] — 2026-10-18 — Built-in speaker zone, Pocket TTS, wake name from config

A third firmware line with a node that can answer from its own speaker, a
local text-to-speech engine that also stands in when the cloud voice is
down, and a wake name that comes from config instead of the code.

- Companion firmware (separate version stream): `array-v1.2.29` /
  `flex-v1.2.29` — audio-loop health counters and a reboot black box in
  `get_info`. `waveshare-v1.2.33` — first releases of the Waveshare
  ESP32-S3-AUDIO line (mic, LED ring, built-in speaker).

### Added
- **Built-in speaker as a reply zone.** A node whose board has a speaker
  (the `waveshare` line) can answer from it. Its room's dropdown offers
  "Built-in speaker" (stored as the reserved zone value `builtin`); only that
  node can use it, and the server refuses it for any other room. An unmapped
  speaker node replies on its own speaker by default, resolved at run time
  without writing config. Exactly one output sounds each reply: the bot tells
  the node whether to play (`set_speaker {enabled, volume}`) after it
  connects and whenever its mapping or volume changes, and Sonos/WiiM skip a
  `builtin` reply. A node counts as a speaker only after it confirms its
  speaker works in that session, so older firmware or a failed speaker keeps
  replies on the shared zones. Volume lives in `node_speaker.volumes`
  (default 40); the Zones table shows a `<room> · built-in` row with a
  volume slider, a test clip and offline / speaker-fault chips, and the
  Nodes table a `speaker` badge.
- **Third firmware line `waveshare`.** Upload, OTA track
  (`firmware/waveshare/`, `/firmware/waveshare/*`), line chip and
  `deploy_firmware.sh --track waveshare`, with the same cross-line refusal
  as `array` and `flex`.
- **Pocket TTS.** An optional local text-to-speech provider (`provider:
  pocket`, CPU) and a local hot standby behind a cloud provider
  (`tts.fallback: pocket`): when the cloud voice can't connect, replies
  switch to Pocket and switch back once it recovers. The dashboard offers the
  provider, its voice list, the fallback and its voice, and shows a fallback
  chip while it is in use. Install with `requirements-pocket.txt`.
- **Wake name card.** The name the bot answers to (`wake.name`) and the
  other spellings speech-to-text writes for it (`wake.heard_as`) are set
  from the dashboard and apply live. The last two transcripts heard after a
  wake are listed with one-click "add as spelling"; filler words (stop, yes,
  no, cancel, hey, ok) are never accepted as spellings. `GET/PUT
  /api/dashboard/wake`.
- **Stock openWakeWord models** (`hey_jarvis`, `hey_mycroft`, `alexa`) are
  selectable in the wake-model panel and can't be deleted; their licence is
  shown. The public default is Hey Jarvis (CC BY-NC-SA 4.0, non-commercial).
- **Intelligence agent picker.** Agents are discovered per provider, with
  masked hints for which key each one uses and a per-profile key save.

### Fixed
- The TTS fallback fires when the primary can't connect at all, and only
  when the primary has no socket, so a brief blip on a live connection never
  switches voices.
- Adding a WiiM zone no longer shows the Sonos player-UID field.

### Changed
- The wake name comes only from `wake.name` — never from the model file
  name — and nothing in the code hardcodes a name. Unset means no name: the
  wake-phrase-only drop filter then works from `wake.heard_as` alone.
- `deploy_firmware.sh` refuses to publish an image built with a bench-only
  debug option (mic-level log, data-channel logging).
- Dashboard: TTS and tuning commit buttons read "Save"; "Test saved config"
  sits bottom-left; API key Save buttons sit beside their key boxes; the
  spelling box and chips sit under the wake name.
- `scripts/` holds operations tools only; the automated suite lives in
  `tests/` and live hardware probes in `tests/manual/`.

### Removed
- The Claude and custom OpenAI-compatible intelligence placeholders.

### Upgrading
- Set `wake.name` (and any `wake.heard_as` spellings) to match your wake
  word — earlier versions had a name built in. See `config.example.yaml`.
- Pocket TTS is optional: `pip install -r requirements-pocket.txt`, then
  pick it in the TTS panel.
- `.env.example` now lists `LVI_OPENCLAW_KEY` next to `LVI_HERMES_KEY`.

## [0.6.4] — 2026-10-17 — Node-reported mute, board line, safer firmware pipeline

The server side of the firmware 1.2.9 → 1.2.26 cleanup series. Nodes now
report their own mute state and hardware line, the dashboard follows them,
a TTS service that dies mid-session revives itself, and the firmware
pipeline names each line once and builds every image from the committed
configuration.

- Companion firmware (separate version stream): `array-v1.2.26` /
  `flex-v1.2.26` — mute that survives reboots, array mute button and
  dashboard as one control, line reporting, escaped command replies,
  verified Wi-Fi changes with automatic fallback, hardware watchdogs.

### Added
- **Node-reported mute state.** Nodes on firmware 1.2.11+ include `muted` in
  their connect message and push a `mute_push` event when the physical mute
  button on an array node is pressed. `/status` carries `rooms[].muted`, and
  the dashboard's Mute button follows the node's reported state instead of
  asking for it. The mute route accepts array nodes (firmware 1.2.11+) and
  updates the room from the node's acknowledgement.
- **Board line chip.** Nodes on firmware 1.2.19+ report their line (`array`
  or `flex`); `/status` carries `rooms[].line` and the Nodes table shows it
  as a chip next to the firmware version. Only the two known lines are
  accepted; older firmware simply shows no chip.
- **Per-room TTS health.** `/status` rooms carry `tts_usable` and
  `tts_provider`, and the Nodes table shows a "tts down" chip when a node
  can hear but its TTS service can no longer speak.

### Fixed
- **A dead websocket TTS service revives on the next reply.** pipecat marks
  a websocket TTS service permanently unusable after three failed
  reconnects, which left a room silent until a restart. The next speak now
  makes one bounded reconnect attempt (cooldown `tts.revive_cooldown_secs`,
  default 30); a failed attempt never breaks the speak path.
- **Repeated questions are no longer mistaken for echo.** Echo containment
  now requires nearly all transcript words to come from something the
  assistant said (was 60%), and replies sent to a dead TTS service no longer
  arm echo detection at all.
- Dashboard: a duplicate in-flight node command is a 409 Conflict instead of
  a bare 504; timeouts and half-open sessions say what happened and that the
  node self-heals within about 10 minutes.
- Dashboard: Restart bot can no longer start two restarts at once; a mapping
  row's Save keeps other rows' unsaved picks; the wake-model picker activates
  exactly the row clicked when two files share a name; the volume slider no
  longer saves on Tab; untouched voice sliders no longer overwrite configured
  values; a stale mute reply can no longer overwrite a newer toggle; the
  Intelligence chip no longer shows brand colors while loading, and its test
  button is labeled "Test saved config".
- Wi-Fi changes: the dashboard message matches firmware 1.2.23+, which
  reboots onto the new network and returns to the previous one by itself
  if it can't reach the bot there.

### Changed
- **One name per firmware line, everywhere: `array` and `flex`.** The array
  line's OTA track was called `node`; it is now `array` in the server
  directories (`firmware/array/`), the upload API (`track: "array"`), the
  dashboard track picker and `deploy_firmware.sh`. Served images are named
  for their line (`array_esp32.bin`, `flex_esp32.bin`); an upload or deploy
  removes any other `.bin` left in the track directory.
- `deploy_firmware.sh --track array|flex` is now **required** — there is no
  default line.
- `deploy_firmware.sh` builds both lines from the firmware repo's `main`
  branch (the firmware's per-line branches were folded into one source tree),
  passing `-DBOARD=<track>` and using one build directory per line
  (`build/array`, `build/flex`). Any other checked-out branch is refused.
- Board identity reads the image's project name as well as its OTA URL:
  firmware 1.2.9+ images are `array_esp32` / `flex_esp32` and must agree with
  their own URL; older `node_esp32` images are still accepted, judged by URL.

- `deploy_firmware.sh` builds each line from a fresh configuration generated
  from the firmware's `sdkconfig.defaults` (kept in `build/<line>/sdkconfig`).
  A previously generated `sdkconfig` in the firmware checkout could silently
  override a change to the defaults.

### Removed
- The manifest-only `POST /api/dashboard/firmware/version` route. The
  board-checked firmware upload is now the only way to publish an image.
- The legacy `core/dashboard/page.html` fallback page (out of date with the
  current API). A missing `dist/` now returns a 404 that names the build step.

### Upgrading
- On the bot host, move the array track directory before restarting:
  `mv firmware/node firmware/array`. The old `/firmware/version` and
  `/firmware/<file>` URLs keep serving the array track permanently, so array
  nodes on firmware 1.2.8 or older keep updating.
- Upload API callers: `track: "node"` is now a 400 — send `"array"`.

## [0.6.3] — 2026-10-15 — Session leak, firmware safety, Whisper at startup

Hardening release — the server side of the firmware 1.2.8 hardening work.
The bot no longer grows by ~90 MB and 4 threads per WebRTC session;
firmware publishing can no longer cross-flash a node, leak the WiFi
password into an image or into serial logs; Whisper loads at boot instead
of inside the first question. Every fix ships with suite checks that fail
against the pre-fix code; suite 188 → 202 checks. Verified on both node
types after each change.

### Upgrading from 0.6.2
- **`deploy_firmware.sh` no longer takes WiFi credentials.** `WIFI_SSID` /
  `WIFI_PASSWORD` are ignored (with a note) and the build verifies both are
  empty before publishing — OTA images are served over plain HTTP to the
  whole LAN. Nodes read WiFi from NVS (dashboard WiFi flow). **Before
  deploying a cred-free image, every node on that track must already have
  WiFi in NVS** — a node running any cred-free image and online has proven
  it. A brand-new board needs one USB flash with credentials first (the
  firmware has no provisioning AP).
- **`deploy_firmware.sh` checks the firmware checkout first:** track `node`
  builds only from `main`, track `flex` only from `flex-xvf3800`; detached
  HEAD, uncommitted changes to tracked files or a hand-edited
  `deps/libpeer` refuse. Switch branches with the `git checkout` it prints.
- **Firmware uploads must match their board.** `POST /firmware/upload`
  reads the image's own OTA track (its compiled update URL) and refuses an
  image declared for the other track — or one it can't identify — with a
  400 that names the right track.
- **Bot startup takes ~2 s longer** and holds Whisper (~0.6 GB RAM, ~2.2 GB
  VRAM) from boot instead of from the first voice turn.
- Companion firmware (separate version stream): array `v1.2.8` / Flex
  `flex-v1.2.8` — OTA rollback, data-channel handshake fix (20/20 first-try
  connects), OTA download retry, 100 KB of internal RAM back. Recommended,
  not required by this release.

### Fixed
- **Per-session memory/thread leak**: every ended WebRTC
  session's pipeline was a reference cycle that Python 3.14.4's incremental
  GC never reclaimed (~110 sessions → 10.4 GB RSS / 406 threads). The bot
  now reclaims each ended session deterministically (~120 ms, logged), and
  the local STT closes the OpenAI client its cloud base class creates per
  session. 20-restart soak: threads and RSS flat.
- **First voice turn could grow the bot by ~2 GB:** Whisper loaded lazily
  inside the first transcription, and a long-running fragmented heap kept
  the ~1.2 GB load spike. It now loads + warms at startup (fresh heap,
  `malloc_trim`), which also removes ~2.5 s from the first wake word of
  the day.
- **`deploy_firmware.sh` builds logged every data-channel message to
  serial — including `set_wifi` passwords** (it exported
  `LOG_DATACHANNEL_MESSAGES` empty, which the firmware CMake treated as
  on). The script now unsets it; the firmware side requires `=1`.
- **Cross-flash guards:** a wrong firmware checkout (e.g. on
  `flex-xvf3800` with the default `node` track) would have published the
  other board's firmware; the upload route accepted a Flex image on the
  array track. Both refuse now (see Upgrading). `scripts/test_deploy_guard.sh`
  proves the checkout guard against a throwaway clone.
- WiFi credentials are no longer baked into OTA images (see Upgrading).

### Known issue
- A talking room (e.g. a TV) can keep the gate awake after one wake: room
  speech extends the follow-up window and every reply re-arms it.
  Multi-turn conversation is deliberate and stays uncapped; the planned fix
  is on the TV side (auto-mute/duck on wake).

## [0.6.2] — 2026-10-13 — Dashboard hardening + one provider, one variable

Dashboard hardening: two previously unreported bugs, the "one provider,
one variable" naming rule, and a batch of small fixes. Every fix ships with
suite checks that fail against the pre-fix code; suite 162 → 188 checks.

### Upgrading from 0.6.1
- **Config section `brain:` is now `intelligence:`** — rename it in
  `config.yaml` (no fallback reads the old name). Env overrides follow:
  `LVI_INTELLIGENCE_KIND` / `LVI_INTELLIGENCE_AGENT` / `LVI_INTELLIGENCE_MAX_TOKENS`.
- **`LVI_BRAIN_KEY` is gone.** Each lane reads exactly one variable:
  Hermes → `LVI_HERMES_KEY`, OpenClaw → `LVI_OPENCLAW_KEY`. No cross-kind
  fallback — a missing key is one clear error line, never a wrong-token 401.
- **API contract tightened** (scripts calling the dashboard API):
  `PUT /intelligence/key` requires `kind`; `POST /firmware/upload`
  requires `track` (no silent array default); every write payload
  rejects unknown fields (422); `POST /nodes/{room}/mute` requires an
  explicit `mute`; `/mapping` returns Sonos display names only with
  `?names=1`.

### Added
- **Voice catalog from the server** — `GET /tts` returns `catalog`
  (2 ElevenLabs models, 40 Aura, 36 Flux); the dashboard's pick-lists
  are built from it. All 76 Deepgram voices are now pickable (the UI
  listed only 28 — 48 were unreachable).
- **Per-kind Intelligence URL memory** — switching provider swaps in
  that lane's remembered URL; the OpenClaw lane can't be pointed at the
  Hermes api_server by accident.
- **Unmapped nodes are mappable** — the mapping table lists every
  connected node with an "unmapped" chip, so a fresh install (or a
  third node) can be mapped from the dashboard.
- **Dashboard token prompt** — with `LVI_DASHBOARD_TOKEN` set, a save
  prompts once, remembers the token, and retries (previously every save
  just failed).
- **Firmware upload safety** — no default track; confirm names the
  line and warns when the filename hints at the other one; after a push,
  pick which nodes restart (nothing pre-checked).
- `deploy_firmware.sh --track flex`, plus a version-from-image gate that
  refuses to ship on a manifest/binary mismatch.
- `effective_volumes` on `/mapping` — what each zone actually plays at.

### Fixed — dashboard
- Intelligence key save follows the provider on screen, not the one in
  config (the UI could write the OpenClaw token into `LVI_HERMES_KEY`).
- Adding a WiiM zone 422'd every time (`player_uid` was required for all
  backends) — now Sonos-only.
- Mute button after a node reboot: re-reads the node's state per
  connection; unknown state shows "Mute?" and a click mutes.
- Restart / Wi-Fi routes returned 200 when the node said no — now 502;
  the password is never echoed.
- Misspelled fields silently no-op'd (a typo'd tuning key rewrote
  `wake.threshold`); an empty mute body muted the node.
- Deepgram TTS first save 422'd (the voice menu held the ElevenLabs model).
- Tuning saves sent all six knobs and reverted edits made elsewhere —
  now only touched knobs.
- Polling: one refresh cycle at a time, hidden tabs skip, and the 5 s
  poll no longer runs Sonos lookups (it was probing the WiiM too).
- One volume default (35) for both output backends and the dashboard —
  Sonos defaulted to 70 when config omitted `clip_volume`; WiiM sliders
  showed the Sonos global.
- `fw=None` in the Nodes table (fw_push / offer-tee race); mute errors
  and tooltip tell the per-line truth (array = physical button).

### Fixed — security / robustness
- **Form-upload 422s returned 500** and **key-route 422s logged the full
  API key to the journal** — pipecat's app-wide validation handler read
  the request body and logged rejected values. Dashboard routes now
  answer their own 422s (no body read, rejected values stripped).
- Wake barge-in interrupt never ran (async callback was never awaited).
- Small fixes: config anchored to the repo root (cwd-proof), clip
  server can't half-bind, TTS secrets checked at construction, zone
  delete is one atomic multi-section write.
- `firmware/flex/` gitignored (flex images bake fallback Wi-Fi creds).

## [0.6.1] — 2026-10-07 — Software mute + ElevenLabs voice shaping

Node mute becomes a first-class control and the flash-v2.5 voice gets
tunable character.

### Added — software mic mute + per-model OTA
- Dashboard **Mute button** (Nodes card, per row): zeroes the mic at
  the node — same effect as the array board's physical button. The
  flex board has no physical mute, so this is software: flex fw 1.1.0
  adds the `set_mute` command (toggle or explicit) and reports
  `muted` in `get_info`; the mic is silenced in the same place the
  turn lock silences it. Button state is confirmed from the node's
  ack, never optimistic, and re-derived from the node on every page
  load (new `GET /nodes/{room}/info`) — a refresh can't lie about a
  muted mic. Online-gated; firmware 1.1.0+.
- **Per-model OTA tracks**: `/firmware/{track}/
  version` + `/firmware/{track}/{bin}` routes; the flex node reads
  `/firmware/flex/version`, the array node's legacy URLs stay
  byte-identical, and a track without a deployed manifest 404s = no
  update (safe). Flex firmware 1.1.0 is the first over-the-air update on
  the new track — no cable needed.
- **ElevenLabs flash-v2.5 speed + stability**: config keys
  `tts.elevenlabs_speed` (0.7–1.2) and `tts.elevenlabs_stability`
  (0.0–1.0) with dashboard sliders (draft law — synced once, local
  until saved). Absent config = API defaults, never a made-up value.
  The v3/v4 dialogue lane deliberately gets no knobs (ElevenLabs
  guidance: the model drives expressivity there).

### Fixed — dashboard
- Discover's "Fill into add form" silently dropped the Sonos UID —
  it read `d.player_uid` but the route returns `uid`. Now fills the
  complete zone (name, backend, IP, RINCON) in one click.
- Mute toggle double-fire race — rapid clicks during ack latency
  could flip the node twice; dedicated `muteBusy` flag + ask-once
  ref guard on the mount-time info fetch.

## [0.6.0] — 2026-10-07 — HeroUI dashboard + second node type (Flex)

The dashboard becomes a React app, and LenoxVI gets its second mic node
type.

### Added — HeroUI dashboard (replaces legacy page.html)
- React + Vite + Tailwind v4 + HeroUI v3 single-page dashboard
  (`dashboard-ui/`), built on the LenoxVI design kit; prebuilt `dist/`
  committed so a fresh clone needs zero node toolchain. Wireframe-C
  single page: Overview, Rooms, Nodes, Zones, Voice (TTS + tuning),
  Wake word model, Intelligence, Firmware.
- Common message strip — every section's status/error/success renders
  in one header area with explicit owner labels (no more int-msg debug
  output in the strip).
- Node row actions (inline forms, no `prompt()`): Restart (confirm-
  gated), Rename (pattern-validated), Wi-Fi (scan → RSSI-sorted SSID
  list → password → apply; auto-rollback on bad password). All
  online-gated; firmware 1.1.0+ required.
- Firmware upload panel ported (multipart POST, version read from the
  image's ESP app descriptor, atomic manifest write, optional
  per-node restart nudge) — the last legacy-page feature.
- Zones: add-zone form with Sonos/WiiM backend pick, Discover Sonos
  on LAN, per-zone volume sliders (live override — next clip, no
  restart), per-row test + remove.
- Statusbar duplicate-reconnect fix: last-good facts are kept during
  restart countdown; NET chatter no longer duplicates the line.
- Dashboard suite pins the new contracts (mapping-wipe guards
  included).

### Fixed — dashboard correctness (audit + live finds)
- Mapping wipe: the React save sent a BARE merged dict (no
  `room_zones` wrapper) — Pydantic's `default_factory` silently
  turned it into `{}` and the route wiped all mappings; three real
  wipes hit production config (2 user saves, 1 test harness). Fix at
  three layers: UI wraps the body; `MappingPayload` is
  `extra="forbid"` (unwrapped = loud 422); the route refuses an
  explicit empty `room_zones` (400). Suite pins both guards.
- Room mappings are LIVE — the bot reads `room_zones` per reply and
  the dashboard write clears the config cache in-process; the
  "applies on next bot restart" message was stale copy. UI now says
  the truth.
- Kind-aware intelligence key resolution: the hermes lane auths with
  `LVI_HERMES_KEY`, the openclaw lane with `LVI_BRAIN_KEY` — a kind
  flip can no longer cross tokens (suite checks pin both lanes). Same fix in the intelligence probe.
- Intelligence test button renders the body's `ok` field, never the
  HTTP status (the route answers 200 with `{ok: false}` on failure —
  caught live when the strip said "✓ ok" while the backend logged
  401).
- Supersede ack rate-limited: one "Hold on — switching." per wake
  window (`intelligence.supersede_ack_gap`, default 45s, re-armed on fresh
  turns) — a two-person math session triple-fired it in 10s.
- TTS provider switch sent a stale elevenlabs model string under
  deepgram (the Pydantic pattern correctly 422'd it) — provider-
  aware body restored; Flux speed/expressivity UI restored.

### Added — second node type: reSpeaker Flex
- Second mic node type supported end to end: wake, Whisper
  transcription, intelligence reply, zone playback — multi-turn with
  conversation memory.
- New firmware line `flex-xvf3800` (firmware repo `lenoxvi-firmware`,
  tagged `v1.0.0`): inthost XMOS image architecture — the XVF3800 is
  the I2S clock master (formatBCE inthost v1.0.7), ESP32 on the
  proven slave config. Stock Flex images + XIAO-as-master falsified
  at 48k (no APLL on S3) and 16k (stable bus, garbage data).
- Per-model OTA track: flex builds address `/firmware/flex/version`
  (404 today = no update, safe); unset track stays byte-identical to
  the legacy URL — cross-flashing is impossible.
- Dashboard Firmware column self-identifies the node model (1.0.0 =
  flex, 1.1.3 = array node).

### Docs
- README full rewrite: dashboard surface, WiiM backend, turn-taking,
  Intelligence lanes.

## [0.5.0] — 2026-10-05 — Intelligence + turn-taking hardening

Three feature arcs: a second intelligence provider, turn-taking, and
hardening.

### Added — Intelligence lane
- `intelligence.kind: openclaw` — voice turns answered by an OpenClaw Gateway
  agent (`model: openclaw/<agent>`, sticky rooms via
  `x-openclaw-session-key`, Hermes-only `conversation` field omitted).
  Requires `gateway.http.endpoints.chatCompletions.enabled: true` on the
  OpenClaw side (off by default, 2026.9.8).
- Key resolution `LVI_BRAIN_KEY` → `LVI_HERMES_KEY` fallback; a missing key
  no longer bricks construction — the turn fails
  with a clear message and the dashboard reports it.
- Dashboard "Intelligence" section (Settings): provider picker
  (Hermes / OpenClaw; Claude + custom OpenAI-compatible designed-for),
  per-kind fields, key set ✓, Test-connection button (health + one
  minimal completion, latency), system info line.
- Reasoning-preamble meter: OpenClaw/GLM planning text can merge into
  `content` (seen in testing) — logged with a counter, never
  silently stripped.

### Added — Turn-taking
- Epoch turns: wake-word speech runs as a background task with
  generation epochs; a new wake **supersedes** — old reply discarded at
  every await, no history write, spoken ack "Hold on — switching."
- Wake-word barge-in: 2 consecutive OWW frames over threshold interrupt
  the sounding clip (`wake.barge_in`, default false) — both
  backends `stop_current_clip()` + `_speech_epoch` bump.
- Load-anchored timers: speech/duck timers anchor at clip-load
  with epoch guard + generation-time fallback — the ring LED no longer
  desyncs from the speaker on long intelligence calls.
- Echo-gate relax: confirmed wake or barge-in relaxes the duration gate
  to a ~0.5s tail; text containment stays armed.

### Fixed
- Mic could go deaf: `process_frame`'s final passthrough was lost during
  the turn-taking rework; regression check added.
- `kind: openclaw` silently fell back to the echo stub — bot.py dispatch
  only recognized `hermes`; regression check added (construction
  dispatch AST).
- Dashboard 422s render as `[object Object]` — Pydantic detail arrays now
  stringify with fields; first hit: empty `agent` on Intelligence save.
- API URL hidden for Hermes — provider-agnostic plumbing now shows for
  every kind (any deployment's Hermes lives at its own URL).

## [0.4.0] — 2026-10-04

The config-editable dashboard release. The dashboard stops being
read-only-plus-sliders and becomes the primary way to operate LenoxVI:
TTS provider/voice/model, Flux's speed + expressivity, and the six
pipecat ear-tuning knobs all live in the GUI now, with a restart button
and a save→apply flow that never forces a tab hop.

### Added
- **TTS settings panel** — provider, voice and model configurable from
  the dashboard; ElevenLabs voice ID + model (eleven_flash_v2_5 / v2),
  keys stored in `.env` via a dedicated key route (set/unset status
  only — never echoed back, never in config.yaml), settings in
  config.yaml
- **Provider dropdown with three backends** — ElevenLabs, Deepgram
  Aura-2 (40 cloud voices, model IS the voice), Deepgram Flux
  (voice-agent-first, voices incl. rufus/hannah/bree); provider-aware
  routes with 422 on unknown voices; per-clip sample-rate reset on
  Sonos (24k↔16k chipmunk prevention)
- **Flux speed + expressivity knobs** — sliders (0.5–1.5× in 0.05
  steps; expressivity -2..2), server-side range validation, partial
  updates preserve tuned values
- **Voice Tuning panel** — the six ear-tuning knobs as sliders: wake
  sensitivity (0.30–0.60), awake timeout (5–60s), VAD confidence
  (0.30–0.90), VAD min volume (0.10–0.90), turn-end lull (0.2–2.0s),
  echo dedup window (2–30s). Writes MERGE per section — intelligence.fillers,
  max_tokens, wake.enabled can never be clobbered by a tuning save
- **Restart bot button** — POST /api/dashboard/restart via a detached
  systemd transient unit (survives the bot's own death); UI polls
  uptime reset, auto-reloads panels; honest error surfaces (no silent
  catch)
- **Post-save "restart now?" popup** — every Voice-tab save offers a
  one-click apply; cancel keeps apply-later
- **Three-tab dashboard** — Zones (mapping/nodes/zones) / Voice (TTS
  + tuning) / Settings (system info, bot control, firmware)
- **System info bar** atop Settings — Bot Version, TTS provider, STT
  model, OWW model, all resolved through the same config > env >
  default chains the consumers use (moves with the host, never static)
- **Live version in statusbar** — git describe at boot, updates every
  commit
- **intelligence.max_tokens: 300** — guardrail against runaway intelligence replies
  (config-gated, default 300)

### Fixed
- Key-save form natively submitted — page reloaded to Zones mid-save,
  masking the 200 OK
- Restart handler silently swallowed fetch errors — now retries once
  and surfaces real failures
- Statusbar selector lost its `#` — dumped raw template literal into
  the header

## [0.3.5] — 2026-10-04

The second-output-backend release. The WiiM Pro arrives and becomes a
first-class voice lane in one afternoon — plus the session-visibility fix
that makes every voice conversation auditable in the Hermes dashboard.

### Added
- **WiiM output backend** (`core/wiim_output.py`) — interrupt-no-restore
  reply playback on WiiM/LinkPlay streamers, ratified as the affordable
  dedicated AI-out lane (Sonos stays the full-integration lane). Same
  FrameProcessor contract as SonosOutput (speaking LEDs, echo gate,
  wake-gate speech hints), no duck holder — the interrupt IS the duck.
  Volume snapshot → set → play → restore around every clip. Note:
  `playPromptUrl` is a STUB on
  current firmware (Linkplay 4.8.827634) — acks OK, never fetches — so
  replies play via `setPlayerCmd:play` as main media. Music stops when
  the assistant speaks, nothing restores (client-owned sources are
  unresumable).
  `X-Hermes-Session-Key` rides every intelligence call: sticky rooms merge into
  ONE long-lived dashboard session per room, fresh pipelines get one
  session per connection — both visible and auditable in the Hermes
  admin dash (previously voice sessions were unkeyed and invisible there)
- **Per-zone backend dispatch** — a room mapped to a `wiim:` zone gets
  WiiMOutput, `sonos:` zones keep the audioClip overlay path. One merged
  zone namespace in the dashboard with backend tags; Add-zone takes a
  backend selector (WiiM zones need no player_uid); delete / volume
  slider / test tone all dispatch on backend. Adding a zone to a backend
  implies enabling that backend

### Fixed
- **WiiM hardware-fade compensation** — the Pro ramps volume in over the
  first ~1s of main-media playback and ramps out at the end; short
  replies ramped down before finishing. No firmware knob exists, so
  reply clips carry 0.25s
  lead-in silence + 0.35s tail. Sonos lane untouched
- **Voice sessions invisible in the Hermes dashboard** — see
  X-Hermes-Session-Key above
- Three WiiM-backend boot bugs from the first deploy: hallucinated
  `cfg_env` API (killed every pipeline construction — mic wouldn't wake),
  missing `enabled: true` on dashboard-added wiim zones (silent disabled
  backend), and the shared clip server never started when no SonosOutput
  existed in the pipeline (LEDs cycled, zero audio). Deploy gate added:
  an import smoke test before every restart that touches core/

### Changed
- **Dashboard zone UX** — native-name prefill from discovery (no more
  lowercase mangling), no parenthetical echo when key = native name,
  backend column without decorative markers

## [0.3.4] — 2026-10-04

The zone-tuning release: zone keys now match the players' native Sonos
names, and volume becomes a by-ear dashboard control instead of a
config-file number.

### Added
- **Per-zone volume sliders** — a Volume column in the dashboard zones table
  with a slider (0–100) per zone and a live numeric readout. Two layers, one
  write: durable `sonos.zone_volumes` in config.yaml PLUS a live in-memory
  override in `sonos_output`, so a slider change applies to the very NEXT
  clip — no bot restart. Volume tuning is a by-ear loop; the restart
  contract would make the feature useless. Resolution: live override >
  config per-zone > global `sonos.clip_volume`; sliding back to the global
  value removes the override (config stays minimal). All three play sites
  honor it — replies, duck-holder, and the test tone, so the tone IS the
  tuning instrument. New guarded `PUT /zones/{name}/volume`; mapping GET
  carries `zone_volumes` + `clip_volume`
- **`intelligence.zone` documented as the conversation label prefix** — fresh-mode
  pipelines mint `voice-<zone>-<id>` from it; with native-name zone keys the
  labels read sensibly in Hermes conversation history

### Fixed
- **Test tone silent on zone keys containing spaces** — the tone's filename
  embedded the raw zone key, and Sonos's audioClip fetcher fails silently
  on unencoded spaces: the tone never sounded on zone keys containing spaces
  while space-free keys worked, and replies dodged it by generating
  space-free names. Slugify the filename at the route and
  percent-encode the path in `url_for` — no future zone name can break a
  clip fetch

### Changed
- **Zone keys = native Sonos player names** across config (zones,
  default_zone, room_zones, `intelligence.zone` label prefix, zone_volumes
  overrides). The
  dashboard already displayed native names; now the keys match them, so a
  user reading Sonos's app and the dashboard sees one name per player.
  Keyed lookups all the way down (room_zones → zones → play), so a
  consistent rename is safe, and the slug fix means spaces in keys are fine
- **`sonos.clip_volume` default 70 → 35** — recalibrated by ear for a
  direct speaker path

## [0.3.3] — 2026-10-03

The rename + voice-UX release: the project takes its public name. One
silent-failure bug found by a live test,
one lying verify found by a deliberate wrong-password rollback test, and two
conversation features shipped and verified the same day.

### Added
- **Per-node session modes (fresh | sticky)** — dashboard checkbox per node.
  Sticky rooms keep conversation memory across node reconnects AND bot
  restarts: history stash `data/sessions.json` (atomic write after every
  successful turn — crash-safe, no teardown hook; gitignored) restored on
  pipeline construction, plus the stable conversation label `voice-<room>`
  so Hermes server-side conversation continuity applies. Fresh is the
  privacy default — unmapped rooms start every session clean. New guarded
  `POST /nodes/{room}/session-mode`; status carries `session_modes`.
  Spec: **sticky = stash + server-side continuity**. Verified: a
  "what's the last thing I asked you" test across a node restart AND a bot
  restart answered correctly from restored history (`6aec78e`)
- **Tiered fillers** — one spoken line per latency tier so a slow intelligence turn
  no longer goes silent after the first ack: tier 1 acks at 3s (the classic
  pool), tier 2 reports progress at 12s, tier 3 reassures at 25s, then the
  assistant waits quietly. Config `intelligence.fillers` is a tiered list; a flat string list
  still parses as a single ack tier at `intelligence.filler_after_secs` (legacy
  configs unchanged). All lines <2s spoken; tier 2/3 lines meet the ≥3-word
  echo-containment contract. Verified: all three tiers fire in sequence on
  a long turn, and rotation never repeats a line within a session
  (`de385c1`)
- **Wake-window forensics** — an AWAKE window that closes without VAD now
  logs delivered-frame count, peak amplitude, echo-gate state, and
  whether VAD ever fired, so the next silent failure names its own cause
  (frames=0 = stalled track; low peak = below-threshold speech;
  echo_blocked = a Sonos clip swallowed it) (`a55b2ec`)

### Fixed
- **pc_id capture was a silent no-op** (regression) — `SmallWebRTCTransport`
  is a facade holding `._client`, not `._webrtc_connection`; the runner args
  carry the connection directly. Wakes had been recording `room: null` in
  the status API (`a55b2ec`)
- **Wi-Fi `set_wifi` lying verify** (firmware 1.1.1) — NVS writes never
  reached the live WiFi driver (config only pushed at boot), so a wrong
  password re-associated on the OLD creds and acked "connected" in 1.2s —
  rollback unreachable. 1.1.1 pushes creds into the driver on `set_wifi`
  AND after rollback restore; a wrong password now produces 3 auth strikes
  → auto-restore → rejoin. Found by a deliberate wrong-password rollback
  test
- **Rename sweep coherence** — project renamed across 25 files: the env
  prefix, the systemd unit example and the GitHub repo follow the new name;
  the firmware's factory default room name is the project name (fresh
  flashes boot with it; deployed nodes keep their dashboard names)

### Known issues
- Node command acks that fire while the node is off-network (rollback
  notifications) never reach the dashboard — the node reappearing online
  is the signal. Rider queued: queue the ack for delivery after rejoin
- A disconnect loop (22s session deaths, media-stream errors) was seen
  once on a bot process that started while a node was mid-restart; a
  fresh process held a clean session. Not reproduced; forensics in place

## [0.3.2] — 2026-10-01 — Multi-node foundation + resilience

Correctness, resilience and the multi-node foundation. Behavior is
unchanged on a single-node setup.

### Added
- **Zone capability audit at startup**: every configured zone is checked
  against the all-S2 audioClip scope at boot — an unsupported player is
  logged before it can eat a reply mid-conversation (`fb2e585`)

### Fixed
- **Intelligence failure after the filler no longer goes silent**: the post-filler
  intelligence await has its own try/except (a sibling except clause cannot catch
  an exception raised in another handler's body) — the user hears
  "Sorry — I can't reach my intelligence right now" instead of dead air, and the
  outage reply never enters the server-side history as a fake memory
  (`897de5d`)
- **Duck-holder hardening**: every Sonos call is timeout-bounded; a dead
  websocket is dropped and reconnected on the next poll tick instead of
  retrying forever on the corpse; the wake gate's LOCKED state has a
  safety cap (refined by real clip duration via `note_speech`) so a lost
  stop-frame can no longer deafen the mic; the 8s silence tail is padded
  only when the holder is enabled and the turn is live, and lands AFTER
  the echo gate so the mic still reopens at the last syllable; duck
  ownership starts when the clip LOADS, not at generation end; the holder
  dies in `cleanup()`, not just on CancelFrame (`634af8f`)
- **Event loop unblocked**: MP3 encodes (reply clips, duck clip, dashboard
  test tones) and soco calls (name lookups, UPnP discovery) run in
  threads; dashboard node commands (restart/scan/rename/Wi-Fi) ride the
  direct data-channel wire via the output transport — a 40s intelligence call
  can no longer stall a command into a 504 (`7ae9ecb`)
- **Multi-node architecture**: room identity is per-pipeline — replies,
  wake records, and ducks resolve through THIS connection's pc_id instead
  of the global last-connected room; one openWakeWord Model per pipeline
  (the class-shared model mixed concurrent sessions' rolling buffers and
  every reconnect reset the live session's buffers); two physical nodes
  sharing a room name now log a loud rename warning instead of silently
  evicting each other into a reconnect loop (`a23187a`)

## [0.3.1] — 2026-09-30

Node management + audio UX — nodes get self-service and replies learn
to duck. All fixes and adjustments to existing features (per the
versioning rule: settings ship on main; a tag marks the day's shipped
state).

### Added
- **Duck-holder** (`sonos.duck_while_awake`, default on): the Sonos
  audioClip duck now spans the ENTIRE awake window, not just reply
  clips — an eternal loop holds a 30s silence clip on the wake room's
  zone while the gate is awake (refresh at 24s), so full-volume music/TV
  can no longer feed STT as phantom user turns (song lyrics transcribed
  as speech). Reply clips carry an 8s silence TAIL so the holder's
  refresh lands silence-on-silence — the duck is gapless from wake to
  sleep, no full-volume blips between reply and listening. Gate close
  cancels the holder and the music unducks once. Echo gate + LED timing
  stay tied to SPEECH-only duration (`9b4000d`, `3d28fa7`, `20d5d25`)
- **LED direct wire** (`core/led_state.py`): LED state messages ride
  the WebRTC data channel directly (output transport `send_message`),
  bypassing the pipeline — a blocked intelligence call stalls pipeline frames
  3-40s in both directions, which left the ring holding stale purple
  through the whole "On it." → answer gap. Speaking
  state now fires from SonosOutput's real-time TTS frames downstream
  of the intelligence; end-of-speaking is duration-timed to actual playback
  (`d02075a`, `69d9c5f`)
- Dashboard: firmware push reads the version from the uploaded image's
  embedded app descriptor (magic-checked; manual version entry removed);
  uploads stage to a temp file and swap atomically — a rejected upload
  can never clobber the served image (`6f04906`, `a09aa80`)

### Changed
- Firmware binary renamed to the generic `node_esp32.bin` across
  the OTA chain (served name, manifest, deploy script, upload dest,
  firmware CMake project name) — generic naming;
  version untouched, node unaffected (equal-version skip) (`c73832c`)
- Zone keys are now the player's native Sonos name (a player's
  native name IS its name) — dashboard displays live Sonos names
  verbatim (`bbed6cd`, `c2062f3`)

### Fixed
- Command channel end-to-end: `on_app_message` transport handler
  (sender = pc_id string), awaited `queue_frame`, `"type":"cmd_result"`
  envelopes, room eviction for stale same-room sessions (504s into a
  zombie session), NVS writes on an internal-RAM stack (PSRAM stack +
  flash-cache suspend = double fault), asymmetric liveness protocol
  (probe/pong without reply-to-reply loop) (`fd3fb32`…`3a9da0d`)
- Renames carry the `room_zones` mapping key — no ghost rows, no
  orphaned zone routing (`7465111`)
- Sync pipeline: host `config.yaml` is now authoritative —
  `sync_host.sh` PULLS it to the dev machine, never pushes (push-every-sync
  clobbered dashboard-written `room_zones` twice) (`26208bf`)
- Stale-session disconnects no longer mark a live room offline
  (`b7137d0`); payload schemas drop the redundant `room` field
  (`8777e85`)

## [0.3.0] — 2026-09-29

Web dashboard v1 — LenoxVI gets a GUI. Mapping, node status, zone
management, and test clips, all served by the bot process itself.

### Added
- `core/dashboard/` package: web dashboard at `/dashboard` — room→zone
  mapping page, live node list, Sonos LAN discovery (soco), per-zone
  test clips, status strip. Served by the bot's own FastAPI app on
  :7860 (same pattern as firmware_server); no new process or port
  (`ce8b35f`)
- Offer tee (pure ASGI): correlates the node firmware's `room` field
  in POST /api/offer with pipecat's server-side pc_id at
  response-send time — the pipeline runs as the offer request's
  background task, so session-end correlation is minutes too late
  (measured live; `ce81afb`)
- Room-aware reply routing: wake gate `on_wake` hook + SonosOutput
  `zone_selector` — a wake in a mapped room plays the reply clip in
  that room's zone; unmapped falls back to `sonos.default_zone`
  (`ce8b35f`)
- Dashboard config writes: comment-preserving config.yaml round-trip
  (ruamel.yaml), atomic write + rolling backup, config cache
  invalidated so reads reflect writes immediately (`ce8b35f`)
- `room_zones:` and `dashboard.enabled:` config sections; new deps
  soco 0.31.4 + ruamel.yaml 0.19.1 (optional at boot — voice runs
  without them, `f47f6a0`)
- `scripts/test_dashboard.py`: 20-check offline test harness (fakes /api/offer
  in both immediate and background-task shapes; exercises the tee,
  registry, config round-trips, validation, and a live Sonos test
  clip)

### Changed
- Comment de-personalization pass: the voice system
  prompt and code comments no longer name the user; the OWW model test
  docstring defers its positive case to a live test instead of a person
- Removed the "never address the user as boss" rule comment — how the
  intelligence addresses its user is Hermes-side config, not repo policy
- Comment accuracy + formatting sweep:
  wake_word docstring now matches the 2026-09-19 OWW-always-fed behavior
  and the permanent echo gate; sonos_output says MP3 not WAV; _is_own_echo
  describes the >=60%/3-word overlap it actually does; deploy_firmware
  drop the fictional check-ota message; config.example awake_timeout
  aligned with the code default (15.0); README glob/journald fixes;
  unused imports removed; import order tightened; no logic changes

## [0.2.2] — 2026-09-20 — Model caching + cleanups

Model caching and cleanups.

### Changed
- PipelineTask → PipelineWorker (deprecated alias, removed in pipecat 2.0);
  imports from `pipecat.pipeline.worker` — verified against pipecat 1.11.0
- Whisper + OWW models now cached per-process, shared across pipelines:
  every node reconnect used to reload large-v3-turbo onto the GPU (seconds
  of deaf STT + VRAM churn); OWW shared model resets its rolling buffers
  per pipeline (`reset()`)
- continuity_test reads intelligence.url via the config surface (was env-only,
  KeyError against a secrets-only .env)
- SonosOutput reads config via core/config's CONFIG_PATH (LVI_CONFIG split-brain)
- stt_local: dead `settings.language` branch → the real `self._language`
- .env.example: real voice ID replaced with placeholder (account data)
- Version comments drifted to pipecat 1.11 (were 1.10)

## [0.2.1] — 2026-09-20 — Fresh-clone fixes + tuning knobs

Cleanup and efficiency pass — fresh-clone fixes, wake-gate efficiency,
new tuning knobs.

### Fixed
- requirements.txt: fresh-clone booby traps — missing `lameenc`,
  `sonos-websocket`, `pipecat-ai-prebuilt` (fresh install died at the
  first spoken reply); TTS code default was the repo's own
  measured-broken `eleven_v3` (`3db43e4`)
- OWW wake gate fed 640 samples per chunk (1280 *bytes* labeled "80ms")
  — half openWakeWord's canonical 1280-sample frame, 2x inference on
  the 24/7 hot path; now 2560 bytes (`443b8e2`)
- containment_test Case 3 had `or True` — an assertion structurally
  unable to fail; now asserts real output (`766bd4d`)
- intelligence outage now speaks a fallback line instead of filler-then-silence

### Added
- `wake.model`, `intelligence.lull_secs`, `intelligence.dedup_window` config knobs
  (were documented-but-ignored or buried in constructor defaults)
- README fresh-clone section (deps → config → .env → wake model → cert → run)

### Removed
- config.yaml untracked (was in the index despite .gitignore — baked LAN
  IPs/Sonos UID into history); template is config.example.yaml
- 888KB never-flashable slave XMOS bin
- Overdue debug scaffolding: aiortc DEBUG, once-per-second wake diag
  (~86k journald lines/day), dead attribute, no-op assignments

## [0.2.0] — 2026-09-20

First tagged snapshot. Full pipeline live (wake gate → VAD → local
whisper STT → Hermes intelligence → ElevenLabs TTS → Sonos audioClip overlay),
running as a systemd service.

### Added
- Initial bot: local whisper ears, stub intelligence, ElevenLabs mouth (`8a8e8a8`)
- sonos_output: Sonos audioClip overlay — clip volume ~70, ~200ms load,
  opportunistic clip pruning at startup (`20c2a72`)
- config.yaml as the single tuning surface (yaml > `LVI_*` env > code default) (`96ce51b`)
- systemd service: boot survival, crash-restart, journald logging (`3a588e0`)
- `scripts/sync_host.sh`: bundle-based host sync, no creds on hosts (`11b16eb`)
- requirements.txt with pinned top-level deps (pipecat 1.11.0) (`f4d4203`)

### Changed
- `.env` is secrets-only (3 lines); all behavior lives in config.yaml;
  `config.example.yaml` is the committed template (`1993d34`, `c67ff7c`)
- Clip retention 7d → 48h (serve-once garbage) (`ca0e4fb`)
- README rewritten: config surface and fresh-clone path documented (`fc10870`, `127f4a6`)
- AGENTS.md/CLAUDE.md carried by sync_host (reset deleted them) (`1434922`)

### Removed
- Chatterbox local TTS lane removed entirely — production TTS is
  ElevenLabs only (`fede243` → parked `5b593c8` → removed `1ce3941`)
- Dead one-shot scripts: replay_clip, sonos_voice_test, ws_probe (`05157f8`)

### Fixed
- TTS voice clobber: yaml default overrode production — pinned
  eleven_flash_v2_5 (`fb0cf94`)