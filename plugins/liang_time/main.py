"""按时间和所用网关调整「主动回复」概率。

两件事合在一处算，免得互相踩：

- **梁文峰时间**（默认 9-12、14-18）把所有随机回复概率砍半；
- **走指定的那条网关**（`fast_source_id`，通常是包月不计 token 的那条）时主动回复概率翻倍。

算出来的值写进运行时配置 `provider_ltm_settings.active_reply.possibility_reply`，
**不落盘**——重启后自动回到 `cmd_config.json` 里手写的那个基准值。
"""

import asyncio
import json
from datetime import datetime, time
from pathlib import Path

from astrbot.api import logger
from astrbot.api.star import Context, Star

CONFIG = Path(__file__).parent / "config.json"
STATE = Path(__file__).parent / "pace.json"
CMDS_CFG = Path(__file__).resolve().parents[2] / "cmd_config.json"
CHECK_INTERVAL = 30


def load_cfg() -> dict:
    """Read this plugin's config, falling back to an empty dict."""
    try:
        data = json.loads(CONFIG.read_text(encoding="utf-8-sig"))
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def load_windows() -> tuple[bool, list[tuple[time, time]]]:
    """读配置；支持 windows 列表，也兼容旧的 start/end。

    Returns:
        ``(enabled, windows)``；关掉或读不出时分别是 ``(False, [])`` / 空列表。
    """
    cfg = load_cfg()
    if not cfg.get("enabled", True):
        return False, []

    raw = cfg.get("windows")
    pairs: list[tuple[str, str]] = []
    if isinstance(raw, list) and raw:
        for item in raw:
            if isinstance(item, (list, tuple)) and len(item) == 2:
                pairs.append((str(item[0]), str(item[1])))
    elif cfg.get("start") and cfg.get("end"):
        pairs.append((str(cfg["start"]), str(cfg["end"])))

    out: list[tuple[time, time]] = []
    for s, e in pairs:
        try:
            out.append((time.fromisoformat(s), time.fromisoformat(e)))
        except ValueError:
            continue
    return True, out


def in_window(now: datetime | None = None) -> bool:
    """当前是否处于「梁文峰时间」。

    Args:
        now: 用来判断的时刻，默认取当前时间。

    Returns:
        True 表示在窗口内；窗口格式不对或插件被关掉时返回 False。
    """
    enabled, windows = load_windows()
    if not enabled:
        return False
    cur = (now or datetime.now()).time()
    for a, b in windows:
        if a <= b:
            if a <= cur < b:
                return True
        elif cur >= a or cur < b:  # 跨夜
            return True
    return False


def gateway_source() -> str | None:
    """看当前主力 provider 挂在哪个来源上。

    Returns:
        ``provider_sources`` 里的来源 id，读不出来时 None。
    """
    try:
        cfg = json.loads(CMDS_CFG.read_text(encoding="utf-8-sig"))
    except Exception:  # noqa: BLE001
        return None
    model = (cfg.get("agent_runner") or {}).get("config", {}).get("model") or {}
    pid = model.get("provider_id")
    for prov in cfg.get("provider") or []:
        if prov.get("id") == pid:
            return prov.get("provider_source_id")
    return None


def disk_base() -> float | None:
    """读 `cmd_config.json` 里手写的那个主动回复概率（磁盘上的基准值）。

    Returns:
        基准概率，读不出来时 None。
    """
    try:
        cfg = json.loads(CMDS_CFG.read_text(encoding="utf-8-sig"))
        value = cfg["provider_ltm_settings"]["active_reply"]["possibility_reply"]
        return float(value)
    except (OSError, KeyError, TypeError, ValueError):
        return None


def read_state() -> float | None:
    """读上一次存下的基准值（防止运行时配置被写脏后找不到原值）。

    Returns:
        基准概率，没有记录或读不出来时 None。
    """
    try:
        return float(json.loads(STATE.read_text(encoding="utf-8"))["base"])
    except (OSError, KeyError, TypeError, ValueError):
        return None


