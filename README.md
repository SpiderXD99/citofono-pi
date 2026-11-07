# Citofono — pagina dei PIN e tunnel, su Raspberry Pi

Gestisci dal telefono i **PIN fissi** e i **codici ospite/temporanei** di un
videocitofono DNAKE (placca esterna), in casa e — con il tunnel — anche da fuori.
Tutto gira su un **Raspberry Pi** sulla stessa rete della placca: il Pi ospita la
pagina e il tunnel, e parla con la placca tramite la sua **web API sulla porta 80**
(autenticandosi con la normale password admin della placca). Non modifica il
firmware e non usa accessi di servizio dei dispositivi.

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

## Aggiornamenti

Il Pi resta aggiornato da questo repo, senza doverci accedere:
- dal pannello, **Aggiorna adesso** (`git pull` + riavvio);
- in automatico ogni notte (timer `systemd`).

Per aggiornare pubblichi semplicemente qui; il Pi prende le novità.

## Nota

Progetto personale per i propri dispositivi, sulla propria rete. Non è associato a
DNAKE. La porta 80 della placca non va mai esposta direttamente su Internet.
