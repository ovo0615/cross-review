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


def force_auto(project: Path) -> None:
    """把專案釘成 auto 觸發。

    預設是 threshold（小改不送審），所以驗「有改動就攔阻」的情境必須
    自己明講要 auto，否則測的其實是門檻沒跨過——那種綠燈什麼都沒證明。
    """
    rdir = project / ".claude" / "review"
    rdir.mkdir(parents=True, exist_ok=True)
    (rdir / "config.json").write_text(
        json.dumps({"trigger": "auto"}, ensure_ascii=False), encoding="utf-8")


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
        # str() 不能省：detail 常常是 list 或 dict，而這裡只有斷言失敗時才會走到。
        # 少了它，失敗的測試會以 TypeError 崩潰而不是印出 [FAIL]——
        # 整批測試就這樣停在半路，看起來像環境壞了而不是有測試沒過。
        line += "\n        " + str(detail)
    print(line)


# ---------------------------------------------------------------- 情境
def scenario_non_git(base: Path) -> None:
    project = base / "proj"
    (project / "src").mkdir(parents=True)
    force_auto(project)
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
    # config.json 的產生改由 scenario_trigger_modes 驗（這裡已被 force_auto
    # 先寫過，留在這裡只會是一條什麼都沒證明的綠燈）。
    check("config.json 仍在", (rdir / "config.json").exists())
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
    force_auto(project)
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

    # 網址白名單與 Chrome 的 proxy bypass 必須是同一份來源導出的。
    # 兩套手寫清單一定會不同步：0.0.0.0 通過了白名單卻不在 bypass 裡，
    # 實際導覽被送去失效的 proxy，截圖直接失敗。
    from cross_review.cdp import LOCAL_HOSTS, _bypass_list
    bypass = _bypass_list()
    mismatched = [h for h in LOCAL_HOSTS
                  if ("[::1]" if h == "::1" else h) not in bypass]
    check("每個本機主機都在 proxy bypass 清單裡", not mismatched, str(mismatched))

    # host-resolver-rules 的 MAP * 會把 IP 字面值也一起映射掉，
    # 所以每個本機主機都要個別 EXCLUDE。先前只寫了 EXCLUDE localhost，
    # 結果 localhost:埠 能用、127.0.0.1:埠 直接連不上——而白名單兩個都接受。
    # 政策允許了實作做不到的事，因為測試一直用 localhost 所以一直沒現形。
    from cross_review.cdp import _resolver_rules
    rules = _resolver_rules()
    missing_excludes = [h for h in LOCAL_HOSTS
                        if ("EXCLUDE " + h.strip("[]")) not in rules]
    check("每個本機主機都被 host-resolver-rules 排除",
          not missing_excludes, str(missing_excludes) + " / " + rules)
    check("非本機仍然被解析規則擋掉", rules.startswith("MAP * ~NOTFOUND"), rules)

    # 安全旗標不能用 truthiness：字串 "false" 是 truthy，
    # 邊界會被靜默打開而使用者以為自己關著。
    from cross_review.shots import as_bool
    for bad in ("false", "False", "no", "off", "0", ""):
        check("字串 " + repr(bad) + " 不會被當成 true",
              as_bool(bad, False) is False, repr(as_bool(bad, False)))
    for good in ("true", "True", "yes", "on", "1"):
        check("字串 " + repr(good) + " 會被當成 true", as_bool(good, False) is True)
    check("看不懂的字串用預設（安全的那邊）", as_bool("maybe", False) is False)
    check("allow_remote_urls 寫成字串 false 時仍然拒絕遠端",
          bool(url_is_allowed("http://10.0.0.5/", {"allow_remote_urls": "false"})))
    check("allow_file_urls 寫成字串 false 時仍然拒絕 file://",
          bool(url_is_allowed("file:///C:/x.txt", {"allow_file_urls": "false"})))

    # 探測請求不能走系統 proxy——那會把本機網址（含路徑與查詢字串）送出去。
    # 跟預設 opener 對比才與環境無關：預設的一定帶 ProxyHandler（讀環境變數），
    # 我們的因為傳了 ProxyHandler({}) 而完全不註冊 proxy 處理。
    # 這台機器沒設 proxy，所以直接比對 handler 清單看不出差異——
    # 必須在「有 proxy 的環境」下才測得到。臨時塞一個假的環境變數。
    import urllib.request as _ur
    from cross_review.shots import _NoRedirect
    saved = os.environ.get("HTTP_PROXY")
    os.environ["HTTP_PROXY"] = "http://proxy.invalid:8080"
    try:
        default_names = [type(h).__name__ for h in _ur.build_opener().handlers]
        ours_names = [type(h).__name__ for h in _ur.build_opener(
            _ur.ProxyHandler({}), _NoRedirect).handlers]
    finally:
        if saved is None:
            os.environ.pop("HTTP_PROXY", None)
        else:
            os.environ["HTTP_PROXY"] = saved
    check("有 proxy 環境時，預設 opener 會帶 ProxyHandler",
          "ProxyHandler" in default_names, str(default_names))
    check("有 proxy 環境時，我們的 opener 仍然不帶 proxy 處理",
          "ProxyHandler" not in ours_names, str(ours_names))

    # 問 Chrome 除錯埠的那條路也不能走 proxy——它回傳的 WebSocket 位址
    # 接下來會被拿去連線，等於讓外部決定我們的 CDP 連到哪裡。
    from cross_review.cdp import _NO_PROXY, _is_local_ws
    check("除錯埠的請求不帶 proxy 處理",
          "ProxyHandler" not in [type(h).__name__ for h in _NO_PROXY.handlers],
          str([type(h).__name__ for h in _NO_PROXY.handlers]))

    for good in ("ws://127.0.0.1:9222/devtools/page/AB",
                 "ws://localhost:9222/devtools/page/AB"):
        check("接受本機的 CDP 位址 " + good.split("/")[2], _is_local_ws(good))
    for bad in ("ws://evil.example.com:9222/devtools/page/AB",
                "ws://10.0.0.5:9222/devtools/page/AB",
                "http://127.0.0.1:9222/devtools/page/AB",
                "", "不是網址"):
        check("拒絕非本機或非 ws 的 CDP 位址 " + repr(bad)[:34],
              not _is_local_ws(bad))

    # 撞名一千次之後也不能回傳已用過的名字（那會覆寫別人的基準圖）。
    from cross_review.shots import unique_base
    taken = set()
    names = [unique_base("same", taken) for _ in range(1200)]
    check("撞名一千次以上仍然不會重複", len(set(names)) == len(names),
          str(len(names) - len(set(names))) + " 個重複")

    # Browser 建構失敗時不能留下暫存 profile 與孤兒行程。
    import tempfile as _tf
    from cross_review.cdp import Browser as _Browser
    before = set(Path(_tf.gettempdir()).glob("xrv-chrome-*"))
    try:
        _Browser(str(base / "不存在的chrome.exe"), 800, 600)
        check("不存在的 Chrome 應該要拋例外", False, "沒有拋例外")
    except Exception:
        check("不存在的 Chrome 會拋例外", True)
    after = set(Path(_tf.gettempdir()).glob("xrv-chrome-*"))
    check("建構失敗不會留下暫存 profile", after <= before,
          str(sorted(p.name for p in (after - before))))

    # scroll 的參數不對時要說出來，不能靜默成功。
    from cross_review.shots import apply_action as _apply

    class _FakeBrowser:
        def eval(self, expr):
            return False

    check("scroll 給錯 to 且沒有 selector 會回報錯誤",
          bool(_apply(_FakeBrowser(), {"do": "scroll", "to": "middle"})),
          repr(_apply(_FakeBrowser(), {"do": "scroll", "to": "middle"})))
    check("scroll 的 selector 找不到元素會回報錯誤",
          bool(_apply(_FakeBrowser(), {"do": "scroll", "selector": "#nope"})),
          repr(_apply(_FakeBrowser(), {"do": "scroll", "selector": "#nope"})))


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
    force_auto(proj)
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

    # --- review 目錄底下的「子項」各自也要檢查 ---
    # 最陰的是懸空的 errors.log 連結：exists() 是 False，
    # 所以「存在才檢查」的寫法會跳過它，接著 open(..., "a") 跟著寫到外面。
    childproj = base / "childlink"
    (childproj / ".claude" / "review").mkdir(parents=True)
    ghost2 = base / "ghost_errors_target"
    elink = childproj / ".claude" / "review" / "errors.log"
    made_e = False
    try:
        elink.symlink_to(ghost2)
        made_e = True
    except (OSError, NotImplementedError):
        # 檔案 symlink 要權限，但目錄 junction 不用。
        # 先指到一個存在的目錄再把它刪掉，就得到一個懸空連結——
        # review_child_is_safe 不分檔案或目錄，走的是同一條判斷。
        if os.name == "nt":
            ghost2.mkdir(exist_ok=True)
            r = subprocess.run(["cmd", "/c", "mklink", "/J", str(elink), str(ghost2)],
                               capture_output=True, timeout=30)
            made_e = r.returncode == 0
            if made_e:
                try:
                    ghost2.rmdir()
                except OSError:
                    made_e = False
    check("review 目錄本身是安全的", common.review_dir_is_safe(childproj))
    if made_e:
        check("懸空的 errors.log 連結會被判定不安全",
              not common.review_child_is_safe(childproj, "errors.log"))
        common.log_error(childproj, "這一行不該跟著連結寫到外面")
        check("懸空的 errors.log 連結不會被寫入", not ghost2.exists(),
              "ghost 檔被建立了")
    else:
        print("[SKIP] 懸空的 errors.log 連結會被判定不安全（無法建立檔案連結）")

    # shots/ 指向專案外也要擋
    slink = childproj / ".claude" / "review" / "shots"
    made_s = False
    outdir = base / "shots_outside"
    outdir.mkdir(exist_ok=True)
    try:
        slink.symlink_to(outdir, target_is_directory=True)
        made_s = True
    except (OSError, NotImplementedError):
        if os.name == "nt":
            r = subprocess.run(["cmd", "/c", "mklink", "/J", str(slink), str(outdir)],
                               capture_output=True, timeout=30)
            made_s = r.returncode == 0 and slink.exists()
    if made_s:
        check("指向專案外的 shots/ 會被判定不安全",
              not common.review_child_is_safe(childproj, "shots"))
    else:
        print("[SKIP] 指向專案外的 shots/ 會被判定不安全（無法建立連結）")

    # --- 工作單裡的非程式碼檔不能被讀進材料包 ---
    secretproj = base / "secretproj"
    (secretproj / ".claude").mkdir(parents=True)
    secret = secretproj / ".env"
    secret.write_text("API_KEY=絕對不能外流\n", encoding="utf-8")
    (secretproj / "ok.py").write_text("x = 1\n", encoding="utf-8")
    sjob = {"round": 1, "transcript": str(base / "nonexistent.jsonl"),
            "start_line": 0, "end_line": 0,
            "files": [str(secret), str(secretproj / "ok.py")], "deleted": []}
    stext, smeta = review.build_code_dossier(secretproj, sjob, common.DEFAULT_CONFIG)
    check("工作單裡的 .env 不會被讀進材料包",
          "絕對不能外流" not in stext, stext[:300])
    check("同一份工作單裡的程式碼檔仍然會被收錄",
          any(f.endswith("ok.py") for f in smeta["files"]), str(smeta["files"]))
    check("被拒絕的檔案有進 rejected 而不是靜默消失",
          any(str(f).endswith(".env") for f in smeta["rejected"]),
          str(smeta["rejected"]))

    # 被拒絕的「刪除項」也要進 rejected。只記 files 的話，工作單若只含
    # 這種刪除項就會變成「沒有改動」→ 回傳成功 → 寫 .done → 假完成。
    djob = {"round": 1, "transcript": str(base / "nonexistent.jsonl"),
            "start_line": 0, "end_line": 0, "files": [],
            "deleted": [str(base / "outside_deleted.py"), str(secretproj / "gone.env")]}
    _dt, dmeta = review.build_code_dossier(secretproj, djob, common.DEFAULT_CONFIG)
    check("被拒絕的刪除項也會進 rejected",
          len(dmeta["rejected"]) == 2, str(dmeta["rejected"]))
    check("只含被拒絕刪除項時不會被當成「沒有改動」",
          not dmeta["files"] and not dmeta["deleted"] and dmeta["rejected"],
          str(dmeta))

    # 個別 PNG 路徑也要檢查，不能只檢查 shots/ 目錄本身。
    pngproj = base / "pngproj"
    (pngproj / ".claude" / "review" / "shots").mkdir(parents=True)
    pngout = base / "png_outside"
    pngout.mkdir(exist_ok=True)
    plink = pngproj / ".claude" / "review" / "shots" / "x.png"
    made_p = False
    try:
        plink.symlink_to(pngout, target_is_directory=True)
        made_p = True
    except (OSError, NotImplementedError):
        if os.name == "nt":
            r = subprocess.run(["cmd", "/c", "mklink", "/J", str(plink), str(pngout)],
                               capture_output=True, timeout=30)
            made_p = r.returncode == 0 and plink.exists()
    check("shots/ 目錄本身是安全的",
          common.review_child_is_safe(pngproj, "shots"))
    if made_p:
        check("指向專案外的個別 PNG 路徑會被判定不安全",
              not common.review_child_is_safe(pngproj, "shots", "x.png"))
    else:
        print("[SKIP] 指向專案外的個別 PNG 路徑會被判定不安全（無法建立連結）")

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


