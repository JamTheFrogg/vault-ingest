"""Assemble the bundled Python engine: a relocatable CPython, the locked dependencies, the vault_ingest package.

    python scripts/build_engine.py [--out DIR]           build the folder for this OS (default: build/engine)
    python scripts/build_engine.py --check [--out DIR]   only run the self-check on a folder that is already built

The NVIDIA CUDA wheels are not in the folder; the app downloads them on first run.
After changing dependencies in pyproject.toml, regenerate the lock file:

    echo faster-whisper | uv pip compile pyproject.toml - --extra app --universal --python-version 3.12 --generate-hashes -o requirements-app.txt
"""
from __future__ import annotations

import argparse
import hashlib
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
LOCK = REPO / "requirements-app.txt"
APP_FILES = ("vault_ingest", "prompts", "configs", "companion.toml")

# https://github.com/astral-sh/python-build-standalone/releases/tag/20261110 (hashes from its SHA256SUMS)
PBS_TAG = "20261110"
PYTHON = "3.12.16"
PBS_URL = ("https://github.com/astral-sh/python-build-standalone/releases/download/"
           "{tag}/cpython-{python}+{tag}-{target}-install_only_stripped.tar.gz")
TARGETS = {  # (sys.platform, machine) -> (target, sha256 of the archive)
    ("win32", "amd64"): ("x86_64-pc-windows-msvc", "355ee61b8207b91574491b1c6dc9b0ebb4408fc56bfafe498f3ef439314b52dc"),
    ("linux", "x86_64"): ("x86_64-unknown-linux-gnu", "6a9e62dad033790aef722dbd370e0ce76ce17b73627ba3ae679f537049350b39"),
    ("darwin", "arm64"): ("aarch64-apple-darwin", "2f11b8aa3726bdbd87dd9eb76568412faddf4f47e26913fa45d6a2e424972acd"),
}

# Runs inside the engine: every dependency pyproject.toml declares for this OS must be installed.
LOCK_COVERS_PYPROJECT = """
import sys, tomllib
from importlib.metadata import PackageNotFoundError, version
from pip._vendor.packaging.requirements import Requirement

project = tomllib.load(open(sys.argv[1], "rb"))["project"]
extras = project.get("optional-dependencies", {})
missing = []
for line in project["dependencies"] + [dep for name in extras if name != "dev" for dep in extras[name]]:
    req = Requirement(line)
    if req.name.startswith("nvidia-") or (req.marker and not req.marker.evaluate()):
        continue
    try:
        found = req.specifier.contains(version(req.name), prereleases=True)
    except PackageNotFoundError:
        found = False
    if not found:
        missing.append(line)
if missing:
    sys.exit("requirements-app.txt is out of date (see the top of scripts/build_engine.py); not covered: "
             + ", ".join(missing))
"""


def python_exe(engine: Path) -> Path:
    return engine / "python.exe" if os.name == "nt" else engine / "bin" / "python3"


def site_packages(engine: Path) -> Path:
    if os.name == "nt":
        return engine / "Lib" / "site-packages"
    return engine / "lib" / f"python{PYTHON.rsplit('.', 1)[0]}" / "site-packages"


def run_engine(engine: Path, *args: str) -> None:
    # -I: no PYTHON* variables, no user site-packages, no current directory on sys.path.
    # Without it a check started in the repo would import the repo's vault_ingest, not the bundled one.
    code = subprocess.run([str(python_exe(engine)), "-I", *args]).returncode
    if code:
        raise SystemExit(code)  # the engine has already printed why


def fetch_python(engine: Path) -> None:
    """Download the pinned interpreter archive, verify its SHA-256, unpack it as `engine`."""
    key = (sys.platform, platform.machine().lower())
    if key not in TARGETS:
        raise SystemExit(f"No pinned interpreter for {key}. Add it to TARGETS in {Path(__file__).name}.")
    target, expected = TARGETS[key]
    url = PBS_URL.format(tag=PBS_TAG, python=PYTHON, target=target)
    engine.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=engine.parent) as tmp:
        archive = Path(tmp) / "python.tar.gz"
        print(f"Downloading {url}")
        digest = hashlib.sha256()
        with urllib.request.urlopen(url, timeout=120) as response, open(archive, "wb") as fh:
            while block := response.read(1 << 20):
                digest.update(block)
                fh.write(block)
        if digest.hexdigest() != expected:
            raise SystemExit(f"SHA-256 mismatch for {url}: got {digest.hexdigest()}, expected {expected}")
        with tarfile.open(archive) as tar:
            tar.extractall(tmp, filter="data")
        (Path(tmp) / "python").rename(engine)  # the archive holds one top folder, `python/`


def build(engine: Path) -> None:
    if engine.exists():
        raise SystemExit(f"{engine} already exists. Remove it or pass another --out; this script deletes nothing.")
    fetch_python(engine)
    run_engine(engine, "-m", "pip", "install", "--require-hashes", "--disable-pip-version-check",
               "--no-warn-script-location", "-r", str(LOCK))
    site = site_packages(engine)
    if (site / "nvidia").exists():
        raise SystemExit("NVIDIA wheels are in requirements-app.txt; they are downloaded on first run, not bundled.")
    run_engine(engine, "-c", LOCK_COVERS_PYPROJECT, str(REPO / "pyproject.toml"))

    # config.PROJECT_DIR is the parent of the package: prompts/ and companion.toml must sit beside it.
    app = engine / "app"
    for name in APP_FILES:
        source = REPO / name
        if source.is_dir():
            shutil.copytree(source, app / name, ignore=shutil.ignore_patterns("__pycache__"))
        else:
            shutil.copy2(source, app / name)
    # A relative .pth entry keeps the folder relocatable; a venv or an editable install would not be.
    (site / "vault_ingest.pth").write_text(os.path.relpath(app, site) + "\n", encoding="utf-8")


def check(engine: Path) -> None:
    """Imports that need native libraries, then the CLI: `--help` touches no settings, vault or network."""
    if not python_exe(engine).is_file():
        raise SystemExit(f"No engine at {engine}. Build it first: python scripts/build_engine.py")
    run_engine(engine, "-c", "import ctranslate2, faster_whisper, vault_ingest.cli")
    run_engine(engine, "-m", "vault_ingest", "--help")
    size = sum(p.stat().st_size for p in engine.rglob("*") if p.is_file() and not p.is_symlink())
    print(f"Engine OK: {engine} ({size / 1e6:.0f} MB)")


def main() -> int:
    ap = argparse.ArgumentParser(description="Assemble the bundled Python engine folder.")
    ap.add_argument("--out", type=Path, default=REPO / "build" / "engine", help="engine folder (default: build/engine)")
    ap.add_argument("--check", action="store_true", help="only run the self-check on an existing folder")
    args = ap.parse_args()
    engine = args.out.resolve()
    if not args.check:
        build(engine)
    check(engine)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
