# Voice notes

Send an agent a voice note in Element X and it reads it as if you had typed it. The words are
worked out on this server by one shared service; nothing goes to a cloud provider.

## How it works

```
voice note -> Matrix -> agent (sandbox) -> voice service (logos-voice) -> text -> agent
```

- **One service for every agent.** `logos-voice` holds one Whisper model ("small",
  multilingual, so English and French both work). Putting the model in each agent would
  cost the server its memory once per agent.
- **Each agent has its own token** (`LOGOS_VOICE_TOKEN` in its home volume). A request
  without a known token is refused.
- **The sandbox may make exactly one request there:** `POST /v1/audio/transcriptions`.
  The OpenShell policy rule `voice` allows nothing else.
- **Nothing is kept.** The recording is written to a temporary file, transcribed and
  deleted. The service logs which agent asked, how long the recording was, the language and
  how long it took. It never logs the words.
- **It costs memory only while in use.** The model loads on the first voice note (under a
  second) and is dropped after ten quiet minutes. Loaded, it uses about 500 MB.
- **It cannot call out.** The container has no name resolver, and the telemetry in one of
  its libraries (onnxruntime) is switched off.

On this server an 11-second recording takes about 2 seconds.

## Turning it on

In `agents/<name>.yaml`:

```yaml
voice: true
```

Then `scripts/logos-agent up <name>`. The first `up` builds the image `logos-voice:1` from
`infra/voice/Dockerfile` (a few minutes, once) and starts the container. hermes is pointed at
it as an OpenAI-style transcription endpoint (`stt.provider: openai` with a `base_url`), so
the agent image is unchanged.

Without `voice: true`, speech-to-text is switched off for that agent and a voice note is not
understood.

`voice_echo: true` also posts the text of each voice note in the chat before the answer, so the
person can see what was understood. It is off by default (a voice note gets the answer only) and
on for Béré, who asked for it.

The agent can change it too: the `logos-voice` plugin (`infra/agent-plugins/logos-voice/`) gives
it a `voice_transcripts` tool, so the person can say "show me what you heard" or "stop sending the
transcripts". The choice is kept in `/opt/data/voice-echo.json` in the agent's home, survives
rebuilds, and from then on wins over `voice_echo`. It applies from the next voice note, with no
restart: the plugin makes hermes check it on every voice note instead of once at start-up. Delete
that file to go back to the yaml setting.

## What is inside the image

| Part | Version | Why |
| --- | --- | --- |
| `python` base image | pinned by digest | runs the server |
| `faster-whisper`, `av` | 1.2.1, 18.1.0 | the same pins hermes uses for its own speech-to-text |
| `ctranslate2`, `onnxruntime` | 4.9.0, 1.32.0 | what faster-whisper runs on |
| `Systran/faster-whisper-small` | fixed revision | the model, fetched at build time only |

## Limits

- One recording at a time; a second waits its turn.
- 100 MB per recording. The web chat records up to 30 minutes (about 7 MB at 32 kbit/s); 30 minutes takes about 6 to turn into words.
- Runs on the processor. A long recording takes roughly a fifth of its length.

## In web chat

The chat box and Home's **Ask** have a microphone button for agents with voice on (23 Nov: tap to
start, tap again to finish; it was a hold, and letting go by mistake lost the recording).

1. **Tap the microphone.** A strip replaces the box: a live waveform of your voice (so you can see it
   is hearing you), the time, ✕ to throw it away (it asks first if it is 15 seconds or more) and ■ to
   finish. It records up to **30 minutes** and keeps the screen awake where the browser allows.
2. **Tap ■.** It is sent at once, as a **voice message**: a bubble with the recording's real shape
   (48 bars) and its length, and its transcript underneath, to glance over and correct names in a
   later message. While the words are being worked out the bubble shows "Turning it into words…".
   The agent receives the words as an ordinary message; the shape and length travel beside them
   (`org.logos.voice` in the message), which agents and Element X ignore (Element X shows plain text).
   The audio itself is not kept.
3. **If it fails,** nothing is sent; the line under the box says why and **Try again** sends the same
   recording again.

On Home, **Ask** works the same way: finish a recording and it opens your agent's chat with the voice
message already sent.

