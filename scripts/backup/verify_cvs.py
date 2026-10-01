"""Check that restored CV files decrypt with the configured key.

A restored data/cvs directory is only worth something if CV_ENCRYPTION_KEY
(gosha/cv_crypto.py) still opens it — the backups hold ciphertext, and the
key lives in .env, not in the backup. Prints counts, never CV text.

    CV_ENCRYPTION_KEY=... python scripts/backup/verify_cvs.py /restore-out/data/cvs

Exit 0 when every *.enc file decrypts (and there is at least one), 1 otherwise.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from gosha import cv_crypto  # noqa: E402


def verify(cv_dir: Path) -> tuple[int, list[str]]:
    """(files that decrypt, names of files that do not)."""
    ok, bad = 0, []
    for path in sorted(cv_dir.glob("*.enc")):
        try:
            cv_crypto.decrypt(path.read_bytes())
            ok += 1
        except Exception as exc:  # report every failure, not just the first
            bad.append(f"{path.name}: {type(exc).__name__}")
    return ok, bad


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__.strip().splitlines()[-3], file=sys.stderr)
        return 2
    cv_dir = Path(sys.argv[1])
    if cv_crypto.cipher() is None:
        print("CV_ENCRYPTION_KEY is not set: nothing to verify with.", file=sys.stderr)
        return 1
    ok, bad = verify(cv_dir)
    print(f"{ok} CV file(s) decrypt, {len(bad)} do not")
    for line in bad:
        print(f"  FAILED {line}")
    return 0 if ok and not bad else 1


if __name__ == "__main__":
    sys.exit(main())
