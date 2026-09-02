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
