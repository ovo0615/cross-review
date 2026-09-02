"""極小的 Chrome DevTools Protocol 用戶端，零相依。

只為了一件事：在畫面上做幾個設定描述的動作（點、輸入、捲動、等待），
然後拍下當時的畫面。一次性的 `chrome --screenshot` 做不到這件事，
因為它每次都是全新的行程，狀態不會留下來。

Python 標準函式庫沒有 WebSocket，而 CDP 只能走 WebSocket，
所以這裡自己實作了用戶端需要的那一小塊 RFC 6455：
握手、送遮罩過的文字訊框、收（可能很大、可能分段的）訊框。
螢幕截圖回來是 base64，動輒好幾 MB，所以 64 位元長度與延續訊框必須處理。

user-data-dir 刻意放在系統暫存目錄而不是專案的 .claude 底下：
使用者既有的 stop-hook.ps1 會殺掉指令列同時含 --headless 與 claude 的
Chrome 行程，放在 .claude 底下會被它掃到。
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlparse


class WebSocket:
    """只支援用戶端、只支援文字訊框、只走 ws://（CDP 在本機不用 TLS）。"""

    def __init__(self, url: str, timeout: float = 30.0):
        parsed = urlparse(url)
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or 80
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query

        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.sock.settimeout(timeout)
        self._buf = b""
        try:
            self._handshake(host, port, path)
        except BaseException:
            # 握手失敗時 self.ws 還沒被指派，Browser.close() 關不到這個 socket，
            # 只能等垃圾回收——反覆失敗就會暫時累積描述元。自己收乾淨。
            try:
                self.sock.close()
            except Exception:
                pass
            raise

    def _handshake(self, host: str, port: int, path: str) -> None:
        key = base64.b64encode(os.urandom(16)).decode()
        request = (
            "GET " + path + " HTTP/1.1\r\n"
            "Host: " + host + ":" + str(port) + "\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            "Sec-WebSocket-Key: " + key + "\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        self.sock.sendall(request.encode())

        header = b""
        while b"\r\n\r\n" not in header:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("CDP 握手時連線被關閉")
            header += chunk
        head, _, rest = header.partition(b"\r\n\r\n")
        if b"101" not in head.split(b"\r\n")[0]:
            raise ConnectionError("CDP 握手失敗：" + head.split(b"\r\n")[0].decode("latin-1"))
        self._buf = rest

    # -------------------------------------------------- 低階讀寫
    def _recv_exact(self, n: int) -> bytes:
        while len(self._buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("CDP 連線中斷")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        header = bytearray([0x80 | opcode])
        length = len(payload)
        if length < 126:
            header.append(0x80 | length)
        elif length < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", length)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", length)
        mask = os.urandom(4)
        header += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(bytes(header) + masked)

    def _read_frame(self):
        b1, b2 = self._recv_exact(2)
        fin = bool(b1 & 0x80)
        opcode = b1 & 0x0F
        masked = bool(b2 & 0x80)
        length = b2 & 0x7F
        if length == 126:
            length = struct.unpack(">H", self._recv_exact(2))[0]
        elif length == 127:
            length = struct.unpack(">Q", self._recv_exact(8))[0]
        mask = self._recv_exact(4) if masked else None
        payload = self._recv_exact(length) if length else b""
        if mask:
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        return fin, opcode, payload

    def send(self, text: str) -> None:
        self._send_frame(0x1, text.encode("utf-8"))

    def recv(self) -> str:
        """回傳一則完整訊息，自動把延續訊框接起來，並回應 ping。"""
        chunks = []
        while True:
            fin, opcode, payload = self._read_frame()
            if opcode == 0x8:                      # close
                raise ConnectionError("CDP 連線被對方關閉")
            if opcode == 0x9:                      # ping
                self._send_frame(0xA, payload)
                continue
            if opcode == 0xA:                      # pong
                continue
            chunks.append(payload)
            if fin:
                break
        return b"".join(chunks).decode("utf-8", "replace")

    def close(self) -> None:
        try:
            self._send_frame(0x8, b"")
        except Exception:
            pass
        try:
            self.sock.close()
        except Exception:
            pass


# 「本機」的唯一定義。網址白名單與 Chrome 的 proxy bypass 都從這裡導出——
# 兩套手寫清單一定會不同步：先前 0.0.0.0 通過了白名單，實際導覽卻被送去
# 失效的 proxy，截圖直接失敗。
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "[::1]", "0.0.0.0"}


def _bypass_list() -> str:
    """給 --proxy-bypass-list 用。IPv6 要用中括號的寫法。"""
    entries = []
    for host in sorted(LOCAL_HOSTS):
        entries.append("[::1]" if host == "::1" else host)
    return ";".join(dict.fromkeys(entries))


