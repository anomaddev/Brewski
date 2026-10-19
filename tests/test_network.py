"""Address line for the LCD networking screen."""

from brewski.network import clear_identity_cache, display_identity, format_identity


def test_the_address_line_includes_the_listen_port():
    assert format_identity("192.168.1.42", "stillpi", 8080) == ("192.168.1.42:8080", "stillpi")
    assert format_identity(None, "  ", 8080) == ("No address", "—")
    assert format_identity("255.255.255.255", "brewski", 65535) == ("255.255.255.255", "brewski")


def test_a_missing_address_is_shown_on_the_panel(monkeypatch):
    monkeypatch.setattr("brewski.network._primary_ipv4", lambda: None)
    monkeypatch.setattr("brewski.network._hostname", lambda: "stillpi")
    clear_identity_cache()
    assert display_identity() == ("No address", "stillpi")
