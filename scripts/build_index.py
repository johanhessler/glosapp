#!/usr/bin/env python3
"""Bygger glosor/index.json från alla CSV-filer i glosor/.

Körs automatiskt av GitHub-workflowen vid varje push, men kan också köras lokalt:
    python scripts/build_index.py
"""
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from glos_csv import read_list

root_dir = Path(__file__).resolve().parent.parent
list_dir = root_dir / "glosor"
index_file = list_dir / "index.json"


def last_changed(path: Path) -> str:
    """Senaste commit-datum för filen (fungerar i GitHub Actions), annars filens mtime."""
    try:
        out = subprocess.run(
            ["git", "log", "-1", "--format=%cs", "--", str(path)],
            cwd=root_dir, capture_output=True, text=True, check=True,
        ).stdout.strip()
        if out:
            return out
    except (OSError, subprocess.CalledProcessError):
        pass
    return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).strftime("%Y-%m-%d")


def main() -> None:
    lists = []
    for path in sorted(list_dir.glob("*.csv"), key=lambda p: p.name, reverse=True):
        info = read_list(path)
        if not info["words"]:
            print(f"Hoppar över tom fil: {path.name}")
            continue
        sentences_file = path.with_suffix(".sentences.json")
        lists.append({
            "file": path.name,
            "title": info["title"],
            "count": len(info["words"]),
            "updated": last_changed(path),
            "has_sentences": sentences_file.exists(),
        })

    index_file.write_text(
        json.dumps({"lists": lists}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"Skrev {index_file.relative_to(root_dir)} med {len(lists)} listor")


if __name__ == "__main__":
    main()
