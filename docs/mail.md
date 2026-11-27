# Mail: each person's agent reads their own email

Story E1 (read), F43 (several mailboxes, 27 Nov 2026), E2 (the agent drafts replies, 27 Nov).
Decided 20 Nov 2026: read, then drafts, then sending only after the person presses **Send** on the
dashboard (E3, on hold). Greg's and Béré's agents; the household agent never.

## How it fits together

- **The agent never holds a mail login.** It calls the Logos mail service
  (`gateway/mail_service.py`, container `logos-mail`) on the Docker bridge, port 8099, MCP, with its
  own token. The service answers each agent for its own person's mailboxes only.
- **Several mailboxes (F43).** A person can connect as many as they like, Gmail and Outlook
  mixed. The tools look in all of them, each email says which `inbox` it is in, and every tool
  takes an `inbox` (an address, part of one, or "gmail" / "outlook") to look in one only. Email
  ids are `<mailbox id>:<id>`.
- **A local copy.** Every 5 minutes the service copies the last 90 days of Inbox and Sent of each
  mailbox into `logos-mail-state` (SQLite), and the tools read the copy. Nothing in this story can send,
  delete, move or mark mail: Gmail is read with `BODY.PEEK`, Outlook with GET requests only.
  A busy mailbox's first copy stops at 2,000 (Outlook) or 1,500 (Gmail) messages a folder; each
  sync then copies one more batch of older mail until the 90 days are in, and until then the tools
  say how far back the copy goes.
- **Logins**, one per mailbox, live in `~/.config/logos/mail/<person>/<id>.json` (folder 0700,
  files 0600), written by Settings › Email or `scripts/logos-mail`; `<id>` is 8 random hex
  characters. `logos-agent up` passes them to the container on stdin into the read-only
  `logos-mail` volume; they are never on a command line, in git, a sandbox or a chat.
- **Untrusted text.** Every tool result says the emails are other people's words, and
  `souls/_shared/mail.md` forbids acting on anything an email asks, sharing it with other agents
  or the household brain, or searching the web with it. The sandbox rule `mail` lets through
  only the five read tools.
- **Excluded senders** ("Private senders" in Settings › Email, or
  `scripts/logos-mail exclude <person> mybank.example [--inbox <address>]`) are kept per mailbox:
  never copied from that mailbox, and mail from them already in its copy is deleted on the next
  sync after the restart.
- **Disconnecting** a mailbox removes its login and, when the service restarts, its copy.
- The container: its own network `logos-mail` (it needs the internet: Microsoft and Google),
  read only, uid 1000, no capabilities, 256 MB.

| Tool | What it does |
| --- | --- |
| `mail_new` | Newest Inbox emails (default the last 24 hours): sender, subject, preview, inbox |
| `mail_needs_reply` | The newest email of each conversation that may need an answer, with its reply facts (below): "reply" first, then "maybe"; what needs none is only counted (`left_out`, by reason) |
| `mail_search` | Words, a name or an address, in subject, sender, recipients and text |
| `mail_read` | One email in full (first 8,000 characters), attachment names only |
| `mail_thread` | The whole conversation, oldest first, who said what (quoted copies cut out); the person's other recent emails to that correspondent; three of their own emails to write like them; the reply facts of the newest received email |

The morning briefing gets one line: how many emails need a reply ("reply" only), and the first senders.

## Drafting replies (E2)

The agent writes a reply in the chat, marked **Draft reply to <name>** (not sent), and asks: copy
it if it works, say what to change, or skip. The person sends it from their own mail app. **Logos
sends nothing**; there is no tool for it. The rules are in `souls/_shared/mail.md`, "Drafting replies".

Greg's worry (27 Nov): the agent was not good at telling what needs a reply or what earlier emails
said. So the service works out the facts itself, `reply_facts` in `gateway/mail_service.py`, and
gives them with each email:

| Fact | From |
| --- | --- |
| `sender`: person, newsletter, no-reply, automated, mailing list, you | List-Unsubscribe, Precedence: bulk, a bulk-mail service's headers (Mailchimp, SendGrid, SES, ...), Auto-Submitted, List-Id, a no-reply or company address (`info@`, `support@`, ...), an unsubscribe line in the text |
| `you_are`: to, cc, not named (Bcc or a list) | To and Cc against all the person's addresses |
| `asks` | The questions and requests in the email's own text (quoted earlier messages, signatures, small talk and "let me know if you have any questions" left out) |
| `your_person_wrote_to_them` | Emails in Sent to that address |
| `your_person_replied_since` | A sent email in the same conversation after it |
| `age_days` | Its date |

