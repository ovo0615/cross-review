"""額度／連續失敗的斷路器。

審查失敗時每一輪都重試是有害的：每次都再燒一次額度、再等一次、
再產生一次噪音，而且永遠不會自己停。

## 為什麼沒有共用的狀態檔

「讀出整份 → 改 → 整份寫回」在有第二個寫入者時就是競態。這個工具有
**三個**會動狀態的行程：hook、程式碼審查、視覺審查。後兩者是併行的。

拆檔拆了三次才拆乾淨，每次都是同一個道理沒推到底：

1. 一開始全部放 state.json —— hook 與審查互相覆蓋。
2. 拆出 breaker.json —— 但背景審查自己就有兩個行程，仍然互相覆蓋。
   實測重現過：code 記下額度暫停後，visual 用更早的快照寫回，暫停消失。
3. 拆成 breaker.<模式>.json，但額度暫停還留在共用的 breaker.json。
   當時的理由是「兩個模式寫進去的是同一件事實」——**那是錯的**：
   一邊可能從訊息解析到明確的恢復時間，另一邊沒解析到而用一小時兜底，
   後寫的那個會把期限縮短，工具就在額度還受限時提前重試。

現在**沒有任何檔案有兩個寫入者**：每個模式只寫 `breaker.<模式>.json`，
帳號層級的暫停也記在各自的檔案裡，讀的時候取所有模式的**最大值**。
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

_KNOWN_MODES = ("code", "visual")
_LEGACY_FILE = "breaker.json"      # 舊格式，只讀不寫


def _mode_path(project: Path, mode: str) -> Path:
    return common.review_dir(project) / ("breaker." + str(mode) + ".json")


def _float(value) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _from_legacy(project: Path, mode: str) -> dict:
    """把舊版的單一檔案讀成這個模式的狀態。只讀，不回寫。

    沒有這段的話，升級之後舊的 `pause_kind == "failures"` 會被當成帳號層級的
    暫停，同時擋住 code 與 visual，而且成功也解除不了——使用者只會看到
    「審查一直停著」而沒有任何線索。
    """
    data = common.read_json(common.review_dir(project) / _LEGACY_FILE)
    if not isinstance(data, dict):
        return {"failures": 0, "paused_until": 0.0, "account_until": 0.0, "reason": ""}

    fails = data.get("failures")
    if isinstance(fails, dict):
        count = fails.get(mode)
    else:
        count = data.get("consecutive_failures")
    count = count if isinstance(count, int) and count >= 0 else 0

    kind = str(data.get("pause_kind") or "")
    until = _float(data.get("paused_until"))
    mine, account = 0.0, 0.0
    if kind in ("quota", "rate"):
        account = until
    elif kind == "failures" and str(data.get("pause_mode") or "") == mode:
        mine = until
    entry = (data.get("mode_pause") or {}).get(mode)
    if isinstance(entry, dict):
        mine = max(mine, _float(entry.get("until")))
    return {"failures": count, "paused_until": mine,
            "account_until": account, "reason": str(data.get("reason") or "")}


def _load_mode(project: Path, mode: str) -> dict:
    data = common.read_json(_mode_path(project, mode))
    if not isinstance(data, dict) or not data:
        return _from_legacy(project, mode)
    failures = data.get("failures")
    return {
        "failures": failures if isinstance(failures, int) and failures >= 0 else 0,
        "paused_until": _float(data.get("paused_until")),
        "account_until": _float(data.get("account_until")),
        "reason": str(data.get("reason") or ""),
    }


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


def _note(scope: str, until: float, reason) -> str:
    return ("cross-review：" + scope + "暫停中，" + _stamp(until)
            + " 自動恢復。原因：" + str(reason or "未記錄")[:200]
            + "（要立刻恢復就刪掉 .claude/review/breaker*.json）")


def record_failure(project: Path, message: str, mode: str = "code") -> str:
    """記一次失敗，必要時跳閘。回傳要告訴使用者的話（沒事就是空字串）。"""
    mode = str(mode or "code")
    data = _load_mode(project, mode)
    data["failures"] += 1
    data["reason"] = str(message)[:500]
    note = ""

    reset_at = parse_reset_time(message)
    quota = is_quota_error(message)
    if quota or (_RATE.search(message or "") and reset_at):
        # 帳號層級的限制，擋掉所有模式。記在自己的檔案裡，讀的時候取最大值——
        # 寫進共用檔的話，兜底的一小時會把另一邊解析到的明確期限蓋短。
        until = reset_at or (time.time() + COOLDOWN_SEC)
        data["account_until"] = max(data["account_until"], until)
        note = (("額度已用完，審查暫停到 " if quota else "被限流，審查暫停到 ")
                + _stamp(data["account_until"]) + "（時間到自動恢復）")
    elif data["failures"] >= MAX_FAILURES:
        data["paused_until"] = time.time() + COOLDOWN_SEC
        note = ("「" + mode + "」連續 " + str(data["failures"])
                + " 次審查失敗，這個模式暫停 " + str(COOLDOWN_SEC // 60)
                + " 分鐘（時間到自動恢復）")

    common.write_json(_mode_path(project, mode), data)
    if note:
        common.log_error(project, "斷路器跳閘：" + note + "。最後的錯誤：" + str(message)[:300])
    return note


def record_success(project: Path, mode: str = "code") -> None:
    """成功一次就把**這個模式**的計數與暫停歸零。

    帳號層級的暫停（account_until）不清：那是額度／限流，只有時間能解除。
    併行派工時，後完成的那個成功很可能是在額度耗盡之前就送出去的，
    拿它來解除等於立刻再燒一次。
    """
    mode = str(mode or "code")
    data = _load_mode(project, mode)
    if data["failures"] or data["paused_until"]:
        common.write_json(_mode_path(project, mode), {
            "failures": 0,
            "paused_until": 0.0,
            "account_until": data["account_until"],
            "reason": "",
        })


def paused_note(project: Path, mode: str = None) -> str:
    """目前在暫停中的話，回傳要告訴使用者的話；否則空字串。

    傳 mode 就只問那個模式的連續失敗暫停——程式碼審查壞掉不該連帶讓
    視覺審查也停下來。帳號層級的暫停一律擋所有模式，而且要看過每一個
    模式的檔案取最大值：期限是各自從錯誤訊息解析出來的，不一定相同。

    暫停期間每一輪都講一次。這件事寧可吵也不能忘記——
    「審查停著」跟「審查通過」在畫面上長得一模一樣。
    """
    now = time.time()
    best, reason = 0.0, ""
    for name in _KNOWN_MODES:
        data = _load_mode(project, name)
        if data["account_until"] > best:
            best, reason = data["account_until"], data["reason"]
    if best > now:
        return _note("審查", best, reason)

    for name in ([str(mode)] if mode else _KNOWN_MODES):
        data = _load_mode(project, name)
        if data["paused_until"] > now:
            return _note("「" + name + "」審查", data["paused_until"], data["reason"])
    return ""
