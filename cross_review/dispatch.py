"""派工：決定「這一輪要審什麼」並寫成一份工作單。

hook（自動／門檻觸發）與 run_review.py --now（使用者手動觸發）都走這裡。
兩邊各寫一份的話，工作單的欄位會慢慢分岔，而分岔的症狀是手動觸發的審查
少看了某個東西——不會有錯誤訊息。
"""
from __future__ import annotations

import time
from pathlib import Path

from . import common, transcript as tx

# 手動／門檻模式下，工作單完成後由審查行程寫這個檔案，
# 再由 hook 在下一次 Stop 讀它、推進水位線。
# 不讓審查行程直接改 state.json：hook 也在寫同一個檔案，兩個行程互相覆蓋。
# （這就是斷路器狀態當初必須拆成 breaker.json 的同一個理由。）
RECEIPT = "reviewed.json"


def detect(project: Path, transcript_path: str, cursor: int, watermark: float):
    """這一輪（或累積至今）改到了什麼。回傳 (parsed, end_line, files, deleted)。"""
    parsed = tx.parse(Path(transcript_path), cursor) if transcript_path else {}
    end_line = parsed.get("end_line", cursor)
    files, deleted, _source = tx.changed_code_files(project, parsed, since=watermark)
    return parsed, end_line, files, deleted


def next_round(project: Path, state: dict) -> int:
    """下一個回合編號。

    不能只看 state["round"]：手動觸發的審查不寫 state.json（那是 hook 的
    責任），所以它建的工作單在 hook 兌現收據之前不會反映在 state 裡。
    掃一遍已存在的工作單就不會撞號。
    """
    highest = int(state.get("round", 0))
    try:
        for f in common.review_dir(project).glob("job-*.json"):
            m = f.stem.split("-", 1)
            if len(m) == 2 and m[1].isdigit():
                highest = max(highest, int(m[1]))
    except OSError:
        pass
    return highest + 1


def write_receipt(project: Path, job: dict, head_sha: str) -> None:
    """手動觸發的審查跑完後留下的收據，由 hook 在下一次 Stop 兌現。"""
    common.write_json(common.review_dir(project) / RECEIPT, {
        "round": job.get("round", 0),
        "transcript": job.get("transcript", ""),
        "end_line": job.get("end_line", 0),
        "watermark": time.time(),
        "head_sha": head_sha,
        "deleted": job.get("deleted") or [],
        "at": time.strftime("%Y-%m-%d %H:%M:%S"),
    })


def create_job(project: Path, round_no: int, transcript_path: str, cursor: int,
               end_line: int, watermark: float, files: list, deletions: list,
               base_sha: str):
    """寫出 job-N.json。不碰 state.json——那是 hook 的責任。"""
    job_path = common.review_dir(project) / ("job-" + str(round_no) + ".json")
    fingerprints = {}
    for path in files:
        try:
            st = Path(path).stat()
            fingerprints[path] = [round(st.st_mtime, 3), st.st_size]
        except OSError:
            pass
    common.write_json(job_path, {
        "round": round_no,
        "project": str(project),
        "transcript": transcript_path,
        "start_line": cursor,
        "end_line": end_line,
        "since": watermark,
        # 這一輪的起點是「**上一次審查當下**的 HEAD」，不是現在的 HEAD。
        # hook 在回合結束時才跑，那時執行者可能已經把這一輪 commit 掉了，
        # 拿當下的 HEAD 當基準，diff 只會剩下 commit 之後的零星改動。
        "base_sha": base_sha,
        "created": time.strftime("%Y-%m-%d %H:%M:%S"),
        "files": files,
        "deleted": deletions,
        # 每個檔案在派工當下的指紋。背景審查跑那 150～650 秒裡，執行者可能
        # 又改了同一個檔案，審查於是讀到下一回合的內容卻宣稱自己審的是這一輪。
        "fingerprints": fingerprints,
    })
    return job_path


def backlog_note(files: list, deletions: list) -> str:
    """沒有送審時每一輪講的那一行。

    寧可吵也不能沉默：「累積著沒審」跟「審過了沒問題」在畫面上長得一模一樣。
    """
    n = len(files) + len(deletions)
    return ("cross-review：累積 " + str(n) + " 個程式碼檔案還沒審查"
            "（要送審就說「審查」，或用 /cross-review）")


def over_threshold(cfg: dict, project: Path, files: list, deletions: list,
                   base_sha: str) -> str:
    """累積量是否已經大到該自動送一次。是的話回傳理由，否則空字串。

    只用來保證「不會漏」，不用來判斷「值不值得」——改一行設定可能比改三百行
    測試更危險，行數跟風險沒有關係。值不值得由使用者決定。
    """
    n = len(files) + len(deletions)
    limit_files = common.positive_int(cfg, "auto_when_files", 1)
    if n >= limit_files:
        return "累積 " + str(n) + " 個檔案（門檻 " + str(limit_files) + "）"
    limit_bytes = common.positive_int(cfg, "auto_when_diff_bytes", 1)
    if files and (project / ".git").exists():
        size = len(tx.git_diff(project, 10_000_000, files,
                               base=base_sha or "HEAD").encode("utf-8"))
        if size >= limit_bytes:
            return ("改動量 " + str(size // 1024) + " KB（門檻 "
                    + str(limit_bytes // 1024) + " KB）")
    return ""
