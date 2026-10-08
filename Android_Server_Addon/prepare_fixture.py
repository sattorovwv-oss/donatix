"""Build a pristine regression fixture; never execute or copy credentials."""
import argparse
import shutil
from pathlib import Path

from stage import check_site


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    current = root.parent / "Server/donatix"
    if args.destination.exists():
        raise SystemExit("Fixture destination must be new")
    for folder in (current, root / "verification_original/donatix"):
        for p in folder.rglob("*"):
            if not p.is_file() or "__pycache__" in p.parts:
                continue
            rel = p.relative_to(folder)
            if not (p.suffix in {".py", ".html", ".txt"} or "static" in rel.parts) or any(x.startswith(".") for x in rel.parts):
                continue
            target = args.destination / "donatix" / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(p, target)
    check_site(args.destination)
    print("Pristine source fixture prepared; no credentials or databases copied")


if __name__ == "__main__":
    main()
