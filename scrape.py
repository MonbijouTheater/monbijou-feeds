#!/usr/bin/env python3
"""
Monbijou Theater – Spielplan-Feeds

Liest den öffentlichen Spielplan von https://monbijou.billeto.net aus und erzeugt
im Ordner docs/ (wird über GitHub Pages veröffentlicht):

  termine.ics           alle Vorstellungen als Kalender-Abo
  termine-englisch.ics  nur englischsprachige Vorstellungen
  produktionen.xml      RSS – ein Eintrag pro Produktion bzw. Märchen-Reihe (Social Media)
  wochenprogramm.xml    RSS – ein Eintrag pro Kalenderwoche (Newsletter, Wochenvorschau)
  events.json           alle Daten maschinenlesbar
  index.html            Übersichtsseite mit Links und nächsten Terminen
"""

from __future__ import annotations

import hashlib
import html
import json
import re
import sys
import time
from dataclasses import dataclass, asdict, field
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

BASE = "https://monbijou.billeto.net"
OUT = Path(__file__).parent / "docs"
TZ = ZoneInfo("Europe/Berlin")
DEFAULT_DURATION = timedelta(minutes=90)
MAX_PAGES = 40
HEADERS = {"User-Agent": "Monbijou-Feeds/1.0 (+https://github.com/MonbijouTheater/monbijou-feeds)"}
ADDRESS = "Monbijou Theater, Monbijoustraße 3B, 10117 Berlin"

# Produktionen, die englischsprachig sind, aber im Spielplan nicht so markiert werden
ENGLISH_TITLES = ["Darkest Thoughts"]

# Die vielen Märchen-Kombinationen ("Rotkäppchen & Frau Holle" …) werden für den RSS-Feed
# zu Reihen zusammengefasst, sonst entstünden über 100 Einträge.
SERIES = {
    "erwachsene": "Märchen für Erwachsene",
    "familie": "Märchen für Kinder & Familien",
    "schule": "Märchen für Kitas & Schulen",
}

TICKET_URL_RE = re.compile(r"(?:window\.open|basket)\(\s*'([^']+)'")
INFO_ID_RE = re.compile(r"/eventinfo/(\d+)")


@dataclass
class Event:
    uid: str
    start: str            # ISO, Europe/Berlin
    title: str
    subtitle: str
    venue: str
    ticket_url: str
    info_url: str
    sold_out: bool
    english: bool
    audience: str         # erwachsene | familie | schule | sonstige
    series: str = ""      # Gruppierung für den RSS-Feed
    description: str = ""


# --------------------------------------------------------------------------- #
# Auslesen
# --------------------------------------------------------------------------- #

def fetch(session: requests.Session, path: str) -> str:
    for attempt in range(3):
        try:
            r = session.get(BASE + path, headers=HEADERS, timeout=30)
            r.raise_for_status()
            r.encoding = "utf-8"
            return r.text
        except requests.RequestException as e:
            if attempt == 2:
                raise
            print(f"  Wiederhole {path}: {e}", file=sys.stderr)
            time.sleep(3)
    raise RuntimeError("unreachable")


def classify(title: str, subtitle: str) -> tuple[bool, str]:
    text = f"{title} {subtitle}".lower()
    english = ("englisch" in text or "english" in text
               or any(t.lower() in title.lower() for t in ENGLISH_TITLES))
    if "erwachsene" in text:
        audience = "erwachsene"
    elif "schule" in text or "kita" in text or "kindergärten" in text:
        audience = "schule"
    elif "kinder" in text or "famili" in text:
        audience = "familie"
    else:
        audience = "sonstige"
    return english, audience


