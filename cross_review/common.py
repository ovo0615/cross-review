"""共用的路徑、設定、狀態與 codex.exe 解析。

這個模組刻意不做任何慢的事。hook 每一輪都會載入它，必須瞬間完成。
"""
from __future__ import annotations

import glob
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path


# ---------------------------------------------------------------- 主控台編碼
def force_utf8_stdio() -> None:
    """Windows 主控台預設 cp950。

    hook 的 stdout 會被 Claude Code 當 UTF-8 讀，不強制轉換的話中文變亂碼
    （CLAUDE.md 4.3 的同一類陷阱）。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass


# ---------------------------------------------------------------- 檔案讀寫
# 一律明確指定 UTF-8。codex 的 -o 輸出是無 BOM 的 UTF-8，
# 用系統預設編碼讀它在 Windows 上一定壞（今天實測踩過）。
def read_text(path) -> str:
    return Path(path).read_text(encoding="utf-8")


def write_text(path, text: str) -> None:
    """先寫暫存檔再原子替換。

    直接覆寫的話，寫到一半被中斷就留下半份檔案；而 read_json 讀不開會靜默
    回退成空的預設值，於是 state.json 壞掉等於「游標歸零、回合歸零」，
    沒有任何錯誤訊息。os.replace 在同一個磁碟區上是原子的：
    讀到的要嘛是舊的完整內容，要嘛是新的完整內容。
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp-" + str(os.getpid()))
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, p)
    except Exception:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
        raise


def read_json(path, default=None):
    try:
        return json.loads(read_text(path))
    except Exception:
        return default


def write_json(path, obj) -> None:
    write_text(path, json.dumps(obj, ensure_ascii=False, indent=2) + "\n")


# ---------------------------------------------------------------- 專案目錄
REVIEW_DIRNAME = os.path.join(".claude", "review")


def review_dir(project: Path) -> Path:
    return project / REVIEW_DIRNAME


def review_dir_is_safe(project: Path) -> bool:
    """確認 .claude/review 真的落在專案裡面。

    這個工具會在每個專案自動執行，包含 clone 回來的版本庫。版本庫可以帶著
    一個把 .claude/review 指到專案外的 symlink 或 junction，工具接著就會把
    config.json、state.json、報告寫到那個位置去，覆蓋掉使用者的別的檔案。
    """
    target = review_dir(project)
    try:
        resolved = target.resolve()
        expected = project.resolve() / ".claude" / "review"
    except OSError:
        return False

    # 判斷方式只有一條：解析之後的路徑必須「正好」是預期的位置。
    #
    # 第一版是往上走訪祖先、看有沒有哪一層被改指向別處，那個做法從根本上就錯。
    # 懸空的 symlink 會騙過它：.claude/review 指向專案外一個還不存在的路徑時，
    # resolve() 之後 exists() 是 False，於是走進「還沒建立」那條分支、
    # 去檢查安全的 .claude 父目錄然後放行——接著 mkdir 會沿著 symlink
    # 在專案外把目錄建出來。
    #
    # resolve() 會解析路徑上的每一個環節（symlink、junction、.. 等），
    # 所以只要結果不等於預期位置，就代表中間有人被改了指向，不管那一層是誰、
    # 也不管目標存不存在。
    if resolved != expected:
        return False

    # 已存在但不是目錄（普通檔案）也不安全：檢查會過，接著 mkdir 會拋例外。
    if resolved.exists() and not resolved.is_dir():
        return False
    return True


def review_child_is_safe(project: Path, *parts) -> bool:
    """確認 .claude/review 底下的某個子項沒有被改指向別處。

    只檢查 review 目錄本身是不夠的：shots/、baseline/、errors.log 都可以
    各自被做成連結。最陰的是**懸空的** errors.log —— exists() 是 False，
    所以任何「存在才檢查」的寫法都會跳過它，接著 open(path, "a")
    會跟著連結把內容追加到專案外的檔案上。

    判斷方式跟 review_dir_is_safe 一樣：解析後必須正好等於預期位置。
    """
    if not review_dir_is_safe(project):
        return False
    target = review_dir(project).joinpath(*parts)
    try:
        return target.resolve() == review_dir(project).resolve().joinpath(*parts)
    except OSError:
        return False


