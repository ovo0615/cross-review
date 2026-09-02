"""Stop hook：一個必須瞬間完成的本地檢查（ADR-0002）。

它不呼叫審查者，不花使用者任何時間。它只回答兩個問題：
  1. 這一回合改到程式碼了嗎？
  2. 送審了嗎？
沒送就攔阻，攔阻理由就是那兩行要執行的背景指令（第 11 題）。
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from . import breaker, common, dispatch, transcript as tx

TOOL_ROOT = Path(__file__).resolve().parent.parent
RUNNER = TOOL_ROOT / "run_review.py"


def emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False))
    sys.stdout.flush()


def block(reason: str) -> None:
    emit({"decision": "block", "reason": reason})


def passthrough(message: str = "") -> None:
    if message:
        emit({"systemMessage": message})
    sys.exit(0)


def modes_started(job_path: Path, modes: list) -> list:
    """回傳「派出去了但沒被啟動」的模式。

    不能只看有沒有任何一個 .started：兩個審查只跑了一個時，
    那一輪仍然會被當成審過。
    """
    missing = []
    for mode in modes:
        if not (job_path.parent / (job_path.stem + "." + mode + ".started")).exists():
            missing.append(mode)
    return missing


def build_reason(job_path: Path, files: list, project: Path, modes: list) -> str:
    rel = []
    for path in files[:8]:
        try:
            rel.append(str(Path(path).relative_to(project)))
        except Exception:
            rel.append(str(path))
    more = "" if len(files) <= 8 else "（另有 " + str(len(files) - 8) + " 個）"

    lines = [
        "cross-review：這一回合改到了 " + str(len(files)) + " 個程式碼檔案，但還沒送審。",
        "",
        "改到的是：" + "、".join(rel) + more,
        "",
        "請用**背景任務**執行下面的指令（Bash 工具，run_in_background=true）。",
        "不要在前景等它們——審查要 20 秒到 11 分鐘不等，跑完 harness 會自動叫醒你。",
        "",
    ]
    # 用跑這支 hook 的同一個直譯器，不要硬寫 py -3：那假設了 Windows Python
    # Launcher 存在。sys.executable 一定跑得起來，因為它就是現在正在跑的東西。
    python = sys.executable or "py -3"
    # 視覺排前面：它 20 秒就回來，程式碼要 150～650 秒（ADR-0003）。
    for mode in modes:
        lines.append('"' + python + '" "' + str(RUNNER) + '" --job "'
                     + str(job_path) + '" --mode ' + mode)
    lines += [
        "",
        "丟出去之後就直接收尾、把話回完給使用者，不要等結果、不要在回覆裡預測審查會說什麼。",
        "審查報告回來時再處理：把發現講給使用者聽，附上你自己同不同意的判斷，但不要自己動手改。",
    ]
    return "\n".join(lines)


def main() -> int:
    common.force_utf8_stdio()

    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except Exception:
        return 0  # 讀不到輸入就安靜放行，絕不擋住使用者做事

    cwd = payload.get("cwd") or ""
    transcript_path = payload.get("transcript_path") or ""
    if not cwd or not transcript_path:
        return 0

    if not Path(cwd).exists():
        return 0
    # 不能直接信 cwd —— 它會跟著 shell 漂移（跑 npm 時切進 web_app\frontend
    # 之類）。往上找真正的專案根目錄，否則會在子目錄裡另開一套審查狀態。
    project = common.find_project_root(Path(cwd))
    if common.is_disabled(project):
        return 0
    if not common.review_dir_is_safe(project):
        # 專案把 .claude/review 指到外面去了。什麼都不寫，只出聲。
        passthrough("cross-review：" + str(common.review_dir(project))
                    + " 指向專案外部，拒絕在那裡寫入任何東西，這個專案的審查已停用。")

    cfg = common.ensure_config(project)
    if not cfg.get("enabled", True):
        return 0

    modes = []
    if cfg.get("visual_review", True) and cfg.get("shots"):
        modes.append("visual")   # 快的排前面（ADR-0003）
    if cfg.get("code_review", True):
        modes.append("code")
    if not modes:
        return 0  # 兩種審查都關掉了，這個專案等於停用

    # 斷路器跳閘中就不要再送審——每次重試都是再燒一次額度然後再失敗一次。
    # 但一定要講出來：「審查停著」跟「審查通過」在畫面上長得一模一樣。
    # 額度／限流的暫停擋掉所有模式；某個模式自己連續失敗造成的暫停只擋它自己，
    # 程式碼審查壞掉不該連帶讓視覺審查也停下來。
    pause_notes = [note for note in
                   (breaker.paused_note(project, m) for m in modes) if note]
    modes = [m for m in modes if not breaker.paused_note(project, m)]
    if not modes:
        passthrough(pause_notes[0])

    state = common.load_state(project)

    # 手動觸發的審查跑完只留一張收據，不直接改 state.json——hook 也在寫
    # 同一個檔案，兩個行程互相覆蓋。收據在這裡兌現（單一寫入者仍是 hook）。
    receipt = common.read_json(common.review_dir(project) / dispatch.RECEIPT)
    if (isinstance(receipt, dict)
            and int(receipt.get("round") or 0) > int(state.get("receipt_round") or 0)):
        state["watermark"] = float(receipt.get("watermark")
                                   or state.get("watermark", 0.0))
        state["head_sha"] = receipt.get("head_sha") or state.get("head_sha", "")
        state["round"] = max(int(state.get("round", 0)),
                             int(receipt.get("round") or 0))
        state["reported_deletions"] = sorted(
            set(state.get("reported_deletions") or [])
            | set(receipt.get("deleted") or []))
        if receipt.get("transcript") == transcript_path:
            # 換過 session 的話，收據裡的行號指的是另一份逐字稿，不能套用。
            state["cursor"] = int(receipt.get("end_line") or state.get("cursor", 0))
        state["receipt_round"] = int(receipt.get("round") or 0)
        common.save_state(project, state)
    if state.get("transcript") != transcript_path:
        # 新的 session：游標從頭算起。
        stale = state.get("pending")
        state["cursor"] = 0
        state["transcript"] = transcript_path
        state["pending"] = None
        if stale:
            # 上一個 session 攔阻之後就結束了，那批改動從來沒送審。
            # 水位線已經被推到攔阻當下，如果照樣沿用，那些檔案的 mtime
            # 全都早於水位線，之後永遠不會再被發現。把水位線退回去。
            state["watermark"] = float(stale.get("prev_watermark", 0.0))
            # 刪除不看 mtime，靠 reported_deletions 去重。只退水位線不清這份
            # 清單的話，那個刪除已經被標記成報告過了，之後永遠不會再送審。
            stale_job = common.read_json(stale.get("job")) or {}
            forget = set(stale_job.get("deleted") or [])
            if forget:
                state["reported_deletions"] = sorted(
                    set(state.get("reported_deletions") or []) - forget)
            common.log_error(
                project,
                "回合 #" + str(stale.get("round")) + " 在上一個 session 攔阻後沒送審就結束了，"
                "水位線已退回、刪除紀錄已清除，這批改動會在本 session 重新送審。",
            )

    rdir = common.review_dir(project)
    pending = state.get("pending")

    # ---- 先處理上一次攔阻留下的工作單 ----
    if pending:
        job_path = Path(pending["job"])
        missing = modes_started(job_path, pending.get("modes") or ["code"])
        state["cursor"] = pending["end_line"]
        state["watermark"] = pending.get("watermark", state.get("watermark", 0.0))
        state["pending"] = None
        common.save_state(project, state)

        if missing:
            # 已經擋過一次卻還是沒跑（或只跑了一半）。不再擋——避免無窮迴圈——
            # 但一定要出聲。這種失敗長得跟「審過了沒問題」一模一樣，最危險。
            note = "回合 #" + str(pending["round"]) + " 有審查沒有被啟動：" + "、".join(missing)
            common.log_error(project, note + "，已放棄該回合的這些審查。")
            passthrough("cross-review：" + note + "。這一輪那部分沒有人審過，已記到 .claude/review/errors.log。")

        # 全部都啟動了。記下來，之後回頭確認它們有沒有真的跑完。
        state["last_job"] = {
            "round": pending["round"],
            "job": pending["job"],
            "modes": pending.get("modes") or ["code"],
            "deadline": time.time() + float(common.positive_int(cfg, "codex_timeout_sec", 30)) + 180,
        }
        common.save_state(project, state)

    # ---- 上一個工作單有沒有真的跑完 ----
    # `.started` 只證明它啟動了；啟動後立刻崩潰
    # 或被中斷的話，那一輪會看起來像審過了。這裡不擋（擋了就變同步），但要出聲。
    #
    # 檢查不能太急：第二次 Stop 只比第一次晚一秒，那時審查才剛開始。
    # 要等過了逾時上限才算數——review.py 正常結束（含失敗結束）都會寫 .done，
    # 只有硬崩潰或被殺才不會。
    last = state.get("last_job")
    if last:
        job_path = Path(last["job"])
        unfinished = [m for m in last.get("modes", [])
                      if not (job_path.parent / (job_path.stem + "." + m + ".done")).exists()]
        if not unfinished:
            state["last_job"] = None
            common.save_state(project, state)
        elif time.time() > float(last.get("deadline", 0)):
            state["last_job"] = None
            note = ("回合 #" + str(last["round"]) + " 的審查啟動後沒有跑完："
                    + "、".join(unfinished))
            common.log_error(project, note + "。可能中途崩潰或被中斷，沒有結果。")
            common.save_state(project, state)
            passthrough("cross-review：" + note
                        + "。那部分沒有結果，已記到 .claude/review/errors.log。")

    watermark = float(state.get("watermark", 0.0))
    if not watermark:
        # 這個 session 的第一輪：水位線設成 transcript 的建立時間，
        # 也就是「這個 session 開始的時刻」。比這個新的改動就是這個 session 的工作。
        # 不能用 0——那會把整個專案的所有程式碼檔都當成本回合改動。
        try:
            watermark = Path(transcript_path).stat().st_ctime
        except OSError:
            watermark = time.time() - 900
    parsed, end_line, files, deleted = dispatch.detect(
        project, transcript_path, state.get("cursor", 0), watermark)

    # 刪除沒有 mtime 可比，而 git status 會一直列出它直到 commit。
    # 只報告沒報告過的那些，否則就變成當初那個反覆攔阻的 bug。
    # 但這份清單必須會縮：檔案重新出現時要把它移出去，
    # 否則「刪掉→重建→再刪」的第二次刪除會被永遠當成報告過而漏審。
    reported = {p for p in (state.get("reported_deletions") or [])
                if not Path(p).exists()}
    fresh_deletions = [p for p in deleted if p not in reported]

    if not files and not fresh_deletions:
        state["cursor"] = end_line
        common.save_state(project, state)
        return 0  # 沒改程式碼：完全靜默，零成本（第 2 題的閘門）

    # ---- 手動／門檻模式的閘門 ----
    # 每一輪都審，在小修時完全不划算：實測一次約 11 萬 tokens、170～650 秒。
    # manual 永不自動送；threshold 只在累積量大到可能漏掉時才自動送一次。
    trigger = str(cfg.get("trigger") or "auto").lower()
    if trigger not in common.TRIGGERS:
        trigger = "auto"
    auto_reason = ""
    if trigger != "auto":
        if trigger == "threshold":
            auto_reason = dispatch.over_threshold(
                cfg, project, files, fresh_deletions, state.get("head_sha") or "")
        if not auto_reason:
            # 不派工就什麼都不推進——水位線、游標、head_sha、reported_deletions
            # 全部維持原樣，這批改動於是留在累積裡，下一輪繼續被看見。
            common.save_state(project, state)
            passthrough(dispatch.backlog_note(files, fresh_deletions))

    state["reported_deletions"] = sorted(reported | set(fresh_deletions))

    # ---- 建工作單並攔阻 ----
    round_no = dispatch.next_round(project, state)
    now = time.time()
    head_now = tx.git_head(project)
    job_path = dispatch.create_job(
        project, round_no, transcript_path, state.get("cursor", 0), end_line,
        watermark, files, fresh_deletions,
        base_sha=state.get("head_sha") or head_now)

    # 這一次的 HEAD 就是下一輪的起點。
    state["head_sha"] = head_now
    state["round"] = round_no
    state["pending"] = {
        "round": round_no,
        "job": str(job_path),
        "end_line": end_line,
        "modes": modes,
        # 水位線在派工當下就定住，下一輪只看比這個新的改動。
        "watermark": now,
        # 派工前的水位線。這個 pending 若沒送審就隨 session 消失，
        # 要靠它把水位線退回去，否則那批改動再也不會被發現。
        "prev_watermark": watermark,
    }
    common.save_state(project, state)

    reason = build_reason(job_path, files + fresh_deletions, project, modes)
    if auto_reason:
        reason = ("cross-review：" + auto_reason + "，已超過自動送審門檻。"
                  + "（要改成完全手動就用 /cross-review manual）\n\n") + reason
    if pause_notes:
        # 部分暫停：還是要派工，但不能讓「有一種審查停著」這件事消失。
        reason = pause_notes[0] + "\n\n" + reason
    block(reason)
    return 0


if __name__ == "__main__":
    sys.exit(main())
