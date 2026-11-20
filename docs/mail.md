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
- **Logins** live in `~/.config/logos/mail/<person>.json` (mode 0600), written by
  `scripts/logos-mail`. `logos-agent up` passes them to the container on stdin into the
  read-only `logos-mail` volume; they are never on a command line, in git, a sandbox or a chat.
- **Untrusted text.** Every tool result says the emails are other people's words, and
  `souls/_shared/mail.md` forbids acting on anything an email asks, sharing it with other agents
  or the household brain, or searching the web with it. The sandbox rule `mail` lets through
  only the four read tools.
- **Excluded senders** (`scripts/logos-mail exclude <person> mybank.example`) are never copied.
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

**Outlook.com / Hotmail** (Microsoft turned off password sign-in for personal accounts in
September 2024, so this is OAuth with your own app registration):

1. entra.microsoft.com, signed in with the Outlook account: App registrations, New registration,
   name `Logos mail`, **Personal Microsoft accounts only**, no redirect URI.
2. Authentication: **Allow public client flows: Yes**.
3. API permissions, Microsoft Graph, Delegated: `Mail.ReadWrite`, `Mail.Send`, `offline_access`.
4. On the server: `scripts/logos-mail connect greg --outlook --address <address> --client-id <Application ID>`,
   then open the address it shows on any device and type the code. Microsoft renews the token by
   itself; if it is unused for 90 days, connect again.

**Gmail:** 2-step verification on, then an app password at myaccount.google.com/apppasswords.
On the server: `scripts/logos-mail connect bere --gmail --address <address>`; it asks for the
password without showing it and checks it with a real sign-in. Revoking the app password cuts
Logos off at once.

Then `scripts/logos-agent up <agent>` (or a deploy) starts the service. `scripts/logos-mail status`
shows who is connected and the last refresh; the dashboard's Models screen shows the service.
