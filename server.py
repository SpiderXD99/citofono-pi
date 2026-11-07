#!/usr/bin/env python3
"""Citofono su Raspberry Pi: pagina dei PIN + pannello admin, senza telnet.

Fa da ponte verso la placca (web API sulla porta 80, autenticata con la sua
password admin) e da pannello di configurazione del Pi (tunnel Cloudflare,
indirizzi, credenziali). Serve solo /data/pin/ e /admin: tutto il resto e' 404.

I SEGRETI STANNO SOLO SUL PI, in /etc/citofono/ (file 600), MAI nel repository:
  citofono.conf   indirizzi e opzioni (PLACCA, MONITOR, porte, ADMIN_LAN_ONLY...)
  admin.json      utente + hash PBKDF2 della password del pannello admin
  tunnel.env      TUNNEL_TOKEN del tunnel Cloudflare
  cert.pem/key.pem  certificato del server (autofirmato)

Solo libreria standard di Python 3. Avvio: server.py --conf /etc/citofono/citofono.conf
"""

import hashlib
import hmac
import http.server
import json
import os
import re
import secrets
import ssl
import subprocess
import sys
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
}
UTENTE = 'admin'                           # unico utente della placca ammesso dal proxy
VALORE = re.compile(r'^[A-Za-z0-9%._~*()!-]*$')  # valori form (gia' url-encoded dal browser)
INOLTRATI = {'action', 'index', 'group', 'name', 'room', 'relays', 'status', 'type', 'cards',
             'pin_mode', 'pin_code', 'pass_enable', 'pass_num', 'active_enable', 'start_date',
             'end_date', 'start_time', 'end_time', 'week', 'password', 'old_password',
             'new_password'}
CAMPI_SEMPLICI = {'op', 'sid', 'api', 'user', 'hash', 'azione', 'token',
                  'admin_user', 'admin_pass', 'nuovo_user', 'nuova_pass', 'placca', 'monitor', 'pubblico'}
BLOCCATA = 'la placca ha bloccato gli accessi per qualche minuto dopo una password sbagliata: riprova tra 2-3 minuti'
ITER = 210000                              # iterazioni PBKDF2 per la password admin
SESSIONI = {}                              # token admin -> scadenza (epoch)
DURATA = 1800                              # 30 minuti di sessione admin
TENTATIVI = {}                             # ip -> [numero fail, bloccato_fino_a]
MAX_FAIL, BLOCCO = 5, 300                  # 5 errori -> 5 minuti di blocco
CONF = dict(PREDEFINITI)
VERSIONE = []


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


# ---------- rete verso placca / monitor ----------
def richiesta(url, dati=None, sid='', timeout=15):
    intestazioni = {}
    if sid:
        intestazioni['Cookie'] = 'SessionID=' + sid
    if dati is not None:
        intestazioni['Content-Type'] = 'application/x-www-form-urlencoded'
        dati = dati.encode('ascii')
    contesto = ssl._create_unverified_context()  # certificati autofirmati di placca/monitor
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=dati, headers=intestazioni),
                                    timeout=timeout, context=contesto) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except (urllib.error.URLError, OSError):
        return 0, b''


