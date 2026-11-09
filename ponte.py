#!/usr/bin/env python3
"""ponte.py - il Raspberry assicura il relay sul monitor, senza telnet a mano.

Entra nel monitor via telnet (credenziali salvate solo sul Pi, in
/etc/citofono/ponte.conf), verifica che il binario del relay sia presente e
avviato, e se manca lo reinstalla e lo lancia. E' idempotente: si puo' eseguire
a ripetizione (dal pulsante del pannello e da un timer systemd).

Uso:
    python3 ponte.py            # assicura il relay, stampa l'esito (per il timer)
    python3 ponte.py --conf /percorso/ponte.conf

Config (/etc/citofono/ponte.conf, permessi 600):
    MON_HOST=192.168.1.22       # IP del monitor (lato casa)
    MON_PORT=9900               # porta telnet del monitor
    MON_USER=root               # utente di manutenzione (dato dal costruttore)
    MON_PASS=...                # password
    RELAY_PORT=8070             # porta su cui il relay ascolta (sul monitor)
    PLACCA_IP=192.168.68.70     # IP della placca (lato 2 fili)
    PLACCA_PORT=80
    RELAY_DEST=/dnake/data/relay        # dove sta il relay (zona scrivibile persistente)
    RELAY_SRC=/opt/citofono/ponte-monitor/relay   # copia sul Pi, per reinstallarlo
"""
import os
import random
import re
import socket
import sys
import threading
import time

IAC, DONT, DO, WONT, WILL, SB, SE = 255, 254, 253, 251, 252, 250, 240

PREDEFINITI = {
    'MON_PORT': '9900', 'MON_USER': 'root', 'RELAY_PORT': '8070',
    'PLACCA_PORT': '80', 'RELAY_DEST': '/dnake/data/relay',
    'RELAY_SRC': '/opt/citofono/ponte-monitor/relay',
}


def leggi_conf(percorso):
    conf = dict(PREDEFINITI)
    try:
        with open(percorso, encoding='utf-8') as f:
            for riga in f:
                riga = riga.strip()
                if riga and not riga.startswith('#') and '=' in riga:
                    k, v = riga.split('=', 1)
                    conf[k.strip()] = v.strip()
    except FileNotFoundError:
        pass
    return conf


class ErrorePonte(Exception):
    pass


class Telnet:
    """Client telnet minimale: rifiuta ogni opzione (IAC), fa login, esegue comandi."""

    def __init__(self, host, port, timeout=20):
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.sock.settimeout(timeout)
        self.timeout = timeout
        self.grezzo = b''      # byte ricevuti non ancora processati (per IAC a cavallo)
        self.testo = b''       # testo pulito accumulato

    # -- gestione IAC: risponde rifiutando tutto, toglie le sequenze dal testo --
    def _processa(self, dati):
        self.grezzo += dati
        out = bytearray()
        i, n = 0, len(self.grezzo)
        while i < n:
            b = self.grezzo[i]
            if b != IAC:
                out.append(b)
                i += 1
                continue
            if i + 1 >= n:
                break                       # IAC a fine buffer: aspetta il resto
            c = self.grezzo[i + 1]
            if c == IAC:
                out.append(IAC)
                i += 2
            elif c in (DO, DONT, WILL, WONT):
                if i + 2 >= n:
                    break
                opt = self.grezzo[i + 2]
                if c == DO:
                    self.sock.sendall(bytes([IAC, WONT, opt]))
                elif c == WILL:
                    self.sock.sendall(bytes([IAC, DONT, opt]))
                elif c == WONT:
                    self.sock.sendall(bytes([IAC, DONT, opt]))
                else:                       # DONT
                    self.sock.sendall(bytes([IAC, WONT, opt]))
                i += 3
            elif c == SB:
                j = i + 2
                while j < n - 1 and not (self.grezzo[j] == IAC and self.grezzo[j + 1] == SE):
                    j += 1
                if j >= n - 1:
                    break
                i = j + 2
            else:
                i += 2                       # altro comando IAC a 2 byte
        self.grezzo = self.grezzo[i:]
        self.testo += bytes(out)

    def leggi_fino(self, token, timeout=None):
        token = token.encode() if isinstance(token, str) else token
        fine = time.time() + (timeout or self.timeout)
        while True:
            if token in self.testo:
                idx = self.testo.index(token) + len(token)
                presa, self.testo = self.testo[:idx], self.testo[idx:]
                return presa
            if time.time() > fine:
                raise ErrorePonte('timeout aspettando %r (ricevuto finora: %r)'
                                  % (token, self.testo[-200:]))
            self.sock.settimeout(max(0.1, fine - time.time()))
            try:
                dati = self.sock.recv(4096)
            except socket.timeout:
                continue
            if not dati:
                raise ErrorePonte('connessione chiusa dal monitor')
            self._processa(dati)

    def invia(self, riga):
        self.sock.sendall((riga + '\n').encode())

    def login(self, utente, password):
        self.leggi_fino('ogin:')             # "login:" / "Login:"
        self.invia(utente)
        self.leggi_fino('assword:')          # "Password:"
        self.invia(password)
        # dopo la password: o un prompt di shell, o un errore
        fine = time.time() + self.timeout
        while time.time() < fine:
            t = self.testo.lower()
            if b'incorrect' in t or b'fail' in t or b'ogin:' in t:
                raise ErrorePonte('login rifiutato dal monitor (credenziali errate?)')
            if b'#' in self.testo or b'$' in self.testo:
                return
            self.sock.settimeout(1)
            try:
                dati = self.sock.recv(4096)
            except socket.timeout:
                continue
            if not dati:
                raise ErrorePonte('connessione chiusa durante il login')
            self._processa(dati)
        raise ErrorePonte('nessun prompt dopo il login')

    def esegui(self, cmd, timeout=None):
        """Esegue cmd, ritorna (uscita, testo). Usa marcatori per isolare l'output."""
        m = '__P%d__' % random.randint(1, 1 << 30)
        self.testo = b''                     # scarta eco/prompt precedenti
        self.invia('echo %s; %s 2>&1; echo %s$?' % (m, cmd, m))
        self.leggi_fino(m, timeout)          # primo marcatore (apertura)
        corpo = self.leggi_fino(m, timeout)  # fino al secondo marcatore
        coda = self.leggi_fino('\n', timeout)
        try:
            uscita = int(re.sub(rb'[^0-9].*$', b'', coda.strip() or b'0') or b'0')
        except ValueError:
            uscita = 0
        testo = corpo[:corpo.rindex(m.encode())] if m.encode() in corpo else corpo
        return uscita, testo.decode('utf-8', 'replace').strip()

    def chiudi(self):
        try:
            self.invia('exit')
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass


