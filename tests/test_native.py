from kicad_autorouter._native import native_version


def test_native_extension_is_loaded():
    assert native_version() == "0.1.9"
