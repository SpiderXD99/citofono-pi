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

import base64
import hashlib
import hmac
import html
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

try:                                       # solo per le notifiche push (python3-cryptography)
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    CRITTO = True
except ImportError:
    CRITTO = False

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
    'PUSH': '/etc/citofono/push.json',              # dispositivi iscritti alle notifiche
    'PUSH_STATO': '/etc/citofono/push_stato.json',  # entrate gia' notificate
    'VAPID': '/etc/citofono/vapid.pem',             # chiave privata per firmare le notifiche
    'ACCESSI': '/etc/citofono/accessi.json',        # registro degli accessi al sito (Admin)
    'PROPRIETA': '/etc/citofono/proprieta.json',
    # 'pi' = Raspberry in casa (relay, Wi-Fi, tunnel con systemd); 'fly' = app su Fly.io:
    # la placca arriva da xfrpc sul monitor attraverso frps (PLACCA=127.0.0.1:17080)
    'PIATTAFORMA': 'pi',
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
                  'pass', 'nuova', 'pin_user', 'pin_pass', 'placca_pass', 'ruolo',
                  'ora', 'endpoint', 'p256dh', 'auth', 'modo'}
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
# service worker "prima la rete": serve sempre la versione aggiornata; la cache
# fa solo da fallback offline. All'attivazione svuota le cache delle versioni vecchie.
SW = (b"var CACHE='citofono-v2';\n"
      b"self.addEventListener('install',function(e){self.skipWaiting();});\n"
      b"self.addEventListener('activate',function(e){e.waitUntil(Promise.all([\n"
      b"  self.clients.claim(),\n"
      b"  caches.keys().then(function(k){return Promise.all(k.filter(function(n){return n!==CACHE;})\n"
      b"    .map(function(n){return caches.delete(n);}));})\n"
      b"]));});\n"
      b"self.addEventListener('fetch',function(e){\n"
      b"  var r=e.request; if(r.method!=='GET'){return;}\n"
      b"  var u=new URL(r.url); if(u.origin!==self.location.origin){return;}\n"
      b"  if(/^\\/(api\\.cgi|aggiorna|update)/.test(u.pathname)){return;}\n"
      b"  e.respondWith(fetch(r).then(function(res){\n"
      b"    var c=res.clone(); caches.open(CACHE).then(function(ch){ch.put(r,c);}).catch(function(){});\n"
      b"    return res;\n"
      b"  }).catch(function(){return caches.match(r);}));\n"
      b"});\n"
      # notifiche degli accessi: il Pi le manda cifrate, qui si mostrano; il tocco apre la pagina
      b"self.addEventListener('push',function(e){\n"
      b"  var d={}; try{d=e.data?e.data.json():{};}catch(x){d={body:e.data?e.data.text():''};}\n"
      b"  e.waitUntil(self.registration.showNotification(d.title||'Citofono',{body:d.body||'',\n"
      b"    tag:d.tag||undefined,icon:'/icona-192.png',badge:'/icona-192.png',data:{url:d.url||'/#accessi'}}));\n"
      b"});\n"
      b"self.addEventListener('notificationclick',function(e){\n"
      b"  e.notification.close(); var url=(e.notification.data&&e.notification.data.url)||'/#accessi';\n"
      b"  e.waitUntil(self.clients.matchAll({type:'window',includeUncontrolled:true}).then(function(l){\n"
      b"    for(var i=0;i<l.length;i++){if('focus' in l[i]){l[i].navigate(url).catch(function(){});return l[i].focus();}}\n"
      b"    return self.clients.openWindow(url);\n"
      b"  }));\n"
      b"});\n")


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


