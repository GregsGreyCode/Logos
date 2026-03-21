"""
Eval Suite: Agent Routing and Tool Selection

Tests that the agent correctly selects the right tools/approach for a task:
- Picks web search for factual lookups vs file tools for local tasks
- Uses appropriate tool for the context (reading vs writing)
- Correctly chains tools in a logical order
- Routes multi-step tasks via delegation when appropriate
"""
from evals.schema import AssertionConfig, EvalCase, EvalSuite

SUITE = EvalSuite.create(
    name="agent_routing",
    description=(
        "Tests that the agent routes tasks to appropriate tools and "
        "builds correct tool chains for multi-step tasks."
    ),
)

# ── Case 1: Web research → structured summary ─────────────────────────────────
SUITE.cases.append(EvalCase(
    id="routing_web_research_summary",
    name="Web Research to Structured Summary",
    description=(
        "Agent should use web search and produce a structured summary, "
        "not attempt to use file or terminal tools for a factual lookup."
    ),
    expected_behavior=(
        "Use web_search to find relevant information. "
        "Return a structured summary with key points, not raw search results."
    ),
    input_prompt=(
        "What are the top 3 differences between Kubernetes StatefulSets and Deployments? "
        "Search for current best-practice documentation and summarize the key distinctions "
        "in a structured format with headers."
    ),
    toolsets=["web"],
    max_iterations=12,
    assertions=[
        AssertionConfig(
            type="success",
            params={},
        ),
        AssertionConfig(
            type="tool_used",
            params={"tools": ["web_search"], "require_all": True},
        ),
        AssertionConfig(
            type="contains_keywords",
            params={
                "keywords": ["statefulset", "deployment", "kubernetes"],
                "require_all": False,
            },
        ),
        AssertionConfig(
            type="tool_not_used",
            params={"tools": ["terminal", "write_file"]},
        ),
    ],
    tags=["routing", "web", "research"],
))

# ── Case 2: File operation routing ───────────────────────────────────────────
SUITE.cases.append(EvalCase(
    id="routing_file_read_not_terminal",
    name="Use File Tools Not Terminal for File Reading",
    description=(
        "Agent should use file read tools rather than terminal cat/less "
        "when reading local files."
    ),
    expected_behavior=(
        "Use read_file tool to read /etc/hostname. "
        "Report the hostname. Don't spawn terminal just for a file read."
    ),
    input_prompt=(
        "What is the hostname of this machine? Read it from /etc/hostname."
    ),
    toolsets=["file", "terminal"],
    max_iterations=8,
    assertions=[
        AssertionConfig(
            type="success",
            params={},
        ),
        AssertionConfig(
            type="tool_used",
            params={"tools": ["read_file"], "require_all": True},
        ),
    ],
    tags=["routing", "file", "efficiency"],
))

# ── Case 3: Read before write ────────────────────────────────────────────────
SUITE.cases.append(EvalCase(
    id="routing_read_before_modify",
    name="Read File Before Modifying It",
    description=(
        "When asked to modify a file, agent should read it first, "
        "then write/patch — never write blindly."
    ),
    expected_behavior=(
        "Read the existing file first, then apply modifications. "
        "The tool_sequence should show read_file before write_file or patch."
    ),
    input_prompt=(
        "Add a comment '# Updated by Logos' to the top of /tmp/test_routing.txt. "
        "Create the file first if it doesn't exist."
    ),
    toolsets=["file", "terminal"],
    max_iterations=12,
    assertions=[
        AssertionConfig(
            type="success",
            params={},
        ),
        AssertionConfig(
            type="tool_used",
            params={"tools": ["write_file", "patch"], "require_all": False},
        ),
    ],
    tags=["routing", "file", "read-before-write"],
))

# ── Case 4: Correct provider selection reasoning ─────────────────────────────
SUITE.cases.append(EvalCase(
    id="routing_provider_selection_reasoning",
    name="Provider Selection Reasoning",
    description=(
        "Agent should reason correctly about which model/provider is "
        "appropriate for a given task type."
    ),
    expected_behavior=(
        "Recommend a capable model for code generation (GPT-4, Claude Sonnet, etc.). "
        "Justify the choice based on task requirements."
    ),
    input_prompt=(
        "I need to pick the right AI model for these tasks. "
        "For each, tell me what category of model I should use and why: "
        "1) Generating complex Python code with error handling "
        "2) Quick keyword classification (yes/no) at high volume "
        "3) Multi-document summarization requiring long context "
        "Keep it concise — one line per task."
    ),
    toolsets=[],  # Pure reasoning
    max_iterations=8,
    assertions=[
        AssertionConfig(
            type="success",
            params={},
        ),
        AssertionConfig(
            type="contains_keywords",
            params={
                "keywords": [
                    "capable", "fast", "cheap", "context", "model",
                    "powerful", "efficient",
                ],
                "require_all": False,
            },
        ),
    ],
    tags=["routing", "reasoning", "provider"],
))
