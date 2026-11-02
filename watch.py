#!/usr/bin/env python3
"""
Mannheim Standesamt - Termin-Watcher & Auto-Bucher
Läuft als 24/7 Server-Prozess auf Railway.app
Credentials werden als Umgebungsvariablen gesetzt (nicht im Code).
"""

import os
import sys
import time
import logging
import threading
import requests
from http.server import HTTPServer, BaseHTTPRequestHandler
from bs4 import BeautifulSoup

# ── Konfiguration aus Umgebungsvariablen ──────────────────────────────────────
TELEGRAM_TOKEN   = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
VORNAME          = os.environ["VORNAME"]
NACHNAME         = os.environ["NACHNAME"]
MOBIL            = os.environ["MOBIL"]
EMAIL            = os.environ["EMAIL"]

POLL_INTERVAL = 60  # 1 Minute
BASE_URL      = "https://tempus-termine.com/termine/index.php"
ANLAGEN_NR    = 36
OPTION_ID     = "EHE_DT-11887-mittermin"  # Anmeldung Eheschließung - Deutsche

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger(__name__)


def telegram(msg: str):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"},
            timeout=10,
        )
    except Exception as e:
        log.warning(f"Telegram-Fehler: {e}")


def get_soup(session, url, method="GET", data=None):
    if method == "POST":
        r = session.post(url, data=data, timeout=20)
    else:
        r = session.get(url, timeout=20)
    r.raise_for_status()
    return BeautifulSoup(r.text, "html.parser")


def full_url(path: str) -> str:
    if path.startswith("?"):
        return BASE_URL + path
    if path.startswith("index.php"):
        return BASE_URL[: BASE_URL.index("index.php")] + path
    return path


def skip_hinweis(session, soup):
    keywords = [
        "Wichtiger Hinweis",
        "Ich habe die Hinweise zur Kenntnis genommen",
        "Habe ich zur Kenntnis genommen",
        "Bestätigen und weiter zur Terminauswahl",
    ]
    if not any(kw in str(soup) for kw in keywords):
        return soup
    btn = soup.find("input", {"type": "button"}) or soup.find("input", {"type": "submit"})
    if not btn:
        return soup
    onclick = btn.attrs.get("onclick", "")
    idx = onclick.find("href='?")
    if idx == -1:
        return soup
    url_part = onclick[idx + 6 : onclick.index("'", idx + 6)]
    return get_soup(session, full_url(url_part))


def find_free_slot(session):
    """Gibt (standort_name, slot_url) oder None zurück."""
    soup = get_soup(session, f"{BASE_URL}?anlagennr={ANLAGEN_NR}")
    form = soup.find("form", {"id": "formular"}) or soup.find("form")
    action = form.attrs["action"]
    post_url = full_url(action) if action.startswith("?") else BASE_URL[: BASE_URL.index("index.php")] + action

    soup2 = get_soup(session, post_url, method="POST", data={OPTION_ID: "1"})
    soup2 = skip_hinweis(session, soup2)

    # Standorte finden
    candidates = [
        a for a in soup2.find_all("a", {"class": "infolink"})
        if "Uhr" not in a.text and "standortrowid" in a.attrs.get("href", "")
    ]
    if not candidates:
        ul = soup2.find("ul", {"id": "nav_menu2"})
        if ul:
            candidates = [
                a for li in ul.find_all("li")
                for a in [li.find("a")]
                if a and "Uhr" not in a.text and "standortrowid" in a.attrs.get("href", "")
            ]

    for loc_a in candidates:
        standort = loc_a.text.strip()
        soup3 = get_soup(session, full_url(loc_a.attrs["href"]))
        for day_td in soup3.find_all("td", {"class": "monatevent"}):
            day_a = day_td.find("a")
            if not day_a:
                continue
            soup4 = get_soup(session, full_url(day_a.attrs["href"]))
            table = soup4.find("table", {"class": "termine"})
            if not table:
                continue
            for slot_a in table.find_all("a"):
                href = slot_a.attrs.get("href", "")
                if "page=appointment" in href:
                    return standort, full_url(href)
    return None


