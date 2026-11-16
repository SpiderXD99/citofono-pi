# Citofono — pagina dei PIN e tunnel, su Raspberry Pi

Gestisci dal telefono i **PIN fissi**, i **codici ospite/temporanei** e le **tessere RFID** di un
videocitofono DNAKE (placca esterna), in casa e — con il tunnel — anche da fuori.
Tutto gira su un **Raspberry Pi** sulla stessa rete della placca: il Pi ospita la
pagina e il tunnel, e parla con la placca tramite la sua **web API sulla porta 80**
(autenticandosi con la normale password admin della placca). Non modifica il
firmware e non usa accessi di servizio dei dispositivi.

> **Senza Raspberry:** la stessa app può girare su **Fly.io**, con il monitor che si
> collega da solo tramite `xfrpc` (già nel suo firmware). Guida: [FLY.md](FLY.md).

## Come è fatto

- **`server.py`** — piccolo server (solo libreria standard di Python 3). Serve due
  pagine e fa da ponte verso la placca. Due sole rotte pubbliche:
  `/data/pin/` (pagina dei PIN) e `/admin` (pannello). Tutto il resto è 404.
- **`index.html`** — la pagina dei PIN.
- **`admin.html`** — il pannello: tunnel Cloudflare, indirizzi, password, aggiornamento.
- **`installa.sh`**, **`*.service`/`*.timer`** — installazione e servizi `systemd`.

## Sicurezza — da leggere

**Questo repository è pubblico e contiene solo codice.** I segreti **non sono qui**
e non devono mai entrarci. Vivono solo sul Pi, in `/etc/citofono/` (file `600`):

| file sul Pi | cosa contiene |
|---|---|
| `citofono.conf` | IP della placca, URL del monitor, opzioni |
| `admin.json` | utente + **hash PBKDF2** della password del pannello |
| `tunnel.env` | il **token** del tunnel Cloudflare |
| `cert.pem` / `key.pem` | certificato del server (autofirmato) |

Il `.gitignore` tiene questi file fuori dal repo per costruzione. Se un token
dovesse finire in un commit, va considerato compromesso e **rigenerato** su Cloudflare.

Altre scelte di sicurezza:
- il pannello admin ha una password propria (minimo 12 caratteri), con **blocco
  dopo pochi tentativi** sbagliati e sessione via cookie `HttpOnly`/`SameSite`;
- il tunnel instrada l'intero host: da Internet la pagina dei PIN è su `/` e il
  pannello su `/admin`. Il pannello è protetto **solo** dalla password: tienila
  lunga. Se preferisci non esporlo, chiudilo alla sola rete di casa con
  `ADMIN_LAN_ONLY=1` (allora da fuori resta solo la pagina dei PIN).

## Installazione sul Pi

Raspberry Pi OS (Lite va benissimo), sulla stessa rete della placca.

```bash
sudo apt update && sudo apt install -y git
git clone https://github.com/UTENTE/REPO.git /opt/citofono
cd /opt/citofono
sudo sh installa.sh
```

L'installer chiede l'IP della placca, l'URL del monitor (facoltativo) e la
password del pannello; installa `cloudflared`, crea il certificato e avvia il
servizio. Alla fine stampa gli indirizzi:

- pagina PIN: `https://IP_DEL_PI/`
- pannello: `https://IP_DEL_PI/admin`

Il browser avvisa del certificato autofirmato: è atteso, accetta.

## Tunnel Cloudflare (accesso da fuori)

Nel pannello, incolla il **token** del tunnel e premi **Avvia**. Su Cloudflare, nel
tunnel, imposta una rotta: hostname a tua scelta, **Path vuoto** (l'intero host),
servizio `https://localhost:443` (o `https://IP_DEL_PI:443`) con **No TLS Verify**.
Così da Internet la pagina è su `https://TUO-HOST/` e il pannello su
`https://TUO-HOST/admin`. Scrivi l'hostname anche nel campo **Indirizzo pubblico
del tunnel** del pannello: così, a tunnel attivo, ti mostra il link pronto.

## Tessere RFID

Nel modulo di una persona ci sono fino a 3 tessere; nella pagina c'è anche la **tessera
master** della placca. Il numero si legge in due modi:
- **Placca**: premi il pulsante e passa la tessera sul lettore della placca (entro 30 s).
  Funziona con qualunque telefono, anche iPhone.
- **NFC**: appoggi la tessera al telefono. Solo **Chrome su Android** (su iPhone una pagina
  web non può usare l'NFC). La prima volta la pagina chiede di passare la stessa tessera
  anche sulla placca, per capire come convertire l'UID nel numero della placca; poi basta il
  telefono. Se la placca usa tessere Mifare criptate l'NFC del telefono non è utilizzabile.

## Notifiche degli accessi

In **Impostazioni** › «Notifiche su questo dispositivo» ogni telefono può attivare un
avviso, anche col sito chiuso. In «Avvisami di» si spuntano una o più voci (vale l'unione):
**tutti gli accessi**, **i miei accessi** (le persone create da quell'utente), **accessi
falliti** (PIN o tessera non validi) e **accessi al sito** (ingressi e tentativi falliti
nella pagina e nel pannello admin, con utente e IP ma mai la password; più tentativi in 20
secondi arrivano in un'unica notifica). L'utente semplice vede solo «i miei accessi» e
«accessi falliti», sempre ristretti alle sue persone.
Il Pi legge il registro della placca ogni 20 secondi e manda una notifica Web Push cifrata
tramite il servizio del browser (Google, Apple, Mozilla, Microsoft). Le notifiche non
contengono PIN né numeri di tessera; l'utente semplice riceve solo le proprie entrate.
- Serve l'indirizzo pubblico **https** (tunnel): con l'indirizzo di casa e il certificato
  autofirmato il browser non le permette.
- **iPhone** (iOS 16.4+): prima «Aggiungi alla schermata Home», poi attivarle dall'app.
- Il Pi deve avere la password della placca (Admin › Utenti della pagina) e il registro della
  placca acceso (scheda Accessi).
- Serve `python3-cryptography` (lo installa `installa.sh`; su un Pi già installato:
  `sudo apt install python3-cryptography` e riavvio del servizio `citofono`).

## Registro degli accessi al sito

In **Admin › Accessi al sito** ci sono gli ultimi tentativi di entrare nella pagina, nel
pannello admin e nell'aggiornamento automatico: data e ora, IP (quello vero del visitatore
anche dal tunnel), dispositivo, esito. Per i tentativi **falliti** c'è anche la password
scritta, nascosta finché non premi «Mostra password». Attenzione: spesso è la password giusta
con un errore di battitura, o quella di un altro servizio. Il file
(`/etc/citofono/accessi.json`, 600) resta solo sul Pi: ultime 300 voci, massimo 90 giorni,
e si può svuotare dal pannello.

## Aggiornamenti

Il Pi resta aggiornato da questo repo, senza doverci accedere:
- dal pannello, **Aggiorna adesso** (`git pull` + riavvio);
- in automatico ogni notte (timer `systemd`).

Per aggiornare pubblichi semplicemente qui; il Pi prende le novità.

## Nota

Progetto personale per i propri dispositivi, sulla propria rete. Non è associato a
DNAKE. La porta 80 della placca non va mai esposta direttamente su Internet.
