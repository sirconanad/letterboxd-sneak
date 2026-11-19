#!/usr/bin/env python3
"""Measure how far in advance a sneak / OV-sneak screening happened.

Pulls every diary entry tagged ``sneak`` or ``ov sneak`` from a Letterboxd
data export, records the watch date, then looks up the German theatrical
release date (German cinema release, aka Kinostart) on The Movie Database
(TMDB) and prints the gap:
how many days before the German premiere the film was screened.

Sources of truth:
- watch dates + tags: Letterboxd data export (``diary.csv``, inside the
  ``letterboxd-*-utc.zip`` backup) -- tags are not exposed on the diary page.
- German premiere: TMDB ``/search/movie`` + ``/movie/{id}/release_dates``
  (country ``DE``, type ``3`` = theatrical). Needs a free TMDB API key.

Results are cached in ``sneak-cache.json`` so re-runs (and crash recovery)
cost no further API calls.

The TMDB key is read from ``.env`` next to the repo (``TMDB_API_KEY=...``),
from the ``TMDB_API_KEY`` environment variable, or from ``--api-key``. It is
never stored in this file.

Examples:
    python3 sneak-gap.py                     # auto-detect newest export
    python3 sneak-gap.py --export ../letterboxd-sirconanad-2026-11-19-00-26-utc.zip
    python3 sneak-gap.py --no-lookup         # just list the sneak films
    python3 sneak-gap.py --csv sneak-gap.csv
"""

import argparse
import csv
import io
import json
import os
import sys
import time
import urllib.parse
import urllib.request
import zipfile
from collections import defaultdict, deque
from dataclasses import asdict, dataclass
from datetime import date, datetime
from pathlib import Path

SLEAK_TAGS = ("sneak", "ov sneak")

# ../.env relative to this file (sneak-preview/sneak-gap.py -> repo root)
DEFAULT_ENV_FILE = Path(__file__).resolve().parent.parent / ".env"


def load_dotenv(paths=(DEFAULT_ENV_FILE, Path.cwd() / ".env")) -> None:
    """Load KEY=VALUE pairs from .env files without overriding existing vars."""
    for path in paths:
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip('"'))


# ------------------------------------------------------------------ export


def find_export() -> Path | None:
    """Locate the newest Letterboxd backup zip near the repo."""
    hits = []
    for d in (Path.cwd(), Path.cwd().parent):
        hits.extend(d.glob("letterboxd-*.zip"))
    if hits:
        return max(hits, key=lambda p: p.stat().st_mtime)
    diary = Path.cwd().parent / "diary.csv"
    return diary if diary.exists() else None


def read_diary_csv(path: Path) -> list[dict]:
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as zf:
            raw = zf.read("diary.csv").decode("utf-8", "replace")
    else:
        raw = path.read_text(encoding="utf-8", errors="replace")
    return list(csv.DictReader(io.StringIO(raw)))


@dataclass
class Sneak:
    title: str
    year: str
    watched: date
    tags: list[str]
    rating: float | None = None   # 0.5..5 from the diary export, None = unrated
    liked: bool = False           # whether the film is in the export's likes
    kinostart: date | None = None
    tmdb_id: int | None = None
    gap_days: int | None = None  # release - watched (positive = saw it first)


def is_sneak_tags(tags: str) -> bool:
    norm = {t.strip().lower().replace("-", " ") for t in tags.split(",")}
    return bool(norm & set(SLEAK_TAGS))


def parse_rating(value: str) -> float | None:
    """Parse the export's Rating column (0.5..5, blank = unrated)."""
    value = (value or "").strip()
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None


# --------------------------------------------------------------------- export


def load_liked(path: Path) -> set[tuple[str, str]]:
    """Set of (name.lower(), year) liked films, from the export's likes CSV."""
    if not zipfile.is_zipfile(path):
        return set()
    try:
        with zipfile.ZipFile(path) as zf:
            raw = zf.read("likes/films.csv").decode("utf-8", "replace")
    except KeyError:
        return set()
    out: set[tuple[str, str]] = set()
    for row in csv.DictReader(io.StringIO(raw)):
        name = (row.get("Name") or "").strip().lower()
        year = (row.get("Year") or "").strip()
        if name:
            out.add((name, year))
    return out


