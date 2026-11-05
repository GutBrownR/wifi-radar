"""WiFi Radar - escaner de red local + servidor web.

Solo usa la libreria estandar de Python. Escanea tu subred /24 (ping + tabla ARP)
y sirve el radar 3D en http://localhost:8000. Solo escucha en 127.0.0.1.
"""
import ipaddress
import json
import os
import re
import socket
import struct
import subprocess
import sys
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from history import History
from motion import MotionDetector

BASE = os.path.dirname(os.path.abspath(__file__))
WEB = os.path.join(BASE, "web")
NAMES_FILE = os.path.join(BASE, "names.json")
KNOWN_FILE = os.path.join(BASE, "known.json")  # equipos en los que confías
DEMO = "--demo" in sys.argv  # datos inventados: no escanea ni toca history.db (ver demo.py)
PORT = 8766 if DEMO else 8765
SCAN_EVERY = 5       # segundos entre escaneos
ONLINE_WINDOW = 40   # segundos sin responder ping para pasar a "sin respuesta"
NO_WINDOW = 0x08000000  # CREATE_NO_WINDOW (Windows)

# Fabricantes que identifico con certeza (el resto se muestra como desconocido).
OUI = {
    "B8:27:EB": "Raspberry Pi",
    "DC:A6:32": "Raspberry Pi",
    "E4:5F:01": "Raspberry Pi",
    "00:50:56": "VMware",
    "08:00:27": "VirtualBox",
}

lock = threading.Lock()
radar_on = threading.Event()  # interruptor del radar: apagado = ningun escaneo corre
radar_on.set()
devices = {}  # mac -> dict
state = {"scanned_at": 0, "scanning": False, "gateway": None, "my_ip": None, "subnet": None}
hostname_tried = {}
probe_tried = {}
mdns_tried = {}
names = {}
history = None if DEMO else History(os.path.join(BASE, "history.db"))  # quien estuvo en la red y cuando
motion = MotionDetector(on_event=lambda kind, text, ts: history.add_event(kind, text, ts))


def run(cmd):
    return subprocess.run(
        cmd, capture_output=True, text=True, errors="ignore", creationflags=NO_WINDOW
    ).stdout


def load_names():
    global names
    try:
        with open(NAMES_FILE, encoding="utf-8") as f:
            names = {k.lower(): v for k, v in json.load(f).items()}
    except (OSError, ValueError):
        names = {}


known = None  # None = aún no hay lista; la primera lectura de tu red se toma como base


def load_known():
    global known
    try:
        with open(KNOWN_FILE, encoding="utf-8") as f:
            known = {m.lower() for m in json.load(f)}
    except (OSError, ValueError):
        known = None


def save_known():
    with open(KNOWN_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(known or []), f, indent=2)


def ensure_baseline():
    """Primera vez: los equipos que ya están conectados se marcan como conocidos."""
    global known
    if known is None and devices:
        known = set(devices)
        save_known()


def save_names():
    with open(NAMES_FILE, "w", encoding="utf-8") as f:
        json.dump(names, f, ensure_ascii=False, indent=2)


def find_gateway():
    best = None
    for line in run(["route", "print", "-4", "0.0.0.0"]).splitlines():
        p = line.split()
        if len(p) >= 5 and p[0] == "0.0.0.0" and p[1] == "0.0.0.0":
            try:
                metric = int(p[4])
            except ValueError:
                metric = 9999
            if best is None or metric < best[0]:
                best = (metric, p[2])
    return best[1] if best else None


def local_ip_for(gateway):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((gateway, 1))  # UDP: no envia trafico, solo elige la interfaz
        return s.getsockname()[0]
    finally:
        s.close()


def ping(ip):
    out = run(["ping", "-n", "1", "-w", "400", ip])
    if "TTL=" not in out.upper():
        return None
    m = re.search(r"(?:tiempo|time)\s*([=<])\s*(\d+)", out, re.I)
    if not m:
        return 1.0
    return float(m.group(2)) if m.group(1) == "=" else 0.5


