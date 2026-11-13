"""
Controllo turni sul Totem ATAC con notifica ntfy (e WhatsApp CallMeBot se attivato).

Regole (date dall'utente):
- si aggiorna solo il turno del giorno dopo;
- il sabato si aggiornano domenica e lunedi' (in momenti diversi);
- la domenica non si aggiorna nulla (il programma non gira);
- controllo ogni minuto dalle 9:00 alle 14:00 (ora italiana).

ATTENZIONE: il repository e' pubblico. Questo programma NON deve mai stampare
nei registri (log) il contenuto dei turni, nomi o matricole.
"""

import hashlib
import json
import os
import re
import sys
import time
import urllib.parse
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

ROMA = ZoneInfo("Europe/Rome")
URL_LOGIN = os.environ.get("TOTEM_URL", "https://turni.atac.roma.it/totem/logon.aspx")
URL_TOTEM = urllib.parse.urljoin(URL_LOGIN, "totem.aspx")
UTENTE = os.environ.get("TOTEM_UTENTE", "")
PASSWORD = os.environ.get("TOTEM_PASSWORD", "")
WA_NUMERO = os.environ.get("WHATSAPP_NUMERO", "")
WA_APIKEY = os.environ.get("CALLMEBOT_APIKEY", "")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")

INTERVALLO_SECONDI = int(os.environ.get("INTERVALLO_SECONDI", "60"))
ORA_INIZIO = 9
ORA_FINE = 14
FILE_STATO = "stato/stato.json"
MODALITA_TEST = os.environ.get("MODALITA_TEST", "false").lower() == "true"

MESI = ["Gennaio", "Febbraio", "Marzo", "Aprile", "Maggio", "Giugno", "Luglio",
        "Agosto", "Settembre", "Ottobre", "Novembre", "Dicembre"]
GIORNI = ["Lunedì", "Martedì", "Mercoledì", "Giovedì", "Venerdì", "Sabato", "Domenica"]

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/130 Safari/537.36"}


def log(msg):
    print(f"[{datetime.now(ROMA):%H:%M:%S}] {msg}", flush=True)


# ---------------------------------------------------------------- Notifiche
def ntfy(testo):
    for tentativo in range(3):
        try:
            r = requests.post(
                f"https://ntfy.sh/{NTFY_TOPIC}",
                data=testo.encode("utf-8"),
                headers={"Title": "Turni Totem", "Priority": "high", "Tags": "bus"},
                timeout=30,
            )
            if r.status_code == 200:
                log("Notifica ntfy inviata")
                return True
            log(f"ntfy: risposta {r.status_code}, riprovo")
        except requests.RequestException:
            log("ntfy: errore di rete, riprovo")
        time.sleep(10)
    return False


def whatsapp(testo):
    """Manda la notifica: ntfy e/o WhatsApp, a seconda dei Secrets presenti."""
    ok = False
    if NTFY_TOPIC:
        ok = ntfy(testo) or ok
    if WA_NUMERO and WA_APIKEY:
        ok = whatsapp_callmebot(testo) or ok
    if not (NTFY_TOPIC or (WA_NUMERO and WA_APIKEY)):
        log("Nessuna notifica configurata (mancano i Secrets)")
    return ok


def whatsapp_callmebot(testo):
    for tentativo in range(3):
        try:
            r = requests.get(
                "https://api.callmebot.com/whatsapp.php",
                params={"phone": WA_NUMERO, "text": testo, "apikey": WA_APIKEY},
                timeout=30,
            )
            if r.status_code == 200:
                log("Messaggio WhatsApp inviato")
                return True
            log(f"WhatsApp: risposta {r.status_code}, riprovo")
        except requests.RequestException:
            log("WhatsApp: errore di rete, riprovo")
        time.sleep(10)
    return False


# ---------------------------------------------------------------- Totem
def campi_form(soup):
    """Tutti i campi nascosti/precompilati del form ASP.NET."""
    dati = {}
    for inp in soup.find_all("input"):
        nome = inp.get("name")
        tipo = (inp.get("type") or "text").lower()
        if not nome or tipo in ("submit", "button", "image", "checkbox", "radio"):
            continue
        dati[nome] = inp.get("value", "")
    return dati


def ha_campo_password(soup):
    return soup.find("input", {"type": "password"}) is not None


