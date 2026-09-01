# 截圖只在 dev server 已經開著時拍，工具絕不自己啟動應用程式

視覺審查前先偵測 port 有沒有人在聽。有就直接用 headless Chrome 拍；
沒有就跳過視覺審查並出聲告知，**不會**依 `.claude/launch.json` 自行啟動
`npm run dev` 或 `uvicorn`。

理由是開發機上的既有證據。那台機器的 `~/.claude/hooks/stop-hook.ps1`
每一輪都在做這件事：

```powershell
Get-NetTCPConnection -LocalPort 8099 -State Listen | taskkill /PID $i /T /F
Where-Object { $_.CommandLine -match '--headless' } | taskkill ...
```

那台機器已經被孤兒 dev server 與殘留的 headless Chrome 咬到需要寫一支 hook 每輪收屍。
讓一個每輪自動觸發、而且沒有人盯著看的背景腳本去啟動同一類程序，
是在同一個坑上再挖一次。

這個限制在實務上幾乎不痛：使用者在改 GUI 時 dev server 本來就開著，
而 Vite 的 HMR 保證那個開著的 server 已經是最新版，拍下去就是最新畫面——
不需要啟動任何東西，也不需要關掉任何東西。

## Consequences

- 沒開 dev server 的回合沒有視覺審查。依「失敗一律出聲」的原則，這件事會被講出來，
  不會靜默跳過。