def scenario_cdp_events(base: Path) -> None:
    """CDP 的事件佇列。重現審查者抓到的那個競爭，不需要真的 Chrome。

    競爭是這樣：call() 等自己的回應時若把沿路的事件丟掉，
    Page.loadEventFired 只要比 Page.navigate 的回應早到就永遠消失，
    goto() 接著等一個已經過去的事件，一路等到 socket 逾時。
    原本載入成功的畫面因此被判成審查失敗。
    """
    import json as _json
    import socket as _socket
    sys.path.insert(0, str(TOOL_ROOT))
    from cross_review.cdp import Browser

    class FakeWS:
        """模擬 CDP 端點。

        responses：method -> 該指令的 result
        inject   ：(在哪個 method 的回應之前, 要插入的事件)
                   ——這正是競爭的形狀：事件比指令回應早到。
        腳本用完就丟 socket.timeout（模擬沒有新訊息）。
        """

        def __init__(self, responses=None, inject=None, errors=()):
            self.responses = responses or {}
            self.inject = list(inject or [])
            self.errors = set(errors)      # 這些 method 回傳錯誤
            self.pending = []
            self.sent = []
            self.recv_calls = 0

        def send(self, text):
            msg = _json.loads(text)
            self.sent.append(msg)
            for after, event in self.inject:
                if after == msg["method"]:
                    self.pending.append(event)
            if msg["method"] in self.errors:
                self.pending.append(
                    {"id": msg["id"], "error": {"message": "不支援"}})
            else:
                self.pending.append(
                    {"id": msg["id"], "result": self.responses.get(msg["method"], {})})

        def recv(self):
            self.recv_calls += 1
            if not self.pending:
                raise _socket.timeout("沒有更多訊息")
            return _json.dumps(self.pending.pop(0))

    def make(responses=None, inject=None, errors=()):
        b = Browser.__new__(Browser)      # 不要真的啟動 Chrome
        b.ws = FakeWS(responses, inject, errors)
        b.timeout = 1.0
        b._id = 0
        b._events = []
        return b

    NAV = {"loaderId": "L-NEW", "frameId": "F-NEW"}

    # --- call() 必須把事件存起來，不是丟掉 ---
    b = make({"Page.enable": {"ok": True}},
             [("Page.enable", {"method": "Page.loadEventFired"})])
    result = b.call("Page.enable")
    check("call() 拿得到自己的回應", result == {"ok": True}, str(result))
    check("沿路的事件被存進佇列（不是丟掉）",
          [e.get("method") for e in b._events] == ["Page.loadEventFired"],
          str(b._events))

    # --- 競爭本身：這一次導航的載入事件比 navigate 的回應早到 ---
    b = make({"Page.navigate": NAV},
             [("Page.navigate", {"method": "Page.lifecycleEvent",
                                 "params": {"name": "load", "loaderId": "L-NEW"}})])
    b.goto("http://localhost:1/", settle_ms=0)
    # 判斷成功與否只看「有沒有多讀不存在的訊息」，不要用牆鐘時間——
    # 那在忙碌的機器上會偶發失敗，而且它證明不了任何邏輯。
    check("載入事件早到時 goto() 仍然認得（競爭已修）",
          b.ws.recv_calls == 4,
          "讀了 %d 次，預期 4 次（enable／lifecycle／事件／navigate）" % b.ws.recv_calls)

    # --- 上一頁的停止事件在清空「之後」抵達，不能被當成這一次完成 ---
    # 關鍵：主 frame 在前後頁導航時**是同一個 frameId**，所以這裡刻意用
    # 跟本次導航相同的 F-NEW。先前的測試用 F-OLD 對 F-NEW，
    # 等於假設前後導航會有不同 frameId——那個假設本身就是錯的，
    # 於是測試通過了，而真正會發生的情境完全沒被覆蓋。
    b = make({"Page.navigate": NAV},
             [("Page.navigate", {"method": "Page.frameStoppedLoading",
                                 "params": {"frameId": "F-NEW"}})])
    b.goto("http://localhost:1/", settle_ms=0)
    check("同一個主 frame 的舊停止事件不會被當成這一次導航完成",
          b.ws.recv_calls > 4,
          "只讀了 %d 次，代表拿 frameId 相符的舊事件充數" % b.ws.recv_calls)

    # --- 別人的 loaderId 也不算數 ---
    b = make({"Page.navigate": NAV},
             [("Page.navigate", {"method": "Page.lifecycleEvent",
                                 "params": {"name": "load", "loaderId": "L-OLD"}})])
    b.goto("http://localhost:1/", settle_ms=0)
    check("別次導航的 loaderId 不算數", b.ws.recv_calls > 4, str(b.ws.recv_calls))

    # --- 有 loaderId 時，不帶識別的事件一律不採信 ---
    b = make({"Page.navigate": NAV},
             [("Page.navigate", {"method": "Page.loadEventFired"})])
    b.goto("http://localhost:1/", settle_ms=0)
    check("有 loaderId 時不採信沒帶識別的 loadEventFired",
          b.ws.recv_calls > 4,
          "只讀了 %d 次，代表用了關聯不上的事件" % b.ws.recv_calls)

    # --- 降級的條件是「lifecycle 沒 enable 成功」，不是「沒有 loaderId」---
    # 這兩件事無關：setLifecycleEventsEnabled 失敗時，Page.navigate 通常
    # 還是會回傳 loaderId。把降級綁在 loaderId 上，會讓不支援 lifecycle 的
    # 環境下所有事件都被拒絕——每個畫面卡滿 30 秒逾時。
    for method, params in (("Page.loadEventFired", {}),
                           ("Page.frameStoppedLoading", {"frameId": "F-NEW"})):
        b = make({"Page.navigate": NAV},          # 注意：仍然有 loaderId
                 [("Page.navigate", {"method": method, "params": params})],
                 errors=["Page.setLifecycleEventsEnabled"])
        b.goto("http://localhost:1/", settle_ms=0)
        check("lifecycle enable 失敗時會降級採信 " + method,
              b.ws.recv_calls == 4,
              "讀了 %d 次，代表沒有降級、在等永遠不會來的 lifecycleEvent"
              % b.ws.recv_calls)

    # --- 導航直接失敗時要立刻拋，不要白等 30 秒再對錯誤頁截圖 ---
    b = make({"Page.navigate": {"errorText": "net::ERR_CONNECTION_REFUSED"}})
    try:
        b.goto("http://localhost:1/", settle_ms=0)
        check("Page.navigate 回報 errorText 時要拋例外", False, "沒有拋")
    except ConnectionError as exc:
        check("Page.navigate 回報 errorText 時要拋例外",
              "ERR_CONNECTION_REFUSED" in str(exc), str(exc))
    check("導航失敗時不會繼續等事件", b.ws.recv_calls == 3, str(b.ws.recv_calls))

    # --- navigate 之前要清掉上一頁殘留的事件 ---
    b = make({"Page.navigate": NAV})
    b._events.append({"method": "Page.lifecycleEvent",
                      "params": {"name": "load", "loaderId": "L-NEW"}})
    b.goto("http://localhost:1/", settle_ms=0)
    check("清空之前的殘留事件不會讓這次的等待立刻成功",
          b.ws.recv_calls > 3,
          "只讀了 %d 次，代表拿殘留事件充數" % b.ws.recv_calls)

    # --- 等不到載入事件時要走 settle 兜底，不是拋例外 ---
    b = make({"Page.navigate": NAV})
    try:
        b.goto("http://localhost:1/", settle_ms=0)
        check("等不到載入事件時走 settle 兜底而不是拋例外", True)
    except Exception as exc:
        check("等不到載入事件時走 settle 兜底而不是拋例外", False, repr(exc))

    # --- 佇列要有上限，頁面狂噴事件時不能無限成長 ---
    b = make()
    for i in range(Browser.MAX_QUEUED_EVENTS + 50):
        b._queue_event({"method": "Some.event", "n": i})
    check("事件佇列有上限", len(b._events) == Browser.MAX_QUEUED_EVENTS,
          str(len(b._events)))
    check("超過上限時丟掉最舊的", b._events[-1]["n"] == Browser.MAX_QUEUED_EVENTS + 49,
          str(b._events[-1]))
    b._queue_event({"id": 99, "result": {}})
    check("指令回應不會被當成事件存起來",
          all("method" in e for e in b._events))

    # --- 視埠尺寸必須完全等於設定值（要真的開 Chrome 才測得到）---
    # --window-size 給的是視窗大小，可視區還要扣掉瀏覽器自己的東西。
    # 加上網路隔離旗標後 Chrome 多一條提示列，可視高度從 804 掉到 748——
    # 同一份設定、同一個 --window-size，截圖卻小了 56 px。
    # 視覺回歸整個功能的前提就是尺寸穩定，所以這一項不能只靠結構檢查。
    import struct as _struct
    from cross_review.shots import resolve_chrome as _chrome
    chrome = _chrome()
    if not chrome:
        check("找得到 Chrome 才能驗視埠", False, "找不到 chrome.exe")
    else:
        shot = base / "viewport.png"
        for w, h, local_only in ((900, 640, True), (900, 640, False)):
            with Browser(chrome, w, h, local_only=local_only) as br:
                br.goto("about:blank", settle_ms=100)
                br.screenshot(shot)
            head = shot.read_bytes()[:33]
            got = _struct.unpack(">II", head[16:24])
            check("視埠等於設定值（local_only=%s）" % local_only,
                  got == (w, h), "拿到 %sx%s，預期 %sx%s" % (got[0], got[1], w, h))
        shot.unlink()


