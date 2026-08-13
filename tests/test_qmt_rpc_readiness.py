from pathlib import Path

from qmt_shell import qmt_sdk
from sinan.config import QmtRpcCfg
from sinan.live.qmt_rpc import verify_qmt_rpc


class _FakeClient:
    calls = []
    fail_on = None

    def connect(self, host, port, token, timeout):
        self.calls.append(("connect", host, port, token, timeout))
        if self.fail_on == "tcp":
            raise OSError("refused")
        return self

    def call(self, fn, *args, **kwargs):
        self.calls.append((fn, args, kwargs))
        if self.fail_on == fn:
            raise qmt_sdk.QmtRpcError("probe failed")
        if fn == "rpc.health":
            return {"service": "sinan-qmt-rpc", "protocol": 1,
                    "account": "80391000", "account_type": "STOCK",
                    "trade_mode": "sim", "allow_trade": True,
                    "server_time": "2026-08-13T23:30:00"}
        if fn == "C.get_stock_name":
            return "沪深300ETF华泰柏瑞"
        if fn == "C.get_full_tick":
            return {"510300.SH": {"lastPrice": 4.729}}
        raise AssertionError(fn)

    def close(self):
        self.calls.append(("close",))


def _run(monkeypatch, tmp_path: Path, fail_on=None):
    token = tmp_path / "token"
    token.write_text("secret", encoding="utf-8")
    _FakeClient.calls = []
    _FakeClient.fail_on = fail_on
    monkeypatch.setattr(qmt_sdk, "_Client", _FakeClient)
    return verify_qmt_rpc(QmtRpcCfg(host="qmt.example", port=58620, timeout=3),
                          token_path=token)


def test_verify_qmt_rpc_reports_health_and_quote(monkeypatch, tmp_path):
    result = _run(monkeypatch, tmp_path)
    assert result.ready
    assert result.stage == "ready"
    assert result.health["allow_trade"] is True
    assert result.quote == {"symbol": "510300.SH", "name": "沪深300ETF华泰柏瑞",
                            "last_price": 4.729}
    assert not any(call[0] in {"passorder", "cancel"} for call in _FakeClient.calls)


def test_verify_qmt_rpc_marks_tcp_failure(monkeypatch, tmp_path):
    result = _run(monkeypatch, tmp_path, "tcp")
    assert not result.ready and result.stage == "tcp"


def test_verify_qmt_rpc_marks_health_or_auth_failure(monkeypatch, tmp_path):
    result = _run(monkeypatch, tmp_path, "rpc.health")
    assert not result.ready and result.stage == "health"


def test_verify_qmt_rpc_marks_quote_failure(monkeypatch, tmp_path):
    result = _run(monkeypatch, tmp_path, "C.get_full_tick")
    assert not result.ready and result.stage == "quote"