def _salva_hash(chiave, md5):
    fd = os.open(CONF[chiave], os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        json.dump({'hash': md5}, f)


def _leggi_hash(chiave):
    try:
        with open(CONF[chiave], encoding='utf-8') as f:
            return json.load(f).get('hash', '')
    except (OSError, ValueError):
        return ''


def salva_placca_hash(md5):
    _salva_hash('PLACCA_CRED', md5)


def leggi_placca_hash():
    return _leggi_hash('PLACCA_CRED')


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


def leggi_registro(sid, ids=None):
    """Registro sblocchi. La placca non restituisce l'elenco: api=unlock_logs da'
    {enable, file_name} e le entrate stanno in un file XML (/data/unlock.xml) con
    <max>N</max> e un blocco <dN> per entrata (group, name, number, status, relays,
    user_type, unlock_type, ts). ids = identificatori dell'utente semplice: si tengono
    solo le entrate col suo nome o il suo numero (PIN/tessera)."""
    try:
        j = json.loads(alla_placca('/cgi-bin/webapi.cgi?api=unlock_logs', None, sid).decode('utf-8', 'replace'))
    except ValueError:
        raise Errore(502, 'risposta della placca non valida (registro)')
    if not isinstance(j, dict):
        j = {}
    d = j.get('data') if isinstance(j.get('data'), dict) else j
    attivo = str(d.get('enable', '0')).strip() == '1'
    nome_file = str(d.get('file_name', '')).strip()
    voci = []
    if attivo and re.fullmatch(r'/data/[A-Za-z0-9_.-]+\.xml', nome_file):
        testo = alla_placca(nome_file, None, sid).decode('utf-8', 'replace')
        for m in re.finditer(r'<d(\d+)>(.*?)</d\1>', testo, re.S):
            r = {k: html.unescape(v.strip()) for k, v in re.findall(r'<(\w+)>([^<]*)</\1>', m.group(2))}
            if ids is not None and not ({r.get('name', '').lower(), r.get('number', '').lower()} & ids):
                continue
            voci.append({k: r.get(k, '') for k in ('group', 'name', 'number', 'status', 'relays',
                                                   'user_type', 'unlock_type', 'ts')})
    return {'enable': 1 if attivo else 0, 'list': voci}


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


# ---------- orologio della placca (api=time) ----------
def sid_placca(sid):
    """Sessione della placca: quella della pagina, o un accesso con la password salvata."""
    if sid:
        return sid
    h = leggi_placca_hash()
    if not h:
        raise Errore(400, 'password della placca non salvata nel pannello admin')
    try:
        return str(json.loads(alla_placca('/cgi-bin/webapi.cgi?api=login&username=%s&password=%s'
                                          % (UTENTE, h)))['data']['SessionID'])
    except (ValueError, KeyError, TypeError):
        raise Errore(401, 'password della placca non corretta')


def _leggi_ora(sid):
    corpo = alla_placca('/cgi-bin/webapi.cgi?api=time', None, sid_placca(sid))
    try:
        j = json.loads(corpo.decode('utf-8', 'replace'))
    except ValueError:
        raise Errore(502, 'risposta non valida (ora della placca)')
    if not isinstance(j, dict):
        j = {}
    d = j.get('data') if isinstance(j.get('data'), dict) else j
    if not re.fullmatch(r'\d{4}-\d\d-\d\d \d\d:\d\d:\d\d', str(d.get('current_time', ''))):
        raise Errore(502, "la placca non ha restituito l'ora")
    return d


def ora_placca(sid):
    d = _leggi_ora(sid)
    return {'ora': d['current_time'], 'tz': str(d.get('tz', ''))}


def imposta_ora(sid, ora):
    """Imposta l'ora della placca (stringa 'AAAA-MM-GG hh:mm:ss', ora locale). Si
    rileggono e si rimandano fuso e formati, come fa la web originale."""
    d = _leggi_ora(sid)
    if not re.fullmatch(r'20\d\d-[01]\d-[0-3]\d [0-2]\d:[0-5]\d:[0-5]\d', ora):
        raise Errore(400, 'ora non valida')
    campi = [('current_time', ora), ('ntp_enable', '0')]
    for k in ('tz', 'time_format', 'date_format'):
        if k in d:
            campi.append((k, str(d[k])))
    corpo = '&'.join('%s=%s' % (k, urllib.parse.quote_plus(v)) for k, v in campi)
    r = alla_placca('/cgi-bin/webapi.cgi?api=time', corpo, sid_placca(sid))
    try:
        esito = int(str(json.loads(r.decode('utf-8', 'replace')).get('result', '0')))
    except (ValueError, AttributeError):
        esito = 0
    if esito < 0:
        raise Errore(502, "la placca ha rifiutato l'ora")
    return ora_placca(sid)


# ---------- notifiche push degli accessi (Web Push, RFC 8291 + VAPID RFC 8292) ----------
# Il Pi legge il registro della placca ogni POLL_PUSH secondi e, per ogni entrata nuova,
# manda una notifica cifrata ai dispositivi iscritti. Nessun servizio esterno a parte il
# servizio push del browser (Google, Mozilla, Apple, Microsoft): solo verso di loro si
# inviano richieste, mai verso indirizzi scelti dal client.
POLL_PUSH = 20
MAX_PER_GIRO = 5                           # oltre: una notifica riassuntiva
SERVIZI_PUSH = ('fcm.googleapis.com', 'updates.push.services.mozilla.com', 'push.services.mozilla.com',
                'web.push.apple.com', 'notify.windows.com')
_PUSH_LOCK = threading.Lock()
_SORVEGLIA = {'sid': '', 'pausa': 0.0, 'nuovo_inizio': True, 'errore': ''}


def b64u(b):
    return base64.urlsafe_b64encode(b).rstrip(b'=').decode('ascii')


def ub64u(t):
    t = str(t)
    return base64.urlsafe_b64decode(t + '=' * (-len(t) % 4))


def _hkdf(sale, ikm, info, n):
    prk = hmac.new(sale, ikm, hashlib.sha256).digest()
    return hmac.new(prk, info + b'\x01', hashlib.sha256).digest()[:n]


def chiave_vapid():
    try:
        with open(CONF['VAPID'], 'rb') as f:
            return serialization.load_pem_private_key(f.read(), None)
    except (OSError, ValueError):
        k = ec.generate_private_key(ec.SECP256R1())
        pem = k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                              serialization.NoEncryption())
        fd = os.open(CONF['VAPID'], os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'wb') as f:
            f.write(pem)
        return k


