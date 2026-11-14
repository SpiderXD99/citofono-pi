#!/usr/bin/env python3
"""Banco di prova del bot: finto Telegram + finta placca, poi scenari.

Uso (Linux/WSL):  python3 banco.py -- COMANDO PER LANCIARE IL BOT
  es.  python3 banco.py -- busybox sh ../bot.sh prova
       python3 banco.py --solo-busybox /tmp/bb -- busybox sh ../bot.sh prova
Con --solo-busybox DIR il PATH del bot contiene SOLO i comandi di busybox (link in DIR),
cosi' non si usa per sbaglio uno strumento che sul monitor non c'e'.
"""
import datetime, hashlib, json, os, re, shutil, subprocess, sys, tempfile, threading, time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TOKEN = '123456789:AAprovaprovaprovaprovaprovaprova12'
GESTORE, ESTRANEO = 1001, 2002
PASSWORD_PLACCA = 'segreto12'
SID = '777'

# ---------------- finto Telegram ----------------
class TG:
    def __init__(self):
        self.coda, self.inviati, self.cv = [], [], threading.Condition()
        self.prossimo, self.comandi, self.offset_visti = 1, None, []

    def messaggio(self, da, testo, chat=None, citato=None, spazi=False):
        m = {'message_id': self.prossimo, 'from': {'id': da, 'is_bot': False, 'first_name': 'Prova'},
             'chat': {'id': chat if chat is not None else da, 'type': 'private'},
             'date': int(time.time())}
        if citato:
            m['reply_to_message'] = {'message_id': 1, 'from': {'id': 999, 'is_bot': True},
                                     'chat': {'id': da}, 'date': 1, 'text': citato}
        m['text'] = testo
        u = {'update_id': 5000 + self.prossimo, 'message': m}
        self.prossimo += 1
        with self.cv:
            self.coda.append((u, spazi)); self.cv.notify_all()

TGS = TG()

class GestoreTG(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_POST(self):
        n = int(self.headers.get('Content-Length') or 0)
        campi = dict(urllib.parse.parse_qsl(self.rfile.read(n).decode()))
        m = re.fullmatch(r'/bot([^/]+)/(\w+)', self.path)
        if not m or m.group(1) != TOKEN:
            return self.manda(401, {'ok': False, 'description': 'Unauthorized'})
        metodo = m.group(2)
        if metodo == 'getMe':
            return self.manda(200, {'ok': True, 'result': {'id': 1, 'is_bot': True, 'username': 'citofono_prova_bot'}})
        if metodo == 'setMyCommands':
            TGS.comandi = json.loads(campi['commands'])
            return self.manda(200, {'ok': True, 'result': True})
        if metodo == 'sendMessage':
            with TGS.cv:
                TGS.inviati.append((int(campi['chat_id']), campi['text'])); TGS.cv.notify_all()
            return self.manda(200, {'ok': True, 'result': {'message_id': 1}})
        if metodo == 'getUpdates':
            off = int(campi.get('offset') or 0)
            TGS.offset_visti.append(off)
            fine = time.time() + min(int(campi.get('timeout') or 0), 2)
            with TGS.cv:
                while True:
                    pronti = [(u, s) for u, s in TGS.coda if u['update_id'] >= off]
                    if pronti or time.time() >= fine:
                        break
                    TGS.cv.wait(0.2)
            spazi = any(s for _, s in pronti)
            corpo = {'ok': True, 'result': [u for u, _ in pronti]}
            return self.manda(200, corpo, spazi)
        self.manda(404, {'ok': False})

    def manda(self, stato, d, spazi=False):
        b = (json.dumps(d) if spazi else json.dumps(d, separators=(',', ':'))).encode()
        self.send_response(stato); self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(b))); self.end_headers(); self.wfile.write(b)

# ---------------- finta placca ----------------
COL = ['nome', 'gruppo', 'stanza', 'tipo', 'stato', 'rele', 'tessere', 'pin_mode', 'pin',
       'pass_on', 'usi', 'valid_on', 'dal', 'al', 'dalle', 'alle', 'settimana']
