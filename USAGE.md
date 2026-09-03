# 操作說明

## 前置需求

| 需要 | 用途 | 確認 |
|---|---|---|
| Python 3.9+ | 跑工具 | `py -3 --version` |
| Codex CLI（已登入） | 當審查者 | `codex --version` |
| Google Chrome | 只有視覺審查要 | 裝在預設位置即可 |

**不需要 `pip install` 任何東西**，只用標準函式庫。

## 安裝

**一個專案一個專案裝**，一行指令：

```bash
py -3 <工具>un_review.py --install   "<專案>"
py -3 <工具>un_review.py --uninstall "<專案>"
```

它只動 `<專案>/.claude/settings.json` 裡屬於本工具的那一筆，其他設定原封不動；
重複執行不會裝兩次。**新開的 session 才會生效，既有的要重開。**

**不要裝在全域。** 材料包送的是檔案全文，而全域安裝等於任何目錄開 session 都被
當成專案——實測餵一個 `%TEMP%` 路徑給 hook，它照樣認成專案、在那裡建出
`.claude/review/` 並要求送審。**安裝這個動作本身就是「這個專案的程式碼可以出去」
的同意。**


## 三個指令

```bash
py -3 <工具>\run_review.py --now "<專案>"                  # 送審
py -3 <工具>\run_review.py --now "<專案>" --mode visual    # 只看畫面
py -3 <工具>\run_review.py --usage "<專案>"                # 查用量
```

對 Claude 說「審查」也可以。

## 觸發模式

| 模式 | 行為 |
|---|---|
| `manual`（預設） | 不自動送，每輪只報累積量 |
| `threshold` | 累積 ≥ 10 檔或 ≥ 20 KB 才送 |
| `auto` | 有改動就送 |

```bash
py -3 <工具>\run_review.py --trigger threshold "<專案>"
```

**授權寫在 `~/.claude/cross-review.json`，不是專案裡。** 專案的 `trigger` 只能收緊、放不寬——否則 clone 回來的版本庫能自己開啟自動送審。

沒送審的回合什麼都不推進，累積量會一路長大，不會漏。

`manual` 下 `--now` 會先讀逐字稿：**使用者最近那句話沒有要求審查就拒絕送出**。
判準是使用者的原話，因為那是執行者唯一偽造不了的東西——只寫「不要自己送審」
的提示攔不住已經決定好流程的執行者（實測過）。誤判時加 `--force`，會記進
`errors.log`。

## 視覺審查

dev server 要自己先開著。設定 `<專案>/.claude/review/config.json`：

```json
"shots": [{
  "name": "主畫面",
  "url": "http://localhost:5190/",
  "width": 1440, "height": 900,
  "actions": [
    { "do": "click", "text": "開始" },
    { "do": "wait",  "ms": 1200 },
    { "do": "shot",  "name": "Layout" }
  ]
}]
```

動作：`click`（用畫面文字）、`type`、`scroll`、`wait`、`shot`。

第一次跑會建基準圖，之後每次跟它比。要重設就刪 `baseline/` 裡對應的檔案。

## 產出位置

`<專案>/.claude/review/`：報告、材料包、基準圖、用量帳本、狀態。不進版本庫。

## 疑難排解

| 症狀 | 處理 |
|---|---|
| 完全沒訊息 | 這輪沒改程式碼（正常），或 session 開在裝 hook 之前 |
| 找不到 codex.exe | 沒裝或沒登入 |
| 一個畫面都沒拍到 | dev server 沒開 |
| 基準圖整批不符 | 視窗尺寸變了，或刪掉 `baseline/` 重建 |
| 掃到別人的檔案 | 寫進 `ignore_paths`（巢狀 repo／worktree 會自動排除） |
| 額度用完 | 自動暫停到恢復時間，不用處理 |
| 要完全關掉 | `config.json` 的 `enabled` 設 `false` |

## 換模型

```json
"codex_model": "gpt-5.6-sol",
"codex_reasoning_effort": "high"
```

`sol`／`terra`／`luna` 由強到快；effort 可到 `ultra`。**不要留空**——留空會沿用你自己的 Codex 設定，審查品質會跟著它漂移。

## 注意

材料包送的是**檔案全文**。全域安裝時任何目錄開 session 都算專案，所以預設 `manual`，你說了才送。

設計決策見 [`docs/adr/`](docs/adr/)。