def collect_sneaks(rows: list[dict],
                   liked_set: set | None = None) -> list[Sneak]:
    liked_set = liked_set or set()
    out = []
    for r in rows:
        if not is_sneak_tags(r.get("Tags") or ""):
            continue
        name = (r.get("Name") or "?").strip()
        year = (r.get("Year") or "").strip()
        out.append(Sneak(
            title=name,
            year=year,
            watched=date.fromisoformat(r.get("Watched Date") or "1970-01-01"),
            rating=parse_rating(r.get("Rating")),
            liked=(name.lower(), year) in liked_set,
            tags=[t.strip() for t in (r.get("Tags") or "").split(",")
                  if t.strip()],
        ))
    return out


# -------------------------------------------------------------------- TMDB


class TMDB:
    BASE = "https://api.themoviedb.org/3"

    def __init__(self, api_key: str, sleep: float = 0.25):
        self.key = api_key
        self.sleep = sleep

    def _get(self, path: str, params: dict):
        params = dict(params, api_key=self.key)
        url = f"{self.BASE}{path}?{urllib.parse.urlencode(params)}"
        with urllib.request.urlopen(url, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def search(self, title: str, year: str):
        params = {"query": title, "language": "en-US",
                  "include_adult": "false"}
        if year:
            params["year"] = year
        data = self._get("/search/movie", params)
        results = data.get("results") or []
        # Prefer the hit whose release year matches, otherwise first hit.
        for res in results:
            if (res.get("release_date") or "")[:4] == year:
                return res
        return results[0] if results else None

    def de_release(self, movie_id: int):
        """Return (release_date_iso, note) for the DE theatrical release."""
        data = self._get(f"/movie/{movie_id}/release_dates", {})
        for entry in data.get("results") or []:
            if entry.get("iso_3166_1") != "DE":
                continue
            dates = entry.get("release_dates") or []
            theatrical = [d for d in dates if d.get("type") == 3]
            pool = theatrical or dates
            if not pool:
                return None, "no date found"
            best = min(pool, key=lambda d: d.get("release_date", "9999"))
            note = best.get("note") or "Theatrical" if theatrical else \
                (best.get("note") or "other")
            return best.get("release_date", "")[:10] or None, note
        return None, "no DE release"


# ------------------------------------------------------------ cache + lookup


def load_cache(path: Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def save_cache(path: Path, cache: dict):
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cache, indent=2, ensure_ascii=False) + "\n",
                   encoding="utf-8")
    tmp.replace(path)


def fill_kinostarts(sneaks: list[Sneak], tmdb: TMDB, cache: dict,
                    cache_path: Path):
    """Look up German premieres, reusing/populating the cache. Mutates sneaks."""
    for i, s in enumerate(sneaks, 1):
        key = f"{s.year}|{s.title}"
        if key in cache:
            hit = cache[key]
            s.tmdb_id = hit.get("id")
            s.kinostart = (date.fromisoformat(hit["kinostart"])
                           if hit.get("kinostart") else None)
            print(f"[{i}/{len(sneaks)}] (cached) {s.title} "
                  f"({s.year}) -> {hit.get('kinostart') or 'none'}")
            continue
        print(f"[{i}/{len(sneaks)}] {s.title} ({s.year})", flush=True)
        res = tmdb.search(s.title, s.year)
        if res is None:
            cache[key] = {"id": None, "kinostart": None}
            print("    no TMDB match")
        else:
            s.tmdb_id = res["id"]
            iso, note = tmdb.de_release(res["id"])
            if iso:
                s.kinostart = date.fromisoformat(iso)
            cache[key] = {"id": res["id"], "kinostart": iso,
                          "film": res.get("title"),
                          "note": note}
            print(f"    id={res['id']} kinostart={iso} "
                  f"(note: {note})")
        time.sleep(tmdb.sleep)
        if i % 10 == 0:
            save_cache(cache_path, cache)
    save_cache(cache_path, cache)