def parse_page(page_html: str) -> list[Event]:
    soup = BeautifulSoup(page_html, "html.parser")
    events: list[Event] = []
    for row in soup.select("#calendar .event"):
        date_el = row.select_one(".date.desktop")
        time_el = row.select_one(".time")
        title_el = row.select_one(".title")
        if not (date_el and time_el and title_el):
            continue
        date_s = date_el.get_text(strip=True)
        time_s = time_el.get_text(strip=True)
        try:
            start = datetime.strptime(f"{date_s} {time_s}", "%d.%m.%Y %H:%M").replace(tzinfo=TZ)
        except ValueError:
            print(f"  Übersprungen (Datum unlesbar): {date_s} {time_s}", file=sys.stderr)
            continue

        title = " ".join(title_el.get_text(" ", strip=True).split())
        sub_el = row.select_one(".subtitle")
        subtitle = " ".join(sub_el.get_text(" ", strip=True).split()) if sub_el else ""

        halves = row.select(".w3-rest .w3-half")
        venue = halves[1].get_text(strip=True) if len(halves) > 1 else ""

        buy = row.select_one(".buy")
        sold_out = False
        ticket_url = ""
        if buy:
            sold_out = bool(buy.select_one('[title="ausverkauft"]')) or "w3-disabled" in str(buy)
            btn = buy.select_one("button")
            m = TICKET_URL_RE.search(btn.get("onclick", "")) if btn else None
            if m:
                ticket_url = html.unescape(m.group(1))
        if not ticket_url:
            ticket_url = BASE + "/"

        info_url = ""
        link = title_el.select_one("a")
        if link:
            m = INFO_ID_RE.search(link.get("href", ""))
            if m:
                info_url = f"{BASE}/eventinfo/{m.group(1)}"

        english, audience = classify(title, subtitle)
        uid_src = f"{start.isoformat()}|{title}|{venue}"
        uid = hashlib.sha1(uid_src.encode()).hexdigest()[:16] + "@monbijou-feeds"

        series = title
        if not info_url and not english and audience in SERIES:
            series = SERIES[audience]
        events.append(Event(uid, start.isoformat(), title, subtitle, venue,
                            ticket_url, info_url, sold_out, english, audience, series))
    return events


def parse_description(info_html: str) -> str:
    soup = BeautifulSoup(info_html, "html.parser")
    piece = soup.select_one("#piece")
    if not piece:
        return ""
    for el in piece.select("input, script"):
        el.decompose()
    paras = [" ".join(p.get_text(" ", strip=True).split()) for p in piece.select("p")]
    text = "\n\n".join(p for p in paras if p)
    return text or " ".join(piece.get_text(" ", strip=True).split())


def scrape() -> list[Event]:
    session = requests.Session()
    all_events: list[Event] = []
    seen: set[str] = set()
    for page in range(1, MAX_PAGES + 1):
        path = "/" if page == 1 else f"/events/{page}"
        evs = parse_page(fetch(session, path))
        new = [e for e in evs if e.uid not in seen]
        print(f"Seite {page}: {len(evs)} Termine ({len(new)} neu)")
        if not new:
            break
        for e in new:
            seen.add(e.uid)
        all_events.extend(new)
        time.sleep(0.5)

    # Beschreibung je Produktion einmal holen
    desc_cache: dict[str, str] = {}
    for e in all_events:
        if e.title not in desc_cache and e.info_url:
            try:
                desc_cache[e.title] = parse_description(fetch(session, e.info_url.replace(BASE, "")))
            except requests.RequestException:
                desc_cache[e.title] = ""
            time.sleep(0.3)
        e.description = desc_cache.get(e.title, "")

    all_events.sort(key=lambda e: e.start)
    return all_events


# --------------------------------------------------------------------------- #
# Ausgabe
# --------------------------------------------------------------------------- #

def ics_escape(s: str) -> str:
    return (s.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")
             .replace("\r", "").replace("\n", "\\n"))


def ics_fold(line: str) -> str:
    out, cur = [], line.encode("utf-8")
    while len(cur) > 75:
        cut = 75 if not out else 74
        while cut > 0 and (cur[cut] & 0xC0) == 0x80:  # keine UTF-8-Zeichen zerschneiden
            cut -= 1
        out.append(cur[:cut].decode("utf-8"))
        cur = cur[cut:]
    out.append(cur.decode("utf-8"))
    return "\r\n ".join(out)


