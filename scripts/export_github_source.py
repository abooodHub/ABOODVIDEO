"""Create a clean publication directory without altering the source Git history/index."""
import argparse
from pathlib import Path
import shutil
import subprocess

from check_repository_secrets import inspect, local_secrets

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path, help="New directory; existing paths are refused")
    args = parser.parse_args()
    output = args.output.resolve()
    if output.exists():
        raise SystemExit("Output must be a new directory")
    candidates = subprocess.check_output(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"], cwd=ROOT,
    ).decode().split("\0")
    selected = []
    secrets = local_secrets()
    for name in sorted(set(candidates) - {""}):
        path = ROOT / name
        if not path.is_file():
            continue
        if path.is_symlink() or not path.resolve().is_relative_to(ROOT):
            raise SystemExit("Unsafe source path: " + name)
        ignored = subprocess.run(["git", "check-ignore", "--no-index", "-q", "--", name], cwd=ROOT)
        if ignored.returncode == 0:
            continue
        if inspect(name, path.read_bytes(), secrets):
            raise SystemExit("Credential check blocked export: " + name + " [redacted]")
        selected.append((name, path))
    output.mkdir(parents=True)
    for name, source in selected:
        target = output / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    # Initialize an empty repository, without creating a commit or a remote.
    subprocess.run(["git", "init", "-b", "main", str(output)], check=True, stdout=subprocess.DEVNULL)
    print(f"Exported {len(selected)} files to {output}; no credentials, media, local history or commit included.")


if __name__ == "__main__":
    main()