# -------------------------------------------------------------------- output


def fmt_table(sneaks: list[Sneak]) -> str:
    header = (f"{'Film':<42} {'Year':<5} {'Rate':<5} {'Like':<4} "
              f"{'Watched':<11} {'Cinema (DE)':<13} {'Gap':>10}")
    rows = [header, "-" * len(header)]
    for s in sorted(sneaks, key=lambda x: x.watched, reverse=True):
        if s.kinostart and s.gap_days is not None:
            gap = (f"{abs(s.gap_days)} days "
                   f"{'before' if s.gap_days > 0 else 'after'} release")
        else:
            gap = "n/a"
        rate = f"{s.rating:g}" if s.rating is not None else "–"
        like = "♥" if s.liked else "–"
        rows.append(f"{s.title[:42]:<42} {s.year:<5} {rate:<5} {like:<4} "
                    f"{s.watched.isoformat():<11} "
                    f"{s.kinostart.isoformat() if s.kinostart else '–':<13} "
                    f"{gap:>10}")
    rows.append("-" * len(header))
    with_release = [s for s in sneaks if s.gap_days is not None]
    if with_release:
        avg = sum(s.gap_days for s in with_release) / len(with_release)
        most_ahead = max(with_release, key=lambda s: s.gap_days)
        rows.append(f"Sneaks with a German release: "
                    f"{len(with_release)}/{len(sneaks)}")
        rows.append(f"On average seen {avg:.1f} days before the release")
        rows.append(f"Biggest lead: {most_ahead.title} "
                    f"({most_ahead.gap_days} days early)")
    not_found = [s for s in sneaks if s.tmdb_id is None]
    no_de = [s for s in sneaks if s.tmdb_id is not None
             and s.kinostart is None]
    if not_found:
        rows.append(f"No TMDB match ({len(not_found)}): "
                    + ", ".join(s.title for s in not_found))
    if no_de:
        rows.append(f"No German release ({len(no_de)}): "
                    + ", ".join(s.title for s in no_de))
    return "\n".join(rows)


def fmt_markdown(sneaks: list[Sneak]) -> str:
    ordered = sorted(sneaks, key=lambda x: x.watched, reverse=True)
    lines = ["# Sneak / OV-Sneak vs. German cinema release", "",
             f"{len(sneaks)} diary entries tagged **sneak** or **ov sneak**; "
             "\"gap\" = release date − watch date (negative means watched "
             "after the release).", "",
             "| Film | Year | Rating | Liked | Watched | Cinema release (DE) | Gap | Tags |",
             "|---|---|---|---|---|---|---|---|"]
    for s in ordered:
        if s.kinostart and s.gap_days is not None:
            gap = (f"+{s.gap_days} (early)" if s.gap_days > 0
                   else f"{s.gap_days} (late)")
        else:
            gap = "n/a"
        rate = f"{s.rating:g}" if s.rating is not None else "–"
        like = "Yes" if s.liked else "–"
        lines.append(
            f"| {s.title} | {s.year} | {rate} | {like} | "
            f"{s.watched.isoformat()} | "
            f"{s.kinostart.isoformat() if s.kinostart else '–'} | "
            f"{gap} | {', '.join(s.tags)} |")
    with_release = [s for s in sneaks if s.gap_days is not None]
    if with_release:
        avg = sum(s.gap_days for s in with_release) / len(with_release)
        most_ahead = max(with_release, key=lambda s: s.gap_days)
        lines += ["", "## Summary", "",
                  f"- Sneaks with a German release: "
                  f"{len(with_release)}/{len(sneaks)}",
                  f"- On average seen **{avg:.1f} days** before the release",
                  f"- Biggest lead: **{most_ahead.title}** "
                  f"({most_ahead.gap_days} days early)"]
    not_found = [s for s in sneaks if s.tmdb_id is None]
    no_de = [s for s in sneaks if s.tmdb_id is not None
             and s.kinostart is None]
    if not_found:
        lines += ["", f"### No TMDB match ({len(not_found)})"] + \
                 [f"- {s.title} ({s.year})" for s in not_found]
    if no_de:
        lines += ["", f"### No German cinema release ({len(no_de)})"] + \
                 [f"- {s.title} ({s.year})" for s in no_de]
    return "\n".join(lines) + "\n"


