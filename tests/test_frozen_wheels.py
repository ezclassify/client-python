"""The CI wheelhouse must preserve bytes, not just distribution versions."""

import importlib.metadata
import importlib.util
import zipfile
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / "scripts" / "check_frozen_wheels.py"
SPEC = importlib.util.spec_from_file_location("check_frozen_wheels", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_wheel_payload_identity(tmp_path: Path) -> None:
    metadata_path = "example-1.0.dist-info/METADATA"
    metadata = b"Name: example\nVersion: 1.0\n"
    files = {"example/__init__.py": b"value = 1\n", metadata_path: metadata}
    for name, content in files.items():
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    record = tmp_path / "example-1.0.dist-info/RECORD"
    record.write_text("".join(f"{name},,\n" for name in files), encoding="utf-8")
    distribution = importlib.metadata.PathDistribution(record.parent)
    wheel = tmp_path / "example-1.0-py3-none-any.whl"

    def pack(payload: dict[str, bytes]) -> None:
        with zipfile.ZipFile(wheel, "w") as archive:
            for name, content in payload.items():
                archive.writestr(name, content)

    pack(files)
    MODULE.check_wheel(wheel, distribution)
    pack({**files, "example/__init__.py": b"value = 2\n"})
    with pytest.raises(ValueError, match="payload differs"):
        MODULE.check_wheel(wheel, distribution)
    pack({**files, "example/extra.py": b"new\n"})
    with pytest.raises(ValueError, match="file set differs"):
        MODULE.check_wheel(wheel, distribution)
    pack({metadata_path: metadata})
    with pytest.raises(ValueError, match="file set differs"):
        MODULE.check_wheel(wheel, distribution)
    pack({**files, metadata_path: b"Name: renamed\nVersion: 1.0\n"})
    with pytest.raises(ValueError, match="name/version differs"):
        MODULE.check_wheel(wheel, distribution)
