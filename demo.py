"""Modo demo: datos INVENTADOS para capturas y pruebas. No escanea nada ni toca tu historial real.

Uso:  python server.py --demo      (abre http://radar.localhost:8766)
"""
import time

from history import day_bounds

NOW = time.time


def _extra():
    return {"radar_on": True, "motion": {"status": "calma", "progress": 8, "total": 8, "watching": 6,
                                          "moving": [], "delta": {}, "since": None, "last_event": None}}


def devices():
    t = NOW()
    rows = [  # ip, mac, nombre, tipo, actividad, rtt
        ("192.168.1.1", "02:00:00:00:00:01", "Router de la casa", "Router", "activo", 2, True, False),
        ("192.168.1.12", "02:00:00:00:00:12", "Celular de Ana", "iPhone / iPad", "activo", 14, False, False),
        ("192.168.1.15", "02:00:00:00:00:15", "Tablet de Luis", "iPhone / iPad", "reposo", 38, False, False),
        ("192.168.1.20", "02:00:00:00:00:20", "Smart TV sala", "Chromecast / TV / parlante", "reposo", 22, False, False),
        ("192.168.1.24", "02:00:00:00:00:24", "PC del estudio", "PC Windows / NAS", "activo", 6, False, False),
        ("192.168.1.31", "self", "Esta laptop", None, "activo", 0.5, False, True),
        ("192.168.1.40", "02:00:00:00:00:40", "Equipo desconocido", None, "activo", 18, False, False),
    ]
    out = []
    for ip, mac, name, kind, act, rtt, gw, me in rows:
        out.append({"mac": mac, "ip": ip, "name": name, "custom_name": not gw and not me, "vendor": "Demo",
                    "kind": kind, "known": mac != "02:00:00:00:00:40", "activity": act, "rtt": rtt,
                    "online": True, "is_gateway": gw, "is_self": me, "first_seen": t - 86400})
    return {"gateway": "192.168.1.1", "subnet": "192.168.1.0/24", "scanned_at": t, "scanning": False,
            "devices": out, **_extra()}


def wifi():
    t = NOW()

    def ap(ssid, mac, sig, ch, band, auth, own=False, label=None, st=None, util=None):
        rssi = round(sig / 2 - 100)
        dist = round(min(max(10 ** (((-47 if band.startswith("5") else -40) - rssi) / 30), 1.0), 80.0), 1)
        sec = {"WPA2-Personal": ("WPA2", "baja"), "Open": ("SIN CLAVE", "alta")}[auth]
        return {"ssid": ssid, "bssid": mac, "signal": sig, "channel": ch, "band": band, "radio": "802.11ax",
                "stations": st, "utilization": util, "auth": auth, "cipher": "CCMP", "dist_m": dist,
                "rssi_dbm": rssi, "own": own, "security": sec[0], "risk": sec[1], "label": label,
                "delta_db": None, "moving": False}

    aps = [
        ap("MiCasa_2.4G", "02:aa:00:00:00:01", 82, 6, "2.4 GHz", "WPA2-Personal", own=True, st=6, util=22),
        ap("MiCasa_5G", "02:aa:00:00:00:02", 64, 44, "5 GHz", "WPA2-Personal", own=True, st=3, util=9),
        ap("Dron demo", "02:aa:00:00:00:03", 91, 1, "2.4 GHz", "Open", label="Dron demo"),
        ap("Vecinos_Lopez", "02:aa:00:00:00:04", 58, 6, "2.4 GHz", "WPA2-Personal", util=61),
        ap("FibraNet_4821", "02:aa:00:00:00:05", 47, 1, "2.4 GHz", "WPA2-Personal", util=48),
        ap("Depto_3B", "02:aa:00:00:00:06", 36, 11, "2.4 GHz", "WPA2-Personal"),
        ap("CafeBar_Free", "02:aa:00:00:00:07", 28, 11, "2.4 GHz", "Open"),
        ap("Casa_Rojas_5G", "02:aa:00:00:00:08", 31, 100, "5 GHz", "WPA2-Personal"),
    ]
    alerts = [{"level": "info", "title": "2 red(es) cercana(s) sin seguridad adecuada",
               "text": "Cualquiera puede leer lo que pase por redes abiertas. No te conectes a ellas con datos sensibles."}]
    advice = [{"band": "2.4 GHz", "current": 6, "current_score": 61, "best": 11, "best_score": 28, "change": True,
               "text": "El canal 6 está bastante ocupado (hay 3 redes más). El canal 11 estaría más libre."},
              {"band": "5 GHz", "current": 44, "current_score": 5, "best": 44, "best_score": 5, "change": False,
               "text": "Tu canal de 5 GHz está libre. No hace falta cambiarlo."}]
    return {"scanned_at": t, "aps": aps, "alerts": alerts, "advice": advice, **_extra()}


