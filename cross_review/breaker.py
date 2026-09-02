"""額度／連續失敗的斷路器。

審查失敗時每一輪都重試是有害的：每次都再燒一次額度、再等一次、
再產生一次噪音，而且永遠不會自己停。

## 為什麼是「只追加的事件記錄」而不是一份狀態檔

這個檔案的併發問題被修過四次，每一次都是修「這一種交錯」：

1. 全部放 state.json —— hook 與審查互相覆蓋。
2. 拆出 breaker.json —— 背景審查自己就有兩個行程，仍然互相覆蓋。
3. 拆成每模式一檔 —— 但額度暫停還共用一個檔，兩邊解析到的恢復時間不同，
   後寫的會把期限縮短。
4. 寫入前重讀並取最大值 —— 額度暫停保住了，但「舊的成功」仍會抹掉
   「它讀完之後才出現的失敗」；補了那個之後，三個行程交錯時
   `max(0, 目前 - 讀到的)` 又會把新的失敗算成 0。

每一次補丁都更複雜，而且下一種交錯永遠還在後面。根因不是某個順序，
是**無鎖的「讀出 → 修改 → 整份寫回」**。

所以現在完全沒有這個動作：每次事件只**追加一行**（追加是原子的），
狀態一律用讀的推導出來。交錯於是不再是需要窮舉的東西。

連續失敗造成的暫停也是**推導**的（失敗數達門檻就從最後一次失敗算起），
不是寫入時決定再存起來——存起來就又需要合併，又回到原點。
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime, timedelta
from pathlib import Path

from . import common

# 這是 Codex 額度用完時的真實輸出（2026-09-02 實際擷取，不是猜的）：
#   ERROR: You have hit your usage limit. Upgrade to Pro (...),
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
REASON_MAX = 400          # 一行要夠短，追加才是一次寫入

_KNOWN_MODES = ("code", "visual")


def _log_path(project: Path, mode: str) -> Path:
    return common.review_dir(project) / ("breaker." + str(mode) + ".log")


def _float(value) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _append(project: Path, mode: str, event: dict) -> None:
    """追加一行。這是這個模組唯一的寫入動作，而且不讀取既有內容。"""
    path = _log_path(project, mode)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8", newline="\n") as fh:
            print(json.dumps(event, ensure_ascii=False), file=fh)
            fh.flush()
    except OSError:
        pass          # 記不下來不該讓審查失敗


def _events(project: Path, mode: str) -> list:
    path = _log_path(project, mode)
    if not path.exists():
        return []
    out = []
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for raw in fh:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    obj = json.loads(raw)
                except Exception:
                    continue      # 半行（極少見的交錯）不該讓整份讀不出來
                if isinstance(obj, dict):
                    out.append(obj)
    except OSError:
        return []
    return out


def _from_legacy(project: Path, mode: str) -> dict:
    """舊版的狀態檔。只讀不寫，讓升級的專案不會突然失去還有效的暫停。

    舊格式有兩代：共用的 breaker.json（可能帶 pause_kind／pause_mode／
    mode_pause），以及每模式一個的 breaker.<模式>.json。兩者都要看。
    """
    out = {"failures": 0, "paused_until": 0.0, "account_until": 0.0, "reason": ""}
    rdir = common.review_dir(project)

    shared = common.read_json(rdir / "breaker.json")
    if isinstance(shared, dict):
        fails = shared.get("failures")
        count = fails.get(mode) if isinstance(fails, dict) else shared.get(
            "consecutive_failures")
        out["failures"] = count if isinstance(count, int) and count >= 0 else 0
        kind = str(shared.get("pause_kind") or "")
        until = _float(shared.get("paused_until"))
        if kind in ("quota", "rate"):
            out["account_until"] = until
        elif kind == "failures" and str(shared.get("pause_mode") or "") == mode:
            out["paused_until"] = until
        entry = (shared.get("mode_pause") or {}).get(mode)
        if isinstance(entry, dict):
            out["paused_until"] = max(out["paused_until"], _float(entry.get("until")))
        out["reason"] = str(shared.get("reason") or "")

    per = common.read_json(rdir / ("breaker." + str(mode) + ".json"))
    if isinstance(per, dict) and per:
        fails = per.get("failures")
        if isinstance(fails, int) and fails > out["failures"]:
            out["failures"] = fails
        out["paused_until"] = max(out["paused_until"], _float(per.get("paused_until")))
        acc = _float(per.get("account_until"))
        if acc > out["account_until"]:
            out["account_until"] = acc
            out["reason"] = str(per.get("reason") or "") or out["reason"]
        elif not out["reason"]:
            out["reason"] = str(per.get("reason") or "")
    return out


def _state(project: Path, mode: str) -> dict:
    """把事件記錄推導成目前的狀態。沒有任何寫入。"""
    mode = str(mode or "code")
    events = _events(project, mode)
    legacy = _from_legacy(project, mode)

    # 帳號層級（額度／限流）。只有時間能解除，所以連「最後一次成功之前」
    # 的事件也要算。
    #
    # 伺服器明講的恢復時間（`try again at 1:27 PM`）優先於兜底的一小時：
    # 單純取最大值的話，另一個模式沒解析到時間而用兜底值，會把 13:27
    # 拉長成 14:25——不會提前重試（安全），但白等一小時（浪費）。
    exact, exact_reason = 0.0, ""
    rough, rough_reason = legacy["account_until"], legacy["reason"]
    for event in events:
        value = _float(event.get("account_until"))
        if not value:
            continue
        if event.get("account_exact"):
            if value > exact:
                exact, exact_reason = value, str(event.get("reason") or "")
        elif value > rough:
            rough, rough_reason = value, str(event.get("reason") or "")
    # 這裡**不做**「明確優先於兜底」的取捨——那要跨模式才算得對。
    # 只在單一模式內取捨的話，paused_note() 彙整 code 與 visual 時又會退回
    # 單純取最大值，於是 code 解析到的 13:52 仍會被 visual 兜底的 14:32 拉長。
    # （上一版就是這樣：修在 _state() 裡，實際生效的判斷卻在 paused_note()。）
    account = max(exact, rough)
    account_reason = exact_reason if exact >= rough else rough_reason

    def wrap(failures, paused, reason):
        return {"failures": failures, "paused_until": paused, "reason": reason,
                "account_until": account, "account_reason": account_reason,
                # 分開帶出去，讓 paused_note() 跨模式做「明確優先」的取捨。
                "exact_until": exact, "exact_reason": exact_reason,
                "rough_until": rough, "rough_reason": rough_reason}

    if not events:
        return wrap(legacy["failures"], legacy["paused_until"], legacy["reason"])

    last_ok = -1
    for i, event in enumerate(events):
        if event.get("ok"):
            last_ok = i
    recent = [e for e in events[last_ok + 1:] if not e.get("ok")]
    # 記錄裡還沒有任何一次成功時，舊格式累積的失敗次數要繼續算——否則升級
    # 當下舊檔記著 2 次，新格式再失敗一次只會算成 1，門檻永遠差一步。
    base = legacy["failures"] if last_ok < 0 else 0
    failures = len(recent) + base
    reason = str(recent[-1].get("reason") or "") if recent else legacy["reason"]

    # 這個模式的暫停是**推導**出來的，不是存起來的：失敗數達門檻，
    # 就從最後一次失敗算起冷卻。存起來的話又要合併，又回到讀改寫回。
    paused = (_float(recent[-1].get("t")) + COOLDOWN_SEC
              if (failures >= MAX_FAILURES and recent) else 0.0)
    if legacy["paused_until"] > paused:
        paused = legacy["paused_until"]
        if not reason:
            reason = legacy["reason"]
    return wrap(failures, paused, reason)


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
            + "（要立刻恢復就刪掉 .claude/review/breaker*）")


def record_failure(project: Path, message: str, mode: str = "code") -> str:
    """記一次失敗，必要時跳閘。回傳要告訴使用者的話（沒事就是空字串）。"""
    mode = str(mode or "code")
    reset_at = parse_reset_time(message)
    quota = is_quota_error(message)
    account_until = 0.0
    if quota or (_RATE.search(message or "") and reset_at):
        # reset_at 有值＝伺服器明講了時間，那比兜底的一小時精準。
        # 帳號層級的限制，擋掉所有模式。期限寫進事件本身，讀的時候取最大值——
        # 另一個模式沒解析到時間而用兜底的一小時，也蓋不短這一個。
        account_until = reset_at or (time.time() + COOLDOWN_SEC)

    _append(project, mode, {
        "t": time.time(),
        "ok": False,
        "reason": str(message)[:REASON_MAX],
        "account_until": account_until,
        "account_exact": bool(reset_at),
    })

    # 訊息從追加之後的狀態推導，才會跟實際生效的一致。
    state = _state(project, mode)
    note = ""
    if account_until:
        note = (("額度已用完，審查暫停到 " if quota else "被限流，審查暫停到 ")
                + _stamp(state["account_until"]) + "（時間到自動恢復）")
    elif state["paused_until"] > time.time():
        note = ("「" + mode + "」連續 " + str(state["failures"])
                + " 次審查失敗，這個模式暫停 " + str(COOLDOWN_SEC // 60)
                + " 分鐘（時間到自動恢復）")
    if note:
        common.log_error(project, "斷路器跳閘：" + note + "。最後的錯誤：" + str(message)[:300])
    return note


def record_success(project: Path, mode: str = "code") -> None:
    """記一次成功。之後的連續失敗從這裡重新算起。

    帳號層級的暫停不受影響：那是額度／限流，只有時間能解除。併行派工時，
    後完成的那個成功很可能是在額度耗盡之前就送出去的，拿它來解除等於
    立刻再燒一次。
    """
    mode = str(mode or "code")
    # 沒有累積中的失敗時，這一筆成功不帶任何資訊。跳過它，正常運作
    # （一路成功）的專案記錄就一直是空的，不會每輪長一行。
    if _state(project, mode)["failures"] <= 0:
        return
    _append(project, mode, {"t": time.time(), "ok": True})


def paused_note(project: Path, mode: str = None) -> str:
    """目前在暫停中的話，回傳要告訴使用者的話；否則空字串。

    傳 mode 就只問那個模式的連續失敗暫停——程式碼審查壞掉不該連帶讓
    視覺審查也停下來。帳號層級的暫停一律擋所有模式，而且要看過每一個
    模式取最大值：期限是各自從錯誤訊息解析出來的，不一定相同。

    暫停期間每一輪都講一次。這件事寧可吵也不能忘記——
    「審查停著」跟「審查通過」在畫面上長得一模一樣。
    """
    now = time.time()
    # 每個模式只推導一次。原本帳號層級掃一輪、模式暫停再掃一輪，
    # 同一份記錄被解析兩次。
    states = {name: _state(project, name) for name in _KNOWN_MODES}

    # 帳號層級的取捨要在**這裡**做，因為只有這裡看得到所有模式：
    # 伺服器明講的恢復時間優先於兜底的一小時，否則另一個模式沒解析到時間
    # 而用兜底值，會把 13:52 拉長成 14:32——不會提前重試（安全），但白等。
    exact = max((s["exact_until"] for s in states.values()), default=0.0)
    rough = max((s["rough_until"] for s in states.values()), default=0.0)
    if exact > now:
        reason = next((s["exact_reason"] for s in states.values()
                       if s["exact_until"] == exact), "")
        return _note("審查", exact, reason)
    if rough > now:
        reason = next((s["rough_reason"] for s in states.values()
                       if s["rough_until"] == rough), "")
        return _note("審查", rough, reason)

    for name in ([str(mode)] if mode else _KNOWN_MODES):
        state = states.get(name) or _state(project, name)
        if state["paused_until"] > now:
            return _note("「" + name + "」審查", state["paused_until"], state["reason"])
    return ""
