import pytest

try:
    import ledgerlens_accel
except ImportError:
    ledgerlens_accel = None

pytestmark = pytest.mark.skipif(
    ledgerlens_accel is None,
    reason="ledgerlens_accel Rust extension not installed",
)


def test_extension_importable():
    assert ledgerlens_accel is not None
