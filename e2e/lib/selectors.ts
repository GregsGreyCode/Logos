/**
 * Centralized selector map for the Logos UI.
 *
 * When data-testid attributes are added to the app, update here.
 * All tests import from this file — never hardcode selectors in tests.
 */

// ── Login page ──────────────────────────────────────────────────────────────
export const login = {
  usernameInput: "#identifier",
  passwordInput: "#password",
  submitButton: 'button[type="submit"]',
  errorMessage: "p.text-red-400",
} as const;

// ── Main navigation ─────────────────────────────────────────────────────────
export const nav = {
  agentsTab: 'button:has-text("Agents")',
  chatsTab: 'button:has-text("Chats")',
  settingsTab: 'button:has-text("Settings")',
  adminTab: 'button:has-text("Admin")',
  accountMenu: 'button:has-text("Admin"):last-of-type', // top-right dropdown
} as const;

// ── Chat ────────────────────────────────────────────────────────────────────
export const chat = {
  textarea: 'textarea[autocomplete="off"]',
  sendButton: 'button:has-text("Send")',
  newChatButton: 'button:has-text("New Chat"), button:has-text("New Topic")',
  addAgentButton: 'button:has-text("Add Agent")',
  agentNameHeader: ".text-base.font-semibold",
  messageContainer: ".space-y-4",
  // STAMP chips — use the bold letter as anchor
  stampS: 'button:has(span.font-bold.text-indigo-500)',
  stampM: 'button:has(span.font-bold)',
  stampP: "span:has(span.font-bold.text-emerald-600)",
  // Platform filters
  filterWeb: 'button:has-text("Web")',
  filterTG: 'button:has-text("TG")',
  filterDC: 'button:has-text("DC")',
  // Status bar
  statusBar: ".text-xs.text-gray-700",
  // Error card (typed errors from agent)
  errorCard: '[class*="border-l-4"]',
  retryButton: 'button:has-text("Retry")',
} as const;

// ── Agents page ─────────────────────────────────────────────────────────────
export const agents = {
  createButton: 'button:has-text("Create Agent")',
  cancelButton: 'button:has-text("Cancel")',
  nameInput: 'input[placeholder*="Agent name"]',
  soulSelect: 'select:visible >> nth=0',
  modelSelect: 'select:visible >> nth=1',
  descriptionInput: 'input[placeholder*="does this agent"]',
  toolsetsSummary: 'details summary:has-text("Toolsets")',
  toolsetCheckbox: 'details:has(summary:has-text("Toolsets")) input[type="checkbox"]',
  submitCreate: 'button:has-text("Create Agent"):visible >> nth=-1', // last visible
  worldCanvas: "#agent-world",
  agentCard: ".grid .rounded-xl.cursor-pointer",
  editButton: 'button:has-text("Edit")',
  deleteButton: 'button:has-text("Delete")',
  chatButton: 'button:has-text("Chat")',
} as const;

// ── Settings ────────────────────────────────────────────────────────────────
export const settings = {
  inferenceTab: 'button:has-text("Inference")',
  routingTab: 'button:has-text("Routing")',
  toolsTab: 'button:has-text("Tools")',
  proposalsTab: 'button:has-text("Proposals")',
  // Inference
  addProviderButton: 'button:has-text("Add Provider"):visible >> nth=0',
  scanButton: 'button:has-text("Scan")',
  registerButton: 'button:has-text("Register")',
  pingButton: 'button:has-text("Ping")',
  // Routing
  newProfileButton: 'button:has-text("New Profile")',
  addRuleButton: 'button:has-text("Add Rule")',
  saveRulesButton: 'button:has-text("Save Rules")',
  // Tools
  mcpSection: 'text=MCP Tool Servers',
  servicesSection: 'text=Services',
} as const;

// ── Admin ───────────────────────────────────────────────────────────────────
export const admin = {
  usersTab: '.border-b button:has-text("Users")',
  securityTab: '.border-b button:has-text("Security")',
  workflowsTab: '.border-b button:has-text("Workflows")',
  runsTab: '.border-b button:has-text("Runs")',
  auditTab: '.border-b button:has-text("Audit Log")',
  approvalsTab: '.border-b button:has-text("Approvals")',
  // Users
  newUserButton: 'button:has-text("New User")',
  userTable: "table",
  // Security
  newPolicyButton: 'button:has-text("New Policy")',
  // Workflows
  buildButton: 'button:has-text("Build")',
  importButton: 'button:has-text("Import JSON")',
} as const;
