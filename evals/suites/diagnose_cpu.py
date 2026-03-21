"""
Eval Suite: System Diagnostics (CPU/Process Investigation)

Tests that the agent correctly investigates high CPU usage by:
- Using terminal/process tools to inspect running processes
- Identifying likely culprits
- Providing a structured diagnosis with actionable steps

This suite assumes the agent has access to terminal tools.
"""
from evals.schema import AssertionConfig, EvalCase, EvalSuite

SUITE = EvalSuite.create(
    name="diagnose_cpu",
    description=(
        "Tests that the agent investigates high CPU usage correctly: "
        "uses process inspection tools, identifies culprits, and gives actionable steps."
    ),
)

# ── Case 1: Basic CPU investigation ──────────────────────────────────────────
SUITE.cases.append(EvalCase(
    id="cpu_basic_investigation",
    name="Basic CPU Investigation",
    description=(
        "Agent should run ps/top commands to identify high-CPU processes."
    ),
    expected_behavior=(
        "Run 'ps aux --sort=-%cpu' or equivalent, identify top process, "
        "provide name + PID + CPU%, suggest investigation steps."
    ),
    input_prompt=(
        "CPU on this machine is running at 90%+. "
        "Use the terminal to identify which processes are consuming the most CPU. "
        "Report the top 5 processes by CPU usage with their PIDs and names."
    ),
    toolsets=["terminal"],
    max_iterations=15,
    assertions=[
        AssertionConfig(
            type="success",
            params={},
        ),
        AssertionConfig(
            type="tool_used",
            params={"tools": ["terminal"], "require_all": True},
        ),
        AssertionConfig(
            type="contains_keywords",
            params={
                "keywords": ["cpu", "process", "pid"],
                "require_all": False,
            },
        ),
    ],
    tags=["diagnostics", "terminal", "system"],
))

# ── Case 2: Process investigation with output parsing ────────────────────────
SUITE.cases.append(EvalCase(
    id="cpu_identify_and_explain",
    name="CPU Identify and Explain",
    description=(
        "Agent should identify a high-CPU process and explain what it might be doing."
    ),
    expected_behavior=(
        "Identify a specific process, look it up, explain likely cause, "
        "and suggest whether it's safe to kill or requires investigation."
    ),
    input_prompt=(
        "The system load average is very high. "
        "Investigate using terminal commands: check current processes, load average, "
        "and the top CPU-consuming process. "
        "Explain what you find and whether any action is needed."
    ),
    toolsets=["terminal"],
    max_iterations=20,
    assertions=[
        AssertionConfig(
            type="success",
            params={},
        ),
        AssertionConfig(
            type="tool_used",
            params={"tools": ["terminal"], "require_all": True},
        ),
        AssertionConfig(
            type="contains_keywords",
            params={
                "keywords": ["load", "process", "cpu", "investigate"],
                "require_all": False,
            },
        ),
    ],
    tags=["diagnostics", "system", "explanation"],
))

# ── Case 3: Prometheus metrics query (requires Prometheus access) ─────────────
SUITE.cases.append(EvalCase(
    id="cpu_prometheus_query",
    name="CPU Prometheus Query",
    description=(
        "Agent should construct a PromQL query to investigate CPU metrics. "
        "Designed for environments where Prometheus is accessible."
    ),
    expected_behavior=(
        "Construct a PromQL query like "
        "'100 - avg(rate(node_cpu_seconds_total{mode=\"idle\"}[5m])) * 100' "
        "to measure CPU utilization."
    ),
    input_prompt=(
        "I need to diagnose high CPU on a host being monitored by Prometheus. "
        "Without running any terminal commands, describe the exact PromQL queries "
        "I should use to: "
        "1) Get overall CPU utilization percentage "
        "2) Find which process (job) is consuming the most CPU "
        "3) Check if there's been a spike in the last hour. "
        "Provide the exact query strings I can paste into Grafana."
    ),
    toolsets=[],  # No tool access — pure reasoning
    max_iterations=10,
    assertions=[
        AssertionConfig(
            type="success",
            params={},
        ),
        AssertionConfig(
            type="contains_keywords",
            params={
                "keywords": ["promql", "node_cpu", "rate", "avg"],
                "require_all": False,
            },
        ),
        AssertionConfig(
            type="output_matches",
            params={
                "pattern": r"node_cpu|cpu_seconds|cpu_utilization|rate\(",
                "case_insensitive": True,
            },
        ),
    ],
    tags=["prometheus", "observability", "reasoning"],
))