def scenario_trigger_modes(base: Path) -> None:
    """手動／門檻觸發。

    這一段要證明的核心是「不送審的那幾輪，改動不會消失」——累積量必須
    一路長大，而不是被水位線推掉。漏掉的改動不會有任何錯誤訊息。
    """
    proj = base / "trigproj"
    (proj / "src").mkdir(parents=True)
    (proj / "src" / "a.py").write_text("a = 1", encoding="utf-8")
    time.sleep(0.05)
    t = base / "trigsession.jsonl"
    t.write_text("", encoding="utf-8")

    # 全新專案沒有 config，hook 應該建一份，且預設是 threshold。
    touch(proj / "src" / "a.py", "a = 2")
    out, _ = run_hook(proj, t)
    cfg_path = proj / ".claude" / "review" / "config.json"
    check("全新專案會產生 config.json", cfg_path.exists())
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    check("預設觸發模式是 threshold", cfg.get("trigger") == "threshold", cfg.get("trigger"))
    check("小改不攔阻", "decision" not in (out or {}), out)
    check("小改會報累積量", "累積 1 個" in (out or {}).get("systemMessage", ""), out)

    rdir = proj / ".claude" / "review"
    check("沒送審就不建工作單",
          not list(rdir.glob("job-*.json")), [f.name for f in rdir.glob("*")])

    # 純手動：再改兩個檔，累積量要長到 3，不能被水位線推掉。
    cfg_path.write_text(json.dumps({"trigger": "manual"}), encoding="utf-8")
    before = json.loads((rdir / "state.json").read_text(encoding="utf-8"))
    touch(proj / "src" / "b.py", "b = 1")
    touch(proj / "src" / "c.py", "c = 1")
    out, _ = run_hook(proj, t)
    check("手動模式永不攔阻", "decision" not in (out or {}), out)
    check("沒送審的改動會累積，不會消失",
          "累積 3 個" in (out or {}).get("systemMessage", ""), out)
    after = json.loads((rdir / "state.json").read_text(encoding="utf-8"))
    check("沒送審就不推進水位線",
          after.get("watermark") == before.get("watermark"),
          (before.get("watermark"), after.get("watermark")))
    check("沒送審就不推進回合編號",
          after.get("round") == before.get("round"),
          (before.get("round"), after.get("round")))

    # 門檻：把門檻壓到 2，同一批累積就該自動送一次。
    cfg_path.write_text(json.dumps({"trigger": "threshold", "auto_when_files": 2}),
                        encoding="utf-8")
    touch(proj / "src" / "a.py", "a = 3")
    out, _ = run_hook(proj, t)
    check("超過門檻會自動送審", (out or {}).get("decision") == "block", out)
    check("自動送審要說明是門檻觸發的",
          "門檻" in (out or {}).get("reason", ""), (out or {}).get("reason", "")[:120])
    check("門檻觸發也會建工作單", bool(list(rdir.glob("job-*.json"))))


