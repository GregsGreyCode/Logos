# Logos E2E Tests

Playwright-based functionality tests for the Logos web UI.

## Quick Start

```bash
cd e2e
npm install
npx playwright install chromium
cp .env.example .env
# Edit .env with your credentials and BASE_URL
npx playwright test          # all tests
npx playwright test --grep @smoke      # smoke only
npx playwright test --grep @regression # full regression
npx playwright test --ui     # interactive UI mode
```

## Project Structure

```
e2e/
  .auth/              # Saved session state (gitignored)
  lib/
    selectors.ts      # Centralized selector map — update here when UI changes
    helpers.ts        # Reusable actions (login, sendMessage, createAgent, etc.)
  tests/
    auth.setup.ts     # Pre-test auth setup (saves storageState)
    smoke.spec.ts     # Quick deploy checks (~30s)
    auth.spec.ts      # Login/logout/session tests
    agents.spec.ts    # Agent CRUD, world canvas
    chat.spec.ts      # Chat messaging, STAMP chips
    settings.spec.ts  # Inference, routing, tools, proposals
    admin.spec.ts     # Users, security, workflows, runs, audit, approvals
  playwright.config.ts
  .env.example
```

## Test Strategy

### Smoke Suite (`@smoke`)
- Runs on every deploy
- Verifies all tabs load, nav works, no console errors
- ~30 seconds, no inference backend needed

### Regression Suite (`@regression`)
- Runs on PRs and release candidates
- Full CRUD flows, form validation, error handling
- Chat tests that need an inference backend are auto-skipped if unavailable

### What's Mocked vs Real
- **Auth**: Real — tests hit the actual login endpoint
- **Agent CRUD**: Real — creates/deletes via API and UI
- **Chat messages**: Real if backend is available, skipped otherwise
- **Settings/Admin**: Real — reads existing state, doesn't modify infra

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `BASE_URL` | Yes | Logos instance URL |
| `ADMIN_USERNAME` | Yes | Admin login username |
| `ADMIN_PASSWORD` | Yes | Admin login password |
| `LMSTUDIO_URL` | No | LM Studio endpoint for model tests |

## CI Integration

```yaml
# GitHub Actions example
e2e-tests:
  runs-on: ubuntu-latest
  steps:
    - uses: actions/checkout@v4
    - uses: actions/setup-node@v4
      with: { node-version: 20 }
    - name: Install
      working-directory: e2e
      run: |
        npm ci
        npx playwright install chromium --with-deps
    - name: Run smoke tests
      working-directory: e2e
      env:
        BASE_URL: ${{ secrets.LOGOS_URL }}
        ADMIN_USERNAME: ${{ secrets.LOGOS_USER }}
        ADMIN_PASSWORD: ${{ secrets.LOGOS_PASS }}
      run: npx playwright test --grep @smoke
    - uses: actions/upload-artifact@v4
      if: failure()
      with:
        name: playwright-report
        path: e2e/playwright-report/
```

## Recommended Test IDs for Logos

Add these `data-testid` attributes to `main_app.html` to make tests
resilient to CSS/layout changes:

### Navigation
- `data-testid="nav-agents"` — Agents tab button
- `data-testid="nav-chats"` — Chats tab button
- `data-testid="nav-settings"` — Settings tab button
- `data-testid="nav-admin"` — Admin tab button

### Chat
- `data-testid="chat-header"` — Chat header container
- `data-testid="chat-agent-name"` — Agent name in header
- `data-testid="stamp-s"` — Soul chip
- `data-testid="stamp-t"` — Tools chip
- `data-testid="stamp-m"` — Model chip
- `data-testid="stamp-p"` — Policy chip
- `data-testid="chat-input"` — Message textarea
- `data-testid="chat-send"` — Send button
- `data-testid="chat-new"` — New Chat/Topic button
- `data-testid="chat-message"` — Each message in the chat
- `data-testid="chat-error"` — Error card in chat
- `data-testid="chat-retry"` — Retry button on error card

### Agents
- `data-testid="agents-world"` — World canvas container
- `data-testid="agents-create-btn"` — Create Agent button
- `data-testid="agent-form-name"` — Name input in create form
- `data-testid="agent-form-soul"` — Soul select
- `data-testid="agent-form-model"` — Model select
- `data-testid="agent-form-desc"` — Description input
- `data-testid="agent-form-submit"` — Create Agent submit button
- `data-testid="agent-card-{id}"` — Individual agent card
- `data-testid="agent-edit-{id}"` — Edit button on agent card
- `data-testid="agent-delete-{id}"` — Delete button on agent card

### Settings
- `data-testid="settings-inference"` — Inference sub-tab
- `data-testid="settings-routing"` — Routing sub-tab
- `data-testid="settings-tools"` — Tools sub-tab
- `data-testid="provider-card-{id}"` — Cloud provider card
- `data-testid="machine-card-{id}"` — Local machine card
- `data-testid="machine-status-{id}"` — Machine health status indicator

### Admin
- `data-testid="admin-users"` — Users sub-tab
- `data-testid="admin-security"` — Security sub-tab
- `data-testid="user-row-{id}"` — User table row
- `data-testid="user-role-{id}"` — Role dropdown for user
- `data-testid="user-delete-{id}"` — Delete button for user
