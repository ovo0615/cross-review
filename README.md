# cross-review

**Claude 負責產出、Codex 負責審查，兩者之間的往返不經過人。**

Claude Code 每次講完話，如果這一輪改到程式碼，Stop hook 會攔它一次，
要它把審查丟成背景任務再收尾。審查在背景跑完時，Claude Code 會自動把它叫醒
並交付報告——不必等你發下一句話。**你全程零等待，也不必在兩個工具之間傳話。**

審查分兩種，各自獨立跑：

| | 讀什麼 | 實測耗時 |
|---|---|---|
| **視覺審查** | 截圖、DOM 文字、上一回合的基準圖 | 50～120 秒 |
| **程式碼審查** | git diff／改動檔案、工具呼叫紀錄、你的原話與你的決定 | 120～650 秒 |

## 它實際抓到什麼

這個工具的每一行程式碼都被它自己審查過，**18 輪**。以下是它在自己身上抓到、
而 100 多項單元測試沒抓到的東西：

- **視覺回歸**：改動之後畫面哪裡壞了，而且會區分「你這次弄壞的」與「本來就壞的」
- **文字截斷**：把畫面上看得到的字跟 DOM 裡的字對照，抓出被容器切掉的內容
- **函式契約不一致**：一個函式回傳 2 個值、呼叫端解包 3 個——測試自己也解 2 個，
  於是測試全綠而正式路徑必然崩潰
- **假完成**：審查失敗卻被標記成功，症狀跟「審過了沒問題」一模一樣
- **只修一半**：同一個安全檢查加在 A 沒加在 B、同一個編碼修正改了甲沒改乙

最後一類是它最擅長的。人修完一個地方會記得「我剛修過」，
自動審查每一輪都重新讀整份程式碼。

在一個真實的 React + Vite 專案上跑過完整的驗證，
含視覺回歸、安全邊界與材料包上限——結果與實測數字在
[docs/validation.md](docs/validation.md)。

## 需要什麼

