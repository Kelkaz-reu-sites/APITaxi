#!/usr/bin/env python3
"""Offline audit of public TLS trust stores after the October 2026 CA incident.

No certificates, environment values or credentials are printed. This checks
actual PEM/DER contents as well as Python downloaders and ensurepip seed wheels.
Sources: PSF announcement 109576 and emudhra.we-have-a-problem.com/roots.json.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import importlib
import importlib.metadata
import importlib.util
import json
import os
import re
import shutil
import ssl
import subprocess
import sysconfig
import zipfile
from pathlib import Path

BLOCKED_ROOTS = {
    "86a1ecba089c4a8d3bbe2734c612ba341d813e043cf9e8a862cd5c57a36bbe6b": "emSign ECC Root CA - G3",
    "d7a7a0fb5d7e2731d771e9484ebcdef71d5f0c3e0a2948782bc83ee0ea699ef4": "AAA Certificate Services",
    "7dd78d5f4f13459a83dff9abbba62edbaf6f2d102bf257fd712f4d9f2746ed8d": "emSign Root TLS CA - G3",
    "f2edaaff3d9b701fb4d14c9f285f8c0bad90fc3bcc1fa07d47f4718edaba34a5": "emSign Root CS CA - G3",
}
MINIMUMS = {
    "certifi": "2026.10.19",
    "pip": "26.2.2",
    "uv": "0.13.4",
    "sigstore": "4.5.1",
}
MAX_BUNDLE_BYTES = 4 * 1024 * 1024
PEM = re.compile(rb"-----BEGIN CERTIFICATE-----\s*(.*?)-----END CERTIFICATE-----", re.DOTALL)


def version_at_least(value: str, minimum: str) -> bool:
    """Require a final release; an rc of the minimum is not a patched release."""
    if not re.fullmatch(r"\d+(?:\.\d+)*(?:\+[A-Za-z0-9._-]+)?", value):
        return False
    actual = tuple(int(p) for p in value.split("+", 1)[0].split("."))
    expected = tuple(int(p) for p in minimum.split("."))
    width = max(len(actual), len(expected))
    return actual + (0,) * (width - len(actual)) >= expected + (0,) * (
        width - len(expected)
    )


def inspect_der(certificates: list[bytes], source: str) -> dict:
    if not certificates:
        raise ValueError("empty CA store")
    hashes = {hashlib.sha256(cert).hexdigest() for cert in certificates}
    return {
        "source": source,
        "certificates": len(certificates),
        "blocked_roots": sorted(
            BLOCKED_ROOTS[h] for h in hashes & BLOCKED_ROOTS.keys()
        ),
    }


def inspect_pem(data: bytes, source: str) -> dict:
    if len(data) > MAX_BUNDLE_BYTES:
        raise ValueError("CA store exceeds size limit")
    try:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.load_verify_locations(cadata=data.decode("ascii"))
        if not context.get_ca_certs(binary_form=True):
            raise ValueError("PEM contains no CA certificate")
    except (UnicodeError, ssl.SSLError) as exc:
        raise ValueError("CA store is not a valid X509 bundle") from exc
    certificates = [
        base64.b64decode(re.sub(rb"\s+", b"", part), validate=True)
        for part in PEM.findall(data)
    ]
    return inspect_der(certificates, source)


def read_bundle(path: Path) -> bytes:
    with path.open("rb") as handle:
        data = handle.read(MAX_BUNDLE_BYTES + 1)
    if len(data) > MAX_BUNDLE_BYTES:
        raise ValueError("CA store exceeds size limit")
    return data


def inspect_seed_wheels(seed_dir: Path) -> tuple[list[dict], list[str]]:
    stores, errors = [], []
    for wheel in sorted(seed_dir.glob("*.whl")):
        try:
            match = re.fullmatch(r"pip-([^-]+)-.*\.whl", wheel.name)
            if match and not version_at_least(match.group(1), MINIMUMS["pip"]):
                errors.append(f"ensurepip seed below {MINIMUMS['pip']}")
            with zipfile.ZipFile(wheel) as archive:
                for member in archive.infolist():
                    if member.filename.endswith("certifi/cacert.pem"):
                        if member.file_size > MAX_BUNDLE_BYTES:
                            raise ValueError("seed CA store exceeds size limit")
                        stores.append(
                            inspect_pem(archive.read(member), f"ensurepip:{wheel.name}")
                        )
        except (OSError, ValueError, zipfile.BadZipFile) as exc:
            errors.append(f"ensurepip: {type(exc).__name__}")
    return stores, errors


def audit(*, uv_binary: str | None = None) -> dict:
    report = {
        "status": "passed",
        "stores": [],
        "packages": {},
        "ensurepip": [],
        "errors": [],
    }
    stores = report["stores"]
    errors = report["errors"]
    system_paths = set()
    default = ssl.get_default_verify_paths()
    for candidate in (
        default.cafile,
        default.openssl_cafile,
        "/etc/ssl/certs/ca-certificates.crt",
    ):
        if candidate and Path(candidate).is_file():
            system_paths.add(Path(candidate).resolve())
    for candidate in sorted(system_paths):
        try:
            stores.append(inspect_pem(read_bundle(candidate), f"system:{candidate}"))
        except (OSError, ValueError) as exc:
            errors.append(f"system CA store: {type(exc).__name__}")
    try:
        context = ssl.create_default_context()
        # OpenSSL capath directories are lazy: load the public OS bundle too.
        for candidate in sorted(system_paths):
            context.load_verify_locations(cafile=str(candidate))
        stores.append(
            inspect_der(context.get_ca_certs(binary_form=True), "python:effective")
        )
    except (OSError, ValueError, ssl.SSLError) as exc:
        errors.append(f"effective CA store: {type(exc).__name__}")
    for variable in (
        "REQUESTS_CA_BUNDLE",
        "CURL_CA_BUNDLE",
        "PIP_CERT",
        "UV_CA_BUNDLE",
        "NODE_EXTRA_CA_CERTS",
    ):
        candidate = os.environ.get(variable)
        if candidate:
            try:
                stores.append(
                    inspect_pem(read_bundle(Path(candidate)), f"environment:{variable}")
                )
            except (OSError, ValueError) as exc:
                errors.append(f"{variable}: {type(exc).__name__}")
    system_aliases = set()
    for name in ("certifi", "pip._vendor.certifi"):
        try:
            if name.startswith("pip.") and importlib.util.find_spec("pip") is None:
                continue
            if importlib.util.find_spec(name) is None:
                continue
            module = importlib.import_module(name)
            path = Path(module.where())
            store = inspect_pem(read_bundle(path), f"python:{name}")
            stores.append(store)
            if path.resolve() in system_paths and not store["blocked_roots"]:
                if name == "certifi":
                    system_aliases.add("certifi")
                elif name == "pip._vendor.certifi":
                    # Distro pip can retain its upstream version while its
                    # verifier actually delegates to the patched OS store.
                    pip_module = importlib.util.find_spec("pip")
                    if (
                        pip_module
                        and pip_module.origin
                        and Path(pip_module.origin).is_relative_to(
                            "/usr/lib/python3/dist-packages/pip"
                        )
                    ):
                        system_aliases.add("pip")
        except (ImportError, OSError, ValueError) as exc:
            errors.append(f"{name}: {type(exc).__name__}")
    for name, minimum in MINIMUMS.items():
        try:
            version = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            continue
        patched = version_at_least(version, minimum)
        # Debian/Ubuntu certifi can intentionally delegate to a patched OS store.
        system_alias = name in system_aliases
        report["packages"][name] = {
            "version": version,
            "minimum": minimum,
            "system_alias": system_alias,
        }
        if not patched and not system_alias:
            errors.append(f"{name} below {minimum}")
    seed_dir = Path(sysconfig.get_path("stdlib")) / "ensurepip" / "_bundled"
    seed_stores, seed_errors = inspect_seed_wheels(seed_dir)
    report["ensurepip"] = seed_stores
    errors.extend(seed_errors)
    executable = uv_binary or shutil.which("uv")
    if executable:
        try:
            result = subprocess.run(
                [executable, "--version"],
                check=True,
                capture_output=True,
                text=True,
                timeout=10,
            )
            match = re.match(r"uv (\S+)", result.stdout)
            version = match.group(1) if match else "unknown"
            report["uv_binary"] = {"version": version, "minimum": MINIMUMS["uv"]}
            if not version_at_least(version, MINIMUMS["uv"]):
                errors.append(f"uv binary below {MINIMUMS['uv']}")
        except (OSError, subprocess.SubprocessError) as exc:
            errors.append(f"uv binary: {type(exc).__name__}")
    for store in stores + report["ensurepip"]:
        if store["blocked_roots"]:
            errors.append(f"compromised root in {store['source']}")
    if errors:
        report["status"] = "failed"
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--uv-binary",
        help="Also check this installed uv executable, without network access",
    )
    parser.add_argument(
        "--pem",
        type=Path,
        help="Check only this public PEM bundle (e.g. exported Java cacerts)",
    )
    args = parser.parse_args()
    if args.pem:
        try:
            store = inspect_pem(read_bundle(args.pem), "explicit-public-bundle")
            report = {
                "status": "failed" if store["blocked_roots"] else "passed",
                "stores": [store],
            }
        except (OSError, ValueError) as exc:
            report = {"status": "failed", "errors": [type(exc).__name__]}
    else:
        report = audit(uv_binary=args.uv_binary)
    print(json.dumps(report, sort_keys=True))
    return int(report["status"] != "passed")


if __name__ == "__main__":
    raise SystemExit(main())
