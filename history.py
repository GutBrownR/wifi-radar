"""Historial de presencia: quien estuvo conectado a tu red, desde cuando y hasta cuando.

Guarda solo: MAC, nombre, hora de llegada y hora en que se dejo de ver. No guarda trafico ni paginas.
Todo queda en history.db (SQLite) dentro de esta carpeta.
"""
import sqlite3
import threading
import time

GRACE = 180  # segundos sin verlo para darlo por ausente (evita cortes por un escaneo fallido)


def day_bounds(offset_days, now=None):
    """(inicio, fin) del dia local, `offset_days` dias antes de hoy."""
    t = time.localtime(now or time.time())
    start = time.mktime((t.tm_year, t.tm_mon, t.tm_mday - offset_days, 0, 0, 0, 0, 0, -1))
    end = time.mktime((t.tm_year, t.tm_mon, t.tm_mday - offset_days + 1, 0, 0, 0, 0, 0, -1))
    return start, end


class History:
    def __init__(self, path):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.lock = threading.Lock()
        self.db.executescript(
            """
            create table if not exists sessions(id integer primary key, mac text, name text,
                                                start real, last_seen real, end real);
            create index if not exists ix_sessions on sessions(mac, start);
            create table if not exists events(id integer primary key, ts real, kind text, text text);
            """
        )
        # Si el radar se cerro con sesiones abiertas, se cierran en la ultima vez que se vieron.
        self.db.execute("update sessions set end = last_seen where end is null")
        self.db.commit()
        self.open = {}   # mac -> id de sesion abierta
        self.last = {}   # mac -> ultima vez visto

    def update(self, seen, now=None):
        """seen: {mac: nombre} de lo que esta conectado ahora mismo."""
        now = now or time.time()
        with self.lock:
            for mac, name in seen.items():
                sid = self.open.get(mac)
                if sid is not None and now - self.last[mac] > GRACE:
                    # el radar estuvo apagado o sin datos: no invento presencia en ese hueco
                    self.db.execute("update sessions set end = ? where id = ?", (self.last[mac], sid))
                    sid = None
                if sid is None:
                    cur = self.db.execute(
                        "insert into sessions(mac, name, start, last_seen) values (?,?,?,?)", (mac, name, now, now))
                    self.open[mac] = cur.lastrowid
                else:
                    self.db.execute("update sessions set last_seen = ?, name = ? where id = ?", (now, name, sid))
                self.last[mac] = now
            for mac, sid in list(self.open.items()):
                if mac not in seen and now - self.last[mac] > GRACE:
                    self.db.execute("update sessions set end = ? where id = ?", (self.last[mac], sid))
                    del self.open[mac]
            self.db.commit()

    def add_event(self, kind, text, ts=None):
        with self.lock:
            self.db.execute("insert into events(ts, kind, text) values (?,?,?)", (ts or time.time(), kind, text))
            self.db.commit()

    def day(self, offset_days):
        now = time.time()
        start, end = day_bounds(offset_days, now)
        end_cap = min(end, now)
        with self.lock:
            rows = self.db.execute(
                "select mac, name, start, coalesce(end, last_seen), end is null from sessions "
                "where start < ? and coalesce(end, last_seen) >= ? order by start", (end, start)).fetchall()
            events = self.db.execute(
                "select ts, kind, text from events where ts >= ? and ts < ? order by ts", (start, end)).fetchall()
        devs = {}
        for mac, name, s, e, is_open in rows:
            s, e = max(s, start), min(max(e, s), end_cap)
            d = devs.setdefault(mac, {"mac": mac, "name": name, "segments": [], "total": 0, "present_now": False})
            d["name"] = name
            d["segments"].append([s, e])
            d["total"] += e - s
            if is_open and offset_days == 0:
                d["present_now"] = True
        out = list(devs.values())
        for d in out:
            d["first"] = d["segments"][0][0]
            d["last"] = d["segments"][-1][1]
        out.sort(key=lambda d: -d["total"])
        return {"day_start": start, "day_end": end, "now": now, "devices": out,
                "events": [{"ts": t, "kind": k, "text": x} for t, k, x in events]}
