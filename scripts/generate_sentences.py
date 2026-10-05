#!/usr/bin/env python3
"""Genererar exempelmeningar med Gemini för glosor som saknar mening.

För varje glosor/<namn>.csv skrivs glosor/<namn>.sentences.json. Bara ord som saknar
mening skickas till Gemini, så varje ord kostar ett anrop en gång.

    GEMINI_API_KEY=... python scripts/generate_sentences.py               # nya ord + ändrat tema
    GEMINI_API_KEY=... python scripts/generate_sentences.py --regenerate  # gör om alla olåsta meningar

Tema: "# tema: ..." överst i CSV-filen. Saknas raden väljer Gemini ett tema utifrån titeln
och orden. Det sparas i meningsfilen ("theme_auto": true) och återanvänds för nya ord.
Ändras temat i en lista görs dess olåsta meningar om automatiskt vid nästa körning.
Vill du behålla en mening du rättat för hand: sätt "locked": true på raden i JSON-filen.
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from glos_csv import read_list

root_dir = Path(__file__).resolve().parent.parent
list_dir = root_dir / "glosor"
config_file = root_dir / "sentences_config.json"
api_url = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
retry_attempts = 8   # 7 omförsök: 15 + 30 + 60 + 4 × 120 s ≈ 10 min
batch_size = 10      # ord per anrop – mindre anrop klarar sig bättre när Gemini är belastat
batch_pause = 4      # s mellan anrop, snällt mot gratisnivåns gräns för anrop per minut

default_config = {
    "learner": "svensk elev i mellanstadiet, nybörjare i engelska (ungefär CEFR A1–A2)",
    "max_words": 12,
    "model": "gemini-3.5-flash",
}


# ---------------------------------------------------------------- prompt
def build_prompt(words: list[dict], theme: str, learner: str, max_words: int,
                 title: str = "", choose_theme: bool = False) -> str:
    word_lines = json.dumps(
        [{"id": i, "en": w["en"], "sv": w["sv"]} for i, w in enumerate(words)],
        ensure_ascii=False,
    )
    fit_rule = (
        "Use the theme whenever it fits naturally (places, people, objects and activities in that world). "
        "If a word really cannot fit the theme, write a simple everyday sentence instead - never a strange or forced sentence.\n"
    )
    item_format = '{"id": 0, "sentence_en": "...", "sentence_sv": "...", "target": "..."}'
    if choose_theme:
        # Gemini väljer tema för hela listan i samma anrop
        theme_rule = (
            f'- THEME: first choose ONE theme for the whole list, based on the chapter title "{title}" and the words: '
            "a concrete world where most of the words fit naturally. Write the theme in Swedish, at most 12 words, "
            'and return it as "theme". Then set every sentence in that world. ' + fit_rule
        )
        json_format = '{"theme": "...", "items": [' + item_format + "]}"
    else:
        theme_rule = f"- THEME: set every sentence in this world: {theme}. " + fit_rule if theme else ""
        json_format = '{"items": [' + item_format + "]}"
    return f"""You write example sentences that help a Swedish child learn English vocabulary words.

Learner: {learner}

Rules:
- Exactly one English sentence per word, at most {max_words} words long.
- Use simple grammar and very common words that the learner already knows, apart from the target word.
- The sentence MUST contain the target English word. A natural inflected form is fine (plural, -s, -ed, -ing, irregular past).
- If the English field has alternatives separated by "|", pick ONE of them. Text in parentheses, like "(to)", is optional.
- Give the sentence a concrete, vivid situation so the meaning of the word is clear from context.
{theme_rule}- Content must be friendly and suitable for children. No violence, no brands, no real people.
- "sentence_sv": a natural Swedish translation of the sentence.
- "target": the word exactly as it is written in sentence_en (same letters and form), so it can be blanked out in a gap-fill exercise.

Return ONLY JSON in this format:
{json_format}