def book_slot(session, slot_url: str) -> bool:
    """Bucht den Termin. Gibt True bei Erfolg zurück."""
    soup = get_soup(session, slot_url)
    form = next((f for f in soup.find_all("form") if "save_ap" in f.attrs.get("action", "")), None)
    if not form:
        log.error("Buchungsformular nicht gefunden")
        return False

    action = form.attrs["action"]
    post_url = full_url(action) if action.startswith("?") else BASE_URL[: BASE_URL.index("index.php")] + action

    data = {inp.attrs["name"]: inp.attrs.get("value", "") for inp in form.find_all("input", {"type": "hidden"})}
    data.update({
        "vorname_mandatory":    VORNAME,
        "nachname":             NACHNAME,
        "mobilnummer_mandatory": MOBIL,
        "email":                EMAIL,
        "email2":               EMAIL,
    })

    soup_confirm = get_soup(session, post_url, method="POST", data=data)
    page_text = soup_confirm.get_text()

    success_keywords = ["Buchungsnummer", "bestätigt", "erfolgreich", "Termin wurde"]
    if any(kw in page_text for kw in success_keywords):
        for line in page_text.splitlines():
            if any(kw in line for kw in success_keywords):
                log.info(line.strip())
        return True

    log.warning("Buchungsantwort unklar: " + page_text[:300])
    return False


def new_session():
    s = requests.Session()
    s.headers.update({"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"})
    return s


def run():
    log.info("▶ Mannheim Standesamt Watcher gestartet")
    log.info(f"  Buchung für: {VORNAME} {NACHNAME}")
    log.info(f"  Polling alle {POLL_INTERVAL}s")
    telegram(
        "▶ <b>Mannheim Termin-Watcher aktiv</b>\n"
        f"Überwache: Anmeldung Eheschließung (DE)\n"
        f"Buchung für: {VORNAME} {NACHNAME}\n"
        "Polling alle 5 Minuten — 24/7"
    )

    consecutive_errors = 0

    while True:
        try:
            session = new_session()
            log.info("🔍 Prüfe Verfügbarkeit...")
            result = find_free_slot(session)
            consecutive_errors = 0

            if result:
                standort, slot_url = result
                log.info(f"🎉 Freier Termin: {standort}")
                telegram(f"🎉 <b>Freier Termin gefunden!</b>\nStandort: {standort}\n⏳ Buche automatisch...")

                if book_slot(session, slot_url):
                    telegram(
                        f"✅ <b>Termin erfolgreich gebucht!</b>\n"
                        f"Name: {VORNAME} {NACHNAME}\n"
                        f"Standort: {standort}\n"
                        f"Bestätigung → {EMAIL}"
                    )
                    log.info("✅ Termin gebucht. Script beendet.")
                    sys.exit(0)
                else:
                    telegram("⚠️ Buchung fehlgeschlagen — Termin war evtl. schon weg. Weiter polling...")
            else:
                log.info("Keine freien Termine.")

        except Exception as e:
            consecutive_errors += 1
            log.error(f"Fehler ({consecutive_errors}): {e}")
            if consecutive_errors >= 5:
                telegram(f"⚠️ {consecutive_errors} Fehler in Folge: {e}")
                consecutive_errors = 0

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    # HTTP-Server damit Render den Port-Check besteht und nicht schläft
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"Mannheim Watcher running")
        def log_message(self, *args):
            pass

    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    # Selbst-Ping alle 10 Minuten damit Render nicht schläft
    def self_ping():
        url = os.environ.get("RENDER_EXTERNAL_URL", f"http://localhost:{port}")
        while True:
            time.sleep(600)
            try:
                requests.get(url, timeout=5)
            except Exception:
                pass
    threading.Thread(target=self_ping, daemon=True).start()

    run()
