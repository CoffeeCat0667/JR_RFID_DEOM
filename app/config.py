"""配置持久化与文件日志。"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

APP_NAME = "JRRFID"
BASE_DIR = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / APP_NAME
CONFIG_PATH = BASE_DIR / "config.json"
LOG_DIR = BASE_DIR / "logs"

DEFAULTS = {"port": "", "baud": 115200, "url": "", "device_id": "RFID-PLATE-001"}

logger = logging.getLogger("rfid")


def load_config() -> dict:
    config = dict(DEFAULTS)
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return config
    except Exception as exc:
        logger.warning("读取配置失败：%s", exc)
        return config
    if not isinstance(data, dict):
        logger.warning("配置文件格式不正确，使用默认配置")
        return config
    if isinstance(data.get("port"), str):
        config["port"] = data["port"]
    if isinstance(data.get("baud"), int) and data["baud"] > 0:
        config["baud"] = data["baud"]
    if isinstance(data.get("url"), str):
        config["url"] = data["url"]
    if isinstance(data.get("device_id"), str):
        config["device_id"] = data["device_id"]
    return config


def save_config(config: dict) -> None:
    try:
        BASE_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(
            json.dumps(config, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as exc:
        logger.warning("保存配置失败：%s", exc)


def setup_logging() -> logging.Logger:
    log = logging.getLogger("rfid")
    if log.handlers:
        return log
    log.setLevel(logging.INFO)
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(
            LOG_DIR / time.strftime("app-%Y%m%d.log"),
            encoding="utf-8",
        )
    except Exception as exc:
        log.warning("初始化文件日志失败：%s", exc)
        return log
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(handler)
    return log