def utc_stamp(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def build_ics(events: list[Event], name: str) -> str:
    now = utc_stamp(datetime.now(timezone.utc))
    lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0",
        "PRODID:-//Monbijou Theater//Spielplan-Feeds//DE",
        "CALSCALE:GREGORIAN", "METHOD:PUBLISH",
        f"X-WR-CALNAME:{ics_escape(name)}", "X-WR-TIMEZONE:Europe/Berlin",
        "REFRESH-INTERVAL;VALUE=DURATION:PT6H", "X-PUBLISHED-TTL:PT6H",
    ]
    for e in events:
        start = datetime.fromisoformat(e.start)
        summary = e.title + (" – AUSVERKAUFT" if e.sold_out else "")
        desc_parts = [p for p in (e.subtitle, e.description, f"Tickets: {e.ticket_url}") if p]
        lines += [
            "BEGIN:VEVENT",
            f"UID:{e.uid}",
            f"DTSTAMP:{now}",
            f"DTSTART:{utc_stamp(start)}",
            f"DTEND:{utc_stamp(start + DEFAULT_DURATION)}",
            f"SUMMARY:{ics_escape(summary)}",
            f"LOCATION:{ics_escape(f'{e.venue}, {ADDRESS}')}",
            f"DESCRIPTION:{ics_escape(chr(10).join(desc_parts))}",
            f"URL:{e.ticket_url}",
            "END:VEVENT",
        ]
    lines.append("END:VCALENDAR")
    return "\r\n".join(ics_fold(l) for l in lines) + "\r\n"


WEEKDAYS = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]


def fmt_date(dt: datetime) -> str:
    return f"{WEEKDAYS[dt.weekday()]}. {dt:%d.%m.%Y, %H:%M} Uhr"


def productions(events: list[Event]) -> list[dict]:
    groups: dict[str, list[Event]] = {}
    for e in events:
        groups.setdefault(e.series or e.title, []).append(e)
    prods = []
    for title, evs in groups.items():
        evs.sort(key=lambda e: e.start)
        prods.append({
            "title": title,
            "guid": hashlib.sha1(title.encode()).hexdigest()[:16],
            "first": evs[0].start,
            "subtitles": sorted({e.subtitle for e in evs if e.subtitle}),
            "venues": sorted({e.venue for e in evs if e.venue}),
            "english": any(e.english for e in evs),
            "description": next((e.description for e in evs if e.description), ""),
            "titles": sorted({e.title for e in evs}) if len({e.title for e in evs}) > 1 else [],
            "ticket_url": next((e.ticket_url for e in evs if not e.sold_out), evs[0].ticket_url),
            "dates": [{"start": e.start, "sold_out": e.sold_out, "title": e.title,
                       "subtitle": e.subtitle} for e in evs],
        })
    prods.sort(key=lambda p: p["first"])
    return prods


def previous_pubdates(path: Path) -> dict[str, str]:
    """pubDate bereits bekannter Produktionen beibehalten, damit Make.com nur Neues meldet."""
    if not path.exists():
        return {}
    xml = path.read_text(encoding="utf-8")
    return dict(re.findall(r'<guid isPermaLink="false">([^<]+)</guid><pubDate>([^<]+)</pubDate>', xml))


