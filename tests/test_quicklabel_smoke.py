"""Smoke test: package imports and exposes a version."""

def test_imports():
    import quicklabel
    assert quicklabel.__version__
