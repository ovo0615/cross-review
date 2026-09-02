# ADR-0010：送出的同意住在使用者層級，專案設定只能收緊

日期：2026-09-02
狀態：已採用（取代 ADR-0008 關於預設值的部分）

## 背景

ADR-0008 選了 `threshold` 當預設，理由是「純手動會忘記，門檻是安全網」。
那個判斷成立於一個前提：工具是**逐專案安裝**的，「要不要讓這個專案的程式碼
出去給第三方模型」在安裝那一刻就已經表態過了。

2026-09-02 改成全域安裝之後，那個前提消失了：任何目錄開 session 都會被當成專案。
實測餵一個 `%TEMP%` 路徑給 hook，它照樣認成專案、在那裡建出 `.claude/review/`、
掃出 41 KB 改動並要求送審。而這台機器的 `D:\` 底下有客戶資料。

於是預設改成 `manual`，把「同意送出」這個動作移到 `trigger`。

**但那個修正只做了一半。** 第 39 回合的審查指出：`trigger` 不在 `SECURITY_KEYS`
裡，所以任何專案的 `.claude/review/config.json` 都能把自己設成 `auto`——包括
從別處 clone 回來、版本庫裡本來就帶著這個檔案的專案。實測確認成立。

配上全域 hook，那條路徑是：**clone 一個陌生的 repo → 開 session →
它的程式碼自動送去 Codex，使用者完全沒有點過頭。**

## 決定

同意必須住在一個版本庫碰不到的地方。

* 授權記在 `%USERPROFILE%\.claude\cross-review.json` 的 `triggers`，以專案的
  絕對路徑為鍵。沒有紀錄就是 `manual`。
* `run_review.py --trigger` 只寫這裡，不再寫專案設定。
* 專案設定的 `trigger` 仍然讀，但**只能往嚴格的方向生效**：
  生效值 = 使用者授予的與專案要求的取較嚴格者
  （`manual` < `threshold` < `auto`）。專案可以把自己收得更緊，放不寬。

這跟 `disable_codex_plugins` 是同一個原則：內建清單是地板，另一邊只能加不能減。
**預設拒絕如果可以被對方打開，那就不是邊界。**

## 後果

- ADR-0008 關於「預設 `threshold`」與「反對預設 `manual`」的段落不再適用。
  該文件其餘部分（三種模式的定義、門檻只保證不漏不判斷值不值得、
  不送審的回合什麼都不推進）仍然有效。
- 既有專案的 `config.json` 裡若寫著 `threshold`／`auto`，升級後會降級成
  `manual`，直到使用者用 `--trigger` 明確授權。這是刻意的：那些值是在舊的
  信任模型下寫的。
## 這條邊界擋得住什麼、擋不住什麼

**擋得住：版本庫裡 commit 進來的 `.claude/review/config.json`。**
這是現實中會發生的情況——別人的專案本來就用 cross-review 且設成 `threshold`，
你 clone 回來之後那份設定不該自動對你生效。

**擋不住：版本庫裡 commit 進來的 `.claude/settings.json`。**
官方文件寫明那個檔案就是設計來 commit 給團隊用的，內容包含
「Team permissions, **hooks**, plugins, and the environment variables the project needs」。
能寫那個檔案的版本庫可以直接定義自己的 Stop hook 執行任意指令——
到那一步，它根本不需要繞過 cross-review 的同意檔，自己呼叫 codex 就好。

所以在那個威脅模型下，**任何工具內部的邊界都是無效的**，控制點是 Claude Code
自己的「信任這個資料夾」提示。把 `Path.home()` 換成不受環境變數影響的
作業系統 API 只會關掉比較弱的那條路、留著比較強的那條，屬於安全劇場。

第一版曾經加過一個 `CROSS_REVIEW_SETTINGS` 環境變數給測試用，並在這裡寫著
「clone 回來的版本庫設不了環境變數」。那句話是錯的（見上），該覆寫已移除——
**不是因為它會被利用，而是因為那是這個工具自己開的、沒有必要的門。**
測試改成把家目錄指到暫存目錄，用的是作業系統本來就有的機制。