class Totem:
    def __init__(self):
        self.s = requests.Session()
        self.s.headers.update(HEADERS)
        self.pagina = None  # ultimo HTML del calendario (soup)

    def accedi(self):
        r = self.s.get(URL_LOGIN, timeout=30)
        soup = BeautifulSoup(r.text, "html.parser")
        form = soup.find("form")
        dati = campi_form(soup)
        campo_pw = soup.find("input", {"type": "password"})
        campo_utente = None
        for inp in soup.find_all("input"):
            if (inp.get("type") or "text").lower() in ("text", "email", "tel") and inp.get("name"):
                campo_utente = inp
                break
        if not (campo_pw and campo_utente):
            raise RuntimeError("Pagina di accesso non riconosciuta")
        dati[campo_utente["name"]] = UTENTE
        dati[campo_pw["name"]] = PASSWORD
        # pulsante "Continua" (non quelli di intranet/intramobile)
        for b in soup.find_all(["input", "button"]):
            testo = (b.get("value") or b.get_text() or "").strip().lower()
            if testo == "continua" and b.get("name"):
                dati[b["name"]] = b.get("value", "")
                break
        azione = urllib.parse.urljoin(r.url, form.get("action") or r.url) if form else r.url
        r2 = self.s.post(azione, data=dati, timeout=30)
        soup2 = BeautifulSoup(r2.text, "html.parser")
        if ha_campo_password(soup2):
            return False
        self.pagina = soup2 if "totem" in r2.url.lower() else None
        return True

    def carica_calendario(self):
        r = self.s.get(URL_TOTEM, timeout=30)
        soup = BeautifulSoup(r.text, "html.parser")
        if ha_campo_password(soup) or "logon" in r.url.lower():
            return None  # sessione scaduta
        self.pagina = soup
        self.url_pagina = r.url
        return soup

    def mese_successivo(self):
        """Clicca '>>' per passare al mese dopo (postback ASP.NET)."""
        soup = self.pagina
        dati = campi_form(soup)
        bersaglio = None
        for el in soup.find_all(["a", "input", "button"]):
            testo = (el.get("value") or el.get_text() or "").replace(" ", "").strip()
            if testo in (">>", "»", "&gt;&gt;"):
                bersaglio = el
                break
        if bersaglio is None:
            raise RuntimeError("Pulsante mese successivo non trovato")
        href = bersaglio.get("href") or ""
        m = re.search(r"__doPostBack\('([^']*)','([^']*)'\)", href)
        if m:
            dati["__EVENTTARGET"] = m.group(1)
            dati["__EVENTARGUMENT"] = m.group(2)
        elif bersaglio.get("name"):
            dati[bersaglio["name"]] = bersaglio.get("value", "")
        else:
            raise RuntimeError("Pulsante mese successivo non utilizzabile")
        form = soup.find("form")
        azione = urllib.parse.urljoin(self.url_pagina, form.get("action") if form else "")
        r = self.s.post(azione, data=dati, timeout=30)
        nuova = BeautifulSoup(r.text, "html.parser")
        if ha_campo_password(nuova):
            return None
        self.pagina = nuova
        return nuova


def mese_mostrato(soup):
    testo = soup.get_text(" ", strip=True)
    m = re.search(r"(" + "|".join(MESI) + r")\s*-\s*(\d{4})", testo)
    if not m:
        return None
    return MESI.index(m.group(1)) + 1, int(m.group(2))


def colore(el):
    """Colore/stile della casella, per accorgersi anche dei cambi di colore."""
    parti = []
    for x in [el] + el.find_all(True):
        stile = (x.get("style") or "").lower()
        m = re.search(r"background(?:-color)?\s*:\s*([^;]+)", stile)
        if m:
            parti.append(m.group(1).strip())
        if x.get("bgcolor"):
            parti.append(x["bgcolor"].lower())
        if x.get("class"):
            parti.append(".".join(x["class"]))
    return "|".join(parti)


RE_CASELLA = re.compile(r"^(\d{1,2})(?:\s+(.*))?$")