class Placca:
    def __init__(self):
        self.righe, self.in_corso, self.richieste, self.scadi_ogni, self.login = [], None, 0, 7, 0
        self.sid_valido = SID
PL = Placca()

class GestorePlacca(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def cookie_ok(self):
        PL.richieste += 1
        if PL.richieste % PL.scadi_ogni == 0:      # ogni tanto la sessione scade: il bot deve rientrare
            PL.sid_valido = str(int(PL.sid_valido) + 1)
            return False
        return ('SessionID=%s' % PL.sid_valido) in (self.headers.get('Cookie') or '')

    def do_GET(self):
        u = urllib.parse.urlsplit(self.path); q = dict(urllib.parse.parse_qsl(u.query))
        if u.path == '/cgi-bin/webapi.cgi' and q.get('api') == 'login':
            if q.get('username') == 'admin' and q.get('password') == hashlib.md5(PASSWORD_PLACCA.encode()).hexdigest():
                PL.login += 1
                return self.manda(200, json.dumps({'result': '0', 'data': {'SessionID': PL.sid_valido}}))
            return self.manda(401, 'no')
        if not self.cookie_ok():
            return self.manda(401, 'Unauthorized')
        if u.path == '/cgi-bin/webapi.cgi' and q.get('api') == 'user':
            return self.manda(200, json.dumps({'result': '0', 'file_name': '/data/users.xml'}))
        if u.path == '/data/users.xml':
            if not PL.righe:
                return self.manda(404, 'not found')
            return self.manda(200, '\n'.join(PL.righe) + '\n', 'text/xml')
        self.manda(404, 'not found')

    def do_POST(self):
        n = int(self.headers.get('Content-Length') or 0)
        corpo = self.rfile.read(n).decode()
        if not self.cookie_ok():
            return self.manda(401, 'Unauthorized')
        f = dict(urllib.parse.parse_qsl(corpo, keep_blank_values=True))
        a = f.get('action')
        if a == '3':
            esito, PL.in_corso = (PL.in_corso or '0'), None
            return self.manda(200, json.dumps({'result': esito}))
        if a == '1':
            if f.get('active_enable') == '1' and f.get('end_date', '') < f.get('start_date', ''):
                return self.manda(200, json.dumps({'result': '-3'}))
            v = {'nome': f['name'], 'gruppo': f.get('group', '-1'), 'stanza': f.get('room') or '-1',
                 'tipo': f.get('type', '0'), 'stato': f.get('status', '1'), 'rele': f.get('relays', ''),
                 'tessere': f.get('cards', ''), 'pin_mode': f.get('pin_mode', '0'), 'pin': f.get('pin_code', ''),
                 'pass_on': f.get('pass_enable', '0'), 'usi': f.get('pass_num', '0'),
                 'valid_on': f.get('active_enable', '0'), 'dal': f.get('start_date', ''), 'al': f.get('end_date', ''),
                 'dalle': f.get('start_time', ''), 'alle': f.get('end_time', ''), 'settimana': f.get('week', '')}
            PL.righe.append(','.join(v[k] for k in COL))
        elif a == '0':
            del PL.righe[int(f['index'])]
        else:
            return self.manda(200, json.dumps({'result': '-1'}))
        PL.in_corso = '0'
        return self.manda(200, json.dumps({'result': '100'}))   # "in corso": il bot deve chiedere action=3

    def manda(self, stato, testo, tipo='application/json'):
        b = testo.encode()
        self.send_response(stato); self.send_header('Content-Type', tipo)
        self.send_header('Content-Length', str(len(b))); self.end_headers(); self.wfile.write(b)

def riga(nome):
    for r in PL.righe:
        c = r.split(',')
        if c[0] == nome:
            return dict(zip(COL, c))
    return None

# ---------------- scenari ----------------
ok = tot = 0
def V(nome, cond, extra=''):
    global ok, tot
    tot += 1; ok += 1 if cond else 0
    print(('  OK ' if cond else 'FAIL ') + nome + (('   [' + extra.replace('\n', ' | ')[:160] + ']') if extra and not cond else ''))

def chiedi(da, testo, chat=None, **kw):
    with TGS.cv:
        n = len(TGS.inviati)
    TGS.messaggio(da, testo, chat, **kw)
    fine = time.time() + int(os.environ.get('BANCO_ATTESA', '30'))   # in emulazione ARM serve di piu'
    with TGS.cv:
        while len(TGS.inviati) == n and time.time() < fine:
            TGS.cv.wait(0.2)
        return TGS.inviati[n] if len(TGS.inviati) > n else (None, '(nessuna risposta)')

def main():
    args = sys.argv[1:]
    solo_bb = None
    if args and args[0] == '--solo-busybox':
        solo_bb, args = args[1], args[2:]
    if args and args[0] == '--':
        args = args[1:]
    tg = ThreadingHTTPServer(('127.0.0.1', 0), GestoreTG)
    pl = ThreadingHTTPServer(('127.0.0.1', 0), GestorePlacca)
    for s in (tg, pl):
        threading.Thread(target=s.serve_forever, daemon=True).start()
    d = tempfile.mkdtemp(); v = os.path.join(d, 'var')
    with open(os.path.join(d, 'config'), 'w') as f:
        f.write('TOKEN=%s\nGESTORI=%d\nPLACCA_MD5=%s\nPLACCA=127.0.0.1:%d\nRELE=0;1\nTG_API=http://127.0.0.1:%d\n'
                % (TOKEN, GESTORE, hashlib.md5(PASSWORD_PLACCA.encode()).hexdigest(), pl.server_port, tg.server_port))
    env = dict(os.environ, TGBOT_DIR=d, TGBOT_TMP=v)
    if solo_bb:
        env['TGBOT_PATH'] = solo_bb
        env['PATH'] = solo_bb
    bot = subprocess.Popen(args, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    uscita = []
    threading.Thread(target=lambda: uscita.extend(bot.stdout), daemon=True).start()
    try:
        oggi = datetime.date.today()
        fine = time.time() + int(os.environ.get('BANCO_ATTESA', '30'))
        while TGS.comandi is None and time.time() < fine and bot.poll() is None:
            time.sleep(0.3)
        V('menu dei comandi registrato (setMyCommands)', TGS.comandi and TGS.comandi[0]['command'] == 'codici')

        c, t = chiedi(ESTRANEO, '/codici')
        V('estraneo: rifiutato, gli viene detto il suo ID', c == ESTRANEO and 'Non sei autorizzato' in t and str(ESTRANEO) in t, t)
        c, t = chiedi(ESTRANEO, '/id')
        V('/id funziona per chiunque', str(ESTRANEO) in t, t)
        c, t = chiedi(GESTORE, '/codici', chat=-500)
        V('gestore in un gruppo: rifiutato (i PIN non vanno in chat condivise)', 'chat privata' in t, t)

        c, t = chiedi(GESTORE, '/codici')
        V('elenco vuoto', 'Nessun codice' in t, t)

        c, t = chiedi(GESTORE, '/ospite Mario 3 x2')
        m = re.search(r'PIN (\d{6})', t)
        r = riga('Mario')
        fine3 = (oggi + datetime.timedelta(days=2)).isoformat()
        V('/ospite Mario 3 x2: risposta con PIN a 6 cifre', bool(m), t)
        V('  sulla placca: PIN, 3 giorni, 2 usi, tutto il giorno, tutti i giorni, rele 0;1',
          bool(r and m and r['pin'] == m.group(1) and r['dal'] == oggi.isoformat() and r['al'] == fine3
               and r['pass_on'] == '1' and r['usi'] == '2' and r['valid_on'] == '1' and r['dalle'] == '00:00'
               and r['alle'] == '23:59' and r['settimana'] == '1;1;1;1;1;1;1' and r['rele'] == '0;1'), str(r))
        V('  messaggio da inoltrare con PIN e #', bool(m) and ('digita %s e poi #' % m.group(1)) in t, t)

        c, t = chiedi(GESTORE, '/ospite Anna 2026-12-24 2026-12-26')
        r = riga('Anna')
        V('/ospite con date esplicite', bool(r and r['dal'] == '2026-12-24' and r['al'] == '2026-12-26' and r['pass_on'] == '0'), t + str(r))
        c, t = chiedi(GESTORE, '/ospite Zeta 2026-12-26 2026-12-24')
        V('date al contrario: rifiutate prima di toccare la placca', 'prima di quella di inizio' in t and riga('Zeta') is None, t)

        c, t = chiedi(GESTORE, '/permanente Casa 4821')
        r = riga('Casa')
        V('/permanente Casa 4821', bool(r and r['pin'] == '4821' and r['valid_on'] == '0' and r['pass_on'] == '0'), t + str(r))
        c, t = chiedi(GESTORE, '/permanente Doppio 4821')
        V('PIN gia usato: rifiutato', 'gia\' usato' in t and riga('Doppio') is None, t)
        c, t = chiedi(GESTORE, '/permanente Corto 12')
        V('PIN troppo corto: rifiutato', 'da 4 a 8 cifre' in t and riga('Corto') is None, t)

        # input ostili: niente esecuzione, niente glob, niente campi in piu' verso la placca
        n0 = len(PL.righe)
        c, t = chiedi(GESTORE, '/ospite Ma$(reboot) 3')
        V('nome con $(...) rifiutato', 'NOME' in t and len(PL.righe) == n0, t)
        c, t = chiedi(GESTORE, '/ospite *')
        V('nome * (glob) rifiutato', 'NOME' in t and len(PL.righe) == n0, t)
        c, t = chiedi(GESTORE, '/ospite Eva&pin_code=1111 2')
        V('nome con & (iniezione di campi) rifiutato', 'NOME' in t and len(PL.righe) == n0, t)
        c, t = chiedi(GESTORE, '/permanente Bob "1234')
        V('PIN con virgolette rifiutato', 'solo di cifre' in t and riga('Bob') is None, t)

        c, t = chiedi(GESTORE, '/codici', citato='testo del messaggio citato')
        V('messaggio con citazione: usa il testo vero (/codici)', 'Codici sulla placca (3)' in t, t)
        V('  elenco: Mario con usi, Anna con date, Casa senza scadenza',
          '1. Mario - PIN' in t and 'usi rimasti 2' in t and '2. Anna' in t and 'dal 2026-12-24 al 2026-12-26' in t
          and '3. Casa - PIN 4821' in t, t)
        c, t = chiedi(GESTORE, '/stato', spazi=True)
        V('JSON con spazi: capito lo stesso (/stato)', 'raggiungibile, 3 codici' in t, t)

        c, t = chiedi(GESTORE, '/elimina 2')
        V('/elimina 2 (Anna)', 'Eliminato il codice di Anna' in t and riga('Anna') is None and riga('Mario') and riga('Casa'), t)
        c, t = chiedi(GESTORE, '/elimina casa')
        V('/elimina per nome (maiuscole indifferenti)', 'Eliminato il codice di Casa' in t and riga('Casa') is None, t)
        c, t = chiedi(GESTORE, '/elimina 9')
        V('/elimina numero inesistente', "Non c'e' il codice numero 9" in t and len(PL.righe) == 1, t)
        c, t = chiedi(GESTORE, '/boh')
        V('comando sconosciuto: aiuto', 'Comando sconosciuto' in t and '/ospite' in t, t)

        V('sessione scaduta piu volte: il bot e rientrato da solo', PL.login >= 2, 'login=%d' % PL.login)
        try:
            with open(os.path.join(d, 'offset')) as f:
                off = int(f.read())
        except (OSError, ValueError):
            off = -1
        V('offset salvato (niente messaggi rieseguiti dopo un riavvio)', off == 5000 + TGS.prossimo, str(off))
        V('nessuna password in chiaro nel config letto', PASSWORD_PLACCA not in open(os.path.join(d, 'config')).read())
    finally:
        bot.terminate()
        try:
            bot.wait(5)
        except subprocess.TimeoutExpired:
            bot.kill()
        shutil.rmtree(d, ignore_errors=True)
    print('\n%d/%d' % (ok, tot))
    if ok != tot:
        print('--- uscita del bot (ultime righe) ---')
        print(''.join(uscita[-30:]))
    sys.exit(0 if ok == tot else 1)

if __name__ == '__main__':
    main()