def _punto(chiave_pubblica):
    return chiave_pubblica.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)


def vapid_pubblica():
    return b64u(_punto(chiave_vapid().public_key()))


def cifra_push(p256dh, auth, testo, sale=None, locale=None):
    """Cifratura aes128gcm di RFC 8291 (un solo record)."""
    ua_pub = ub64u(p256dh)
    segreto = ub64u(auth)
    as_priv = locale or ec.generate_private_key(ec.SECP256R1())
    as_pub = _punto(as_priv.public_key())
    ecdh = as_priv.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_pub))
    sale = sale or os.urandom(16)
    ikm = _hkdf(segreto, ecdh, b'WebPush: info\x00' + ua_pub + as_pub, 32)
    cek = _hkdf(sale, ikm, b'Content-Encoding: aes128gcm\x00', 16)
    nonce = _hkdf(sale, ikm, b'Content-Encoding: nonce\x00', 12)
    cifrato = AESGCM(cek).encrypt(nonce, testo + b'\x02', None)
    return sale + (4096).to_bytes(4, 'big') + bytes([len(as_pub)]) + as_pub + cifrato


def _jwt_vapid(aud):
    sub = ('https://' + CONF['PUBBLICO']) if re.fullmatch(r'[A-Za-z0-9.-]+', CONF['PUBBLICO'] or '') \
        else 'mailto:citofono@example.invalid'
    testa = b64u(json.dumps({'typ': 'JWT', 'alg': 'ES256'}, separators=(',', ':')).encode())
    corpo = b64u(json.dumps({'aud': aud, 'exp': int(time.time()) + 12 * 3600, 'sub': sub},
                            separators=(',', ':')).encode())
    firmato = (testa + '.' + corpo).encode('ascii')
    r, s_ = decode_dss_signature(chiave_vapid().sign(firmato, ec.ECDSA(hashes.SHA256())))
    return testa + '.' + corpo + '.' + b64u(r.to_bytes(32, 'big') + s_.to_bytes(32, 'big'))


def endpoint_valido(url):
    u = urllib.parse.urlsplit(url)
    host = (u.hostname or '').lower()
    extra = [h for h in (CONF.get('PUSH_EXTRA') or '').split(',') if h]   # solo per le prove
    if host in extra:
        return True
    return u.scheme == 'https' and any(host == h or host.endswith('.' + h) for h in SERVIZI_PUSH)


def invia_push(iscr, dati, ttl=86400):
    """Manda una notifica. Ritorna il codice HTTP (0 = rete); 404/410 = iscrizione scaduta."""
    url = iscr['endpoint']
    if not endpoint_valido(url):
        return 400
    u = urllib.parse.urlsplit(url)
    corpo = cifra_push(iscr['p256dh'], iscr['auth'], json.dumps(dati).encode('utf-8'))
    intest = {'TTL': str(ttl), 'Urgency': 'high', 'Content-Encoding': 'aes128gcm',
              'Content-Type': 'application/octet-stream', 'Content-Length': str(len(corpo)),
              'Authorization': 'vapid t=%s, k=%s' % (_jwt_vapid('%s://%s' % (u.scheme, u.netloc)), vapid_pubblica())}
    conn = None
    try:
        if u.scheme == 'https':
            conn = http.client.HTTPSConnection(u.hostname, u.port or 443, timeout=15)
        else:
            conn = http.client.HTTPConnection(u.hostname, u.port or 80, timeout=15)
        conn.request('POST', u.path + (('?' + u.query) if u.query else ''), body=corpo, headers=intest)
        r = conn.getresponse()
        _SORVEGLIA['risposta'] = '%d %s' % (r.status, r.read(300).decode('utf-8', 'replace').strip())
        return r.status
    except (OSError, http.client.HTTPException):
        return 0
    finally:
        if conn is not None:
            conn.close()


