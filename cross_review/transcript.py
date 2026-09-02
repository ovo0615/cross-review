"""解析 Claude Code 自己寫的 session transcript（JSONL）。

這是 ADR-0001 的實作核心：材料包的內容來自 harness 記錄的工具呼叫，
不是執行者的自述。執行者沒有機會挑選或修飾這裡讀到的東西。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

from . import common

# 會實際寫檔的工具。它們的 input 裡有明確的 file_path。
WRITE_TOOLS = {"Edit", "Write", "NotebookEdit", "MultiEdit"}

# 使用者的決定是以 AskUserQuestion 的 tool_result 回來的，不是一般發話。
# 濾掉全部 tool_result 會讓審查者看得到使用者的要求、看不到使用者的答案，
# 於是每一輪都指控執行者沒照使用者的話做。第一次真實審查就踩到了這個坑。
DECISION_TOOL = "AskUserQuestion"

# 斜線指令送進來的使用者訊息會被包成這個樣子，真正的話在 command-args 裡。
_COMMAND_ARGS = re.compile(r"<command-args>(.*?)</command-args>", re.S)
_COMMAND_TAGS = re.compile(r"</?command-(message|name|args)>", re.S)

# harness 塞進對話裡的東西，不是使用者說的話。
# 背景任務完成的通知長得像一則使用者訊息，於是材料包的「使用者原始要求」
# 變成「Submit round 8 for review」——審查者當然無從判斷需求符不符合。
_NOISE = re.compile(
    r"<task-notification>|<system-reminder>|\[SYSTEM NOTIFICATION - NOT USER INPUT\]",
    re.I,
)


def is_harness_noise(text: str) -> bool:
    return bool(_NOISE.search(text or ""))


def _text_of(message: dict) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            b.get("text", "")
            for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        ]
        return "".join(parts)
    return ""


def _clean_user_text(text: str) -> str:
    """斜線指令的包裝拆掉，只留使用者真正打的那句話。"""
    m = _COMMAND_ARGS.search(text)
    if m:
        return m.group(1).strip()
    return _COMMAND_TAGS.sub("", text).strip()


# 使用者用來指涉「執行者上一則回覆」的說法。命中才會把那則回覆收進材料包，
# 所以寧可窄一點：誤收的代價是材料包變大，漏收的代價是審查者看不懂需求。
_REFERENTIAL = re.compile(
    "你的建議|你說的|你提的|你列的|依照你|照你|按你|如你所說"
    "|可以動手|可以執行|就這樣做|照做|都採用|採用你|同意你"
    "|第[ ]*[0-9]+[ ]*項|那幾項|上面那"
    "|your (recommendation|suggestion)|as you (suggested|said)"
    "|go ahead|do it", re.I)
_REFERENCE_MAX_BYTES = 6000


def parse(transcript_path: Path, start_line: int = 0, stop_line: int = 0) -> dict:
    """讀第 start_line 行之後、到 stop_line（含）為止。stop_line 為 0 代表讀到檔尾。

    stop_line 不是最佳化，是正確性。背景審查是非同步的：它跑那 150～650 秒的
    期間，使用者可能已經講了下一句話、執行者也可能已經改了別的東西。
    若讀到檔尾，這一回合的材料包就會混進下一回合的要求與改動，
    而報告仍宣稱自己審的是第 N 回合。工作單記下的區間就是為了釘住範圍。

    回傳的 end_line 是實際讀到的最後一行，給游標用。
    """
    result = {
        "user_requests": [],   # 這段區間裡使用者說過的話（逐字）
        # 使用者說「依照你的建議」時，那個建議本文在**執行者**的上一則回覆裡，
        # 而材料包只收使用者發言。審查者於是知道使用者同意了某件事，
        # 卻不知道那是什麼——第 31 回合的審查就是這樣回報「無法完整核對需求」。
        "referenced_context": [],
        "user_decisions": [],  # 使用者在 AskUserQuestion 裡選的答案（逐字）
        "write_paths": [],     # Edit/Write 明確寫過的檔案
        "bash_commands": [],   # Bash 執行過的指令原文
        "tool_summary": {},    # 工具名稱 -> 次數
        "end_line": start_line,
    }

    if not transcript_path.exists():
        return result

    # tool_use 的 id -> 工具名稱，用來認出哪個 tool_result 屬於 AskUserQuestion。
    tool_names = {}
    last_assistant = ""

    with open(transcript_path, encoding="utf-8", errors="replace") as fh:
        for n, raw in enumerate(fh, 1):
            if stop_line and n > stop_line:
                break
            result["end_line"] = n
            if n <= start_line:
                continue
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except Exception:
                continue

            kind = obj.get("type")
            message = obj.get("message") or {}

            if kind == "user" and not obj.get("isMeta"):
                content = message.get("content")
                tool_results = [
                    b for b in content
                    if isinstance(b, dict) and b.get("type") == "tool_result"
                ] if isinstance(content, list) else []

                if tool_results:
                    for block in tool_results:
                        if tool_names.get(block.get("tool_use_id")) != DECISION_TOOL:
                            continue
                        text = block.get("content")
                        if isinstance(text, list):
                            text = "".join(
                                b.get("text", "") for b in text
                                if isinstance(b, dict)
                            )
                        if text:
                            result["user_decisions"].append(str(text).strip())
                    continue

                raw_text = _text_of(message)
                if is_harness_noise(raw_text):
                    continue          # 背景任務通知等，不是使用者說的話
                text = _clean_user_text(raw_text)
                if text:
                    result["user_requests"].append(text)
                    if last_assistant and _REFERENTIAL.search(text):
                        result["referenced_context"].append(last_assistant)
                continue

            if kind != "assistant":
                continue

            # 記住這一則助手回覆的文字。只有在下一則使用者發言出現指代
            # （「依照你的建議」之類）時才會被收進材料包，平常不佔任何空間。
            said = "\n".join(
                b.get("text", "") for b in (message.get("content") or [])
                if isinstance(b, dict) and b.get("type") == "text").strip()
            if said:
                encoded = said.encode("utf-8")
                if len(encoded) > _REFERENCE_MAX_BYTES:
                    # 取尾巴：建議與結論通常寫在最後。
                    said = ("**[前面截斷]**\n"
                            + encoded[-_REFERENCE_MAX_BYTES:].decode("utf-8", "ignore"))
                last_assistant = said

            for block in message.get("content") or []:
                if not isinstance(block, dict) or block.get("type") != "tool_use":
                    continue
                name = block.get("name") or "?"
                data = block.get("input") or {}
                result["tool_summary"][name] = result["tool_summary"].get(name, 0) + 1
                if block.get("id"):
                    tool_names[block["id"]] = name

                if name in WRITE_TOOLS:
                    path = data.get("file_path") or data.get("notebook_path")
                    if path:
                        result["write_paths"].append(str(path))
                elif name == "Bash":
                    cmd = data.get("command")
                    if cmd:
                        result["bash_commands"].append(str(cmd))

    return result


def git_changed_files(project: Path) -> list:
    """有 git 的專案用 git status 當權威來源。

    這比 transcript 更可靠：不管檔案是被哪個工具、哪種方式改的，git 都看得到。
    沒有 git 就回空清單，改用 transcript 的結果。
    """
    if not (project / ".git").exists():
        return []
    try:
        proc = subprocess.run(
            ["git", "-C", str(project), "status", "--porcelain", "--untracked-files=all"],
            capture_output=True, timeout=20,
        )
    except Exception:
        return []
    if proc.returncode != 0:
        return []

    files = []
    for line in proc.stdout.decode("utf-8", "replace").splitlines():
        if len(line) < 4:
            continue
        path = line[3:].strip().strip('"')
        # 重新命名的格式是 "old -> new"，取新的那個。
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        files.append(str(project / path))
    return files


def git_head(project: Path) -> str:
    """回合開始時的 commit。拿不到就回空字串。"""
    if not (project / ".git").exists():
        return ""
    try:
        proc = subprocess.run(["git", "-C", str(project), "rev-parse", "HEAD"],
                              capture_output=True, timeout=20)
    except Exception:
        return ""
    if proc.returncode != 0:
        return ""
    return proc.stdout.decode("utf-8", "replace").strip()


def diff_covers(diff_text: str, project: Path) -> set:
    """這份 diff 實際涵蓋了哪些檔案（絕對路徑）。

    只有被 diff 涵蓋的檔案才可以省略全文——新增的檔案不會出現在
    `git diff` 裡（untracked），非 git 專案更是完全沒有 diff。
    那兩種情況若也省略全文，審查者會什麼都看不到。
    """
    covered = set()
    for line in diff_text.splitlines():
        rel = _diff_header_path(line)
        if not rel:
            continue
        try:
            covered.add(str((project / rel).resolve()))
        except Exception:
            pass
    return covered


def _diff_header_path(line: str) -> str:
    """`diff --git a/<path> b/<path>` 這一行裡的檔案路徑；不是這種行就回空字串。"""
    if not line.startswith("diff --git "):
        return ""
    parts = line.split(" b/", 1)
    if len(parts) != 2:
        return ""
    return parts[1].strip()


def _diff_sections(text: str) -> list:
    """把 diff 切成「一個檔案一段」，回傳 [(檔案相對路徑, 該段文字), ...]。

    預算不夠時要丟掉的是「整個檔案的那一段」，不是「後面那幾個位元組」。
    """
    sections = []
    name, buf = "", []
    for line in text.splitlines(keepends=True):
        header = _diff_header_path(line)
        if header:
            if buf:
                sections.append((name, "".join(buf)))
            name, buf = header, [line]
        else:
            buf.append(line)
    if buf:
        sections.append((name, "".join(buf)))
    return sections


def git_diff(project: Path, max_bytes: int, paths=None, base: str = "HEAD",
             context: int = 20) -> str:
    """相對於 `base` 的改動，**只限於指定的那幾個檔案**。

    base 預設是 HEAD，但呼叫端應該傳「回合開始時的那個 commit」。
    用 HEAD 的話，只要執行者在收尾前 commit 過，diff 就是空的——
    材料包於是退而送整份檔案，那是最貴的一條路：實測一個 57.8 KB 的
    測試檔佔掉整份材料包的 58%，而同樣的改動用 diff 表示只要 9.4 KB。

    原本是 `git diff HEAD -- .`，把整個工作目錄都算進來。真實專案上立刻出事：
    `dist/` 底下一包 minified bundle 的差異就吃光了 200 KB 的材料包額度，
    真正改動的兩個原始碼檔完全擠不進去，審查者只能回報「我看不到那些檔案」。
    建置產物本來就不在收錄清單裡，diff 也不該把它們算進來。
    """
    if not (project / ".git").exists():
        return ""
    if not paths:
        return ""
    rel = []
    for p in paths:
        try:
            rel.append(str(Path(p).relative_to(project)))
        except Exception:
            rel.append(str(p))

    def run(ref: str):
        try:
            return subprocess.run(
                ["git", "-C", str(project), "diff", "-U" + str(int(context)),
                 ref, "--"] + rel,
                capture_output=True, timeout=30)
        except Exception:
            return None

    proc = run(base or "HEAD")
    if (proc is None or proc.returncode != 0) and (base or "HEAD") != "HEAD":
        # base 失效（例如被 rebase 掉了）就退回 HEAD，不要整個沒有 diff。
        proc = run("HEAD")
    if proc is None or proc.returncode != 0:
        return ""
    text = proc.stdout.decode("utf-8", "replace")
    if len(text.encode("utf-8")) <= max_bytes:
        return text

    # 超過預算時**不可以**從中間切。原本是直接截 max_bytes 個位元組，
    # 於是某個檔案的標頭在切點之前、hunk 在切點之後——diff_covers() 看到
    # 標頭就把它算成「已涵蓋」，build_code_dossier() 隨即省略它的全文，
    # 切點之後的改動就這樣完全不進材料包，報告卻說「已由 diff 提供」。
    # 那比不做這個最佳化更糟：以前截斷還有全文兜底，現在是靜默漏審。
    # 改成整段整段地取，放不下的檔案完全不出現在 diff 裡，於是不會被算進
    # covered，會照原本的路徑拿到全文。
    kept, dropped, used = [], [], 0
    for name, body in _diff_sections(text):
        size = len(body.encode("utf-8"))
        if used + size <= max_bytes:
            kept.append(body)
            used += size
        else:
            dropped.append(name or "（無法辨識的區段）")
    out = "".join(kept)
    if dropped:
        out += ("\n[... 下列檔案的 diff 放不進 " + str(max_bytes)
                + " 位元組的預算，完全沒有納入這份 diff（它們會改走全文）："
                + "、".join(dropped) + " ...]\n")
    return out


def walk_code_files(project: Path, since: float) -> list:
    """掃出修改時間晚於水位線的程式碼檔。

    這是唯一可靠的「發現」機制。git status 與工具呼叫紀錄都只是線索，
    兩者都有盲點：非 git 專案沒有前者，而 bypass 模式下 Claude 常常用
    Bash（sed、heredoc、重新導向）改檔，那些沒有 file_path 可記錄。
    第一次在真實 session 裡試跑就是栽在這個組合上——hook 跑了，
    卻認為這一輪什麼都沒改。

    走一遍目錄的成本實測：最大的專案 6,377 個項目 165 毫秒，一般專案 15 毫秒內。
    """
    hits = []
    for dirpath, dirnames, filenames in os.walk(project):
        # 就地修剪，不要走進去。點開頭的目錄一併跳過：
        # .git、.venv、.claude、.vite、.next、.pytest_cache 全在裡面。
        # 點開頭的目錄照 is_code_file 的同一份白名單處理。
        # 兩邊不一致的話白名單等於沒用：非 git 專案裡用 Bash 改 .github/workflows
        # 會完全漏審，因為走目錄這條路先把它剪掉了。
        dirnames[:] = [
            d for d in dirnames
            if d not in common.IGNORED_PARTS
            and (not d.startswith(".") or d in common.ALLOWED_DOT_DIRS)
        ]
        for name in filenames:
            if os.path.splitext(name)[1].lower() not in common.CODE_SUFFIXES:
                continue
            path = os.path.join(dirpath, name)
            try:
                if os.stat(path).st_mtime > since:
                    hits.append(path)
            except OSError:
                pass
    return hits


def changed_code_files(project: Path, parsed: dict, since: float = 0.0) -> tuple:
    """把 transcript 與 git 的結果合起來，過濾成「這一輪動過的程式碼檔」。

    `since` 是上一回合的水位線（epoch 秒）。這個參數不是最佳化，是正確性：
    `git status --porcelain` 回傳的是整個工作目錄裡所有未提交的檔案，
    不是這一輪改的。少了水位線，只要使用者沒有 commit，每一次 Stop 都會
    看到同一批檔案而反覆攔阻，執行者永遠收不了尾。
    （第一次真實審查抓到的 blocking 級 bug。）

    用 mtime 過濾還有一個副作用是好的：透過 Bash（sed、heredoc、重新導向）
    改的檔案沒有 file_path 可記錄，但它們的 mtime 一樣會動。

    回傳 (檔案清單, 來源說明)。來源說明會寫進材料包。
    """
    from_git = git_changed_files(project)
    from_tools = list(parsed.get("write_paths", []))
    from_walk = walk_code_files(project, since) if since else []

    existing, deleted = {}, {}
    for path in from_git + from_tools + from_walk:
        try:
            resolved = Path(path).resolve()
        except Exception:
            continue
        text = str(resolved)
        if not common.is_inside(text, project):
            continue
        if not common.is_code_file(text):
            continue
        try:
            mtime = resolved.stat().st_mtime
        except OSError:
            # 檔案不在磁碟上了。刪除也是一種改動，原本在這裡直接 continue，
            # 於是「這一輪只刪了程式碼檔」完全不會觸發審查。
            # 但刪除沒有 mtime 可比，而 git status 會一直列出它直到 commit，
            # 所以不能無條件納入——去重交給呼叫端的狀態處理。
            deleted[text] = True
            continue
        if since and mtime <= since:
            continue
        existing[text] = True

    parts = ["修改時間晚於本回合水位線的程式碼檔"] if since else []
    if from_git:
        parts.append("git status")
    parts.append("Claude Code 工具呼叫紀錄")
    source = "、".join(parts) + ("" if from_git else "（此專案非 git repo）")
    return sorted(existing), sorted(deleted), source
