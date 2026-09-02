"""背景審查（ADR-0002）。

由執行者以背景任務啟動：跑完時 harness 會自動喚醒執行者並交付這支程式的
stdout，不需要使用者發話。所以 stdout 就是要給執行者看的報告，寫清楚一點。

用法：
    py -3 -m cross_review.review --job <job.json> --mode code
    py -3 -m cross_review.review --job <job.json> --mode visual
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from . import (breaker, common, dispatch, shots as shots_mod,
               transcript as tx, usage)

SCHEMA_DIR = Path(__file__).parent / "schemas"

CODE_PROMPT = """你是這個專案的獨立審查者。

材料包在 {dossier}，由工具直接從 Claude Code 的工具呼叫紀錄與 git 產生。
執行者沒有經手，也沒有機會挑選或修飾內容。

讀完材料包，回答兩件事：
  1. 做出來的東西，跟使用者原本要的一樣嗎？（漏做、多做、方向偏了）
  2. 這些程式碼會壞嗎？（邊界、例外、資源洩漏、安全、效能）
另外把你看到的架構與風格問題也記下來，但那些不構成攔阻。

blocking 只在「再走下去會白做」時才設 true。風格、命名、架構偏好、
測試覆蓋不足，一律 false。

材料包開頭若標示了截斷，你的 verdict 必須提到你只看了其中一部分。

不要修改任何檔案。依 output schema 回覆。"""

VISUAL_PROMPT = """你是這個專案的視覺審查者。

材料包在 {dossier}，附圖是這一回合實際跑起來的畫面截圖。
若某個畫面同時附了基準圖，基準圖是上一回合的同一個畫面。

逐項檢查每一張圖，每一項都要看過：
  1. 有沒有文字在容器邊緣被切掉？把畫面上看得到的文字跟材料包裡的
     DOM 文字對照——DOM 裡有、畫面上看不到，就是被截斷或被遮住。
  2. 有沒有內容溢出容器或超出視窗？
  3. 有沒有元素互相重疊？
  4. 有沒有文字與背景對比度低到難以閱讀？
  5. 中文有沒有顯示異常（亂碼、方框、重複字）？
  6. 字型是否符合規範：中文微軟正黑體、英文與數字 Calibri？
  7. 中文內文的標點是不是全形？
  8. 對齊或間距有沒有明顯不一致？
  9. 若有基準圖：除了這回合預期內的改動，還有什麼地方變了？
     這一項是最重要的，使用者最想要的就是「有沒有又弄壞別的地方」。