def scenario_receipt(base: Path) -> None:
    """手動觸發的審查跑完後，收據要由 hook 兌現。

    審查行程不能直接改 state.json——hook 也在寫那個檔案，兩個行程互相覆蓋。
    """
    from cross_review import dispatch
    proj = base / "receiptproj"
    (proj / "src").mkdir(parents=True)
    (proj / "src" / "a.py").write_text("a = 1", encoding="utf-8")
    rdir = proj / ".claude" / "review"
    rdir.mkdir(parents=True, exist_ok=True)
    (rdir / "config.json").write_text(json.dumps({"trigger": "manual"}),
                                      encoding="utf-8")
    time.sleep(0.05)
    t = base / "receiptsession.jsonl"
    t.write_text("", encoding="utf-8")

    touch(proj / "src" / "a.py", "a = 2")
    out, _ = run_hook(proj, t)
    check("兌現前有累積", "累積 1 個" in (out or {}).get("systemMessage", ""), out)

    # 假裝手動審查跑完了，留下收據。
    dispatch.write_receipt(proj, {"round": 7, "transcript": str(t),
                                  "end_line": 0, "deleted": []}, head_sha="abc123")
    out, _ = run_hook(proj, t)
    check("兌現收據後累積歸零", out is None or "累積" not in str(out), out)
    st = json.loads((rdir / "state.json").read_text(encoding="utf-8"))
    check("收據推進了回合編號", st.get("round") == 7, st.get("round"))
    check("收據推進了 head_sha", st.get("head_sha") == "abc123", st.get("head_sha"))
    check("收據只兌現一次", st.get("receipt_round") == 7, st.get("receipt_round"))

    # --- 第 30 回合審查抓到的：收據不能用「審完當下」當水位線 ---
    p2 = base / "receiptclock"
    (p2 / ".claude" / "review").mkdir(parents=True)
    jp = dispatch.create_job(p2, 1, "", 0, 0, 0.0, [], [], base_sha="abc")
    job = json.loads(jp.read_text(encoding="utf-8"))
    check("工作單有記下派工時刻", bool(job.get("dispatched")), job.get("dispatched"))
    time.sleep(0.6)                       # 假裝審查跑了一段時間
    dispatch.write_receipt(p2, job, "abc")
    receipt = json.loads(
        (p2 / ".claude" / "review" / "reviewed.json").read_text(encoding="utf-8"))
    check("收據沿用派工時刻，不是審完時刻",
          abs(receipt["watermark"] - job["dispatched"]) < 0.001,
          receipt["watermark"] - job["dispatched"])

    # --- 部分模式沒跑完就不能寫收據 ---
    p3 = base / "receiptpartial"
    (p3 / ".claude" / "review").mkdir(parents=True)
    jp3 = dispatch.create_job(p3, 1, "", 0, 0, 0.0, [], [], base_sha="abc")
    (jp3.with_suffix(".visual.done")).write_text("", encoding="utf-8")
    check("只有一種審查跑完就不算審過",
          not dispatch.all_modes_done(jp3, ["visual", "code"]))
    (jp3.with_suffix(".code.done")).write_text("", encoding="utf-8")
    check("兩種都跑完才算審過",
          dispatch.all_modes_done(jp3, ["visual", "code"]))
    check("一種模式都沒派就不算審過", not dispatch.all_modes_done(jp3, []))

    # --- 未追蹤的新檔案也要算進門檻（git diff 裡沒有它） ---
    p4 = base / "threshnew"
    (p4 / "src").mkdir(parents=True)
    (p4 / ".claude" / "review").mkdir(parents=True)
    env4 = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
                GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
    (p4 / "src" / "seed.py").write_text("x = 1", encoding="utf-8")
    for cmd in (["init", "-q"], ["add", "-A"], ["commit", "-qm", "i"]):
        subprocess.run(["git", "-C", str(p4)] + cmd, env=env4,
                       capture_output=True, timeout=60)
    newbig = p4 / "src" / "brand_new.py"
    newbig.write_text("# " + "a" * 60000, encoding="utf-8")
    from cross_review import common as _c
    hit = dispatch.over_threshold(dict(_c.DEFAULT_CONFIG), p4, [str(newbig)], [], "HEAD")
    check("未追蹤的大檔會觸發門檻", bool(hit), repr(hit))

    # --- 派工時刻必須在偵測之前取（第 31 回合審查） ---
    p5 = base / "dispatchclock"
    (p5 / ".claude" / "review").mkdir(parents=True)
    jp5 = dispatch.create_job(p5, 1, "", 0, 0, 0.0, [], [], base_sha="a",
                              dispatched=12345.0)
    check("create_job 會沿用呼叫端傳進來的派工時刻",
          json.loads(jp5.read_text(encoding="utf-8"))["dispatched"] == 12345.0)


