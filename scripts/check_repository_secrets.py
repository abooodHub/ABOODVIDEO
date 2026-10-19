"""Check publishable files, staged blobs and optionally HEAD history; never print secrets."""
import argparse
import ast
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[1]
PATTERNS = {
    "private signing key": re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "provider credential": re.compile(rb"(?:ak_live_[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{50,}|AKIA[0-9A-Z]{16}|sk_live_[A-Za-z0-9]{20,})"),
}


def git(*args):
    return subprocess.check_output(["git", *args], cwd=ROOT, stderr=subprocess.DEVNULL)


def local_secrets():
    path = ROOT / ".env"
    if not path.is_file():
        return []
    values = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        if line.lstrip().startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip().strip("\"'")
        if len(value) >= 16 and re.search(r"PASSWORD|SECRET|SALT|TOKEN|API_KEY|HMAC", key, re.I):
            values.append(value.encode())
    return values


def inspect(name, data, secrets=()):
    if b"\0" in data:
        return []
    findings = []
    for label, pattern in PATTERNS.items():
        for match in pattern.finditer(data):
            findings.append((data[:match.start()].count(b"\n") + 1, label))
    for secret in secrets:
        if secret in data:
            findings.append((data[:data.index(secret)].count(b"\n") + 1, "matches private local configuration"))
    # Real account setup helpers must not create predictable owner passwords.
    if name.startswith("scripts/") and name.endswith(".py"):
        try:
            tree = ast.parse(data.decode("utf-8-sig"))
        except (SyntaxError, UnicodeDecodeError):
            return findings
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id == "hash_password" and node.args
                    and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)):
                findings.append((node.lineno, "fixed account password"))
            if isinstance(node, ast.Dict):
                for key, value in zip(node.keys, node.values):
                    if (isinstance(key, ast.Constant) and key.value in {"password", "api_key", "ssh_password"}
                            and isinstance(value, ast.Constant) and isinstance(value.value, str) and value.value):
                        findings.append((value.lineno, "fixed login credential"))
    return findings


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--index", action="store_true", help="Also inspect the exact staged file contents")
    parser.add_argument("--history", action="store_true", help="Inspect all blobs reachable from HEAD (not private local snapshot refs)")
    args = parser.parse_args()
    secrets = local_secrets()
    sources = []
    names = set(git("ls-files", "--cached", "--others", "--exclude-standard", "-z").decode().split("\0"))
    for name in sorted(names - {""}):
        path = ROOT / name
        if path.is_file():
            sources.append(("working", name, path.read_bytes()))
    if args.index:
        for name in git("ls-files", "-z").decode().split("\0"):
            if name:
                sources.append(("index", name, git("show", ":" + name)))
    if args.history:
        has_head = subprocess.run(["git", "rev-parse", "--verify", "HEAD"], cwd=ROOT,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
        if not has_head:
            print("No commit yet; historical scanning starts after the first commit.")
        for line in (git("rev-list", "--objects", "HEAD").decode().splitlines() if has_head else []):
            oid, _, name = line.partition(" ")
            if name and git("cat-file", "-t", oid).strip() == b"blob":
                sources.append(("history", name, git("cat-file", "blob", oid)))
    count = 0
    for scope, name, data in sources:
        for line, label in inspect(name, data, secrets):
            print(f"{scope}: {name}:{line}: {label} [redacted]")
            count += 1
    print(f"Checked {len(sources)} file versions; {count} credential findings.")
    return int(count != 0)


if __name__ == "__main__":
    raise SystemExit(main())
