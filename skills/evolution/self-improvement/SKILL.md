# Self-Improvement Skill

**Skill ID:** `evolution/self-improvement`
**Trigger:** Scheduled (configurable interval, default 1 week) or manual via Evolution tab
**Agent:** All agents (required execution)

## Purpose

Analyse the platform's own codebase (the user's fork, configured in Evolution Settings), identify a concrete improvement opportunity, and submit a structured proposal for human review via the Evolution API.

## Source of truth: the user's fork

Each Logos deployment works against **the user's own fork** of the Logos repository. The fork URL is stored in Evolution Settings (`git_remote_url`). The agent clones / pulls that fork to read the latest code, and when a proposal is accepted, opens a PR against it. This means:

- Every user gets their own evolution history and code lineage.
- Changes are isolated — one deployment's improvements don't affect others.
- The user can pull upstream improvements from the canonical repo at their own pace.

## Instructions

You are performing a scheduled self-improvement analysis of the Logos platform.

### Step 1 — Understand the codebase

Read the repository to understand current architecture, recent changes, and known pain points. Focus on:
- Files modified in the last 30 days (use `git log --since="30 days ago" --name-only`)
- Any TODO/FIXME/HACK comments in source files
- Error patterns visible in recent agent runs (query `/runs` if available)
- Complexity hotspots (large functions, deeply nested logic)

### Step 2 — Identify an improvement

Choose **one** specific, actionable improvement. Prefer:
- Bug fixes or reliability improvements over cosmetic changes
- Changes that affect frequently-used paths
- Improvements that can be implemented as a single coherent diff

Do **not** propose:
- Changes already covered by open proposals (check `/evolution/proposals?status=pending`)
- Vague "refactor everything" suggestions
- Changes to security-critical auth code unless you have high confidence

### Step 3 — Prepare the proposal

Draft:
- **title**: One sentence, verb-first (e.g. "Cache model health checks to reduce LAN polling")
- **summary**: 2–4 paragraphs covering: what the problem is, how you propose to fix it, expected benefit, and risks
- **diff_text**: A unified diff of the proposed change (or `null` if exploratory)
- **target_files**: List of files that would be modified
- **proposal_type**: One of `improvement`, `bugfix`, `refactor`, `new_feature`

### Step 4 — Submit

POST to `/evolution/proposals` with the proposal JSON. The platform will notify the user for review.

### Step 5 — Handle questions

If a proposal enters `questioned` status, read `question_text` and POST your answer to `/evolution/proposals/{id}/answer`. The proposal returns to `pending` for re-review.

## Constraints

- Maximum `max_pending` proposals outstanding at once (read from `/evolution/settings`)
- Do not submit duplicate proposals for the same issue within 30 days
- The final decision (accept/decline) rests entirely with the human reviewer
- If the user's git fork URL is configured in settings, the accepted diff should be applied to a branch named `evolution/{proposal-id}` and a PR opened against the configured base branch

## Output

After submitting, respond with a brief summary:
- What improvement was identified and why
- What was proposed
- The proposal ID for tracking