# -------------------------------------------------------------------- main


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--export", metavar="PATH",
                    help="Letterboxd data export zip (auto-detected next to "
                         "the repo); a fresh download reflects new entries")
    ap.add_argument("--api-key", dest="api_key",
                    help="TMDB API key (or set TMDB_API_KEY)")
    ap.add_argument("--no-lookup", action="store_true",
                    help="only list the sneak films, no TMDB lookups")
    ap.add_argument("--csv", metavar="FILE",
                    help="also write results as CSV")
    ap.add_argument("--markdown", metavar="FILE",
                    help="also write results as a Markdown file")
    ap.add_argument("--sleep", type=float, default=0.25,
                    help="delay between TMDB calls (default 0.25 s)")
    ap.add_argument("--limit", type=int, default=0,
                    help="only process the first N films (testing)")
    args = ap.parse_args(argv)

    export = Path(args.export) if args.export else find_export()
    if export is None or not export.exists():
        print("error: no Letterboxd data export found; download one from "
              "https://letterboxd.com/settings/data/ and pass its path or "
              "place it next to the repo", file=sys.stderr)
        return 1

    try:
        sneaks = collect_sneaks(read_diary_csv(export), load_liked(export))
    except (ValueError, OSError, KeyError) as exc:
        print(f"error: could not read export: {exc}", file=sys.stderr)
        return 1
    if args.limit:
        sneaks = sneaks[: args.limit]
    print(f"{export.name} -> {len(sneaks)} sneak/OV-sneak "
          f"entr{'y' if len(sneaks) == 1 else 'ies'}")
    if not sneaks:
        return 0

    if args.no_lookup:
        for s in sneaks:
            print(f"{s.watched.isoformat()}  {s.title} ({s.year})")
        return 0

    load_dotenv()
    api_key = (args.api_key
               or os.environ.get("TMDB_API_KEY")
               or (lambda f: f.read_text().strip()
                   if f.exists() and f.read_text().strip() else None)(
                       Path.home() / ".tmdb_api_key"))
    if not api_key:
        print("error: no TMDB API key. Set TMDB_API_KEY in "
              f"{DEFAULT_ENV_FILE}, in the environment, via --api-key, or "
              "get a free key at https://www.themoviedb.org/settings/api",
              file=sys.stderr)
        return 1

    cache_path = Path.cwd() / "sneak-cache.json"
    cache = load_cache(cache_path)
    fill_kinostarts(sneaks, TMDB(api_key, args.sleep), cache, cache_path)

    for s in sneaks:
        if s.kinostart:
            s.gap_days = (s.kinostart - s.watched).days

    print("\n" + fmt_table(sneaks))

    if args.csv:
        out = Path(args.csv)
        with out.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=[
                "title", "year", "rating", "liked", "watched", "kinostart",
                "gap_days", "tags", "tmdb_id"])
            writer.writeheader()
            for s in sorted(sneaks, key=lambda x: x.watched, reverse=True):
                writer.writerow({
                    "title": s.title, "year": s.year,
                    "rating": f"{s.rating:g}" if s.rating is not None else "",
                    "liked": "yes" if s.liked else "",
                    "watched": s.watched.isoformat(),
                    "kinostart": s.kinostart.isoformat() if s.kinostart else "",
                    "gap_days": s.gap_days if s.gap_days is not None else "",
                    "tags": ", ".join(s.tags),
                    "tmdb_id": s.tmdb_id if s.tmdb_id else "",
                })
        print(f"wrote CSV: {out}")

    if args.markdown:
        out = Path(args.markdown)
        out.write_text(fmt_markdown(sneaks), encoding="utf-8")
        print(f"wrote Markdown: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
