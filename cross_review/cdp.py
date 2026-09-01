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
                "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE localhost",
                "--proxy-server=http://127.0.0.1:1",
                "--proxy-bypass-list=" + _bypass_list(),
            ]
        args.append("about:blank")

        self.proc = None
        self.ws = None
        self._id = 0
        try:
            self.proc = subprocess.Popen(
                args,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            self._connect()
        except BaseException:
            # 建構子拋例外時 close() 永遠不會被呼叫，呼叫端的 finally 也
            # 因為變數還沒被賦值而清不掉——Chrome 行程與暫存 profile 就留下來了。
            # 重試幾次就會累積成一堆孤兒程序。這裡自己收乾淨再把例外丟出去。
            self.close()
            raise

    def _endpoint(self, path: str, timeout: float):
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            try:
                url = "http://127.0.0.1:" + str(self.port) + path
                with urllib.request.urlopen(url, timeout=2) as resp:
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
        self.ws = WebSocket(page["webSocketDebuggerUrl"], timeout=self.timeout)

    def call(self, method: str, params: dict = None, timeout: float = None):
        """送一個 CDP 指令，等它自己的回應（沿路的事件先擱著）。"""
        self._id += 1
        msg_id = self._id
        self.ws.send(json.dumps({"id": msg_id, "method": method, "params": params or {}}))
        deadline = time.time() + (timeout or self.timeout)
        while time.time() < deadline:
            data = json.loads(self.ws.recv())
            if data.get("id") != msg_id:
                continue          # 這是事件或別的回應
            if "error" in data:
                raise RuntimeError(method + " 失敗：" + json.dumps(data["error"], ensure_ascii=False))
            return data.get("result", {})
        raise TimeoutError(method + " 逾時")

    # -------------------------------------------------- 高階操作
    def goto(self, url: str, settle_ms: int = 1200) -> None:
        self.call("Page.enable")
        self.call("Page.navigate", {"url": url})
        # 等 load 事件；等不到就靠 settle 的固定時間兜底，不要整個卡死。
        deadline = time.time() + self.timeout
        while time.time() < deadline:
            data = json.loads(self.ws.recv())
            if data.get("method") in ("Page.loadEventFired", "Page.frameStoppedLoading"):
                break
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
                except Exception:
                    pass
            self.proc = None
        shutil.rmtree(self.profile, ignore_errors=True)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