def is_disabled(project: Path) -> bool:
    """專案層級的永久關閉開關（第 6 題）。"""
    return (review_dir(project) / "DISABLED").exists()


# ---------------------------------------------------------------- 副檔名規則
# 第 6 題：只有改到程式碼才叫審查者。改投影片、教材、模型檔一律不觸發。
CODE_SUFFIXES = {
    ".py", ".pyw", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".vue", ".svelte",
    ".ps1", ".psm1", ".bat", ".cmd", ".sh",
    ".json", ".toml", ".yaml", ".yml", ".ini", ".cfg",
    ".css", ".scss", ".less", ".html", ".htm",
    ".sql", ".rs", ".go", ".java", ".c", ".h", ".cpp", ".hpp", ".cs",
}

# 材料包只收文字。二進位與大資料檔一律不進去，不管大小（設計決定，非可調）。
NEVER_INCLUDE_SUFFIXES = {
    ".aedt", ".aedtz", ".aedb", ".s2p", ".s4p", ".s8p", ".snp",
    ".pptx", ".docx", ".xlsx", ".pdf", ".zip", ".7z", ".exe", ".dll", ".pyd",
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".svg", ".mp4", ".webm",
    ".db", ".sqlite", ".pkl", ".npy", ".npz", ".bin",
}

# 這些目錄底下的東西一律不算「使用者改了程式碼」。
IGNORED_PARTS = {
    "node_modules", ".git", ".venv", "venv", "__pycache__", "dist", "build",
    ".next", ".vite", ".pytest_cache", ".mypy_cache", "site-packages",
}


# 點開頭但確實是程式碼／設定的目錄。原本一律排除所有點開頭目錄，
# 代價是 CI 設定、devcontainer、編輯器設定全部靜默漏審——
# 而 CI 設定改壞的後果通常比一般程式碼更嚴重。
ALLOWED_DOT_DIRS = {
    ".github", ".gitlab", ".circleci", ".azure", ".devcontainer", ".vscode",
}


def is_code_file(path: str) -> bool:
    p = Path(path)
    if any(part in IGNORED_PARTS for part in p.parts):
        return False
    # 點開頭的目錄預設排除：.git、.venv、.vite、.pytest_cache，
    # 以及最重要的 .claude —— 工具自己吐出來的 job-*.json、state.json、
    # config.json 都是 .json，不排掉的話工具會開始審查自己的輸出。
    # 第一次在真實專案上跑，6 個「改動檔案」裡有 4 個是這種東西。
    for part in p.parts[:-1]:
        if part.startswith(".") and part not in ALLOWED_DOT_DIRS:
            return False
    suffix = p.suffix.lower()
    if suffix in NEVER_INCLUDE_SUFFIXES:
        return False
    return suffix in CODE_SUFFIXES


