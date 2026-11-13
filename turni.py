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
NTFY_TOPIC_COLLEGHI = os.environ.get("NTFY_TOPIC_COLLEGHI", "")

INTERVALLO_SECONDI = int(os.environ.get("INTERVALLO_SECONDI", "60"))
ORA_INIZIO = 9
MINUTO_INIZIO = 30
MINUTI_DOPO_AGGIORNAMENTO = int(os.environ.get("MINUTI_DOPO_AGGIORNAMENTO", "15"))
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
def ntfy(testo, topic=None):
    topic = topic or NTFY_TOPIC
    for tentativo in range(3):
        try:
            r = requests.post(
                f"https://ntfy.sh/{topic}",
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
        self.matricola = None

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
        return True


def _righe(r):
    try:
        j = r.json()
    except ValueError:
        return None
    if not j.get("success", True):
        return None
    return j.get("jsonroot") or []


def scarica(totem, url, dati):
    r = totem.s.post(urllib.parse.urljoin(URL_TOTEM, url), data=dati, timeout=30,
                     headers={"X-Requested-With": "XMLHttpRequest"})
    if "logon" in r.url.lower():
        return None
    return _righe(r)


def matricole_possibili():
    m = os.environ.get("TOTEM_MATRICOLA", "").strip()
    cand = [m] if m else []
    cand.append(UTENTE.strip())
    cifre = re.sub(r"\D", "", UTENTE)
    if cifre and cifre not in cand:
        cand.append(cifre)
    return [c for c in cand if c]


def leggi_giorni(totem, date):
    """Legge turno e colore delle date richieste dai dati del totem.
    Ritorna {"AAAA-MM-GG": {"testo": ..., "colore": ...}} oppure None se la
    sessione e' scaduta."""
    inizio, fine = min(date), max(date)
    di = inizio.strftime("%Y-%m-%d 00:00:00")
    df = fine.strftime("%Y-%m-%d 23:59:59")
    nc = str(int(time.time() * 1000))
    turni = None
    for matr in ([totem.matricola] if getattr(totem, "matricola", None) else matricole_possibili()):
        turni = scarica(totem, "bdrop.aspx?sStatus=0",
                        {"strMatricola": matr, "dataInizio": di, "dataFine": df, "_nc": nc})
        if turni is None:
            return None
        if turni:
            totem.matricola = matr
            break
    if not turni:
        return {}
    colori = scarica(totem, "colorazioneCalendario.aspx",
                     {"Matricola": totem.matricola, "daData": di, "aData": df, "_nc": nc}) or []
    risultato = {}
    for d in date:
        iso = d.isoformat()
        att = [str(t.get("ATTIVITA_EFFETTIVA") or "").strip() for t in turni
               if str(t.get("DATAATTIVITA") or "")[:10] == iso]
        col = sorted(f"{c.get('seq')}:{c.get('status')}" for c in colori
                     if str(c.get("GiornoEvidenza") or "")[:10] == iso)
        ids = sorted(str(t.get("ID")) for t in turni if str(t.get("DATAATTIVITA") or "")[:10] == iso)
        stati = sorted({str(c.get("status") or "").strip() for c in colori
                        if str(c.get("GiornoEvidenza") or "")[:10] == iso} - {""})
        risultato[iso] = {"testo": " ".join(a for a in att if a), "colore": "|".join(col),
                          "stato": ", ".join(stati),
                          "id": "|".join(ids)}
    return risultato


def giorni_da_controllare(oggi):
    """Domani; il sabato anche il lunedi'. La domenica niente."""
    wd = oggi.weekday()  # 0=lun ... 5=sab, 6=dom
    if wd == 6:
        return []
    giorni = [oggi + timedelta(days=1)]
    if wd == 5:
        giorni.append(oggi + timedelta(days=2))
    return giorni


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
    stato = {k: v for k, v in stato.items() if k.startswith("_") or k >= limite}
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
    if not MODALITA_TEST and adesso.hour < 9:
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
        try:
            letti = leggi_giorni(totem, date) or {}
        except Exception as e:  # noqa: BLE001
            log(f"Errore lettura: {type(e).__name__}")
            letti = {}
        log(f"Test: caselle lette {len(letti)}, con turno scritto {sum(1 for v in letti.values() if v['testo'])}")
        log(f"Test ID: presenti {all(v.get('id') for v in letti.values())}")
        time.sleep(5)
        try:
            totem.matricola = None
            totem2 = Totem()
            totem2.accedi()
            letti2 = leggi_giorni(totem2, date) or {}
            stabili = all(letti2.get(k, {}).get("id") == v.get("id") for k, v in letti.items())
            log(f"Test ID: uguali dopo nuova lettura e nuovo accesso = {stabili}")
        except Exception as e:  # noqa: BLE001
            log(f"Test ID: errore {type(e).__name__}")
        righe = [f"{nome_giorno(k)}: {v['testo'] or '(vuoto)'} (ID {v.get('id') or '?'})" for k, v in letti.items()]
        whatsapp("✅ Prova sistema turni riuscita!\n" + ("\n".join(righe) or "Nessuna casella letta"))
        for k, v in letti.items():
            stato[k] = v
        salva_stato(stato)
        log(f"Test completato: lette {len(letti)} caselle")
        return

    # attesa fino alle 9:00
    if stato.get("_finito") == oggi.isoformat():
        log("Turni di oggi già aggiornati: niente da fare")
        return
    inizio = datetime.combine(oggi, datetime.min.time(), ROMA).replace(hour=ORA_INIZIO, minute=MINUTO_INIZIO)
    fine = datetime.combine(oggi, datetime.min.time(), ROMA).replace(hour=ORA_FINE)
    if datetime.now(ROMA) >= fine:
        log("Sono già passate le 14: esco")
        return
    attesa = (inizio - datetime.now(ROMA)).total_seconds()
    if attesa > 0:
        log(f"Aspetto le {ORA_INIZIO}:{MINUTO_INIZIO:02d} ({int(attesa // 60)} minuti)")
        time.sleep(attesa)

    errori_di_fila = 0
    avvisato_errore = False
    aggiornati = set()      # giorni il cui turno e' gia' uscito
    stop_alle = None        # quando fermarsi dopo l'uscita dei turni
    controlli = 0
    while datetime.now(ROMA) < fine:
        if stop_alle and datetime.now(ROMA) >= stop_alle:
            log("Turni usciti e controllo extra finito: mi fermo")
            stato["_finito"] = oggi.isoformat()
            break
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
                msg = None
                if prima.get("testo") != casella["testo"]:
                    msg = (f"🚌 Turno aggiornato!\n{nome_giorno(iso)}: "
                           f"{prima.get('testo') or '(vuoto)'} ➜ {casella['testo'] or '(vuoto)'}")
                elif prima.get("colore") != casella["colore"]:
                    msg = (f"🚌 Turno modificato!\n{nome_giorno(iso)}: {casella['testo']}\n"
                           f"Stato richiesta: {prima.get('stato') or '(nessuno)'} ➜ {casella.get('stato') or '(nessuno)'}")
                elif prima.get("id") and casella.get("id") and prima["id"] != casella["id"]:
                    msg = (f"🔄 Il gestionale ha aggiornato {nome_giorno(iso)}\n"
                           f"Turno invariato: {casella['testo'] or '(vuoto)'}")
                    log("ID cambiato con turno invariato")
                if msg is not None:
                    # ID solo nella notifica privata, mai nei registri pubblici
                    msg += f"\nID: {prima.get('id') or '?'} ➜ {casella.get('id') or '?'}"
                if msg is None:
                    if prima != casella:
                        stato[iso] = casella
                    continue
                if whatsapp(msg):
                    stato[iso] = casella
                log("Novità trovata e notificata")
                pubblicazione = not msg.startswith("🚌 Turno modificato")
                if pubblicazione:
                    aggiornati.add(iso)
                    if stop_alle is None and all(d.isoformat() in aggiornati for d in date):
                        # resto ancora un po' per violazioni / richieste respinte
                        stop_alle = datetime.now(ROMA) + timedelta(minutes=MINUTI_DOPO_AGGIORNAMENTO)
                        log(f"Tutti i turni usciti: controllo ancora {MINUTI_DOPO_AGGIORNAMENTO} minuti")
                if pubblicazione and NTFY_TOPIC_COLLEGHI and stato.get("_colleghi") != oggi.isoformat():
                    if ntfy("🚌 Variazioni aggiornate", NTFY_TOPIC_COLLEGHI):
                        stato["_colleghi"] = oggi.isoformat()
                        log("Avviso colleghi inviato")
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
