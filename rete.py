#!/usr/bin/env python3
"""rete.py - gestione rete/Wi-Fi del Pi dal pannello, tramite NetworkManager (nmcli).

Funzioni usate da server.py:
  stato()              -> dizionario: connessioni attive, IP, hotspot, ecc.
  scansiona()          -> elenco delle reti Wi-Fi viste
  connetti(ssid, pw)   -> prova una rete NUOVA e, se non ha Internet, torna a quella
                          di prima (applica-sicuro, in un processo staccato).
  config_hotspot(ssid, pw) -> crea/aggiorna la rete di soccorso 'citofono-setup'.

Nessun segreto nel repo: le password del Wi-Fi e dell'hotspot le gestisce
NetworkManager (file in /etc/NetworkManager, non in questo repository).
"""
import os
import re
import subprocess

HOTSPOT = 'citofono-setup'
APPLY = '/opt/citofono/wifi-apply.sh'


class ErroreRete(Exception):
    pass


def _nmcli(args, timeout=20, terse=True):
    cmd = ['nmcli']
    if terse:
        cmd += ['-t']
    cmd += args
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        raise ErroreRete('NetworkManager (nmcli) non trovato su questo sistema')
    except subprocess.SubprocessError as e:
        raise ErroreRete('nmcli non risponde: %s' % e)
    return r.returncode, r.stdout, r.stderr


def _righe(out):
    # nmcli -t separa i campi con ':' e protegge i ':' nei valori con '\:'
    for riga in out.splitlines():
        if not riga:
            continue
        campi, cur, esc = [], '', False
        for ch in riga:
            if esc:
                cur += ch
                esc = False
            elif ch == '\\':
                esc = True
            elif ch == ':':
                campi.append(cur)
                cur = ''
            else:
                cur += ch
        campi.append(cur)
        yield campi


def stato():
    d = {'nm': True, 'connesso': False, 'connettivita': '', 'attive': [],
         'wifi_ssid': '', 'wifi_ip': '', 'eth_ip': '', 'hotspot_ssid': '',
         'hotspot_attivo': False, 'guardia': False}
    rc, out, err = _nmcli(['-f', 'NAME,DEVICE,TYPE,STATE', 'connection', 'show', '--active'])
    if rc != 0 and 'not found' in (err or '').lower():
        d['nm'] = False
        return d
    for c in _righe(out):
        if len(c) < 4:
            continue
        nome, dev, tipo, st = c[0], c[1], c[2], c[3]
        d['attive'].append({'nome': nome, 'dev': dev, 'tipo': tipo})
        if nome == HOTSPOT:
            d['hotspot_attivo'] = True
        elif tipo.endswith('wireless') or tipo == '802-11-wireless':
            d['wifi_ssid'] = nome
        if dev and dev.startswith('eth'):
            d['eth_connesso'] = True
    # IP delle interfacce
    for dev, chiave in (('wlan0', 'wifi_ip'), ('eth0', 'eth_ip')):
        rc, out, err = _nmcli(['-f', 'IP4.ADDRESS', 'device', 'show', dev])
        for c in _righe(out):
            if len(c) >= 2 and c[1]:
                d[chiave] = c[1].split('/')[0]
                break
    # connettivita' (full/limited/none)
    rc, out, err = _nmcli(['networking', 'connectivity'])
    d['connettivita'] = out.strip()
    d['connesso'] = d['connettivita'] == 'full'
    # hotspot configurato? e la guardia e' attiva?
    rc, out, err = _nmcli(['-f', 'NAME', 'connection', 'show'])
    nomi = [c[0] for c in _righe(out) if c]
    if HOTSPOT in nomi:
        rc, o2, e2 = _nmcli(['-f', '802-11-wireless.ssid', 'connection', 'show', HOTSPOT])
        for c in _righe(o2):
            if len(c) >= 2:
                d['hotspot_ssid'] = c[1]
    try:
        r = subprocess.run(['systemctl', 'is-enabled', 'rete-hotspot.timer'],
                           capture_output=True, text=True, timeout=10)
        d['guardia'] = r.stdout.strip() == 'enabled'
    except (OSError, subprocess.SubprocessError):
        pass
    return d


def scansiona():
    rc, out, err = _nmcli(['-f', 'IN-USE,SSID,SIGNAL,SECURITY', 'device', 'wifi', 'list',
                           '--rescan', 'auto'], timeout=25)
    if rc != 0:
        raise ErroreRete('scansione Wi-Fi fallita: %s' % (err or '').strip())
    reti, visti = [], set()
    for c in _righe(out):
        if len(c) < 4:
            continue
        inuso, ssid, segnale, sicurezza = c[0], c[1], c[2], c[3]
        if not ssid or ssid in visti:
            continue
        visti.add(ssid)
        reti.append({'ssid': ssid, 'segnale': int(segnale) if segnale.isdigit() else 0,
                     'sicurezza': sicurezza, 'in_uso': inuso.strip() == '*'})
    reti.sort(key=lambda x: -x['segnale'])
    return reti


def connetti(ssid, pw):
    """Applica-sicuro: in un processo staccato prova la nuova rete e, se non ha
    Internet entro ~45s, torna a quella attiva di prima. Ritorna subito."""
    if not ssid:
        raise ErroreRete('nome rete mancante')
    attuale = stato().get('wifi_ssid', '')
    if not os.path.isfile(APPLY):
        raise ErroreRete('script %s mancante' % APPLY)
    subprocess.Popen(['setsid', 'sh', APPLY, ssid, pw or '', attuale],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, close_fds=True)
    return attuale


def config_hotspot(ssid, pw):
    """Crea o aggiorna la rete di soccorso 'citofono-setup' (non si autoconnette:
    la accende la guardia quando il Pi resta isolato)."""
    if not ssid:
        raise ErroreRete('nome hotspot mancante')
    if pw is not None and pw != '' and len(pw) < 8:
        raise ErroreRete('la password dell’hotspot deve avere almeno 8 caratteri')
    rc, out, err = _nmcli(['-f', 'NAME', 'connection', 'show'])
    esiste = HOTSPOT in [c[0] for c in _righe(out) if c]
    if not esiste and not pw:
        raise ErroreRete('serve una password per creare l’hotspot (almeno 8 caratteri)')
    if not esiste:
        rc, o, e = _nmcli(['connection', 'add', 'type', 'wifi', 'ifname', 'wlan0',
                           'con-name', HOTSPOT, 'autoconnect', 'no', 'ssid', ssid], terse=False)
        if rc != 0:
            raise ErroreRete('creazione hotspot fallita: %s' % (e or '').strip())
    mod = ['connection', 'modify', HOTSPOT,
           '802-11-wireless.mode', 'ap', '802-11-wireless.band', 'bg',
           '802-11-wireless.ssid', ssid, 'ipv4.method', 'shared',
           'wifi-sec.key-mgmt', 'wpa-psk']
    if pw:
        mod += ['wifi-sec.psk', pw]
    rc, o, e = _nmcli(mod, terse=False)
    if rc != 0:
        raise ErroreRete('configurazione hotspot fallita: %s' % (e or '').strip())
    return True
