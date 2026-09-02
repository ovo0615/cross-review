"""用量帳本。

報告開頭那一行只告訴你「這一次」花了多少。要判斷「該不該調整設定」
需要的是趨勢，而趨勢不能靠翻報告——實際發生過：要算 26 輪的總量，
只能一份一份報告去撈，那不是可以持續做的事。

每一次審查記一行，之後隨時彙總得出來。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from . import common

FILENAME = "usage.jsonl"


def _path(project: Path) -> Path:
    return common.review_dir(project) / FILENAME


def record(project: Path, mode: str, round_no: int, data: dict,
           dossier_bytes: int = 0) -> None:
    """記一次審查。寫不進去就算了——記帳失敗不該讓審查失敗。"""
    if not common.review_child_is_safe(project, FILENAME):
        return
    row = {
        "at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "mode": mode,
        "round": round_no,
        "model": data.get("_model", ""),
        "effort": data.get("_effort", ""),
        "seconds": data.get("_elapsed_sec", 0),
        "tokens": data.get("_tokens", 0),
        "dossier_bytes": dossier_bytes,
        "findings": len(data.get("findings") or []),
        "blocking": bool(data.get("blocking")),
        # 這一趟到底有沒有跑成。失敗的那幾趟也要進帳本（額度照燒），
        # 但彙總時要看得出來哪些是白花的。
        "ok": not data.get("_failed"),
    }
    try:
        path = _path(project)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        pass


def read_rows(project: Path) -> list:
    path = _path(project)
    if not path.exists():
        return []
    rows = []
    try:
        for line in common.read_text(path).splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue          # 壞掉的一行不該讓整份帳本讀不出來
            if not isinstance(obj, dict):
                # `null`、陣列、數字都是合法 JSON 卻不是一筆紀錄。
                # 只擋 JSONDecodeError 的話，這種行會一路帶到 summary()
                # 的 r.get() 那裡才炸掉——註解說會略過壞行，實際上是崩潰。
                continue
            rows.append(obj)
    except Exception:
        return []
    return rows


def _num(value, default=0):
    """帳本裡的欄位型別不保證正確（手改過、程式換過版）。
    直接 int()/float() 一個字串就是整份彙總崩掉，寧可當成 0。"""
    try:
        return type(default)(value)
    except (TypeError, ValueError):
        return default


def summary(project: Path) -> str:
    rows = read_rows(project)
    if not rows:
        return "還沒有任何用量紀錄（" + str(_path(project)) + " 不存在或是空的）。"

    total = sum(_num(r.get("tokens"), 0) for r in rows)
    secs = sum(_num(r.get("seconds"), 0.0) for r in rows)
    lines = [
        "用量彙總：" + str(project),
        "",
        "共 %d 次審查，%s tokens，累計 %.0f 分鐘" % (len(rows), format(total, ","), secs / 60),
        "",
        "%-12s %5s %14s %12s %8s" % ("模型", "次數", "tokens", "平均", "分鐘"),
        "-" * 56,
    ]
    by_model = {}
    for r in rows:
        # str() 不能省：帳本裡的 model 若是數字，這裡的字串相接會 TypeError，
        # 跟上面 _num() 想達成的「壞行不該弄崩整份彙總」是同一件事。
        key = str(r.get("model") or "?") + " / " + str(r.get("effort") or "?")
        acc = by_model.setdefault(key, {"n": 0, "tok": 0, "sec": 0.0})
        acc["n"] += 1
        acc["tok"] += _num(r.get("tokens"), 0)
        acc["sec"] += _num(r.get("seconds"), 0.0)
    for key in sorted(by_model, key=lambda k: -by_model[k]["tok"]):
        a = by_model[key]
        lines.append("%-12s %5d %14s %12s %8.0f"
                     % (key[:12], a["n"], format(a["tok"], ","),
                        format(a["tok"] // max(1, a["n"]), ","), a["sec"] / 60))

    lines += ["", "最近 5 次：", ""]
    for r in rows[-5:]:
        lines.append("  %s  #%-3s %-7s %8s tokens  材料包 %5.1f KB  發現 %d%s"
                     % (r.get("at", "?"), r.get("round", "?"), r.get("mode", "?"),
                        format(_num(r.get("tokens"), 0), ","),
                        _num(r.get("dossier_bytes"), 0) / 1024,
                        _num(r.get("findings"), 0),
                        "（攔阻）" if r.get("blocking") else ""))
    return "\n".join(lines)
