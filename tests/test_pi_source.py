from brewski.sources.pi import PiSource


def test_pi_source_reports_startup_steps_in_order(store, monkeypatch):
    monkeypatch.setattr("brewski.sources.pi.panel_pin_factory", lambda: object())
    monkeypatch.setattr(PiSource, "_open_heaters", lambda self, _settings: None)
    monkeypatch.setattr(PiSource, "_open_spi", lambda self, _settings: None)
    monkeypatch.setattr(PiSource, "_open_climate", lambda self, _settings: None)
    steps: list[str] = []
    PiSource(store, status=steps.append)
    assert steps == ["Opening outputs", "Opening probes", "Cabinet sensor"]