def find_project_root(cwd: Path) -> Path:
    """從 hook 拿到的 cwd 往上找真正的專案根目錄。

    hook 輸入裡的 cwd 是「當下的工作目錄」，會跟著 shell 漂移。
    第一次在真實專案上跑就踩到了：那個 session 為了跑 npm 切進
    web_app\\frontend，Stop hook 於是把 frontend 當成專案，
    在裡面另外開了一整套 .claude\\review，用空設定跑了一次審查，
    還在使用者的版本庫裡留下一個未追蹤的目錄。

    依序找：git 根目錄 → 裝過 hook 的目錄 → 有 .claude 的目錄 → cwd。

    往上找一定要有界線。第一版沒有，測試立刻抓到：沒有任何標記的專案會
    一路往上走到 %USERPROFILE%——那裡永遠有 .claude——於是整個家目錄
    被當成專案，走目錄那一步會去掃使用者的全部檔案。比原本的 bug 還糟。
    界線是家目錄（不含）與磁碟機根目錄，另加深度上限。
    """
    # 兩邊都要 resolve。Windows 的 8.3 短檔名會讓字面比對失效：
    # Path.home() 給 C:\Users\longname，實際路徑卻可能是 C:\Users\LONGNA~1，
    # 家目錄的界線就形同虛設。
    try:
        cwd = Path(cwd).resolve()
    except Exception:
        cwd = Path(cwd)
    try:
        home = Path.home().resolve()
    except Exception:
        home = None

    chain = []
    for candidate in [cwd] + list(cwd.parents):
        if candidate == candidate.parent:      # 磁碟機根目錄
            break
        if home is not None and candidate == home:
            break                              # 家目錄本身不是任何人的專案
        chain.append(candidate)
        if len(chain) >= 8:
            break

    for marker in ((".git",), (".claude", "settings.json"), (".claude",)):
        for candidate in chain:
            if candidate.joinpath(*marker).exists():
                return candidate
    return cwd


def is_inside(child: str, parent: Path) -> bool:
    try:
        Path(child).resolve().relative_to(parent.resolve())
        return True
    except Exception:
        return False


# ---------------------------------------------------------------- 專案設定
# 什麼時候送審。實測每次審查約 11 萬 tokens、170～650 秒，
# 每一輪都審在小修時完全不划算——這是成本上最大的一個槓桿。
#   "auto"      有改動就攔阻，非審不可。交付前的收尾用。
#   "manual"    永不攔阻，只報累積量，使用者說了才送。
#   "threshold" 平常同 manual，累積超過門檻時自動送一次。
# 預設是 threshold 而不是 manual：純手動有一個很具體的失效方式——會忘記，
# 而「忘記送審」跟「審過了沒問題」在畫面上長得一模一樣。門檻是安全網，
# 只保證不會漏，不判斷值不值得。
TRIGGERS = ("auto", "manual", "threshold")

DEFAULT_CONFIG = {
    "enabled": True,
    "code_review": True,
    "visual_review": True,
    "trigger": "threshold",
    # 這個專案裡不屬於「我」的目錄（相對專案根目錄）。水位線只看 mtime，
    # 分不出是誰改的：同一個工作目錄裡有第二個代理人時，它的改動會被算成
    # 本回合的工作而送去審查——白燒額度，也等於把別人未完成的東西交出去。
    # 巢狀的版本庫／worktree 已經自動排除，這裡處理的是同一個版本庫裡
    # 由別人負責的目錄。
    "ignore_paths": [],
    "auto_when_files": 10,
    "auto_when_diff_bytes": 20_480,   # 20 KB
    # 材料包上限（第 14 題）。超過就截斷，並在報告開頭強制標明。
    "max_files": 40,
    "max_bytes": 200_000,
    # 視覺審查的畫面清單（第 20 題）。第一次啟用時由 launch.json 自動產生。
    "shots": [],
    # 一次最多送幾張圖給審查者。每個畫面若有基準圖就佔兩張，
    # 所以 8 大約等於 4 個畫面做視覺回歸。
    "max_images": 8,
    # 畫面網址的安全邊界。預設只允許本機 http(s)：畫面清單是專案控制的，
    # 而這個工具會在每個專案自動跑，包含從別處 clone 回來的版本庫。
    "allow_remote_urls": False,
    "allow_file_urls": False,
    # 審查那一趟要關掉的 Codex plugin。一個每輪自動觸發、沒有人盯著的
    # 背景程序不應該有能力接管滑鼠鍵盤或開使用者的 Chrome。
    "disable_codex_plugins": [
        "computer-use@openai-bundled",
        "browser@openai-bundled",
        "chrome@openai-bundled",
    ],
    "codex_timeout_sec": 900,
    # 超過這個大小、而且已經被 diff 涵蓋的檔案，只送 diff 不送全文。
    # diff 帶前後各 20 行，足以看懂一個函式；而一個 57.8 KB 的測試檔
    # 送全文會佔掉整份材料包的 58%。
    "full_content_max_bytes": 8000,
    # 審查者用哪個模型、多用力。留空＝沿用 ~/.codex/config.toml 的全域設定。
    # 值得知道的是預設不是最強的：gpt-5.6-luna 是「fast and affordable」那一階，
    # 同一代裡 gpt-5.6-sol 才是 frontier。重要的專案可以在這裡調上去。
    "codex_model": "",
    "codex_reasoning_effort": "",
}


