"""Detector de movimiento por señal WiFi (idea del proyecto KevinB2212/wifi-radar).

Cuando alguien se mueve entre tu laptop y los routers cercanos, la señal de varias redes cambia un poco.
1. Calibra: mira ~1 minuto cuanto vale la señal de cada red cuando todo esta quieto.
2. Vigila: si VARIAS redes se desvian de lo normal en escaneos seguidos, avisa movimiento.
Es aproximado: detecta "algo se movio", no "una persona esta aqui". Un microondas o una puerta tambien cuentan.
"""
import statistics
import threading

CAL_SCANS = 8          # escaneos para calibrar (a ~8 s cada uno = ~1 minuto)
MIN_PRESENCE = 0.75    # solo se usan redes vistas en el 75% de la calibracion
MIN_SIGNAL = 20        # ignora redes muy debiles: su señal salta sola
BASE_DB = 4.0          # desvio minimo (dB) para contar una red como "movida"
CONFIRM = 2            # escaneos seguidos desviada
QUIET_TO_CLEAR = 3     # escaneos tranquilos para dar el movimiento por terminado
DRIFT = 0.02           # la linea base se ajusta despacio cuando todo esta tranquilo


def _db(signal):
    return signal / 2 - 100  # Windows: % -> dBm aproximado


class MotionDetector:
    def __init__(self, on_event=None):
        self.lock = threading.Lock()
        self.on_event = on_event
        self.reset()

    def reset(self):
        with self.lock:
            self.cal = []          # lista de {bssid: signal}
            self.base = {}         # bssid -> (media dB, desvio dB)
            self.streak = {}       # bssid -> escaneos seguidos desviada
            self.delta = {}        # bssid -> desvio actual (dB)
            self.moving = set()
            self.active = False
            self.since = None
            self.quiet = 0
            self.last_event = None

    def feed(self, aps, now):
        events = []
        with self.lock:
            cur = {a["bssid"]: a["signal"] for a in aps if a.get("signal")}
            if len(self.cal) < CAL_SCANS:
                self.cal.append(cur)
                if len(self.cal) == CAL_SCANS:
                    self._build_base()
                return
            self.delta, flagged = {}, set()
            for b, (mean, sd) in self.base.items():
                if b not in cur:
                    continue
                dev = _db(cur[b]) - mean
                self.delta[b] = round(dev, 1)
                if abs(dev) > max(BASE_DB, 3 * sd):
                    self.streak[b] = self.streak.get(b, 0) + 1
                else:
                    self.streak[b] = 0
                if self.streak[b] >= CONFIRM:
                    flagged.add(b)
            self.moving = flagged
            votes_needed = min(3, max(2, len(self.base) // 3))
            if len(flagged) >= votes_needed:
                self.quiet = 0
                if not self.active:
                    self.active, self.since = True, now
                    events.append(("movimiento", f"Movimiento detectado ({len(flagged)} redes cambiaron su señal)"))
            else:
                self.quiet += 1
                if self.active and self.quiet >= QUIET_TO_CLEAR:
                    self.active = False
                    events.append(("calma", "Movimiento terminado"))
                    self.since = None
                if not self.active:  # todo tranquilo: la linea base sigue despacio a la señal real
                    for b, (mean, sd) in list(self.base.items()):
                        if b in cur:
                            self.base[b] = (mean + DRIFT * (_db(cur[b]) - mean), sd)
            if events:
                self.last_event = {"ts": now, "kind": events[-1][0], "text": events[-1][1]}
        if self.on_event:
            for kind, text in events:
                self.on_event(kind, text, now)

    def _build_base(self):
        seen = {}
        for scan in self.cal:
            for b, s in scan.items():
                seen.setdefault(b, []).append(_db(s))
        for b, vals in seen.items():
            if len(vals) / CAL_SCANS >= MIN_PRESENCE and statistics.mean(vals) >= _db(MIN_SIGNAL):
                self.base[b] = (statistics.mean(vals), statistics.pstdev(vals))

    def state(self):
        with self.lock:
            if len(self.cal) < CAL_SCANS:
                status = "calibrando"
            else:
                status = "movimiento" if self.active else "calma"
            return {"status": status, "progress": len(self.cal), "total": CAL_SCANS,
                    "watching": len(self.base), "moving": sorted(self.moving), "delta": dict(self.delta),
                    "since": self.since, "last_event": self.last_event}