def leggi_caselle(soup):
    """Ritorna {giorno: {"testo": ..., "colore": ...}} per il mese mostrato.
    Funziona sia con tabelle (td) sia con caselle fatte di div/span."""
    candidati = []
    for el in soup.find_all(["td", "div", "li", "span", "a", "button", "p"]):
        testo = " ".join(el.get_text(" ", strip=True).split())
        m = RE_CASELLA.match(testo)
        if not m or not 1 <= int(m.group(1)) <= 31:
            continue
        candidati.append((el, testo, m))
    insieme = {id(c[0]) for c in candidati}
    caselle = {}
    for el, testo, m in candidati:
        # se un elemento interno ha lo stesso identico testo, uso quello interno
        giorno = int(m.group(1))
        salta = False
        for d in el.find_all(True):
            if id(d) not in insieme:
                continue
            td = " ".join(d.get_text(" ", strip=True).split())
            md = RE_CASELLA.match(td)
            # contiene un'altra casella (giorno diverso) = e' un contenitore
            # oppure un elemento interno ha lo stesso testo = uso quello interno
            if td == testo or int(md.group(1)) != giorno:
                salta = True
                break
        if salta:
            continue
        voce = {"testo": (m.group(2) or "").strip(), "colore": colore(el)}
        attuale = caselle.get(giorno)
        # tengo la versione piu' completa (con il turno scritto)
        if attuale is None or len(voce["testo"]) > len(attuale["testo"]):
            caselle[giorno] = voce
    return caselle


def diagnosi(soup, url):
    """Stampa solo la STRUTTURA della pagina (niente nomi, niente turni)."""
    log(f"DIAGNOSI pagina: {urllib.parse.urlparse(url).path}")
    log(f"DIAGNOSI mese trovato: {mese_mostrato(soup) is not None}")
    log(f"DIAGNOSI caselle lette: {len(leggi_caselle(soup))}")
    conteggi = {t: len(soup.find_all(t)) for t in ["table", "td", "div", "iframe", "frame", "form", "script"]}
    log(f"DIAGNOSI elementi: {conteggi}")
    for f in soup.find_all(["iframe", "frame"]):
        log(f"DIAGNOSI frame: {f.get('src')}")
    for sc in soup.find_all("script"):
        if sc.get("src"):
            log(f"DIAGNOSI script: {sc.get('src')}")
    for el in soup.find_all(["a", "input", "button", "img", "span"]):
        t = (el.get("value") or el.get_text() or el.get("alt") or el.get("title") or "").replace(" ", "")
        if t in (">>", "»", "<<", "«", ">", "<") or "succ" in (el.get("id") or "").lower() or "next" in (el.get("id") or "").lower():
            log(f"DIAGNOSI freccia: <{el.name}> id={el.get('id')} name={el.get('name')} "
                f"href={'postback' if '__doPostBack' in (el.get('href') or '') else el.get('href')} "
                f"onclick={'si' if el.get('onclick') else 'no'} testo={t}")


def giorni_da_controllare(oggi):
    """Domani; il sabato anche il lunedi'. La domenica niente."""
    wd = oggi.weekday()  # 0=lun ... 5=sab, 6=dom
    if wd == 6:
        return []
    giorni = [oggi + timedelta(days=1)]
    if wd == 5:
        giorni.append(oggi + timedelta(days=2))
    return giorni


def leggi_giorni(totem, date):
    """Legge le caselle delle date richieste, anche se cadono nel mese dopo."""
    risultato = {}
    soup = totem.carica_calendario()
    if soup is None:
        return None
    mm = mese_mostrato(soup)
    caselle = leggi_caselle(soup)
    per_mese_dopo = []
    for d in date:
        if mm == (d.month, d.year) and d.day in caselle:
            risultato[d.isoformat()] = caselle[d.day]
        else:
            per_mese_dopo.append(d)
    if per_mese_dopo:
        soup2 = totem.mese_successivo()
        if soup2 is None:
            return None
        mm2 = mese_mostrato(soup2)
        caselle2 = leggi_caselle(soup2)
        for d in per_mese_dopo:
            if mm2 == (d.month, d.year) and d.day in caselle2:
                risultato[d.isoformat()] = caselle2[d.day]
    return risultato


