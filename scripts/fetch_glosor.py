#!/usr/bin/env python3
"""Hämtar lärarens övningar från glosor.eu och skriver nya CSV-filer i glosor/.

Loggar in med klassens konto, läser översikten och skriver en CSV per övning
som inte redan finns. Committar inte själv – i GitHub Action gör workflowen det.

    python scripts/fetch_glosor.py              # skriv nya CSV-filer
    python scripts/fetch_glosor.py --dry-run    # visa bara vad som skulle skrivas

Inloggningen läses från miljövariabler (GitHub Secrets i Action) eller lokalt från
den git-ignorerade filen local/.env:
    GLOSOR_EU_USER=...
    GLOSOR_EU_PASS=...
Värdena skrivs aldrig ut. Saknas de hoppar skriptet över hämtningen (exit 0).
Stdlib only.
"""
import argparse
import html
import http.cookiejar
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path

root_dir = Path(__file__).resolve().parent.parent
list_dir = root_dir / "glosor"
env_file = root_dir / "local" / ".env"

base_url = "https://glosor.eu/"
login_url = "https://glosor.eu/logga-in/"
user_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0 Safari/537.36"

# Länk i översikten: <a href="https://glosor.eu/ovning/<slug>.<id>.html">...</a>
exercise_link_re = re.compile(r'<a[^>]+href="((?:https://glosor\.eu)?/ovning/([\w-]+)\.(\d+)\.html)"[^>]*>(.*?)</a>', re.S)
# Ordpar på övningssidan: <span id="q0ca"><span class="exl-c2">engelska</span> <span class="exl-c2">svenska</span></span>
pair_re = re.compile(r'<span id="q\d+ca">\s*<span class="exl-c2">(.*?)</span>\s*<span class="exl-c2">(.*?)</span>\s*</span>', re.S)
h1_re = re.compile(r"<h1[^>]*>(.*?)</h1>", re.S)
# "LÄXFÖRHÖR 7/10", "LÄSFÖRHÖR 30/9" i titeln, "laxforhor-7-10" i sluggen
title_suffix_re = re.compile(r"\s*\b(?:LÄX|LÄS|LAX|LAS)F[ÖO]RH[ÖO]R\b.*$", re.I)
title_date_re = re.compile(r"\b(\d{1,2})/(\d{1,2})\b")
slug_re = re.compile(r"^(?P<name>.*?)(?:-(?:lax|las)forhor)?(?:-(?P<d>\d{1,2})-(?P<m>\d{1,2}))?$")


# ---------------------------------------------------------------- inloggning
def load_credentials() -> tuple[str, str] | None:
    values = {}
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                values[k.strip()] = v.strip().strip('"').strip("'")
    user = os.environ.get("GLOSOR_EU_USER") or values.get("GLOSOR_EU_USER", "")
    password = os.environ.get("GLOSOR_EU_PASS") or values.get("GLOSOR_EU_PASS", "")
    if not user or not password:
        return None
    return user, password


def make_opener() -> urllib.request.OpenerDirector:
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    opener.addheaders = [("User-Agent", user_agent), ("Accept-Language", "sv-SE,sv;q=0.9")]
    return opener


def fetch(opener, url: str, data: dict | None = None) -> str:
    body = urllib.parse.urlencode(data).encode("utf-8") if data is not None else None
    with opener.open(url, data=body, timeout=30) as resp:
        return resp.read().decode("utf-8", "replace")


def is_logged_in(page: str) -> bool:
    return "Logga ut" in page


def login(opener, user: str, password: str) -> str:
    """Loggar in och returnerar översiktssidan."""
    fetch(opener, base_url)  # sätter sessionskakan
    page = fetch(opener, login_url, {"username": user, "psw": password, "check": "0"})
    if not is_logged_in(page):
        page = fetch(opener, base_url)
    if not is_logged_in(page):
        sys.exit("Inloggningen misslyckades – kontrollera GLOSOR_EU_USER/GLOSOR_EU_PASS")
    return page


# ---------------------------------------------------------------- tolkning
def clean(fragment: str) -> str:
    """HTML → text. Kommatecken mellan alternativ (<span class="exl-com">,</span>) blir |."""
    fragment = re.sub(r'<span class="exl-com">\s*,\s*</span>', "|", fragment)
    text = html.unescape(re.sub(r"<[^>]+>", "", fragment))
    text = re.sub(r"\s+", " ", text).strip()
    return "|".join(p.strip() for p in text.split("|") if p.strip())


