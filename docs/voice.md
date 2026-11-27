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

Say an agent's name to the Voice PE ("Hey Finn", "Hey Home") and that agent answers out loud
(below: wake words). Home Assistant turns the speech into words (its Whisper add-on, or Logos's own
speech to text, which also tells who is speaking: below) and hands them to the `logos` conversation
agent, a small Home Assistant integration kept in the private repo `logos-voice-pe`. It posts them to
`POST /api/v2/voice/converse` (`gateway/voice_converse.py`), which runs the agent that was called
(Home when no name was heard) and returns the words to speak, read out in that agent's own voice (below).

- **Token:** made on first start in `~/.config/logos/voice-converse.token` (0600). Paste it into
  the integration when adding it in Home Assistant. The path accepts nothing else.
- **Spoken answers:** the agent is told the reply will be read aloud (a few short sentences);
  Markdown, links and emoji are taken out before speaking.
- **One conversation per speaker per day**, so follow-ups keep their context.
- **Follow-ups:** an answer that ends in a question makes the speaker listen again without the
  wake word.
- **Never a long wait:** after 45 seconds the speaker says the answer will be in the chat; the
  agent carries on and posts the answer there when it is ready (below).
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

### In the chat (V1.3, 27 Nov)

Every exchange at the speaker also shows in the agent's chat: its main Matrix room (`home_room`, where
its scheduled messages go; for Home, the room Greg and Béré share), in Element X and in the Logos chat.

    🔊 Heard at the house speaker:
    > “What's on tomorrow?”

    The dentist at ten, then nothing until the evening.

- **The agent posts it**, through its own Matrix client, so it is end-to-end encrypted like its other
  replies (agent plugin `infra/agent-plugins/logos-speaker-chat`, run as the speaker turn finishes).
  The words heard are quoted inside the agent's message: they are never shown as something a person
  typed. When Logos recognised the voice (below) it says who: "🔊 Greg at the house speaker:";
  otherwise only "the house speaker".
- **The full answer**, not the shortened one that was read out.
- **Answered out loud:** a notice (`m.notice`), a record that does not count as unread and that agents
  do not answer. **Too late for the speaker** (it said "I'll put the answer in our chat"): a normal
  message, so it is noticed. The message carries `org.logos.speaker` (`{heard, late}`).
- **The chat conversation is told** (hermes's delivery mirror, into the room's own conversation, not
  one of its threads), so "what did I just ask the speaker?" can be answered in the chat. It goes into
  the room's current conversation: after the daily fresh start (03:00), until someone writes in the
  chat, that is still the day before's, so the note may not carry over. The speaker's
  own conversation stays separate (one per speaker per day): the chat is not read out to the room.
- **In the Logos chat** it shows as the agent's message with a microphone, "Heard at the house
  speaker" ("· answered here" when it came too late) and the words in italics, the answer underneath.
  Only an agent's message is shown that way. Element X shows the plain text above.
- Not posted: the nightly timing check (`voice-probe-…`), a turn with no answer, and agents without a
  `home_room`. Nothing here is logged but that it was posted.
- The phone alert works as for any agent message: Logos's own alerts show the first words
  (`logos-alert-text`), so whoever has alerts on sees "🔊 Heard at the house speaker: …".

Why this way: the speaker turn runs through hermes's HTTP API, which cannot post in Matrix, and Logos
cannot write into an encrypted room as the agent (only the agent's own device holds its keys; a second
device would show as unverified). The plugin runs in the same hermes process as the agent's Matrix
client, so it posts as the agent's verified device. Running the speaker turn inside the Matrix
conversation itself was ruled out: hermes guards a gateway conversation against a second writer, a
speaker question could not wait behind a chat turn, and the spoken-answer instructions would leak into
the chat.

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
- **Known limit:** Home Assistant keeps its own cache of spoken phrases by their words. Now that
  two agents answer (wake words, below), the same short answer ("Done.") from both may come back
  in the first one's voice. Still to look at.

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