def config_path(project: Path) -> Path:
    return review_dir(project) / "config.json"


# 這幾個鍵決定信任邊界，**專案設定不得碰**。
# 只能從使用者層級的 %USERPROFILE%\.claude\cross-review.json 設定。
#
# 上一輪我用「內建清單是地板」修好了 plugin 那條，卻在網址旗標上又犯同一個錯：
# allow_file_urls 與 allow_remote_urls 放在專案設定裡，等於讓被限制的一方
# 自己解除限制。預設拒絕如果可以被對方打開，那就不是邊界。
SECURITY_KEYS = {"allow_remote_urls", "allow_file_urls", "disable_codex_plugins"}


def user_settings_path() -> Path:
    return Path.home() / ".claude" / "cross-review.json"


def load_config(project: Path) -> dict:
    """讀設定。專案設定是資料，不是權限。

    這個工具會在每個專案自動執行，包含使用者從別處 clone 回來的版本庫，
    所以專案設定不得放寬安全邊界——只能收緊。
    """
    cfg = dict(DEFAULT_CONFIG)

    stored = read_json(config_path(project))
    if isinstance(stored, dict):
        cfg.update({k: v for k, v in stored.items() if k not in SECURITY_KEYS})

    user = read_json(user_settings_path())
    if isinstance(user, dict):
        cfg.update({k: user[k] for k in SECURITY_KEYS if k in user})

    # 內建的停用清單是地板，使用者層級只能往上加，不能拿掉。
    forced = set(DEFAULT_CONFIG["disable_codex_plugins"])
    given = cfg.get("disable_codex_plugins")
    if not isinstance(given, list):
        given = []
    cfg["disable_codex_plugins"] = sorted(forced | {str(x) for x in given})
    return cfg


def positive_int(cfg: dict, key: str, minimum: int = 1) -> int:
    """讀一個必須是正整數的設定值，壞掉就回退到內建預設。

    這些值會直接參與切片與算術。設定檔裡打成字串、負數或 null，
    背景審查就會例外結束、不寫 .done，然後被判定成「沒跑完」——
    使用者只會看到審查消失，不會知道是自己的設定檔打錯了。
    """
    default = int(DEFAULT_CONFIG.get(key, minimum))
    try:
        value = int(cfg.get(key, default))
    except (TypeError, ValueError):
        return default          # 根本不是數字：用有文件記載的預設值
    if value <= 0:
        return default          # 負數或零顯然是打錯了
    if value < minimum:
        # 低於下限就夾到下限，**不能退回預設**。
        # 預設遠大於下限（例如 max_bytes 預設 200,000、下限 1,000），
        # 退回預設等於「你要求更小，我給你大 200 倍的東西」——
        # 靜默給出比要求更大的值，永遠比給出更小的危險。
        return minimum
    return value


