#!/usr/bin/env python3
"""Genererar exempelmeningar med Gemini för glosor som saknar mening.

För varje glosor/<namn>.csv skrivs glosor/<namn>.sentences.json. Bara ord som saknar
mening skickas till Gemini, så varje ord kostar ett anrop en gång.

    GEMINI_API_KEY=... python scripts/generate_sentences.py               # bara nya ord
    GEMINI_API_KEY=... python scripts/generate_sentences.py --regenerate  # gör om alla olåsta
                                                                          # meningar med annat tema

Tema: "# tema: ..." överst i CSV-filen. Utan tema blir det vardagliga meningar.
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

default_config = {
    "learner": "svensk elev i mellanstadiet, nybörjare i engelska (ungefär CEFR A1–A2)",
    "max_words": 12,
    "model": "gemini-3.5-flash",
}


# ---------------------------------------------------------------- prompt
def build_prompt(words: list[dict], theme: str, learner: str, max_words: int) -> str:
    word_lines = json.dumps(
        [{"id": i, "en": w["en"], "sv": w["sv"]} for i, w in enumerate(words)],
        ensure_ascii=False,
    )
    theme_rule = (
        f"- THEME: set every sentence in this world: {theme}. "
        "Use the theme whenever it fits naturally (places, people, objects and activities in that world). "
        "If a word really cannot fit the theme, write a simple everyday sentence instead - never a strange or forced sentence.\n"
        if theme else ""
    )
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
{{"items": [{{"id": 0, "sentence_en": "...", "sentence_sv": "...", "target": "..."}}]}}

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
    delay = 10
    for attempt in range(5):
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                data = json.load(resp)
            break
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:500]
            if e.code in (429, 500, 502, 503, 504) and attempt < 4:
                print(f"  Gemini svarade {e.code}, försöker igen om {delay} s …")
                time.sleep(delay)
                delay *= 2
                continue
            raise RuntimeError(f"Gemini-fel {e.code}: {detail}") from None
    text = "".join(
        p.get("text", "")
        for c in data.get("candidates", [])[:1]
        for p in c.get("content", {}).get("parts", [])
    )
    return parse_json(text)


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


def generate(words: list[dict], theme: str, cfg: dict, api_key: str) -> dict[int, dict]:
    """Returnerar {index i words: item}. Försöker en gång till med de ord som blev fel."""
    result: dict[int, dict] = {}
    pending = list(range(len(words)))
    for round_no in (1, 2):
        if not pending:
            break
        batch = [words[i] for i in pending]
        prompt = build_prompt(batch, theme, cfg["learner"], int(cfg["max_words"]))
        data = call_gemini(prompt, cfg["model"], api_key)
        still = []
        by_id = {it.get("id"): it for it in data.get("items", []) if isinstance(it, dict)}
        for local_id, idx in enumerate(pending):
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
    return result


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
    theme = info["theme"] or ""
    out_path = path.with_suffix(".sentences.json")
    existing = {}
    if out_path.exists():
        for it in json.loads(out_path.read_text(encoding="utf-8")).get("items", []):
            existing[(it.get("sv"), it.get("en"))] = it

    todo = []
    for w in info["words"]:
        old = existing.get((w["sv"], w["en"]))
        if old is None:
            todo.append(w)
        elif regenerate and not old.get("locked") and old.get("theme") != theme:
            todo.append(w)

    keep_keys = {(w["sv"], w["en"]) for w in info["words"]}
    pruned = any(k not in keep_keys for k in existing)
    if not todo and not pruned:
        print(f"{path.name}: alla ord har meningar")
        return False

    if todo:
        print(f"{path.name}: genererar {len(todo)} meningar (tema: {theme or 'inget'})")
        generated = generate(todo, theme, cfg, api_key)
        for idx, it in generated.items():
            w = todo[idx]
            existing[(w["sv"], w["en"])] = {
                "sv": w["sv"],
                "en": w["en"],
                "sentence_en": it["sentence_en"].strip(),
                "sentence_sv": it["sentence_sv"].strip(),
                "target": it["target"].strip(),
                "theme": theme,
                "locked": False,
            }

    items = [existing[(w["sv"], w["en"])] for w in info["words"] if (w["sv"], w["en"]) in existing]
    out_path.write_text(
        json.dumps({"items": items}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"  skrev {out_path.relative_to(root_dir)} ({len(items)} meningar)")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--regenerate", action="store_true", help="gör om olåsta meningar som har ett annat tema")
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
