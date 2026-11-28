# People: adding and removing someone

People › Add a person (backlog F38) does in one flow what used to be done by hand for each new person:
a dashboard account, an agent definition, `scripts/logos-agent up`, a Matrix account, `scripts/logos-calendar add-person`,
and joining the household agent. Each household runs its own Logos (decision D2), so this is used inside that household's own install.

Code: `gateway/people.py` (the work), `gateway/people_api.py` (the endpoints), `gateway/html/invite.html` (the page the person
opens), `gateway/ui/people.js` (the screens). Tests: `tests/unit/test_people.py`, `tests/ui/checks/people_admin.mjs`.

## Adding someone

1. **An admin invites them.** People › Add a person: their name, a username (lowercase; it is also their agent's name, their
   chat name and their calendar name), an optional email, and what to set up: chat in Element X, their own calendar, joining
   the household agent, and whether their agent may read their email once they connect it. Logos makes their account in the
   *pending* state (it cannot sign in) and shows a link **once**: `https://<logos>/invite#<secret>`. Send it to them privately.
   It works once, for 1 to 14 days (7 by default). A lost link: open their page and make a new one (the old one stops working),
   or cancel the invitation.
2. **They open the link and choose a password** (at least 10 characters). Their account becomes active and they are signed in.
   The same password makes their chat account (`@<username>:<server>`) and their calendar account, while they wait; it is
   not kept anywhere else. If one of those fails, the rest still happens and their page says what failed.
3. **In the background** (a minute or two):
   - their agent's definition is written from the template (below), outside the repo;
   - the agent gets its chat account, and a direct chat with the person, encrypted, is made from the agent's side: they find
     an invitation to it in Element X. It is the agent's home room, where reminders and briefings go;
   - the agent is started (`up`, without touching the model server);
   - if they joined the household agent and it runs on this machine, it is restarted so it answers them and sees their busy
     times. If it does not run here, it knows them from its next start.

Their page (People › their name) shows each step. A failed step can be run again with **Run the setup again**; what is done
is kept. A restart of Logos in the middle marks the step as interrupted.

Changing their dashboard password later does not change their chat or calendar password.

### The template

The new agent is like the first people's agents (`agents/greg.yaml`): the general soul, the noticeboard, voice notes, web
search, the nightly tidy-up, the weekly digest, the morning briefing (with chat and a calendar), owners `[<username>]` and
chat only with them. The model, image and timezone are copied from the household agent. Not included: the house speaker, the
lights, and the Claude advisor; add them to the definition by hand if wanted.

### Where it is kept

The repo is public, so nothing that names a person is written into it. Definitions made here live in
`~/.logos/people/agents/<name>.yaml` (0600, folder 0700; `LOGOS_PEOPLE_DIR` moves the whole folder). The runtime reads that
folder beside `agents/`: `scripts/logos-agent up <name>` works for them as for anyone, and a `<name>.local.yaml` beside the
definition is laid over it. An agent in `agents/` always wins over one of the same name there.

Joining the household agent is not an edit of `agents/household.yaml`: when the household agent's definition is loaded,
every person here with `person.household: true` is added to its `owners`, its `matrix.allowed_users`, and (with a calendar)
its `calendar.free_busy`.

## Removing someone

Their page › Remove (type their username to confirm). Not possible for yourself, for an admin, or for people whose agent is
defined in the repo (set up by hand, removed by hand). Then, in the background:

- their account is suspended, their sessions end at once, and open invitations are cancelled;
- their agent is stopped and its two volumes (home: memories, chats, settings; brain) are archived into
  `~/.logos/people/graveyard/<name>/` (each archive is read back before anything is deleted), then the volumes are deleted;
  their definition moves there too;
- their calendar login is removed and nobody sees their calendar any more (it stays in the calendar volume);
- their mailbox login is deleted (they connect it again after a restore);
- the shared services forget their agent's keys, and the household agent is restarted without them.

For **30 days** their page has **Restore**: volumes, definition, calendar login and account (with the password they had)
come back, the agent is started and the household agent told. After 30 days Logos deletes everything for good, checking
every hour: the archives, their calendar, their dashboard account, and it closes their chat account and their agent's.

Until then their chat account stays open: they can still read rooms they are in (the household room) but no agent answers
them. Remove them from shared rooms in Element X if that matters.

## Who can see what

Each person's page lists the agents they use (and who shares them), what their own agent can reach, and who can see what is
theirs: their agent's chats and brain (only them; admins get no exception), their calendar (their agent, the other people's
agents and phones read only, the household agent busy times only), their email (their agent only, read only). A person can
open their own page; only admins open other people's.

## Admin actions

The People screen lists who invited, changed, removed, restored or deleted whom (`audit_logs`). No secret is ever written
there or in the logs: the invitation's secret exists only in the admin's browser and the link; Logos keeps its SHA-256.

## Security, in short

- Admin only (`manage_users`), with the CSRF check, for everything but the invitation page and its two endpoints.
- The invitation secret is 256 bits, single use (marked used in the same database statement that checks it), expiring,
  stored only as a hash, and travels in the address's `#fragment` (never sent to a server, so not in any log or Referer) and
  then in a JSON body. The invitation page removes it from the address bar at once and sends no Referer. Its endpoints are
  rate limited and accept JSON only.
- Usernames that could pass for something else are refused: `*-agent` (agents' chat accounts), `logos*`, `household`, test
  agents, and a name whose old agent volumes still exist or that is in the graveyard.
- A taken chat name is refused before anything is made; an existing chat account is never taken over.
- The middleware refuses a session whose account is no longer active, so removing someone takes effect immediately.
- Adding or removing someone never loads or reloads the model on the AI computer.

## Backups

The nightly export (`scripts/logos-backup-export`) has their brains and memories like everyone's (it finds agents by their volumes) and the definitions as `people-agents.tar.gz`. The graveyard is covered by the VM backup only.
