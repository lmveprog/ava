# security

Ava listens to a room, sees the screen, and can press keys on the machine she
runs on. That is a lot of reach for a hobby project, so here is exactly what she
does with it — and where the walls are.

## what leaves the machine

By default, nothing that you say does.

| what | where it runs |
| --- | --- |
| wake word (`ok ava`, double clap) | on device, vosk |
| transcription | on device, whisper large-v3-turbo via mlx |
| speech | on device, kokoro or chatterbox |
| screen analysis | on device, ollama |
| conversation | your own endpoint, `http://127.0.0.1:1234/v1` by default |

Three things do go out, and only when the request asks for them:

- **web search** — the query, to DuckDuckGo or to an official source.
- **calendar** — to Google, if you connected an account.
- **news and weather** — a handful of public feeds.

Two engines are opt-in and off by default. If you set `MISTRAL_API_KEY`, Voxtral
gets the audio of your commands. If you set `ELEVENLABS_API_KEY`, ElevenLabs gets
the text of Ava's replies. Leave both empty and she stays local.

## secrets on disk

| file | holds | mode |
| --- | --- | --- |
| `config.json` | google oauth client id + secret | `0600` |
| `.cache/google_token.json` | google refresh + access token | `0600` |
| `.cache/` | the above, plus caches | `0700` |
| `.env` | mistral / elevenlabs keys | yours to set |

All three are in `.gitignore` and have never been committed. They are written
through `paths.write_private()`, which creates the file at `0600` and renames it
into place — the mode is never a `chmod` applied after the fact, so there is no
window where the token sits there world-readable. `tests/test_secrets.py` holds
that behaviour down.

Ava never asks for your Google password. The connect button runs a normal
OAuth 2.0 desktop flow with PKCE: the consent page opens in your browser, Google
comes back to `127.0.0.1` on an ephemeral port, and the `state` parameter is
compared with `secrets.compare_digest`. The OAuth client is yours — you create it
in your own Google Cloud project, so your calendar never passes through anybody
else's credentials.

## acting on the mac

Ava can type, click, scroll and open applications. Two rules keep that bounded:

- **An explicit allowlist.** `parse_computer_intent()` recognises a fixed set of
  phrasings and returns a typed intent. Anything it doesn't recognise produces
  no action at all — there is no path from arbitrary speech to an arbitrary
  command.
- **AppleScript takes arguments, never string interpolation.** Text you dictate
  and names you click are passed as `argv` to `osascript`, so a quote or a
  newline in what you said cannot close the script and start a new statement.
  Nothing anywhere in the project uses `shell=True`.

Anything that isn't trivially reversible — pressing return, closing a window,
pasting, or a click on a button whose label looks like *send*, *pay*, *delete* —
is held back and spoken as a confirmation. The pending action expires after 30
seconds, so a forgotten "confirm" can't be picked up an hour later by a sentence
that happens to contain the word.

## untrusted input

**The screen is data, not instructions.** When Ava analyses a screenshot, the
prompt tells the vision model in so many words to treat the image as untrusted
and to obey nothing written in it. More importantly, the model's answer is only
ever spoken and displayed — no code path turns a vision reply into an action.

**Web results are text.** The panel builds every bubble with `textContent`, never
`innerHTML`, so a page title can't inject markup. Source links are filtered to
`http`/`https` before they reach the panel, and clicking one goes through
`open_external()`, which re-checks the scheme before handing it to `open`.

**Skills can run code.** A skill is a folder with a `SKILL.md` and, optionally, a
script — the same trust level as a script you run yourself from your terminal.
They are only read from `skills/` in this repo and `~/Documents/ava-skills`,
never downloaded automatically. A skill's `command:` is resolved and checked to
be inside its own folder, so `../../../bin/rm` is refused; scripts run with no
shell, with the request passed as an argument, and are killed after 45 seconds.
You can turn the whole mechanism off with `"skills": {"enabled": false}`.

**The wake word model is pinned.** `bootstrap.py` checks the SHA-256 of the vosk
archive before unpacking it, and refuses anything that doesn't match. Extraction
rejects members that would write outside `models/`.

## the permissions macOS will ask for

Each one is requested the first time the matching feature is used, never at
startup:

- **Microphone** — to hear you.
- **Accessibility** — to type, click, or drive a window.
- **Screen recording** — for the screen diagnosis.
- **Automation → Calendar** — to read events offline.
- **Automation → Promethee** — to start a focus session.

Screenshots are only taken when a request explicitly asks for one, and they stay
in `~/Pictures/Ava`.

## reporting something

Open an issue at https://github.com/lmveprog/ava/issues. This is a personal
project with no security team behind it — if it is something sensitive, say so
in the title and leave the details out until we can talk.
