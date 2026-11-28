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
import re
import sys
from datetime import datetime, timedelta

import requests

from turni import ROMA, Totem, leggi_giorni, log

TOPIC_CASA = os.environ.get("NTFY_TOPIC_CASA", "").strip()
GIORNI_AVANTI = int(os.environ.get("GIORNI_AVANTI", "21"))
GIORNI_DETTAGLIO = 2  # oggi e domani: anche i dettagli grezzi (per controllo)
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


def _ora(v):
    m = re.search(r"(\d{1,2})[:.](\d{2})", str(v))
    return f"{int(m.group(1)):02d}:{m.group(2)}" if m else ""


def _min(hhmm):
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def orari_e_luoghi(dati):
    """Dai campi del dettaglio ricava allocazione (linea-tabella), luoghi e orari.
    I nomi dei campi del totem non sono noti a priori: li riconosco dalle
    parole che contengono.

    Turni SPEZZATI (piu' pezzi nello stesso giorno): ogni riga del totem e' un
    pezzo. Le righe non arrivano sempre in ordine, quindi NON prendo "il primo
    inizio e l'ultima fine" che trovo, ma:
      - inizio = l'orario di inizio piu' presto,
      - fine   = l'orario di fine piu' tardi (se un pezzo finisce dopo
                 mezzanotte conta come giorno dopo),
      - p      = l'elenco dei pezzi in ordine, es. [["06:15","10:16"],["10:43","12:47"]].
    """
    righe = {}
    for chiave, valore in dati.items():
        fonte, resto = chiave.split(".", 1) if "." in chiave else ("", chiave)
        nome, _, i = resto.partition("#")
        righe.setdefault((fonte, i), {})[nome.upper()] = str(valore).strip()
    out = {"al": "", "li": "", "lf": ""}
    pezzi = []
    for (fonte, i) in sorted(righe, key=lambda x: (x[0], int(x[1]) if x[1].isdigit() else 0)):
        r = righe[(fonte, i)]
        oi = of = li = lf = ""
        for k, v in r.items():
            if not v or v.lower() in ("none", "null"):
                continue
            ini = "INI" in k or "PART" in k or "INIZ" in k
            fin = "FIN" in k or "ARRIV" in k
            if "ALLOC" in k and not out["al"]:
                out["al"] = v[:20]
            elif "LUOG" in k and ini:
                li = v[:20]
            elif "LUOG" in k and fin:
                lf = v[:20]
            elif ("ORA" in k or "ORARIO" in k) and ini and _ora(v):
                oi = _ora(v)
            elif ("ORA" in k or "ORARIO" in k) and fin and _ora(v):
                of = _ora(v)
        if oi:
            pezzi.append({"oi": oi, "of": of, "li": li, "lf": lf})
    if pezzi:
        # stesso pezzo letto da due fonti diverse: lo tengo una volta sola
        visti, unici = set(), []
        for p in pezzi:
            if (p["oi"], p["of"]) not in visti:
                visti.add((p["oi"], p["of"]))
                unici.append(p)
        unici.sort(key=lambda p: _min(p["oi"]))
        primo = unici[0]
        inizio = _min(primo["oi"])

        def fine_assoluta(p):
            if not p["of"]:
                return -1
            f = _min(p["of"])
            return f + 1440 if f <= _min(p["oi"]) or f < inizio else f
        ultimo = max(unici, key=fine_assoluta)
        out["oi"] = primo["oi"]
        out["li"] = primo["li"] or out["li"]
        if ultimo["of"]:
            out["of"] = ultimo["of"]
            out["lf"] = ultimo["lf"] or out["lf"]
        if len(unici) > 1:
            out["p"] = [[p["oi"], p["of"]] for p in unici]
    return {k: v for k, v in out.items() if v}


def costruisci(letti, oggi):
    giorni = {}
    for iso in sorted(letti):
        c = letti[iso]
        voce = {"t": c.get("testo", ""), "s": c.get("stato", ""), "c": c.get("colore", "")}
        voce.update(orari_e_luoghi(c.get("dati", {})))
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