def ensure_config(project: Path) -> dict:
    """第一次進到一個專案時，產生一份可以直接跑的最小設定。

    git 排除每次都要確認，不能只在建立設定時做一次：已經裝過舊版的專案
    設定檔早就存在，直接 return 的話那些專案永遠補不上排除規則，
    `.claude/review/` 的產物會一直出現在 git status 裡。
    """
    ensure_git_excluded(project)

    path = config_path(project)
    if path.exists():
        return load_config(project)

    cfg = dict(DEFAULT_CONFIG)
    cfg["shots"] = default_shots(project)
    write_json(path, cfg)
    return cfg


def default_shots(project: Path) -> list:
    """從 .claude/launch.json 推出預設畫面清單（第 20 題）。

    launch.json 是 Claude Code 既有的慣例。抓不到就回空清單——
    沒有畫面就沒有視覺審查，而且會出聲，不會靜默跳過。
    """
    launch = read_json(project / ".claude" / "launch.json")
    if not isinstance(launch, dict):
        return []
    shots = []
    for conf in launch.get("configurations", []) or []:
        port = conf.get("port")
        if not port:
            continue
        name = str(conf.get("name") or f"port-{port}")
        # 後端 API 沒有畫面可拍，只拍前端。
        if any(k in name.lower() for k in ("backend", "api", "server")):
            continue
        url = conf.get("url") or f"http://localhost:{port}/"
        # actions 留空代表「載入頁面、拍一張」。要做互動就往裡面加步驟，例如：
        #   {"do": "shot",  "name": "初始"}
        #   {"do": "click", "selector": "#tab-stackup"}
        #   {"do": "wait",  "ms": 500}
        #   {"do": "type",  "selector": "#width", "text": "1.5"}
        #   {"do": "scroll","to": "bottom"}
        #   {"do": "shot",  "name": "疊構分頁"}
        shots.append({
            "name": name, "url": url, "width": 1440, "height": 900, "actions": [],
        })
    return shots


def ensure_git_excluded(project: Path) -> None:
    """有 git 的專案讓 git 忽略 .claude/review/。

    寫進 `.git/info/exclude`，不是 `.gitignore`。`.gitignore` 是追蹤中的檔案，
    動它等於在使用者的工作樹留下一筆跟他的需求無關的改動——第一次在真實
    專案上跑，審查者就把這件事列為發現。`.git/info/exclude` 效果一樣，
    但它屬於這個 clone、不進版控，工具因此完全不留痕跡。
    """
    if not (project / ".git").exists():
        return
    # 不能假設 .git 是目錄。linked worktree 與 submodule 的 .git 是一個檔案，
    # 裡面只寫著 "gitdir: <真正的路徑>"。實測時遇到的專案就是一個 worktree，
    # 原本的 is_dir() 檢查讓這裡直接 return，什麼都沒做也沒出聲。
    # info/exclude 讀的是 common dir，不是各 worktree 各自的 gitdir。
    try:
        proc = subprocess.run(
            ["git", "-C", str(project), "rev-parse", "--git-common-dir"],
            capture_output=True, timeout=20,
        )
        if proc.returncode != 0:
            return
        gitdir = Path(proc.stdout.decode("utf-8", "replace").strip())
        if not gitdir.is_absolute():
            gitdir = project / gitdir
    except Exception:
        return

    entry = ".claude/review/"
    try:
        info = gitdir / "info"
        info.mkdir(parents=True, exist_ok=True)
        path = info / "exclude"
        existing = read_text(path) if path.exists() else ""
        if entry in existing:
            return
        sep = "" if (not existing or existing.endswith("\n")) else "\n"
        write_text(path, existing + sep + "\n# cross-review 的報告與狀態\n" + entry + "\n")
    except Exception:
        pass


# ---------------------------------------------------------------- 狀態
def state_path(project: Path) -> Path:
    return review_dir(project) / "state.json"


def load_state(project: Path) -> dict:
    st = read_json(state_path(project))
    if not isinstance(st, dict):
        st = {}
    st.setdefault("cursor", 0)        # transcript 已處理到第幾行
    st.setdefault("round", 0)         # 回合編號
    st.setdefault("transcript", "")   # 游標對應的 transcript 檔
    st.setdefault("watermark", 0.0)   # 上一回合派工的時刻（epoch 秒）
    return st


