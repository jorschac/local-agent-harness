from harness.paths import init_home, load_config


def test_existing_configuration_read_never_initializes_or_chmods(tmp_path, monkeypatch):
    import harness.paths as paths
    init_home(tmp_path)
    original = (tmp_path / "config.yaml").read_bytes()
    monkeypatch.setattr(paths, "init_home", lambda *a: (_ for _ in ()).throw(AssertionError("Read attempted filesystem initialization")))
    assert load_config(tmp_path)["alias"] == "harness-default"
    assert (tmp_path / "config.yaml").read_bytes() == original