The recording goes from the browser to the dashboard, which passes it to the voice service with the
agent's own token. It is a **job**: `POST /api/v2/agents/<name>/voice?job=1` answers at once with a
job id and the page asks `GET /api/v2/agents/<name>/voice/<job>` until the words are ready. A long
recording therefore does not hold one connection open for minutes (a proxy may cut a quiet one),
and if the page is reloaded meanwhile, the words are fetched again when it comes back. Jobs are kept
30 minutes, only for the person who made them. Without `?job=1` the call waits and answers with the
words directly (the other screens use that).

Browsers only allow the microphone on an `https://` address (or `localhost`), so the button
explains itself and does nothing on a plain `http://` address.

On the Agents screen each of your agents' marks has a small microphone: tap it, speak, tap ■, and that agent's chat opens with the voice message sent. Holding a mark only lifts it, to move it (it used to also record, with a swipe up to send and down to cancel: too easy to get wrong).

## The house speaker (Home Assistant Voice PE)

Say the wake word to the Voice PE and Home answers out loud. Home Assistant turns the speech
into words (its Whisper add-on for now) and hands them to the `logos` conversation agent, a small
Home Assistant integration kept in the private repo `logos-voice-pe`. It posts them to
`POST /api/v2/voice/converse` (`gateway/voice_converse.py`), which runs the Household agent and
returns the words to speak (Piper reads them out).

- **Token:** made on first start in `~/.config/logos/voice-converse.token` (0600). Paste it into
  the integration when adding it in Home Assistant. The path accepts nothing else.
- **Spoken answers:** the agent is told the reply will be read aloud (a few short sentences);
  Markdown, links and emoji are taken out before speaking.
- **One conversation per speaker per day**, so follow-ups keep their context.
- **Follow-ups:** an answer that ends in a question makes the speaker listen again without the
  wake word.
- **Never a long wait:** after 45 seconds the speaker says the answer will be in the chat; the
  agent carries on and keeps the answer in that conversation.
- **The words are never logged:** only the agent, the device, the outcome and how long it took.

### Why the first question was slow, and what keeps it fast

Every speaker request starts with the same thing: Home's tools and instructions, about 24,000
tokens. Reading those afresh takes LM Studio about 35 seconds on the current GPU (27 Nov: the first
question took 40.3 s, 39.8 s of it in one model call for 23,982 tokens and a 42-token answer). Once
it has read them, it holds them, and a later request that starts the same way skips straight to the
new part: 0.1 s if nothing changed, about 1 s when only the end of the instructions did (a new
day's date, the hourly brain index). The 18.7 s first question that evening was already held; its
time went on two model calls (a tool, then the answer), not on reading. Follow-ups take 3 to 8 s.

The voice instructions themselves do not get in the way: hermes adds them at the very end of the
system message, after the tools and Home's own instructions, so they stay the same from one
request to the next.

So the model guard keeps the speaker's prompt warm (`gateway/model_guard.py`, `Warmer`). It
remembers the latest house-speaker request's system message and tools (in memory only, never what
was said; the nightly check at 04:30 is the first each day) and every 15 minutes, once nothing has
used the model for a minute, sends them again with a one-character question and a one-token answer.
When LM Studio still holds them that costs 0.1 to 1 s; when something else pushed them out, the
35 s is paid then, while nobody waits, instead of when someone speaks. It is a bare model call: no
agent turn, nothing in any conversation, and never a model load. A speaker request that arrives
during a warm-up of its own prompt waits for it rather than read the same prompt a second time
alongside.

Measured 27 Nov against the real LM Studio, with a 21,800-token speaker prompt: the warm-up 33.3 s
(cold), then the first question of a new conversation started answering in 1.3 s, against 34.1 s
without it.

- Each warm-up is a `"event": "warm"` line in `docker logs logos-model-guard` (`seconds`, and
  `cold: true` when the prompt had to be read afresh); `/guard/health` counts them under `warm`.
- On by default. `LOGOS_VOICE_WARM_EVERY` (seconds) changes the interval and `0` turns it off; it
  is read by `scripts/logos-models up` (and the deploy), which recreates the guard with it.
- Not done: warming when the wake word is heard. A person speaks for only 3 to 6 seconds before
  the question arrives, against 35 s of reading, so it would save little once the prompt is kept
  warm anyway.

Plan, decisions and the next stories (wake word = the agent's name, voice prints, a voice per
agent) are in the spec doc's Voice tab.
