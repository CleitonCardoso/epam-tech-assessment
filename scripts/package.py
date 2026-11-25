"""Build the source archive for delivery: stdlib only, deterministic, exclusion-driven.

    python scripts/package.py                  # -> dist/epam-tech-assessment.zip
    python scripts/package.py --out X.zip

The assessment asks for source in a zip without .venv or node_modules. This script also drops
local caches, build output, the real .env, and anything else that would make the archive either
larger than the source or machine-specific. `.env.example` stays in: it is part of the source.

The default destination is `dist/` **inside the repository**, which is gitignored. An earlier
version wrote the archive as a sibling of the repo, which polluted the shared workspace root
where every other project lives — do not reintroduce that.

Verification is built in: after writing, the archive is re-opened and its entries are checked
for the excluded names, so a silent exclusion bug cannot ship.
"""

from __future__ import annotations

import argparse
import os
import sys
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

EXCLUDED_DIRS = {
    ".git",
    ".venv",
    "venv",
    "env",
    "node_modules",
    "dist",
    "build",
    ".tmp",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".idea",
    ".vscode",
    ".rehearsal",
    # Terraform downloads providers here: hundreds of MB, and reproducible via `terraform init`.
    ".terraform",
}

EXCLUDED_FILES = {
    ".env",
    ".DS_Store",
    "Thumbs.db",
}

EXCLUDED_SUFFIXES = (
    ".pyc",
    ".pyo",
    ".log",
    ".zip",
    ".tfstate",
    ".tfplan",
)

# Files the reviewer must receive; their absence fails the build rather than the review.
REQUIRED = [
    "README.md",
    "pyproject.toml",
    "requirements.txt",
    "requirements-dev.txt",
    "Dockerfile",
    "compose.yaml",
    "compose.prod.yaml",
    ".env.example",
    "app/main.py",
    "app/worker.py",
    "app/config.py",
    "app/models.py",
    "app/registry.py",
    "app/api/documents.py",
    "app/storage/objects.py",
    "app/storage/jobs.py",
    "app/queue.py",
    "app/processing.py",
    "app/parsers/base.py",
    "app/parsers/pypdf_parser.py",
    "app/parsers/gemini_parser.py",
    "app/providers/fake.py",
    "app/providers/gemini.py",
    "infra/versions.tf",
    "infra/modules/s3/main.tf",
    "infra/modules/s3/variables.tf",
    "infra/modules/s3/outputs.tf",
    "infra/modules/ec2-app/main.tf",
    "infra/modules/ec2-app/user_data.sh.tftpl",
    "infra/envs/local/main.tf",
    "infra/envs/prod/main.tf",
    "infra/envs/prod/variables.tf",
    "infra/envs/prod/terraform.tfvars.example",
    "scripts/smoke.sh",
    "scripts/stress.py",
    "scripts/resilience.sh",
    "docs/PERFORMANCE.md",
    "tests/unit/test_hardening.py",
    "tests/integration/test_concurrency.py",
    "compose.prod.yaml",
    "scripts/make_samples.py",
    "samples/sample-text.pdf",
    "samples/sample-blank.pdf",
    "samples/sample-corrupt.pdf",
    "tests/conftest.py",
    "tests/unit/test_contract.py",
    "tests/integration/test_api.py",
    "tests/e2e/test_proxy.py",
    "docs/CONTRACT.md",
    "docs/DECISIONS.md",
    "docs/PROGRESS.md",
    "frontend/Dockerfile",
    "frontend/nginx.conf",
    "frontend/package.json",
    "frontend/package-lock.json",
    "frontend/src/App.tsx",
]


def is_excluded(relative: Path) -> bool:
    parts = set(relative.parts)
    if parts & EXCLUDED_DIRS:
        return True
    if relative.name in EXCLUDED_FILES:
        return True
    # Covers terraform.tfstate and terraform.tfstate.backup alike.
    if ".tfstate" in relative.name:
        return True
    return relative.name.endswith(EXCLUDED_SUFFIXES)


def collect() -> list[Path]:
    files: list[Path] = []
    for path in sorted(REPO_ROOT.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(REPO_ROOT)
        if is_excluded(relative):
            continue
        files.append(relative)
    return files


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out",
        default=str(REPO_ROOT / "dist" / f"{REPO_ROOT.name}.zip"),
        help="destination archive (default: dist/<repo-name>.zip inside the repo, gitignored)",
    )
    args = parser.parse_args()
    destination = Path(args.out).resolve()

    files = collect()
    present = {str(f).replace(os.sep, "/") for f in files}
    missing = [name for name in REQUIRED if name not in present]
    if missing:
        print("REFUSING TO PACKAGE — missing required files:", file=sys.stderr)
        for name in missing:
            print(f"  - {name}", file=sys.stderr)
        return 1

    destination.parent.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for relative in files:
            archive.write(REPO_ROOT / relative, arcname=f"{REPO_ROOT.name}/{relative}")

    # Re-open and verify: exclusions are a claim until checked.
    with zipfile.ZipFile(destination) as archive:
        names = archive.namelist()
        bad = [
            name
            for name in names
            if any(f"/{part}/" in name or name.endswith(f"/{part}") for part in EXCLUDED_DIRS)
            or name.endswith((".pyc", ".log"))
            or name.endswith("/.env")
        ]
        corrupt = archive.testzip()

    size_mb = destination.stat().st_size / (1024 * 1024)
    print(f"wrote {destination}")
    print(f"  entries : {len(names)}")
    print(f"  size    : {size_mb:.2f} MB")
    if corrupt:
        print(f"  ZIP READ-BACK FAILED at {corrupt}", file=sys.stderr)
        return 1
    if bad:
        print("  EXCLUDED CONTENT LEAKED:", file=sys.stderr)
        for name in bad[:10]:
            print(f"    - {name}", file=sys.stderr)
        return 1
    print("  verification: no excluded paths, archive reads back cleanly")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