def alla_placca(percorso, dati=None, sid=''):
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
def gestisci(corpo, dal_tunnel, cookie, ip):
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

    # --- admin: login e operazioni sul Pi ---
    if op == 'admin_login':
        if CONF['ADMIN_LAN_ONLY'] == '1' and dal_tunnel:
            raise Errore(403, 'il pannello admin si apre solo da casa')
        if bloccato(ip):
            raise Errore(429, 'troppi tentativi: riprova tra qualche minuto')
        u = urllib.parse.unquote_plus(campi.get('admin_user', ''))
        p = urllib.parse.unquote_plus(campi.get('admin_pass', ''))
        if not verifica_admin(u, p):
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
        if not sessione_ok(cookie):
            raise Errore(401, 'accesso admin richiesto')
    elif op in ('tunnel', 'monitor'):
        # il pannello (sessione admin) oppure la pagina PIN (sessione valida della placca)
        if not sessione_ok(cookie):
            if not sid:
                raise Errore(401, 'accesso richiesto')
            alla_placca('/cgi-bin/webapi.cgi?api=unlock_passwd', None, sid)

    if op == 'admin_logout':
        SESSIONI.pop(cookie, None)
        return js, b'{"ok":1}', {'Set-Cookie': 'AdminSID=; Max-Age=0; Path=/'}
    if op == 'admin_state':
        dati = {'placca': CONF['PLACCA'], 'monitor': CONF['MONITOR'], 'pubblico': CONF['PUBBLICO'],
                'lan_only': CONF['ADMIN_LAN_ONLY'], 'tunnel': stato_tunnel()}
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
    if op == 'admin_update':
        return testo, aggiorna_dal_repo().encode('utf-8') or b'aggiornato\n', None
    if op == 'tunnel':
        s = comanda_tunnel(campi.get('azione', ''), campi.get('token', ''))
        return testo, ('\n'.join('%s=%s' % kv for kv in s.items()) + '\n').encode('utf-8'), None
    if op == 'monitor':
        if not campi.get('old_password') or not campi.get('new_password'):
            raise Errore(400, 'password mancanti')
        return js, password_monitor(campi['old_password'], campi['new_password']), None

    # --- pagina PIN: proxy verso la placca (serve la sessione della placca) ---
    if not re.fullmatch(r'[0-9.:]+', CONF['PLACCA']):
        raise Errore(502, 'IP della placca non impostato: aprilo nel pannello admin')
    if op == 'login':
        h = campi.get('hash', '')
        if campi.get('user') != UTENTE or not re.fullmatch(r'[0-9a-f]{32}', h):
            raise Errore(401, 'utente o password non validi')
        return js, alla_placca('/cgi-bin/webapi.cgi?api=login&username=%s&password=%s' % (UTENTE, h)), None
    if op == 'api':
        api = campi.get('api', '')
        if api not in ('user', 'unlock_passwd', 'password'):
            raise Errore(403, 'operazione non permessa')
        if not sid:
            raise Errore(401, 'accesso richiesto')
        return js, alla_placca('/cgi-bin/webapi.cgi?api=' + api, '&'.join(avanti), sid), None
    if op == 'elenco':
        if not sid:
            raise Errore(401, 'accesso richiesto')
        return testo, alla_placca('/data/users.xml', None, sid), None
    raise Errore(400, 'operazione sconosciuta')


class Gestore(http.server.BaseHTTPRequestHandler):
    server_version = 'citofono'
    sys_version = ''
    protocol_version = 'HTTP/1.1'

    def _cookie(self):
        for pezzo in (self.headers.get('Cookie') or '').split(';'):
            k, _, v = pezzo.strip().partition('=')
            if k == 'AdminSID':
                return v
        return ''

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
        if p in ('/', '/data', '/data/', '/data/pin'):
            return self.manda(302, 'text/plain; charset=utf-8', b'', {'Location': '/data/pin/'})
        if p in ('/data/pin/', '/data/pin/index.html'):
            return self._pagina(CONF['PAGINA'])
        if p in ('/data/pin/admin', '/data/pin/admin.html', '/admin', '/admin/'):
            if CONF['ADMIN_LAN_ONLY'] == '1' and self.headers.get('Cf-Connecting-Ip'):
                return self.manda(403, 'text/plain; charset=utf-8', b'il pannello admin si apre solo da casa\n')
            return self._pagina(CONF['ADMIN_PAGINA'])
        self.manda(404, 'text/plain; charset=utf-8', b'non trovato\n')

    do_HEAD = do_GET

    def do_POST(self):
        if self.path.split('?', 1)[0] != '/data/pin/api.cgi':
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
            tipo, uscita, extra = gestisci(corpo, dal_tunnel, self._cookie(), ip)
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
    print('citofono su %s://%s:%s/data/pin/  (admin: /admin)  -> placca %s' % (
        'https' if CONF['CERT'] else 'http', CONF['INDIRIZZO'], CONF['PORTA'], CONF['PLACCA'] or '(da impostare)'),
        flush=True)
    server.serve_forever()


if __name__ == '__main__':
    main()
