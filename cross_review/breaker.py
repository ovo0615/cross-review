"""額度／連續失敗的斷路器。

審查失敗時每一輪都重試是有害的：每次都再燒一次額度、再等一次、
再產生一次噪音，而且永遠不會自己停。

狀態刻意放在 breaker.json 而不是 state.json：hook 與背景審查是兩個行程，
各自讀寫同一個檔案會互相覆蓋。分開就沒有這個問題。
"""
from __future__ import annotations

import re
import time
from datetime import datetime, timedelta
from pathlib import Path

from . import common

# 這是 Codex 額度用完時的真實輸出（2026-09-02 實際擷取，不是猜的）：
#   ERROR: You've hit your usage limit. Upgrade to Pro (...),
#   visit https://chatgpt.com/codex/settings/usage to purchase more credits
#   or try again at 1:27 PM.
_QUOTA = re.compile(r"usage limit|purchase more credits|rate.?limit", re.I)
_RESET_AT = re.compile(r"try again at\s+(\d{1,2}):(\d{2})\s*([AaPp][Mm])?")

MAX_FAILURES = 3          # 連續失敗幾次才跳閘（單次可能只是網路抖動）
COOLDOWN_SEC = 3600       # 非額度類失敗的冷卻時間


def _path(project: Path) -> Path:
    return common.review_dir(project) / "breaker.json"


def load(project: Path) -> dict:
    data = common.read_json(_path(project))
    if not isinstance(data, dict):
        data = {}
    data.setdefault("consecutive_failures", 0)
    data.setdefault("paused_until", 0.0)
    data.setdefault("reason", "")
    return data


def is_quota_error(message: str) -> bool:
    return bool(_QUOTA.search(message or ""))


def parse_reset_time(message: str, now: float = None) -> float:
    """從錯誤訊息裡撈出「什麼時候可以再試」。

    Codex 的額度訊息直接寫了時間（`try again at 1:27 PM`），
    這比固定冷卻時間精準得多——早一秒重試就是白燒，晚一小時就是白等。
    撈不到就回 0，由呼叫端用固定冷卻兜底。
    """
    m = _RESET_AT.search(message or "")
    if not m:
        return 0.0
    hour, minute = int(m.group(1)), int(m.group(2))
    ampm = (m.group(3) or "").lower()
    if ampm == "pm" and hour != 12:
        hour += 12
    elif ampm == "am" and hour == 12:
        hour = 0
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return 0.0

    base = datetime.fromtimestamp(now if now is not None else time.time())
    target = base.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= base:
        target += timedelta(days=1)      # 訊息講的是明天的那個時刻
    return target.timestamp()


def record_failure(project: Path, message: str) -> str:
    """記一次失敗，必要時跳閘。回傳要告訴使用者的話（沒事就是空字串）。"""
    data = load(project)
    data["consecutive_failures"] = int(data.get("consecutive_failures", 0)) + 1
    data["reason"] = str(message)[:500]

    if is_quota_error(message):
        # 額度用完不必等三次——再試也是白試，而且訊息裡就寫了恢復時間。
        until = parse_reset_time(message)
        if not until:
            until = time.time() + COOLDOWN_SEC
        data["paused_until"] = until
        note = ("額度已用完，審查暫停到 "
                + datetime.fromtimestamp(until).strftime("%m/%d %H:%M")
                + "（時間到自動恢復）")
    elif data["consecutive_failures"] >= MAX_FAILURES:
        data["paused_until"] = time.time() + COOLDOWN_SEC
        note = ("連續 " + str(data["consecutive_failures"]) + " 次審查失敗，暫停 "
                + str(COOLDOWN_SEC // 60) + " 分鐘（時間到自動恢復）")
    else:
        note = ""

    common.write_json(_path(project), data)
    if note:
        common.log_error(project, "斷路器跳閘：" + note + "。最後的錯誤：" + str(message)[:300])
    return note


def record_success(project: Path) -> None:
    """成功一次就把計數歸零、解除暫停。"""
    data = load(project)
    if data["consecutive_failures"] or data["paused_until"]:
        common.write_json(_path(project),
                          {"consecutive_failures": 0, "paused_until": 0.0, "reason": ""})


def paused_note(project: Path) -> str:
    """目前在暫停中的話，回傳要告訴使用者的話；否則空字串。

    暫停期間每一輪都講一次。這件事寧可吵也不能忘記——
    「審查停著」跟「審查通過」在畫面上長得一模一樣。
    """
    data = load(project)
    until = float(data.get("paused_until") or 0)
    if until <= time.time():
        return ""
    return ("cross-review：審查暫停中，"
            + datetime.fromtimestamp(until).strftime("%m/%d %H:%M")
            + " 自動恢復。原因：" + str(data.get("reason") or "未記錄")[:200]
            + "（要立刻恢復就刪掉 .claude/review/breaker.json）")
