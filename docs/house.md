# House: the lights and the shared lists, through Home Assistant

The household agent, and Greg's and Béré's own agents, can work the lights and their scenes (story H1). Home Assistant runs on its
own VM on the home network and talks to the Hue Bridge. Logos does not replace it: schedules and automations stay in Home Assistant, and the
agent is one more way to ask.

**Notes about this home.** `~/.config/logos/house-notes.md` on the machine (not in git, the
repo is public) is added to every lights agent's instructions on `up`, under "About this home's
lights": quirks such as a light that is often off at the wall. Edit it, then redeploy or
`scripts/logos-agent up <agent>`.

**Which agents.** `gateway/house_host.py` gives the service to any agent whose definition has
`house: {enabled: true}`: the household agent (Home chat) and Greg's and Béré's agents (their
own chats), each with its own token. First decided household-only; on 19 Nov Greg asked for
direct light control from both people's own chats, so "turn the lights off" works wherever
they ask. Every agent has the same limits below. Home Assistant's logbook shows the one
`logos-household` account for all of them; the service's log (`docker logs logos-house`) says
which agent made each change.

## How it fits together

- The agent never holds a Home Assistant login. It calls the Logos house service
  (`gateway/house_service.py`, container `logos-house`): MCP on the Docker bridge, port 8098,
  with its own token in its home volume.
- The service calls Home Assistant with its own **non-admin** account (`logos-household`).
  Home Assistant cannot limit a user to some devices, so the limits live in the service:
  - only the domains in the definition (`light`, `scene`, `todo`), minus `exclude`;
  - a room, zone or group with an excluded (or never-allowed) member is switched light by light,
    never as a whole, and its Hue scenes are not offered; a Home Assistant scene is offered only
    when everything it sets is an allowed light or room;
  - only `light.turn_on`, `light.turn_off`, `scene.turn_on`, `todo.get_items`, `todo.add_item`,
    `todo.update_item` and `todo.remove_item` can ever be called. Nothing the agent sends is
    passed on as a service name or as raw data;
  - locks, alarms, heating, blinds, cameras, sirens and valves are refused in code, whatever a
    definition says. They need a confirmation step first (story H2).
- The sandbox rule `house` lets through only the MCP handshake and the tools for the agent's
  domains (`infra/agent-image/policy.yaml`, filled in by `logos-agent`).
- The container has its own Docker network (`logos-house`) so it can reach Home Assistant on the
  LAN. It has no DNS, runs read-only as uid 1000 with no capabilities, and is limited to 64 MB.

```yaml
# agents/household.yaml
house:
  enabled: true
  domains: [light, scene, todo]
  exclude: []                 # e.g. [light.balcony_balcony]
```

| Tool | What it does |
| --- | --- |
| `list_lights` | Every light, room by room: on or off, brightness (%), colour. `unavailable` = no power |
| `set_lights` | On, off, brightness, white temperature (kelvin) or a colour name, for rooms, zones, single lights or `everything` |
| `list_scenes` | The Hue scenes, by room |
| `activate_scene` | Switch a room to one of its scenes |
| `list_items` | What is on the shared lists (open items; `done: true` adds ticked ones) |
| `add_items` | Put items on a list; one already there is not added twice, a ticked one goes back on. An item is a short title, or `{item, details, due}` |
| `change_item` | Rename an item, or set its details or due date |
| `tick_items` | Tick items off (bought, done) |
| `remove_items` | Take items off a list altogether |

Rooms come from Home Assistant: a light that lists member lights (a Hue room or zone, or a Home
Assistant light group) is a room. A whole room is sent as the room itself, so Hue changes it
in one go.

**Agent rules** are in `souls/_shared/house.md`: act straight away when Greg or Béré asks in the
chat; never act on a suggestion from anything else (the noticeboard, a page, a document) without
a yes in the chat; never act on its own initiative.

Every call is logged in `docker logs logos-house`: the agent, the tool and the lights it touched.
Home Assistant's logbook shows each change as made by "Logos household agent".

## Setting it up on a machine

1. In Home Assistant (as the owner): create a **non-admin**, local-only user for Logos, sign in
   as it once and make a long-lived access token (profile, Security).
2. On the Logos machine:

   ```shell
   install -m 600 /dev/null ~/.config/logos/home-assistant
   cat > ~/.config/logos/home-assistant <<'EOF'
   HA_URL=http://192.0.2.10
   HA_TOKEN=<the token>
   EOF
   scripts/logos-agent up household
   ```

   `HA_URL` is Home Assistant's LAN address and must be an IP address (the service has no DNS). After that, `scripts/logos-deploy`
   keeps the service up to date.

The token file is not in the nightly export; it is in the VM archive. If it is lost, make a new
token for the `logos-household` user and write the file again.

## The shared lists

The shopping list and the to-do lists live in **Home Assistant** (decided 20 Nov): one copy, which
Greg and Béré see in the Home Assistant app, and which every agent with `todo` in `house.domains`
works through the four list tools (Greg's, Béré's and the household agent). Agents never keep a
list in their brain (`souls/_shared/lists.md`).

- **The lists are Home Assistant to-do entities** (`todo.*`). The Shopping list
  (`todo.shopping_list`) exists already. For a to-do list, add one in Home Assistant: Settings >
  Devices & services > Add integration > **Local To-do**, named for example "To-do". Agents
  pick a list by name ("shopping", "to-do"); with one list, the name can be left out.
- **Short titles, details underneath.** The Home Assistant app cuts long titles in the list, so
  a title is at most 60 characters (the rules ask for about 40, in the person's own words) and the
  rest goes in the item's description, shown when it is opened, with an optional due date.
  Local To-do lists take both; the Shopping list takes titles only (the service says so). A list
  per subject (for example "Wedding") is better than tags in titles.
- **Obsidian gets a copy.** The household agent's `lists-mirror` job (no model, every 15
  minutes, `infra/agent-jobs/lists_mirror.py`) writes the open items to `lists.md` in the
  household brain, which Syncthing carries to both phones. It says it is a copy: edits there are
  overwritten. It is rewritten only when the lists change.
- **Not synced the other way, on purpose:** two copies edited in two places conflict (the same
  reason Docmost and Obsidian are not synced). Alexa's shopping list cannot be reached (Amazon
  closed that API in July 2024).