Words:
{word_lines}
"""


# ---------------------------------------------------------------- Gemini
def call_gemini(prompt: str, model: str, api_key: str) -> dict:
    body = json.dumps({
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"responseMimeType": "application/json", "temperature": 0.9},
    }).encode("utf-8")
    req = urllib.request.Request(
        api_url.format(model=model),
        data=body,
        headers={"Content-Type": "application/json", "x-goog-api-key": api_key},
        method="POST",
    )
    # Gratisnivån svarar ofta 503 (hög belastning) eller 429 (för många anrop). Det gör inget
    # om det tar några minuter, så vi väntar 15, 30, 60 och sedan 120 s åt gången (~10 min totalt).
    for attempt in range(1, retry_attempts + 1):
        try:
            with urllib.request.urlopen(req, timeout=180) as resp:
                data = json.load(resp)
            break
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")
            if e.code not in (429, 500, 502, 503, 504) or attempt == retry_attempts:
                raise RuntimeError(f"Gemini-fel {e.code}: {detail[:500]}") from None
            delay = max(retry_delay(attempt), suggested_delay(e.headers.get("Retry-After"), detail))
            print(f"  Gemini svarade {e.code}, försöker igen om {delay} s ({attempt}/{retry_attempts - 1}) …")
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt == retry_attempts:
                raise RuntimeError(f"Kunde inte nå Gemini: {e}") from None
            delay = retry_delay(attempt)
            print(f"  Kunde inte nå Gemini ({e}), försöker igen om {delay} s …")
        time.sleep(delay)
    text = "".join(
        p.get("text", "")
        for c in data.get("candidates", [])[:1]
        for p in c.get("content", {}).get("parts", [])
    )
    return parse_json(text)


def retry_delay(attempt: int) -> int:
    return min(15 * 2 ** (attempt - 1), 120)


def suggested_delay(retry_after: str | None, detail: str) -> int:
    """Väntetid som Google själv föreslår (Retry-After eller RetryInfo.retryDelay), annars 0."""
    if retry_after and retry_after.strip().isdigit():
        return min(int(retry_after), 300)
    m = re.search(r'"retryDelay"\s*:\s*"(\d+)(?:\.\d+)?s"', detail)
    return min(int(m.group(1)) + 1, 300) if m else 0


def parse_json(text: str) -> dict:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, flags=re.S)
        if m:
            return json.loads(m.group(0))
        raise


# ---------------------------------------------------------------- validering
def target_in_sentence(target: str, sentence: str) -> bool:
    if not target.strip():
        return False
    return re.search(r"(?<![\w'])" + re.escape(target.strip()) + r"(?![\w'])", sentence, flags=re.I) is not None


def valid_item(item: dict, max_words: int) -> bool:
    try:
        en, sv, target = item["sentence_en"].strip(), item["sentence_sv"].strip(), item["target"].strip()
    except (KeyError, AttributeError):
        return False
    return bool(en and sv) and len(en.split()) <= max_words + 4 and target_in_sentence(target, en)


def generate(words: list[dict], theme: str, cfg: dict, api_key: str,
             title: str = "", choose_theme: bool = False) -> tuple[dict[int, dict], str, int]:
    """Returnerar ({index i words: item}, tema, antal ord där Gemini inte gick att nå).

    Orden skickas i omgångar om batch_size. Ord vars mening inte klarar kontrollen skickas
    en gång till. Går ett anrop inte igenom ens efter omförsöken hoppas de orden över – de
    saknar då mening och tas med vid nästa körning.
    Med choose_theme väljer Gemini temat i första lyckade anropet; resten använder samma tema.
    """
    result: dict[int, dict] = {}
    unreachable: list[int] = []
    pending = list(range(len(words)))
    first_call = True
    for round_no in (1, 2):
        if not pending:
            break
        still = []
        for start in range(0, len(pending), batch_size):
            chunk = pending[start:start + batch_size]
            if not first_call:
                time.sleep(batch_pause)
            first_call = False
            prompt = build_prompt([words[i] for i in chunk], theme, cfg["learner"], int(cfg["max_words"]),
                                  title, choose_theme)
            try:
                data = call_gemini(prompt, cfg["model"], api_key)
            except RuntimeError as e:
                print(f"  {e}".splitlines()[0][:300])
                print(f"  Ger upp {len(chunk)} ord för den här gången")
                unreachable += chunk
                continue
            if choose_theme:
                theme = str(data.get("theme") or "").strip()[:150]
                choose_theme = False
            by_id = {it.get("id"): it for it in data.get("items", []) if isinstance(it, dict)}
            for local_id, idx in enumerate(chunk):
                it = by_id.get(local_id)
                if it and valid_item(it, int(cfg["max_words"])):
                    result[idx] = it
                else:
                    still.append(idx)
        if still and round_no == 1:
            print(f"  {len(still)} meningar klarade inte kontrollen, försöker igen …")
        pending = still
    for idx in pending:
        print(f"  Hoppar över '{words[idx]['en']}' (ingen giltig mening)")
    return result, theme, len(unreachable)


# ---------------------------------------------------------------- huvudflöde
def load_config() -> dict:
    cfg = dict(default_config)
    if config_file.exists():
        cfg.update(json.loads(config_file.read_text(encoding="utf-8")))
    return cfg


def process_list(path: Path, cfg: dict, api_key: str, regenerate: bool) -> bool:
    info = read_list(path)
    if not info["words"]:
        return False
    out_path = path.with_suffix(".sentences.json")
    existing = {}
    if out_path.exists():
        for it in json.loads(out_path.read_text(encoding="utf-8")).get("items", []):
            existing[(it.get("sv"), it.get("en"))] = it

    # Tema från CSV:n, annars ett som Gemini redan valt för listan, annars låter vi Gemini välja
    csv_theme = info["theme"] or ""
    theme_auto = not csv_theme
    if csv_theme:
        theme = csv_theme
    else:
        theme = next((it["theme"] for it in existing.values() if it.get("theme_auto") and it.get("theme")), "")

    def stale(old: dict) -> bool:
        if csv_theme:
            return (old.get("theme") or "") != csv_theme
        return not old.get("theme_auto")  # # tema: borttagen → Gemini väljer nytt

    todo = []
    for w in info["words"]:
        old = existing.get((w["sv"], w["en"]))
        if old is None:
            todo.append(w)
        elif not old.get("locked") and (regenerate or stale(old)):
            todo.append(w)

    keep_keys = {(w["sv"], w["en"]) for w in info["words"]}
    pruned = any(k not in keep_keys for k in existing)
    if not todo and not pruned:
        print(f"{path.name}: alla ord har meningar")
        return False

    unreachable = 0
    if todo:
        choose_theme = theme_auto and not theme
        print(f"{path.name}: genererar {len(todo)} meningar (tema: {'väljs av Gemini' if choose_theme else theme})")
        generated, theme, unreachable = generate(todo, theme, cfg, api_key, info["title"], choose_theme)
        if choose_theme and generated:
            print(f"  Gemini valde tema: {theme or '(inget)'}")
        for idx, it in generated.items():
            w = todo[idx]
            existing[(w["sv"], w["en"])] = {
                "sv": w["sv"],
                "en": w["en"],
                "sentence_en": it["sentence_en"].strip(),
                "sentence_sv": it["sentence_sv"].strip(),
                "target": it["target"].strip(),
                "theme": theme,
                "theme_auto": theme_auto,
                "locked": False,
            }

    items = [existing[(w["sv"], w["en"])] for w in info["words"] if (w["sv"], w["en"]) in existing]
    changed = bool(items) or out_path.exists()  # skapa ingen tom meningsfil
    if changed:
        out_path.write_text(
            json.dumps({"items": items}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(f"  skrev {out_path.relative_to(root_dir)} ({len(items)} meningar)")
    if unreachable:
        # det som lyckades är sparat; resten tas vid nästa körning
        raise RuntimeError(f"{unreachable} ord fick ingen mening (Gemini otillgänglig) – försöker igen vid nästa körning")
    return changed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--regenerate", action="store_true", help="gör om alla olåsta meningar, även med samma tema")
    parser.add_argument("files", nargs="*", help="bara dessa CSV-filer (standard: alla i glosor/)")
    args = parser.parse_args()

    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        print("GEMINI_API_KEY saknas – hoppar över meningsgenereringen (appen fungerar ändå).")
        return 0

    cfg = load_config()
    paths = [Path(f).resolve() for f in args.files] if args.files else sorted(list_dir.glob("*.csv"))
    failures = 0
    for i, path in enumerate(paths):
        try:
            changed = process_list(path, cfg, api_key, args.regenerate)
        except Exception as e:  # en trasig lista ska inte stoppa resten
            failures += 1
            print(f"{path.name}: FEL – {e}", file=sys.stderr)
            continue
        if changed and i < len(paths) - 1:
            time.sleep(4)  # snällt mot gratisnivåns gräns för anrop per minut
    if failures:
        print(f"{failures} listor misslyckades – se felen ovan.", file=sys.stderr)
        return 1  # syns som varning i GitHub Actions (steget har continue-on-error)
    return 0


if __name__ == "__main__":
    sys.exit(main())
