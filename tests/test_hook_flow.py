"""hook 流程的端對端測試（不需要真的 Claude Code session）。

用合成的 transcript JSONL 餵給 run_hook.py，驗證整條判斷鏈。
每個情境都先把上一輪的 pending 結清，避免互相污染。

兩條真實踩過的坑各有一個回歸測試：
  - git 專案裡未提交的舊檔案會反覆攔阻（第一次真實審查抓到）
  - 非 git 專案裡用 Bash 改的檔案完全不會被發現（第一次真實試跑抓到）
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

TOOL_ROOT = Path(__file__).resolve().parent.parent
RUN_HOOK = TOOL_ROOT / "run_hook.py"


# ---------------------------------------------------------------- 工具
def jsonl(*objects) -> str:
    return "".join(json.dumps(o, ensure_ascii=False) + "\n" for o in objects)


def user_msg(text: str) -> dict:
    return {"type": "user", "message": {"role": "user", "content": text}}


def tool_use(name: str, inp: dict) -> dict:
    return {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "tool_use", "name": name, "input": inp}],
        },
    }


def append(transcript: Path, text: str) -> None:
    with open(transcript, "a", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


def touch(path: Path, content: str) -> None:
    """真的改檔案。

    水位線靠 mtime 判斷「這一輪動過什麼」，所以測試不能只在 transcript 裡
    假裝改過——現實中 Claude 寫檔一定會更新 mtime。
    """
    time.sleep(0.05)
    path.write_text(content, encoding="utf-8")


def run_hook(project: Path, transcript: Path):
    payload = json.dumps({
        "cwd": str(project),
        "transcript_path": str(transcript),
        "hook_event_name": "Stop",
        "stop_hook_active": False,
    })
    proc = subprocess.run(
        [sys.executable, str(RUN_HOOK)],
        input=payload.encode("utf-8"),
        capture_output=True,
        timeout=120,
    )
    out = proc.stdout.decode("utf-8", "replace").strip()
    parsed = None
    if out:
        try:
            parsed = json.loads(out)
        except Exception:
            parsed = {"_unparsed": out}
    return parsed, proc.stderr.decode("utf-8", "replace")


def state_of(project: Path) -> dict:
    path = project / ".claude" / "review" / "state.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def drain(project: Path, transcript: Path) -> None:
    """把還沒結清的 pending 標記成已啟動並結清，讓下一個情境從乾淨狀態開始。"""
    for _ in range(3):
        st = state_of(project)
        pending = st.get("pending")
        if not pending:
            return
        rdir = project / ".claude" / "review"
        job = Path(pending["job"])
        for mode in pending.get("modes") or ["code"]:
            (rdir / (job.stem + "." + mode + ".started")).write_text("x", encoding="utf-8")
        run_hook(project, transcript)


RESULTS = []


def check(label: str, condition: bool, detail: str = "") -> None:
    RESULTS.append(bool(condition))
    line = ("[PASS] " if condition else "[FAIL] ") + label
    if detail and not condition:
        line += "\n        " + detail
    print(line)


# ---------------------------------------------------------------- 情境
def scenario_non_git(base: Path) -> None:
    project = base / "proj"
    (project / "src").mkdir(parents=True)
    (project / "src" / "app.py").write_text("print('hi')\n", encoding="utf-8")
    (project / "notes.md").write_text("# 純文件\n", encoding="utf-8")

    # transcript 必須在專案檔案之後建立：水位線起始值取它的建立時間，
    # 代表「這個 session 開始的時刻」。
    time.sleep(0.05)
    transcript = base / "session.jsonl"
    transcript.write_text("", encoding="utf-8")

    # --- 只改文件不算程式碼 ---
    touch(project / "notes.md", "# 改過的文件\n")
    append(transcript, jsonl(user_msg("幫我改一下文件"),
                             tool_use("Write", {"file_path": str(project / "notes.md")})))
    out, err = run_hook(project, transcript)
    check("改 .md 不觸發審查", out is None, repr(out)[:200] + " stderr=" + err[:150])

    # --- 改程式碼會攔阻 ---
    touch(project / "src" / "app.py", "import asyncio\n")
    append(transcript, jsonl(user_msg("把 app.py 改成非同步"),
                             tool_use("Edit", {"file_path": str(project / "src" / "app.py")})))
    out, _ = run_hook(project, transcript)
    check("改 .py 會攔阻", bool(out) and out.get("decision") == "block", repr(out)[:250])

    reason = (out or {}).get("reason", "")
    check("攔阻訊息含 code 模式指令", "--mode code" in reason, reason[:200])
    check("攔阻訊息含 run_review.py 絕對路徑",
          str(TOOL_ROOT / "run_review.py") in reason, reason[:200])
    check("攔阻訊息要求背景執行", "背景任務" in reason, reason[:200])
    check("攔阻訊息提到改了幾個檔", "1 個程式碼檔案" in reason, reason[:200])
    check("沒有 shots 就不派視覺審查", "--mode visual" not in reason, reason[:200])

    rdir = project / ".claude" / "review"
    jobs = sorted(rdir.glob("job-*.json"))
    check("產生了工作單", len(jobs) == 1, str([p.name for p in rdir.iterdir()]))
    check("產生了 config.json", (rdir / "config.json").exists())
    if not jobs:
        return
    job = json.loads(jobs[0].read_text(encoding="utf-8"))
    check("工作單記錄了改動檔案",
          any(f.endswith("app.py") for f in job["files"]), str(job["files"]))
    check("工作單帶著水位線", job.get("since", 0) > 0, str(job.get("since")))

    # --- 送審旗標立起來後放行，且不會反覆攔阻 ---
    drain(project, transcript)
    st = state_of(project)
    check("送審之後 pending 已清空", not st.get("pending"), str(st))
    append(transcript, jsonl(user_msg("好了嗎")))
    out, _ = run_hook(project, transcript)
    check("同一批檔案不會反覆攔阻", out is None, repr(out)[:250])

    # --- 攔阻後執行者沒跑：不再攔阻，但要出聲 ---
    touch(project / "src" / "app.py", "import asyncio\nasync def main(): ...\n")
    append(transcript, jsonl(user_msg("再改一次"),
                             tool_use("Edit", {"file_path": str(project / "src" / "app.py")})))
    out, _ = run_hook(project, transcript)
    check("第二回合再次攔阻", bool(out) and out.get("decision") == "block", repr(out)[:200])
    out, _ = run_hook(project, transcript)          # 這次沒有人跑審查
    check("沒送審時不再攔阻（避免無窮迴圈）",
          not (out or {}).get("decision"), repr(out)[:250])
    check("放棄時有出聲", bool((out or {}).get("systemMessage")), repr(out)[:250])
    log = rdir / "errors.log"
    check("放棄時有記錯誤",
          log.exists() and "沒有被啟動" in log.read_text(encoding="utf-8"),
          log.read_text(encoding="utf-8")[:150] if log.exists() else "errors.log 不存在")

    # --- 用 Bash 改的檔案也要被發現（真實試跑踩到的坑） ---
    # 刻意只動檔案，transcript 裡沒有任何對應的 Edit/Write file_path。
    touch(project / "src" / "app.py", "print('changed via bash')\n")
    append(transcript, jsonl(tool_use("Bash", {"command": "sed -i 's/x/y/' src/app.py"})))
    out, _ = run_hook(project, transcript)
    check("Bash 改的檔案（非 git 專案）也會被發現",
          bool(out) and out.get("decision") == "block", "沒有攔阻 -> " + repr(out)[:250])
    drain(project, transcript)

    # --- node_modules 要被忽略 ---
    nm = project / "node_modules" / "pkg"
    nm.mkdir(parents=True)
    touch(nm / "index.js", "module.exports = 1\n")
    append(transcript, jsonl(tool_use("Write", {"file_path": str(nm / "index.js")})))
    out, _ = run_hook(project, transcript)
    check("node_modules 的改動不觸發", out is None, repr(out)[:250])

    # --- DISABLED 開關 ---
    (rdir / "DISABLED").write_text("", encoding="utf-8")
    touch(project / "src" / "app.py", "print('during disabled')\n")
    append(transcript, jsonl(tool_use("Edit", {"file_path": str(project / "src" / "app.py")})))
    out, _ = run_hook(project, transcript)
    check("DISABLED 之後完全靜默", out is None, repr(out)[:200])

    (rdir / "DISABLED").unlink()
    out, _ = run_hook(project, transcript)
    check("重新啟用後補審停用期間的改動",
          bool(out) and out.get("decision") == "block", repr(out)[:200])
    drain(project, transcript)

    # --- 兩種審查都關掉就等於停用 ---
    cfg_path = rdir / "config.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    cfg["code_review"] = False
    cfg["visual_review"] = False
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    touch(project / "src" / "app.py", "print('still off')\n")
    append(transcript, jsonl(tool_use("Edit", {"file_path": str(project / "src" / "app.py")})))
    out, _ = run_hook(project, transcript)
    check("code_review 與 visual_review 都關掉就完全靜默", out is None, repr(out)[:200])


def scenario_git(base: Path) -> None:
    project = base / "gitproj"
    (project / "src").mkdir(parents=True)
    (project / "src" / "main.py").write_text("x = 1\n", encoding="utf-8")
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
    for cmd in (["init", "-q"], ["add", "-A"], ["commit", "-qm", "init"]):
        subprocess.run(["git", "-C", str(project)] + cmd, env=env,
                       capture_output=True, timeout=60)

    time.sleep(0.05)
    transcript = base / "gitsession.jsonl"
    transcript.write_text("", encoding="utf-8")

    touch(project / "src" / "main.py", "x = 2\n")
    append(transcript, jsonl(user_msg("改一下 main.py"),
                             tool_use("Edit", {"file_path": str(project / "src" / "main.py")})))
    out, _ = run_hook(project, transcript)
    check("git 專案：改了檔案會攔阻",
          bool(out) and out.get("decision") == "block", repr(out)[:250])

    # 用 .git/info/exclude 而不是 .gitignore：後者是追蹤中的檔案，
    # 動它會在使用者的工作樹留下與需求無關的改動。
    exclude = project / ".git" / "info" / "exclude"
    check("git 專案：.git/info/exclude 已加入 .claude/review/",
          exclude.exists() and ".claude/review/" in exclude.read_text(encoding="utf-8"))
    check("git 專案：沒有動到 .gitignore",
          not (project / ".gitignore").exists())

    drain(project, transcript)

    # 檔案仍然未提交。沒有水位線的話 git status 會再次回報它，於是反覆攔阻。
    append(transcript, jsonl(user_msg("好了嗎")))
    out, _ = run_hook(project, transcript)
    check("git 專案：未提交的舊檔案不會反覆攔阻",
          not (out or {}).get("decision"), "仍然攔阻 -> " + repr(out)[:250])

    # 真的又改了才該再攔一次。
    touch(project / "src" / "main.py", "x = 3\n")
    append(transcript, jsonl(tool_use("Edit", {"file_path": str(project / "src" / "main.py")})))
    out, _ = run_hook(project, transcript)
    check("git 專案：真的再改一次才會再攔阻",
          bool(out) and out.get("decision") == "block", repr(out)[:250])

    # 只啟動一部分模式時要出聲。
    st = state_of(project)
    pending = st.get("pending") or {}
    modes = pending.get("modes") or []
    if len(modes) > 1:
        rdir = project / ".claude" / "review"
        job = Path(pending["job"])
        (rdir / (job.stem + "." + modes[0] + ".started")).write_text("x", encoding="utf-8")
        out, _ = run_hook(project, transcript)
        check("只跑了一半的審查會出聲",
              bool((out or {}).get("systemMessage")), repr(out)[:200])
    else:
        drain(project, transcript)
        check("全部模式都啟動了就安靜放行", not state_of(project).get("pending"))


def scenario_boundaries(base: Path) -> None:
    """安全邊界與專案根目錄判定。全部是真實踩到或被審查抓到的問題。"""
    sys.path.insert(0, str(TOOL_ROOT))
    from cross_review import common
    from cross_review.shots import url_is_allowed

    # --- cwd 漂移：hook 從子目錄被呼叫時要找回真正的專案根目錄 ---
    # 真實案例：session 為了跑 npm 切進 web_app\frontend，Stop hook 於是
    # 把 frontend 當成專案，在裡面另開一整套 .claude\review。
    proj = base / "rootdetect"
    sub = proj / "web_app" / "frontend"
    sub.mkdir(parents=True)
    (proj / ".claude").mkdir()
    (proj / ".claude" / "settings.json").write_text("{}", encoding="utf-8")
    # 比對 resolve 過的路徑：Windows 的 8.3 短檔名會讓同一個目錄有兩種寫法。
    check("從子目錄能找回專案根目錄（靠 .claude/settings.json）",
          common.find_project_root(sub) == proj.resolve(),
          str(common.find_project_root(sub)))

    gitproj = base / "rootdetect_git"
    gitsub = gitproj / "a" / "b"
    gitsub.mkdir(parents=True)
    (gitproj / ".git").mkdir()
    check("從子目錄能找回專案根目錄（靠 .git）",
          common.find_project_root(gitsub) == gitproj.resolve(),
          str(common.find_project_root(gitsub)))

    # --- 工具自己的產物絕不能被當成改動 ---
    check("不把 .claude/review/state.json 當成程式碼",
          not common.is_code_file(str(proj / ".claude" / "review" / "state.json")))
    check("不把 .claude/review/job-1.json 當成程式碼",
          not common.is_code_file(str(proj / ".claude" / "review" / "job-1.json")))
    check("一般的 .json 仍然算程式碼",
          common.is_code_file(str(proj / "package.json")))
    # CI 設定改壞的後果通常比一般程式碼更嚴重，不能因為目錄名以點開頭就漏審。
    check("CI 設定（.github/workflows）要審",
          common.is_code_file(str(proj / ".github" / "workflows" / "ci.yml")))
    check("devcontainer 設定要審",
          common.is_code_file(str(proj / ".devcontainer" / "devcontainer.json")))
    check(".venv 底下的仍然不審",
          not common.is_code_file(str(proj / ".venv" / "Lib" / "x.py")))

    # --- 專案設定不能放寬安全邊界 ---
    cfgdir = proj / ".claude" / "review"
    cfgdir.mkdir(parents=True, exist_ok=True)
    (cfgdir / "config.json").write_text(
        json.dumps({"disable_codex_plugins": []}), encoding="utf-8")
    cfg = common.load_config(proj)
    forced = set(common.DEFAULT_CONFIG["disable_codex_plugins"])
    check("專案設定不能拿掉內建的 plugin 停用清單",
          forced.issubset(set(cfg["disable_codex_plugins"])),
          str(cfg["disable_codex_plugins"]))

    # --- 畫面網址的安全邊界 ---
    plain = dict(common.DEFAULT_CONFIG)
    check("預設拒絕 file:// 網址",
          bool(url_is_allowed("file:///C:/Users/secret.txt", plain)))
    check("預設拒絕非本機網址",
          bool(url_is_allowed("http://10.0.0.5:8080/", plain)))
    check("允許本機 http",
          not url_is_allowed("http://localhost:5190/", plain))
    check("允許 127.0.0.1",
          not url_is_allowed("http://127.0.0.1:5190/", plain))
    check("開了 allow_remote_urls 之後才放行",
          not url_is_allowed("http://10.0.0.5:8080/", dict(plain, allow_remote_urls=True)))


def scenario_worktree(base: Path) -> None:
    """linked worktree：.git 是檔案不是目錄。

    實測時遇到的專案就是這種形式。原本用
    is_dir() 判斷，於是 ensure_git_excluded 直接 return，什麼都沒做也沒出聲。
    而且 info/exclude 讀的是 common dir，不是各 worktree 各自的 gitdir。
    """
    sys.path.insert(0, str(TOOL_ROOT))
    from cross_review import common

    main_repo = base / "wt_main"
    main_repo.mkdir()
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
    (main_repo / "a.py").write_text("x = 1\n", encoding="utf-8")
    for cmd in (["init", "-q"], ["add", "-A"], ["commit", "-qm", "init"]):
        subprocess.run(["git", "-C", str(main_repo)] + cmd, env=env,
                       capture_output=True, timeout=60)

    linked = base / "wt_linked"
    r = subprocess.run(
        ["git", "-C", str(main_repo), "worktree", "add", "-q", "-b", "side", str(linked)],
        env=env, capture_output=True, timeout=60)
    if r.returncode != 0 or not linked.exists():
        check("能建立測試用 worktree", False,
              r.stderr.decode("utf-8", "replace")[:200])
        return
    check("worktree 的 .git 是檔案不是目錄", (linked / ".git").is_file())

    common.ensure_git_excluded(linked)
    (linked / ".claude" / "review").mkdir(parents=True, exist_ok=True)
    (linked / ".claude" / "review" / "state.json").write_text("{}", encoding="utf-8")
    status = subprocess.run(
        ["git", "-C", str(linked), "status", "--porcelain"],
        capture_output=True, timeout=60).stdout.decode("utf-8", "replace")
    check("worktree 裡 .claude/review/ 有被排除",
          ".claude/review" not in status, status[:200])
    check("worktree 裡沒有動到 .gitignore",
          not (linked / ".gitignore").exists())


def scenario_pinning(base: Path) -> None:
    """工作單釘住範圍、刪除也算改動、啟動了但沒跑完要出聲。"""
    sys.path.insert(0, str(TOOL_ROOT))
    from cross_review import transcript as tx

    # --- parse 的 stop_line 要真的停住 ---
    t = base / "pin.jsonl"
    t.write_text(jsonl(user_msg("第一句"), user_msg("第二句"), user_msg("第三句")),
                 encoding="utf-8")
    whole = tx.parse(t, 0)
    part = tx.parse(t, 0, stop_line=2)
    check("parse 讀到檔尾會拿到三句", len(whole["user_requests"]) == 3,
          str(whole["user_requests"]))
    check("parse 的 stop_line 會停在第二行", len(part["user_requests"]) == 2,
          str(part["user_requests"]))

    # --- harness 的通知不是使用者說的話 ---
    # 背景任務完成的通知長得像一則使用者訊息，材料包的「使用者原始要求」
    # 因此變成「Submit round 8 for review」，審查者當然無從判斷需求符不符合。
    noisy = base / "noise.jsonl"
    noisy.write_text(jsonl(
        user_msg("幫我把掃描改成非同步"),
        user_msg("<task-notification>\n<task-id>abc123</task-id>\n</task-notification>"),
        user_msg("<system-reminder>\n某些提醒\n</system-reminder>"),
    ), encoding="utf-8")
    got = tx.parse(noisy, 0)["user_requests"]
    check("背景任務通知不算使用者發話", len(got) == 1, str(got))
    check("留下來的是真正的要求", got and "非同步" in got[0], str(got))

    # --- 刪除程式碼檔要觸發，但只觸發一次 ---
    proj = base / "deltest"
    (proj / "src").mkdir(parents=True)
    (proj / "src" / "gone.py").write_text("x = 1\n", encoding="utf-8")
    (proj / "src" / "stay.py").write_text("y = 1\n", encoding="utf-8")
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
    for cmd in (["init", "-q"], ["add", "-A"], ["commit", "-qm", "init"]):
        subprocess.run(["git", "-C", str(proj)] + cmd, env=env,
                       capture_output=True, timeout=60)

    time.sleep(0.05)
    dt = base / "delsession.jsonl"
    dt.write_text("", encoding="utf-8")

    (proj / "src" / "gone.py").unlink()          # 只刪，什麼都不改
    append(dt, jsonl(user_msg("把 gone.py 刪掉"),
                     tool_use("Bash", {"command": "rm src/gone.py"})))
    out, _ = run_hook(proj, dt)
    check("只刪除程式碼檔也會攔阻",
          bool(out) and out.get("decision") == "block", repr(out)[:250])
    if (out or {}).get("decision") == "block":
        job = json.loads(sorted((proj / ".claude" / "review").glob("job-*.json"))[-1]
                         .read_text(encoding="utf-8"))
        check("工作單把刪除記在 deleted 欄位",
              any(f.endswith("gone.py") for f in job.get("deleted", [])),
              str(job.get("deleted")))
    drain(proj, dt)

    # git status 會一直列出這個刪除直到 commit——不能每輪都再攔一次。
    append(dt, jsonl(user_msg("好了嗎")))
    out, _ = run_hook(proj, dt)
    check("同一個刪除不會反覆攔阻", not (out or {}).get("decision"), repr(out)[:250])

    # --- 刪掉、重建、再刪：第二次刪除也要被抓到 ---
    # reported_deletions 只累積不清除的話，第二次會被永遠當成報告過。
    touch(proj / "src" / "gone.py", "z = 1\n")
    append(dt, jsonl(tool_use("Write", {"file_path": str(proj / "src" / "gone.py")})))
    out, _ = run_hook(proj, dt)
    check("檔案重建會被當成改動", bool(out) and out.get("decision") == "block",
          repr(out)[:200])
    drain(proj, dt)

    (proj / "src" / "gone.py").unlink()
    append(dt, jsonl(tool_use("Bash", {"command": "rm src/gone.py"})))
    out, _ = run_hook(proj, dt)
    check("同一個路徑第二次被刪也會攔阻",
          bool(out) and out.get("decision") == "block",
          "漏審了 -> " + repr(out)[:250])
    drain(proj, dt)

    # --- 啟動了但沒跑完（沒有 .done）要出聲 ---
    rdir = proj / ".claude" / "review"
    st = json.loads((rdir / "state.json").read_text(encoding="utf-8"))
    last = st.get("last_job")
    check("結清時有記下 last_job 供之後查證", bool(last), str(st)[:200])
    if last:
        last["deadline"] = 0          # 假裝已經超過逾時上限
        st["last_job"] = last
        (rdir / "state.json").write_text(json.dumps(st, ensure_ascii=False),
                                         encoding="utf-8")
        out, _ = run_hook(proj, dt)
        check("審查啟動了卻沒有 .done 會出聲",
              bool((out or {}).get("systemMessage")), repr(out)[:250])
        check("沒跑完也有記進 errors.log",
              "沒有跑完" in (rdir / "errors.log").read_text(encoding="utf-8"))


def scenario_usage(base: Path) -> None:
    """從 codex 的真實輸出撈出模型、努力程度、token 數。

    **這些全部在 stderr，不在 stdout。**2026-09-01 實測分開捕捉兩條串流：
    stdout 只有 3 個字元（模型的回答本身），631 字元的標頭與 `tokens used`
    全在 stderr。第一版只解析 stdout，於是報告永遠只印得出秒數——
    而單元測試照樣通過，因為餵給它的是我自己打的字串。
    所以這個測試餵的是 stderr 形狀的文字，run_codex 也必須兩條都讀。
    """
    sys.path.insert(0, str(TOOL_ROOT))
    from cross_review import common

    # 逐字照抄 codex exec 在這台機器上實際印出來的樣子（2026-09-01 擷取）。
    real = "\n".join([
        "OpenAI Codex v0.151.0-alpha.7.2",
        "--------",
        "workdir: D:\\somewhere",
        "model: gpt-5.6-luna",
        "provider: openai",
        "approval: never",
        "sandbox: read-only",
        "reasoning effort: high",
        "reasoning summaries: none",
        "session id: 01a05d91",
        "--------",
        "codex",
        '{"blocking":false}',
        "tokens used",
        "36,270",
        "",
    ])

    facts = common.parse_run_facts(real)
    check("撈得到模型", facts["_model"] == "gpt-5.6-luna", str(facts))
    check("撈得到努力程度", facts["_effort"] == "high", str(facts))
    check("撈得到 token 數（含千分位逗號）", facts["_tokens"] == 36270, str(facts))

    line = common.usage_line(dict(facts, _elapsed_sec=193.8))
    for token in ("gpt-5.6-luna", "high", "193.8", "36,270"):
        check("用量那一行含「" + token + "」", token in line, line)

    empty = common.parse_run_facts("完全不像 codex 的輸出")
    check("撈不到時不會爆炸",
          empty == {"_model": "", "_effort": "", "_tokens": 0}, str(empty))

    # 只給 stdout（實際上只有回答本身）必須撈不到任何東西——
    # 這一項就是在鎖住「不能只讀 stdout」這件事。
    only_stdout = common.parse_run_facts('{"blocking":false}\n')
    check("只讀 stdout 撈不到任何用量資訊",
          only_stdout == {"_model": "", "_effort": "", "_tokens": 0}, str(only_stdout))

    # 兩條串流合起來才完整，run_codex 就是這樣做的。
    combined = common.parse_run_facts('{"blocking":false}\n' + "\n" + real)
    check("stdout ＋ stderr 合起來才撈得到",
          combined["_model"] == "gpt-5.6-luna" and combined["_tokens"] == 36270,
          str(combined))


def scenario_hardening(base: Path) -> None:
    """審查者在回合 #7 抓到的那幾條：路徑越界、原子寫入、圖片配對、持久脈絡。"""
    sys.path.insert(0, str(TOOL_ROOT))
    from cross_review import common, review

    # --- .claude/review 指到專案外要被拒絕 ---
    proj = base / "harden"
    (proj / ".claude").mkdir(parents=True)
    outside = base / "outside_target"
    outside.mkdir()
    check("正常的 .claude/review 視為安全", common.review_dir_is_safe(proj))

    linked = base / "harden_linked"
    (linked / ".claude").mkdir(parents=True)
    made = False
    link = linked / ".claude" / "review"
    try:
        link.symlink_to(outside, target_is_directory=True)
        made = True
    except (OSError, NotImplementedError):
        # Windows 上建 symlink 要權限，但目錄 junction 不用——
        # 而 junction 正是這個防護要擋的東西，不能因為權限不足就不測。
        if os.name == "nt":
            r = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(outside)],
                               capture_output=True, timeout=30)
            made = r.returncode == 0 and link.exists()
    if made:
        check(".claude/review 指到專案外會被拒絕",
              not common.review_dir_is_safe(linked),
              str((linked / ".claude" / "review").resolve()))
    else:
        print("[SKIP] .claude/review 指到專案外會被拒絕（沒有建立 symlink 的權限）")

    # --- 懸空的 symlink：指向專案外一個「還不存在」的路徑 ---
    # 第一版靠走訪祖先判斷，這種情況會被騙過去：resolve() 後 exists() 是 False，
    # 於是只檢查安全的父目錄就放行，接著 mkdir 沿著連結在專案外把目錄建出來。
    dangling = base / "harden_dangling"
    (dangling / ".claude").mkdir(parents=True)
    ghost = base / "ghost_target_that_does_not_exist"
    dlink = dangling / ".claude" / "review"
    made_d = False
    try:
        dlink.symlink_to(ghost, target_is_directory=True)
        made_d = True
    except (OSError, NotImplementedError):
        if os.name == "nt":
            ghost.mkdir()          # junction 需要目標存在，建完再刪掉製造懸空
            r = subprocess.run(["cmd", "/c", "mklink", "/J", str(dlink), str(ghost)],
                               capture_output=True, timeout=30)
            made_d = r.returncode == 0
            if made_d:
                try:
                    ghost.rmdir()
                except OSError:
                    pass
    if made_d:
        check("指向專案外的懸空連結會被拒絕",
              not common.review_dir_is_safe(dangling))
    else:
        print("[SKIP] 指向專案外的懸空連結會被拒絕（無法建立連結）")

    # --- .claude/review 是普通檔案而不是目錄，也不安全 ---
    # 安全檢查若放行，接著 mkdir 會拋例外，每一輪都崩在建工作單之前。
    asfile = base / "harden_file"
    (asfile / ".claude").mkdir(parents=True)
    (asfile / ".claude" / "review").write_text("我不是目錄", encoding="utf-8")
    check(".claude/review 是普通檔案時視為不安全",
          not common.review_dir_is_safe(asfile))

    # --- errors.log 在不安全的專案裡不能被寫出來 ---
    common.log_error(asfile, "這一行不該被寫出去")
    check("不安全的專案不會寫 errors.log",
          not (asfile / ".claude" / "review").is_dir())

    # --- 設定值壞掉要回退，不能讓背景審查例外結束 ---
    for bad in ({"max_files": "四十"}, {"max_files": -3}, {"max_files": None},
                {"max_files": 0}):
        got = common.positive_int(bad, "max_files")
        check("壞掉的 max_files（" + repr(bad["max_files"]) + "）會回退到預設",
              got == common.DEFAULT_CONFIG["max_files"], str(got))
    check("正常的 max_files 照用", common.positive_int({"max_files": 7}, "max_files") == 7)

    # 低於下限要夾到下限，不能退回預設——預設遠大於下限，
    # 退回去等於「你要求更小，我給你大得多的東西」。
    got = common.positive_int({"max_bytes": 500}, "max_bytes", 1000)
    check("max_bytes 低於下限時夾到下限而不是退回預設",
          got == 1000, str(got) + "（預設是 "
          + str(common.DEFAULT_CONFIG["max_bytes"]) + "）")
    check("任何情況下都不會給出比要求更大的值",
          common.positive_int({"max_bytes": 500}, "max_bytes", 1000)
          < common.DEFAULT_CONFIG["max_bytes"])

    # --- 原子寫入：暫存檔不能留下，內容必須完整 ---
    target = proj / "atomic.json"
    common.write_json(target, {"a": 1})
    common.write_json(target, {"a": 2})
    check("原子寫入的內容正確", common.read_json(target) == {"a": 2})
    leftovers = list(proj.glob("atomic.json.tmp-*"))
    check("原子寫入沒有留下暫存檔", not leftovers, str(leftovers))

    # --- 圖片上限要以配對為單位切，不能切在配對中間 ---
    def shot(name, baseline):
        return {"name": name, "url": "http://localhost:1/", "viewport": "800x600",
                "png": str(proj / (name + ".png")),
                "baseline": str(proj / (name + "-b.png")) if baseline else "",
                "dom_text": ""}

    collected = {"shots": [shot("A", True), shot("B", True), shot("C", True)],
                 "skipped": [], "errors": []}
    job = {"round": 1, "transcript": str(base / "nonexistent.jsonl"),
           "start_line": 0, "end_line": 0}
    # 解包的數量必須跟正式呼叫端一致。先前測試解 2 個、run_visual 解 3 個，
    # 兩邊各自「通過」，而任何成功拍到畫面的視覺審查都會當場 ValueError。
    text, images, dropped_names = review.build_visual_dossier(
        proj, job, collected, max_images=3)
    check("圖片上限以配對為單位（3 張上限只放得下 1 個配對）",
          len(images) == 2, str(len(images)))
    check("沒送出的畫面要在材料包裡點名",
          "超過圖片上限" in text and "- B" in text and "- C" in text,
          text[-400:])
    check("沒送出的畫面不會又被描述成有基準圖",
          text.count("**有基準圖**") == 1, str(text.count("**有基準圖**")))
    check("被丟掉的畫面名稱有回傳給報告用",
          dropped_names == ["B", "C"], str(dropped_names))

    # 契約一致性：build_visual_dossier 回傳幾個值，run_visual 就必須解幾個。
    # 先前是拿 inspect 搜固定字串，改個變數名就失效——那只驗證了文字形式。
    # 改成用 AST 比對真正的回傳元素數與解包目標數。
    import ast as _ast
    tree = _ast.parse(Path(review.__file__).read_text(encoding="utf-8"))
    returns, unpacks = set(), set()
    for node in _ast.walk(tree):
        if isinstance(node, _ast.FunctionDef) and node.name == "build_visual_dossier":
            for sub in _ast.walk(node):
                if isinstance(sub, _ast.Return) and isinstance(sub.value, _ast.Tuple):
                    returns.add(len(sub.value.elts))
        if isinstance(node, _ast.Assign) and isinstance(node.value, _ast.Call):
            fn = node.value.func
            fname = getattr(fn, "id", None) or getattr(fn, "attr", None)
            if fname == "build_visual_dossier":
                for target in node.targets:
                    if isinstance(target, _ast.Tuple):
                        unpacks.add(len(target.elts))
    check("build_visual_dossier 的回傳數量一致", len(returns) == 1, str(returns))
    check("有人真的呼叫 build_visual_dossier", bool(unpacks), str(unpacks))
    check("回傳數量與解包數量相符", returns == unpacks,
          "回傳 " + str(returns) + " 個，解包 " + str(unpacks) + " 個")

    # build_code_dossier 算出來的東西必須真的帶進 meta，
    # 否則報告會靜默退回一個低估的數字。
    codejob = {"round": 1, "transcript": str(base / "nonexistent.jsonl"),
               "start_line": 0, "end_line": 0, "files": [], "deleted": []}
    _text, meta = review.build_code_dossier(proj, codejob, common.DEFAULT_CONFIG)
    for key in ("total_changed", "files", "deleted", "rejected",
                "full", "truncated", "partial"):
        check("meta 帶出 " + key, key in meta, str(sorted(meta)))

    # 游標在檔頭時不能回頭找「之前的要求」——傳 stop_line=0 給 tx.parse
    # 代表讀到檔尾，會把之後的訊息當成之前的。
    tline = base / "earlier.jsonl"
    tline.write_text(jsonl(user_msg("第一句"), user_msg("第二句")), encoding="utf-8")
    check("start_line 為 0 時不回頭找",
          review.earlier_requests({"transcript": str(tline), "start_line": 0}) == [],
          str(review.earlier_requests({"transcript": str(tline), "start_line": 0})))
    check("start_line 大於 0 時才回頭找",
          review.earlier_requests({"transcript": str(tline), "start_line": 1}) == ["第一句"],
          str(review.earlier_requests({"transcript": str(tline), "start_line": 1})))

    # max_bytes 是硬上限。逐點扣除永遠追不乾淨，最後一定要夾住。
    bigproj = base / "bigdossier"
    (bigproj / "src").mkdir(parents=True)
    big = bigproj / "src" / "big.py"
    big.write_text("# 中文註解讓位元組數變三倍\n" * 4000, encoding="utf-8")
    bigjob = {"round": 1, "transcript": str(base / "nonexistent.jsonl"),
              "start_line": 0, "end_line": 0,
              "files": [str(big)], "deleted": []}
    cfg_small = dict(common.DEFAULT_CONFIG, max_bytes=5000)
    text2, meta2 = review.build_code_dossier(bigproj, bigjob, cfg_small)
    size = len(text2.encode("utf-8"))
    check("材料包不超過 max_bytes", size <= 5000, str(size) + " 位元組")

    # max_bytes 小到比截斷提示本身還短時，也必須守住上限。
    cfg_tiny = dict(common.DEFAULT_CONFIG, max_bytes=1000)
    text3, _meta3 = review.build_code_dossier(bigproj, bigjob, cfg_tiny)
    size3 = len(text3.encode("utf-8"))
    check("極小的 max_bytes 也守得住", size3 <= 1000, str(size3) + " 位元組")
    check("被硬性截斷時有說出來", "硬性截斷" in text2, text2[-300:])
    check("被硬性截斷時報告會標示節錄", bool(meta2["truncated"]), str(meta2["truncated"]))

    # --- 持久脈絡：CONTEXT.md 與 ADR 要進材料包 ---
    (proj / "docs" / "adr").mkdir(parents=True)
    (proj / "CONTEXT.md").write_text("# 詞彙\n\n**執行者**：做事的那一方。\n",
                                     encoding="utf-8")
    (proj / "docs" / "adr" / "0001-something.md").write_text(
        "# 這是刻意的決定\n\n因為某某理由。\n", encoding="utf-8")
    got = []
    # add_durable_context 的 add 可以不帶參數（空行），list.append 不行。
    review.add_durable_context(lambda text="": got.append(text), proj)
    blob = "\n".join(got)
    check("材料包帶上 CONTEXT.md", "**執行者**" in blob, blob[:200])
    check("材料包帶上 ADR", "這是刻意的決定" in blob, blob[:200])
    check("材料包明講已定案的決定不要當缺陷回報",
          "不要把已經記錄在案的決定當成缺陷回報" in blob, blob[:300])

    # --- 額度不夠時要砍最舊的 ADR，不是最新的 ---
    # ADR 依檔名排序，直接讀下去的話編號最大的最先被擠掉——
    # 而最新的決定通常正是這一輪最相關的那一份。
    manyproj = base / "manyadr"
    (manyproj / "docs" / "adr").mkdir(parents=True)
    for i in range(1, 6):
        (manyproj / "docs" / "adr" / ("000" + str(i) + "-decision.md")).write_text(
            "# 決定 " + str(i) + "\n\n" + ("x" * 500) + "\n", encoding="utf-8")
    got3 = []
    review.add_durable_context(lambda text="": got3.append(text), manyproj, limit=1200)
    blob3 = "\n".join(got3)
    check("額度不足時保留最新的 ADR", "### 0005-decision.md" in blob3, blob3[:300])
    check("額度不足時砍掉最舊的 ADR", "### 0001-decision.md" not in blob3, blob3[:300])
    check("被砍掉的 ADR 有被點名",
          "未收錄" in blob3 and "0001-decision.md" in blob3, blob3[:400])

    # --- 持久脈絡也要擋路徑越界 ---
    # 才剛為 .claude/review 修好，轉頭就在這段新程式碼裡犯一次。
    # 檔案 symlink 在 Windows 上要權限，但目錄 junction 不用——
    # 把 docs/adr 整個指到專案外，測的是同一條防護。
    outside_adr = base / "outside_adr"
    outside_adr.mkdir()
    (outside_adr / "0001-secret.md").write_text("這是專案外的機密\n", encoding="utf-8")
    linkproj = base / "durable_link"
    (linkproj / "docs").mkdir(parents=True)
    adr_link = linkproj / "docs" / "adr"
    made_c = False
    try:
        adr_link.symlink_to(outside_adr, target_is_directory=True)
        made_c = True
    except (OSError, NotImplementedError):
        if os.name == "nt":
            r = subprocess.run(["cmd", "/c", "mklink", "/J",
                                str(adr_link), str(outside_adr)],
                               capture_output=True, timeout=30)
            made_c = r.returncode == 0 and adr_link.exists()
    if made_c:
        got2 = []
        review.add_durable_context(lambda text="": got2.append(text), linkproj)
        check("指向專案外的 docs/adr 不會被讀進材料包",
              "專案外的機密" not in "\n".join(got2), "\n".join(got2)[:300])
    else:
        print("[SKIP] 指向專案外的 docs/adr 不會被讀進材料包（無法建立連結）")

    # --- 走目錄時的點開頭白名單要跟 is_code_file 一致 ---
    from cross_review import transcript as tx2
    walkproj = base / "walkdots"
    (walkproj / ".github" / "workflows").mkdir(parents=True)
    (walkproj / ".venv" / "Lib").mkdir(parents=True)
    (walkproj / ".github" / "workflows" / "ci.yml").write_text("on: push\n",
                                                               encoding="utf-8")
    (walkproj / ".venv" / "Lib" / "x.py").write_text("x=1\n", encoding="utf-8")
    hits = tx2.walk_code_files(walkproj, 0.0)
    check("走目錄時 .github 不會被剪掉",
          any("ci.yml" in h for h in hits), str(hits))
    check("走目錄時 .venv 仍然被剪掉",
          not any("x.py" in h for h in hits), str(hits))


def main() -> int:
    base = Path(tempfile.mkdtemp(prefix="crossreview_test_"))
    try:
        scenario_non_git(base)
        scenario_git(base)
        scenario_boundaries(base)
        scenario_worktree(base)
        scenario_pinning(base)
        scenario_usage(base)
        scenario_hardening(base)
    finally:
        shutil.rmtree(base, ignore_errors=True)

    passed = sum(1 for r in RESULTS if r)
    print()
    print("整體：" + str(passed) + " / " + str(len(RESULTS))
          + ("　全部通過" if passed == len(RESULTS) else "　有測試失敗"))
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    sys.exit(main())
