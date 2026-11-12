#!/usr/bin/env python3
"""Citofono su Raspberry Pi: pagina dei PIN + pannello admin, senza telnet.

Fa da ponte verso la placca (web API sulla porta 80, autenticata con la sua
password admin) e da pannello di configurazione del Pi (tunnel Cloudflare,
indirizzi, credenziali). Serve la pagina su / e il pannello su /admin: tutto il resto e' 404.

I SEGRETI STANNO SOLO SUL PI, in /etc/citofono/ (file 600), MAI nel repository:
  citofono.conf   indirizzi e opzioni (PLACCA, MONITOR, porte, ADMIN_LAN_ONLY...)
  admin.json      utente + hash PBKDF2 della password del pannello admin
  tunnel.env      TUNNEL_TOKEN del tunnel Cloudflare
  cert.pem/key.pem  certificato del server (autofirmato)

Solo libreria standard di Python 3. Avvio: server.py --conf /etc/citofono/citofono.conf
"""

import hashlib
import hmac
import http.client
import http.server
import json
import os
import re
import secrets
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

PREDEFINITI = {
    'PLACCA': '',                          # IP della placca, es. 192.168.68.90
    'MONITOR': '',                         # web del monitor, es. https://192.168.68.80
    'PUBBLICO': '',                        # hostname del tunnel su Cloudflare, es. citofono.esempio.com
    'INDIRIZZO': '0.0.0.0',
    'PORTA': '443',
    'CERT': '/etc/citofono/cert.pem',
    'CHIAVE_TLS': '/etc/citofono/key.pem',
    'PAGINA': '/opt/citofono/index.html',
    'ADMIN_PAGINA': '/opt/citofono/admin.html',
    'ADMIN': '/etc/citofono/admin.json',
    'TUNNEL_ENV': '/etc/citofono/tunnel.env',
    'SERVIZIO': 'citofono-tunnel.service',
    'CLOUDFLARED': '/usr/bin/cloudflared',
    'APP_DIR': '/opt/citofono',
    'ADMIN_LAN_ONLY': '0',                 # 1 = pannello admin solo da casa (LAN)
    'CONF': '/etc/citofono/citofono.conf',
    'PONTE_CONF': '/etc/citofono/ponte.conf',
    'UTENTI': '/etc/citofono/utenti.json',
    'PLACCA_CRED': '/etc/citofono/placca.json',
    'PROPRIETA': '/etc/citofono/proprieta.json',
}
UTENTE = 'admin'                           # unico utente della placca ammesso dal proxy
VALORE = re.compile(r'^[A-Za-z0-9%._~*()!+-]*$')  # valori form (gia' url-encoded; '+' = spazio)
INOLTRATI = {'action', 'index', 'group', 'name', 'room', 'relays', 'status', 'type', 'cards',
             'pin_mode', 'pin_code', 'pass_enable', 'pass_num', 'active_enable', 'start_date',
             'end_date', 'start_time', 'end_time', 'week', 'password', 'old_password',
             'new_password',
             # api=card: lettura tessere col lettore della placca e tessera master
             'enable', 'admin_card', 'security', 'mifare_enable', 'mifare_block',
             'mifare_sector', 'mifare_key'}
CAMPI_SEMPLICI = {'op', 'sid', 'api', 'user', 'hash', 'azione', 'token',
                  'admin_user', 'admin_pass', 'nuovo_user', 'nuova_pass', 'placca', 'monitor', 'pubblico',
                  'mon_host', 'mon_port', 'mon_user', 'mon_pass', 'placca_ip', 'relay_port',
                  'ssid', 'wifi_pass', 'hs_ssid', 'hs_pass',
                  'pass', 'nuova', 'pin_user', 'pin_pass', 'placca_pass', 'ruolo'}
BLOCCATA ='la placca ha bloccato gli accessi per qualche minuto dopo una password sbagliata: riprova tra 2-3 minuti'
ITER = 210000                              # iterazioni PBKDF2 per la password admin
SESSIONI = {}                              # token admin -> scadenza (epoch)
PINSESS = {}                               # token pagina / -> {user, ruolo, scad}
DURATA = 1800                              # 30 minuti di sessione admin
TENTATIVI = {}                             # ip -> [numero fail, bloccato_fino_a]
MAX_FAIL, BLOCCO = 5, 300                  # 5 errori -> 5 minuti di blocco
CONF = dict(PREDEFINITI)
VERSIONE = []

# --- PWA: la pagina si puo' installare come app dal telefono ---
_ICONA = ("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'%3E"
          "%3Crect width='64' height='64' rx='14' fill='%231d5fd1'/%3E%3Cg fill='%23fff'%3E"
          "%3Ccircle cx='20' cy='20' r='5'/%3E%3Ccircle cx='32' cy='20' r='5'/%3E%3Ccircle cx='44' cy='20' r='5'/%3E"
          "%3Ccircle cx='20' cy='32' r='5'/%3E%3Ccircle cx='32' cy='32' r='5'/%3E%3Ccircle cx='44' cy='32' r='5'/%3E"
          "%3Ccircle cx='20' cy='44' r='5'/%3E%3Ccircle cx='32' cy='44' r='5'/%3E%3Ccircle cx='44' cy='44' r='5'/%3E"
          "%3C/g%3E%3C/svg%3E")
MANIFEST = json.dumps({
    'name': 'Citofono PIN', 'short_name': 'Citofono', 'start_url': '/', 'scope': '/',
    'display': 'standalone', 'background_color': '#0c1117', 'theme_color': '#1d5fd1',
    'icons': [{'src': '/icona-192.png', 'sizes': '192x192', 'type': 'image/png', 'purpose': 'any'},
              {'src': '/icona-512.png', 'sizes': '512x512', 'type': 'image/png', 'purpose': 'maskable'}],
}).encode('utf-8')
SW = (b"self.addEventListener('install',function(e){self.skipWaiting();});\n"
      b"self.addEventListener('activate',function(e){e.waitUntil(self.clients.claim());});\n"
      b"self.addEventListener('fetch',function(e){});\n")


class Errore(Exception):
    def __init__(self, stato, testo):
        super().__init__(testo)
        self.stato, self.testo = stato, testo