### Who is speaking (voice prints, VO.1 and VO.2)

Logos can tell who is speaking at the speaker from their voice. Home then answers for that person
("what's on today?" is their day, "add milk" is from them), and a personal agent (Greg's Finn, later
Béré's) answers only its own person and politely says no to anyone else.

```
speaker -> Home Assistant -> logos-stt (Wyoming): the words, and an embedding of the voice (kept 2 min)
                          -> /api/v2/voice/converse -> "whose voice said these words?" -> agent
```

- **The audio has to reach Logos,** so Logos offers its own speech to text to Home Assistant, as a
  Wyoming service like the speaker voices: `logos-stt`. It turns the speech into words (NVIDIA's
  Parakeet, English: the model Home Assistant's Whisper add-on itself picks by default) and, from
  the same audio, works out a voice embedding (192 numbers, the CAM++ model from 3D-Speaker). It
  keeps the embedding in memory for two minutes under a hash of the words. When the words arrive
  at `voice/converse`, the dashboard asks for that embedding and compares it with the voice prints
  (`gateway/voice_id.py`). Words Home Assistant changed on the way are matched to the one command
  heard in the last 20 seconds, if there is only one.
- **As quick as Home Assistant's own.** Measured 27 Nov on this dev machine, 30 LibriSpeech
  commands of about 4.5 s, from the end of speech to the words: logos-stt 0.3 s (1.1% of words
  wrong); Home Assistant's Whisper add-on container with its default model (the same Parakeet)
  0.25 s (1.1%), with Whisper small 1.1 s (3.6%), base 0.7 s (4.7%), tiny 0.3 s (9.6%). The extra
  50 ms is the voice embedding. It holds about 900 MB of memory.
- **Three answers:** that person (`known`), `not sure` (too short, too close to call, or just under
  the strictness), or `someone else`. Only `known` names anyone. Home is told who it is in its
  spoken-answer instructions ("The person speaking is Greg …"), and the words reach it as
  `[Speaker: Greg] what's on today?` (which the chat post uses).
  Only the end of the instructions changes with who is speaking, so the warm prompt (above) still
  holds: about a second more when the speaker changes.
- **A personal agent** answers only when its own person was recognised. Anyone else hears "Sorry, I
  only answer Greg. Home can help you."; a voice it is not sure of hears "… Could you say it again?";
  and while its person has no voice print, "… I don't know their voice yet". The agent is not run.
  Home (and any agent for everyone) answers anyone; when nobody has a voice print, nothing changes.
- **A voice print is a strong hint, not a lock.** A recording of someone's voice can fool it.
  Anything private stays behind the phone-on-the-home-network rule (a later story), not behind the
  voice.

#### Your voice (Settings)

Settings › **Your voice**: read the words on the screen for about twelve seconds, tap ■, and the
voice print is made. It says how good the recording was (seconds of speech, how clean) and how good
the print is: your own samples' scores, how close the 40 reference voices come, and the strictness
it set. Then:

- **Test: me** and **Test: someone else** (hand the phone over): each says the score and whether it
  was recognised. Someone else heard makes it stricter if they came close.
- **Recording again** adds to the print (another room or distance helps).
- **At the house speaker:** ask the speaker something, then **Check what it heard**: the last two
  minutes, each scored against your print only. If it did not recognise you, **That was me** adds it
  (not if it sounds more like someone else who has a print). The speaker's microphone sounds
  different from a phone's, so this matters (measured below).
- **Delete my voice print.**

Each person sees and manages only their own. The browser's own noise suppression is switched off
for this recording, so the voice is heard as it is.

#### How it decides (ported from the wake-word spike)

- A print is several samples: the recording's speech cut into pieces about as long as a spoken
  command (3 s), plus the whole. A new voice's score is the average of its likeness to the centre
  of your samples and to your three closest ones.
- **The strictness** sits just above the closest other voice: 40 people from LibriSpeech (three
  clips each, in the image) with a margin, and anyone you let it hear in "Test: someone else".
  Never looser than 0.5. The spike put it halfway between the others and your own scores; here you
  enrol on a phone and are heard through the speaker, which scores you lower, and halfway refused
  too many (below).
- **It learns:** a clear match at the speaker (well above the strictness, two seconds or more,
  clean) is added to your print; the newest ten are kept beside the enrolment.
- Two people both above their strictness and less than 0.08 apart: not sure.

Measured 27 Nov on this dev machine with 40 LibriSpeech speakers who are not among the reference
voices (enrolled from 13 s of reading; tested on 3-second commands; 3,120 tries by others each way):

| | own commands recognised | others wrongly accepted |
| --- | --- | --- |
| same kind of microphone | 96% | 0 of 3,120 |
| speaker-like (band-limited, echo, room noise) | 71% | 0 of 3,120 |
| speaker-like, after two "That was me" | 91% | 1 of 3,120 |
| Piper voices (synthetic, 36 clips × 40 prints) | | 0 of 1,440 |

In a household of two, a guest's command was taken for one of them once in 1,520 tries (after
learning); the rest were "someone else" or "not sure". Real people at the real speaker will differ:
test it with Settings › Your voice.

#### What is kept, and what is not

- **Voice prints:** the embeddings and their strictness, in the dashboard's database
  (`voice_prints`), one row per person. Never audio.
- **`logos-stt`:** the last two minutes of embeddings, in memory, under hashes of the words. The
  audio is transcribed in memory and dropped; a recording from Settings goes to a temporary file
  and is deleted at once. Its log line has the length, the seconds of speech and the time taken.
- **The dashboard's log:** the answer (`known`, `not_sure`, `someone_else`), the person and the
  score. Never the words.
- **Two doors:** Wyoming on the LAN (speech in, words out; no tokens behind it), and a small HTTP
  door for the dashboard on 127.0.0.1 only, with its own token (`~/.config/logos/stt.token`). The
  container is read-only, has no capabilities and no DNS, and holds no voice prints.

#### Turning it on

1. Add to `~/.config/logos/speaker` (beside `TTS_BIND`):

   ```
   STT_PORT=10300           # Logos speech to text for Home Assistant (Wyoming's usual port)
   ```

2. `scripts/logos-agent up household`. The first time it builds `logos-stt:1` from
   `infra/stt/Dockerfile` (a few minutes, about 1.7 GB on disk; it fetches the models and 340 MB of
   LibriSpeech to pick the reference voices from) and starts the container.
3. In Home Assistant: Settings › Devices & services › Add integration › **Wyoming Protocol**, host =
   the Logos machine's address, port 10300. It shows up as "logos" (speech to text).
4. Settings › Voice assistants › the speaker's pipeline › Speech-to-text: choose **logos**.
5. Each person: Logos › Settings › Your voice, record, test, then ask the speaker something and
   check it there.

Check it: `docker logs logos-stt` shows a line per command, e.g. `heard 3.6 s (3.3 s of speech) in
0.3 s`, and the dashboard's log `voice id: known greg (score 0.71)`.

| Part | Version | Why |
| --- | --- | --- |
| `python` base image | pinned by digest | the voice service's |
| `sherpa-onnx` | 2.0.1 | runs both models |
| Parakeet TDT 0.6B v2 (int8) | pinned by SHA-256 | English speech to text, as Home Assistant's add-on uses |
| 3D-Speaker CAM++ (zh_en common advanced) | pinned by SHA-256 | voice embeddings (the spike's model) |
| `av`, `numpy` | 18.1.0, 2.3.4 | reading browser recordings |
| `wyoming` | 1.11.0 | the protocol Home Assistant speaks |
| LibriSpeech dev-clean | pinned by SHA-256 | 120 clips of 40 speakers: the reference voices |

### Private things out loud only when your phone is home (D2, 27 Nov)

Greg's decision: an agent says someone's private details out loud (their own email, personal calendar,
messages, private notes) only while that person's phone is on the home network. Otherwise the speaker
says the details are in the chat, and the agent writes them there after a `Details:` line: only what
comes before that line is read out, and the whole answer is copied to the chat (logos-speaker-chat).
Shared household things (the lists, the lights, the household calendar) are not private.

- **Whose phone:** `~/.config/logos/presence` (not in git), one `username=tracker` per line, for example
  `greg=device_tracker.greg_s_phone`. The tracker must say `home` (the Home Assistant Companion app's
  location or Wi-Fi report).
- **The person** is the one the voice print recognised. Nobody recognised, no tracker, a tracker that is
  not `home`, or Home Assistant not answering: away, the cautious answer.
- Checked through Logos's own Home Assistant account, at most every 30 s per person
  (`gateway/presence.py`). Only "home" or "away" is logged.

The words sent to the agent lose the name that woke the speaker ("Hey Finn, what's on?" becomes
"what's on?"): Home Assistant keeps the audio from just before the wake word, so the name is often
heard as well.

### Wake words: each agent's name (VW.1, 27 Nov)

The speaker wakes to the agents' names and the name picks the agent. The names are not trained
into anything: the speaker streams what it hears to Home Assistant, Home Assistant streams it to
a third small service, `logos-wake`, and that listens for the names with an open-vocabulary
keyword spotter (sherpa-onnx). Change a name and the speaker wakes to the new one at once.

```
speaker --(audio, all the time)--> Home Assistant --(Wyoming)--> logos-wake
                                                                     | heard "Hey Finn": greg
                                                                     v  (heard.json)
speaker -> Home Assistant (Whisper) -> /api/v2/voice/converse -> Finn
```

- **The names** come from the agents' definitions:

  ```yaml
  speaker_name: Finn                    # agents/greg.yaml: "Hey Finn" and "Finn"
  speaker_name: Home                    # agents/household.yaml
  speaker_wake: [Hey Home]              # optional: exactly these phrases ({Hey Home: 0.1} sets a threshold)
  ```

  Without `speaker_wake`, a name gives "Hey <name>" (threshold 0.1) and the bare name (0.25,
  stricter: one word is easier to say by accident). Home has only "Hey Home": "home" is too
  common a word (it woke on ordinary speech, below). A higher threshold wakes less readily. A
  phrase two agents claim wakes neither. Accents are dropped ("Béré" is "Bere").
- **Following a change.** The names go into `~/.local/state/logos/wake/names.json` on every
  `scripts/logos-agent up` and when the dashboard starts; `logos-wake` reads it again whenever it
  changes (no restart). `docker logs logos-wake` lists the names it now listens for.
- **Which agent.** Each detection is noted in `heard.json` (agent and phrase, kept a minute).
  `voice/converse` takes the agent heard in the last 30 seconds, once; a follow-up in the same
  Home Assistant conversation stays with it; otherwise Home (or `LOGOS_VOICE_AGENT`). Wyoming does
  not say which speaker streamed the audio, so with two speakers spoken to within seconds of each
  other the second could get the first one's agent. Finn answering only Greg is the voice-print
  work (VO.2), not this.
- **In Home Assistant** each name is its own wake word, plus "Any agent's name" (`logos_any`):
  choose that one in the pipeline and every name wakes it.
- **Nothing is kept.** The audio goes through the model and is dropped. The log line has the
  phrase, the agent, the threshold and how far into the stream, e.g.
  `heard 'Hey Finn' -> greg (threshold 0.1) at 7.31 s into the stream`. Never sound or words.
- **What it can do:** say that it heard a name. Like `logos-tts` it is on the LAN without a login,
  so anyone there can stream audio to it; it can reach nothing. Read-only container, no
  capabilities, no DNS, one core and 512 MB at most. It runs as the Logos user (not 10000) so it
  can write `heard.json` in a folder only that user owns.

The speaker's own firmware cannot stream to a wake word service: the official Voice PE firmware
always hears its wake word on the device ("Okay Nabu", "Hey Jarvis", "Hey Mycroft") and has no
setting to change that. So the speaker needs a small custom build, kept in `logos-voice-pe`
(`firmware/`): the official config, pinned, with only the wake word moved to Home Assistant.
Building, flashing and going back to the official firmware are in that repo's README.

#### Measured (27 Nov, this machine, Piper voices and LibriVox)

Through the Wyoming protocol as Home Assistant speaks it (`scripts/wake-bench.py` in
`logos-voice-pe`), with the shipped names and thresholds. Speech: the twelve Piper voices of
`logos-tts` saying "Hey Finn, what time is it?", "Finn, turn the kitchen lights off." and "Hey
Home, what's on tomorrow?", clean, in room noise at 10 dB SNR, and 20 dB quieter.

| | clean | noise | quiet |
| --- | --- | --- | --- |
| "Hey Finn" | 10/12 | 8/12 | 10/12 |
| "Finn" | 9/12 | 5/12 | 7/12 |
| "Hey Home" | 10/12 | 8/12 | 9/12 |

- **Misses** are mostly a few voices (Piper's `joe`, `alba`, `amy` miss most of them): synthetic
  voices, so a real person at the speaker will differ either way. Lowering the threshold from
  0.25 to 0.1 caught "Hey Home" more often (20 to 27 of 36) and changed nothing for "Finn"; a
  stronger boost (2) lost more than it gained, and adding the spelling "Fin" only added false wakes.
- **False wakes:** 2 in 27.6 minutes of ordinary speech (25 minutes of LibriVox readings, five
  readers, plus Piper reading 60 everyday sentences full of "home", "fine", "Finland", "hey"):
  "Hey Home" once in Joyce's *Eveline* and "Finn" once, about 4 an hour of continuous talk. The
  bare "Home" woke 5 times in the same speech, which is why Home has only "Hey Home". A false wake
  costs a question to Home or Finn that nobody asked; the speaker's light shows it.
- **Latency:** the detection arrives 0.39 s (median; 0.65 s p90) after the end of the name, in
  real time. Home Assistant keeps the audio from before the detection for speech-to-text, so the
  question can follow the name without a pause (the words may then start with the name).
- **Cost while it listens:** 3.4 % of one core (AMD Ryzen AI 9 HX 370) and 83 MB, streaming two
  minutes of quiet room noise.

#### Turning the wake words on

1. Add to `~/.config/logos/speaker` on the Logos machine:

   ```
   WAKE_PORT=10400          # turns them on; listens on TTS_BIND (or WAKE_BIND)
   ```

2. `scripts/logos-agent up household`. The first time it builds `logos-wake:1` from
   `infra/wake/Dockerfile` (a minute; 280 MB) and starts it.
3. In Home Assistant: Settings › Devices & services › Add integration › **Wyoming Protocol**, host
   = the Logos machine, port 10400. It shows up as "logos" with the names.
4. Settings › Voice assistants › the speaker's pipeline › **Streaming wake word engine**: "logos",
   wake word "Any agent's name".
5. Flash the speaker with the `logos-voice-pe` firmware (its README). Until then nothing changes:
   the official firmware keeps waking to "Okay Nabu", and those requests go to Home.

| Part | Version | Why |
| --- | --- | --- |
| `python` slim base image | pinned by digest | runs the server |
| `sherpa-onnx` | 2.0.1 | the keyword spotter (Apache-2.0) |
| `sentencepiece` | 0.2.2 | spells a name in the model's word pieces |
| `wyoming` | 1.11.0 | the protocol Home Assistant speaks to voice services |
| `sherpa-onnx-kws-zipformer-gigaspeech-3.3M-2024-01-01` | k2-fsa's release, SHA-256 checked | the English keyword model, fetched at build time only |

Plan, decisions and the next stories (wake word = the agent's name, voice prints) are in the spec doc's Voice tab.
