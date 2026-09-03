"""背景審查的進入點。

執行者會以背景任務執行這支檔案（指令由 Stop hook 的攔阻訊息提供）。
它的 stdout 就是跑完之後交回給執行者的報告。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cross_review.review import main  # noqa: E402

if __name__ == "__main__":
    # `--usage [專案路徑]` 直接印用量彙總，不跑審查。
    # `--install <專案>` / `--uninstall <專案>` 裝上或移除這個專案的 Stop hook。
    if len(sys.argv) > 1 and sys.argv[1] in ("--install", "--uninstall"):
        from cross_review import common
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass
        target = Path(sys.argv[2]) if len(sys.argv) > 2 else Path.cwd()
        try:
            action = (common.install_hook if sys.argv[1] == "--install"
                      else common.uninstall_hook)
            print(action(target))
        except RuntimeError as exc:
            print("⚠️ " + str(exc))
            sys.exit(1)
        sys.exit(0)

    # `--now [專案路徑]` 手動送審一次（手動／門檻模式下 hook 不會建工作單）。
    if len(sys.argv) > 1 and sys.argv[1] == "--now":
        from cross_review.review import run_now
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass
        rest = list(sys.argv[2:])
        only = None
        force = "--force" in rest
        rest = [a for a in rest if a != "--force"]
        # 手寫解析要自己驗：`--now PROJECT --mode`（少了值）原本會安靜地退回
        # 跑全部模式，等於使用者以為只跑視覺、實際上連程式碼審查也送出去了。
        while "--mode" in rest:
            i = rest.index("--mode")
            value = rest[i + 1] if len(rest) > i + 1 else None
            if not value or value.startswith("-"):
                print("--mode 後面要接 code 或 visual。")
                sys.exit(2)
            if only and only != value:
                print("--mode 只能指定一次（收到 " + only + " 與 " + value + "）。")
                sys.exit(2)
            if value not in ("code", "visual"):
                print("--mode 只能是 code 或 visual，收到：" + value)
                sys.exit(2)
            only = value
            del rest[i:i + 2]
        if len(rest) > 1:
            print("多餘的參數：" + "、".join(rest[1:]))
            sys.exit(2)
        target = Path(rest[0]) if rest else Path.cwd()
        sys.exit(run_now(target, only, force))

    # `--trigger auto|manual|threshold [專案路徑]` 改這個專案的觸發模式。
    if len(sys.argv) > 2 and sys.argv[1] == "--trigger":
        from cross_review.review import set_trigger
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass
        target = Path(sys.argv[3]) if len(sys.argv) > 3 else Path.cwd()
        sys.exit(set_trigger(target, sys.argv[2]))

    if len(sys.argv) > 1 and sys.argv[1] == "--usage":
        from cross_review import usage
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass
        target = Path(sys.argv[2]) if len(sys.argv) > 2 else Path.cwd()
        print(usage.summary(target))
        sys.exit(0)
    sys.exit(main())