# ---------- configurazione ----------
def leggi_conf(percorso):
    CONF['CONF'] = percorso
    try:
        with open(percorso, encoding='utf-8') as f:
            for riga in f:
                riga = riga.strip()
                if riga and not riga.startswith('#') and '=' in riga:
                    k, v = riga.split('=', 1)
                    CONF[k.strip()] = v.strip().strip('"')
    except FileNotFoundError:
        pass


def salva_conf(aggiorna):
    """Riscrive citofono.conf cambiando solo le chiavi passate, conservando i commenti."""
    CONF.update(aggiorna)
    righe, viste = [], set()
    try:
        with open(CONF['CONF'], encoding='utf-8') as f:
            for riga in f:
                s = riga.strip()
                if s and not s.startswith('#') and '=' in s:
                    k = s.split('=', 1)[0].strip()
                    if k in aggiorna:
                        righe.append('%s=%s\n' % (k, aggiorna[k]))
                        viste.add(k)
                        continue
                righe.append(riga if riga.endswith('\n') else riga + '\n')
    except FileNotFoundError:
        pass
    for k, v in aggiorna.items():
        if k not in viste:
            righe.append('%s=%s\n' % (k, v))
    tmp = CONF['CONF'] + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write(''.join(righe))
    os.replace(tmp, CONF['CONF'])


# ---------- password admin (PBKDF2) ----------
def carica_admin():
    try:
        with open(CONF['ADMIN'], encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def salva_ponte(agg):
    """Scrive /etc/citofono/ponte.conf (permessi 600): credenziali del monitor e
    parametri del relay. I segreti stanno solo qui, mai nel repo."""
    conf = {}
    try:
        with open(CONF['PONTE_CONF'], encoding='utf-8') as f:
            for riga in f:
                riga = riga.strip()
                if riga and not riga.startswith('#') and '=' in riga:
                    k, v = riga.split('=', 1)
                    conf[k.strip()] = v.strip()
    except OSError:
        pass
    conf.update(agg)
    d = os.path.dirname(CONF['PONTE_CONF'])
    if d and not os.path.isdir(d):
        os.makedirs(d, 0o700, exist_ok=True)
    fd = os.open(CONF['PONTE_CONF'], os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        for k, v in conf.items():
            f.write('%s=%s\n' % (k, v))


def salva_admin(utente, password):
    salt = secrets.token_bytes(16)
    h = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt, ITER)
    dati = {'user': utente, 'iter': ITER, 'salt': salt.hex(), 'hash': h.hex()}
    fd = os.open(CONF['ADMIN'], os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        json.dump(dati, f)


def verifica_admin(utente, password):
    a = carica_admin()
    if not a:
        return False
    h = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), bytes.fromhex(a['salt']), a.get('iter', ITER))
    return hmac.compare_digest(utente, a['user']) and hmac.compare_digest(h.hex(), a['hash'])


# ---------- utenti della pagina PIN (account separati da placca/monitor) ----------
def carica_utenti():
    try:
        with open(CONF['UTENTI'], encoding='utf-8') as f:
            d = json.load(f)
        return d if isinstance(d, list) else d.get('utenti', [])
    except (OSError, ValueError):
        return []


def salva_utenti(lista):
    fd = os.open(CONF['UTENTI'], os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        json.dump(lista, f)


def aggiungi_utente(utente, password, ruolo='user'):
    salt = secrets.token_bytes(16)
    h = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt, ITER)
    voce = {'user': utente, 'ruolo': ruolo if ruolo in ('admin', 'user') else 'user',
            'iter': ITER, 'salt': salt.hex(), 'hash': h.hex()}
    lista = [u for u in carica_utenti() if u.get('user') != utente]
    lista.append(voce)
    salva_utenti(lista)


def ruolo_utente(utente):
    for u in carica_utenti():
        if u.get('user') == utente:
            return u.get('ruolo', 'user')
    return ''


def esiste_admin():
    return any(u.get('ruolo', 'user') == 'admin' for u in carica_utenti())


def elimina_utente(utente):
    salva_utenti([u for u in carica_utenti() if u.get('user') != utente])


def verifica_utente(utente, password):
    for u in carica_utenti():
        if hmac.compare_digest(utente, u.get('user', '')):
            h = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'),
                                    bytes.fromhex(u['salt']), u.get('iter', ITER))
            return hmac.compare_digest(h.hex(), u['hash'])
    return False


def cambia_password_utente(utente, nuova):
    """Reimposta la password di un account esistente, conservandone il ruolo."""
    lista = carica_utenti()
    for u in lista:
        if u.get('user') == utente:
            salt = secrets.token_bytes(16)
            u['iter'] = ITER
            u['salt'] = salt.hex()
            u['hash'] = hashlib.pbkdf2_hmac('sha256', nuova.encode('utf-8'), salt, ITER).hex()
            salva_utenti(lista)
            return True
    return False


def imposta_ruolo(utente, ruolo):
    """Cambia il ruolo di un account. Ritorna 'ok', 'inesistente' o 'ultimo_admin'
    (non si puo' lasciare la pagina senza nessun gestore)."""
    ruolo = 'admin' if ruolo == 'admin' else 'user'
    lista = carica_utenti()
    voce = next((u for u in lista if u.get('user') == utente), None)
    if not voce:
        return 'inesistente'
    if voce.get('ruolo', 'user') == 'admin' and ruolo == 'user':
        if not any(u.get('ruolo', 'user') == 'admin' and u.get('user') != utente for u in lista):
            return 'ultimo_admin'
    voce['ruolo'] = ruolo
    salva_utenti(lista)
    return 'ok'


