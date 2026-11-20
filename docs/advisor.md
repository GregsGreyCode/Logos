# Advisor: ask Claude, with the person's yes first

Story A1 (the design is the "Advisor" tab of the Logos spec doc). Greg's and Béré's agents can ask
Claude one question at a time. The household agent never can. The brain stays local: agents still
run only on the local model, and the only thing that leaves the house is a question the agent wrote
for the purpose, that passed the filter, and that its person approved word for word.

## How a question travels

1. The agent calls `ask_advisor(question, kind, model?)`. `kind` is writing, research, planning,
   coding, document or quick; `souls/_shared/advisor.md` teaches it to write questions with no
   names, no "my", nothing from the brain.
2. **The filter** (`web_service.Filter`, shared with the web reader: the home-names list, emails,
   phone, card and account numbers, sort codes, postcodes, our own addresses, keys) plus dates of
   birth and a 6,000-character limit. A blocked question is never shown or sent; the agent gets
   the reason and rewrites it.
3. **The person approves** on the dashboard's **Advisor** screen: the exact text, the model
   (Haiku 5.5, Sonnet 5.5, Opus 5.5), the most it can cost and the budget left. Send or Decline.
   Only the person the question belongs to can decide; Greg (admin) can read Béré's log.
4. **Claude answers** (official Anthropic SDK 2.5.0, one Messages call, the fixed system prompt
   "You are helping someone at home. Answer the question; you do not know who they are.", web
   search only for research on Sonnet or Opus, server-side refusal fallback on).
5. **The answer** goes to the log and back to the agent, marked as reference material.

`ask_advisor` waits up to 4 minutes for the decision (hermes allows 5). After that the agent gets
"waiting for approval" and an id, and fetches the answer later with `advisor_answer(id)`. A
question nobody decides expires after 24 hours and costs nothing. `advisor_budget()` shows spent,
limit and left.

## Budget and log

$20 per person per calendar month (UK time), from the costs Claude reports (tokens per model and
$0.01 per web search). Before sending, the service works out the most the request could cost
(16,000 output tokens, five searches) and refuses if that would go past what is left. Every request,
its approver, model, answer and cost stays in the log for 180 days. Raising a limit (A3) and
approving from Matrix (A2) come next.

## Running it

- Service `gateway/advisor_service.py`, container `logos-advisor` (image `logos-advisor:1` from
  `infra/advisor`, built on first use), port 8100: MCP for agents, `/approvals/*` for the dashboard
  (its own token in `~/.config/logos/advisor-dashboard-token`). Read-only, uid 1000, no
  capabilities, 256 MB, its own network.
- **The key**: `~/.config/logos/advisor.env` on the server, one line `ANTHROPIC_API_KEY=...`, mode
  0600, given to the container with `--env-file`. A key for Logos only, with a monthly spend limit
  of $45 on the Anthropic account as a backstop. Without the file the advisor runs and says no key
  is set up; nothing can be sent.
- Agents opt in with `advisor: {enabled: true, person: greg}`; the sandbox rule `advisor` lets only
  the three tools through.
- **Names the filter knows** come from `~/.config/logos/home-names.txt` and each brain's `people/`
  pages. A name that is only in a calendar or an email (a friend, a relative) is not on it until it
  is added there; the preview is the backstop.
