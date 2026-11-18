"""Scarica dal totem lo storico dei propri turni (tutti i giorni disponibili) per migliorare
CarlettiBus (numeri delle tabelle, uscite e rientri in rimessa).
I dati vengono CIFRATI prima di essere salvati: il file nel repository non è leggibile da nessuno
senza la chiave privata (che non è nel repository)."""
import json, os, subprocess, sys
from datetime import date, timedelta
import turni

DA = date.fromisoformat(os.environ.get("DA", "2023-09-01"))
A = date.today() + timedelta(days=21)


def main():
    t = turni.Totem()
    if not t.accedi():
        sys.exit("Accesso al totem non riuscito")
    righe = {"turno": [], "dettaglio": []}
    d = DA
    while d <= A:
        f = min(A, d + timedelta(days=30))
        di, df = d.strftime("%Y-%m-%d 00:00:00"), f.strftime("%Y-%m-%d 23:59:59")
        for matr in ([t.matricola] if t.matricola else turni.matricole_possibili()):
            tu = turni.scarica(t, "bdrop.aspx?sStatus=0", {"strMatricola": matr, "dataInizio": di, "dataFine": df})
            if tu:
                t.matricola = matr
                righe["turno"] += tu
                break
        if t.matricola:
            de = turni.scarica(t, "bdrop.aspx?sStatus=6", {"strMatricola": t.matricola, "dataInizio": di, "dataFine": df}) or []
            righe["dettaglio"] += de
        print(d, "->", f, "turni", len(righe["turno"]), "dettagli", len(righe["dettaglio"]))
        d = f + timedelta(days=1)
    # niente dati personali inutili: tolgo nome e matricola dai campi
    for r in righe["turno"] + righe["dettaglio"]:
        for k in list(r):
            if any(x in k.upper() for x in ("NOME", "COGNOME", "MATRICOLA", "NOMINATIVO")):
                del r[k]
    json.dump(righe, open("storico.json", "w"), default=str)
    # cifratura: chiave casuale AES, a sua volta cifrata con la chiave pubblica
    subprocess.run("openssl rand -hex 32 > k.txt && "
                   "openssl enc -aes-256-cbc -pbkdf2 -salt -in storico.json -out storico.bin -pass file:k.txt && "
                   "openssl pkeyutl -encrypt -pubin -inkey chiave_storico.pem -in k.txt -out storico.key && "
                   "rm -f k.txt storico.json", shell=True, check=True)
    print("Fatto: giorni con turno", len({str(r.get('DATAATTIVITA'))[:10] for r in righe['turno']}))


if __name__ == "__main__":
    main()