def _resolver_rules() -> str:
    """給 --host-resolver-rules 用。

    `MAP *` 會把 **IP 字面值也一起** 映射掉，所以每一個本機主機都要
    個別 EXCLUDE。先前只寫了 EXCLUDE localhost，結果 localhost:埠 能用、
    127.0.0.1:埠 直接連不上——而白名單明明兩個都接受。
    政策允許了實作做不到的事，而且因為測試一直用 localhost 所以沒現形。
    """
    excludes = []
    for host in sorted(LOCAL_HOSTS):
        excludes.append("EXCLUDE " + host.strip("[]"))
    return "MAP * ~NOTFOUND, " + ", ".join(dict.fromkeys(excludes))


# 對本機除錯埠的請求絕不能走系統 proxy。
_NO_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _is_local_ws(url: str) -> bool:
    """CDP 的 WebSocket 端點必須落在本機。

    webSocketDebuggerUrl 是「別人給的」——正常情況下是本機 Chrome，
    但只要那一段 HTTP 被 proxy 或異常回應影響，它就可能指向外部。
    連上去之後我們會把整個瀏覽器交給對方指揮，所以連線前必須確認落點。
    """
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    if parsed.scheme not in ("ws", "wss"):
        return False
    host = (parsed.hostname or "").lower()
    return host in LOCAL_HOSTS


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Browser:
    """一個 headless Chrome 行程加一個分頁，用完就收乾淨。"""

    def __init__(self, chrome: str, width: int, height: int, timeout: float = 30.0,
                 local_only: bool = True):
        self.port = _free_port()
        self.profile = Path(tempfile.mkdtemp(prefix="xrv-chrome-"))
        self.timeout = timeout

        args = [
            chrome,
            "--headless=new",
            "--disable-gpu",
            "--hide-scrollbars",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-extensions",
            "--remote-debugging-port=" + str(self.port),
            "--user-data-dir=" + str(self.profile),
            "--window-size=" + str(width) + "," + str(height),
        ]
        if local_only:
            # 載入之後再檢查 location.href 是來不及的——請求早就發出去了。
            # 頁面用 JavaScript 或 meta refresh 轉向外部時，事後拒絕擋不住
            # 那一次外連。所以要在行程層級先斷掉對外的路：
            #   1. 所有主機名稱解析失敗，localhost 例外（擋掉用網域的外連）
            #   2. 非本機的流量一律走一個沒有人在聽的 proxy（連 IP 直連也擋掉）
            args += [
                "--host-resolver-rules=" + _resolver_rules(),
                "--proxy-server=http://127.0.0.1:1",
                "--proxy-bypass-list=" + _bypass_list(),
            ]
        args.append("about:blank")

        self.proc = None
        self.ws = None
        self._id = 0
        self._events = []
        try:
            self.proc = subprocess.Popen(
                args,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            self._connect()
            self._force_viewport(width, height)
        except BaseException:
            # 建構子拋例外時 close() 永遠不會被呼叫，呼叫端的 finally 也
            # 因為變數還沒被賦值而清不掉——Chrome 行程與暫存 profile 就留下來了。
            # 重試幾次就會累積成一堆孤兒程序。這裡自己收乾淨再把例外丟出去。
            self.close()
            raise

    def _endpoint(self, path: str, timeout: float):
        """問 Chrome 的除錯埠。

        一定要用不走 proxy 的 opener。全域的 urlopen 會沿用系統的
        HTTP_PROXY／HTTPS_PROXY，沒排除 127.0.0.1 的話，這個對本機除錯埠的
        請求會被送去外部 proxy——而它回傳的 webSocketDebuggerUrl
        接下來會被拿去連線，等於讓外部決定我們的 CDP 連到哪裡。
        （page_is_ready 那邊剛修好同一個問題，這裡漏了。）
        """
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            try:
                url = "http://127.0.0.1:" + str(self.port) + path
                with _NO_PROXY.open(url, timeout=2) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except Exception as exc:
                last = exc
                time.sleep(0.15)
        raise ConnectionError("連不上 Chrome 的除錯埠：" + repr(last))

    def _connect(self) -> None:
        targets = self._endpoint("/json/list", self.timeout)
        page = next((t for t in targets if t.get("type") == "page"), None)
        if not page:
            raise ConnectionError("Chrome 沒有可用的分頁")
        ws_url = str(page.get("webSocketDebuggerUrl") or "")
        if not _is_local_ws(ws_url):
            raise ConnectionError(
                "Chrome 回報的除錯 WebSocket 不在本機，拒絕連線：" + ws_url)
        self.ws = WebSocket(ws_url, timeout=self.timeout)

    # 等指令回應時收到的事件先存這裡，不能丟掉。
    # 丟掉的話會有一個真實的競爭：Page.loadEventFired 只要比 Page.navigate
    # 的回應早到，就永遠消失，goto() 接著等一個已經過去的事件，
    # 一路等到 socket 逾時才拋例外——原本載入成功的畫面被判成審查失敗。
    MAX_QUEUED_EVENTS = 200

    def _queue_event(self, data: dict) -> None:
        if "method" not in data:
            return                      # 別的指令的回應，跟我們無關
        self._events.append(data)
        if len(self._events) > self.MAX_QUEUED_EVENTS:
            del self._events[0]         # 頁面狂噴事件時不要無限成長

    def _force_viewport(self, width: int, height: int) -> None:
        """用 CDP 明確指定視埠，不要依賴 --window-size。

        `--window-size` 給的是**視窗**大小，實際的可視區還要扣掉瀏覽器自己的
        東西。實測：加上網路隔離旗標之後，Chrome 多出一條提示列，
        可視高度從 804 掉到 748——同一份設定、同一個 --window-size，
        截圖卻小了 56 px，而視覺回歸整個功能的前提就是尺寸穩定。
        （這是視覺審查在工具自己身上抓到的：它報告「所有截圖比基準少 56 px」。）

        setDeviceMetricsOverride 直接規定可視區，瀏覽器有沒有提示列、
        有沒有捲軸都不影響，截圖尺寸完全等於設定值。
        """
        self.call("Emulation.setDeviceMetricsOverride", {
            "width": int(width),
            "height": int(height),
            "deviceScaleFactor": 1,
            "mobile": False,
        })

    def call(self, method: str, params: dict = None, timeout: float = None):
        """送一個 CDP 指令，等它自己的回應。

        沿路收到的事件會**存進佇列**（不是丟掉），讓 goto() 之類的等待者
        事後查得到。先前這裡直接 continue，那正是上面那個競爭的來源。
        """
        self._id += 1
        msg_id = self._id
        self.ws.send(json.dumps({"id": msg_id, "method": method, "params": params or {}}))
        deadline = time.time() + (timeout or self.timeout)
        while time.time() < deadline:
            try:
                data = json.loads(self.ws.recv())
            except socket.timeout:
                continue                # 由外層的 deadline 決定要不要放棄
            if data.get("id") != msg_id:
                self._queue_event(data)
                continue
            if "error" in data:
                raise RuntimeError(method + " 失敗：" + json.dumps(data["error"], ensure_ascii=False))
            return data.get("result", {})
        raise TimeoutError(method + " 逾時")

    # -------------------------------------------------- 高階操作
    @staticmethod
    def _is_this_navigation(event: dict, loader_id: str, frame_id: str,
                            lifecycle_on: bool = True) -> bool:
        """這個事件是不是「這一次」導航完成的證據。

        只清空佇列是不夠的：上一頁的停止事件若在清空之後、navigate 回應
        之前才抵達，一樣會被誤認成新頁載入完成，然後拍到還沒載入完的畫面。

        **只有 loaderId 能唯一識別一次導航。**

        先前這裡也接受 frameId 相符的 Page.frameStoppedLoading，那是錯的：
        frameId 識別的是 frame，不是單次導航——主 frame 在前後頁導航時
        通常還是同一個 frameId。所以上一頁的停止事件會跟新導航「相符」，
        競爭根本沒被封住。（而我的測試用 F-OLD 對 F-NEW 來模擬，
        等於假設前後導航會有不同 frameId——那個假設本身就是錯的，
        測試因此把有 bug 的行為固定成了預期行為。）

        降級的條件是「**lifecycle 有沒有 enable 成功**」，不是「有沒有 loaderId」。
        這兩件事無關：setLifecycleEventsEnabled 失敗時，Page.navigate 通常
        還是會回傳 loaderId。上一版把降級綁在 loaderId 上，結果在不支援
        lifecycle 的環境下，所有事件都被拒絕——**每個畫面都卡滿 30 秒逾時**。
        """
        method = event.get("method")
        params = event.get("params") or {}

        if method == "Page.lifecycleEvent":
            if params.get("name") not in ("load", "networkIdle"):
                return False
            return not loader_id or params.get("loaderId") == loader_id

        if lifecycle_on:
            return False        # 有可靠證據可等，就不用關聯不上的訊號

        # 以下是降級路徑：沒有 loaderId 可用時才會走到。
        if method == "Page.frameStoppedLoading":
            return not frame_id or params.get("frameId") == frame_id
        if method == "Page.loadEventFired":
            return True
        return False

    def goto(self, url: str, settle_ms: int = 1200) -> None:
        """導覽到 url，等**這一次**導航的載入事件，然後再靜置一小段時間。

        等不到載入事件時真的會靠 settle 兜底——先前的註解這樣寫，
        但程式碼做不到：ws.recv() 逾時會拋例外，根本走不到那行 sleep。
        現在 socket 逾時被捕捉，行為與說明一致。
        """
        self.call("Page.enable")
        # lifecycleEvent 是唯一帶 loaderId 的載入事件。enable 失敗不致命，
        # 但一定要記住失敗了——降級的條件是這個，不是「有沒有 loaderId」。
        lifecycle_on = True
        try:
            self.call("Page.setLifecycleEventsEnabled", {"enabled": True})
        except Exception:
            lifecycle_on = False

        # 佇列在 navigate 之前清掉，擋掉「清空之前」的殘留；
        # 「清空之後」才抵達的殘留由 loaderId 關聯擋掉。
        self._events.clear()
        result = self.call("Page.navigate", {"url": url})

        # 導航直接失敗就不要等了。不檢查的話會白等滿 30 秒逾時，
        # 然後對著 Chrome 的錯誤頁截圖並當成目標畫面送審。
        error_text = str(result.get("errorText") or "")
        if error_text:
            raise ConnectionError("導覽 " + url + " 失敗：" + error_text)

        loader_id = str(result.get("loaderId") or "")
        frame_id = str(result.get("frameId") or "")

        # 先查佇列——這一次的載入事件可能在等 navigate 回應時就已經到了。
        if any(self._is_this_navigation(e, loader_id, frame_id, lifecycle_on)
               for e in self._events):
            time.sleep(settle_ms / 1000.0)
            return

        deadline = time.time() + self.timeout
        while time.time() < deadline:
            try:
                data = json.loads(self.ws.recv())
            except socket.timeout:
                break                   # 等不到就走 settle，不要整個卡死
            except Exception:
                break
            if self._is_this_navigation(data, loader_id, frame_id, lifecycle_on):
                break
            self._queue_event(data)
        time.sleep(settle_ms / 1000.0)

    def eval(self, expression: str):
        result = self.call("Runtime.evaluate", {
            "expression": expression,
            "returnByValue": True,
            "awaitPromise": True,
        })
        if result.get("exceptionDetails"):
            raise RuntimeError("頁面內執行失敗：" + json.dumps(
                result["exceptionDetails"], ensure_ascii=False)[:300])
        return (result.get("result") or {}).get("value")

    def screenshot(self, out_png: Path) -> None:
        result = self.call("Page.captureScreenshot", {"format": "png"}, timeout=90)
        out_png = Path(out_png).resolve()
        out_png.parent.mkdir(parents=True, exist_ok=True)
        out_png.write_bytes(base64.b64decode(result["data"]))

    def visible_text(self, limit: int = 6000) -> str:
        text = self.eval("document.body ? document.body.innerText : ''") or ""
        text = "\n".join(line.strip() for line in str(text).splitlines() if line.strip())
        if len(text) > limit:
            text = text[:limit] + "\n[... DOM 文字在此截斷 ...]"
        return text

    def close(self) -> None:
        """收乾淨。要能在半初始化的狀態下被呼叫（建構子失敗時就是這樣）。"""
        if self.ws:
            try:
                self.ws.close()
            except Exception:
                pass
            self.ws = None
        if self.proc is not None:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=10)
            except Exception:
                try:
                    self.proc.kill()
                    # kill() 之後一定要再等。不等的話行程可能還握著 profile
                    # 裡的檔案，接下來的 rmtree 會失敗，而 ignore_errors=True
                    # 把失敗吞掉——暫存目錄就這樣一次一次累積下來。
                    self.proc.wait(timeout=10)
                except Exception:
                    pass
            self.proc = None

        # Chrome 會生一堆子行程。主行程結束後它們可能還在收尾，
        # 所以 rmtree 要retry，不能一次失敗就靜默放棄。
        for attempt in range(5):
            shutil.rmtree(self.profile, ignore_errors=True)
            if not self.profile.exists():
                return
            time.sleep(0.4 * (attempt + 1))
        if self.profile.exists():
            # 真的刪不掉就講出來，不要無聲留下垃圾。
            sys.stderr.write("cross-review：暫存 profile 刪不掉，請手動清除 "
                             + str(self.profile) + "\n")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
