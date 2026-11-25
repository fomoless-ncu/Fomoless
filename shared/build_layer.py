"""建置時使用的打包工具，負責把共用程式放進 Lambda Layer 所需的目錄結構，讓 Lambda 可以執行。

由 SAM Makefile 呼叫，也能在本機用 Python 驗證產物結構。
只打包白名單中的純 Python 模組，不安裝依賴或複製整個專案。
models 所需的 Pydantic 由函式自己的 requirements.txt 提供。
"""

import shutil
import sys
from pathlib import Path

# 指定要打包的檔案
LAYER_MODULES = ("__init__.py", "privacy.py", "models.py")

def build_layer(artifacts_dir: Path) -> Path:
    """輸出 Lambda 可直接匯入的 python/shared/*.py，回傳套件目錄。"""
    # 找到原始檔案的位置
    source = Path(__file__).resolve().parent  
    # 建立輸出資料夾
    package = artifacts_dir / "python" / "shared"  
    package.mkdir(parents=True, exist_ok=True)
    # 複製檔案並回傳位置，迴圈逐一處理清單中的檔案
    for filename in LAYER_MODULES:
        shutil.copy2(source / filename, package / filename)
    return package

# 只有直接執行這支檔案時，才執行下面的程式
# 輸出目錄字串長度應該剛好為 2 ；如果少傳或多傳參數，就以 SystemExit 結束程式
if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("Usage: python build_layer.py ARTIFACTS_DIR")
    build_layer(Path(sys.argv[1]))
