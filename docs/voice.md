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
returns the words to speak, read out in that agent's own voice (below).

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

### Each agent in its own voice

Home Assistant's Piper add-on has one voice per pipeline, but more than one agent will answer
through the same speaker (Finn and Béré's agent, once the wake word picks the agent). So Logos
reads the answers out itself: a second small service, `logos-tts`, runs Piper with twelve English
voices and offers them to Home Assistant as a **Wyoming** text-to-speech service.

```
speaker -> Home Assistant -> /api/v2/voice/converse -> agent -> words
                                     |  notes: these words, this agent's voice
speaker <- Home Assistant <- logos-tts (Wyoming, Piper) <- "say these words"
```

- **How it knows who is speaking.** Home Assistant asks only "say these words" (with its
  pipeline's voice). When `voice/converse` returns an answer it notes a hash of the words and the
  agent's voice for two minutes (`gateway/speaker_voice.py`), in a file `logos-tts` reads
  (`~/.local/state/logos/speaker/said.json`, hashes only). The same words arriving from Home
  Assistant are read in that voice.
- **Anything else** (Home Assistant's own announcements, a timer) is read in the voice the
  pipeline names, so set the pipeline to Home's voice too.
- **Choosing the voice.** Settings › Speaker lists your agents and the twelve voices, each with a
  ▶ to hear a sample. For the Household agent either of you can change it. It applies from the
  next answer. The choice is kept in the dashboard's database (beside the agents' colours); until
  someone chooses, the agent's definition decides:

  ```yaml
  speaker_voice: en_GB-northern_english_male-medium   # agents/household.yaml (Home's, Greg's pick)
  ```

  An agent without `speaker_voice` and without a choice speaks in Home's voice.
- **What it can do:** speak, and nothing else. Wyoming has no login, so anyone on the LAN who can
  reach the port can make it say something; it can read nothing and reach nothing. The container
  is read-only, has no capabilities, no DNS, 1 GB of memory at most, and holds no tokens.
- **The words are never logged:** the log line has the voice, the agent, the length and the time.
- **Cost:** a voice loads on first use (under a second) and is dropped after ten quiet minutes; at
  most two are kept loaded, about 300 MB together. A short answer takes under half a second.
- **Known limit:** Home Assistant keeps its own cache of spoken phrases by their words. Once two
  agents answer, the same short answer ("Done.") from both may come back in the first one's
  voice. Fine with Home alone; to look at when routing by wake word arrives.

#### Turning it on

1. On the Logos machine, `~/.config/logos/speaker` (not in the repo):

   ```
   TTS_BIND=192.0.2.20      # this machine's LAN address (0.0.0.0 for every address)
   TTS_PORT=10200           # optional, Wyoming's usual port
   ```

2. `scripts/logos-agent up household`. The first time it builds the image `logos-tts:1` from
   `infra/tts/Dockerfile` (a few minutes, about 1.9 GB on disk, most of it the voices) and starts
   the container. Any agent with `speaker_voice:` starts it the same way; without the file it
   says "speaker voices: off" and carries on.
3. In Home Assistant: Settings › Devices & services › Add integration › **Wyoming Protocol**, host
   = the Logos machine's address, port 10200. It shows up as "logos" with twelve voices.
4. Settings › Voice assistants › the speaker's pipeline › Text-to-speech: choose **logos**, and as
   its voice `en_GB-northern_english_male-medium` (used for Home Assistant's own words). The old
   Piper add-on can stay for other pipelines or be removed.

Check it from the Logos machine: Settings › Speaker › ▶ plays a sample (the dashboard asks
`logos-tts` over Wyoming, like Home Assistant does), and `docker logs logos-tts` shows a line per
answer, e.g. `en_GB-northern_english_male-medium for household: 42 characters in 0.3 s`.

| Part | Version | Why |
| --- | --- | --- |
| `python` slim base image | pinned by digest | runs the server |
| `piper-tts` | 1.8.0 | the speech engine Home Assistant's Piper add-on uses (GPL-3.0, on its own in the container) |
| `wyoming` | 1.11.0 | the protocol Home Assistant speaks to voice services |
| `onnxruntime` | 1.32.0 | what Piper runs on (the voice service's pin) |
| `rhasspy/piper-voices` | fixed revision (12 Nov) | twelve English medium voices, fetched at build time only |

Plan, decisions and the next stories (wake word = the agent's name, voice prints) are in the spec doc's Voice tab.
