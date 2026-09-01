"""背景審查的進入點。

執行者會以背景任務執行這支檔案（指令由 Stop hook 的攔阻訊息提供）。
它的 stdout 就是跑完之後交回給執行者的報告。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cross_review.review import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