# ---------------------------------------------------------------- Stato
def carica_stato():
    try:
        with open(FILE_STATO, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def salva_stato(stato):
    os.makedirs(os.path.dirname(FILE_STATO), exist_ok=True)
    # tieni solo gli ultimi 20 giorni
    limite = (datetime.now(ROMA).date() - timedelta(days=20)).isoformat()
    stato = {k: v for k, v in stato.items() if k >= limite}
    with open(FILE_STATO, "w", encoding="utf-8") as f:
        json.dump(stato, f, ensure_ascii=False)


def nome_giorno(iso):
    d = datetime.fromisoformat(iso).date()
    return f"{GIORNI[d.weekday()]} {d.day:02d}/{d.month:02d}"


def impronta(casella):
    return hashlib.sha256(json.dumps(casella, sort_keys=True).encode()).hexdigest()[:12]


# ---------------------------------------------------------------- Programma
def main():
    if not (UTENTE and PASSWORD):
        log("Mancano TOTEM_UTENTE / TOTEM_PASSWORD nei Secrets")
        sys.exit(1)

    adesso = datetime.now(ROMA)
    oggi = adesso.date()
    date = giorni_da_controllare(oggi)
    if not date and not MODALITA_TEST:
        log("Oggi è domenica: nessun turno da controllare")
        return
    if MODALITA_TEST and not date:
        date = [oggi + timedelta(days=1)]
    # GitHub parte due volte (per gestire ora legale/solare): la partenza
    # troppo presto viene scartata, ci pensa quella dopo.
    if not MODALITA_TEST and adesso.hour < 8:
        log("Troppo presto: ci pensa l'avvio successivo")
        return

    totem = Totem()
    if not totem.accedi():
        log("Accesso al totem NON riuscito")
        whatsapp("⚠️ Turni totem: accesso non riuscito. Controlla utente e password nei Secrets di GitHub.")
        sys.exit(1)
    log("Accesso al totem riuscito")

    stato = carica_stato()

    if MODALITA_TEST:
        soup = totem.carica_calendario()
        if soup is not None:
            diagnosi(soup, totem.url_pagina)
        try:
            letti = leggi_giorni(totem, date) or {}
        except Exception as e:  # noqa: BLE001
            log(f"Errore lettura: {e}")
            letti = {}
        righe = [f"{nome_giorno(k)}: {v['testo'] or '(vuoto)'}" for k, v in letti.items()]
        whatsapp("✅ Prova sistema turni riuscita!\n" + ("\n".join(righe) or "Nessuna casella letta"))
        for k, v in letti.items():
            stato[k] = v
        salva_stato(stato)
        log(f"Test completato: lette {len(letti)} caselle")
        return

    # attesa fino alle 9:00
    inizio = datetime.combine(oggi, datetime.min.time(), ROMA).replace(hour=ORA_INIZIO)
    fine = datetime.combine(oggi, datetime.min.time(), ROMA).replace(hour=ORA_FINE)
    if datetime.now(ROMA) >= fine:
        log("Sono già passate le 14: esco")
        return
    attesa = (inizio - datetime.now(ROMA)).total_seconds()
    if attesa > 0:
        log(f"Aspetto le {ORA_INIZIO}:00 ({int(attesa // 60)} minuti)")
        time.sleep(attesa)

    errori_di_fila = 0
    avvisato_errore = False
    controlli = 0
    while datetime.now(ROMA) < fine:
        try:
            letti = leggi_giorni(totem, date)
            if letti is None:
                log("Sessione scaduta, rientro")
                if not totem.accedi():
                    whatsapp("⚠️ Turni totem: accesso non riuscito durante il controllo.")
                    sys.exit(1)
                letti = leggi_giorni(totem, date) or {}
            controlli += 1
            errori_di_fila = 0
            for iso, casella in letti.items():
                prima = stato.get(iso)
                if prima is None:
                    stato[iso] = casella  # prima volta: memorizzo
                    continue
                if impronta(prima) != impronta(casella):
                    if prima["testo"] != casella["testo"]:
                        msg = (f"🚌 Turno aggiornato!\n{nome_giorno(iso)}: "
                               f"{prima['testo'] or '(vuoto)'} ➜ {casella['testo'] or '(vuoto)'}")
                    else:
                        msg = (f"🚌 Turno modificato!\n{nome_giorno(iso)}: "
                               f"{casella['testo']} (è cambiato il colore della casella)")
                    if whatsapp(msg):
                        stato[iso] = casella
                    log("Novità trovata e notificata")
            salva_stato(stato)
            if controlli % 30 == 1:
                log(f"Controllo n. {controlli}: tutto regolare")
        except Exception as e:  # noqa: BLE001 - qualsiasi errore: riprovo
            errori_di_fila += 1
            log(f"Errore ({type(e).__name__}), riprovo")
            if errori_di_fila >= 15 and not avvisato_errore:
                whatsapp("⚠️ Turni totem: il controllo dà errori da 15 minuti. Il sito potrebbe essere giù.")
                avvisato_errore = True
            if errori_di_fila % 5 == 0:
                try:
                    totem = Totem()
                    totem.accedi()
                except Exception:  # noqa: BLE001
                    pass
        time.sleep(INTERVALLO_SECONDI)

    salva_stato(stato)
    log(f"Fine giornata: {controlli} controlli fatti")


if __name__ == "__main__":
    main()
