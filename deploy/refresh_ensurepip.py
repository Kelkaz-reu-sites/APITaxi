#!/usr/bin/env python3
"""Replace an installed interpreter's ensurepip seed with a verified pip wheel.

This is offline and does not install pip in the active virtual environment.
The wheel SHA-256 is the one published in PSF announcement 109576.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sysconfig
from pathlib import Path

PIP_VERSION = "26.2.2"
PIP_WHEEL = "pip-26.2.2-py3-none-any.whl"
PIP_SHA256 = "fd7f4dfa8190e014013b9f08f3e973f4e4c26ffd179a204895533a7254813a70"


def refresh(wheel: Path, ensurepip: Path) -> dict:
    if (
        wheel.name != PIP_WHEEL
        or hashlib.sha256(wheel.read_bytes()).hexdigest() != PIP_SHA256
    ):
        raise ValueError("pip wheel does not match the PSF-published checksum")
    module = ensurepip / "__init__.py"
    text = module.read_text()
    replaced, count = re.subn(
        r'^_PIP_VERSION = ["\'][^"\']+["\']$',
        f'_PIP_VERSION = "{PIP_VERSION}"',
        text,
        flags=re.M,
    )
    if count != 1:
        raise ValueError("Unknown ensurepip seed selection; no change made")
    bundled = ensurepip / "_bundled"
    if not bundled.is_dir():
        raise ValueError("Missing ensurepip bundled wheel directory")
    destination = bundled / PIP_WHEEL
    temporary = destination.with_suffix(".whl.new")
    shutil.copyfile(wheel, temporary)
    temporary.chmod(0o644)
    temporary.replace(destination)
    module.write_text(replaced)
    for previous in bundled.glob("pip-*.whl"):
        if previous != destination:
            previous.unlink()
    return {
        "status": "passed",
        "ensurepip_seed": PIP_VERSION,
        "wheel_sha256": PIP_SHA256,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wheel", type=Path)
    args = parser.parse_args()
    result = refresh(args.wheel, Path(sysconfig.get_path("stdlib")) / "ensurepip")
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
