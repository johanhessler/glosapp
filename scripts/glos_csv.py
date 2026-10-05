"""Gemensam inläsning av glos-CSV:er (används av build_index.py och generate_sentences.py)."""
import csv
import io
import re
from pathlib import Path

header_q = {"svenska", "swedish", "sv", "ord", "fråga"}
header_a = {"engelska", "english", "en", "svar", "översättning"}


def title_from_file(name: str) -> str:
    s = re.sub(r"\.csv$", "", name, flags=re.I)
    s = re.sub(r"^\d{4}-\d{2}-\d{2}[-_ ]*", "", s)
    s = re.sub(r"^\d{4}-v\d{1,2}[-_ ]*", "", s, flags=re.I)
    s = re.sub(r"[-_]+", " ", s).strip()
    return (s[:1].upper() + s[1:]) if s else name


def read_list(path: Path) -> dict:
    """Returnerar {"title", "theme", "words": [{"sv", "en"}]}.

    Stödda kommentarsrader överst i filen:
        # titel: Djur på bondgården
        # tema: fotboll och Premier League     (ersätter interests i sentences_config.json för just den listan)
    """
    text = path.read_text(encoding="utf-8-sig")
    meta = {"titel": None, "tema": None}
    body = []
    for line in text.splitlines():
        t = line.strip()
        if not t:
            continue
        m = re.match(r"^#\s*(titel|tema)\s*:\s*(.+)$", t, flags=re.I)
        if m:
            meta[m.group(1).lower()] = m.group(2).strip()
            continue
        if t.startswith("#"):
            continue
        body.append(line)

    words = []
    if body:
        first = body[0]
        delim = "\t" if "\t" in first else (";" if ";" in first else ",")
        rows = list(csv.reader(io.StringIO("\n".join(body)), delimiter=delim))
        if rows and len(rows[0]) >= 2:
            if rows[0][0].strip().lower() in header_q and rows[0][1].strip().lower() in header_a:
                rows = rows[1:]
        words = [
            {"sv": r[0].strip(), "en": r[1].strip()}
            for r in rows
            if len(r) >= 2 and r[0].strip() and r[1].strip()
        ]

    return {
        "title": meta["titel"] or title_from_file(path.name),
        "theme": meta["tema"],
        "words": words,
    }
