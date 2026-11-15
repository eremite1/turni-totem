"""
Turni per CASA NEROLA (Home Assistant).

Legge dal totem TUTTI i turni da ieri fino a 3 settimane avanti (i turni escono il venerdi per la
seconda settimana successiva; oltre ci sono solo i riposi),
compresi quelli cambiati (il totem restituisce il turno effettivo, cioè
dopo i cambi, piu' lo stato delle richieste) e li manda alla casa su un
canale ntfy privato (Secret NTFY_TOPIC_CASA).

ATTENZIONE: il repository e' pubblico. Questo programma NON stampa mai
nei registri il contenuto dei turni, nomi o matricole.
"""
import json
import os
import sys
from datetime import datetime, timedelta

import requests

from turni import ROMA, Totem, leggi_giorni, log

TOPIC_CASA = os.environ.get("NTFY_TOPIC_CASA", "").strip()
GIORNI_AVANTI = int(os.environ.get("GIORNI_AVANTI", "21"))
GIORNI_DETTAGLIO = 3  # oggi, domani, dopodomani: con i dettagli completi
LIMITE_BYTE = 3900    # ntfy accetta messaggi fino a 4096 byte

# Campi del dettaglio da NON mandare mai (dati personali)
ESCLUSI = ("MATRICOLA", "NOME", "COGNOME", "CODFISC", "CODICE_FISCALE", "TELEFONO", "EMAIL")


def dettagli_utili(dati):
    """Dai dati grezzi del giorno tiene solo i campi utili (orari, linea...)."""
    out = {}
    for chiave, valore in dati.items():
        nome = chiave.split(".", 1)[-1].split("#")[0].upper()
        if any(e in nome for e in ESCLUSI) or nome in ("ID", "SEQ"):
            continue
        v = str(valore).strip()
        if not v or v.lower() in ("none", "null", "0"):
            continue
        out[chiave] = v[:60]
    return out


def costruisci(letti, oggi):
    giorni = {}
    for iso in sorted(letti):
        c = letti[iso]
        voce = {"t": c.get("testo", ""), "s": c.get("stato", ""), "c": c.get("colore", "")}
        if (datetime.fromisoformat(iso).date() - oggi).days in range(GIORNI_DETTAGLIO):
            voce["x"] = dettagli_utili(c.get("dati", {}))
        giorni[iso] = voce
    pacco = {"v": 1, "agg": datetime.now(ROMA).strftime("%Y-%m-%d %H:%M"), "g": giorni}
    testo = json.dumps(pacco, ensure_ascii=False, separators=(",", ":"))
    # Se troppo lungo, tolgo i dettagli dai giorni piu' lontani
    for iso in sorted(giorni, reverse=True):
        if len(testo.encode("utf-8")) <= LIMITE_BYTE:
            break
        giorni[iso].pop("x", None)
        testo = json.dumps(pacco, ensure_ascii=False, separators=(",", ":"))
    # Se ancora troppo lungo, accorcio il periodo
    while len(testo.encode("utf-8")) > LIMITE_BYTE and len(giorni) > 3:
        giorni.pop(max(giorni))
        testo = json.dumps(pacco, ensure_ascii=False, separators=(",", ":"))
    return testo, len(giorni)


def main():
    if not TOPIC_CASA:
        log("Secret NTFY_TOPIC_CASA non impostato: niente da fare")
        return
    oggi = datetime.now(ROMA).date()
    date = [oggi + timedelta(days=d) for d in range(-1, GIORNI_AVANTI + 1)]
    totem = Totem()
    if not totem.accedi():
        log("Accesso al totem non riuscito")
        sys.exit(1)
    letti = leggi_giorni(totem, date)
    if letti is None:
        log("Sessione scaduta subito dopo l'accesso")
        sys.exit(1)
    if not letti:
        log("Il totem non ha restituito turni")
        sys.exit(1)
    testo, n = costruisci(letti, oggi)
    r = requests.post(f"https://ntfy.sh/{TOPIC_CASA}", data=testo.encode("utf-8"),
                      headers={"Title": "casa-turni", "Tags": "house", "Priority": "min"},
                      timeout=30)
    if r.status_code != 200:
        log(f"Invio alla casa non riuscito (risposta {r.status_code})")
        sys.exit(1)
    log(f"Turni mandati alla casa: {n} giorni")


if __name__ == "__main__":
    main()