def save_state(project: Path, st: dict) -> None:
    write_json(state_path(project), st)


# ---------------------------------------------------------------- 出聲
def log_error(project: Path, message: str) -> None:
    """第 7 題：失敗一律放行，但絕不靜默。

    寫之前先確認路徑沒有越界。errors.log 是用 append 開的，
    目標若是指向專案外的 symlink，就會直接追加到使用者的別的檔案上。
    """
    # 不能用「存在才檢查」：懸空的 errors.log 連結 exists() 是 False，
    # 會直接跳過檢查，然後 open(..., "a") 跟著連結寫到專案外。
    if not review_child_is_safe(project, "errors.log"):
        return
    line = "[" + time.strftime("%Y-%m-%d %H:%M:%S") + "] " + message + "\n"
    path = review_dir(project) / "errors.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8", newline="\n") as fh:
        fh.write(line)


# ---------------------------------------------------------------- codex.exe
def resolve_codex():
    """不要寫死路徑。

    codex.exe 的目錄含 build hash（.../bin/一串十六進位/codex.exe），
    Codex 一更新就換掉。寫死的話某天會變成每輪靜默失敗。
    """
    override = os.environ.get("CROSS_REVIEW_CODEX")
    if override and Path(override).exists():
        return override

    local = os.environ.get("LOCALAPPDATA", "")
    pattern = os.path.join(local, "OpenAI", "Codex", "bin", "*", "codex.exe")
    found = glob.glob(pattern)
    if not found:
        return None
    found.sort(key=os.path.getmtime, reverse=True)
    return found[0]