只根據圖片與材料包判斷，不要臆測看不到的東西。依 output schema 回覆。"""


# ---------------------------------------------------------------- 材料包
def earlier_requests(job: dict, limit: int = 3) -> list:
    """本回合使用者沒發話時，回頭抓游標之前最近幾句真正的使用者要求。

    先前這種情況只寫一句「延續前一輪的要求」，審查者於是完全看不到需求，
    只能回報「無法驗證需求符合度」——而它是對的，材料裡真的沒有需求。
    """
    start = int(job.get("start_line", 0) or 0)
    if start <= 0:
        # 游標在檔頭，前面本來就沒有東西。不能傳 stop_line=0——
        # 那在 tx.parse 裡代表「讀到檔尾」，會把背景審查期間新增的訊息
        # 當成「之前的要求」，污染本回合的需求判定。
        return []
    try:
        before = tx.parse(Path(job["transcript"]), 0, stop_line=start)
    except Exception:
        return []
    return (before.get("user_requests") or [])[-limit:]


def add_user_voice(add, parsed: dict, job: dict = None) -> None:
    """使用者的要求與使用者的決定，兩者都要進材料包。

    只放要求不放決定，會讓審查者每一輪都指控執行者沒照使用者的話做——
    因為使用者在選單裡做的取捨它看不到。第一次真實審查就是這樣誤判的。
    """
    add("## 使用者的原始要求（逐字取自 transcript，未經執行者轉述）")
    add()
    requests = parsed.get("user_requests") or []
    if requests:
        for text in requests:
            for line in text.splitlines():
                add("> " + line)
            add(">")
        add()
    else:
        add("這段區間內使用者沒有新的發話（通常是審查報告回來後的後續處理）。"
            "以下是**在這之前**最近幾則使用者要求，只是脈絡。")
        add()
        add("**請不要拿它們當成本回合的驗收標準**：這一輪很可能只是那些要求裡的"
            "一小步，也可能只是在處理上一份審查報告的發現。"
            "要求裡提到的功能若在本回合的 diff 裡看不到，最可能的原因是"
            "它早就做完了、或者根本不屬於這一輪。")
        add()
        for text in earlier_requests(job or {}):
            for line in text.splitlines():
                add("> " + line)
            add(">")
        add()

    referenced = parsed.get("referenced_context") or []
    if referenced:
        add("## 執行者上一則回覆（脈絡，不一定相關）")
        add()
        add("使用者這一輪的發言很短，或明顯在指涉先前的內容。以下是**執行者"
            "上一則回覆**，也就是使用者當時看得到的東西。")
        add()
        add("**這只是脈絡，不是需求。** 使用者那句話可能是在回應這一段"
            "（「動手」「都做」），也可能是一個自帶內容的獨立要求"
            "（「刪除登入按鈕」），甚至可能是在否決這一段。"
            "需求一律以上面使用者的原文為準；這一段只在他的話單看不成句時才有用。")
        add()
        for text in referenced[-2:]:      # 只取最近兩則，再多就是材料包在膨脹
            for line in text.splitlines():
                add("> " + line)
            add(">")
        add()

    decisions = parsed.get("user_decisions") or []
    if decisions:
        add("## 使用者在這段區間做過的決定（逐字）")
        add()
        add("執行者向使用者提過選項，以下是使用者選的。"
            "**這些決定與上面的要求同等有效**：使用者可能在這裡縮小或改變了原本的要求，"
            "請不要拿已經被否決的選項來指控執行者沒做。")
        add()
        for text in decisions:
            for line in text.splitlines():
                add("> " + line)
            add(">")
        add()


def add_durable_context(add, project: Path, limit: int = 20000) -> None:
    """把不會隨 transcript 滑走的專案脈絡放進材料包。

    材料包只涵蓋「游標之後」那一段對話。回合一短，審查者看到的可能只剩
    一句抱怨——它就會拿那句話去對照整個專案，然後回報「無法確認方向正確」。
    這不是它的錯，是材料的邊界問題。

    CONTEXT.md 與 docs/adr/ 是專案裡不會滑走的部分：詞彙與已定案的決定。
    有就帶上，讓審查者知道哪些事情是刻意的，不要每一輪重新爭論。
    """
    # 讀之前一律確認檔案真的在專案裡面。剛為 .claude/review 修好路徑越界，
    # 轉頭就在這段新程式碼裡犯一次：惡意的 clone 可以把 CONTEXT.md 或某個
    # ADR 做成指向專案外的連結，內容就被送進 Codex 了。
    def safe_read(path: Path, budget_left: int) -> str:
        """額度以 UTF-8 位元組計算，不是字元數。

        中文一個字三個位元組。用字元數切，持久脈絡可以撐到預定上限的三倍，
        把真正的改動內容擠出材料包。同一個錯誤在檔案內容那段修過一次，
        寫這個函式時又犯了一次。
        """
        if budget_left <= 0 or not path.exists():
            return ""
        if not common.is_inside(str(path), project):
            return ""
        try:
            raw = common.read_text(path).encode("utf-8")
        except Exception:
            return ""
        if len(raw) <= budget_left:
            return raw.decode("utf-8", "ignore")
        # 被切一半的設計決策比沒有更糟：審查者會把半份文件當成完整的規則。
        # 一定要在原地講明白。
        return (raw[:budget_left].decode("utf-8", "ignore")
                + "\n\n**[這份文件在此被截斷，以上不是完整內容]**\n")

    budget = limit
    body = safe_read(project / "CONTEXT.md", budget)
    if body:
        add("## 專案詞彙（CONTEXT.md）")
        add()
        add("```")
        add(body)
        add("```")
        add()
        budget -= len(body.encode("utf-8"))

    adr_dir = project / "docs" / "adr"
    if not adr_dir.is_dir() or budget <= 0:
        return
    files = sorted(adr_dir.glob("*.md"))
    if not files:
        return

    # 額度不夠時要砍最舊的，不是最新的。
    # 直接照檔名順序讀下去的話，編號最大的 ADR 最先被擠掉——
    # 而最新的決定通常正是這一輪最相關的那一份。
    # 所以先由新到舊挑出裝得下的，再依編號順序輸出。
    chosen, dropped_adr = [], []
    left = budget
    for path in sorted(files, reverse=True):
        body = safe_read(path, left)
        if not body:
            dropped_adr.append(path.name)
            continue
        cost = len(body.encode("utf-8"))
        if cost > left:
            dropped_adr.append(path.name)
            continue
        left -= cost
        chosen.append((path, body))

    add("## 已定案的設計決策（docs/adr/）")
    add()
    add("這些是刻意的取捨，不是疏漏。**不要把已經記錄在案的決定當成缺陷回報**；"
        "若你認為某個決定本身錯了，請明講是在挑戰那個決定。")
    if dropped_adr:
        add()
        add("（額度不足，以下 ADR 未收錄：" + "、".join(sorted(dropped_adr)) + "）")
    add()
    for path, body in sorted(chosen, key=lambda pair: pair[0].name):
        add("### " + path.name)
        add()
        add(body)
        add()


def build_code_dossier(project: Path, job: dict, cfg: dict) -> tuple:
    # 一切以工作單為準，不重新計算。背景審查跑那 150～650 秒的期間，
    # 使用者可能已經講了下一句話、執行者也可能已經改了別的東西；
    # 若在這裡重讀到檔尾、重算 mtime，材料包就會混進下一回合的內容，
    # 而報告仍宣稱自己審的是第 N 回合。工作單存在的意義就是把範圍釘住。
    parsed = tx.parse(Path(job["transcript"]),
                      job.get("start_line", 0),
                      stop_line=job.get("end_line", 0))
    # 工作單本身的路徑驗證過了，但它列的檔案路徑沒有。這支程式不該
    # 假設工作單的內容可信：裡面的路徑若指向專案外，內容就會被送進 Codex。
    all_pinned = list(job.get("files") or [])
    # 除了「在專案內」，還必須是程式碼檔。只擋路徑不擋種類的話，
    # 被竄改的工作單可以把 .env、金鑰或任何專案內的私密檔列進來，
    # 內容就被讀進材料包送給審查者了。hook 只會寫程式碼檔進去，
    # 但這支程式不該假設工作單可信。
    pinned = [f for f in all_pinned
              if common.is_inside(str(f), project) and common.is_code_file(str(f))]
    # 被越界過濾掉的路徑不能靜默消失。檔案在派工後被換成指向專案外的連結時，
    # 它會同時從 files 與 deleted 消失，run_code 看到空清單就回 0、寫 .done，
    # 於是一個沒有產生報告的回合被當成完成——安全修正造出了新的假完成路徑。
    rejected = [f for f in all_pinned if f not in pinned]
    files = [f for f in pinned if Path(f).exists()]

    # 被過濾掉的刪除項也要記進 rejected。只記 files 的話，
    # 工作單若只含這種刪除項，files 與 deleted 都會是空的、rejected 也是空的，
    # run_code 於是回報「沒有改動」並寫 .done——又一條假完成路徑。
    all_deleted = list(job.get("deleted") or [])
    kept_deleted = [f for f in all_deleted
                    if common.is_inside(str(f), project)
                    and common.is_code_file(str(f))]
    rejected += [f for f in all_deleted if f not in kept_deleted]
    # 釘住之後才消失的檔案要講出來，不能靜默從材料裡蒸發。
    vanished = [f for f in pinned if not Path(f).exists()]
    # deleted 也要過濾。上一輪只為 files 加了越界檢查就收工，
    # 而 deleted 一樣會被送進 git_diff——同一件事又只做了一半。
    deleted = kept_deleted + vanished
    source = job.get("source") or "工作單（hook 在回合結束當下釘住的清單）"

    max_files = common.positive_int(cfg, "max_files")
    max_bytes = common.positive_int(cfg, "max_bytes", 1000)

    # 上限要涵蓋刪除。刪除也是改動，只算還存在的檔案的話，
    # 一次刪掉幾百個檔的回合會把它們全部塞進 git diff，
    # 而且開頭不一定標示節錄——上限等於形同虛設。
    deleted_room = max(0, max_files - len(files))
    if len(deleted) > deleted_room:
        truncated_deleted = deleted[deleted_room:]
        deleted = deleted[:deleted_room]
    else:
        truncated_deleted = []

    # 改動總數在任何截斷之前就先算好。之後 truncated_files 會裝進
    # files[max_files:]，再把 len(files) 跟 len(truncated_files) 相加
    # 就變成重複計算——節錄提示的數字會比實際改動數還大。
    total_changed = len(files) + len(deleted) + len(truncated_deleted)

    included, truncated_files = files[:max_files], files[max_files:]
    truncated_files = truncated_files + truncated_deleted
    lines = []

    def add(text=""):
        lines.append(text)

    add("# 材料包 #" + str(job.get("round", 0)) + "（程式碼審查）")
    add()
    if truncated_files:
        add("> **本次為節錄審查**：這一回合改動了 " + str(total_changed)
            + " 個檔案（含刪除），材料包只收錄了 "
            + str(len(included) + len(deleted))
            + " 個。未收錄的檔案列在最後。你的 verdict 必須提到這件事。")
        add()
    add("改動檔案清單的來源：" + source)
    add()
    if rejected:
        add("> **以下路徑被拒絕**：它們在工作單裡，但已經不在專案目錄內"
            "（可能被換成指向外部的連結）。工具拒絕讀取，這一輪沒有審查它們：")
        add(">")
        for path in rejected:
            add("> - " + str(path))
        add()

    # 指紋比對：這些檔案在派工之後又被動過，所以下面看到的內容不是
    # 這一回合結束當下的樣子。這是非同步審查無法完全避免的事，
    # 但絕不能不講——否則審查者會拿下一回合的內容去評這一回合。
    prints = job.get("fingerprints") or {}
    drifted = []
    for path in files:
        want = prints.get(path)
        if not want:
            continue
        try:
            st = Path(path).stat()
        except OSError:
            continue
        if [round(st.st_mtime, 3), st.st_size] != list(want):
            drifted.append(path)
    if drifted:
        add("> **注意：以下檔案在這一回合結束之後又被修改過**，"
            "材料包裡是它們**目前**的內容，不是本回合結束當下的內容。"
            "評論這些檔案時請把這件事考慮進去：")
        add(">")
        for path in drifted:
            try:
                add("> - " + str(Path(path).relative_to(project)))
            except Exception:
                add("> - " + str(path))
        add()
    if deleted:
        add("**這一回合刪除了以下程式碼檔**（磁碟上已經沒有，只列路徑）：")
        add()
        for path in deleted:
            try:
                add("- " + str(Path(path).relative_to(project)))
            except Exception:
                add("- " + str(path))
        add()

    add_user_voice(add, parsed, job)

    add("## 這一回合執行者實際做了什麼（工具呼叫紀錄）")
    add()
    summary = parsed.get("tool_summary") or {}
    if summary:
        for name in sorted(summary):
            add("- " + name + " x" + str(summary[name]))
    else:
        add("- （無）")
    add()
    bash = parsed.get("bash_commands") or []
    if bash:
        add("執行過的指令：")
        add()
        add("```")
        for cmd in bash[:40]:
            add(cmd if len(cmd) <= 500 else cmd[:500] + " …")
        if len(bash) > 40:
            add("[... 另有 " + str(len(bash) - 40) + " 條指令未列出 ...]")
        add("```")
        add()

    specs = [n for n in ("CLAUDE.md", "AGENTS.md")
             if (project / n).exists()
             and common.is_inside(str(project / n), project)]
    if specs:
        name = specs[0]
        # 截取用 UTF-8 位元組，不是字元數——8000 個中文字是 24,000 位元組，
        # 材料包會直接超出設定上限。同一個錯誤在別處修過三次了。
        raw = common.read_text(project / name).encode("utf-8")
        cut = len(raw) > 8000
        body = raw[:8000].decode("utf-8", "ignore")
        add("## 專案既有規範（" + name + "）")
        if len(specs) > 1:
            add()
            add("（" + "、".join(specs[1:]) + " 也存在，內容通常與這份相同，未重複收錄。）")
        add()
        add("```")
        add(body)
        if cut:
            add("[... 規範檔在此截斷 ...]")
        add("```")
        add()

    # 持久脈絡最多只能吃掉三分之一的額度。它是背景資訊，
    # 不該把「這回合到底改了什麼」擠出材料包。
    add_durable_context(add, project, limit=min(20000, max_bytes // 3))

    add("## 改動內容")
    add()
    # 額度要扣掉前面已經寫進去的東西——使用者的話、工具紀錄、規範檔
    # 加起來可以到一萬多位元組。原本只把 max_bytes 套在 diff 與檔案內容上，
    # 材料包實際大小因此會超過設定的上限。
    used = len("\n".join(lines).encode("utf-8"))
    budget = max(0, max_bytes - used)
    if budget <= 0:
        # 前置內容把額度吃光了。這件事必須在材料包裡講明白——否則審查者
        # 會看到一份沒有任何程式碼的「材料包」，卻不知道那是額度問題。
        add("> **這一回合的程式碼內容完全沒有進入材料包。**前置內容"
            "（使用者要求、工具紀錄、專案規範與設計決策）已經用掉 "
            + str(used) + " 位元組，超過 max_bytes（" + str(max_bytes)
            + "）。你看不到任何改動內容，verdict 必須說明這件事。")
        add()
    # diff 只涵蓋要收錄的那幾個檔案。給整個工作目錄的話，
    # 建置產物的 minified bundle 會把額度吃光，原始碼反而進不來。
    # 刪掉的檔案也要進 diff。只給還存在的檔案的話，git 專案刪除程式碼時
    # 材料包只有檔名沒有內容，審查者無從判斷刪得對不對。
    diff = tx.git_diff(project, budget // 2, included + deleted,
                       base=job.get("base_sha") or "HEAD")
    if diff.strip():
        add("```diff")
        add(diff)
        add("```")
        budget -= len(diff.encode("utf-8"))
        add()
        add("（以上為 git diff，前後各 20 行脈絡。）")
        add()

    # 被 diff 涵蓋的大檔案不再送全文——diff 已經帶了前後各 20 行，
    # 足以看懂一個函式。實測一個 57.8 KB 的測試檔佔掉整份材料包的 58%，
    # 而同樣的改動用 diff 表示只要 9.4 KB。
    # 新增的檔案不在 diff 裡（untracked），非 git 專案更是完全沒有 diff，
    # 那兩種情況一律照送全文，否則審查者會什麼都看不到。
    covered = tx.diff_covers(diff, project)
    full_limit = common.positive_int(cfg, "full_content_max_bytes", 500)
    diff_only = []

    partial_files = []
    full_files = []
    for path in included:
        if budget <= 0:
            truncated_files.append(path)
            continue
        rel = str(Path(path).relative_to(project))
        try:
            file_bytes = Path(path).stat().st_size
        except OSError:
            file_bytes = 0
        if str(Path(path).resolve()) in covered and file_bytes > full_limit:
            diff_only.append(path)
            continue
        try:
            body = common.read_text(path)
        except Exception as exc:
            add("### " + rel + "（讀不到：" + str(exc) + "）")
            add()
            continue
        # 用 UTF-8 位元組計算，不是 Python 字元數。中文一個字三個位元組，
        # 用字元數算會低估三倍，材料包實際大小遠超過設定的上限。
        encoded = body.encode("utf-8")
        if len(encoded) > budget:
            body = encoded[:budget].decode("utf-8", "ignore") + "\n[... 此檔在此截斷 ...]\n"
            # 只截斷單一大檔時，原本不會記進任何清單，meta.truncated 仍是 false，
            # 報告於是完全不提「這是節錄審查」——正是這個機制要防的靜默失效。
            partial_files.append(path)
            budget = 0
        else:
            budget -= len(encoded)
            full_files.append(path)
        # 標題、程式碼圍欄、空行也佔位元組。不扣的話材料包會穩定超出上限，
        # 檔案愈多超得愈多。
        markup = "### " + rel + "\n\n```\n\n```\n\n"
        budget -= len(markup.encode("utf-8"))
        add("### " + rel)
        add()
        add("```")
        add(body)
        add("```")
        add()

    if diff_only:
        add("## 只給 diff、沒有給全文的檔案")
        add()
        add("這些檔案比較大，上面的 diff 已經帶了改動前後各 20 行。"
            "**你看到的是改動本身，不是整個檔案。**若某個判斷需要看到檔案的"
            "其他部分才能下，請在 verdict 裡講明白，不要用猜的。")
        add()
        for path in diff_only:
            try:
                size = Path(path).stat().st_size
            except OSError:
                size = 0
            add("- " + str(Path(path).relative_to(project))
                + "（%.1f KB，未收錄全文）" % (size / 1024))
        add()

    if truncated_files:
        add("## 未收錄的檔案")
        add()
        for path in truncated_files:
            try:
                add("- " + str(Path(path).relative_to(project)))
            except Exception:
                add("- " + str(path))
        add()

    if partial_files:
        add("## 只收錄了一部分內容的檔案")
        add()
        for path in partial_files:
            add("- " + str(Path(path).relative_to(project)) + "（超過材料包上限，後半段沒有進來）")
        add()

    # max_bytes 是硬上限，不是建議值。逐點扣除追不乾淨——截斷提示、
    # Markdown 標記、事後補的警告都會在額度之外追加，而每修一處就冒出下一處。
    # 最後統一夾住，並誠實說明夾過。
    text = "\n".join(lines)
    encoded = text.encode("utf-8")
    if len(encoded) > max_bytes:
        # 先算出提示本身佔多少，再決定留多少內容。固定扣 200 的話，
        # max_bytes 設得比提示還小時，最終產物仍然會超過上限。
        notice = ("\n\n**[材料包在此被硬性截斷：內容超過 max_bytes（"
                  + str(max_bytes) + " 位元組）。以下還有東西沒有進來，"
                  + "你的 verdict 必須提到自己只看了一部分。]**\n")
        keep = max(0, max_bytes - len(notice.encode("utf-8")))
        text = encoded[:keep].decode("utf-8", "ignore") + notice
        if not truncated_files:
            truncated_files = ["（材料包整體超過上限，尾端內容未收錄）"]

    return text, {
        "files": files,
        "deleted": deleted,
        "rejected": rejected,
        "diff_only": diff_only,
        # 報告要用這個數字。算出來卻沒帶出去的話，render_code_report 會退回
        # len(meta["files"])，遇到刪除或節錄就低估實際改動數。
        "total_changed": total_changed,
        "included": included,
        "full": full_files,
        "truncated": truncated_files,
        "partial": partial_files,
        "source": source,
    }


def build_visual_dossier(project: Path, job: dict, collected: dict, max_images: int) -> tuple:
    # 跟程式碼材料包一樣要用工作單釘住範圍。先前只修了 build_code_dossier，
    # 漏掉這裡——同一個檔案、同一個 bug、只修了一半。
    parsed = tx.parse(Path(job["transcript"]),
                      job.get("start_line", 0),
                      stop_line=job.get("end_line", 0))
    lines = []

    def add(text=""):
        lines.append(text)

    add("# 材料包 #" + str(job.get("round", 0)) + "（視覺審查）")
    add()
    add_user_voice(add, parsed, job)

    add_durable_context(add, project, limit=8000)

    add("## 專案的字型與標點規範")
    add()
    add("- 中文：微軟正黑體（Microsoft JhengHei）")
    add("- 英文與數字：Calibri")
    add("- 中文內文標點須為全形：「，」「：」「（）」")
    add()

    images = []
    # 先決定哪些畫面塞得下，再描述它們。
    # 直接對扁平清單做 images[:max_images] 會切在「現圖／基準圖」配對中間：
    # 材料包文字仍宣稱這個畫面有基準圖，實際卻只送了現圖，
    # 審查者於是拿不到比較對象卻以為自己有。
    fitted, dropped, used = [], [], 0
    for shot in collected["shots"]:
        cost = 2 if shot["baseline"] else 1
        if used + cost <= max_images:
            fitted.append(shot)
            used += cost
        else:
            dropped.append(shot)

    add("## 這一回合拍到的畫面")
    add()
    for idx, shot in enumerate(fitted, 1):
        add("### 畫面 " + str(idx) + "：" + shot["name"])
        add()
        add("- 網址：" + shot["url"])
        add("- 視窗：" + shot["viewport"])
        if shot["baseline"]:
            add("- **有基準圖**：附圖中，這個畫面會先給這一回合的圖，再給上一回合的基準圖。")
        else:
            add("- 沒有基準圖（這是第一次拍這個畫面），本次無法做視覺回歸。")
        add()
        if shot["dom_text"]:
            add("這個畫面的 DOM 文字（畫面上應該看得到這些字）：")
            add()
            add("```")
            add(shot["dom_text"])
            add("```")
            add()
        images.append(Path(shot["png"]))
        if shot["baseline"]:
            images.append(Path(shot["baseline"]))

    if dropped:
        add("## 超過圖片上限、這一輪沒有送出的畫面")
        add()
        add("一次最多送 " + str(max_images) + " 張圖（有基準圖的畫面佔兩張）。"
            "以下畫面**這一輪完全沒有被看到**，你的 verdict 必須提到這件事：")
        add()
        for shot in dropped:
            add("- " + shot["name"])
        add()

    if collected["skipped"]:
        add("## 跳過的畫面")
        add()
        for item in collected["skipped"]:
            add("- " + item)
        add()
    if collected["errors"]:
        add("## 截圖失敗")
        add()
        for item in collected["errors"]:
            add("- " + item)
        add()

    # 回傳三個值：文字、要送出的圖、以及超過上限沒送出的畫面名稱。
    # 第三個是給報告用的——使用者讀的是報告，不是材料包。
    return "\n".join(lines), images, [sh["name"] for sh in dropped]


# ---------------------------------------------------------------- 報告
def render_code_report(data: dict, meta: dict) -> str:
    lines = ["# 程式碼審查報告 — 回合 #" + str(meta["round"]), ""]
    partial = meta.get("partial") or []
    if meta["truncated"] or partial:
        # 數字要對得起來：完整＋部分＋沒收錄 = 改動總數。
        # 舊版寫成「6 個改動檔案中只審了 6 個」，自相矛盾又看不出漏了什麼。
        note = ("> **節錄審查**：改動 "
                + str(meta.get("total_changed", len(meta["files"])))
                + " 個檔案（含刪除），"
                "完整收錄 " + str(len(meta.get("full") or [])) + " 個")
        if partial:
            note += "、只收錄前半段 " + str(len(partial)) + " 個"
        if meta["truncated"]:
            note += "、完全沒收錄 " + str(len(meta["truncated"])) + " 個"
        lines += [note + "。", ""]
    lines += [common.usage_line(data),
              "攔阻級：" + ("是" if data.get("blocking") else "否"), "",
              "**" + str(data.get("verdict", "")) + "**", ""]
    findings = data.get("findings") or []
    if not findings:
        lines.append("沒有發現。")
    for i, f in enumerate(findings, 1):
        lines.append(str(i) + ". `[" + str(f.get("severity")) + "]` **"
                     + str(f.get("category")) + "** — " + str(f.get("file")))
        lines.append("   " + str(f.get("summary")))
    return "\n".join(lines) + "\n"


def render_visual_report(data: dict, meta: dict) -> str:
    lines = ["# 視覺審查報告 — 回合 #" + str(meta["round"]), "",
             common.usage_line(data),
             "拍到 " + str(meta["shot_count"]) + " 個畫面，其中 "
             + str(meta["baseline_count"]) + " 個有基準圖可比對。", ""]

    # 部分畫面失敗卻仍有其他畫面成功時，原本錯誤只進 errors.log，
    # 報告與 stdout 都看不到——一份不完整的視覺審查看起來會像正常完成。
    if meta.get("errors"):
        lines += ["> **有畫面沒拍成，這份審查並不完整**："]
        lines += ["> - " + item for item in meta["errors"]]
        lines += [""]
    if meta.get("dropped"):
        lines += ["> **以下畫面因為超過 max_images 沒有送去審查**："]
        lines += ["> - " + item for item in meta["dropped"]]
        lines += [""]
    if meta.get("skipped"):
        lines += ["> 跳過的畫面："]
        lines += ["> - " + item for item in meta["skipped"]]
        lines += [""]

    lines += ["**" + str(data.get("verdict", "")) + "**", ""]
    findings = data.get("findings") or []
    if not findings:
        lines.append("沒有發現。")
    for i, f in enumerate(findings, 1):
        lines.append(str(i) + ". `[" + str(f.get("severity")) + "]` **"
                     + str(f.get("category")) + "** — " + str(f.get("shot")))
        lines.append("   位置：" + str(f.get("where")))
        lines.append("   " + str(f.get("summary")))
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- 主流程
def run_code(project: Path, job: dict, cfg: dict) -> int:
    rdir = common.review_dir(project)
    round_no = job.get("round", 0)
    dossier_text, meta = build_code_dossier(project, job, cfg)
    meta["round"] = round_no

    # 只刪除程式碼檔也是一種改動，一樣要送審。原本只看 files 為不為空，
    # 於是「刪除-only」的回合直接 return，卻照樣寫了 .done ——
    # 那一輪看起來審過了，其實沒有任何人看過。假完成比漏審更糟。
    if not meta["files"] and not meta.get("deleted"):
        if meta.get("rejected"):
            # 全部被越界過濾掉了。這不是「沒有改動」，是「有改動但拒絕讀取」，
            # 不能當成乾淨完成——那又會變成一個沒有報告卻寫了 .done 的回合。
            msg = ("工作單裡的檔案全部不在專案目錄內，拒絕讀取，本回合未審查："
                   + "、".join(str(p) for p in meta["rejected"][:5]))
            common.log_error(project, msg)
            print("⚠️ " + msg)
            return 1
        print("本回合沒有可審查的程式碼檔案改動，程式碼審查跳過。")
        return 0

    dossier_path = rdir / ("dossier-" + str(round_no) + "-code.md")
    common.write_text(dossier_path, dossier_text)

    data, err = common.run_codex(
        project=project,
        prompt=CODE_PROMPT.format(dossier=dossier_path),
        schema_file=SCHEMA_DIR / "code.json",
        out_file=rdir / ("raw-" + str(round_no) + "-code.json"),
        cfg=cfg,
        require={"blocking": bool, "verdict": str, "findings": list},
    )
    if err:
        common.log_error(project, "程式碼審查失敗：" + err)
        note = breaker.record_failure(project, err, "code")
        # 失敗那一趟也燒了額度（額度錯誤訊息裡就寫著 tokens used），
        # 不記的話帳本會在最需要分析額度的時候系統性少算。
        usage.record(project, "code", round_no, data or {},
                     len(dossier_text.encode("utf-8")))
        print("⚠️ 本回合的程式碼審查沒有跑成。原因：" + err)
        if note:
            print("⛔ " + note)
        print("（已記到 .claude/review/errors.log。這不是『審過了沒問題』。）")
        return 1

    breaker.record_success(project, "code")
    usage.record(project, "code", round_no, data,
                 len(dossier_text.encode("utf-8")))

    report = render_code_report(data, meta)
    common.write_text(rdir / ("report-" + str(round_no) + "-code.md"), report)
    common.write_text(rdir / "latest-code.md", report)
    print(report)
    return 0


def run_visual(project: Path, job: dict, cfg: dict) -> int:
    rdir = common.review_dir(project)
    round_no = job.get("round", 0)

    collected = shots_mod.collect(project, cfg)
    for item in collected["errors"]:
        common.log_error(project, "截圖失敗：" + item)

    if not collected["shots"]:
        # ADR-0007 說 `.done` 代表「產出了報告」。全部跳過時若只印到 stdout
        # 而不留報告檔，那個語意就自相矛盾了——所以這裡也要產出報告。
        lines = ["# 視覺審查報告 — 回合 #" + str(round_no), "",
                 "**本回合沒有進行視覺審查：一個畫面都沒拍到。**", ""]
        if collected["skipped"]:
            lines += ["跳過的畫面："] + ["- " + i for i in collected["skipped"]] + [""]
        if collected["errors"]:
            lines += ["截圖失敗："] + ["- " + i for i in collected["errors"]] + [""]
        report = "\n".join(lines) + "\n"
        common.write_text(rdir / ("report-" + str(round_no) + "-visual.md"), report)
        common.write_text(rdir / "latest-visual.md", report)
        print(report)

        # 全部「跳過」是正常的（dev server 沒開，ADR-0004），算完成；
        # 但有「錯誤」就不是——那代表本來該拍到卻壞了，不能標記成功完成。
        return 0 if not collected["errors"] else 1

    max_images = common.positive_int(cfg, "max_images")
    dossier_text, images, dropped_names = build_visual_dossier(
        project, job, collected, max_images)
    if not images:
        # max_images 太小而每個畫面都有基準圖（各佔兩張）時，會一張都放不下。
        # 這時仍然呼叫 Codex 只會得到一份「看不到任何畫面」的視覺審查報告，
        # 然後被標記成功——又是假完成。
        msg = ("max_images=" + str(max_images) + " 放不下任何一個畫面"
               "（有基準圖的畫面佔兩張），本回合沒有視覺審查。請調高 max_images。")
        common.log_error(project, msg)
        print("⚠️ " + msg)
        return 1
    dossier_path = rdir / ("dossier-" + str(round_no) + "-visual.md")
    common.write_text(dossier_path, dossier_text)

    data, err = common.run_codex(
        project=project,
        prompt=VISUAL_PROMPT.format(dossier=dossier_path),
        schema_file=SCHEMA_DIR / "visual.json",
        out_file=rdir / ("raw-" + str(round_no) + "-visual.json"),
        cfg=cfg,
        images=images,
        require={"verdict": str, "findings": list},
    )
    if err:
        common.log_error(project, "視覺審查失敗：" + err)
        note = breaker.record_failure(project, err, "visual")
        # 失敗那一趟也燒了額度（額度錯誤訊息裡就寫著 tokens used），
        # 不記的話帳本會在最需要分析額度的時候系統性少算。
        usage.record(project, "visual", round_no, data or {},
                     len(dossier_text.encode("utf-8")))
        print("⚠️ 本回合的視覺審查沒有跑成。原因：" + err)
        if note:
            print("⛔ " + note)
        print("（已記到 .claude/review/errors.log。這不是『畫面沒問題』。）")
        return 1

    breaker.record_success(project, "visual")
    usage.record(project, "visual", round_no, data,
                 len(dossier_text.encode("utf-8")))

    meta = {
        "round": round_no,
        "shot_count": len(collected["shots"]),
        "baseline_count": sum(1 for s in collected["shots"] if s["baseline"]),
        "errors": collected["errors"],
        "skipped": collected["skipped"],
        "dropped": dropped_names,
    }
    report = render_visual_report(data, meta)
    common.write_text(rdir / ("report-" + str(round_no) + "-visual.md"), report)
    common.write_text(rdir / "latest-visual.md", report)
    print(report)          # 失敗與跳過的畫面已經寫在報告裡，不再另外補印
    return 0


def main(argv=None) -> int:
    common.force_utf8_stdio()
    ap = argparse.ArgumentParser()
    ap.add_argument("--job", required=True)
    ap.add_argument("--mode", required=True, choices=["code", "visual"])
    args = ap.parse_args(argv)

    job = common.read_json(args.job)
    if not isinstance(job, dict):
        print("讀不到工作單：" + str(args.job))
        return 2

    project = Path(job["project"])
    # 背景審查也要自己檢查路徑越界。hook 擋住了不代表這裡安全：
    # 這支程式是獨立行程，會寫報告、寫 errors.log、寫 .done，
    # 而工作單裡的專案路徑不是它自己算出來的。
    if not common.review_dir_is_safe(project):
        print("⚠️ " + str(common.review_dir(project))
              + " 指向專案外部或不是目錄，拒絕寫入任何東西，本回合未審查。")
        return 2

    # 工作單路徑本身也要驗證。.started 與 .done 是寫在它旁邊的，
    # 專案目錄檢查過了不代表這個路徑安全——這支程式不該假設呼叫者可信。
    try:
        job_resolved = Path(args.job).resolve()
    except OSError:
        print("⚠️ 讀不到工作單路徑：" + str(args.job))
        return 2
    if job_resolved.parent != common.review_dir(project).resolve():
        print("⚠️ 工作單 " + str(job_resolved) + " 不在 "
              + str(common.review_dir(project)) + " 底下，拒絕執行。")
        return 2

    cfg = common.load_config(project)

    # 一啟動就先立旗標，讓下一次 Stop hook 知道執行者真的把審查跑起來了。
    # 這件事必須在任何慢動作之前完成——第二次 Stop 會在 0.5 秒內就發生。
    common.write_text(
        Path(args.job).with_suffix("." + args.mode + ".started"),
        time.strftime("%Y-%m-%d %H:%M:%S") + "\n",
    )

    started = time.time()
    try:
        if args.mode == "code":
            code = run_code(project, job, cfg)
        else:
            code = run_visual(project, job, cfg)
    except Exception as exc:  # 任何未預期的錯誤都要出聲，不能靜默
        common.log_error(project, args.mode + " 審查發生未預期錯誤：" + repr(exc))
        print("⚠️ " + args.mode + " 審查發生未預期錯誤：" + repr(exc))
        return 3

    # `.done` 的語意是「成功完成」，不是「行程結束」。
    # 原本無論成敗都寫，於是 Codex 找不到、逾時、輸出非法 JSON 都會被
    # hook 當成審過了——上一輪才做的「啟動了但沒跑完會出聲」因此只抓得到
    # 崩潰，抓不到失敗。假完成是這套工具最不該犯的錯，連兩輪都栽在這裡。
    if code == 0:
        done_marker = Path(args.job).with_suffix("." + args.mode + ".done")
        common.write_text(done_marker, str(round(time.time() - started, 1)) + "\n")
    return code


if __name__ == "__main__":
    sys.exit(main())


def run_now(project: Path, only: str = None, force: bool = False) -> int:
    """使用者手動觸發：把累積至今的改動組成一份工作單，當場審完。

    `only` 可以限定只跑 "code" 或 "visual"。

    不依賴 hook 產生的工作單——手動／門檻模式下 hook 根本不會建。
    跑完只寫收據（reviewed.json），不碰 state.json：hook 也在寫那個檔案，
    兩個行程各自寫會互相覆蓋，這是斷路器狀態當初必須拆檔的同一個理由。
    """
    project = Path(project).resolve()
    if not common.review_dir_is_safe(project):
        print("⚠️ " + str(common.review_dir(project)) + " 指向專案外部，拒絕動作。")
        return 1
    cfg = common.ensure_config(project)
    state = common.load_state(project)
    transcript_path = state.get("transcript") or ""

    watermark = float(state.get("watermark", 0.0))
    if not watermark and transcript_path:
        try:
            watermark = Path(transcript_path).stat().st_ctime
        except OSError:
            watermark = 0.0

    # ---- manual 模式：使用者沒開口就不送 ----
    #
    # hook 的提示已經寫成「執行者不要自己送審」，實測仍然攔不住：一個 session
    # 讀到了那句話，在使用者說「全修」之後照樣送出下一輪——它已經在自己決定
    # 好的「修完就送審」迴圈裡了。文字訊息對執行者只是建議。
    #
    # 唯一有牙齒的是執行者偽造不了的資料：逐字稿裡使用者說過什麼。
    if (common.effective_trigger(project, cfg) == "manual"
            and transcript_path and not force):
        if not tx.asked_for_review(Path(transcript_path)):
            said = tx.last_user_text(Path(transcript_path)).replace("\n", " ")[:60]
            print("這個專案是 manual 模式，而使用者最近那句話沒有要求審查，所以不送。")
            print("  使用者最後說的是：「" + (said or "（讀不到）") + "」")
            print("  要送審，請使用者說「審查」。")
            print("  若確實是使用者要求的而這裡誤判了，加 --force"
                  "（會記進 errors.log，讓使用者看得到）。")
            return 2
    if force and common.effective_trigger(project, cfg) == "manual":
        common.log_error(project, "manual 模式下以 --force 略過「使用者是否要求審查」"
                                  "的檢查。使用者最後說的是：「"
                         + tx.last_user_text(Path(transcript_path))[:80] + "」")

    scan_started = time.time()      # 必須在偵測之前取，理由見 create_job
    _parsed, end_line, files, deleted = dispatch.detect(
        project, transcript_path, int(state.get("cursor", 0)), watermark,
        ignore=cfg.get("ignore_paths"))
    reported = {p for p in (state.get("reported_deletions") or [])
                if not Path(p).exists()}
    deletions = [p for p in deleted if p not in reported]
    enabled = []
    if cfg.get("visual_review", True) and cfg.get("shots"):
        enabled.append("visual")        # 快的排前面（ADR-0003）
    if cfg.get("code_review", True):
        enabled.append("code")
    modes = list(enabled)
    if only:
        modes = [m for m in modes if m == only]
        if not modes:
            print("這個專案沒有啟用「" + str(only) + "」審查，或沒有設定要看的畫面。")
            return 1

    if not files and not deletions:
        # 視覺審查**不需要**程式碼改動：使用者想看的是畫面，跟這一輪有沒有
        # 改到檔案無關。原本這裡直接回傳，於是「我只想看一眼畫面」做不到——
        # 而那正是這個工具最初要解決的事（不必再自己截圖給人看）。
        # 沒有程式碼改動時，程式碼審查本來就沒東西可看，不算漏審。
        enabled = [m for m in enabled if m == "visual"]
        modes = [m for m in modes if m == "visual"]
        if not modes:
            print("沒有累積的改動，也沒有設定要看的畫面，這一次不用審查。")
            return 0
        print("沒有程式碼改動，只跑視覺審查。")

    round_no = dispatch.next_round(project, state)
    head_now = tx.git_head(project)
    job_path = dispatch.create_job(
        project, round_no, transcript_path, int(state.get("cursor", 0)),
        end_line, watermark, files, deletions,
        base_sha=state.get("head_sha") or head_now, dispatched=scan_started)
    job = common.read_json(job_path) or {}

    worst = 0
    for mode in modes:
        if breaker.paused_note(project, mode):
            print("⛔ " + breaker.paused_note(project, mode))
            worst = worst or 1
            continue
        common.write_text(job_path.with_suffix("." + mode + ".started"), "")
        code = run_visual(project, job, cfg) if mode == "visual" \
            else run_code(project, job, cfg)
        if code == 0:
            common.write_text(job_path.with_suffix("." + mode + ".done"), "")
        else:
            worst = code

    # **每一種**審查都產出報告才算審過。原本是 any()：視覺成功、程式碼失敗
    # （或被斷路器暫停）時照樣寫收據，水位線往前推，失敗的那批程式碼
    # 再也不會被重審——而使用者看到的是「審過了」。
    done = [m for m in modes if job_path.with_suffix("." + m + ".done").exists()]
    # 要看**所有啟用的模式**，不是 --mode 篩過的那幾個。只看篩過的話，
    # `--mode visual` 在有程式碼改動時會因為視覺跑完就寫收據，下一次 hook
    # 推進水位線，那批還沒被程式碼審查看過的改動就靜默消失了。
    if dispatch.all_modes_done(job_path, enabled):
        dispatch.write_receipt(project, job, head_now)
    elif done:
        missing = [m for m in enabled if m not in done]
        print("⚠️ 只有 " + "、".join(done) + " 跑完，"
              + "、".join(missing) + " 沒有結果，這批改動留在累積裡不會推進。")
    return worst


def set_trigger(project: Path, mode: str) -> int:
    """改這個專案的觸發模式（/cross-review 用）。"""
    project = Path(project).resolve()
    if mode not in common.TRIGGERS:
        print("觸發模式只能是：" + "、".join(common.TRIGGERS))
        return 1
    if not common.review_dir_is_safe(project):
        print("⚠️ " + str(common.review_dir(project)) + " 指向專案外部，拒絕動作。")
        return 1
    common.ensure_config(project)
    # 寫在使用者層級，不是專案設定：授權必須住在一個 repo 碰不到的地方。
    common.grant_trigger(project, mode)
    cfg = common.load_config(project)
    explain = {
        "auto": "有改動就攔阻，非審不可。",
        "manual": "永不自動送審，只報累積量，你說了才送。",
        "threshold": ("平常不送審；累積超過 "
                      + str(common.positive_int(cfg, "auto_when_files", 1))
                      + " 個檔案或 "
                      + str(common.positive_int(cfg, "auto_when_diff_bytes", 1) // 1024)
                      + " KB 改動時自動送一次。"),
    }[mode]
    print(str(project) + " 的觸發模式已設為 " + mode + "：" + explain)
    return 0
