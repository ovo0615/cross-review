"""截圖、互動與 DOM 文字擷取。

ADR-0004：只在 dev server 已經開著時拍，絕不自己啟動應用程式。
ADR-0003：除了截圖還要附 DOM 文字——實測顯示審查者光看圖判斷不出
          「文字被截斷」，因為那個資訊在圖片裡並不存在。
ADR-0006：全部走 CDP。一個 Chrome 實例跑完所有畫面，而且截圖與 DOM 文字
          來自同一次渲染。互動是設定描述的，不是程式。
"""
from __future__ import annotations

import os
import re
import shutil
import socket
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

from . import common
from .cdp import Browser

CHROME_CANDIDATES = [
    r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
    r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe",
    r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe",
]


def resolve_chrome():
    override = os.environ.get("CROSS_REVIEW_CHROME")
    if override and Path(override).exists():
        return override
    for candidate in CHROME_CANDIDATES:
        path = os.path.expandvars(candidate)
        if Path(path).exists():
            return path
    return None


LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "[::1]", "0.0.0.0"}


def url_is_allowed(url: str, cfg: dict) -> str:
    """回傳空字串代表這個網址可以拍；否則回傳拒絕的理由。

    畫面清單的 url 來自專案的 config.json 或 launch.json——也就是說，
    它是「專案」控制的，而這個工具會在每個專案自動執行，包含從別處
    clone 回來的版本庫。若不設限，一個惡意專案只要寫一行設定就能讓工具
    去讀 file:///C:/Users/... 或探測內網服務，並把 DOM 文字與截圖
    送進 Codex。預設只允許本機 http(s)。
    """
    parsed = urlparse(url)
    scheme = (parsed.scheme or "").lower()

    if scheme == "file":
        if not cfg.get("allow_file_urls", False):
            return "拒絕 file:// 網址（要允許請在設定裡把 allow_file_urls 設為 true）"
        return "" if Path(parsed.path.lstrip("/")).exists() else "檔案不存在"

    if scheme not in ("http", "https"):
        return "只接受 http(s) 網址，收到的是 " + (scheme or "（沒有 scheme）")

    host = (parsed.hostname or "").lower()
    if host not in LOCAL_HOSTS and not cfg.get("allow_remote_urls", False):
        return ("拒絕非本機網址 " + host
                + "（要允許請在設定裡把 allow_remote_urls 設為 true）")
    return ""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """不跟隨轉址。

    白名單只驗證原始 URL 是不夠的：允許的 localhost 頁面可以回一個 302
    指向外部網站，而 urllib 與 Chrome 預設都會跟過去，
    於是非預期頁面的 DOM 與截圖照樣被送進 Codex，白名單形同虛設。
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(
            newurl, code, "redirect:" + str(newurl), headers, fp)


_OPENER = urllib.request.build_opener(_NoRedirect)


def page_is_ready(url: str, timeout: float = 5.0) -> str:
    """回傳空字串代表可以拍；否則回傳不能拍的原因。

    只檢查 TCP port 有沒有人聽是不夠的：port 可能被別的服務占用，
    或 app 正在回 500——那樣會把錯誤頁當成正常畫面送去審查，
    而審查者不會知道它看的是錯誤頁。
    """
    parsed = urlparse(url)
    if parsed.scheme == "file":
        return "" if Path(parsed.path.lstrip("/")).exists() else "檔案不存在"

    host = parsed.hostname or "localhost"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        with socket.create_connection((host, port), timeout=1.5):
            pass
    except OSError:
        return "沒有人在聽 " + host + ":" + str(port) + "（ADR-0004：工具不自己啟動 app）"

    request = urllib.request.Request(url, headers={"User-Agent": "cross-review"})
    try:
        with _OPENER.open(request, timeout=timeout) as resp:
            status = getattr(resp, "status", None) or resp.getcode()
    except urllib.error.HTTPError as exc:
        if str(exc.reason or "").startswith("redirect:"):
            return ("這個網址會轉址到 " + str(exc.reason)[9:]
                    + "，拒絕跟隨（白名單只認原始網址，轉址等於繞過）")
        return "HTTP " + str(exc.code) + "，這不是正常畫面"
    except Exception as exc:
        return "連得上 port 但取不到頁面：" + str(exc)
    if status and int(status) >= 400:
        return "HTTP " + str(status) + "，這不是正常畫面"
    return ""


def safe_name(text: str) -> str:
    """只換掉檔名不能用的字元，其餘一律保留——中文也保留。

    原本是白名單 `[^A-Za-z0-9_.-]`，結果純中文的畫面名稱被整串換成底線，
    只剩長度可以區分。「串接電路」與「截面阻抗」都是四個字，會產生同一個檔名，
    後拍的直接蓋掉先拍的——基準圖被蓋掉，視覺回歸從此比錯對象，而且無聲無息。
    """
    cleaned = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", text)
    cleaned = re.sub(r"\s+", "_", cleaned).strip("._")
    return cleaned or "shot"


def unique_base(base: str, taken: set) -> str:
    """同名時加序號。中文檔名之後，撞名應該幾乎不會發生，但不留這個洞。"""
    if base not in taken:
        taken.add(base)
        return base
    for i in range(2, 100):
        candidate = base + "-" + str(i)
        if candidate not in taken:
            taken.add(candidate)
            return candidate
    taken.add(base)
    return base


# ---------------------------------------------------------------- 互動
def apply_action(browser: Browser, action: dict) -> str:
    """執行一個設定描述的動作。回傳錯誤訊息，空字串代表成功。

    動作是資料不是程式：使用者在 config.json 裡描述要點什麼、填什麼，
    不需要寫任何一行程式碼。
    """
    kind = str(action.get("do") or "").lower()
    selector = action.get("selector")
    label = action.get("text") if kind == "click" else None

    try:
        if kind == "click":
            # 用文字點擊比 CSS 選擇器穩健得多。真實專案常常沒有 id 也沒有
            # data-testid，只剩一堆同名 class（例如 11 個 .viewtab 分頁按鈕），
            # 那時 nth-of-type 會在分頁順序一改就默默錯位。
            if label:
                # 精確比對優先。退回 startsWith 時若有多個候選就拒絕動作——
                # 同時存在「Run」與「Runtime」時亂點一個而不報錯，
                # 會讓後面的截圖拍到完全不相干的畫面而沒有人發現。
                found = browser.eval(
                    "(() => {"
                    " const t = " + repr(str(label)) + ";"
                    " const els = [...document.querySelectorAll("
                    "   'button, a, [role=\"tab\"], [role=\"button\"], label, summary')];"
                    " const exact = els.filter(e => (e.textContent||'').trim() === t);"
                    " if (exact.length) { exact[0].click(); return 'ok'; }"
                    " const pre = els.filter(e => (e.textContent||'').trim().startsWith(t));"
                    " if (pre.length === 1) { pre[0].click(); return 'ok'; }"
                    " if (pre.length > 1) return 'ambiguous:' + pre.length;"
                    " return 'missing'; })()"
                )
                if found != "ok":
                    if str(found).startswith("ambiguous"):
                        return ("「" + str(label) + "」對到 "
                                + str(found).split(":")[1] + " 個元素，不確定要點哪一個"
                                "（請把文字寫完整，或改用 selector）")
                    return "找不到文字是「" + str(label) + "」的可點元素"
            else:
                found = browser.eval(
                    "(() => { const el = document.querySelector(" + repr(selector) + ");"
                    " if (!el) return false; el.click(); return true; })()"
                )
                if not found:
                    return "找不到元素 " + str(selector)
        elif kind == "type":
            text = str(action.get("text", ""))
            # React／Vue 的受控輸入元件會覆寫 value 的 setter 並記住自己的狀態。
            # 直接指定 el.value 只改了 DOM，框架不知道，畫面可能完全沒反應，
            # 而工具照樣截圖並當成成功。這裡改用原生 setter 再派事件，
            # 這是受控元件唯一吃得到的寫法。使用者的專案就是 React。
            found = browser.eval(
                "(() => { const el = document.querySelector(" + repr(selector) + ");"
                " if (!el) return false;"
                " const proto = el instanceof HTMLTextAreaElement"
                "   ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;"
                " const setter = Object.getOwnPropertyDescriptor(proto, 'value');"
                " el.focus();"
                " if (setter && setter.set) setter.set.call(el, " + repr(text) + ");"
                " else el.value = " + repr(text) + ";"
                " el.dispatchEvent(new Event('input', {bubbles:true}));"
                " el.dispatchEvent(new Event('change', {bubbles:true}));"
                " return el.value === " + repr(text) + "; })()"
            )
            if found is False:
                return "找不到元素 " + str(selector) + "，或填入後的值跟預期不符"
        elif kind == "scroll":
            where = str(action.get("to", "bottom")).lower()
            if where == "bottom":
                browser.eval("window.scrollTo(0, document.body.scrollHeight)")
            elif where == "top":
                browser.eval("window.scrollTo(0, 0)")
            elif selector:
                browser.eval(
                    "(() => { const el = document.querySelector(" + repr(selector) + ");"
                    " if (el) el.scrollIntoView({block:'center'}); })()"
                )
        elif kind == "wait":
            import time as _t
            _t.sleep(min(float(action.get("ms", 500)) / 1000.0, 10.0))
        elif kind == "shot":
            return ""   # 由呼叫端處理
        else:
            return "不認得的動作：" + kind
    except Exception as exc:
        return kind + " 執行失敗：" + str(exc)
    return ""


def run_shot(browser: Browser, shot: dict, current_dir: Path, baseline_dir: Path,
             taken: set, cfg: dict) -> tuple:
    """跑完一個畫面的所有步驟，回傳 (擷取清單, 錯誤清單)。"""
    name = str(shot.get("name") or "shot")
    url = str(shot.get("url"))
    actions = shot.get("actions") or []
    captures, errors = [], []

    browser.goto(url)

    # Chrome 也會跟隨轉址。載入完之後確認我們還在允許的位置，
    # 否則接下來拍的就是別人的頁面。
    try:
        landed = str(browser.eval("location.href") or "")
    except Exception:
        landed = ""
    if landed:
        denied = url_is_allowed(landed, cfg)
        if denied:
            return [], [name + "：載入後停在 " + landed + " — " + denied]

    # 沒有描述任何動作時，就是單純拍一張。
    steps = actions if any(a.get("do") == "shot" for a in actions) else \
        list(actions) + [{"do": "shot", "name": "預設"}]

    for action in steps:
        if action.get("do") == "shot":
            step = str(action.get("name") or "預設")
            base = unique_base(safe_name(name) + "__" + safe_name(step), taken)
            png = current_dir / (base + ".png")
            baseline = baseline_dir / (base + ".png")

            had_baseline = False
            if png.exists():
                shutil.copy2(png, baseline)
                had_baseline = True
            elif baseline.exists():
                had_baseline = True

            try:
                browser.screenshot(png)
                dom_text = browser.visible_text()
            except Exception as exc:
                errors.append(name + " / " + step + "：截圖失敗 " + str(exc))
                continue

            captures.append({
                "name": name + " / " + step,
                "url": url,
                "viewport": str(shot.get("width") or 1440) + "x" + str(shot.get("height") or 900),
                "png": str(png.resolve()),
                "baseline": str(baseline.resolve()) if had_baseline and baseline.exists() else "",
                "dom_text": dom_text,
            })
        else:
            err = apply_action(browser, action)
            if err:
                errors.append(name + "：" + err)

    return captures, errors


def collect(project: Path, cfg: dict) -> dict:
    """依畫面清單拍一輪。一個 Chrome 實例跑完全部。"""
    out = {"shots": [], "skipped": [], "errors": []}
    shot_list = cfg.get("shots") or []
    if not shot_list:
        out["skipped"].append(
            "畫面清單是空的。若這個專案有 GUI，請編輯 .claude/review/config.json 的 shots。"
        )
        return out

    chrome = resolve_chrome()
    if not chrome:
        out["errors"].append("找不到 chrome.exe，無法截圖")
        return out

    current_dir = common.review_dir(project) / "shots"
    baseline_dir = common.review_dir(project) / "baseline"
    current_dir.mkdir(parents=True, exist_ok=True)
    baseline_dir.mkdir(parents=True, exist_ok=True)

    runnable = []
    for shot in shot_list:
        name = str(shot.get("name") or "shot")
        url = str(shot.get("url") or "")
        if not url:
            out["errors"].append(name + "：沒有 url")
            continue
        denied = url_is_allowed(url, cfg)
        if denied:
            # 這是安全拒絕，不是「這輪剛好沒開」，所以算錯誤要出聲。
            out["errors"].append(name + "：" + url + " — " + denied)
            continue
        reason = page_is_ready(url)
        if reason:
            out["skipped"].append(name + "：" + url + " — " + reason)
            continue
        runnable.append(shot)

    if not runnable:
        return out

    # 視窗尺寸是行程層級的，所以依尺寸分組，每組開一次 Chrome。
    groups = {}
    for shot in runnable:
        key = (int(shot.get("width") or 1440), int(shot.get("height") or 900))
        groups.setdefault(key, []).append(shot)

    taken = set()   # 跨所有畫面共用，避免不同群組之間撞名
    for (width, height), group in groups.items():
        browser = None
        try:
            browser = Browser(chrome, width, height)
            for shot in group:
                # 每個畫面各自包起來。原本只有群組層級的 try，
                # 一個網址導覽失敗就會中止同尺寸的其餘畫面，
                # 大量畫面靜默沒拍到而報告只提一行錯誤。
                try:
                    captures, errors = run_shot(
                        browser, shot, current_dir, baseline_dir, taken, cfg)
                except Exception as exc:
                    out["errors"].append(
                        str(shot.get("name") or "shot") + "：這個畫面失敗了 " + str(exc))
                    continue
                out["shots"].extend(captures)
                out["errors"].extend(errors)
        except Exception as exc:
            out["errors"].append(
                str(width) + "x" + str(height) + " 這組畫面失敗：" + str(exc))
        finally:
            if browser:
                browser.close()

    return out