and a `suggestion` with the reason (`why`):

- **no**: their own email; already answered; no-reply; newsletter or marketing; automatic (unless
  it asks something and they wrote to it: then maybe); only thanks, confirms or "FYI"; a list or
  Bcc email with no question; over 30 days old with no question; no question at all.
- **maybe** (the agent asks in one line, never drafts unasked): only in CC; sent to more than five
  people; someone they have never written to; a list or Bcc email with a question; over 30 days
  old; a personal email with no question from someone they write to; an automatic-looking address
  they wrote to that asks something.
- **reply** (the agent offers a draft): a question or request to them, in To, from someone they
  write to or in a conversation they took part in.

Before drafting the agent always reads `mail_thread`, answers the newest message in the light of
the earlier ones, summarises a long conversation first, writes in the person's voice and language,
and leaves a gap in [brackets] for anything only the person knows.

**The eval** (`evals/mail_replies/`, see `evals/README.md`): 16 made-up threads with the expected
decision (reply, no reply, ask) and the points a good draft covers.
`tests/unit/test_mail_reply_facts.py` checks the facts over them without a model; the model eval is
`scripts/logos-eval --only mail`.

## Connecting a mailbox

**In the dashboard (F40, 23 Nov 2026; F43, 27 Nov):** Settings › Email. Each person connects only
their own mailboxes (their username is the `mail.person` of their agent): one card per mailbox
(how it is doing, its private senders, Disconnect) and **+ Add another mailbox**. Connecting the
same address again renews its login and keeps its private senders. Nothing comes back to the
browser but the addresses; each login goes to `~/.config/logos/mail/<person>/<id>.json` (0600)
and the mail service restarts with it (`gateway/mail_connect.py`). Disconnect removes that file
and, if no mailbox is left, stops the service.

- **Gmail:** the page links to Google's 2-Step Verification and App passwords pages; the person
  pastes the 16 letters and Logos checks them with a real read-only sign-in before keeping them.
  Revoking the app password cuts Logos off at once.
- **Outlook.com / Hotmail:** ONE app registration for the whole install (Microsoft turned off
  password sign-in for personal accounts in September 2024). Whoever manages Logos sets it up once;
  the page walks them through it:
  1. entra.microsoft.com, App registrations, New registration, name `Logos mail`,
     **Personal Microsoft accounts only**, no redirect URI.
  2. Authentication: **Allow public client flows: Yes**.
  3. API permissions, Microsoft Graph, Delegated: `Mail.Read`, `offline_access` (read only). A
     registration made before 21 Nov 2026 asked for write and send too: remove `Mail.ReadWrite` and
     `Mail.Send` there.
  4. Paste the Application (client) ID into Settings › Email (kept in `outlook-app.json`).

  Then anyone presses **Get a sign-in code**, opens microsoft.com/devicelogin and types the code; the
  dashboard waits for the sign-in in the background. Microsoft renews the token by itself; if it is
  unused for 90 days, connect again.

**From the server** (the same files): `scripts/logos-mail connect greg --outlook --address <address>`
(uses the Logos app unless `--client-id` is given) or `scripts/logos-mail connect bere --gmail --address <address>`.

Then `scripts/logos-agent up <agent>` (or a deploy) starts the service. `scripts/logos-mail status`
shows every mailbox and the last refresh; the dashboard's Models screen shows the service.

## From one mailbox each to several (F43)

Nothing to do by hand, no reconnecting. The first time the new code reads a person's mailboxes
(the deploy's `logos-agent up`, or opening Settings › Email), their old `<person>.json` moves to
`<person>/<id>.json`, marked `carried_over`. When the service starts with it, the mail it had
already copied (kept per person before) is given to that mailbox, with when it was last refreshed,
and an Outlook mailbox keeps Microsoft's rotated token (`outlook-<person>.token` becomes
`outlook-<id>.token`). The agents get `mail_thread` with the new sandbox rule, so each mail agent
is restarted by the deploy.