def salva_placca_hash(md5):
    fd = os.open(CONF['PLACCA_CRED'], os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        json.dump({'hash': md5}, f)


def leggi_placca_hash():
    try:
        with open(CONF['PLACCA_CRED'], encoding='utf-8') as f:
            return json.load(f).get('hash', '')
    except (OSError, ValueError):
        return ''


# sessione della pagina / (porta il ruolo: il server filtra e autorizza di conseguenza)
def nuova_pinsess(utente, ruolo):
    tok = secrets.token_urlsafe(24)
    PINSESS[tok] = {'user': utente, 'ruolo': ruolo, 'scad': time.time() + DURATA}
    for t, se in list(PINSESS.items()):
        if se['scad'] < time.time():
            PINSESS.pop(t, None)
    return tok


def pinsess(cookie):
    se = PINSESS.get(cookie or '')
    if se and se['scad'] > time.time():
        se['scad'] = time.time() + DURATA
        return se
    return None


# proprieta' dei codici persona: {"<indice riga users.xml>": "<utente>"}.
# L'indice e' stabile sotto le azioni dell'utente semplice (non aggiunge/toglie righe);
# se il gestore aggiunge o elimina persone, riassegna dalla pagina.
def leggi_proprieta():
    try:
        with open(CONF['PROPRIETA'], encoding='utf-8') as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def salva_proprieta(d):
    fd = os.open(CONF['PROPRIETA'], os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        json.dump(d, f)


def proprietario(indice):
    return leggi_proprieta().get(str(indice), '')


# colonne di users.xml, stesso ordine della pagina
COLONNE = ['nome', 'gruppo', 'stanza', 'tipo', 'stato', 'rele', 'tessere', 'pin_mode', 'pin',
           'pass_on', 'usi', 'valid_on', 'dal', 'al', 'dalle', 'alle', 'settimana']


def corpo_modifica_pin(sid, idx, nuovo_pin):
    """Ruolo 'user': ricostruisce la modifica dai valori ATTUALI della persona,
    cambiando SOLO il codice PIN (non utilizzi ne' validita')."""
    if not re.fullmatch(r'[0-9]{4,8}', nuovo_pin):
        raise Errore(400, 'il PIN deve avere da 4 a 8 cifre')
    try:
        testo = alla_placca('/data/users.xml', None, sid).decode('utf-8', 'replace')
        righe = [r for r in testo.splitlines() if r.strip()]
        riga = righe[int(idx)]
    except (IndexError, ValueError):
        raise Errore(409, "l'elenco e' cambiato: aggiorna e riprova")
    c = (riga.split(',') + [''] * len(COLONNE))[:len(COLONNE)]
    v = dict(zip(COLONNE, (x.strip() for x in c)))
    campi = [
        ('action', '2'), ('index', str(idx)), ('group', v['gruppo']), ('name', v['nome']),
        ('room', v['stanza'] if v['stanza'] != '-1' else ''), ('relays', v['rele']),
        ('status', v['stato']), ('type', v['tipo'] or '0'),
        ('cards', '' if v['tessere'] in ('None', '') else v['tessere']),
        ('pin_mode', '1' if nuovo_pin else '0'), ('pin_code', nuovo_pin),
        ('pass_enable', '1' if v['pass_on'] == '1' else '0'),
        ('active_enable', '1' if v['valid_on'] == '1' else '0'),
    ]
    if v['pass_on'] == '1':
        campi.append(('pass_num', v['usi']))
    if v['valid_on'] == '1':
        campi += [('start_date', v['dal']), ('end_date', v['al']),
                  ('start_time', v['dalle']), ('end_time', v['alle']), ('week', v['settimana'])]
    return '&'.join('%s=%s' % (k, urllib.parse.quote_plus(val)) for k, val in campi)


# --- registro sblocchi (api=unlock_logs): filtro per-utente ---
def identificatori_utente(utente, sid):
    """Nome, PIN e tessere delle persone di proprieta' dell'utente: servono a
    riconoscere nel registro quali entrate sono sue."""
    miei = [int(k) for k, v in leggi_proprieta().items() if v == utente and k.isdigit()]
    ids = set()
    try:
        righe = [r for r in alla_placca('/data/users.xml', None, sid).decode('utf-8', 'replace').splitlines() if r.strip()]
    except (OSError, Errore):
        return ids
    for i in miei:
        if 0 <= i < len(righe):
            v = dict(zip(COLONNE, (x.strip() for x in (righe[i].split(',') + [''] * len(COLONNE)))))
            for campo in (v.get('nome', ''), v.get('pin', '')):
                if campo and campo != 'None':
                    ids.add(campo.lower())
            for carta in (v.get('tessere', '') or '').replace(';', ',').split(','):
                carta = carta.strip()
                if carta and carta != 'None':
                    ids.add(carta.lower())
    return ids


def _record_log(d):
    """Dalla risposta di unlock_logs ricava la lista dei record, qualunque forma abbia."""
    if isinstance(d, list):
        return d
    if not isinstance(d, dict):
        return []
    for k in ('list', 'logs', 'records', 'log', 'items', 'rows', 'data'):
        if isinstance(d.get(k), list):
            return d[k]
    idx = {}                                     # forma indicizzata: prefisso_N_campo
    for k, val in d.items():
        m = re.match(r'[A-Za-z]+_?(\d+)_(.+)', k)
        if m:
            idx.setdefault(int(m.group(1)), {})[m.group(2)] = val
    return [idx[i] for i in sorted(idx)]


def filtra_log(grezzo, ids):
    """Tiene solo i record che citano un identificatore dell'utente. Forma ignota o
    nessun identificatore -> lista vuota (a un utente non si mostra mai tutto)."""
    try:
        j = json.loads(grezzo.decode('utf-8', 'replace'))
    except ValueError:
        return b'{"result":"0","data":{"list":[]}}'
    base = j.get('data', j) if isinstance(j, dict) else j
    tenuti = []
    for r in _record_log(base):
        if isinstance(r, dict) and {str(v).strip().lower() for v in r.values()} & ids:
            tenuti.append(r)
    return json.dumps({'result': '0', 'data': {'list': tenuti}}).encode('utf-8')


# ---------- rete verso placca / monitor ----------
# La web della placca e' un mini_httpd minuscolo (anche dietro il relay sul
# monitor nei kit a 2 fili): regge male le connessioni simultanee. Quindi le
# richieste alla placca si fanno UNA ALLA VOLTA (_PLACCA_LOCK) e si ritenta se la
# connessione viene rifiutata, invece di restituire subito 502.
_PLACCA_LOCK = threading.Lock()


def richiesta(url, dati=None, sid='', timeout=15):
    # http.client con close() esplicito: la web della placca (mini_httpd via relay)
    # gestisce una connessione per volta, e una keep-alive lasciata aperta da urllib
    # bloccava la richiesta successiva (RemoteDisconnected). Qui ogni connessione si
    # chiude subito dopo la risposta.
    u = urllib.parse.urlsplit(url)
    percorso = u.path + (('?' + u.query) if u.query else '')
    intestazioni = {'Connection': 'close'}
    if sid:
        intestazioni['Cookie'] = 'SessionID=' + sid
    corpo, metodo = None, 'GET'
    if dati:                                   # corpo vuoto o None = lettura (GET); con campi = POST
        intestazioni['Content-Type'] = 'application/x-www-form-urlencoded'
        corpo = dati.encode('ascii')
        metodo = 'POST'
    for tentativo in range(3):
        conn = None
        try:
            if u.scheme == 'https':                # monitor (certificato autofirmato)
                conn = http.client.HTTPSConnection(u.hostname, u.port or 443, timeout=timeout,
                                                   context=ssl._create_unverified_context())
            else:                                  # placca (http, dietro il relay)
                conn = http.client.HTTPConnection(u.hostname, u.port or 80, timeout=timeout)
            conn.request(metodo, percorso, body=corpo, headers=intestazioni)
            r = conn.getresponse()
            return r.status, r.read()
        except (http.client.HTTPException, OSError):
            if tentativo < 2:
                time.sleep(0.3)
                continue
            return 0, b''
        finally:
            if conn is not None:
                try:
                    conn.close()
                except OSError:
                    pass
    return 0, b''


def alla_placca(percorso, dati=None, sid=''):
    with _PLACCA_LOCK:                      # una richiesta alla placca per volta
        stato, corpo = richiesta('http://' + CONF['PLACCA'] + percorso, dati, sid)
    if stato == 200:
        return corpo
    if stato == 401:
        raise Errore(401, 'accesso scaduto o password errata')
    if stato == 404:
        raise Errore(404, 'non trovato')
    if stato == 503:
        raise Errore(503, BLOCCATA)
    raise Errore(502, 'la placca %s non risponde' % CONF['PLACCA'])


# ---------- tunnel (servizio systemd) ----------
def systemctl(*argomenti):
    cmd = ['systemctl'] + list(argomenti)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        return r.returncode, r.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return 1, ''


def leggi_token():
    try:
        with open(CONF['TUNNEL_ENV'], encoding='utf-8') as f:
            for riga in f:
                if riga.startswith('TUNNEL_TOKEN='):
                    return riga.split('=', 1)[1].strip()
    except OSError:
        pass
    return ''


def versione_cloudflared():
    if not VERSIONE:
        try:
            out = subprocess.run([CONF['CLOUDFLARED'], '--version'], capture_output=True,
                                 text=True, timeout=15).stdout.split()
            VERSIONE.append(out[2] if len(out) > 2 else '')
        except (OSError, subprocess.SubprocessError, IndexError):
            VERSIONE.append('')
    return VERSIONE[0]


def meminfo(chiave):
    try:
        with open('/proc/meminfo') as f:
            return next(r.split()[1] for r in f if r.startswith(chiave))
    except (OSError, StopIteration, IndexError):
        return ''


def stato_tunnel():
    s = CONF['SERVIZIO']
    installato = os.access(CONF['CLOUDFLARED'], os.X_OK)
    attivo = systemctl('is-active', s)[1] == 'active'
    abilitato = systemctl('is-enabled', s)[1] == 'enabled'
    ram = ''
    if attivo:
        pid = systemctl('show', '-p', 'MainPID', '--value', s)[1]
        try:
            with open('/proc/%s/status' % pid) as f:
                ram = next(r.split()[1] for r in f if r.startswith('VmRSS'))
        except (OSError, StopIteration, IndexError):
            pass
    return {'dove': 'pi', 'installato': int(installato), 'token': int(bool(leggi_token())),
            'attivo': int(attivo), 'automatico': int(abilitato), 'ram': ram,
            'libera': meminfo('MemAvailable'), 'versione': versione_cloudflared() if installato else ''}


def comanda_tunnel(azione, token):
    s = CONF['SERVIZIO']
    if azione == 'stato':
        pass
    elif azione in ('avvia', 'ferma'):
        rc, _ = systemctl('enable' if azione == 'avvia' else 'disable', '--now', s)
        if rc:
            raise Errore(500, 'systemctl ha rifiutato il comando (serve che il server giri come root)')
    elif azione == 'token':
        tok = urllib.parse.unquote(token)
        if not re.fullmatch(r'[A-Za-z0-9+/=_-]+', tok or ''):
            raise Errore(400, 'token non valido')
        if len(tok) < 100:
            raise Errore(400, 'token troppo corto: copia tutta la stringa che inizia con eyJ')
        try:
            d = os.path.dirname(CONF['TUNNEL_ENV'])
            if d and not os.path.isdir(d):
                os.makedirs(d, 0o700, exist_ok=True)
            fd = os.open(CONF['TUNNEL_ENV'], os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                f.write('TUNNEL_TOKEN=%s\n' % tok)
        except OSError as e:
            raise Errore(500, 'non riesco a scrivere %s: %s. Il server deve girare come root '
                              '(servizio citofono.service).' % (CONF['TUNNEL_ENV'], e.strerror or e))
        if systemctl('is-active', s)[1] == 'active':
            systemctl('restart', s)
    else:
        raise Errore(400, 'azione sconosciuta')
    return stato_tunnel()


# ---------- password del monitor (web del monitor) ----------
def password_monitor(vecchia, nuova):
    if not CONF['MONITOR']:
        raise Errore(500, 'manca MONITOR nella configurazione')
    base = CONF['MONITOR'].rstrip('/') + '/cgi-bin/webapi.cgi'
    h = hashlib.md5(urllib.parse.unquote_plus(vecchia).encode('utf-8')).hexdigest()
    stato, corpo = richiesta(base + '?api=login&username=admin&password=' + h)
    if stato == 503:
        raise Errore(503, 'il monitor ha bloccato gli accessi per qualche minuto dopo una password sbagliata')
    if stato == 0:
        raise Errore(502, 'il monitor %s non risponde' % CONF['MONITOR'])
    try:
        sid = str(json.loads(corpo)['data']['SessionID'])
    except (ValueError, KeyError, TypeError):
        sid = ''
    if stato != 200 or not sid:
        return b'{"result":"-1","text":"password attuale non corretta"}'
    stato, corpo = richiesta(base + '?api=password',
                             'user=web&old_password=%s&new_password=%s' % (vecchia, nuova), sid)
    if stato != 200:
        raise Errore(502, 'il monitor ha risposto %d' % stato)
    return corpo


# ---------- aggiornamento dal repository ----------
def aggiorna_dal_repo():
    d = CONF['APP_DIR']
    try:
        subprocess.run(['git', 'config', '--global', '--add', 'safe.directory', d],
                       capture_output=True, text=True, timeout=15)
        r = subprocess.run(['git', '-C', d, 'pull', '--ff-only'],
                           capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError) as e:
        raise Errore(500, 'git pull non riuscito: %s' % e)
    out = (r.stdout + r.stderr).strip()
    if r.returncode != 0:
        raise Errore(500, 'git pull non riuscito:\n' + out)
    # riavvio differito: rispondo prima, poi systemd riavvia il server
    try:
        subprocess.Popen(['sh', '-c', 'sleep 1; systemctl restart citofono.service'],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass
    return out


# ---------- sessioni admin ----------
def nuova_sessione():
    tok = secrets.token_urlsafe(32)
    SESSIONI[tok] = time.time() + DURATA
    for t, scad in list(SESSIONI.items()):
        if scad < time.time():
            SESSIONI.pop(t, None)
    return tok


def sessione_ok(cookie):
    scad = SESSIONI.get(cookie or '')
    if scad and scad > time.time():
        SESSIONI[cookie] = time.time() + DURATA
        return True
    return False


def bloccato(ip):
    v = TENTATIVI.get(ip)
    return bool(v and v[1] > time.time())


def segna_fallito(ip):
    v = TENTATIVI.get(ip, [0, 0])
    v[0] += 1
    if v[0] >= MAX_FAIL:
        v[1] = time.time() + BLOCCO
        v[0] = 0
    TENTATIVI[ip] = v


# ---------- smistamento richieste ----------
def gestisci(corpo, dal_tunnel, cookie, cookie_pin, ip):
    campi, avanti = {}, []
    for campo in corpo.split('&') if corpo else []:
        k, _, v = campo.partition('=')
        if not VALORE.match(v):
            raise Errore(400, 'valore non valido: ' + k)
        if k in CAMPI_SEMPLICI:
            campi[k] = v
        elif k == 'chi':
            if v not in ('web', 'system'):
                raise Errore(400, 'password sconosciuta')
            avanti.append('user=' + v)
        elif k in INOLTRATI:
            campi[k] = v
            avanti.append(k + '=' + v)
        else:
            raise Errore(400, 'campo non ammesso: ' + k)
    op, sid = campi.get('op', ''), campi.get('sid', '')
    if not re.fullmatch(r'[0-9A-Za-z]*', sid):
        raise Errore(400, 'sessione non valida')
    js, testo = 'application/json; charset=utf-8', 'text/plain; charset=utf-8'
    extra = None
    # sessione della pagina / (porta il ruolo); il gestore vale anche come sessione admin
    sess = pinsess(cookie_pin)
    ruolo = sess['ruolo'] if sess else None
    gestore = ruolo == 'admin'
    admin_pagina = gestore or sessione_ok(cookie)

    # --- admin: login e operazioni sul Pi ---
    if op == 'admin_login':
        if CONF['ADMIN_LAN_ONLY'] == '1' and dal_tunnel:
            raise Errore(403, 'il pannello admin si apre solo da casa')
        if bloccato(ip):
            raise Errore(429, 'troppi tentativi: riprova tra qualche minuto')
        u = urllib.parse.unquote_plus(campi.get('admin_user', ''))
        p = urllib.parse.unquote_plus(campi.get('admin_pass', ''))
        # /admin si apre con un account di ruolo admin; finche' non ne esiste nessuno
        # vale ancora la vecchia password del pannello (bootstrap, per non restare chiusi fuori)
        if esiste_admin():
            valido = verifica_utente(u, p) and ruolo_utente(u) == 'admin'
        else:
            valido = verifica_admin(u, p)
        if not valido:
            segna_fallito(ip)
            raise Errore(401, 'utente o password non corretti')
        TENTATIVI.pop(ip, None)
        tok = nuova_sessione()
        c = 'AdminSID=%s; HttpOnly; SameSite=Strict; Path=/; Max-Age=%d' % (tok, DURATA)
        if CONF['CERT']:
            c += '; Secure'
        return js, b'{"ok":1}', {'Set-Cookie': c}

    if op.startswith('admin_') and op != 'admin_login':
        if CONF['ADMIN_LAN_ONLY'] == '1' and dal_tunnel:
            raise Errore(403, 'il pannello admin si apre solo da casa')
        if not admin_pagina:                     # sessione del pannello OPPURE gestore della pagina /
            raise Errore(401, 'accesso admin richiesto')
    elif op in ('tunnel', 'monitor'):
        # il pannello/gestore, oppure la pagina PIN con una sessione valida della placca
        if not admin_pagina:
            if not sid:
                raise Errore(401, 'accesso richiesto')
            alla_placca('/cgi-bin/webapi.cgi?api=unlock_passwd', None, sid)

    if op == 'admin_logout':
        SESSIONI.pop(cookie, None)
        return js, b'{"ok":1}', {'Set-Cookie': 'AdminSID=; Max-Age=0; Path=/'}
    if op == 'admin_state':
        dati = {'placca': CONF['PLACCA'], 'monitor': CONF['MONITOR'], 'pubblico': CONF['PUBBLICO'],
                'lan_only': CONF['ADMIN_LAN_ONLY'], 'tunnel': stato_tunnel()}
        try:
            import ponte
            pc = ponte.leggi_conf(CONF['PONTE_CONF'])
            dati['ponte'] = {
                'mon_host': pc.get('MON_HOST', ''), 'mon_port': pc.get('MON_PORT', '9900'),
                'mon_user': pc.get('MON_USER', 'root'), 'placca_ip': pc.get('PLACCA_IP', ''),
                'relay_port': pc.get('RELAY_PORT', '8070'),
                'pass_impostata': bool(pc.get('MON_PASS', '')),
                'attivo': bool(pc.get('MON_HOST')) and ponte._porta_viva(
                    pc.get('MON_HOST'), pc.get('RELAY_PORT', '8070'), timeout=2)}
        except Exception:
            dati['ponte'] = None
        return js, json.dumps(dati).encode('utf-8'), None
    if op == 'admin_save':
        agg = {}
        pl = urllib.parse.unquote(campi.get('placca', ''))
        mo = urllib.parse.unquote(campi.get('monitor', ''))
        if pl:
            if not re.fullmatch(r'[0-9.]+(:[0-9]+)?', pl):
                raise Errore(400, 'IP placca non valido')
            agg['PLACCA'] = pl
        if 'monitor' in campi:
            if mo and not re.fullmatch(r'https?://[0-9A-Za-z.:-]+', mo):
                raise Errore(400, 'indirizzo monitor non valido')
            agg['MONITOR'] = mo
        if 'pubblico' in campi:
            pu = urllib.parse.unquote(campi.get('pubblico', ''))
            if pu and (not re.fullmatch(r'[0-9A-Za-z.-]{1,253}', pu) or '.' not in pu):
                raise Errore(400, 'hostname pubblico non valido')
            agg['PUBBLICO'] = pu
        if agg:
            salva_conf(agg)
        return js, b'{"ok":1}', None
    if op == 'admin_cred':
        u = urllib.parse.unquote_plus(campi.get('nuovo_user', '')) or UTENTE
        p = urllib.parse.unquote_plus(campi.get('nuova_pass', ''))
        if not re.fullmatch(r'[0-9A-Za-z]{1,32}', u):
            raise Errore(400, 'utente: lettere e cifre, max 32')
        if len(p) < 12:
            raise Errore(400, 'la password admin deve avere almeno 12 caratteri')
        salva_admin(u, p)
        return js, b'{"ok":1}', None
    if op == 'admin_relay_save':
        agg = {}
        regole = {
            'mon_host': (r'[0-9A-Za-z.-]{1,253}', 'IP/host del monitor non valido'),
            'mon_port': (r'[0-9]{1,5}', 'porta telnet non valida'),
            'mon_user': (r'[0-9A-Za-z_.-]{1,32}', 'utente non valido'),
            'placca_ip': (r'[0-9.]{1,15}', 'IP della placca non valido'),
            'relay_port': (r'[0-9]{1,5}', 'porta del relay non valida'),
        }
        for campo, (rx, msg) in regole.items():
            if campo in campi:
                v = urllib.parse.unquote_plus(campi.get(campo, ''))
                if v and not re.fullmatch(rx, v):
                    raise Errore(400, msg)
                if v:
                    agg[campo.upper()] = v
        if 'mon_pass' in campi:
            pw = urllib.parse.unquote_plus(campi.get('mon_pass', ''))
            if pw:                              # vuoto = non cambiare la password salvata
                agg['MON_PASS'] = pw
        if agg:
            salva_ponte(agg)
        return js, b'{"ok":1}', None
    if op == 'admin_relay':
        import ponte
        try:
            esito = ponte.assicura_relay(ponte.leggi_conf(CONF['PONTE_CONF']))
        except (ponte.ErrorePonte, OSError) as e:
            raise Errore(502, str(e))
        return js, json.dumps({'ok': 1, 'esito': esito}).encode('utf-8'), None
    if op in ('admin_rete_state', 'admin_wifi_scan', 'admin_wifi_connect', 'admin_hotspot_save'):
        import rete
        try:
            if op == 'admin_rete_state':
                return js, json.dumps(rete.stato()).encode('utf-8'), None
            if op == 'admin_wifi_scan':
                return js, json.dumps(rete.scansiona()).encode('utf-8'), None
            if op == 'admin_wifi_connect':
                ssid = urllib.parse.unquote_plus(campi.get('ssid', ''))
                pw = urllib.parse.unquote_plus(campi.get('wifi_pass', ''))
                if not ssid:
                    raise Errore(400, 'scegli una rete')
                prec = rete.connetti(ssid, pw)
                return js, json.dumps({'ok': 1, 'prec': prec}).encode('utf-8'), None
            # admin_hotspot_save
            ssid = urllib.parse.unquote_plus(campi.get('hs_ssid', ''))
            pw = urllib.parse.unquote_plus(campi.get('hs_pass', ''))
            if not ssid:
                raise Errore(400, 'scegli un nome per l’hotspot')
            rete.config_hotspot(ssid, pw or None)
            return js, b'{"ok":1}', None
        except rete.ErroreRete as e:
            raise Errore(502, str(e))
    if op in ('admin_utenti', 'admin_utente_add', 'admin_utente_del', 'admin_utente_ruolo', 'admin_placca_pass'):
        if op == 'admin_utenti':
            lista = [{'user': u.get('user', ''), 'ruolo': u.get('ruolo', 'user')} for u in carica_utenti()]
            return js, json.dumps({'utenti': lista,
                                   'placca_impostata': bool(leggi_placca_hash())}).encode('utf-8'), None
        if op == 'admin_placca_pass':
            pw = urllib.parse.unquote_plus(campi.get('placca_pass', ''))
            if not pw:
                raise Errore(400, 'scrivi la password della placca')
            salva_placca_hash(hashlib.md5(pw.encode('utf-8')).hexdigest())
            return js, b'{"ok":1}', None
        if op == 'admin_utente_add':
            u = urllib.parse.unquote_plus(campi.get('pin_user', ''))
            pw = urllib.parse.unquote_plus(campi.get('pin_pass', ''))
            if not re.fullmatch(r'[0-9A-Za-z._-]{1,32}', u):
                raise Errore(400, 'utente: lettere, cifre, . _ - (max 32)')
            if len(pw) < 6:
                raise Errore(400, 'la password deve avere almeno 6 caratteri')
            ruolo = campi.get('ruolo', 'user')
            aggiungi_utente(u, pw, 'admin' if ruolo == 'admin' else 'user')
            return js, b'{"ok":1}', None
        if op == 'admin_utente_ruolo':
            u = urllib.parse.unquote_plus(campi.get('pin_user', ''))
            esito = imposta_ruolo(u, campi.get('ruolo', 'user'))
            if esito == 'inesistente':
                raise Errore(404, 'account inesistente')
            if esito == 'ultimo_admin':
                raise Errore(400, 'deve restare almeno un gestore')
            return js, b'{"ok":1}', None
        elimina_utente(urllib.parse.unquote_plus(campi.get('pin_user', '')))
        return js, b'{"ok":1}', None
    if op == 'admin_update':
        return testo, aggiorna_dal_repo().encode('utf-8') or b'aggiornato\n', None
    if op == 'tunnel':
        s = comanda_tunnel(campi.get('azione', ''), campi.get('token', ''))
        return testo, ('\n'.join('%s=%s' % kv for kv in s.items()) + '\n').encode('utf-8'), None
    if op == 'monitor':
        if not campi.get('old_password') or not campi.get('new_password'):
            raise Errore(400, 'password mancanti')
        return js, password_monitor(campi['old_password'], campi['new_password']), None

    # --- pagina PIN (ruolo e admin_pagina sono gia' stati calcolati in cima) ---
    if op == 'chisono':                          # chi sono io per la pagina /
        if sess:
            return js, json.dumps({'user': sess['user'], 'ruolo': sess['ruolo']}).encode('utf-8'), None
        return js, json.dumps({'user': '', 'ruolo': 'admin'}).encode('utf-8'), None
    if op == 'cambia_password':                   # ognuno cambia la password del PROPRIO account
        if not sess:
            raise Errore(401, 'accesso richiesto')
        if sess['user'] not in [u.get('user') for u in carica_utenti()]:
            raise Errore(400, 'account non registrato: crealo nella scheda Admin')
        vecchia = urllib.parse.unquote_plus(campi.get('pass', ''))
        nuova = urllib.parse.unquote_plus(campi.get('nuova', ''))
        if not verifica_utente(sess['user'], vecchia):
            raise Errore(403, 'password attuale errata')
        if len(nuova) < 6:
            raise Errore(400, 'la nuova password deve avere almeno 6 caratteri')
        cambia_password_utente(sess['user'], nuova)
        return js, b'{"ok":1}', None
    if op == 'proprieta':                        # mappa proprieta' + utenti assegnabili (solo gestore)
        if not admin_pagina:
            raise Errore(403, 'riservato al gestore')
        asseg = [u['user'] for u in carica_utenti() if u.get('ruolo', 'user') == 'user']
        return js, json.dumps({'mappa': leggi_proprieta(), 'utenti': asseg}).encode('utf-8'), None
    if op == 'assegna':                          # assegna/togli il proprietario di un codice (solo gestore)
        if not admin_pagina:
            raise Errore(403, 'riservato al gestore')
        idx = campi.get('index', '')
        if not re.fullmatch(r'[0-9]{1,4}', idx):
            raise Errore(400, 'indice non valido')
        u = urllib.parse.unquote_plus(campi.get('pin_user', ''))
        d = leggi_proprieta()
        if u:
            if ruolo_utente(u) != 'user':
                raise Errore(400, 'scegli un utente semplice esistente')
            d[idx] = u
        else:
            d.pop(idx, None)
        salva_proprieta(d)
        return js, b'{"ok":1}', None

    if op == 'login':
        h = campi.get('hash', '')
        pin_user = pin_ruolo = None
        if h and re.fullmatch(r'[0-9a-f]{32}', h):
            ph = h                                   # compat: vecchio flusso (md5 della placca)
        else:
            u = urllib.parse.unquote_plus(campi.get('user', ''))
            pw = urllib.parse.unquote_plus(campi.get('pass', ''))
            if not pw:
                raise Errore(401, 'utente o password non validi')
            acc = verifica_utente(u, pw)
            boot = (not esiste_admin()) and verifica_admin(u, pw)   # primo gestore: password iniziale del pannello
            if acc or boot:
                if bloccato(ip):
                    raise Errore(429, 'troppi tentativi: riprova tra qualche minuto')
                TENTATIVI.pop(ip, None)
                pin_user = u
                pin_ruolo = 'admin' if boot else (ruolo_utente(u) or 'user')
                ph = leggi_placca_hash()
                if pin_ruolo != 'admin' and not ph:
                    raise Errore(502, 'password della placca non impostata nel pannello admin')
            elif not carica_utenti():                # nessun account: pw = password della placca
                ph = hashlib.md5(pw.encode('utf-8')).hexdigest()
            else:
                segna_fallito(ip)
                raise Errore(401, 'utente o password non validi')
        extra = None
        if pin_user:                                 # sessione della pagina / col ruolo
            tok = nuova_pinsess(pin_user, pin_ruolo)
            c = 'PinSID=%s; HttpOnly; SameSite=Strict; Path=/; Max-Age=%d' % (tok, DURATA)
            if CONF['CERT']:
                c += '; Secure'
            extra = {'Set-Cookie': c}
        if ph and re.fullmatch(r'[0-9.:]+', CONF['PLACCA']):
            return js, alla_placca('/cgi-bin/webapi.cgi?api=login&username=%s&password=%s' % (UTENTE, ph)), extra
        if pin_user:                                 # bootstrap: entra lo stesso (serve solo la scheda Admin)
            return js, b'{"data":{"SessionID":""},"bootstrap":1}', extra
        raise Errore(502, 'IP della placca non impostato: aprilo nel pannello admin')

    # --- pagina PIN: proxy verso la placca (serve la sessione della placca) ---
    if not re.fullmatch(r'[0-9.:]+', CONF['PLACCA']):
        raise Errore(502, 'IP della placca non impostato: aprilo nel pannello admin')
    if op == 'api':
        api = campi.get('api', '')
        if api not in ('user', 'unlock_passwd', 'password', 'card', 'unlock_logs'):
            raise Errore(403, 'operazione non permessa')
        if not sid:
            raise Errore(401, 'accesso richiesto')
        corpo_api = '&'.join(avanti)
        if ruolo == 'user':                          # utente semplice
            if api == 'unlock_logs':                 # legge solo le PROPRIE entrate (filtro lato server)
                if avanti:                           # niente scritture (enable/action)
                    raise Errore(403, 'solo lettura')
                grezzo = alla_placca('/cgi-bin/webapi.cgi?api=unlock_logs', None, sid)
                return js, filtra_log(grezzo, identificatori_utente(sess['user'], sid)), None
            if api != 'user':
                raise Errore(403, 'operazione riservata al gestore')
            az = campi.get('action', '')
            if az == '2':                            # modifica: deve essere sua, e cambia solo il PIN
                idx = campi.get('index', '')
                if proprietario(idx) != sess['user']:
                    raise Errore(403, 'questo codice non e\' tuo')
                corpo_api = corpo_modifica_pin(sid, idx, urllib.parse.unquote_plus(campi.get('pin_code', '')))
            elif az not in ('', '3'):                # 1 = aggiungi, 0 = elimina: vietati
                raise Errore(403, 'solo il gestore puo\' aggiungere o togliere codici')
        return js, alla_placca('/cgi-bin/webapi.cgi?api=' + api, corpo_api, sid), None
    if op == 'elenco':
        if not sid:
            raise Errore(401, 'accesso richiesto')
        dati = alla_placca('/data/users.xml', None, sid)
        if ruolo == 'user':                          # mostra solo le righe di proprieta' dell'utente,
            mio = leggi_proprieta()                  # svuotando le altre (gli indici restano coerenti)
            righe = dati.decode('utf-8', 'replace').split('\n')
            dati = '\n'.join(r if mio.get(str(i)) == sess['user'] else ''
                             for i, r in enumerate(righe)).encode('utf-8')
        return testo, dati, None
    raise Errore(400, 'operazione sconosciuta')


class Gestore(http.server.BaseHTTPRequestHandler):
    server_version = 'citofono'
    sys_version = ''
    protocol_version = 'HTTP/1.1'

    def _cookie(self):
        admin = pin = ''
        for pezzo in (self.headers.get('Cookie') or '').split(';'):
            k, _, v = pezzo.strip().partition('=')
            if k == 'AdminSID':
                admin = v
            elif k == 'PinSID':
                pin = v
        return admin, pin

    def manda(self, stato, tipo, corpo, extra=None):
        self.send_response(stato)
        self.send_header('Content-Type', tipo)
        self.send_header('Content-Length', str(len(corpo)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('Referrer-Policy', 'no-referrer')
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(corpo)

    def _pagina(self, percorso):
        try:
            with open(percorso, 'rb') as f:
                return self.manda(200, 'text/html; charset=utf-8', f.read())
        except OSError:
            return self.manda(500, 'text/plain; charset=utf-8', b'pagina mancante\n')

    def do_GET(self):
        p = self.path.split('?', 1)[0]
        # pagina dei PIN sulla radice (/); i vecchi percorsi /data/pin restano come alias
        if p in ('/data', '/data/', '/data/pin'):
            return self.manda(302, 'text/plain; charset=utf-8', b'', {'Location': '/'})
        if p in ('/', '/index.html', '/data/pin/', '/data/pin/index.html'):
            return self._pagina(CONF['PAGINA'])
        # il vecchio pannello separato non esiste piu': ora e' la scheda "Admin" dentro /
        if p in ('/admin', '/admin/', '/admin.html', '/data/pin/admin', '/data/pin/admin.html',
                 '/pannello', '/pannello.html'):
            return self.manda(302, 'text/plain; charset=utf-8', b'', {'Location': '/'})
        if p == '/manifest.webmanifest':
            return self.manda(200, 'application/manifest+json; charset=utf-8', MANIFEST)
        if p == '/sw.js':
            return self.manda(200, 'text/javascript; charset=utf-8', SW)
        if p in ('/icona-180.png', '/icona-192.png', '/icona-512.png'):
            try:
                with open(os.path.join(CONF['APP_DIR'], p.lstrip('/')), 'rb') as f:
                    return self.manda(200, 'image/png', f.read())
            except OSError:
                return self.manda(404, 'text/plain; charset=utf-8', b'non trovato\n')
        self.manda(404, 'text/plain; charset=utf-8', b'non trovato\n')

    do_HEAD = do_GET

    def do_POST(self):
        if self.path.split('?', 1)[0] not in ('/api.cgi', '/data/pin/api.cgi'):
            return self.manda(404, 'text/plain; charset=utf-8', b'non trovato\n')
        try:
            n = int(self.headers.get('Content-Length', ''))
        except ValueError:
            return self.manda(411, 'text/plain; charset=utf-8', b'lunghezza mancante\n')
        if n > 8192:
            return self.manda(413, 'text/plain; charset=utf-8', b'richiesta troppo grande\n')
        corpo = self.rfile.read(n).decode('ascii', 'replace')
        dal_tunnel = bool(self.headers.get('Cf-Connecting-Ip'))
        ip = self.headers.get('Cf-Connecting-Ip') or self.client_address[0]
        try:
            ca, cp = self._cookie()
            tipo, uscita, extra = gestisci(corpo, dal_tunnel, ca, cp, ip)
        except Errore as e:
            return self.manda(e.stato, 'text/plain; charset=utf-8', (e.testo + '\n').encode('utf-8'))
        except Exception as e:
            self.log_error('errore interno: %r', e)
            return self.manda(500, 'text/plain; charset=utf-8', b'errore interno\n')
        self.manda(200, tipo, uscita if uscita.endswith(b'\n') else uscita + b'\n', extra)

    def log_message(self, formato, *argomenti):
        sys.stderr.write('%s %s\n' % (self.address_string(), formato % argomenti))


def main():
    conf = '/etc/citofono/citofono.conf'
    if len(sys.argv) == 3 and sys.argv[1] == '--conf':
        conf = sys.argv[2]
    leggi_conf(conf)
    server = http.server.ThreadingHTTPServer((CONF['INDIRIZZO'], int(CONF['PORTA'])), Gestore)
    if CONF['CERT']:
        ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        ctx.load_cert_chain(CONF['CERT'], CONF['CHIAVE_TLS'])
        server.socket = ctx.wrap_socket(server.socket, server_side=True)
    print('citofono su %s://%s:%s/  (admin: /admin)  -> placca %s' % (
        'https' if CONF['CERT'] else 'http', CONF['INDIRIZZO'], CONF['PORTA'], CONF['PLACCA'] or '(da impostare)'),
        flush=True)
    server.serve_forever()


if __name__ == '__main__':
    main()
