#!/usr/bin/env python3
"""显式把一份本地 targets 发布到其引用的 QMT 实盘配置。"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sinan.config import load_live_profiles, load_settings  # noqa: E402
from sinan.live.qmt_bridge import QmtRpcBridge  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="经 RPC 显式发布 targets 到 QMT")
    parser.add_argument("target_file", type=Path)
    parser.add_argument(
        "--pull", action="store_true", help="发布后尝试拉取该执行日 fills"
    )
    args = parser.parse_args(argv)

    try:
        payload = json.loads(args.target_file.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("targets 文件根节点必须是对象")
        profile_id = str(payload.get("live_profile") or "").strip()
        profiles = load_live_profiles()
        if not profile_id or profile_id not in profiles.profiles:
            raise ValueError(
                f"targets 引用了不存在的实盘配置:{profile_id!r}"
            )
        bridge = QmtRpcBridge(profiles.profiles[profile_id].qmt.rpc)
        result = bridge.publish(payload)
        output = asdict(result)
        if args.pull:
            fills_path = bridge.pull_fills(
                result.strategy, result.date, load_settings().fills_dir
            )
            output["fills_path"] = str(fills_path) if fills_path else None
        print(json.dumps(output, ensure_ascii=False))
        return 0
    except Exception as exc:  # CLI 边界：保留可诊断错误并返回非零
        print(f"发布失败:{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