def _leggi_json(chiave, vuoto):
    try:
        with open(CONF[chiave], encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return vuoto


def _salva_json(chiave, dati):
    fd = os.open(CONF[chiave], os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        json.dump(dati, f)


def iscrizioni():
    v = _leggi_json('PUSH', [])
    return v if isinstance(v, list) else []


def iscrivi(utente, ruolo, endpoint, p256dh, auth, modo):
    if not CRITTO:
        raise Errore(500, 'sul Pi manca la libreria: sudo apt install python3-cryptography')
    if not endpoint_valido(endpoint):
        raise Errore(400, 'servizio di notifiche non riconosciuto')
    try:
        if len(ub64u(p256dh)) != 65 or len(ub64u(auth)) != 16:
            raise ValueError
    except ValueError:
        raise Errore(400, 'chiavi della notifica non valide')
    if modo not in ('tutte', 'negati'):
        raise Errore(400, 'modo non valido')
    with _PUSH_LOCK:
        lista = [i for i in iscrizioni() if i.get('endpoint') != endpoint]
        if not lista:                         # primo dispositivo: si riparte dal registro di adesso
            _SORVEGLIA['nuovo_inizio'] = True
        if len(lista) >= 30:
            raise Errore(400, 'troppi dispositivi iscritti')
        lista.append({'endpoint': endpoint, 'p256dh': p256dh, 'auth': auth, 'user': utente,
                      'ruolo': ruolo, 'modo': modo, 'dal': int(time.time())})
        _salva_json('PUSH', lista)


def togli_iscrizione(endpoint, utente=None):
    with _PUSH_LOCK:
        lista = iscrizioni()
        nuova = [i for i in lista if not (i.get('endpoint') == endpoint and (utente is None or i.get('user') == utente))]
        if len(nuova) != len(lista):
            _salva_json('PUSH', nuova)
        return len(nuova) != len(lista)


def _chiave_entrata(r):
    return '|'.join(str(r.get(k, '')) for k in ('ts', 'name', 'number', 'status', 'unlock_type', 'relays'))


METODI = {'0': 'Tessera', '1': 'PIN', '2': 'Citofono / app', '3': 'Pulsante di uscita'}


def testo_notifica(r):
    """Nessun PIN o numero di tessera nel testo: le notifiche si leggono a schermo bloccato."""
    nome = r.get('name') if r.get('name') not in ('', 'None', None) else ''
    numero = r.get('number') not in ('', 'None', None)
    metodo = METODI.get(str(r.get('unlock_type')), 'Apertura')
    try:
        quando = time.strftime('%d/%m %H:%M', time.localtime(int(r.get('ts'))))
    except (TypeError, ValueError):
        quando = ''
    if str(r.get('status')) == '-1':
        titolo = '⛔ ' + (nome or (metodo + ' non valido' if numero else 'Accesso negato'))
    else:
        titolo = '🔓 ' + (nome or (metodo if not numero else 'Sconosciuto'))
    corpo = ' · '.join(x for x in ((metodo if (nome or numero) else ''), quando) if x)
    if str(r.get('status')) == '-1':
        corpo = 'Accesso negato' + (' · ' + corpo if corpo else '')
    return {'title': titolo, 'body': corpo, 'tag': 'accesso-' + str(r.get('ts', '')), 'url': '/#accessi'}


def _destinatari(entrate, sid):
    """Per ogni iscrizione, le entrate che le interessano (utente semplice: solo le sue)."""
    ids_cache = {}
    for i in iscrizioni():
        scelte = entrate
        if i.get('ruolo') == 'user':
            u = i.get('user', '')
            if u not in ids_cache:
                ids_cache[u] = identificatori_utente(u, sid)
            ids = ids_cache[u]
            scelte = [r for r in scelte if {str(r.get('name', '')).lower(), str(r.get('number', '')).lower()} & ids]
        if i.get('modo') == 'negati':
            scelte = [r for r in scelte if str(r.get('status')) == '-1']
        if scelte:
            yield i, scelte


def giro_sorveglianza():
    """Un controllo del registro. Ritorna quante notifiche ha mandato."""
    if not CRITTO or not iscrizioni() or not re.fullmatch(r'[0-9.:]+', CONF['PLACCA'] or ''):
        return 0
    if not leggi_placca_hash():
        _SORVEGLIA['errore'] = 'password della placca non salvata nel pannello admin'
        return 0
    try:
        if not _SORVEGLIA['sid']:
            _SORVEGLIA['sid'] = sid_placca('')
        reg = leggi_registro(_SORVEGLIA['sid'])
    except Errore as e:
        _SORVEGLIA['sid'] = ''
        _SORVEGLIA['errore'] = str(e)
        if e.stato in (401, 503):             # password cambiata o placca bloccata: non insistere
            _SORVEGLIA['pausa'] = time.time() + 180
        return 0
    _SORVEGLIA['errore'] = '' if reg['enable'] else 'il registro degli accessi della placca e\' spento'
    entrate = reg['list']
    chiavi = [_chiave_entrata(r) for r in entrate]
    stato = _leggi_json('PUSH_STATO', {})
    visti = set(stato.get('visti', [])) if isinstance(stato, dict) else set()
    nuove = [] if _SORVEGLIA['nuovo_inizio'] else [r for r, k in zip(entrate, chiavi) if k not in visti]
    _SORVEGLIA['nuovo_inizio'] = False
    _salva_json('PUSH_STATO', {'visti': chiavi})   # le entrate uscite dal registro non tornano
    if not nuove:
        return 0
    nuove.sort(key=lambda r: int(r.get('ts') or 0) if str(r.get('ts', '')).isdigit() else 0)
    mandate = 0
    for iscr, scelte in list(_destinatari(nuove, _SORVEGLIA['sid'])):
        messaggi = [testo_notifica(r) for r in scelte[-MAX_PER_GIRO:]]
        if len(scelte) > MAX_PER_GIRO:
            messaggi.insert(0, {'title': 'Citofono', 'body': 'e altri %d accessi' % (len(scelte) - MAX_PER_GIRO),
                                'tag': 'accessi-altri', 'url': '/#accessi'})
        for m in messaggi:
            st = invia_push(iscr, m)
            if st in (404, 410):              # il browser ha tolto l'iscrizione
                togli_iscrizione(iscr['endpoint'])
                break
            if 200 <= st < 300:
                mandate += 1
    return mandate


def sorveglia():
    while True:
        time.sleep(POLL_PUSH)
        if time.time() < _SORVEGLIA['pausa']:
            continue
        try:
            giro_sorveglianza()
        except Exception as e:                # il thread non deve morire per un errore imprevisto
            _SORVEGLIA['errore'] = 'errore interno: %r' % (e,)


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


def su_fly():
    return CONF['PIATTAFORMA'] == 'fly'


def pid_di(nome):
    """PID del primo processo che si chiama `nome` (lettura di /proc, senza systemd)."""
    try:
        for d in os.listdir('/proc'):
            if d.isdigit():
                try:
                    with open('/proc/%s/comm' % d) as f:
                        if f.read().strip() == nome:
                            return d
                except OSError:
                    pass
    except OSError:
        pass
    return ''


def rss_kb(pid):
    try:
        with open('/proc/%s/status' % pid) as f:
            return next(r.split()[1] for r in f if r.startswith('VmRSS'))
    except (OSError, StopIteration, IndexError):
        return ''


def porta_viva(indirizzo, timeout=2):
    """True se host:porta accetta connessioni. Su Fly frps apre 17080 solo mentre
    xfrpc del monitor e' collegato: e' l'indicatore di 'monitor collegato'."""
    import socket
    host, _, porta = indirizzo.rpartition(':')
    try:
        with socket.create_connection((host, int(porta)), timeout=timeout):
            return True
    except (OSError, ValueError):
        return False


def stato_tunnel():
    if su_fly():                       # su Fly cloudflared lo avvia avvio.sh e resta sempre acceso
        pid = pid_di('cloudflared')
        return {'dove': 'fly', 'installato': int(os.access(CONF['CLOUDFLARED'], os.X_OK)),
                'token': int(bool(os.environ.get('TUNNEL_TOKEN'))), 'attivo': int(bool(pid)),
                'automatico': 1, 'ram': rss_kb(pid) if pid else '', 'libera': meminfo('MemAvailable'),
                'versione': versione_cloudflared()}
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
    elif su_fly():
        raise Errore(400, 'su Fly il tunnel resta sempre acceso; il token si cambia con '
                          '"fly secrets set TUNNEL_TOKEN=..."')
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
def autorizza_bearer(auth):
    """True se 'Authorization: Bearer <password>' corrisponde a un account di ruolo
    admin (o, finche' non ne esiste nessuno, alla password del pannello). Nessun
    token dedicato: si riusano le credenziali gestore gia' esistenti."""
    if not auth or not auth.startswith('Bearer '):
        return False
    pw = auth[7:].strip()
    if not pw:
        return False
    for u in carica_utenti():
        if u.get('ruolo', 'user') == 'admin':
            h = hashlib.pbkdf2_hmac('sha256', pw.encode('utf-8'),
                                    bytes.fromhex(u['salt']), u.get('iter', ITER))
            if hmac.compare_digest(h.hex(), u['hash']):
                return True
    if not esiste_admin():
        a = carica_admin()
        if a:
            h = hashlib.pbkdf2_hmac('sha256', pw.encode('utf-8'),
                                    bytes.fromhex(a['salt']), a.get('iter', ITER))
            if hmac.compare_digest(h.hex(), a['hash']):
                return True
    return False


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
    if su_fly():
        # niente systemd: esco tra un secondo (dopo la risposta) e il ciclo di avvio.sh
        # rilancia server.py con il codice nuovo; frps e cloudflared restano su
        threading.Timer(1.0, os._exit, (0,)).start()
        return out
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


# ---------- registro degli accessi al sito (scheda Admin) ----------
# Ogni tentativo di accesso: quando, da dove, con quale utente e com'e' andato. Per i
# tentativi FALLITI si conserva anche la password scritta (richiesto dal gestore, per
# riconoscere chi prova a indovinarla): il file e' 600 e resta solo sul Pi, e si
# tengono al massimo MAX_ACCESSI voci e GIORNI_ACCESSI giorni.
MAX_ACCESSI, GIORNI_ACCESSI = 300, 90
_ACCESSI_LOCK = threading.Lock()
_RICHIESTA = threading.local()              # user agent e indirizzo reale della richiesta in corso


def registra_accesso(dove, ip, utente, esito, password=None):
    voce = {'t': int(time.time()), 'dove': dove, 'ip': str(ip)[:64], 'user': str(utente or '')[:64],
            'esito': esito, 'ua': str(getattr(_RICHIESTA, 'ua', '') or '')[:160]}
    peer = str(getattr(_RICHIESTA, 'peer', '') or '')
    if peer and peer != voce['ip'] and not peer.startswith('127.') and peer != '::1':
        voce['via'] = peer[:64]                  # Cf-Connecting-Ip non arrivato dal tunnel: sospetto
    if esito != 'ok' and password is not None:
        voce['pass'] = str(password)[:128]
    limite = time.time() - GIORNI_ACCESSI * 86400
    with _ACCESSI_LOCK:
        try:
            with open(CONF['ACCESSI'], encoding='utf-8') as f:
                lista = json.load(f)
            if not isinstance(lista, list):
                lista = []
        except (OSError, ValueError):
            lista = []
        lista = [v for v in lista if isinstance(v, dict) and v.get('t', 0) >= limite]
        lista.append(voce)
        lista = lista[-MAX_ACCESSI:]
        try:
            fd = os.open(CONF['ACCESSI'], os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                json.dump(lista, f)
        except OSError:
            pass                                 # il registro non deve mai impedire l'accesso


def leggi_accessi():
    with _ACCESSI_LOCK:
        try:
            with open(CONF['ACCESSI'], encoding='utf-8') as f:
                lista = json.load(f)
        except (OSError, ValueError):
            return []
    return list(reversed(lista)) if isinstance(lista, list) else []


def svuota_accessi():
    with _ACCESSI_LOCK:
        try:
            os.remove(CONF['ACCESSI'])
        except OSError:
            pass


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
        u = urllib.parse.unquote_plus(campi.get('admin_user', ''))
        p = urllib.parse.unquote_plus(campi.get('admin_pass', ''))
        if CONF['ADMIN_LAN_ONLY'] == '1' and dal_tunnel:
            registra_accesso('admin', ip, u, 'rifiutato', p)
            raise Errore(403, 'il pannello admin si apre solo da casa')
        if bloccato(ip):
            registra_accesso('admin', ip, u, 'bloccato', p)
            raise Errore(429, 'troppi tentativi: riprova tra qualche minuto')
        # /admin si apre con un account di ruolo admin; finche' non ne esiste nessuno
        # vale ancora la vecchia password del pannello (bootstrap, per non restare chiusi fuori)
        if esiste_admin():
            valido = verifica_utente(u, p) and ruolo_utente(u) == 'admin'
        else:
            valido = verifica_admin(u, p)
        if not valido:
            segna_fallito(ip)
            registra_accesso('admin', ip, u, 'fallito', p)
            raise Errore(401, 'utente o password non corretti')
        TENTATIVI.pop(ip, None)
        registra_accesso('admin', ip, u, 'ok')
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
    if su_fly() and op in ('admin_relay_save', 'admin_relay', 'admin_rete_state', 'admin_wifi_scan',
                           'admin_wifi_connect', 'admin_hotspot_save'):
        raise Errore(400, 'non disponibile su Fly: relay e Wi-Fi esistono solo sul Raspberry')
    if op == 'admin_state':
        dati = {'placca': CONF['PLACCA'], 'monitor': CONF['MONITOR'], 'pubblico': CONF['PUBBLICO'],
                'lan_only': CONF['ADMIN_LAN_ONLY'], 'tunnel': stato_tunnel(),
                'piattaforma': CONF['PIATTAFORMA']}
        if su_fly():
            # il monitor e' "collegato" se frps espone la porta della placca (xfrpc connesso)
            dati['collegamento'] = {'placca': porta_viva(CONF['PLACCA']) if CONF['PLACCA'] else False,
                                    'monitor': porta_viva(urllib.parse.urlsplit(CONF['MONITOR']).netloc)
                                    if CONF['MONITOR'] else False,
                                    'frps': bool(pid_di('frps'))}
            dati['ponte'] = None
            return js, json.dumps(dati).encode('utf-8'), None
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
    if op in ('admin_utenti', 'admin_utente_add', 'admin_utente_del', 'admin_utente_ruolo',
              'admin_utente_pass', 'admin_placca_pass'):
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
        if op == 'admin_utente_pass':
            u = urllib.parse.unquote_plus(campi.get('pin_user', ''))
            pw = urllib.parse.unquote_plus(campi.get('nuova', ''))
            if len(pw) < 6:
                raise Errore(400, 'la password deve avere almeno 6 caratteri')
            if not cambia_password_utente(u, pw):
                raise Errore(404, 'account inesistente')
            return js, b'{"ok":1}', None
        elimina_utente(urllib.parse.unquote_plus(campi.get('pin_user', '')))
        return js, b'{"ok":1}', None
    if op == 'admin_accessi':                     # registro degli accessi al sito
        return js, json.dumps({'accessi': leggi_accessi()}).encode('utf-8'), None
    if op == 'admin_accessi_svuota':
        svuota_accessi()
        return js, b'{"ok":1}', None
    if op == 'admin_orologio':                    # ora della placca = quella del telefono
        r = imposta_ora(sid, urllib.parse.unquote_plus(campi.get('ora', '')))
        return js, json.dumps(r).encode('utf-8'), None
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
                registra_accesso('pagina', ip, u, 'fallito', '')
                raise Errore(401, 'utente o password non validi')
            acc = verifica_utente(u, pw)
            boot = (not esiste_admin()) and verifica_admin(u, pw)   # primo gestore: password iniziale del pannello
            if acc or boot:
                if bloccato(ip):
                    registra_accesso('pagina', ip, u, 'bloccato')
                    raise Errore(429, 'troppi tentativi: riprova tra qualche minuto')
                TENTATIVI.pop(ip, None)
                registra_accesso('pagina', ip, u, 'ok')
                pin_user = u
                pin_ruolo = 'admin' if boot else (ruolo_utente(u) or 'user')
                ph = leggi_placca_hash()
                if pin_ruolo != 'admin' and not ph:
                    raise Errore(502, 'password della placca non impostata nel pannello admin')
            elif not carica_utenti():                # nessun account: pw = password della placca
                ph = hashlib.md5(pw.encode('utf-8')).hexdigest()
                registra_accesso('pagina', ip, u, 'placca')   # l'esito lo decide la placca
            else:
                segna_fallito(ip)
                registra_accesso('pagina', ip, u, 'fallito', pw)
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
    if op.startswith('push_'):                   # notifiche degli accessi su questo dispositivo
        if not sid:
            raise Errore(401, 'accesso richiesto')
        if not sess:                             # senza account: la sessione della placca deve essere vera
            alla_placca('/cgi-bin/webapi.cgi?api=unlock_passwd', None, sid)
        chi, chi_ruolo = (sess['user'], sess['ruolo']) if sess else ('admin', 'admin')
        ep = urllib.parse.unquote_plus(campi.get('endpoint', ''))
        if op == 'push_info':
            mio = next((i for i in iscrizioni() if ep and i.get('endpoint') == ep), None)
            r = {'disponibile': CRITTO, 'chiave': vapid_pubblica() if CRITTO else '',
                 'iscritto': bool(mio), 'modo': mio.get('modo', 'tutte') if mio else 'tutte',
                 'placca_pass': bool(leggi_placca_hash()), 'errore': _SORVEGLIA['errore']}
            return js, json.dumps(r).encode('utf-8'), None
        if op == 'push_iscrivi':
            iscrivi(chi, chi_ruolo, ep, campi.get('p256dh', ''), campi.get('auth', ''), campi.get('modo', 'tutte'))
            return js, b'{"ok":1}', None
        if op == 'push_cancella':
            togli_iscrizione(ep, None if chi_ruolo == 'admin' else chi)
            return js, b'{"ok":1}', None
        if op == 'push_prova':
            mio = next((i for i in iscrizioni() if ep and i.get('endpoint') == ep), None)
            if not mio:
                raise Errore(400, 'questo dispositivo non e\' iscritto')
            st = invia_push(mio, {'title': '🔔 Citofono', 'body': 'Le notifiche degli accessi funzionano.',
                                  'tag': 'prova', 'url': '/#accessi'})
            if st in (404, 410):
                togli_iscrizione(ep)
                raise Errore(410, 'il servizio di notifiche non riconosce piu\' questo dispositivo (%s): '
                                  'chiedi di nuovo l\'accesso alle notifiche' % _SORVEGLIA.get('risposta', ''))
            if not 200 <= st < 300:
                raise Errore(502, 'il servizio di notifiche ha risposto %s' % (st or 'niente'))
            return js, b'{"ok":1}', None
        raise Errore(400, 'operazione sconosciuta')
    if op == 'registro':                         # entrate: il gestore vede tutto, l'utente solo le sue
        if not sid:
            raise Errore(401, 'accesso richiesto')
        ids = identificatori_utente(sess['user'], sid) if ruolo == 'user' else None
        return js, json.dumps(leggi_registro(sid, ids)).encode('utf-8'), None
    if op == 'orologio':                         # ora della placca (lettura, per tutti)
        if not sid:
            raise Errore(401, 'accesso richiesto')
        r = {'gestore': bool(admin_pagina)}
        try:
            r['placca'] = ora_placca(sid)
        except Errore as e:
            r['placca'] = {'errore': str(e)}
        return js, json.dumps(r).encode('utf-8'), None
    if op == 'api':
        api = campi.get('api', '')
        if api not in ('user', 'unlock_passwd', 'password', 'card', 'unlock_logs'):
            raise Errore(403, 'operazione non permessa')
        if not sid:
            raise Errore(401, 'accesso richiesto')
        corpo_api = '&'.join(avanti)
        if ruolo == 'user':                          # utente semplice
            if api != 'user':                        # le proprie entrate si leggono con op=registro
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

    def _aggiorna_endpoint(self):
        """Endpoint dedicato: fa SOLO git pull + riavvio. Autenticazione Bearer
        con una password di ruolo admin (Authorization: Bearer <password>)."""
        ip = self.headers.get('Cf-Connecting-Ip') or self.client_address[0]
        if bloccato(ip):
            return self.manda(429, 'text/plain; charset=utf-8', b'troppi tentativi: riprova tra qualche minuto\n')
        _RICHIESTA.ua = self.headers.get('User-Agent', ''); _RICHIESTA.peer = self.client_address[0]
        auth = self.headers.get('Authorization', '')
        if not autorizza_bearer(auth):
            segna_fallito(ip)
            registra_accesso('aggiorna', ip, '', 'fallito', auth[7:] if auth.startswith('Bearer ') else auth)
            return self.manda(401, 'text/plain; charset=utf-8', b'non autorizzato\n',
                              {'WWW-Authenticate': 'Bearer'})
        TENTATIVI.pop(ip, None)
        registra_accesso('aggiorna', ip, '', 'ok')
        try:
            aggiorna_dal_repo()
        except Errore as e:
            return self.manda(e.stato, 'text/plain; charset=utf-8', (e.testo + '\n').encode('utf-8'))
        return self.manda(200, 'text/plain; charset=utf-8', b'aggiornamento avviato\n')

    def do_GET(self):
        p = self.path.split('?', 1)[0]
        if p in ('/aggiorna', '/update') and self.command != 'HEAD':
            return self._aggiorna_endpoint()
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
        p = self.path.split('?', 1)[0]
        if p in ('/aggiorna', '/update'):
            try:
                n = int(self.headers.get('Content-Length') or 0)
            except ValueError:
                n = 0
            if n > 1048576:
                return self.manda(413, 'text/plain; charset=utf-8', b'richiesta troppo grande\n')
            if n:
                self.rfile.read(n)                 # scarta l'eventuale corpo (es. payload webhook)
            return self._aggiorna_endpoint()
        if p not in ('/api.cgi', '/data/pin/api.cgi'):
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
        _RICHIESTA.ua = self.headers.get('User-Agent', ''); _RICHIESTA.peer = self.client_address[0]
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
    threading.Thread(target=sorveglia, daemon=True).start()   # notifiche degli accessi
    print('citofono su %s://%s:%s/  (admin: /admin)  -> placca %s' % (
        'https' if CONF['CERT'] else 'http', CONF['INDIRIZZO'], CONF['PORTA'], CONF['PLACCA'] or '(da impostare)'),
        flush=True)
    server.serve_forever()


if __name__ == '__main__':
    main()
