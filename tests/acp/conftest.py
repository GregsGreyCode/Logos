import pytest

# Skip the entire acp test suite when the optional [acp] extra is not installed.
acp = pytest.importorskip("acp", reason="acp extra not installed (pip install logos[acp])")
