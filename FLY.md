# Citofono su Fly.io (senza Raspberry)

L'app intera (pagina dei PIN, account, ruoli, admin) gira su **Fly.io**. In casa
non serve nessun computer: il **monitor** si collega lui a Fly con `xfrpc`, che è
già nel suo firmware.

```
telefono → Cloudflare (citofono.tuodominio) → cloudflared ┐
                                                          ├─ su Fly: server.py
monitor (xfrpc, TLS, porta 7000) ─────────────→ frps ─────┘   vede la placca su 127.0.0.1:17080
   └→ placca (porta 80, rete dei 2 fili)
```

- La **placca non è mai esposta**: su Fly la sua porta si apre solo su `127.0.0.1`
  (la usa `server.py`, da Internet non si vede) e solo mentre il monitor è collegato.
- Il collegamento monitor → Fly è **TLS** (verificato: niente in chiaro sul filo) e
  autenticato con un token. frps rifiuta chi non usa TLS.
- `citofono.tuodominio` resta su Cloudflare com'è: cambia solo dove gira cloudflared.

Perché non cloudflared sul monitor: il kernel del monitor è compilato **senza epoll**
e qualunque programma Go (cloudflared, frpc ufficiale, ngrok) si ferma all'avvio.
`xfrpc` è in C con libevent, che senza epoll usa `poll`: funziona.

## Cosa costa
Una macchina piccola sempre accesa (256 MB), un volume da 1 GB e un **IPv4
dedicato** (frp usa TCP grezzo sulla 7000: l'IPv4 condiviso di Fly vale solo per
HTTP/HTTPS). Pochi dollari al mese: controlla i prezzi attuali su fly.io/pricing.

## 1. Creare l'app (dal PC, nella cartella del repository)
Installa `flyctl` (https://fly.io/docs/flyctl/install/) e fai `fly auth login`.

In `fly.toml` cambia `app = "citofono-cambiami"` con un nome libero, poi:

```
fly apps create NOME-APP
fly volumes create citofono_dati --size 1 --region fra
fly ips allocate-v4
```

Segreti (restano su Fly, mai nel repository). Il token frp: solo lettere e cifre,
almeno 24. Per generarlo: `python -c "import secrets;print(secrets.token_hex(24))"`.

```
fly secrets set FRP_TOKEN=IL_TOKEN_GENERATO ADMIN_PASS=una-password-lunga
fly deploy
```

`ADMIN_PASS` serve solo al primo avvio: crea l'utente `admin` per entrare. Non
impostare ancora `TUNNEL_TOKEN` (vedi il passo 4).

Controllo: `fly logs` deve mostrare `frps started successfully` e
`citofono su https://127.0.0.1:443/`.

## 2. Collegare il monitor (telnet, dal Pi o da un PC in casa)
Copia `fly/xfrpc.ini.esempio`, compila `server_addr` (NOME-APP.fly.dev), `token`
(lo stesso FRP_TOKEN) e `local_ip` della placca, e mettilo sul monitor:

```
mkdir -p /dnake/data/xfrpc && cd /dnake/data/xfrpc
wget -O xfrpc.ini http://IP_DEL_PC:8000/xfrpc.ini
chmod 600 xfrpc.ini
/dnake/bin/xfrpc -f -c /dnake/data/xfrpc/xfrpc.ini      # prova in primo piano
```

Deve stampare `login success!` e `Start xfrp proxy services`. Poi Ctrl+C e
avvialo in background: `/dnake/bin/xfrpc -c /dnake/data/xfrpc/xfrpc.ini`.

Due dettagli verificati con lo xfrpc di fabbrica: la chiave del token si chiama
**`token`** (con `auth_token` il login fallisce) e **non** va messo
`use_encryption` (blocca i dati: il TLS cifra già tutto).

⚠ **Avvio automatico**: il monitor non riavvia xfrpc da solo dopo un blackout.
Finché non è risolto (sveglia dalla placca o riga nell'`init` del monitor), dopo un
riavvio del monitor va rilanciato a mano.

## 3. Portare gli account dal Pi (facoltativo)
Per tenere utenti, ruoli e assegnazioni dei PIN, copia dal Pi i file
`/etc/citofono/utenti.json`, `placca.json`, `proprieta.json` (e `admin.json`) nel
volume di Fly, in `/data/`:

```
fly ssh sftp shell
put utenti.json /data/utenti.json
put placca.json /data/placca.json
put proprieta.json /data/proprieta.json
```

Poi `fly machine restart`. **Non** copiare `citofono.conf`: su Fly gli indirizzi
sono diversi (la placca è `127.0.0.1:17080`).

## 4. Spostare il tunnel Cloudflare dal Pi a Fly
Lo stesso tunnel non deve girare in due posti (Cloudflare dividerebbe le richieste).
L'ingresso resta `https://localhost:443` con **No TLS Verify**: su Fly il server
ascolta proprio lì.

```
# sul Pi
sudo systemctl disable --now citofono-tunnel
sudo cat /etc/citofono/tunnel.env          # TUNNEL_TOKEN=eyJ...
# sul PC
fly secrets set TUNNEL_TOKEN=eyJ...
```

Apri `https://citofono.tuodominio/`: nella scheda Admin, **Collegamento al
monitor** deve dire *collegato*.

## Aggiornamenti
Come sul Pi: dopo un commit su `main`,

```
curl -X POST -H "Authorization: Bearer PASSWORD_GESTORE" https://citofono.tuodominio/aggiorna
```

oppure *Aggiorna adesso* nella scheda Admin. Su Fly fa `git pull` nel volume e
riavvia solo `server.py` (tunnel e collegamento restano su). Un `fly deploy` serve
solo se cambiano `Dockerfile` o `fly/avvio.sh`.

## Se qualcosa non va
- *Collegamento al monitor: non collegato*: xfrpc fermo o monitor senza rete.
  Sul monitor: `/dnake/bin/xfrpc -f -c /dnake/data/xfrpc/xfrpc.ini` e leggi l'errore.
- `token in login doesn't match` nei `fly logs`: token diverso, o chiave scritta
  `auth_token` invece di `token`.
- il login riesce ma la placca non risponde: c'è `use_encryption` nel file, toglilo.