def build_rss(prods: list[dict], site_url: str, known: dict[str, str] | None = None) -> str:
    now = format_datetime(datetime.now(timezone.utc))
    known = known or {}
    items = []
    for p in prods:
        dates_html = "".join(
            f"<li>{fmt_date(datetime.fromisoformat(d['start']))}"
            + (f": {html.escape(d['title'])}" if p["titles"] else "")
            + f"{' – <b>ausverkauft</b>' if d['sold_out'] else ''}</li>"
            for d in p["dates"][:20]
        )
        more = len(p["dates"]) - 20
        if more > 0:
            dates_html += f"<li>… und {more} weitere Termine</li>"
        body = (
            (f"<p><i>{html.escape(' · '.join(p['subtitles']))}</i></p>" if p["subtitles"] else "")
            + "".join(f"<p>{html.escape(x)}</p>" for x in p["description"].split("\n\n") if x)
            + f"<p>Ort: {html.escape(', '.join(p['venues']))}</p>"
            + f"<ul>{dates_html}</ul>"
            + f'<p><a href="{html.escape(p["ticket_url"])}">Tickets</a></p>'
        )
        items.append(
            "<item>"
            f"<title>{html.escape(p['title'])}</title>"
            f"<link>{html.escape(p['ticket_url'])}</link>"
            f'<guid isPermaLink="false">{p["guid"]}</guid>'
            f"<pubDate>{known.get(p['guid'], now)}</pubDate>"
            + (f"<category>English</category>" if p["english"] else "")
            + f"<description><![CDATA[{body}]]></description>"
            "</item>"
        )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<rss version="2.0"><channel>'
        "<title>Monbijou Theater – Produktionen</title>"
        f"<link>{site_url}</link>"
        "<description>Aktuelle Produktionen und Termine im Monbijou Theater Berlin</description>"
        "<language>de-de</language>"
        f"<lastBuildDate>{now}</lastBuildDate>"
        + "".join(items)
        + "</channel></rss>\n"
    )


def build_weekly_rss(events: list[Event], site_url: str, known: dict[str, str]) -> str:
    """Ein Eintrag pro Kalenderwoche – ideal für 'Diese Woche im Monbijou' (Social + Newsletter)."""
    now_dt = datetime.now(TZ)
    now = format_datetime(datetime.now(timezone.utc))
    weeks: dict[str, list[Event]] = {}
    for e in events:
        dt = datetime.fromisoformat(e.start)
        y, w, _ = dt.isocalendar()
        weeks.setdefault(f"{y}-KW{w:02d}", []).append(e)
    items = []
    for key, evs in sorted(weeks.items())[:8]:
        first = datetime.fromisoformat(evs[0].start)
        monday = (first - timedelta(days=first.weekday())).date()
        sunday = monday + timedelta(days=6)
        days: dict[str, list[Event]] = {}
        for e in evs:
            days.setdefault(fmt_date(datetime.fromisoformat(e.start)).split(",")[0], []).append(e)
        body = "".join(
            f"<h3>{d}</h3><ul>" + "".join(
                f"<li>{datetime.fromisoformat(e.start):%H:%M} {html.escape(e.title)}"
                + (f" <i>({html.escape(e.subtitle)})</i>" if e.subtitle else "")
                + f" – {html.escape(e.venue)}"
                + (" – <b>ausverkauft</b>" if e.sold_out else f' – <a href="{html.escape(e.ticket_url)}">Tickets</a>')
                + "</li>" for e in lst) + "</ul>"
            for d, lst in days.items()
        )
        # Veröffentlichungsdatum = Montag der Vorwoche 08:00, aber nie in der Zukunft
        pub_dt = datetime.combine(monday - timedelta(days=7), datetime.min.time(), TZ).replace(hour=8)
        pub = known.get(key) or (format_datetime(pub_dt) if pub_dt <= now_dt else None)
        if pub is None:
            continue  # Woche liegt noch zu weit in der Zukunft
        items.append(
            "<item>"
            f"<title>Monbijou Theater: {monday:%d.%m.} – {sunday:%d.%m.%Y} ({len(evs)} Vorstellungen)</title>"
            f"<link>{site_url}</link>"
            f'<guid isPermaLink="false">{key}</guid>'
            f"<pubDate>{pub}</pubDate>"
            f"<description><![CDATA[{body}]]></description>"
            "</item>"
        )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<rss version="2.0"><channel>'
        "<title>Monbijou Theater – Wochenprogramm</title>"
        f"<link>{site_url}</link>"
        "<description>Alle Vorstellungen pro Woche</description>"
        "<language>de-de</language>"
        f"<lastBuildDate>{now}</lastBuildDate>"
        + "".join(items)
        + "</channel></rss>\n"
    )


