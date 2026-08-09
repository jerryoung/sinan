"""
告警推送(方案 §3 / §8):loguru 本地日志 + 企业微信群机器人 webhook。

设计取舍:
  - webhook 为空 → 只写本地日志。影子模式/测试环境零外部依赖,
    测试永远不发真实请求。
  - 推送失败(断网、限频)一律吞掉只记日志——告警通道本身的故障
    绝不能拖垮数据更新 / 信号生成主流程。
"""
from __future__ import annotations

import json
import urllib.request

from loguru import logger

_LEVELS = {"debug": "DEBUG", "info": "INFO", "warning": "WARNING", "error": "ERROR"}


def notify(msg: str, *, level: str = "info", webhook: str = "") -> None:
    """本地日志必记;webhook 非空时再 POST 企业微信 markdown(3s 超时)。"""
    logger.log(_LEVELS.get(level, "INFO"), msg)
    if not webhook:
        return
    body = json.dumps(
        {"msgtype": "markdown", "markdown": {"content": msg}},
        ensure_ascii=False,
    ).encode("utf-8")
    req = urllib.request.Request(
        webhook, data=body, headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=3)
    except Exception as e:  # noqa: BLE001  告警失败只降级为本地日志
        logger.warning(f"企业微信推送失败(已忽略): {e}")