def scenario_referenced_context(base: Path) -> None:
    """使用者說「依照你的建議」時，那個建議本文要進材料包。

    材料包只收使用者發言的話，審查者會知道使用者同意了某件事卻不知道
    那是什麼——第 31 回合的審查就回報過「無法完整核對需求」。
    """
    from cross_review import transcript as tx
    t = base / "refctx.jsonl"
    rows = [
        {"type": "user", "message": {"role": "user", "content": "幫我修那個 bug"}},
        {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "text", "text": "我建議三件事：拆檔案、提早取時刻、補材料包"}]}},
        {"type": "user", "message": {"role": "user", "content": "可以動手"}},
    ]
    t.write_text("\n".join(
        json.dumps(r, ensure_ascii=False) for r in rows), encoding="utf-8")
    parsed = tx.parse(t, 0)
    check("指代時會收進上一則助手回覆",
          any("拆檔案" in x for x in parsed["referenced_context"]),
          parsed["referenced_context"])

    # 真實情況：助手上一回合提建議、回合結束游標推進、使用者這一回合才說
    # 「可以動手」——那則建議**永遠**在游標之前。上面那條 start_line=0 的
    # 斷言測不到這件事（第 32 回合的材料包就是這樣缺掉的）。
    parsed_real = tx.parse(t, 2)
    check("助手回覆在游標之前也要抓得到",
          any("拆檔案" in x for x in parsed_real["referenced_context"]),
          parsed_real["referenced_context"])

    # 又否定又肯定（「第一項不要，其他照做」）原本會被整句丟掉，審查者連
    # 「部分採用」的脈絡都沒有。現在一律保留脈絡，安全性靠材料包的措辭：
    # 它不宣稱使用者同意，只說「這是使用者在回應的東西，以他的原文為準」。
    for word in ("不要照做", "第一項不要，其他照做", "不用等了，照做"):
        rows_n = rows[:2] + [{"type": "user",
                              "message": {"role": "user", "content": word}}]
        tn = base / ("refneg_" + str(abs(hash(word)) % 10000) + ".jsonl")
        tn.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows_n),
                      encoding="utf-8")
        check("否定或部分採用時仍保留脈絡：" + word,
              tx.parse(tn, 0)["referenced_context"] != [],
              tx.parse(tn, 0)["referenced_context"])

    # 措辭是這個設計的安全來源，所以要驗它。
    from cross_review.review import add_user_voice
    rendered_lines = []
    add_user_voice(lambda s="": rendered_lines.append(s),
                   {"user_requests": ["不要照做"],
                    "referenced_context": ["我建議三件事"]}, {})
    rendered = "\n".join(rendered_lines)
    check("材料包不會宣稱使用者同意了那段內容",
          "已經同意" not in rendered and "以上面使用者的原文為準" in rendered,
          rendered[:200])
    check("材料包不會斷言那段脈絡一定相關",
          "不一定相關" in rendered and "不是需求" in rendered,
          rendered[:200])

    # 使用者實際說的是「動手」，不是「可以動手」。白名單漏了這個詞，
    # 連續三輪失效——長度判準才是可靠的那一個。
    for word in ("動手", "繼續", "都做", "好"):
        rows_s2 = rows[:2] + [{"type": "user",
                               "message": {"role": "user", "content": word}}]
        t_s = base / ("refshort_" + str(abs(hash(word)) % 10000) + ".jsonl")
        t_s.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows_s2),
                       encoding="utf-8")
        check("極短的回應一律視為指代：" + word,
              tx.parse(t_s, 0)["referenced_context"] != [],
              tx.parse(t_s, 0)["referenced_context"])

    # 助手訊息的 content 可能是字串而不是 block 陣列。只處理陣列的話，
    # 不但抓不到，還會留著更早那一則，指代指向錯誤的內容。
    rows_s = [
        {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "text", "text": "舊的建議"}]}},
        {"type": "assistant", "message": {"role": "assistant",
                                          "content": "新的建議（字串型態）"}},
        {"type": "user", "message": {"role": "user", "content": "依照你的建議"}},
    ]
    ts = base / "refstr.jsonl"
    ts.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows_s),
                  encoding="utf-8")
    got_s = tx.parse(ts, 0)["referenced_context"]
    check("字串型態的助手訊息也抓得到，而且抓的是最近那一則",
          got_s and "新的建議" in got_s[0], got_s)

    rows2 = rows[:2] + [{"type": "user",
                         "message": {"role": "user", "content":
        "把首頁的標題字級改成 18px，並且把側邊欄背景換成深灰色"}}]
    t2 = base / "refctx2.jsonl"
    t2.write_text("\n".join(
        json.dumps(r, ensure_ascii=False) for r in rows2), encoding="utf-8")
    check("夠長而且自帶需求的發言不收，材料包不會白白變大",
          tx.parse(t2, 0)["referenced_context"] == [],
          tx.parse(t2, 0)["referenced_context"])

    # 太長的回覆要截尾巴留結論，而且要講明被截過。
    rows3 = [
        {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "text", "text": "x" * 9000 + "結論在這裡"}]}},
        {"type": "user", "message": {"role": "user", "content": "依照你的建議"}},
    ]
    t3 = base / "refctx3.jsonl"
    t3.write_text("\n".join(
        json.dumps(r, ensure_ascii=False) for r in rows3), encoding="utf-8")
    got = tx.parse(t3, 0)["referenced_context"]
    check("過長的回覆保留結尾的結論",
          got and "結論在這裡" in got[0], (got or [""])[0][-30:])
    check("截斷有講明", got and "前面截斷" in got[0], (got or [""])[0][:30])