def parse_overview(page: str) -> list[dict]:
    seen, out = set(), []
    for href, slug, ex_id, label in exercise_link_re.findall(page):
        if ex_id in seen:
            continue
        seen.add(ex_id)
        out.append({"url": urllib.parse.urljoin(base_url, href), "slug": slug, "id": ex_id, "label": clean(label)})
    return out


def parse_exercise(page: str) -> dict:
    h1 = h1_re.search(page)
    return {
        "heading": clean(h1.group(1)) if h1 else "",
        "pairs": [(clean(en), clean(sv)) for en, sv in pair_re.findall(page)],
    }


def nice_title(heading: str) -> str:
    """'FOREVER YOUNG- LÄXFÖRHÖR 7/10' → 'Forever Young', 'MY HOBBY- MY JOB …' → 'My Hobby – My Job'."""
    t = title_suffix_re.sub("", heading)
    t = title_date_re.sub("", t)
    t = re.sub(r"\s*-\s+|\s+-\s*", " – ", t)
    t = t.strip(" -–:,")
    if t.isupper():
        t = " ".join(w[:1].upper() + w[1:].lower() for w in t.split(" "))
    return t


def guess_year(day: int, month: int, today: date) -> int:
    """Året som ger datumet närmast idag (läsår går över nyår)."""
    candidates = []
    for y in (today.year - 1, today.year, today.year + 1):
        try:
            candidates.append((abs((date(y, month, day) - today).days), y))
        except ValueError:
            pass
    return min(candidates)[1] if candidates else today.year


def file_name_for(ex: dict, heading: str, today: date) -> str:
    m = slug_re.match(ex["slug"])
    name = m.group("name") if m else ex["slug"]
    d, mo = (m.group("d"), m.group("m")) if m else (None, None)
    if not d:
        hm = title_date_re.search(heading)
        if hm:
            d, mo = hm.groups()
    if d:
        day, month = int(d), int(mo)
        when = date(guess_year(day, month, today), month, day)
    else:
        when = today
    name = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") or ex["id"]
    return f"{when.isoformat()}__{name}.csv"


def existing_ids() -> set[str]:
    ids = set()
    for p in list_dir.glob("*.csv"):
        for line in p.read_text(encoding="utf-8-sig").splitlines()[:5]:
            m = re.match(r"^#\s*källa\s*:.*\.(\d+)\.html", line.strip(), flags=re.I)
            if m:
                ids.add(m.group(1))
    return ids


def to_csv(title: str, url: str, pairs: list[tuple[str, str]]) -> str:
    lines = [f"# titel: {title}", f"# källa: {url}", "svenska;engelska"]
    for en, sv in pairs:
        lines.append(f"{sv.replace(';', ',')};{en.replace(';', ',')}")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- huvudflöde
def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="visa vad som skulle skrivas, skriv inget")
    args = parser.parse_args()

    creds = load_credentials()
    if not creds:
        print("GLOSOR_EU_USER/GLOSOR_EU_PASS saknas – hoppar över hämtningen från glosor.eu.")
        return 0
    user, password = creds
    opener = make_opener()
    overview = login(opener, user, password)
    exercises = parse_overview(overview)
    print(f"Inloggad. {len(exercises)} övningar på glosor.eu.")

    known_ids = existing_ids()
    today = date.today()
    written = 0
    for ex in exercises:
        if ex["id"] in known_ids:
            print(f"  finns redan:  {ex['label']}")
            continue
        time.sleep(1)  # snällt mot sajten
        info = parse_exercise(fetch(opener, ex["url"]))
        heading = info["heading"] or ex["label"]
        name = file_name_for(ex, heading, today)
        path = list_dir / name
        if path.exists():
            print(f"  finns redan:  {name}")
            continue
        if not info["pairs"]:
            print(f"  INGA ORD:     {heading} ({ex['url']}) – sidstrukturen kan ha ändrats", file=sys.stderr)
            continue
        title = nice_title(heading)
        if args.dry_run:
            print(f"  skulle skriva {name}  ({len(info['pairs'])} ord, titel: {title})")
        else:
            path.write_text(to_csv(title, ex["url"], info["pairs"]), encoding="utf-8", newline="\n")
            print(f"  skrev         {name}  ({len(info['pairs'])} ord, titel: {title})")
        written += 1

    if not written:
        print("Inga nya övningar.")
    elif not args.dry_run:
        print("Granska filerna, lägg ev. till '# tema: …' och pusha.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
