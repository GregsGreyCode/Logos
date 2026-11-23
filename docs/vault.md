# The vault: what people send an agent is kept (F47, phase 1, 23 Nov 2026)

Greg, 23 Nov: "I shouldn't have to reshare." The household agent had been sent the bin rota as images and
could not answer "whose turn is it?" until it was sent again. hermes downloads every photo and document into a
cache and **empties that cache after about a day**; the agent keeps only what it wrote down at the time.

## What phase 1 does

```
photo or document ──► Matrix ──► agent (hermes) ──► logos-vault plugin ─┬─► /opt/data/vault/files/…   the original
                                                                         ├─► /opt/data/vault/meta/…    who, when, where
                                                                         └─► tells the agent, this turn
                                    every 10 minutes: vault_digest.py ──► brain/files/<date>-<name>-<id>.md
```

1. **Kept the moment it arrives** (`infra/agent-plugins/logos-vault`). The file is copied into the agent's own
   vault: `files/<yyyy>/<mm>/<id><ext>` (owner-only) and `meta/<id>.json` (who sent it, when, in which chat, the
   caption, what has been done with it). The id is the start of the file's SHA-256, so a file sent twice is
   kept once and the second sending is added to its `seen` list. Photos and documents only: **never voice
   notes or audio** (voice notes are never stored), and not videos yet (large). Over 50 MB is not kept.
2. **The agent is told.** The message it receives carries one line: *Kept in your vault: "Rota.pdf" (id …).
   A written note of it appears in brain/files/ within about ten minutes: look there before ever asking for it
   again.* (Shown once per file, not in the chat.)
3. **A written note in the brain** (`infra/agent-jobs/vault_digest.py`, a script-only job every ten minutes,
   behind people's chats because its model requests say they are scheduled work):
   - **a photo or screenshot:** the local vision model transcribes its text exactly (tables kept as tables
     with every row) and says what it shows;
   - **a PDF with a text layer, a Word file, a text file:** its text, with a short summary above it when long;
   - **anything else, or a scan with no text layer:** its details only, saying it is not read yet (scans are
     read in a later phase, when a page can be rendered to an image);
   - two files per run at most; if the model server is off nothing is counted and the next run tries again; a
     file that fails three times gets a note saying so. The original is never touched.
4. **The rule** (`souls/_shared/vault.md`, in every agent's persona): search `brain/files/` before ever asking
   someone to send a file again, and say where it was found. The brain index (`brain_index.py`) lists the notes
   so the agent knows they exist.

## Where it lives, and who can see it

- In the agent's **own home volume** (`logos-home-<name>`): private to that agent. Béré's files never reach
  Greg's agent. The household agent's vault is the household's, like its chat.
- The **originals are not in the brain**, so Syncthing does not carry them to phones. The **notes are**
  (they are brain pages, as every note is), so they show up in Obsidian beside the agent's other pages.
- Nothing leaves the household: the vision model is the local one, through the model gateway.
- To switch it off for an agent: `vault: false` in `agents/<name>.yaml`.

## Kind to a computer that is sometimes off

The job asks the model server one cheap question (`/models`, 5 seconds) before any real request, so a
switched-off GPU costs one quick call and nothing is counted against the file. Per run: at most one file that
needs the model (a photo; a text or PDF file needs none), a ten-minute limit (no photo is started with under two
minutes left) so it never holds the agent's job tick for long, and its requests are marked as scheduled work so the
model guard queues them behind people's chats. Files that need no model are still done while the model is off.

## Numbers on the Prometheus endpoint

`logos_vault_files`, `logos_vault_bytes`, `logos_vault_files_by_state` (new / retry / done / failed) and the
oldest and newest file's time, per agent: see docs/metrics.md. Counts and sizes only.

## What it does not do yet

- **Nothing is deleted.** "At least 90 days" is met by keeping everything; retention (90 days, then a 30-day
  graveyard like the brains') and pinning come with the Files screen (phase 3).
- **No search by meaning yet** (phase 2: keywords plus embeddings, a `vault_search` tool). Until then the agent
  finds notes by reading `brain/files/`.
- **No scans** (PDFs that are pictures of pages), no video, no audio.
- Reading a big table photo takes the model a few minutes; the note appears when it is done.

## The reading helper

The one Python the sandbox lets reach the model has no PDF, Word or image libraries. hermes's other Python has
them but is not allowed on the network. Reading a file needs no network, so `vault_digest.py` runs a small
subprocess with that Python to read PDFs and Word files and to shrink images (to 2000 pixels, so a table stays
legible), and only the allowed Python talks to the model.

## Voice messages are now told to the agent (`logos-voice-tag`)

A message recorded in the Logos app is sent as a voice message (docs/voice.md). Agents now receive it as a
plain quoted line, exactly as hermes presents a transcribed voice note from Element X (`"Is the dentist on
Thursday?"`), so their rules for voice notes apply: speech to text mistakes names and unusual words; carry on
when the meaning is clear, ask when it is not. In a group, only the words after "Agent name: " are quoted.