| 項目 | 用途 |
|---|---|
| **Claude Code** | 執行者，並提供 Stop hook 與背景任務喚醒機制 |
| **[Codex CLI](https://github.com/openai/codex)** | 審查者。需要已登入 |
| **Python 3.8+** | 整個工具 |
| **Google Chrome** | 視覺審查的截圖與互動（不做視覺審查就不需要） |

**不需要 `pip install` 任何東西。**只用 Python 標準函式庫——
連 CDP 用的 WebSocket 用戶端都是自己寫的（[ADR-0006](docs/adr/0006-interaction-is-configuration-not-code.md)），
就是為了讓它在任何專案零設定跑起來。

目前只在 **Windows** 上驗證過。`codex.exe` 的路徑不寫死，
每次執行時掃 `%LOCALAPPDATA%\OpenAI\Codex\bin\*\codex.exe` 挑最新的——
那串目錄名是 build hash，Codex 一更新就會變。

## 安裝

把這個 repo clone 到任何位置，然後在要啟用的專案建立 `.claude\settings.json`：

```json
{
  "hooks": {
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "py -3 \"<你 clone 的位置>\\run_hook.py\"",
            "timeout": 30,
            "statusMessage": "cross-review：檢查這一輪要不要送審"
          }
        ]
      }
    ]
  }
}
```

設定改完要開一個新的 Claude Code session 才會生效。

### 為什麼是逐專案安裝，而不是裝在全域

技術上，把同一段 `Stop` 項目加進 `%USERPROFILE%\.claude\settings.json`
就會對所有專案生效。**但不建議這樣做，理由跟工具的可靠度無關。**

觸發之後，工具會把**改動檔案的完整內容**送進審查者。副檔名白名單只決定
「要不要審」，不決定「送什麼」——一旦觸發，整份檔案就出去了。

如果你的磁碟上有客戶資料、NDA 範圍內的東西，或任何你不會主動貼給第三方
服務的內容，全域啟用等於：**只要你在那些目錄下改到一個 `.py` 或 `.json`，
那份檔案就自動送出去，而且不會問你。**

「這份程式碼可不可以送給第三方模型」應該逐次判斷，不該由一條副檔名規則
代勞。多貼一次那 12 行，換的是每個專案都由你點頭過。

裝上去之後**只有改到程式碼副檔名時才會真的叫審查者**
（`.py` `.js` `.ts` `.tsx` `.ps1` `.css` `.html` 等）。改文件、投影片、
資料檔不會觸發，一秒都不花。

## 每個專案的設定

第一次觸發時自動產生 `.claude\review\config.json`：

```jsonc
{
  "enabled": true,
  "code_review": true,
  "visual_review": true,
  "max_files": 40,          // 材料包上限（硬上限，會實際夾住）
  "max_bytes": 200000,
  "max_images": 8,          // 有基準圖的畫面佔兩張，8 約等於 4 個畫面
  "shots": [],              // 見下方
  "codex_model": "",        // 留空＝沿用 ~/.codex/config.toml
  "codex_reasoning_effort": "",
  "codex_timeout_sec": 900
}
```

- **永久關掉某個專案**：在 `.claude\review\` 放一個空檔案叫 `DISABLED`
- **只要視覺不要程式碼**：把 `code_review` 設成 `false`

### 審查者用哪個模型

每份報告開頭會印出這一趟實際用了什麼：

```
gpt-5.6-luna · effort=high · 193.8 秒 · 36,270 tokens
```

**Codex 的預設不一定是最強的模型。**`codex_model` 與 `codex_reasoning_effort`
只影響審查那一趟，你自己開的 Codex 不受影響。

> 這個欄位顯示的是**已用**的 token，不是剩餘額度。
> Codex CLI 沒有提供可程式化的額度查詢介面。

### 畫面清單與互動

`shots` 裡每一項是一個畫面。有 `.claude\launch.json` 的專案會自動帶出預設值。

```jsonc
{
  "name": "主畫面",
  "url": "http://localhost:5173/",
  "width": 1440,
  "height": 900,
  "actions": [
    { "do": "click",  "text": "開始" },
    { "do": "wait",   "ms": 1200 },
    { "do": "shot",   "name": "初始" },
    { "do": "click",  "text": "設定" },
    { "do": "shot",   "name": "設定分頁" }
  ]
}
```

`actions` 留空就是「載入頁面、拍一張」。動作是**設定不是程式**。
一個 `shot` 項目只載入頁面一次，所以多個 `shot` 動作可以連續拍下
同一次操作流程的好幾個狀態。

| 動作 | 參數 | 說明 |
|---|---|---|
| `shot` | `name` | 拍一張並命名 |
| `click` | `text` **或** `selector` | 優先用 `text` |
| `type` | `selector`、`text` | 用原生 setter，React／Vue 的受控元件也吃得到 |
| `scroll` | `to`（`top`／`bottom`）或 `selector` | |
| `wait` | `ms` | 上限 10 秒 |

**點擊優先用 `text`。**真實專案常常沒有 `id` 也沒有 `data-testid`——
一個實測的例子是某個工具的分頁列有 11 個同 class 的按鈕，
用 `nth-of-type` 會在分頁順序一改時默默錯位，而且設定檔完全看不出點的是哪一個。

**畫面清單要保持穩定**——視覺回歸是拿這一輪的圖跟上一輪同名的圖比，
清單一變就沒有基準可比。

## 三個刻意的設計

**工具不會自己啟動你的 app**（[ADR-0004](docs/adr/0004-screenshots-only-when-dev-server-is-already-up.md)）。
dev server 沒開就跳過視覺審查並說明原因。你在改 GUI 時本來就開著它，
而 Vite 的 HMR 保證拍到的是最新版。

**失敗一律出聲，絕不靜默。**審查跑不成的樣子絕對不會長得像「審過了沒問題」。
這是整個工具最核心的原則，也是它自己被抓最多次的地方
（[ADR-0007](docs/adr/0007-done-means-a-report-was-produced.md)）。

**審查者讀的是觀察來的事實，不是執行者的自述**
（[ADR-0001](docs/adr/0001-reviewer-reads-observed-facts.md)）。
材料包來自 Claude Code 自己寫的工具呼叫紀錄與 git，
不是讓被審查的一方總結「我這輪做了什麼」。

## 安全邊界

這個工具會在每個專案自動執行，**包含你從別處 clone 回來的版本庫**。
因此：

- 畫面網址預設只允許本機 `http(s)`，不跟隨轉址
- `.claude\review\` 必須確實落在專案內（symlink／junction／懸空連結都會被擋）
- 材料包只讀專案內的檔案
- 審查那一趟會停用 Codex 的滑鼠、瀏覽器與 Chrome plugin
- **這些邊界專案設定改不動**，只能從 `%USERPROFILE%\.claude\cross-review.json` 放寬

## 產出物

全部在專案的 `.claude\review\`。有 git 的專案會自動寫進 `.git\info\exclude`——
**不是 `.gitignore`**，那是追蹤中的檔案，動它會在你的工作樹留下與需求無關的改動。
linked worktree 與 submodule 也處理了。

| 檔案 | 內容 |
|---|---|
| `latest-visual.md` / `latest-code.md` | 最新一份報告 |
| `report-<回合>-<類型>.md` | 每一回合的報告 |
| `dossier-<回合>-<類型>.md` | 送給審查者的材料包原文（要查證時看這個） |
| `shots/` `baseline/` | 這一輪與上一輪的截圖 |
| `errors.log` | 所有失敗 |
| `state.json` | 游標、回合編號、修改時間水位線 |

## 設計決策

`docs/adr/` 記錄了為什麼是這樣而不是那樣，每一份都附著推翻它的實測數字：

| | |
|---|---|
| [0001](docs/adr/0001-reviewer-reads-observed-facts.md) | 審查者讀的是觀察來的事實，不是執行者的自述 |
| [0002](docs/adr/0002-review-runs-in-background.md) | 審查在背景跑，由 harness 喚醒執行者，而不是讓使用者等 |
| [0003](docs/adr/0003-visual-review-is-a-separate-pass.md) | 視覺審查與程式碼審查是兩個獨立的審查 |
| [0004](docs/adr/0004-screenshots-only-when-dev-server-is-already-up.md) | 截圖只在 dev server 已經開著時拍 |
| [0005](docs/adr/0005-changed-files-need-a-watermark.md) | 「這一輪改了什麼」需要修改時間水位線 |
| [0006](docs/adr/0006-interaction-is-configuration-not-code.md) | 互動用設定描述，並自己寫一個極小的 CDP 用戶端 |
| [0007](docs/adr/0007-done-means-a-report-was-produced.md) | `.done` 的語意是「產出了報告」 |

這些 ADR 不只給人看——**它們會被放進每一份材料包**，
讓審查者知道哪些事情是刻意的，不要每一輪重新爭論。
實測效果：同一個爭議連續四輪被提出，寫成 ADR 之後一次就停了。

詞彙定義在 [CONTEXT.md](CONTEXT.md)。

## 開發

```bash
py -3 tests/test_hook_flow.py
```

100 多項端對端測試，用合成的 transcript 驗證整條判斷鏈。
其中大多數是真實踩過的坑留下的回歸測試。

## 授權

MIT。
