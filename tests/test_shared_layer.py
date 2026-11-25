"""驗證 Layer 產物及隔離匯入，避免測試只靠 repo 根目錄而掩蓋部署缺檔。"""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_layer_packages_allowlisted_modules_and_imports_without_repo(tmp_path):
    """從獨立工作目錄匯入 Layer 與服務；Python -I 排除 PYTHONPATH／目前目錄。"""
    artifacts = tmp_path / "layer"
    subprocess.run(
        [sys.executable, str(ROOT / "shared" / "build_layer.py"), str(artifacts)],
        check=True,
        cwd=tmp_path,
    )
    package = artifacts / "python" / "shared"
    assert {path.name for path in package.iterdir()} == {
        "__init__.py", "privacy.py", "models.py",
    }
    assert (package / "privacy.py").read_bytes() == (ROOT / "shared" / "privacy.py").read_bytes()

    # 只提供函式原始碼與 Layer，不把 repo 根目錄放回 sys.path。
    # CI / 本機的第三方套件仍由執行測試的 Python 提供。
    script = """
import sys
import socket
from pathlib import Path
def forbid_network(*args, **kwargs):
    raise AssertionError('Network access is forbidden during layer import tests')
socket.socket.connect = forbid_network
socket.socket.connect_ex = forbid_network
socket.create_connection = forbid_network
socket.getaddrinfo = forbid_network
sys.path[:0] = sys.argv[1:3]
import shared.privacy as privacy
from shared.models import TAG_CATALOG
from push_service import contains_pii, redact_pii
import app
assert Path(privacy.__file__).resolve().is_relative_to(Path(sys.argv[2]).resolve())
assert contains_pii is privacy.contains_pii
assert redact_pii is privacy.redact_pii
assert app.redact_pii is privacy.redact_pii
assert contains_pii('test@example.org')
assert not contains_pii(redact_pii('test@example.org'))
assert TAG_CATALOG
print('Isolated layer imports OK')
"""
    result = subprocess.run(
        [sys.executable, "-I", "-c", script,
         str(ROOT / "backend" / "webhook"), str(artifacts / "python")],
        check=True,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert "Isolated layer imports OK" in result.stdout
