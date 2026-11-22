# Mail: each person's agent reads their own email

Story E1 (read). Decided 20 Nov 2026: read, then drafts, then sending only after the person presses
**Send** on the dashboard (E2, E3, to come). Greg's and Béré's agents; the household agent never.

## How it fits together

- **The agent never holds a mail login.** It calls the Logos mail service
  (`gateway/mail_service.py`, container `logos-mail`) on the Docker bridge, port 8099, MCP, with its
  own token. The service answers for one mailbox per agent: its person's.
- **A local copy.** Every 5 minutes the service copies the last 90 days of Inbox and Sent into
  `logos-mail-state` (SQLite), and the tools read the copy. Nothing in this story can send,
  delete, move or mark mail: Gmail is read with `BODY.PEEK`, Outlook with GET requests only.
  A busy mailbox's first copy stops at 2,000 (Outlook) or 1,500 (Gmail) messages a folder; each
  sync then copies one more batch of older mail until the 90 days are in, and until then the tools
  say how far back the copy goes.
- **Logins** live in `~/.config/logos/mail/<person>.json` (mode 0600), written by
  `scripts/logos-mail`. `logos-agent up` passes them to the container on stdin into the
  read-only `logos-mail` volume; they are never on a command line, in git, a sandbox or a chat.
- **Untrusted text.** Every tool result says the emails are other people's words, and
  `souls/_shared/mail.md` forbids acting on anything an email asks, sharing it with other agents
  or the household brain, or searching the web with it. The sandbox rule `mail` lets through
  only the four read tools.
- **Excluded senders** (`scripts/logos-mail exclude <person> mybank.example`) are never copied, and
  mail from them already in the copy is deleted on the next sync after the restart.
- The container: its own network `logos-mail` (it needs the internet: Microsoft and Google),
  read only, uid 1000, no capabilities, 256 MB.

| Tool | What it does |
| --- | --- |
| `mail_new` | Newest Inbox emails (default the last 24 hours): sender, subject, preview |
| `mail_needs_reply` | Emails from people (not newsletters or notifications) with no reply sent in that thread since |
| `mail_search` | Words, a name or an address, in subject, sender, recipients and text |
| `mail_read` | One email in full (first 8,000 characters), attachment names only |

The morning briefing gets one line: how many emails wait for a reply, and the first senders.

## Connecting a mailbox

**In the dashboard (F40, 23 Nov 2026):** Settings › Email. Each person connects only their own
mailbox (their username is the `mail.person` of their agent). Nothing comes back to the browser but
the address; the login goes to `~/.config/logos/mail/<person>.json` (0600) and the mail service
restarts with it (`gateway/mail_connect.py`). The page also lists "Never read mail from" senders,
and Disconnect (which removes the file and, if no mailbox is left, stops the service).

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
shows who is connected and the last refresh; the dashboard's Models screen shows the service.
