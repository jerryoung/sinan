"""run_signal 实际接线:命名实盘配置必须进入 targets 契约。"""
from types import SimpleNamespace

import pandas as pd

from sinan.config import (LiveProfileCfg, LiveProfilesCfg, Settings,
                          StrategyCfg)


def test_run_signal_resolves_live_profile_into_payload(tmp_path, monkeypatch):
    """执行 main 的最小闭环，防止解析结果只在单元测试里成立。"""
    from scripts import run_signal
    import sinan.config as config_module
    import sinan.data.store as store_module
    import sinan.live.broker as broker_module
    import sinan.live.notify as notify_module
    import sinan.live.reconcile as reconcile_module
    import sinan.live.targets as targets_module
    import sinan.signal.base as signal_module
    import sinan.universe.instruments as instruments_module

    settings = Settings(
        store_root=tmp_path / "store",
        targets_dir=tmp_path / "targets",
        fills_dir=tmp_path / "fills",
        reports_dir=tmp_path / "reports",
    )
    strategy = StrategyCfg(
        name="alpha",
        strategy="donchian",
        universe=["510300"],
        capital=100_000,
        live_profile="local_qmt",
    )
    profiles = LiveProfilesCfg(
        default="local_qmt",
        profiles={
            "local_qmt": LiveProfileCfg(
                name="本地 QMT",
                qmt={
                    "algo": {
                        "quote_mode": "limit",
                        "price_offset": 0.003,
                        "max_order_qty": 5000,
                    }
                },
            )
        },
    )

    class FakeStore:
        def __init__(self, _root):
            pass

        def read_bars(self, **_kwargs):
            return pd.DataFrame([{
                "date": pd.Timestamp("2026-08-10"),
                "symbol": "510300",
                "open": 4.0,
                "high": 4.1,
                "low": 3.9,
                "close": 4.0,
                "close_raw": 4.0,
                "volume": 1_000_000,
                "amount": 4_000_000,
            }])

        def read_instruments(self, **_kwargs):
            return pd.DataFrame([{"symbol": "510300", "name": "沪深300ETF"}])

    captured = {}

    def capture_targets(payload, _targets_dir):
        captured.update(payload)
        return tmp_path / "targets_alpha_20260811.json"

    monkeypatch.setattr(config_module, "load_settings", lambda: settings)
    monkeypatch.setattr(config_module, "load_strategy", lambda _path: strategy)
    monkeypatch.setattr(config_module, "load_live_profiles", lambda: profiles)
    monkeypatch.setattr(store_module, "DataStore", FakeStore)
    monkeypatch.setattr(
        broker_module.QmtShellBroker,
        "latest_fills",
        staticmethod(lambda *_args, **_kwargs: None),
    )
    monkeypatch.setattr(notify_module, "notify", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        reconcile_module,
        "reconcile_fills",
        lambda *_args, **_kwargs: SimpleNamespace(
            skipped="无 fills",
            ok=False,
            deviations=[],
            date=None,
            as_dict=lambda: {"skipped": "无 fills"},
        ),
    )
    monkeypatch.setattr(signal_module, "call_strategy", lambda *_args: {"510300": 0.5})
    monkeypatch.setattr(
        instruments_module,
        "resolve_rule",
        lambda *_args, **_kwargs: SimpleNamespace(lot_size=100),
    )
    monkeypatch.setattr(targets_module, "apply_risk", lambda weights, **_kwargs: (weights, []))
    monkeypatch.setattr(targets_module, "save_targets", capture_targets)
    monkeypatch.setattr(
        "sys.argv",
        ["run_signal.py", "--strategy", "ignored.yaml", "--date", "2026-08-11"],
    )

    assert run_signal.main() == 0
    assert captured["live_profile"] == "local_qmt"
    assert captured["qmt"] == {
        "algo": {
            "quote_mode": "limit",
            "price_offset": 0.003,
            "max_order_qty": 5000,
        }
    }
    assert captured["strategy"] == "alpha"