def write_state(base: float) -> None:
    """把基准值写到插件目录下，只记一个数。

    Args:
        base: 手写的基准概率。
    """
    try:
        STATE.write_text(
            json.dumps({"base": base}, ensure_ascii=False) + "\n", encoding="utf-8"
        )
    except OSError as e:  # noqa: BLE001
        logger.debug(f"liang_time: state write failed ({e})")


# 本插件在这本配置上可能乘出去的倍数，用来认出「磁盘上那个数是算出来的」
DERIVED = (0.25, 0.5, 1.0, 2.0, 4.0)


def resolve_base(on_disk: float | None, remembered: float | None) -> float | None:
    """定下这一次的基准概率。

    运行时配置会被本插件改写，WebUI 一旦保存设置就会把改写后的值写回磁盘。
    所以磁盘上的数不一定可信：它可能只是上一次算出来的结果。判断办法是看它
    能不能由记录里的基准乘一个已知倍数得到——能，就说明是算出来的，继续用记录
    里的基准；不能，就是人手改过的，以磁盘为准。

    Args:
        on_disk: `cmd_config.json` 里当前的数值。
        remembered: 上一次记下的基准值。

    Returns:
        应该拿来做基准的概率；两个来源都读不出来时 None。
    """
    if on_disk is None:
        return remembered
    if remembered is None:
        return on_disk
    if abs(on_disk - remembered) < 1e-9:
        return on_disk
    for factor in DERIVED:
        if abs(on_disk - round(remembered * factor, 4)) < 1e-9:
            return remembered
    return on_disk


class LiangTime(Star):
    """梁文峰时间砍半、新网关翻倍，统一管「主动回复」概率。"""

    def __init__(self, context: Context):
        super().__init__(context)
        # 手写的基准值。运行时配置会被本插件改写，基准得另存一份
        self._base = read_state()
        asyncio.create_task(self._loop())

    def _pace(self) -> tuple[float, str]:
        """算出这一刻该乘多少。

        Returns:
            ``(factor, why)``；``why`` 是写进日志的一行说明，没有调整时为空串。
        """
        cfg = load_cfg()
        if not cfg.get("enabled", True):
            return 1.0, ""
        factor = 1.0
        why: list[str] = []
        if in_window():
            factor *= 0.5
            why.append("梁文峰时间×0.5")
        fast_id = str(cfg.get("fast_source_id") or "")
        try:
            mult = float(cfg.get("fast_multiplier", 2.0) or 2.0)
        except (TypeError, ValueError):
            mult = 2.0
        if fast_id and gateway_source() == fast_id:
            factor *= mult
            why.append(f"新网关×{mult:g}")
        return factor, "  ".join(why)

    def _settings(self) -> dict | None:
        """取运行时那本 active_reply 配置。

        Returns:
            active_reply 字典；配置结构不对时 None。
        """
        try:
            return self.context.get_config()["provider_ltm_settings"]["active_reply"]
        except (KeyError, TypeError):
            return None

    def _sync(self) -> None:
        """把算出来的概率写进运行时配置（不落盘）。"""
        settings = self._settings()
        if settings is None:
            return
        on_disk = disk_base()
        # 每轮重新认一次基准：人手改过的要被认出来，自己算出去的不能认
        base = resolve_base(on_disk, self._base)
        if base is not None and base != self._base:
            logger.info(f"[liang] 基准值 {self._base} -> {base}")
            self._base = base
            write_state(base)
        base = self._base if self._base is not None else on_disk
        if base is None:
            return
        factor, why = self._pace()
        value = round(base * factor, 4)
        try:
            cur = float(settings.get("possibility_reply"))
        except (TypeError, ValueError):
            cur = -1.0
        if abs(value - cur) < 1e-9:
            return
        settings["possibility_reply"] = value
        logger.info(f"[liang] 主动回复 {cur:g} -> {value:g}（{why or '恢复正常'}）")

    async def _loop(self) -> None:
        while True:
            try:
                self._sync()
            except Exception as e:  # noqa: BLE001
                logger.warning(f"liang_time: {e}")
            await asyncio.sleep(CHECK_INTERVAL)
