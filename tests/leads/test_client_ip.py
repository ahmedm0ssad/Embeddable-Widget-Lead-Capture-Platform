from app.dependencies.client_ip import get_client_ip


class _FakeRequest:
    def __init__(self, ip: str | None = "127.0.0.1", xff: str = ""):
        self.client = type("obj", (object,), {"host": ip})() if ip else None
        headers = {}
        if xff:
            headers["x-forwarded-for"] = xff
        self.headers = headers


def _build(peer="127.0.0.1", xff=""):
    return _FakeRequest(ip=peer, xff=xff)


class TestGetClientIp:
    def test_trust_off_ignores_xff(self):
        request = _build(peer="172.18.0.1", xff="8.8.8.8")
        assert get_client_ip(request) == "172.18.0.1"

    def test_trust_off_no_client(self):
        request = _build(peer=None, xff="8.8.8.8")
        assert get_client_ip(request) == "unknown"

    def test_empty_trusted_cidrs_string_disables_trust(self):
        request = _build(peer="172.18.0.1", xff="8.8.8.8")
        assert get_client_ip(request, trusted_cidrs="") == "172.18.0.1"

    def test_reads_env_when_no_override(self, monkeypatch):
        monkeypatch.setenv("TRUSTED_PROXY_CIDRS", "10.0.0.0/8")
        request = _build(peer="10.0.0.1", xff="8.8.8.8, 10.0.0.1")
        assert get_client_ip(request) == "8.8.8.8"

    def test_trusted_proxy_returns_first_untrusted_hop(self):
        request = _build(peer="10.0.0.1", xff="8.8.8.8, 10.0.0.1")
        assert get_client_ip(request, trusted_cidrs="10.0.0.0/8") == "8.8.8.8"

    def test_no_xff_returns_direct_peer(self):
        request = _build(peer="10.0.0.1", xff="")
        assert get_client_ip(request, trusted_cidrs="10.0.0.0/8") == "10.0.0.1"

    def test_all_hops_trusted_falls_back_to_direct_peer(self):
        request = _build(peer="10.0.0.1", xff="10.0.0.2, 10.0.0.1")
        assert get_client_ip(request, trusted_cidrs="10.0.0.0/8") == "10.0.0.1"

    def test_invalid_tokens_skipped(self):
        request = _build(peer="10.0.0.1", xff="not-an-ip, 8.8.8.8, 10.0.0.1")
        assert get_client_ip(request, trusted_cidrs="10.0.0.0/8") == "8.8.8.8"

    def test_ipv6_hop(self):
        request = _build(peer="2001:db8::10", xff="2001:db9::5, 2001:db8::10")
        assert get_client_ip(request, trusted_cidrs="2001:db8::/32") == "2001:db9::5"

    def test_multi_proxy_chain(self):
        request = _build(peer="10.0.0.1", xff="203.0.113.7, 172.16.5.1, 10.0.0.1")
        trusted = "10.0.0.0/8,172.16.0.0/12"
        assert get_client_ip(request, trusted_cidrs=trusted) == "203.0.113.7"

    def test_empty_cidr_segments_skipped(self):
        request = _build(peer="10.0.0.1", xff="8.8.8.8, 10.0.0.1")
        trusted = "10.0.0.0/8,,172.16.0.0/12"
        assert get_client_ip(request, trusted_cidrs=trusted) == "8.8.8.8"

    def test_empty_xff_tokens_skipped(self):
        request = _build(peer="10.0.0.1", xff="8.8.8.8,, 10.0.0.1")
        assert get_client_ip(request, trusted_cidrs="10.0.0.0/8") == "8.8.8.8"

    def test_malformed_cidr_tolerated_and_logged(self, caplog):
        request = _build(peer="10.0.0.1", xff="8.8.8.8, 10.0.0.1")
        with caplog.at_level("WARNING", logger="app.dependencies.client_ip"):
            result = get_client_ip(request, trusted_cidrs="bogus/33,10.0.0.0/8")
        assert result == "8.8.8.8"
        assert any("bogus/33" in record.message for record in caplog.records)

    def test_trust_on_no_client(self):
        request = _build(peer=None, xff="8.8.8.8")
        assert get_client_ip(request, trusted_cidrs="10.0.0.0/8") == "8.8.8.8"
