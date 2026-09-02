"""額度／連續失敗的斷路器。

審查失敗時每一輪都重試是有害的：每次都再燒一次額度、再等一次、
再產生一次噪音，而且永遠不會自己停。

狀態刻意放在 breaker.json 而不是 state.json：hook 與背景審查是兩個行程，
各自讀寫同一個檔案會互相覆蓋。分開就沒有這個問題。

失敗計數是**分模式**的。共用一份計數的話，程式碼審查每輪失敗、
視覺審查每輪成功，成功那邊會把計數清零，於是永遠累積不到門檻——
斷路器等於不存在。（第 29 回合審查發現，實際存在於程式碼中。）
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
_QUOTA = re.compile(r"usage limit|purchase more credits", re.I)
# 限流跟額度用完不是同一件事。原本把兩者併在同一個樣式裡，於是
# 「rate limit exceeded, please retry」這種幾秒後就會好的暫時性錯誤，
# 會被判定成額度耗盡而固定停一小時。限流只有在伺服器明講了恢復時間時
# 才值得暫停；沒講就當一般失敗，讓它去累積次數。
_RATE = re.compile(r"rate.?limit|too many requests", re.I)
_RESET_AT = re.compile(r"try again at\s+(\d{1,2}):(\d{2})\s*([AaPp][Mm])?")

MAX_FAILURES = 3          # 連續失敗幾次才跳閘（單次可能只是網路抖動）
COOLDOWN_SEC = 3600       # 非額度類失敗的冷卻時間

# 時間到才會解除的暫停種類。這兩種都是伺服器端的限制，
# 「另一個模式剛好成功了」不構成解除的理由——那次成功很可能是在
# 額度耗盡之前就派出去的，拿它來解除等於立刻再燒一次。
_TIME_ONLY = ("quota", "rate")


def _path(project: Path) -> Path:
    return common.review_dir(project) / "breaker.json"


def load(project: Path) -> dict:
    data = common.read_json(_path(project))
    if not isinstance(data, dict):
        data = {}
    fails = data.get("failures")
    if not isinstance(fails, dict):
        # 舊格式只有一個共用計數。搬過來當成兩個模式的起點，不要直接丟掉。
        old = data.get("consecutive_failures")
        old = int(old) if isinstance(old, int) else 0
        fails = {"code": old, "visual": old}
    data["failures"] = {k: (int(v) if isinstance(v, int) else 0)
                        for k, v in fails.items()}
    data.setdefault("paused_until", 0.0)
    data.setdefault("pause_kind", "")
    data.setdefault("pause_mode", "")
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


def _stamp(until: float) -> str:
    return datetime.fromtimestamp(until).strftime("%m/%d %H:%M")


def record_failure(project: Path, message: str, mode: str = "code") -> str:
    """記一次失敗，必要時跳閘。回傳要告訴使用者的話（沒事就是空字串）。"""
    data = load(project)
    data["failures"][mode] = int(data["failures"].get(mode, 0)) + 1
    data["reason"] = str(message)[:500]
    note = ""

    reset_at = parse_reset_time(message)
    if is_quota_error(message):
        # 額度用完不必等三次——再試也是白試，而且訊息裡就寫了恢復時間。
        until = reset_at or (time.time() + COOLDOWN_SEC)
        data["paused_until"] = until
        data["pause_kind"] = "quota"
        data["pause_mode"] = ""
        note = "額度已用完，審查暫停到 " + _stamp(until) + "（時間到自動恢復）"
    elif _RATE.search(message or "") and reset_at:
        data["paused_until"] = reset_at
        data["pause_kind"] = "rate"
        data["pause_mode"] = ""
        note = "被限流，審查暫停到 " + _stamp(reset_at) + "（時間到自動恢復）"
    elif data["failures"][mode] >= MAX_FAILURES:
        until = time.time() + COOLDOWN_SEC
        data["paused_until"] = until
        data["pause_kind"] = "failures"
        data["pause_mode"] = mode
        note = ("「" + mode + "」連續 " + str(data["failures"][mode])
                + " 次審查失敗，這個模式暫停 " + str(COOLDOWN_SEC // 60)
                + " 分鐘（時間到自動恢復）")

    common.write_json(_path(project), data)
    if note:
        common.log_error(project, "斷路器跳閘：" + note + "。最後的錯誤：" + str(message)[:300])
    return note


def record_success(project: Path, mode: str = "code") -> None:
    """成功一次就把**這個模式**的計數歸零。

    只解除由這個模式的連續失敗造成的暫停。額度／限流的暫停一律只有
    時間能解除：那是帳號層級的限制，跟哪個模式成功無關，而且併行派工時
    後完成的那個成功會剛好把前一個的額度暫停清掉。
    """
    data = load(project)
    changed = False
    if data["failures"].get(mode):
        data["failures"][mode] = 0
        changed = True
    if (data.get("pause_kind") == "failures"
            and data.get("pause_mode") == mode
            and data.get("paused_until")):
        data["paused_until"] = 0.0
        data["pause_kind"] = ""
        data["pause_mode"] = ""
        changed = True
    if changed:
        common.write_json(_path(project), data)


def paused_note(project: Path, mode: str = None) -> str:
    """目前在暫停中的話，回傳要告訴使用者的話；否則空字串。

    傳 mode 就只問那個模式。連續失敗造成的暫停只擋那個模式——
    程式碼審查壞掉不該連帶讓視覺審查也停下來。

    暫停期間每一輪都講一次。這件事寧可吵也不能忘記——
    「審查停著」跟「審查通過」在畫面上長得一模一樣。
    """
    data = load(project)
    until = float(data.get("paused_until") or 0)
    if until <= time.time():
        return ""
    kind = data.get("pause_kind") or ""
    if kind == "failures" and mode and data.get("pause_mode") != mode:
        return ""
    scope = ("「" + str(data.get("pause_mode")) + "」審查"
             if kind == "failures" else "審查")
    return ("cross-review：" + scope + "暫停中，" + _stamp(until)
            + " 自動恢復。原因：" + str(data.get("reason") or "未記錄")[:200]
            + "（要立刻恢復就刪掉 .claude/review/breaker.json）")