def bluetooth():
    t = NOW()
    paired = [("Celular de Ana", ["a00000000001"], True, "phone"),
              ("Audífonos Sport", ["a00000000002"], True, "audio"),
              ("Mouse inalámbrico", ["a00000000003"], False, "mouse"),
              ("Reloj de Luis", ["a00000000004"], False, "watch")]
    near = [("5da700000001", "", -48, 76, "Random", 0.4, "Apple"),
            ("5da700000002", "", -62, 117, "Random", 1.6, "Samsung"),
            ("5da700000003", "Parlante cocina", -71, -1, "Public", 3.2, None),
            ("5da700000004", "", -78, 6, "Random", 5.6, "Microsoft"),
            ("5da700000005", "", -84, -1, "Random", 9.0, None)]
    return {"scanned_at": t, "adapter": "Adaptador Bluetooth (demo)",
            "devices": [{"name": n, "macs": m, "connected": c, "mac": m[0], "kind": k} for n, m, c, k in paired],
            "nearby": [{"addr": a, "name": n, "rssi": r, "company": c, "addrType": ty, "dist_m": d, "maker": mk}
                       for a, n, r, c, ty, d, mk in near],
            "nearby_at": t, "nearby_error": None, **_extra()}


def history(offset_days=0):
    start, end = day_bounds(offset_days)
    h = lambda hh, mm=0: start + hh * 3600 + mm * 60  # noqa: E731
    plan = [  # mac, nombre, tramos (hora, min, hora, min)
        ("02:00:00:00:00:01", "Router de la casa", [(0, 0, 19, 0)], True),
        ("02:00:00:00:00:24", "PC del estudio", [(8, 30, 12, 45), (14, 0, 18, 30)], False),
        ("02:00:00:00:00:12", "Celular de Ana", [(0, 0, 7, 40), (13, 10, 19, 0)], True),
        ("02:00:00:00:00:15", "Tablet de Luis", [(15, 20, 17, 5), (18, 0, 19, 0)], True),
        ("02:00:00:00:00:20", "Smart TV sala", [(19, 0, 19, 0)], False),
    ]
    devs = []
    for mac, name, segs, now_here in plan:
        s = [[h(a, b), h(c, d)] for a, b, c, d in segs if h(c, d) > h(a, b)]
        if not s:
            continue
        devs.append({"mac": mac, "name": name, "segments": s, "total": sum(e - b for b, e in s),
                     "present_now": now_here and offset_days == 0, "first": s[0][0], "last": s[-1][1]})
    devs.sort(key=lambda d: -d["total"])
    events = [{"ts": h(12, 50), "kind": "movimiento", "text": "Movimiento detectado (3 redes cambiaron su señal)"},
              {"ts": h(12, 52), "kind": "calma", "text": "Movimiento terminado"}]
    return {"day_start": start, "day_end": end, "now": h(19, 0), "devices": devs, "events": events, **_extra()}