def _ip_locale(sock):
    return sock.getsockname()[0]


def _porta_viva(host, port, timeout=3):
    """True se qualcosa accetta connessioni su host:port (cioe' il relay e' su)."""
    try:
        s = socket.create_connection((host, int(port)), timeout=timeout)
        s.close()
        return True
    except OSError:
        return False


def _carica_relay(tn, conf):
    """Manda il binario del relay al monitor via nc (il monitor lo scarica dal Pi)."""
    src = conf['RELAY_SRC']
    if not os.path.isfile(src):
        raise ErrorePonte('binario del relay non trovato sul Pi: %s' % src)
    dati = open(src, 'rb').read()
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(('0.0.0.0', 0))
    srv.listen(1)
    porta = srv.getsockname()[1]
    srv.settimeout(30)
    esito = {}

    def servi():
        try:
            c, _ = srv.accept()
            c.sendall(dati)
            c.shutdown(socket.SHUT_WR)
            c.close()
            esito['ok'] = True
        except OSError as e:
            esito['err'] = str(e)
        finally:
            srv.close()

    t = threading.Thread(target=servi, daemon=True)
    t.start()
    pi = _ip_locale(tn.sock)
    tn.esegui('nc %s %d > %s' % (pi, porta, conf['RELAY_DEST']), timeout=30)
    t.join(timeout=30)
    if not esito.get('ok'):
        raise ErrorePonte('trasferimento del relay fallito: %s' % esito.get('err', 'timeout'))
    tn.esegui('chmod +x %s' % conf['RELAY_DEST'])
    u, out = tn.esegui('wc -c < %s' % conf['RELAY_DEST'])
    n = int(re.sub(r'\D', '', out) or 0)
    if n != len(dati):
        raise ErrorePonte('relay copiato di dimensione sbagliata (%d invece di %d)' % (n, len(dati)))


def assicura_relay(conf):
    """Garantisce che il relay sul monitor sia presente e attivo.
    Ritorna una stringa di esito leggibile."""
    for chiave in ('MON_HOST', 'MON_PASS', 'PLACCA_IP'):
        if not conf.get(chiave):
            raise ErrorePonte('configurazione incompleta: manca %s (impostala nel pannello)' % chiave)
    host, rport = conf['MON_HOST'], conf['RELAY_PORT']
    # 1. gia' attivo? (la porta del relay risponde dal Pi) -> niente da fare
    if _porta_viva(host, rport):
        return 'relay gia attivo: %s:%s risponde' % (host, rport)
    # 2. non risponde: entra nel monitor, assicura il binario, avvialo
    passi = []
    tn = Telnet(host, int(conf['MON_PORT']), timeout=20)
    try:
        tn.login(conf['MON_USER'], conf['MON_PASS'])
        atteso = os.path.getsize(conf['RELAY_SRC']) if os.path.isfile(conf['RELAY_SRC']) else 0
        u, out = tn.esegui('wc -c < %s' % conf['RELAY_DEST'])
        presente = int(re.sub(r'\D', '', out) or 0)
        if presente != atteso or atteso == 0:
            _carica_relay(tn, conf)
            passi.append('relay (re)installato in %s' % conf['RELAY_DEST'])
        else:
            passi.append('relay presente (%d byte)' % presente)
        avvio = 'setsid %s %s %s %s </dev/null >/dev/null 2>&1 &' % (
            conf['RELAY_DEST'], rport, conf['PLACCA_IP'], conf['PLACCA_PORT'])
        tn.esegui(avvio)
        passi.append('relay avviato su :%s -> %s:%s' % (rport, conf['PLACCA_IP'], conf['PLACCA_PORT']))
    finally:
        tn.chiudi()
    # 3. ora la porta risponde?
    for _ in range(6):
        time.sleep(1)
        if _porta_viva(host, rport):
            return '; '.join(passi) + '; %s:%s ora risponde' % (host, rport)
    raise ErrorePonte('relay avviato ma %s:%s non risponde' % (host, rport))


def main():
    conf_path = '/etc/citofono/ponte.conf'
    if len(sys.argv) == 3 and sys.argv[1] == '--conf':
        conf_path = sys.argv[2]
    conf = leggi_conf(conf_path)
    try:
        print(assicura_relay(conf))
    except (ErrorePonte, OSError) as e:
        print('ERRORE: %s' % e, file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()
