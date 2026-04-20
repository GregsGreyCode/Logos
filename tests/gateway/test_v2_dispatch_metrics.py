"""Unit tests for the Phase 1.6 dispatch kill switch + counter.

The kill switch (``LOGOS_DISPATCH_V2_FORCE_V1``) lets operators
roll back to v1 dispatch without a redeploy during the v2-default
soak. The counter (``record_dispatch_path`` + ``dispatch_counts``)
surfaces the v2/(v1+v2) ratio so a silent regression to v1 after
the flip is observable.
"""

from __future__ import annotations

import pytest

from gateway.worker_registry_v2 import (
    dispatch_counts,
    is_dispatch_v2_enabled,
    is_dispatch_v2_forced_to_v1,
    record_dispatch_path,
    reset_dispatch_counts,
)


class TestIsDispatchV2ForcedToV1:
    def test_unset_defaults_to_false(self, monkeypatch):
        monkeypatch.delenv("LOGOS_DISPATCH_V2_FORCE_V1", raising=False)
        assert is_dispatch_v2_forced_to_v1() is False

    def test_value_1_enables_kill_switch(self, monkeypatch):
        monkeypatch.setenv("LOGOS_DISPATCH_V2_FORCE_V1", "1")
        assert is_dispatch_v2_forced_to_v1() is True

    def test_other_truthy_values_do_not_enable(self, monkeypatch):
        # Exact "1" match only — avoids accidentally enabling on
        # "true"/"yes" strings unless explicitly set to the canonical value.
        monkeypatch.setenv("LOGOS_DISPATCH_V2_FORCE_V1", "true")
        assert is_dispatch_v2_forced_to_v1() is False


class TestIsDispatchV2Enabled:
    def test_unset_defaults_to_false(self, monkeypatch):
        monkeypatch.delenv("LOGOS_DISPATCH_V2", raising=False)
        assert is_dispatch_v2_enabled() is False

    def test_value_1_enables(self, monkeypatch):
        monkeypatch.setenv("LOGOS_DISPATCH_V2", "1")
        assert is_dispatch_v2_enabled() is True


class TestDispatchCounter:
    def setup_method(self):
        reset_dispatch_counts()

    def test_initial_snapshot_is_zero(self):
        assert dispatch_counts() == {"v2": 0, "v1": 0, "v2_forced_v1": 0}

    def test_record_v2(self):
        record_dispatch_path("v2")
        record_dispatch_path("v2")
        assert dispatch_counts()["v2"] == 2

    def test_record_v1(self):
        record_dispatch_path("v1")
        assert dispatch_counts()["v1"] == 1

    def test_record_v2_forced_v1(self):
        record_dispatch_path("v2_forced_v1")
        assert dispatch_counts()["v2_forced_v1"] == 1

    def test_unknown_path_is_ignored(self):
        record_dispatch_path("martian")
        assert dispatch_counts() == {"v2": 0, "v1": 0, "v2_forced_v1": 0}

    def test_counters_are_independent(self):
        record_dispatch_path("v2")
        record_dispatch_path("v2")
        record_dispatch_path("v1")
        record_dispatch_path("v2_forced_v1")
        assert dispatch_counts() == {"v2": 2, "v1": 1, "v2_forced_v1": 1}

    def test_snapshot_is_a_copy_not_live(self):
        record_dispatch_path("v2")
        snap = dispatch_counts()
        record_dispatch_path("v2")
        assert snap["v2"] == 1
        assert dispatch_counts()["v2"] == 2

    def test_reset_zeroes_all_counters(self):
        record_dispatch_path("v2")
        record_dispatch_path("v1")
        record_dispatch_path("v2_forced_v1")
        reset_dispatch_counts()
        assert dispatch_counts() == {"v2": 0, "v1": 0, "v2_forced_v1": 0}