def scenario_breaker_and_usage(base: Path) -> None:
    """斷路器與用量帳本。錯誤訊息是 2026-09-02 額度真的用完時擷取的原文。"""
    sys.path.insert(0, str(TOOL_ROOT))
    from cross_review import breaker, common, usage
    import time as _time

    REAL = ("codex 結束（exit 1，34 秒）但沒有輸出：ERROR: You've hit your usage "
            "limit. Upgrade to Pro (https://chatgpt.com/explore/pro), visit "
            "https://chatgpt.com/codex/settings/usage to purchase more credits "
            "or try again at 1:27 PM. / tokens used / 35,477")

    check("認得出額度用完的錯誤", breaker.is_quota_error(REAL))
    check("一般錯誤不會被當成額度問題",
          not breaker.is_quota_error("codex 逾時（900 秒）"))

    # 時間解析：早上 09:00 看到「1:27 PM」應該是今天 13:27。
    morning = _time.mktime((2026, 9, 2, 9, 0, 0, 0, 0, -1))
    got = breaker.parse_reset_time(REAL, now=morning)
    check("從訊息解析出恢復時間",
          _time.strftime("%H:%M", _time.localtime(got)) == "13:27",
          _time.strftime("%Y-%m-%d %H:%M", _time.localtime(got)))
    check("恢復時間在未來", got > morning)
    # 晚上 20:00 看到「1:27 PM」應該是明天，不是今天的過去式。
    evening = _time.mktime((2026, 9, 2, 20, 0, 0, 0, 0, -1))
    got2 = breaker.parse_reset_time(REAL, now=evening)
    check("已經過去的時刻要算成明天", got2 > evening,
          _time.strftime("%Y-%m-%d %H:%M", _time.localtime(got2)))
    check("解析不到時間就回 0", breaker.parse_reset_time("完全不相干的錯誤") == 0.0)

    proj = base / "breakerproj"
    (proj / ".claude" / "review").mkdir(parents=True)

    # 額度錯誤：一次就跳閘，不必等三次——再試也是白試。
    note = breaker.record_failure(proj, REAL)
    check("額度用完一次就跳閘", bool(note), repr(note))
    check("跳閘後 hook 會拿到暫停訊息",
          "審查暫停中" in breaker.paused_note(proj), breaker.paused_note(proj))
    check("暫停訊息含恢復時間", "自動恢復" in breaker.paused_note(proj))

    # 額度暫停**只有時間**能解除。這一段原本斷言「成功一次就解除」，
    # 那正是第 29 回合審查抓到的錯誤行為：兩種審查併行派工時，後完成的那個
    # 成功（很可能在額度耗盡之前就送出去了）會把額度暫停清掉，於是下一輪
    # 立刻再燒一次。測試把 bug 寫成了規格，所以改對之後這一條會失敗。
    breaker.record_success(proj, "visual")
    check("另一個模式成功不解除額度暫停",
          "審查暫停中" in breaker.paused_note(proj), breaker.paused_note(proj))
    breaker.record_success(proj, "code")
    check("同一個模式成功也不解除額度暫停",
          "審查暫停中" in breaker.paused_note(proj), breaker.paused_note(proj))

    # 失敗計數是分模式的：視覺一直成功不該幫程式碼把計數清零。
    proj3 = base / "breakerproj3"
    (proj3 / ".claude" / "review").mkdir(parents=True)
    for i in (1, 2):
        breaker.record_failure(proj3, "boom", "code")
        breaker.record_success(proj3, "visual")
    note3 = breaker.record_failure(proj3, "boom", "code")
    check("視覺的成功不會清掉程式碼的失敗計數", bool(note3), repr(note3))
    check("連續失敗的暫停只擋壞掉的那個模式",
          breaker.paused_note(proj3, "code") != ""
          and breaker.paused_note(proj3, "visual") == "",
          "code=%r visual=%r" % (breaker.paused_note(proj3, "code"),
                                 breaker.paused_note(proj3, "visual")))
    breaker.record_success(proj3, "code")
    check("那個模式自己成功就解除自己的暫停",
          breaker.paused_note(proj3, "code") == "", breaker.paused_note(proj3, "code"))

    # 兩個模式各自跳閘時，後跳的那個不可以蓋掉前一個的暫停——
    # 失敗計數分了模式、暫停狀態卻共用一組的話，還在壞的那個會提前恢復。
    proj6 = base / "breakerproj6"
    (proj6 / ".claude" / "review").mkdir(parents=True)
    for _ in range(3):
        breaker.record_failure(proj6, "boom", "code")
    for _ in range(3):
        breaker.record_failure(proj6, "boom", "visual")
    check("後跳閘的模式不會蓋掉先跳閘的",
          breaker.paused_note(proj6, "code") != ""
          and breaker.paused_note(proj6, "visual") != "",
          "code=%r visual=%r" % (breaker.paused_note(proj6, "code"),
                                 breaker.paused_note(proj6, "visual")))
    breaker.record_success(proj6, "visual")
    check("解除一個模式不會連帶解除另一個",
          breaker.paused_note(proj6, "code") != ""
          and breaker.paused_note(proj6, "visual") == "",
          "code=%r visual=%r" % (breaker.paused_note(proj6, "code"),
                                 breaker.paused_note(proj6, "visual")))

    # 併行的兩個審查行程各自「讀出整份→改→整份寫回」時，後寫入者會蓋掉
    # 前一個——而額度用完時兩邊幾乎同時失敗，那正是這個競態的典型情況。
    # 修法是每個檔案只有一個寫入者：模式檔各自寫，全域檔只放額度／限流。
    proj7 = base / "breakerproj7"
    (proj7 / ".claude" / "review").mkdir(parents=True)
    breaker.record_failure(proj7, REAL, "code")
    check("額度暫停擋住另一個模式",
          breaker.paused_note(proj7, "visual") != "", breaker.paused_note(proj7, "visual"))
    breaker.record_success(proj7, "visual")
    check("另一個模式成功不會蓋掉額度暫停",
          breaker.paused_note(proj7, "code") != "", breaker.paused_note(proj7, "code"))
    rdir7 = proj7 / ".claude" / "review"
    # visual 沒有失敗過，所以不會有它的檔案——record_success() 對一份
    # 本來就乾淨的狀態不寫入。要驗的是「code 的計數寫在自己的檔案裡」。
    check("失敗計數寫在各模式自己的檔案裡",
          (rdir7 / "breaker.code.json").exists(),
          sorted(f.name for f in rdir7.glob("breaker*.json")))
    check("沒有任何共用的狀態檔",
          not (rdir7 / "breaker.json").exists(),
          sorted(f.name for f in rdir7.glob("breaker*.json")))

    # 兩個模式解析到的恢復時間不一定相同：一邊從訊息拿到明確時刻，另一邊
    # 沒拿到而用一小時兜底。共寫一個檔的話，後寫的兜底值會把期限蓋短。
    proj8 = base / "breakerproj8"
    (proj8 / ".claude" / "review").mkdir(parents=True)
    breaker.record_failure(proj8, REAL, "code")            # 帶明確恢復時間
    long_note = breaker.paused_note(proj8, "code")
    breaker.record_failure(proj8, "You've hit your usage limit.", "visual")  # 只能兜底
    check("兜底的期限不會把明確的期限蓋短",
          breaker.paused_note(proj8, "code") == long_note,
          (long_note, breaker.paused_note(proj8, "code")))

    # 舊格式升級：單模式的暫停不可以被當成帳號層級的而擋住另一個模式。
    proj9 = base / "breakerproj9"
    (proj9 / ".claude" / "review").mkdir(parents=True)
    common.write_json(proj9 / ".claude" / "review" / "breaker.json",
                      {"failures": {"code": 3}, "paused_until": time.time() + 3600,
                       "pause_kind": "failures", "pause_mode": "code",
                       "reason": "boom"})
    check("舊格式的單模式暫停仍只擋那個模式",
          breaker.paused_note(proj9, "code") != ""
          and breaker.paused_note(proj9, "visual") == "",
          "code=%r visual=%r" % (breaker.paused_note(proj9, "code"),
                                 breaker.paused_note(proj9, "visual")))
    common.write_json(proj9 / ".claude" / "review" / "breaker.json",
                      {"paused_until": time.time() + 3600, "pause_kind": "quota",
                       "reason": "額度"})
    (proj9 / ".claude" / "review" / "breaker.code.json").unlink(missing_ok=True)
    check("舊格式的額度暫停仍擋所有模式",
          breaker.paused_note(proj9, "visual") != "",
          breaker.paused_note(proj9, "visual"))

    # 升級的實際組合：上上一版同時寫模式檔與共用的 breaker.json，所以一個
    # 在那個版本上遇到額度用完的專案，兩種檔案會同時存在。只在模式檔缺席時
    # 才讀舊檔的話，那個仍然有效的暫停會被靜默丟掉然後提前再送一次審。
    proj10 = base / "breakerproj10"
    r10 = proj10 / ".claude" / "review"
    r10.mkdir(parents=True)
    common.write_json(r10 / "breaker.json",
                      {"paused_until": time.time() + 3600,
                       "pause_kind": "quota", "reason": "額度用完"})
    for m in ("code", "visual"):
        common.write_json(r10 / ("breaker." + m + ".json"),
                          {"failures": 0, "paused_until": 0.0, "reason": ""})
    check("模式檔與舊全域檔同時存在時，舊的額度暫停仍生效",
          breaker.paused_note(proj10, "code") != "",
          breaker.paused_note(proj10, "code"))

    # 同一個模式仍可能有兩個行程（上一輪還在跑、下一輪又派了一次）。
    # 寫入前取最大值，讓競態最多少加一次計數，不會讓已生效的暫停消失。
    proj11 = base / "breakerproj11"
    (proj11 / ".claude" / "review").mkdir(parents=True)
    breaker.record_failure(proj11, REAL, "code")          # 行程 A：額度暫停
    kept = breaker.paused_note(proj11, "code")
    breaker.record_success(proj11, "code")                # 行程 B：成功歸零
    check("成功歸零不會把帳號層級的暫停一起清掉",
          breaker.paused_note(proj11, "code") == kept,
          (kept, breaker.paused_note(proj11, "code")))

    # 成功行程讀完狀態之後、寫回之前，另一個同模式的行程又記了失敗。
    # 舊的成功不該把新的失敗抹掉——寫回時只清「自己讀到的那些」。
    proj12 = base / "breakerproj12"
    (proj12 / ".claude" / "review").mkdir(parents=True)
    for _ in range(2):
        breaker.record_failure(proj12, "boom", "code")
    stale = breaker._load_mode(proj12, "code")      # 成功行程此刻讀到的狀態
    breaker.record_failure(proj12, "boom", "code")  # 第 3 次 → 模式暫停成立
    breaker._write_mode(proj12, "code", stale, reset=True)
    check("成功不會抹掉它讀完之後才出現的模式暫停",
          breaker.paused_note(proj12, "code") != "",
          breaker.paused_note(proj12, "code"))

    # 期限來自舊版的額度限制時，原因也要跟著它走。模式檔裡留著更早的一般
    # 錯誤的話，訊息會變成「因為連線逾時所以等到 13:27」，使用者判斷不了
    # 該等還是該查。
    proj13 = base / "breakerproj13"
    r13 = proj13 / ".claude" / "review"
    r13.mkdir(parents=True)
    common.write_json(r13 / "breaker.json",
                      {"paused_until": time.time() + 3600,
                       "pause_kind": "quota", "reason": "額度用完"})
    common.write_json(r13 / "breaker.code.json",
                      {"failures": 1, "paused_until": 0.0, "account_until": 0.0,
                       "reason": "連線逾時"})
    check("暫停原因要跟著勝出的那個暫停走",
          "額度用完" in breaker.paused_note(proj13, "code"),
          breaker.paused_note(proj13, "code"))

    # 短暫限流沒有恢復時間就不該當成額度耗盡而停一小時。
    proj4 = base / "breakerproj4"
    (proj4 / ".claude" / "review").mkdir(parents=True)
    breaker.record_failure(proj4, "Error: rate limit exceeded, please retry", "code")
    check("限流沒帶恢復時間就當一般失敗",
          breaker.paused_note(proj4, "code") == "", breaker.paused_note(proj4, "code"))
    proj5 = base / "breakerproj5"
    (proj5 / ".claude" / "review").mkdir(parents=True)
    breaker.record_failure(proj5, "rate limit, try again at 11:59 PM", "code")
    check("限流有帶恢復時間就照它等",
          "自動恢復" in breaker.paused_note(proj5, "code"),
          breaker.paused_note(proj5, "code"))

    # diff 超出預算時要整個檔案整個檔案地丟。原本是直接截位元組，於是
    # 標頭在切點前、hunk 在切點後的檔案會被 diff_covers() 算成「已涵蓋」，
    # 全文因此被省略——切點之後的改動靜默消失，報告卻說「已由 diff 提供」。
    from cross_review import transcript as tx
    big = "diff --git a/big.py b/big.py\n" + "".join(
        "-old%d\n+new%d\n" % (i, i) for i in range(400))
    small = "diff --git a/small.py b/small.py\n-a\n+b\n"
    secs = tx._diff_sections(big + small)
    check("diff 切得出每個檔案一段",
          [s[0] for s in secs] == ["big.py", "small.py"], repr([s[0] for s in secs]))
    budget = len(big.encode("utf-8")) - 100          # 連第一個檔案都放不下
    kept, used = [], 0
    for name, body in secs:
        if used + len(body.encode("utf-8")) <= budget:
            kept.append(body)
            used += len(body.encode("utf-8"))
    covered = tx.diff_covers("".join(kept), base)
    check("放不下的檔案不會被宣稱已涵蓋",
          not any(c.endswith("big.py") for c in covered), sorted(covered))

    # 失敗那一趟也燒了額度，帳本一定要記到——否則最想分析額度時剛好少算。
    projf = base / "usagefail"
    (projf / ".claude" / "review").mkdir(parents=True)
    real_stderr = ("model: gpt-5.6-sol\nreasoning effort: high\n"
                   "ERROR: You have hit your usage limit.\ntokens used: 35,477\n")
    facts = common.parse_run_facts(real_stderr)
    facts["_failed"] = True
    usage.record(projf, "code", 1, facts, 51900)
    rows = usage.read_rows(projf)
    check("失敗的那一趟也進帳本", len(rows) == 1, rows)
    check("失敗那趟的 tokens 有記到", rows and rows[0]["tokens"] == 35477, rows)
    check("帳本看得出那趟是白花的", rows and rows[0]["ok"] is False, rows)

    # 合法 JSON 但不是紀錄（null／陣列／數字）也要略過，不能讓 --usage 崩掉。
    projn = base / "usagenull"
    (projn / ".claude" / "review").mkdir(parents=True)
    (projn / ".claude" / "review" / "usage.jsonl").write_text(
        '{"tokens":100,"seconds":1,"model":"m","effort":"e"}\nnull\n[1]\n42\n',
        encoding="utf-8")
    check("非物件的合法 JSON 行會被略過", len(usage.read_rows(projn)) == 1,
          usage.read_rows(projn))
    check("--usage 不會被那種行弄崩", "共 1 次審查" in usage.summary(projn),
          usage.summary(projn).splitlines()[:3])

    # 欄位型別不對的另一種：model 是數字時，summary() 的字串相接會 TypeError。
    projt = base / "usagetype"
    (projt / ".claude" / "review").mkdir(parents=True)
    (projt / ".claude" / "review" / "usage.jsonl").write_text(
        '{"tokens":1,"seconds":1,"model":123,"effort":"high"}', encoding="utf-8")
    check("model 是數字時 --usage 也不會崩",
          "共 1 次審查" in usage.summary(projt), usage.summary(projt)[:60])

    # 一般錯誤要連續三次才跳閘。
    proj2 = base / "breakerproj2"
    (proj2 / ".claude" / "review").mkdir(parents=True)
    for i in (1, 2):
        check("一般錯誤第 %d 次不跳閘" % i,
              not breaker.record_failure(proj2, "codex 逾時（900 秒）"))
    check("一般錯誤第 3 次跳閘",
          bool(breaker.record_failure(proj2, "codex 逾時（900 秒）")))

    # 用量帳本
    usage.record(proj, "code", 1,
                 {"_model": "gpt-5.6-sol", "_effort": "high",
                  "_elapsed_sec": 144.4, "_tokens": 80075,
                  "findings": [1, 2, 3], "blocking": False}, 99800)
    usage.record(proj, "visual", 1,
                 {"_model": "gpt-5.6-luna", "_effort": "high",
                  "_elapsed_sec": 119.3, "_tokens": 65192,
                  "findings": [1], "blocking": False}, 24000)
    text = usage.summary(proj)
    check("彙總含總 tokens", "145,267" in text, text[:300])
    check("彙總依模型分列", "gpt-5.6-sol" in text and "gpt-5.6-luna" in text)
    check("彙總含次數", "共 2 次審查" in text, text[:200])
    check("沒有帳本時不會爆炸",
          "還沒有任何用量紀錄" in usage.summary(base / "沒有這個專案"))

    # 壞掉的一行不該讓整份帳本讀不出來
    with open(proj / ".claude" / "review" / "usage.jsonl", "a",
              encoding="utf-8") as fh:
        fh.write("{這不是合法 JSON\n")
    check("帳本裡有壞行仍讀得出其餘的", len(usage.read_rows(proj)) == 2,
          str(len(usage.read_rows(proj))))


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
        scenario_cdp_events(base)
        scenario_trigger_modes(base)
        scenario_receipt(base)
        scenario_referenced_context(base)
        scenario_breaker_and_usage(base)
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
