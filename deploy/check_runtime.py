#!/usr/bin/env python3
"""Check the deployed runtime, without credentials, data or application startup."""

from __future__ import annotations

import importlib
import importlib.metadata
import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path


def canonical(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def main() -> None:
    assert sys.prefix == "/venv", "Use the production virtual environment"
    sys.path.insert(0, "/app")
    expected = {
        canonical(name): version
        for name, version in re.findall(
            r"^([A-Za-z0-9_.-]+)==([^\s;\\]+)",
            Path("/app/requirements.lock").read_text(),
            re.MULTILINE,
        )
    }
    assert expected, "Missing locked dependency inventory"
    actual = {
        canonical(d.metadata["Name"]): d.version
        for d in importlib.metadata.distributions()
    }
    assert actual == expected, "Runtime packages differ from requirements.lock"
    for name in ("pip", "setuptools", "pkg_resources", "ensurepip"):
        assert importlib.util.find_spec(name) is None, f"Build tool remains: {name}"
    for path in ("/app/.env", "/app/.secrets", "/usr/include/linux"):
        assert not Path(path).exists(), f"Unexpected runtime file: {path}"
    packages = subprocess.run(
        ["dpkg-query", "-W", "-f=${binary:Package}\n"],
        capture_output=True,
        check=True,
        text=True,
    ).stdout.splitlines()
    assert not any(
        name.split(":", 1)[0].endswith("-dev")
        or name.split(":", 1)[0] in {"gcc", "g++", "python3-pip", "python3-venv"}
        for name in packages
    ), "Development package remains in the runtime"
    for name in (
        "APITaxi", "celery", "flask_security", "passlib.context",
        "psycopg2", "shapely", "numpy", "argon2", "bcrypt", "cffi",
    ):
        importlib.import_module(name)
    # Wheel support libraries inherit their loader's RPATH. Check extensions
    # directly and exercise GEOS/BLAS through the Python modules that load them.
    import numpy
    from shapely.geometry import Point

    assert Point(0, 0).buffer(1).area > 3
    assert numpy.linalg.det(numpy.eye(2)) == 1
    native_libraries = [
        path for path in Path("/venv").rglob("*.so*")
        if not any(part.endswith(".libs") for part in path.parts)
    ]
    for library in native_libraries:
        result = subprocess.run(
            ["ldd", str(library)], capture_output=True, text=True, timeout=15,
        )
        missing = [
            line.strip() for line in (result.stdout + result.stderr).splitlines()
            if "not found" in line
        ]
        assert not missing, f"Missing runtime shared library for {library}: {missing}"
    print(json.dumps({
        "status": "passed", "locked_packages": len(expected),
        "native_libraries_checked": len(native_libraries),
        "build_tools_absent": True, "development_headers_absent": True,
    }, sort_keys=True))


if __name__ == "__main__":
    main()