def read_arp(net):
    found = {}
    pat = re.compile(r"\s*(\d+\.\d+\.\d+\.\d+)\s+([0-9a-f]{2}(?:-[0-9a-f]{2}){5})\s+(\S+)", re.I)
    for line in run(["arp", "-a"]).splitlines():
        m = pat.match(line)
        if not m:
            continue
        ip, mac, typ = m.groups()
        mac = mac.replace("-", ":").lower()
        addr = ipaddress.ip_address(ip)
        if addr not in net or addr.is_multicast or ip.endswith(".255"):
            continue
        if mac == "ff:ff:ff:ff:ff:ff" or re.match(r"est|stat", typ, re.I):
            continue
        found[ip] = mac
    return found


def activity_of(hist):
    """Pista de si el equipo está despierto, según cómo responde al ping.

    No mide datos: un celular en reposo ahorra batería y contesta lento o a veces no contesta;
    uno en uso contesta rápido y seguido.
    """
    last = list(hist)[-8:]
    if len(last) < 3:
        return "midiendo"
    replies = sorted(x for x in last if x is not None)
    if not replies:
        return "dormido"
    loss = 1 - len(replies) / len(last)
    median = replies[len(replies) // 2]
    return "activo" if (median <= 20 and loss <= 0.2) else "reposo"


def vendor_of(mac):
    first = int(mac[:2], 16)
    if first & 2:
        return "MAC privada (celular/tablet moderno)"
    return OUI.get(mac[:8].upper(), "Fabricante desconocido")


def resolve_hostname(mac, ip):
    try:
        host = socket.gethostbyaddr(ip)[0]
    except OSError:
        host = None
    with lock:
        if mac in devices:
            devices[mac]["hostname"] = host


# ---------- nombres Bonjour (mDNS) ----------
# iPhone, iPad, Mac, Chromecast, impresoras... anuncian su propio nombre en la red local.
# Aquí solo se les pregunta "¿cómo te llamas?" por el puerto público de mDNS; no se entra al equipo.
def _read_name(data, pos):
    labels, jumped, end = [], False, pos
    for _ in range(64):  # tope contra paquetes mal formados
        n = data[pos]
        if n == 0:
            pos += 1
            break
        if n & 0xC0 == 0xC0:  # puntero de compresión
            ptr = ((n & 0x3F) << 8) | data[pos + 1]
            if not jumped:
                end = pos + 2
            pos, jumped = ptr, True
            continue
        labels.append(data[pos + 1:pos + 1 + n].decode("utf-8", "ignore"))
        pos += 1 + n
    else:
        raise ValueError("nombre demasiado largo")
    return ".".join(labels), (end if jumped else pos)


def parse_mdns_ptr(data):
    qd, an = struct.unpack(">HH", data[4:8])
    pos = 12
    for _ in range(qd):
        _, pos = _read_name(data, pos)
        pos += 4
    for _ in range(an):
        _, pos = _read_name(data, pos)
        rtype, _, _, rdlen = struct.unpack(">HHIH", data[pos:pos + 10])
        pos += 10
        if rtype == 12:
            return _read_name(data, pos)[0]
        pos += rdlen
    return None


def clean_mdns_name(raw):
    name = re.sub(r"\.local\.?$", "", raw or "", flags=re.I)
    name = re.sub(r"\\(\d{3})", lambda m: chr(int(m.group(1))), name)
    name = re.sub(r"[-_]+", " ", name).strip()
    return name[:40] or None


def mdns_name(ip):
    rev = ".".join(reversed(ip.split("."))) + ".in-addr.arpa"
    q = struct.pack(">HHHHHH", 0, 0, 1, 0, 0, 0)
    for label in rev.split("."):
        q += bytes([len(label)]) + label.encode()
    q += b"\x00" + struct.pack(">HH", 12, 0x8001)  # PTR, clase IN, respuesta directa
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(1.2)
    try:
        s.sendto(q, (ip, 5353))
        data, _ = s.recvfrom(2048)
        return clean_mdns_name(parse_mdns_ptr(data))
    except (OSError, ValueError, IndexError, struct.error):
        return None
    finally:
        s.close()


def discover_name(mac, ip):
    name = mdns_name(ip)
    with lock:
        if mac in devices:
            devices[mac]["mdns"] = name


# Puertos que delatan el tipo de equipo (pista, no certeza).
PROBE_PORTS = (62078, 8009, 445, 22, 80)


def guess_kind(ports):
    if 62078 in ports:
        return "iPhone / iPad"
    if 8009 in ports:
        return "Chromecast / TV / parlante"
    if 445 in ports:
        return "PC Windows / NAS"
    if 22 in ports:
        return "Linux / servidor"
    return None


def probe_ports(mac, ip):
    ports = []
    for p in PROBE_PORTS:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(0.6)
        try:
            if s.connect_ex((ip, p)) == 0:
                ports.append(p)
        except OSError:
            pass
        finally:
            s.close()
    with lock:
        if mac in devices:
            devices[mac]["kind"] = guess_kind(ports)
            devices[mac]["ports"] = ports


def scan_once(pool):
    gw = find_gateway()
    if not gw:
        return
    my_ip = local_ip_for(gw)
    net = ipaddress.ip_network(f"{my_ip}/24", strict=False)
    hosts = [str(h) for h in net.hosts()]
    with lock:
        state.update(gateway=gw, my_ip=my_ip, subnet=str(net), scanning=True)
    with ThreadPoolExecutor(64) as ex:
        rtts = dict(zip(hosts, ex.map(ping, hosts)))
    time.sleep(0.3)
    arp = read_arp(net)
    now = time.time()
    with lock:
        present = set()
        for ip, mac in arp.items():
            present.add(mac)
            d = devices.setdefault(
                mac, {"mac": mac, "first_seen": now, "rtt": None, "last_reply": 0, "hostname": None,
                      "hist": deque(maxlen=10), "mdns": None}
            )
            d["ip"] = ip
            d["last_seen"] = now
            d["hist"].append(rtts.get(ip))  # None = no respondió este ping
            if rtts.get(ip) is not None:
                d["rtt"] = rtts[ip]
                d["last_reply"] = now
            if d["hostname"] is None and now - hostname_tried.get(mac, 0) > 300:
                hostname_tried[mac] = now
                pool.submit(resolve_hostname, mac, ip)
            if d.get("mdns") is None and ip != my_ip and now - mdns_tried.get(mac, 0) > 120:
                mdns_tried[mac] = now
                pool.submit(discover_name, mac, ip)
            if ip != my_ip and ip != gw and now - probe_tried.get(mac, 0) > 300:
                probe_tried[mac] = now
                pool.submit(probe_ports, mac, ip)
        for mac in list(devices):
            if mac not in present:
                del devices[mac]
        ensure_baseline()
        state.update(scanned_at=now, scanning=False)


def scan_loop():
    pool = ThreadPoolExecutor(8)
    while True:
        radar_on.wait()
        try:
            scan_once(pool)
            seen = {d["mac"]: d["name"] for d in snapshot()["devices"] if d["mac"] != "self"}
            history.update(seen)
        except Exception as e:  # el escaner nunca debe morir
            print("Error de escaneo:", e)
        time.sleep(SCAN_EVERY)


# ---------- WiFis cercanos (los ve el adaptador WiFi de tu laptop) ----------
wifi_state = {"aps": [], "scanned_at": 0}


def parse_wifi(out):
    aps, ssid, cur, auth, cipher = [], None, None, None, None
    for line in out.splitlines():
        m = re.match(r"\s*SSID\s+\d+\s*:\s*(.*)$", line)
        if m:
            ssid = m.group(1).strip()
            cur, auth, cipher = None, None, None
            continue
        if cur is None:  # estas líneas vienen antes de los BSSID de cada SSID
            m = re.match(r"\s*(?:Autenticaci.{1,3}n|Authentication)\s*:\s*(.+?)\s*$", line, re.I)
            if m:
                auth = re.sub(r"[^\x20-\x7e]+", " ", m.group(1))
                continue
            m = re.match(r"\s*(?:Cifrado|Encryption)\s*:\s*(.+?)\s*$", line, re.I)
            if m:
                cipher = re.sub(r"[^\x20-\x7e]+", " ", m.group(1))
                continue
        m = re.match(r"\s*BSSID\s+\d+\s*:\s*([0-9a-f]{2}(?::[0-9a-f]{2}){5})", line, re.I)
        if m:
            cur = {"ssid": ssid, "bssid": m.group(1).lower(), "signal": 0, "channel": None,
                   "band": None, "radio": None, "stations": None, "utilization": None,
                   "auth": auth, "cipher": cipher}
            aps.append(cur)
            continue
        if cur is None:
            continue
        for key, pat in (
            ("signal", r"\s*(?:Se.{1,3}al|Signal)\s*:\s*(\d+)"),
            ("channel", r"\s*(?:Canal|Channel)\s*:\s*(\d+)"),
            ("band", r"\s*(?:Banda|Band)\s*:\s*(.+?)\s*$"),
            ("radio", r"\s*(?:Tipo de radio|Radio type)\s*:\s*(.+?)\s*$"),
            ("stations", r"\s*(?:Estaciones conectadas|Station Count)\s*:\s*(\d+)"),
            ("utilization", r"\s*(?:Uso del canal|Channel Utilization)\s*:\s*\d+\s*\((\d+)"),
        ):
            mm = re.match(pat, line, re.I)
            if mm:
                v = mm.group(1)
                cur[key] = int(v) if key in ("signal", "channel", "stations", "utilization") else re.sub(r"[^\x20-\x7e]+", " ", v)
                break
    return aps


def estimate_distance(signal, band):
    """Distancia aproximada (m) desde la señal. Modelo de interiores: muy impreciso."""
    rssi = signal / 2 - 100  # conversión habitual de Windows: % -> dBm
    ref = -47 if (band or "").startswith(("5", "6")) else -40  # potencia a 1 m
    d = 10 ** ((ref - rssi) / (10 * 3.0))
    return round(min(max(d, 1.0), 80.0), 1), round(rssi)


def wifi_loop():
    while True:
        radar_on.wait()
        try:
            aps = parse_wifi(run(["netsh", "wlan", "show", "networks", "mode=bssid"]))
            motion.feed(aps, time.time())
            with lock:
                wifi_state["aps"] = aps
                wifi_state["scanned_at"] = time.time()
        except Exception as e:
            print("Error WiFi:", e)
        time.sleep(8)


# ---------- Bluetooth ----------
# Windows solo deja leer, sin programas extra, los equipos Bluetooth emparejados con esta laptop
# y si estan conectados ahora. No escanea los que andan sueltos por el aire.
bt_state = {"devices": [], "scanned_at": 0, "adapter": None}

BT_PS = r"""
$all = @(Get-PnpDevice -Class Bluetooth -ErrorAction SilentlyContinue)
$a = $all | Where-Object { $_.FriendlyName -match 'Wireless Bluetooth|Bluetooth.*Adapter' -and $_.InstanceId -match '^(USB|PCI)' } | Select-Object -First 1
$out = @()
$all | Where-Object { $_.InstanceId -match '^(BTHENUM|BTHLE)\\DEV_([0-9A-F]{12})' } | ForEach-Object {
  $null = $_.InstanceId -match 'DEV_([0-9A-F]{12})'
  $mac = $Matches[1]
  $c = (Get-PnpDeviceProperty -InstanceId $_.InstanceId -KeyName '{83DA6326-97A6-4088-9453-A1923F573B29} 15' -ErrorAction SilentlyContinue).Data
  $out += [pscustomobject]@{ name = $_.FriendlyName; mac = $mac; connected = [bool]$c; le = ($_.InstanceId -like 'BTHLE*') }
}
[pscustomobject]@{ adapter = $a.FriendlyName; devices = $out } | ConvertTo-Json -Compress -Depth 4
"""


def bt_kind(name):
    n = name.lower()
    if re.search(r"iphone|phone|galaxy|pixel|xiaomi|redmi", n):
        return "phone"
    if re.search(r"ipad|tablet", n):
        return "tablet"
    if re.search(r"mouse|m\d{3}|mx |logi", n):
        return "mouse"
    if re.search(r"kwh|headphone|headset|buds|airpods|audio|speaker|parlante|jbl|soundcore|wh-|wf-", n):
        return "audio"
    if re.search(r"keyboard|teclado|k\d{3}", n):
        return "keyboard"
    if re.search(r"watch|band|reloj", n):
        return "watch"
    return "other"


def bt_loop():
    while True:
        radar_on.wait()
        try:
            raw = run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", BT_PS]).strip()
            data = json.loads(raw) if raw else {}
            items = data.get("devices") or []
            if isinstance(items, dict):
                items = [items]
            merged = {}  # un mismo equipo sale dos veces (clasico y LE): lo junto por nombre
            for it in items:
                name = (it.get("name") or "").strip() or "Bluetooth sin nombre"
                m = merged.setdefault(name, {"name": name, "macs": [], "connected": False})
                m["macs"].append(it["mac"].lower())
                m["connected"] = m["connected"] or bool(it.get("connected"))
            out = []
            for m in merged.values():
                out.append({**m, "mac": m["macs"][0], "kind": bt_kind(m["name"])})
            with lock:
                bt_state["devices"] = out
                bt_state["adapter"] = data.get("adapter")
                bt_state["scanned_at"] = time.time()
        except Exception as e:
            print("Error Bluetooth:", e)
        time.sleep(5)


BLE_DIR = os.path.join(BASE, "ble")
BLE_EXE = os.path.join(BLE_DIR, "BleScan.exe")
BLE_SRC = os.path.join(BLE_DIR, "BleScan.cs")
# Fabricantes Bluetooth (ID oficial de la empresa) que reconozco con certeza.
BLE_COMPANY = {6: "Microsoft", 76: "Apple", 117: "Samsung", 224: "Google", 87: "Garmin", 301: "Sony",
               637: "Xiaomi", 343: "Huawei", 741: "Logitech", 10: "Qualcomm", 13: "Texas Instruments"}
ble_state = {"items": [], "scanned_at": 0, "error": None}


def ensure_ble_exe():
    """Compila el mini escaner con el compilador de .NET que ya trae Windows (una sola vez)."""
    if os.path.exists(BLE_EXE) or not os.path.exists(BLE_SRC):
        return os.path.exists(BLE_EXE)
    fx = r"C:\Windows\Microsoft.NET\Framework64\v4.0.30319"
    meta = r"C:\Windows\System32\WinMetadata"
    interop = r"C:\Windows\Microsoft.NET\assembly\GAC_MSIL\System.Runtime.InteropServices.WindowsRuntime\v4.0_4.0.0.0__b03f5f7f11d50a3a\System.Runtime.InteropServices.WindowsRuntime.dll"
    refs = [f"{meta}\\Windows.Devices.winmd", f"{meta}\\Windows.Foundation.winmd", f"{meta}\\Windows.Storage.winmd",
            f"{fx}\\System.Runtime.WindowsRuntime.dll", f"{fx}\\System.Runtime.dll", interop]
    run([f"{fx}\\csc.exe", "/nologo", "/target:exe", f"/out:{BLE_EXE}"] + [f"/r:{r}" for r in refs] + [BLE_SRC])
    return os.path.exists(BLE_EXE)


def ble_loop():
    """Escucha los anuncios Bluetooth LE del aire (pasivo: no se conecta a nada)."""
    if not ensure_ble_exe():
        ble_state["error"] = "no se pudo preparar el escáner Bluetooth"
        return
    while True:
        radar_on.wait()
        try:
            raw = run([BLE_EXE, "6"]).strip()
            items = json.loads(raw) if raw else []
            now = time.time()
            for it in items:
                rssi = it["rssi"]
                it["dist_m"] = round(min(max(10 ** ((-59 - rssi) / 25.0), 0.3), 30.0), 1)  # modelo de interiores, muy aproximado
                it["maker"] = BLE_COMPANY.get(it["company"])
            items.sort(key=lambda x: -x["rssi"])
            with lock:
                ble_state.update(items=items, scanned_at=now, error=None)
        except Exception as e:
            ble_state["error"] = str(e)
            print("Error BLE:", e)
        time.sleep(3)


def bt_snapshot():
    with lock:
        devs = sorted(bt_state["devices"], key=lambda d: (not d["connected"], d["name"].lower()))
        return {"scanned_at": bt_state["scanned_at"], "adapter": bt_state["adapter"], "devices": devs,
                "nearby": ble_state["items"], "nearby_at": ble_state["scanned_at"], "nearby_error": ble_state["error"]}


def security_of(auth):
    """(etiqueta, nivel de riesgo) según el tipo de autenticación que anuncia la red."""
    a = (auth or "").lower()
    if not a:
        return "desconocida", "info"
    if "abiert" in a or "open" in a:
        return "SIN CLAVE", "alta"
    if "wep" in a:
        return "WEP (rota)", "alta"
    if "wpa3" in a:
        return "WPA3", "ok"
    if "wpa2" in a:
        return "WPA2", "ok"
    if "wpa" in a:
        return "WPA (antigua)", "media"
    return auth, "info"


def band_key(ap):
    b = (ap.get("band") or "").strip()
    return "2.4" if b.startswith("2") else "5" if b.startswith("5") else "6" if b.startswith("6") else None


def wifi_alerts(aps):
    alerts = []
    own = [a for a in aps if a["own"]]
    own_ssids = {a["ssid"] for a in own if a["ssid"]}
    for a in own:
        if a["risk"] in ("alta", "media"):
            alerts.append({"level": "alta", "title": f"Tu router usa seguridad {a['security']}",
                           "text": "Cámbiala a WPA2 o WPA3 en el panel del router."})
    for a in aps:
        if not a["own"] and a["ssid"] in own_ssids:
            alerts.append({"level": "alta", "title": f"Posible gemelo malvado: «{a['ssid']}»",
                           "text": f"Hay otra red con el nombre de la tuya, pero no es tu router "
                                   f"(señal {a['signal']}%, canal {a['channel']}). No te conectes a ella."})
    by_ssid = {}
    for a in aps:
        if a["ssid"]:
            by_ssid.setdefault(a["ssid"], []).append(a)
    for ssid, group in by_ssid.items():
        if len({g["security"] for g in group}) > 1 and not any(g["own"] for g in group):
            alerts.append({"level": "media", "title": f"«{ssid}» aparece con seguridad distinta",
                           "text": "El mismo nombre de red anuncia tipos de seguridad diferentes. Puede ser un "
                                   "router mal configurado o una red falsa."})
    weak = [a for a in aps if not a["own"] and a["risk"] == "alta"]
    if weak:
        names = ", ".join(sorted({(a["ssid"] or "(oculta)") + f" [{a['security']}]" for a in weak}))
        alerts.append({"level": "info", "title": f"{len(weak)} red(es) cercana(s) sin seguridad adecuada",
                       "text": f"{names}. Evita conectarte a ellas."})
    return alerts


def channel_advice(aps):
    """Compara la saturación de cada canal con los vecinos y sugiere uno mejor para TU router."""
    out = []
    for bk, label, cands in (("2.4", "2,4 GHz", [1, 6, 11]), ("5", "5 GHz", [36, 40, 44, 48, 149, 153, 157, 161])):
        mine = [a for a in aps if a["own"] and band_key(a) == bk and a["channel"]]
        if not mine:
            continue
        cur = mine[0]["channel"]
        others = [a for a in aps if not a["own"] and band_key(a) == bk and a["channel"]]

        def score(c):
            s = 0.0
            for o in others:
                dc = abs(o["channel"] - c)
                w = max(0.0, 1 - dc / 5) if bk == "2.4" else (1.0 if dc == 0 else 0.5 if dc <= 4 else 0.0)
                s += o["signal"] * w
            return round(s)

        cur_score = score(cur)
        best_score, best = min((score(c), c) for c in cands)
        change = best != cur and best_score <= cur_score * 0.6 and cur_score - best_score >= 10
        text = (f"Cambia tu router al canal {best} (interferencia {best_score} en vez de {cur_score}). "
                "Se hace en el panel del router, sección WiFi."
                if change else f"Tu canal {cur} está bien: casi no hay interferencia de vecinos ({cur_score}).")
        out.append({"band": label, "current": cur, "current_score": cur_score, "best": best,
                    "best_score": best_score, "change": change, "text": text})
    return out


def wifi_snapshot():
    mo = motion.state()
    with lock:
        gw_mac = next((m for m, d in devices.items() if d["ip"] == state["gateway"]), None)
        out = []
        for a in wifi_state["aps"]:
            dist, rssi = estimate_distance(a["signal"], a["band"])
            # Tu router publica sus WiFis con MACs casi iguales a la suya.
            own = bool(gw_mac) and a["bssid"][3:14] == gw_mac[3:14]
            sec, risk = security_of(a.get("auth"))
            out.append({**a, "ssid": a["ssid"] or "", "dist_m": dist, "rssi_dbm": rssi, "own": own,
                        "security": sec, "risk": risk, "label": names.get(a["bssid"].lower()),
                        "delta_db": mo["delta"].get(a["bssid"]), "moving": a["bssid"] in mo["moving"]})
        out.sort(key=lambda x: -x["signal"])
        return {"scanned_at": wifi_state["scanned_at"], "aps": out,
                "alerts": wifi_alerts(out), "advice": channel_advice(out)}


def snapshot():
    now = time.time()
    with lock:
        out = []
        for mac, d in devices.items():
            is_gw = d["ip"] == state["gateway"]
            is_self = d["ip"] == state["my_ip"]
            online = now - d["last_reply"] <= ONLINE_WINDOW
            kind = "Router" if is_gw else d.get("kind")
            name = names.get(mac) or d.get("mdns") or d["hostname"]
            if not name:
                name = f"{kind or 'Dispositivo'} .{d['ip'].split('.')[-1]}"
            out.append(
                {
                    "mac": mac,
                    "ip": d["ip"],
                    "name": name,
                    "custom_name": mac in names,
                    "vendor": vendor_of(mac),
                    "kind": kind,
                    "known": is_gw or is_self or known is None or mac in known,
                    "activity": "activo" if (is_gw or is_self) else activity_of(d["hist"]),
                    "rtt": d["rtt"],
                    "online": online,
                    "is_gateway": is_gw,
                    "is_self": is_self,
                    "first_seen": d["first_seen"],
                }
            )
        # La laptop no aparece en su propia tabla ARP: la agrego a mano.
        if state["my_ip"] and not any(x["is_self"] for x in out):
            out.append(
                {
                    "mac": "self",
                    "ip": state["my_ip"],
                    "name": "Esta laptop",
                    "custom_name": False,
                    "vendor": "Tu equipo",
                    "known": True,
                    "activity": "activo",
                    "rtt": 0.5,
                    "online": True,
                    "is_gateway": False,
                    "is_self": True,
                    "first_seen": 0,
                }
            )
        return {
            "gateway": state["gateway"],
            "subnet": state["subnet"],
            "scanned_at": state["scanned_at"],
            "scanning": state["scanning"],
            "devices": sorted(out, key=lambda x: tuple(int(p) for p in x["ip"].split("."))),
        }


# Solo se atiende a la propia pagina del radar. Una pagina ajena abierta en tu navegador tambien sale de tu
# laptop, pero trae otro "origen": por eso se mira de que pagina viene el pedido, no solo de que equipo.
ALLOWED_HOSTS = {f"radar.localhost:{PORT}", f"localhost:{PORT}", f"127.0.0.1:{PORT}"}
ALLOWED_ORIGINS = {f"http://{h}" for h in ALLOWED_HOSTS}


class Handler(SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _blocked(self, is_write):
        """Motivo por el que se rechaza el pedido, o None si viene de la pagina del radar."""
        if self.headers.get("Host", "") not in ALLOWED_HOSTS:
            return "direccion no permitida"  # frena el truco "DNS rebinding"
        if self.headers.get("Sec-Fetch-Site") == "cross-site":
            return "viene de otra pagina"
        origin = self.headers.get("Origin")
        if origin is not None and origin not in ALLOWED_ORIGINS:
            return "origen no permitido"
        if is_write and self.headers.get("Content-Type", "").split(";")[0].strip().lower() != "application/json":
            return "formato no permitido"  # una pagina ajena no puede mandar JSON sin pedir permiso antes
        return None

    def do_GET(self):
        if self._blocked(False):
            return self._json({"error": "forbidden"}, 403)
        return self._get()

    def do_POST(self):
        if self._blocked(True):
            return self._json({"error": "forbidden"}, 403)
        return self._post()

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _get(self):
        if DEMO and self.path.startswith("/api/"):
            import demo
            route = self.path.split("?")[0]
            if route == "/api/devices":
                return self._json(demo.devices())
            if route == "/api/wifi":
                return self._json(demo.wifi())
            if route == "/api/bluetooth":
                return self._json(demo.bluetooth())
            if route == "/api/history":
                try:
                    day = int(parse_qs(urlparse(self.path).query).get("day", ["0"])[0])
                except ValueError:
                    day = 0
                return self._json(demo.history(max(0, min(day, 60))))
            return self._json({"radar_on": True})
        extra = {"radar_on": radar_on.is_set(), "motion": motion.state()}
        if self.path.startswith("/api/devices"):
            return self._json({**snapshot(), **extra})
        if self.path.startswith("/api/wifi"):
            return self._json({**wifi_snapshot(), **extra})
        if self.path.startswith("/api/bluetooth"):
            return self._json({**bt_snapshot(), **extra})
        if self.path.startswith("/api/power"):
            return self._json({"radar_on": extra["radar_on"]})
        if self.path.startswith("/api/history"):
            try:
                day = int(parse_qs(urlparse(self.path).query).get("day", ["0"])[0])
            except ValueError:
                day = 0
            h = history.day(max(0, min(day, 60)))
            for d in h["devices"]:  # si renombraste el equipo, vale el nombre de ahora
                d["name"] = names.get(d["mac"], d["name"])
            return self._json({**h, **extra})
        return super().do_GET()

    def _post(self):
        global known
        if self.path == "/api/motion/reset":
            motion.reset()
            return self._json({"ok": True})
        if self.path == "/api/power":
            try:
                n = int(self.headers.get("Content-Length", 0))
                on = bool(json.loads(self.rfile.read(n))["on"])
            except (ValueError, KeyError):
                return self._json({"error": "bad request"}, 400)
            radar_on.set() if on else radar_on.clear()
            return self._json({"radar_on": on})
        if self.path == "/api/trust":
            try:
                n = int(self.headers.get("Content-Length", 0))
                data = json.loads(self.rfile.read(n))
            except ValueError:
                return self._json({"error": "bad request"}, 400)
            with lock:
                if known is None:
                    known = set()
                if data.get("all"):
                    known |= set(devices)
                elif data.get("mac"):
                    known.add(str(data["mac"]).lower())
                save_known()
            return self._json({"ok": True})
        if self.path != "/api/rename":
            return self._json({"error": "not found"}, 404)
        try:
            n = int(self.headers.get("Content-Length", 0))
            data = json.loads(self.rfile.read(n))
            mac = str(data["mac"]).lower()
            name = str(data.get("name", "")).strip()[:40]
        except (ValueError, KeyError):
            return self._json({"error": "bad request"}, 400)
        if mac == "self":
            return self._json({"error": "no se puede renombrar"}, 400)
        with lock:
            if name:
                names[mac] = name
            else:
                names.pop(mac, None)
            save_names()
        self._json({"ok": True})


def main():
    if not DEMO:
        load_names()
        load_known()
        threading.Thread(target=scan_loop, daemon=True).start()
        threading.Thread(target=wifi_loop, daemon=True).start()
        threading.Thread(target=bt_loop, daemon=True).start()
        threading.Thread(target=ble_loop, daemon=True).start()
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), partial(Handler, directory=WEB))
    print(f"WiFi Radar listo{' (MODO DEMO, datos inventados)' if DEMO else ''} -> http://radar.localhost:{PORT}  (Ctrl+C para salir)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
