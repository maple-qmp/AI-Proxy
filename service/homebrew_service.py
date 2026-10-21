"""Install a local Homebrew formula for the current AI-Proxy checkout."""

import argparse
import hashlib
import os
from pathlib import Path
import platform
import shlex
import shutil
import subprocess
import tarfile
import tempfile
import tomllib


ROOT = Path(__file__).resolve().parents[1]
TAP = "local/ai-proxy"
FORMULA = f"{TAP}/ai-proxy"


def ruby_quote(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def render_formula(archive: Path, checksum: str, version: str, logs: Path) -> str:
    return f'''class AiProxy < Formula
  desc "Run the local AI-Proxy checkout"
  url {ruby_quote(archive.as_uri())}
  version {ruby_quote(version)}
  sha256 {ruby_quote(checksum)}

  def install
    bin.install "ai-proxy"
  end

  service do
    run [opt_bin/"ai-proxy"]
    keep_alive true
    working_dir {ruby_quote(str(ROOT))}
    log_path {ruby_quote(str(logs / "stdout.log"))}
    error_log_path {ruby_quote(str(logs / "stderr.log"))}
  end
end
'''


def run(*args: str, capture: bool = False) -> str:
    env = {**os.environ, "HOMEBREW_NO_AUTO_UPDATE": "1"}
    result = subprocess.run(args, check=True, text=True, env=env,
                            stdout=subprocess.PIPE if capture else None)
    return result.stdout.strip() if capture else ""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepare-only", type=Path, metavar="DIRECTORY")
    args = parser.parse_args()
    if platform.system() != "Darwin" or os.geteuid() == 0:
        parser.error("Run on macOS as your login user, without sudo")
    python = ROOT / ".venv/bin/python"
    if not python.is_file():
        parser.error("Run uv sync first to create .venv")
    brew = shutil.which("brew")
    if not brew:
        parser.error("Homebrew is required")

    home = Path.home()
    output = args.prepare_only.resolve() if args.prepare_only else home / "Library/Application Support/ai-proxy-service"
    logs = home / "Library/Logs/ai-proxy"
    output.mkdir(parents=True, exist_ok=True)
    archive = output / "ai-proxy-service.tar.gz"
    launcher = f"#!/bin/sh\nset -eu\ncd {shlex.quote(str(ROOT))}\nexec {shlex.quote(str(python))} -m ai_proxy\n"
    with tempfile.TemporaryDirectory(prefix="ai-proxy-service-") as stage:
        executable = Path(stage) / "ai-proxy"
        executable.write_text(launcher)
        executable.chmod(0o755)
        with tarfile.open(archive, "w:gz") as package:
            package.add(executable, arcname="ai-proxy")

    checksum = hashlib.sha256(archive.read_bytes()).hexdigest()
    version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    formula = output / "ai-proxy.rb"
    formula.write_text(render_formula(archive, checksum, version, logs))
    if args.prepare_only:
        print(f"Generated service package and formula in {output}; no service installed or started.")
        return

    logs.mkdir(parents=True, exist_ok=True)
    repository = Path(run(brew, "--repository", capture=True))
    tap_directory = repository / "Library/Taps/local/homebrew-ai-proxy"
    if not tap_directory.exists():
        run(brew, "tap-new", "--no-git", TAP)
    formulas = tap_directory / "Formula"
    formulas.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(formula, formulas / "ai-proxy.rb")
    installed = any(line.split()[0] == "ai-proxy" for line in run(
        brew, "list", "--formula", "--versions", capture=True).splitlines() if line.split())
    run(brew, "reinstall" if installed else "install", "--formula", FORMULA)
    print(f"Installed {FORMULA}. Start or restart with: brew services restart {FORMULA}")


if __name__ == "__main__":
    main()
