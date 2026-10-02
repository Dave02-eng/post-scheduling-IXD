"""
Pianificatore SDT - turni, pause e generazione eventi per il calendario.
========================================================================
Regole operative del sito MXP5 (fasce in cui si puo' caricare):
  - Lun-Gio: AM 06:00-14:00 + PM 14:00-22:00  -> 06:00-22:00
  - Ven:     07:00-14:00 + 14:00-22:00         -> 07:00-22:00
  - Sab:     turno unico 06:00-14:00           -> 06:00-14:00
  - Dom:     turno unico 12:00-20:00           -> 12:00-20:00

Pause (nessun carico):
  - Lun-Sab: 10:10-10:40 e 18:10-18:40
             (il Sab solo la pausa mattutina 10:10-10:40, il turno finisce alle 14)
  - Dom:     16:00-16:30

weekday(): 0=Lun .. 6=Dom
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

# Orari operativi per weekday: (ora_inizio, ora_fine) in ore decimali, o None se chiuso
TURNI = {
    0: (6.0, 22.0),   # Lun
    1: (6.0, 22.0),   # Mar
    2: (6.0, 22.0),   # Mer
    3: (6.0, 22.0),   # Gio
    4: (7.0, 22.0),   # Ven
    5: (6.0, 14.0),   # Sab
    6: (12.0, 20.0),  # Dom
}

# Pause per weekday: lista di (inizio_str, fine_str) in "HH:MM"
PAUSE = {
    0: [("10:10", "10:40"), ("18:10", "18:40")],
    1: [("10:10", "10:40"), ("18:10", "18:40")],
    2: [("10:10", "10:40"), ("18:10", "18:40")],
    3: [("10:10", "10:40"), ("18:10", "18:40")],
    4: [("10:10", "10:40"), ("18:10", "18:40")],
    5: [("10:10", "10:40")],                       # Sab: solo pausa mattina
    6: [("16:00", "16:30")],                       # Dom
}


def _iso(d: date, hhmm: str) -> str:
    h, m = map(int, hhmm.split(":"))
    return datetime(d.year, d.month, d.day, h, m).strftime("%Y-%m-%dT%H:%M:%S")


def _iso_ore(d: date, ore: float) -> str:
    h = int(ore)
    m = int(round((ore - h) * 60))
    return datetime(d.year, d.month, d.day, h, m).strftime("%Y-%m-%dT%H:%M:%S")


def eventi_pause(giorni: list[date]) -> list[dict]:
    """Eventi background rossi per le pause di ogni giorno della settimana."""
    out = []
    for d in giorni:
        wd = d.weekday()
        for (ini, fin) in PAUSE.get(wd, []):
            out.append({
                "title": "PAUSA",
                "start": _iso(d, ini),
                "end": _iso(d, fin),
                "display": "background",
                "color": "#ff4d4d",
                "editable": False,
                "groupId": "pausa",
            })
    return out


def eventi_fuori_turno(giorni: list[date],
                       slot_min: str = "00:00", slot_max: str = "24:00") -> list[dict]:
    """Eventi background grigi per le ore fuori dai turni operativi.

    Copre, per ogni giorno, l'intervallo prima dell'apertura e dopo la chiusura
    (entro la finestra visibile slot_min..slot_max).
    """
    vis_min = float(slot_min.split(":")[0]) + float(slot_min.split(":")[1]) / 60
    vis_max = float(slot_max.split(":")[0]) + float(slot_max.split(":")[1]) / 60
    out = []
    for d in giorni:
        wd = d.weekday()
        turno = TURNI.get(wd)
        if turno is None:
            # giorno chiuso: oscura tutto
            out.append(_bg_grigio(d, vis_min, vis_max))
            continue
        apri, chiudi = turno
        if apri > vis_min:
            out.append(_bg_grigio(d, vis_min, apri))
        if chiudi < vis_max:
            out.append(_bg_grigio(d, chiudi, vis_max))
    return [e for e in out if e]


def _bg_grigio(d: date, ini_ore: float, fin_ore: float) -> dict | None:
    if fin_ore <= ini_ore:
        return None
    return {
        "title": "",
        "start": _iso_ore(d, ini_ore),
        "end": _iso_ore(d, fin_ore),
        "display": "background",
        "color": "#3a3a3a",
        "editable": False,
        "groupId": "fuori_turno",
    }


def in_pausa(dt: datetime) -> bool:
    """True se l'istante cade dentro una pausa."""
    wd = dt.weekday()
    for (ini, fin) in PAUSE.get(wd, []):
        hi, mi = map(int, ini.split(":"))
        hf, mf = map(int, fin.split(":"))
        t0 = time(hi, mi)
        t1 = time(hf, mf)
        if t0 <= dt.time() < t1:
            return True
    return False


def in_turno(dt: datetime) -> bool:
    """True se l'istante cade dentro l'orario operativo del giorno."""
    wd = dt.weekday()
    turno = TURNI.get(wd)
    if turno is None:
        return False
    apri, chiudi = turno
    ore = dt.hour + dt.minute / 60
    return apri <= ore < chiudi


def valida_sdt(dt: datetime) -> tuple[bool, str]:
    """Verifica che un SDT sia in un orario valido (in turno e non in pausa)."""
    if not in_turno(dt):
        return False, "fuori turno"
    if in_pausa(dt):
        return False, "in pausa"
    return True, ""