def run_codex(project: Path, prompt: str, schema_file: Path, out_file: Path,
              cfg: dict, images=None, require=None):
    """跑一趟審查。回傳 (dict, 錯誤訊息)。

    **失敗時第一個回傳值不是 None，而是一份只有 `_` 開頭事實欄位的 dict**
    （`_tokens`、`_elapsed_sec`、`_failed`），因為失敗那一趟也燒了額度：
    實際發生過的額度錯誤訊息裡就寫著 `tokens used / 35,477`，那 3.5 萬個
    token 是真的花掉了，只記成功的話，帳本會在最需要分析額度的時候少算。
    呼叫端一律先看第二個回傳值判斷成功與否，不要拿 dict 是不是 None 來判斷。

    永遠是唯讀沙箱：審查者不得改任何檔案。
    """
    codex = resolve_codex()
    if not codex:
        return {}, "找不到 codex.exe（%LOCALAPPDATA%\\OpenAI\\Codex\\bin\\*\\codex.exe）"

    if out_file.exists():
        out_file.unlink()

    cmd = [codex, "exec"]
    # --image 吃多個值（<FILE>...），所以它後面一定要接另一個旗標，
    # 否則 prompt 的位置引數會被它吸進去。這裡乾脆把 prompt 走 stdin，
    # 完全不用位置引數——順便避開 Windows 命令列 8191 字元的上限。
    for img in images or []:
        cmd += ["-i", str(img)]
    cmd += [
        "-C", str(project),
        "-s", "read-only",
        "--skip-git-repo-check",
        "--output-schema", str(schema_file),
        "-o", str(out_file),
    ]
    for name in cfg.get("disable_codex_plugins", []):
        cmd += ["-c", 'plugins."' + name + '".enabled=false']
    if cfg.get("codex_model"):
        cmd += ["-m", str(cfg["codex_model"])]
    if cfg.get("codex_reasoning_effort"):
        cmd += ["-c", 'model_reasoning_effort="' + str(cfg["codex_reasoning_effort"]) + '"']

    timeout = positive_int(cfg, "codex_timeout_sec", 30)
    started = time.time()
    try:
        proc = subprocess.run(
            cmd, input=prompt.encode("utf-8"), capture_output=True, timeout=timeout
        )
    except subprocess.TimeoutExpired:
        # 逾時看不到輸出，拿不到 token 數；秒數還是要記，那是實際等掉的時間。
        return {"_elapsed_sec": round(float(timeout), 1), "_failed": True}, \
            "codex 逾時（" + str(timeout) + " 秒）"
    elapsed = time.time() - started

    tail = proc.stderr.decode("utf-8", "replace").strip().splitlines()[-3:]
    # 標頭（model、reasoning effort、tokens used）寫在 stderr，不是 stdout。
    facts = parse_run_facts(
        proc.stdout.decode("utf-8", "replace")
        + "\n"
        + proc.stderr.decode("utf-8", "replace"))
    facts["_elapsed_sec"] = round(elapsed, 1)
    facts["_failed"] = True
    if not out_file.exists():
        return facts, (
            "codex 結束（exit " + str(proc.returncode) + "，"
            + format(elapsed, ".0f") + " 秒）但沒有輸出：" + " / ".join(tail)
        )
    # 非零離開碼即使留下了合法 JSON 也不算成功。原本只檢查檔案存不存在、
    # 解不解得開，等於把中途失敗的殘留輸出當成一份完整審查。
    if proc.returncode != 0:
        return facts, (
            "codex 以 exit " + str(proc.returncode) + " 結束（"
            + format(elapsed, ".0f") + " 秒），輸出不採信：" + " / ".join(tail)
        )

    try:
        data = json.loads(read_text(out_file))
    except Exception as exc:
        return facts, "codex 的輸出不是合法 JSON：" + str(exc)

    if not isinstance(data, dict):
        return facts, "codex 的輸出不是物件"

    # --output-schema 理論上由伺服器端保證結構，但「合法 JSON 卻少欄位」
    # 會被渲染成一份空白報告，然後標記成功完成——又是假完成。
    for key, kind in (require or {}).items():
        if key not in data:
            return facts, "codex 的輸出缺少必要欄位 " + key
        if not isinstance(data[key], kind):
            return facts, ("codex 的輸出欄位 " + key + " 型別不對（預期 "
                          + getattr(kind, "__name__", str(kind)) + "）")

    # facts 在上面就算好了（只解析 stdout 的話用量那一行永遠只印得出秒數，
    # 而單元測試會過，因為餵給它的是我自己打的字串，不是真實輸出）。
    data.update(facts)
    data["_failed"] = False
    return data, ""


def parse_run_facts(stdout: str) -> dict:
    """從 codex 的標頭與結尾撈出這一趟到底用了什麼、花了多少。

    模型與努力程度只出現在純文字標頭（--json 模式反而沒有），
    token 總數出現在最後一行。兩者都要，所以維持純文字模式。
    """
    facts = {"_model": "", "_effort": "", "_tokens": 0}
    m = re.search(r"^model:\s*(\S+)", stdout, re.M)
    if m:
        facts["_model"] = m.group(1)
    m = re.search(r"^reasoning effort:\s*(\S+)", stdout, re.M)
    if m:
        facts["_effort"] = m.group(1)
    m = re.search(r"tokens used[\s:]*([\d,]+)", stdout)
    if m:
        try:
            facts["_tokens"] = int(m.group(1).replace(",", ""))
        except ValueError:
            pass
    return facts


def usage_line(data: dict) -> str:
    """報告開頭那一行：這一趟用了什麼模型、多用力、多久、多少 token。"""
    bits = []
    if data.get("_model"):
        bits.append(str(data["_model"]))
    if data.get("_effort"):
        bits.append("effort=" + str(data["_effort"]))
    bits.append(str(data.get("_elapsed_sec", "?")) + " 秒")
    if data.get("_tokens"):
        bits.append(format(int(data["_tokens"]), ",") + " tokens")
    return " · ".join(bits)
