"""
Logos Metrics Engine.

Computes structured observability metrics from existing runs, sessions,
and tool_sequence data in the SQLite state database.

Metrics exposed:
  Run stats     — total/completed/failed/stuck, avg/p50/p95 duration
  Tool stats    — call counts, failure rates, most/least used tools
  Provider stats — per-model/provider breakdown of runs and tokens
  Approval stats — approval/denial counts, denial rate
  Stuck runs    — runs in 'running' status for >1 hour

Prometheus compatibility:
  export_prometheus() returns Prometheus text format that can be scraped
  by node_exporter's textfile collector or a lightweight HTTP endpoint.

Usage:
  from metrics import MetricsEngine
  from hermes_state import SessionDB

  db = SessionDB()
  engine = MetricsEngine(db)
  print(engine.format_dashboard(days=7))
  print(engine.export_prometheus())
"""

from __future__ import annotations

import json
import time
from collections import Counter, defaultdict
from datetime import datetime
from typing import Any, Dict, List, Optional


def _pct(numerator: int, denominator: int) -> float:
    return round(numerator / denominator * 100, 1) if denominator else 0.0


def _percentile(sorted_values: List[float], p: float) -> float:
    """Return the p-th percentile of a sorted list (0-100)."""
    if not sorted_values:
        return 0.0
    idx = (p / 100) * (len(sorted_values) - 1)
    lo = int(idx)
    hi = lo + 1
    if hi >= len(sorted_values):
        return sorted_values[-1]
    frac = idx - lo
    return round(sorted_values[lo] * (1 - frac) + sorted_values[hi] * frac, 2)