def build_index(events: list[Event], site_url: str) -> str:
    def ticket_cell(e: Event) -> str:
        if e.sold_out:
            return "ausverkauft"
        return '<a href="%s">Tickets</a>' % html.escape(e.ticket_url)

    rows = "".join(
        f"<tr><td>{fmt_date(datetime.fromisoformat(e.start))}</td>"
        f"<td><b>{html.escape(e.title)}</b><br><small>{html.escape(e.subtitle)}</small></td>"
        f"<td>{html.escape(e.venue)}</td>"
        f"<td>{ticket_cell(e)}</td></tr>"
        for e in events[:40]
    )
    updated = datetime.now(TZ).strftime("%d.%m.%Y %H:%M")
    return f"""<!doctype html>
<html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Monbijou Theater – Spielplan-Feeds</title>
<style>
body{{font-family:system-ui,sans-serif;max-width:900px;margin:2rem auto;padding:0 1rem;color:#222}}
code{{background:#f2f2f2;padding:2px 6px;border-radius:4px;word-break:break-all}}
table{{border-collapse:collapse;width:100%}}td{{border-bottom:1px solid #ddd;padding:.4rem;vertical-align:top}}
</style></head><body>
<h1>Monbijou Theater – Spielplan-Feeds</h1>
<p>Automatisch aus <a href="{BASE}/">monbijou.billeto.net</a> erzeugt. Stand: {updated} · {len(events)} Termine.</p>
<ul>
<li>Kalender (alle): <code>{site_url}termine.ics</code></li>
<li>Kalender (English): <code>{site_url}termine-englisch.ics</code></li>
<li>RSS (Produktionen): <code>{site_url}produktionen.xml</code></li>
<li>RSS (Wochenprogramm): <code>{site_url}wochenprogramm.xml</code></li>
<li>JSON: <code>{site_url}events.json</code></li>
</ul>
<h2>Nächste Termine</h2>
<table>{rows}</table>
</body></html>
"""


def write_if_changed(path: Path, content: str, ignore: re.Pattern | None = None) -> bool:
    """Schreibt nur, wenn sich der Inhalt (ohne Zeitstempel) geändert hat → keine Leer-Commits."""
    if path.exists():
        old = path.read_text(encoding="utf-8")
        a, b = (ignore.sub("", old), ignore.sub("", content)) if ignore else (old, content)
        if a == b:
            return False
    path.write_text(content, encoding="utf-8", newline="")
    return True


def main() -> int:
    site_url = (sys.argv[1] if len(sys.argv) > 1 else "https://monbijoutheater.github.io/monbijou-feeds/").lower()
    events = scrape()
    if len(events) < 3:
        print("Weniger als 3 Termine gefunden – Seitenaufbau geändert? Abbruch ohne Überschreiben.",
              file=sys.stderr)
        return 1

    OUT.mkdir(exist_ok=True)
    stamp = re.compile(r"DTSTAMP:\S+|<lastBuildDate>[^<]*</lastBuildDate>|Stand: [^·]*·|\"generated\": \"[^\"]*\"")
    english = [e for e in events if e.english]
    prods = productions(events)

    changed = [
        write_if_changed(OUT / "termine.ics", build_ics(events, "Monbijou Theater"), stamp),
        write_if_changed(OUT / "termine-englisch.ics", build_ics(english, "Monbijou Theater – English"), stamp),
        write_if_changed(OUT / "produktionen.xml", build_rss(prods, site_url, previous_pubdates(OUT / "produktionen.xml")), stamp),
        write_if_changed(OUT / "wochenprogramm.xml", build_weekly_rss(
            events, site_url, previous_pubdates(OUT / "wochenprogramm.xml")), stamp),
        write_if_changed(OUT / "events.json", json.dumps(
            {"generated": datetime.now(TZ).isoformat(), "source": BASE,
             "events": [asdict(e) for e in events], "productions": prods},
            ensure_ascii=False, indent=2), stamp),
        write_if_changed(OUT / "index.html", build_index(events, site_url), stamp),
    ]
    (OUT / ".nojekyll").touch()
    print(f"{len(events)} Termine, {len(prods)} Produktionen, {len(english)} englisch. "
          f"{'Dateien aktualisiert.' if any(changed) else 'Keine Änderungen.'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
