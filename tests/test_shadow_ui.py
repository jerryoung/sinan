"""策略看板展示辅助函数回归测试。"""
import json

import ui.shadow as shadow


def test_latest_target_returns_parsed_latest_file(tmp_path, monkeypatch):
    target = tmp_path / "targets_s_20240812.json"
    target.write_text(json.dumps({"strategy": "s", "date": "2024-08-12"}),
                      encoding="utf-8")
    monkeypatch.setattr(shadow, "targets_files", lambda _strategy: [target])

    files, payload = shadow._latest_target("s")

    assert files == [target]
    assert payload["date"] == "2024-08-12"