class MetricsEngine:
    """
    Computes observability metrics from the Logos SQLite state database.

    All methods accept an optional ``days`` parameter (default 7) to window
    the analysis.  The underlying queries all run against the ``runs`` table
    and its JSON ``tool_sequence`` / ``approval_events`` columns.
    """

    STUCK_THRESHOLD_SECONDS = 3600  # 1 hour

    def __init__(self, db):
        self._db = db
        self._conn = db._conn

    # =========================================================================
    # Core data queries
    # =========================================================================

    def _get_completed_runs(self, cutoff: float) -> List[Dict]:
        cursor = self._conn.execute(
            """SELECT id, status, source, model, provider,
                      started_at, ended_at, input_tokens, output_tokens,
                      tool_sequence, approval_events, trigger_type
               FROM runs
               WHERE started_at >= ? AND status != 'running'
               ORDER BY started_at DESC""",
            (cutoff,),
        )
        rows = []
        for row in cursor.fetchall():
            r = dict(row)
            for field in ("tool_sequence", "approval_events"):
                if r.get(field):
                    try:
                        r[field] = json.loads(r[field])
                    except Exception:
                        r[field] = []
            rows.append(r)
        return rows

    def _get_stuck_runs_raw(self) -> List[Dict]:
        threshold = time.time() - self.STUCK_THRESHOLD_SECONDS
        cursor = self._conn.execute(
            """SELECT id, session_id, source, model, started_at,
                      user_message_preview
               FROM runs
               WHERE status = 'running' AND started_at < ?
               ORDER BY started_at ASC""",
            (threshold,),
        )
        return [dict(row) for row in cursor.fetchall()]

    # =========================================================================
    # Run stats
    # =========================================================================

    def run_stats(self, days: int = 7) -> Dict[str, Any]:
        """Aggregate run statistics for the last N days."""
        cutoff = time.time() - (days * 86400)
        runs = self._get_completed_runs(cutoff)

        total = len(runs)
        by_status: Counter = Counter(r["status"] for r in runs)
        completed = by_status.get("completed", 0)
        failed = by_status.get("failed", 0)
        interrupted = by_status.get("interrupted", 0)
        max_iterations = by_status.get("max_iterations", 0)

        # Duration stats (only for runs with both timestamps)
        durations = []
        for r in runs:
            s, e = r.get("started_at"), r.get("ended_at")
            if s and e and e > s:
                durations.append(e - s)
        durations.sort()

        avg_duration = sum(durations) / len(durations) if durations else 0
        p50 = _percentile(durations, 50)
        p95 = _percentile(durations, 95)

        # Tool call counts per run
        tool_counts_per_run = [
            len(r.get("tool_sequence") or [])
            for r in runs
        ]
        avg_tools_per_run = (
            sum(tool_counts_per_run) / len(tool_counts_per_run)
            if tool_counts_per_run else 0
        )

        # By source/trigger
        by_source: Counter = Counter(r.get("source", "unknown") for r in runs)
        by_trigger: Counter = Counter(r.get("trigger_type", "user_message") for r in runs)

        stuck = self._get_stuck_runs_raw()

        return {
            "period_days": days,
            "total": total,
            "completed": completed,
            "failed": failed,
            "interrupted": interrupted,
            "max_iterations": max_iterations,
            "stuck": len(stuck),
            "success_rate": _pct(completed, total),
            "failure_rate": _pct(failed, total),
            "duration": {
                "avg_s": round(avg_duration, 1),
                "p50_s": p50,
                "p95_s": p95,
                "min_s": round(min(durations), 1) if durations else 0,
                "max_s": round(max(durations), 1) if durations else 0,
            },
            "avg_tools_per_run": round(avg_tools_per_run, 1),
            "by_source": dict(by_source.most_common()),
            "by_trigger": dict(by_trigger.most_common()),
        }

    # =========================================================================
    # Tool stats
    # =========================================================================

    def tool_stats(self, days: int = 7) -> Dict[str, Any]:
        """Per-tool call counts, success/failure rates."""
        cutoff = time.time() - (days * 86400)
        runs = self._get_completed_runs(cutoff)

        tool_calls: Counter = Counter()
        tool_errors: Counter = Counter()
        tool_durations: Dict[str, List[float]] = defaultdict(list)

        for r in runs:
            seq = r.get("tool_sequence") or []
            for entry in seq:
                if not isinstance(entry, dict):
                    continue
                name = entry.get("tool", "unknown")
                tool_calls[name] += 1
                if not entry.get("ok", True):
                    tool_errors[name] += 1
                ms = entry.get("ms")
                if ms is not None:
                    tool_durations[name].append(ms)

        total_calls = sum(tool_calls.values())
        tools = []
        for name, count in tool_calls.most_common():
            errors = tool_errors.get(name, 0)
            durations = sorted(tool_durations.get(name, []))
            tools.append({
                "tool": name,
                "calls": count,
                "errors": errors,
                "error_rate": _pct(errors, count),
                "pct_of_total": _pct(count, total_calls),
                "avg_ms": round(sum(durations) / len(durations), 1) if durations else None,
                "p95_ms": _percentile(durations, 95) if durations else None,
            })

        return {
            "period_days": days,
            "total_calls": total_calls,
            "unique_tools": len(tool_calls),
            "tools": tools,
        }

    # =========================================================================
    # Provider / model stats
    # =========================================================================

    def provider_stats(self, days: int = 7) -> Dict[str, Any]:
        """Per-model/provider run and token breakdown."""
        cutoff = time.time() - (days * 86400)
        runs = self._get_completed_runs(cutoff)

        by_model: Dict[str, Dict] = defaultdict(lambda: {
            "runs": 0, "completed": 0, "failed": 0,
            "input_tokens": 0, "output_tokens": 0,
        })
        by_provider: Dict[str, Dict] = defaultdict(lambda: {
            "runs": 0, "input_tokens": 0, "output_tokens": 0,
        })

        for r in runs:
            model = (r.get("model") or "unknown").split("/")[-1]  # strip provider prefix
            provider = r.get("provider") or "unknown"
            status = r.get("status", "unknown")

            m = by_model[model]
            m["runs"] += 1
            m["completed"] += 1 if status == "completed" else 0
            m["failed"] += 1 if status == "failed" else 0
            m["input_tokens"] += r.get("input_tokens") or 0
            m["output_tokens"] += r.get("output_tokens") or 0

            p = by_provider[provider]
            p["runs"] += 1
            p["input_tokens"] += r.get("input_tokens") or 0
            p["output_tokens"] += r.get("output_tokens") or 0

        # Sort by runs descending
        models_list = sorted(
            [{"model": k, **v} for k, v in by_model.items()],
            key=lambda x: x["runs"], reverse=True
        )
        providers_list = sorted(
            [{"provider": k, **v} for k, v in by_provider.items()],
            key=lambda x: x["runs"], reverse=True
        )

        return {
            "period_days": days,
            "by_model": models_list,
            "by_provider": providers_list,
        }

    # =========================================================================
    # Approval / policy stats
    # =========================================================================

    def approval_stats(self, days: int = 7) -> Dict[str, Any]:
        """Approval/denial counts and most-denied command patterns."""
        cutoff = time.time() - (days * 86400)
        runs = self._get_completed_runs(cutoff)

        total_approvals = 0
        total_denials = 0
        denial_commands: List[str] = []

        for r in runs:
            events = r.get("approval_events") or []
            for e in events:
                if not isinstance(e, dict):
                    continue
                if e.get("approved", True):
                    total_approvals += 1
                else:
                    total_denials += 1
                    cmd = (e.get("cmd") or "")[:80]
                    if cmd:
                        denial_commands.append(cmd)

        total_events = total_approvals + total_denials
        denial_rate = _pct(total_denials, total_events)

        # Most common denial prefixes (first 30 chars)
        denial_prefixes: Counter = Counter(cmd[:30] for cmd in denial_commands)
        top_denied = [
            {"command_prefix": cmd, "count": cnt}
            for cmd, cnt in denial_prefixes.most_common(10)
        ]

        return {
            "period_days": days,
            "total_approval_events": total_events,
            "approvals": total_approvals,
            "denials": total_denials,
            "denial_rate": denial_rate,
            "top_denied_commands": top_denied,
        }

    # =========================================================================
    # Stuck runs
    # =========================================================================

    def get_stuck_runs(self) -> List[Dict[str, Any]]:
        """Return runs in 'running' status for more than 1 hour."""
        stuck = self._get_stuck_runs_raw()
        now = time.time()
        result = []
        for r in stuck:
            age_s = now - (r.get("started_at") or now)
            result.append({
                "run_id": r["id"][:8],
                "run_id_full": r["id"],
                "session_id": r.get("session_id", "?")[:16],
                "source": r.get("source", "?"),
                "model": r.get("model", "?"),
                "started_at": r.get("started_at"),
                "age_hours": round(age_s / 3600, 1),
                "prompt_preview": (r.get("user_message_preview") or "")[:60],
            })
        return result

    # =========================================================================
    # Full report
    # =========================================================================

    def full_report(self, days: int = 7) -> Dict[str, Any]:
        """Compute all metrics. Returns a single dict for display or export."""
        return {
            "generated_at": time.time(),
            "period_days": days,
            "runs": self.run_stats(days),
            "tools": self.tool_stats(days),
            "providers": self.provider_stats(days),
            "approvals": self.approval_stats(days),
            "stuck_runs": self.get_stuck_runs(),
        }

    # =========================================================================
    # Terminal dashboard display
    # =========================================================================

    def format_dashboard(self, days: int = 7) -> str:
        """Format metrics as a terminal dashboard."""
        report = self.full_report(days)
        lines = []

        rs = report["runs"]
        ts = report["tools"]
        ps = report["providers"]
        ap = report["approvals"]
        stuck = report["stuck_runs"]

        lines.append("")
        lines.append("  ╔══════════════════════════════════════════════════════════╗")
        lines.append("  ║               📊 Logos Metrics Dashboard                 ║")
        period = f"Last {days} day{'s' if days != 1 else ''}"
        pad = 58 - len(period) - 2
        lines.append(f"  ║{' ' * (pad // 2)} {period} {' ' * (pad - pad // 2)}║")
        lines.append("  ╚══════════════════════════════════════════════════════════╝")
        lines.append("")

        # ── Run Stats ──────────────────────────────────────────────────────
        lines.append("  🏃 Run Statistics")
        lines.append("  " + "─" * 56)
        total = rs["total"]
        if total == 0:
            lines.append("  No completed runs in this period.")
        else:
            lines.append(
                f"  Total runs:     {total:<10}  Success rate:  {rs['success_rate']}%"
            )
            lines.append(
                f"  Completed:      {rs['completed']:<10}  Failed:        {rs['failed']}"
            )
            lines.append(
                f"  Interrupted:    {rs['interrupted']:<10}  Max-iter:      {rs['max_iterations']}"
            )
            d = rs["duration"]
            lines.append(
                f"  Avg duration:   {d['avg_s']:.1f}s{'':<7}  P95 duration:  {d['p95_s']:.1f}s"
            )
            lines.append(
                f"  Avg tools/run:  {rs['avg_tools_per_run']:<10}"
            )

            if rs["by_source"]:
                src_parts = ", ".join(
                    f"{src}: {cnt}" for src, cnt in list(rs["by_source"].items())[:5]
                )
                lines.append(f"  By source:      {src_parts}")

        if stuck:
            lines.append("")
            lines.append(f"  ⚠️  Stuck runs: {len(stuck)}")
            for s in stuck[:3]:
                lines.append(
                    f"    • {s['run_id']} ({s['age_hours']}h) [{s['source']}] "
                    f"{s['prompt_preview'][:40]}"
                )

        lines.append("")

        # ── Tool Stats ─────────────────────────────────────────────────────
        tools_list = ts.get("tools", [])
        if tools_list:
            lines.append("  🔧 Tool Usage")
            lines.append("  " + "─" * 56)
            lines.append(f"  Total calls: {ts['total_calls']:,}  •  Unique tools: {ts['unique_tools']}")
            lines.append("")
            lines.append(f"  {'Tool':<26} {'Calls':>7} {'Errors':>7} {'Err%':>6} {'Avg ms':>8}")
            for t in tools_list[:12]:
                avg_ms = f"{t['avg_ms']:.0f}" if t["avg_ms"] is not None else "—"
                lines.append(
                    f"  {t['tool']:<26} {t['calls']:>7,} "
                    f"{t['errors']:>7} {t['error_rate']:>5.1f}% {avg_ms:>8}"
                )
            if len(tools_list) > 12:
                lines.append(f"  ... and {len(tools_list) - 12} more tools")
            lines.append("")

        # ── Provider Stats ─────────────────────────────────────────────────
        models = ps.get("by_model", [])
        if models:
            lines.append("  🤖 Model Breakdown")
            lines.append("  " + "─" * 56)
            lines.append(f"  {'Model':<30} {'Runs':>6} {'✓':>6} {'✗':>6} {'Tokens':>12}")
            for m in models[:8]:
                tokens = m["input_tokens"] + m["output_tokens"]
                lines.append(
                    f"  {m['model'][:28]:<30} {m['runs']:>6} "
                    f"{m['completed']:>6} {m['failed']:>6} {tokens:>12,}"
                )
            lines.append("")

        # ── Approval Stats ─────────────────────────────────────────────────
        if ap["total_approval_events"] > 0:
            lines.append("  🛡️  Policy / Approvals")
            lines.append("  " + "─" * 56)
            lines.append(
                f"  Total approval events: {ap['total_approval_events']}  "
                f"Approvals: {ap['approvals']}  Denials: {ap['denials']}  "
                f"({ap['denial_rate']}% denial rate)"
            )
            if ap["top_denied_commands"]:
                lines.append("  Top denied:")
                for d in ap["top_denied_commands"][:3]:
                    lines.append(f"    • [{d['count']}x] {d['command_prefix']}")
            lines.append("")

        lines.append(
            f"  Generated at {datetime.fromtimestamp(report['generated_at']).strftime('%Y-%m-%d %H:%M:%S')}"
        )
        return "\n".join(lines)

    # =========================================================================
    # Prometheus text format export
    # =========================================================================

    def export_prometheus(self, days: int = 7) -> str:
        """
        Export metrics in Prometheus text format.

        Can be written to a .prom file and scraped by node_exporter's
        textfile collector, or served by a minimal HTTP handler.
        """
        report = self.full_report(days)
        rs = report["runs"]
        ts = report["tools"]
        ps = report["providers"]
        ap = report["approvals"]
        stuck_count = len(report["stuck_runs"])
        now = int(report["generated_at"] * 1000)  # ms timestamp

        lines = [
            "# HELP logos_runs_total Total agent runs in the observation period",
            "# TYPE logos_runs_total gauge",
            f'logos_runs_total{{period_days="{days}"}} {rs["total"]} {now}',
            "",
            "# HELP logos_runs_completed Completed agent runs",
            "# TYPE logos_runs_completed gauge",
            f'logos_runs_completed{{period_days="{days}"}} {rs["completed"]} {now}',
            "",
            "# HELP logos_runs_failed Failed agent runs",
            "# TYPE logos_runs_failed gauge",
            f'logos_runs_failed{{period_days="{days}"}} {rs["failed"]} {now}',
            "",
            "# HELP logos_runs_stuck Currently stuck runs (running > 1h)",
            "# TYPE logos_runs_stuck gauge",
            f'logos_runs_stuck {stuck_count} {now}',
            "",
            "# HELP logos_run_duration_avg_seconds Average run duration in seconds",
            "# TYPE logos_run_duration_avg_seconds gauge",
            f'logos_run_duration_avg_seconds{{period_days="{days}"}} {rs["duration"]["avg_s"]} {now}',
            "",
            "# HELP logos_run_duration_p95_seconds P95 run duration in seconds",
            "# TYPE logos_run_duration_p95_seconds gauge",
            f'logos_run_duration_p95_seconds{{period_days="{days}"}} {rs["duration"]["p95_s"]} {now}',
            "",
            "# HELP logos_tool_calls_total Total tool invocations",
            "# TYPE logos_tool_calls_total gauge",
        ]

        for t in ts.get("tools", []):
            labels = f'tool="{t["tool"]}",period_days="{days}"'
            lines.append(f'logos_tool_calls_total{{{labels}}} {t["calls"]} {now}')
            lines.append(f'logos_tool_errors_total{{{labels}}} {t["errors"]} {now}')

        lines += [
            "",
            "# HELP logos_approval_events_total Total approval/policy events",
            "# TYPE logos_approval_events_total gauge",
            f'logos_approval_events_total{{period_days="{days}",outcome="approved"}} {ap["approvals"]} {now}',
            f'logos_approval_events_total{{period_days="{days}",outcome="denied"}} {ap["denials"]} {now}',
            "",
        ]

        # Per-model token counts
        lines.append("# HELP logos_model_tokens_total Tokens used per model")
        lines.append("# TYPE logos_model_tokens_total gauge")
        for m in ps.get("by_model", []):
            ml = f'model="{m["model"]}",period_days="{days}"'
            lines.append(
                f'logos_model_tokens_total{{{ml},type="input"}} {m["input_tokens"]} {now}'
            )
            lines.append(
                f'logos_model_tokens_total{{{ml},type="output"}} {m["output_tokens"]} {now}'
            )

        lines.append("")
        return "\n".join(lines)
