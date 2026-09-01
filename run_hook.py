"""Stop hook 的進入點。

settings.json 裡指向這支檔案。它唯一的工作是把工具根目錄放進 sys.path，
讓 cross_review 這個套件在任何專案的工作目錄下都 import 得到。
"""
import json
import sys
import time
import traceback
from pathlib import Path

TOOL_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOL_ROOT))

from cross_review.hook import main  # noqa: E402


def report_own_failure(exc: BaseException) -> None:
    """hook 自己壞掉時，不擋使用者，但一定要讓他看得見。

    原本這裡是空的 `except Exception: sys.exit(0)`——第一次上線後的審查
    立刻指出那等於靜默失效：hook 從此不做事，而使用者以為有人在審。
    這正是「失敗一律出聲」要防的東西，卻被寫在守門員自己身上。
    """
    log = TOOL_ROOT / "hook-crash.log"
    try:
        with open(log, "a", encoding="utf-8", newline="\n") as fh:
            fh.write("[" + time.strftime("%Y-%m-%d %H:%M:%S") + "]\n")
            fh.write("".join(traceback.format_exception(
                type(exc), exc, exc.__traceback__)) + "\n")
    except Exception:
        pass

    # systemMessage 會顯示出來但不擋收尾。stdout 若已經寫過東西就別再寫，
    # 免得輸出變成兩段不合法的 JSON。
    try:
        sys.stdout.write(json.dumps({
            "systemMessage":
                "cross-review 的 hook 自己出錯了，這一輪沒有送審。"
                "詳情見 " + str(log),
        }, ensure_ascii=False))
        sys.stdout.flush()
    except Exception:
        pass


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise                      # passthrough()／正常結束走這裡，不是錯誤
    except BaseException as exc:   # noqa: BLE001 - 守門員不能自己掛掉
        report_own_failure(exc)
        sys.exit(0)

# 這支檔案只是轉接器，實際的 hook 邏輯全部在 cross_review 套件裡。
# 修改邏輯請改 cross_review\hook.py，不要動這裡。
