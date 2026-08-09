# Third-party notices

## Agent Skills (the skill format)

The `skills/` folder follows the open **Agent Skills** standard
(<https://agentskills.io>): one folder per skill, a `SKILL.md` carrying a YAML
frontmatter (`name`, `description`), and progressive disclosure in three steps —
discovery, activation, execution.

The format was originally published by [Anthropic](https://www.anthropic.com/)
and opened up to the ecosystem. Ava implements her own reader for it — no code is
reused — with one extra field, `command`, naming the script to run.

Applying it to a voice assistant, and treating latency and cost as first-class
constraints (see `src/ava/traces.py`), are ideas taken from
[OpenJarvis](https://github.com/open-jarvis/OpenJarvis) (Apache 2.0, "Copyright
2025 The OpenJarvis Authors"). Not a line of its code was copied; only the
architectural ideas were.

## French Vosk model

The offline wake word uses `vosk-model-small-fr-0.22`, distributed by
[Alphacephei](https://alphacephei.com/vosk/models) under the Apache 2.0 licence.
`bootstrap.py` pins its SHA-256.

## Voices

[Kokoro](https://huggingface.co/hexgrad/Kokoro-82M) (Apache 2.0) is the default
local voice, French `ff_siwis`. Chatterbox is the alternative local engine.
Neither is bundled here — both are downloaded on first use.

---

*Note, 8 August 2026: the "AI Orb Mascot" and the Rive runtime are gone. The orb
is drawn locally on a canvas now, and the panel loads no remote asset at all.*
