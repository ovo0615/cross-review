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


def detect(project: Path, transcript_path: str, cursor: int, watermark: float,
           ignore=None):
    """這一輪（或累積至今）改到了什麼。回傳 (parsed, end_line, files, deleted)。

    `ignore` 是專案設定裡的 ignore_paths：同一個工作目錄裡有第二個代理人時，
    它負責的目錄不該被算成我這一輪的工作。
    """
    parsed = tx.parse(Path(transcript_path), cursor) if transcript_path else {}
    end_line = parsed.get("end_line", cursor)
    files, deleted, _source = tx.changed_code_files(
        project, parsed, since=watermark, ignore=ignore)
    if ignore:
        # 走目錄那條路已經剪掉了，但 git status 與工具紀錄那兩條沒有，
        # 所以這裡仍要再過濾一次。
        keep = lambda p: not _under_any(project, p, ignore)
        files = [f for f in files if keep(f)]
        deleted = [d for d in deleted if keep(d)]
    return parsed, end_line, files, deleted


def _under_any(project: Path, path, prefixes) -> bool:
    """path 是否落在 prefixes 列出的任何一個目錄底下。"""
    try:
        rel = Path(path).resolve().relative_to(Path(project).resolve())
    except (ValueError, OSError):
        return False
    return tx.parts_ignored(rel.parts, tx.ignore_prefixes(prefixes))


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


def all_modes_done(job_path, modes: list) -> bool:
    """每一種審查都產出報告了嗎。

    只要有一種沒跑完就不能推進水位線：那批改動沒有被那一種審查看過，
    而使用者看到的會是「審過了」。
    """
    if not modes:
        return False
    return all((job_path.with_suffix("." + m + ".done")).exists() for m in modes)


def write_receipt(project: Path, job: dict, head_sha: str) -> None:
    """手動觸發的審查跑完後留下的收據，由 hook 在下一次 Stop 兌現。"""
    common.write_json(common.review_dir(project) / RECEIPT, {
        "round": job.get("round", 0),
        "transcript": job.get("transcript", ""),
        "end_line": job.get("end_line", 0),
        # 一定要用**派工當下**的時刻，不能用現在。審查要跑 150～650 秒，
        # 這段期間執行者又改的檔案沒進材料包（工作單早就釘死了），
        # 用審完的時刻當水位線會把它們算成已審，下一輪永遠看不到——
        # 而且不會有任何訊息。自動那條路用的就是派工當下（hook 的 now）。
        "watermark": float(job.get("dispatched") or 0) or time.time(),
        "head_sha": head_sha,
        "deleted": job.get("deleted") or [],
        "at": time.strftime("%Y-%m-%d %H:%M:%S"),
    })


def create_job(project: Path, round_no: int, transcript_path: str, cursor: int,
               end_line: int, watermark: float, files: list, deletions: list,
               base_sha: str, dispatched: float = None):
    """寫出 job-N.json。不碰 state.json——那是 hook 的責任。

    `dispatched` 必須是**偵測開始之前**取的時刻，由呼叫端傳進來。
    在這裡才 time.time() 的話，偵測掃描那段期間（實測 44～165 ms）被改的
    檔案會落進空窗：掃描已經走過它，所以不在 files 裡；mtime 卻早於新的
    水位線，收據兌現後就永遠不會再被偵測到——而且不會有任何訊息。
    """
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
        # 偵測開始當下的時刻。手動觸發時由收據沿用它當新的水位線。
        "dispatched": float(dispatched) if dispatched else time.time(),
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
    if not files:
        return ""
    text = ""
    if (project / ".git").exists():
        text = tx.git_diff(project, 10_000_000, files, base=base_sha or "HEAD")
    size = len(text.encode("utf-8"))
    # 只量 diff 會漏掉新增的檔案：未追蹤的檔案根本不在 git diff 裡，
    # 於是一個 60 KB 的全新檔案量出來是 0，兩個門檻都擋不住它。
    # 非 git 專案更是完全沒有 diff。沒被 diff 涵蓋的就用檔案實際大小算。
    covered = tx.diff_covers(text, project) if text else set()
    for path in files:
        try:
            resolved = str(Path(path).resolve())
        except OSError:
            continue
        if resolved in covered:
            continue
        try:
            size += Path(path).stat().st_size
        except OSError:
            pass
    if size >= limit_bytes:
        return ("改動量 " + str(size // 1024) + " KB（門檻 "
                + str(limit_bytes // 1024) + " KB）")
    return ""
