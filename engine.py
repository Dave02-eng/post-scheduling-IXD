"""
Scheduling Delta - motore di calcolo
====================================
Data una settimana WK+1, calcola il delta tra il fabbisogno di truck
(derivato dal forecast giornaliero) e i truck gia' schedulati (SSP),
rispettando il vincolo del CPT orario letto dallo scarico Rodeo.

Output:
  - truck da CHIEDERE (need > schedulato), per lane/giorno
  - VR ID da CANCELLARE (schedulato > need), scelti in modo casuale

Nessuna dipendenza da Rodeo/Midway: tutti gli input arrivano come
DataFrame gia' caricati dalla UI.
"""

from __future__ import annotations

import io
import math
import random
import re
from datetime import date, datetime, timedelta

import pandas as pd

# ─── Costanti / helper ────────────────────────────────────────────────────────

# weekday(): 0=Lun .. 6=Dom
GIORNI_IT = ["Lunedi", "Martedi", "Mercoledi", "Giovedi", "Venerdi", "Sabato", "Domenica"]


# ─── Settimana di riferimento (retail week, parte di DOMENICA) ─────────────────

def settimana_retail(week_num: int, anno: int) -> tuple[date, date]:
    """Ritorna (domenica_inizio, sabato_fine) della retail week indicata.

    Convenzione allineata ai file MXP5/TRN3: la settimana parte di DOMENICA.
    WK1 = la settimana (che parte di domenica) contenente il 1° gennaio.
    Es. WK39 2026 -> domenica 2026-09-20 .. sabato 2026-09-26.
    """
    capodanno = date(anno, 1, 1)
    # domenica <= 1 gennaio (inizio della WK1)
    # weekday(): lun=0..dom=6 ; giorni da tornare indietro fino a domenica
    offset = (capodanno.weekday() + 1) % 7  # dom->0, lun->1, ..., sab->6
    inizio_wk1 = capodanno - timedelta(days=offset)
    inizio = inizio_wk1 + timedelta(weeks=week_num - 1)
    fine = inizio + timedelta(days=6)
    return inizio, fine


def giorni_settimana(week_num: int, anno: int) -> list[date]:
    inizio, _ = settimana_retail(week_num, anno)
    return [inizio + timedelta(days=i) for i in range(7)]


def estrai_dest(valore) -> str:
    """Normalizza una lane a solo codice destinazione.

    'MXP5->FCO1' -> 'FCO1' ; 'FCO1' -> 'FCO1'. Case-insensitive, trimmed.
    """
    if valore is None:
        return ""
    s = str(valore).strip().upper()
    if "->" in s:
        s = s.split("->")[-1].strip()
    if s in ("(BLANK)", "BLANK", "NAN", "NONE"):
        return ""
    return s


def _to_datetime(v):
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return pd.NaT
    return pd.to_datetime(v, errors="coerce", dayfirst=False)


# ─── Parser: file UNICO Forecast + CPT (IXD export) ───────────────────────────

def parse_forecast_cpt(df: pd.DataFrame) -> dict:
    """Legge il file unico IXD che contiene sia i volumi giornalieri sia i CPT.

    Schema colonne (per posizione, date in formato US M/G/A):
      A(0)  Lane            es. 'MXP5->AOI1'
      G(6)  Tipo transfer   Crossdock/Manual/Proactive... (si SOMMA tutto)
      I(8)  Giorno forecast data del volume (es. 9/27/2026 0:00)
      J(9)  CPT             data+ora del pull time (es. 9/30/2026 12:00)
      K(10) Volume          unita' (ultima colonna)

    Ritorna dict:
      - 'forecast' : DataFrame [dest, giorno_date, volume]  (volume sommato per lane+giorno)
      - 'cpt'      : DataFrame [dest, cpt, cpt_ora, cpt_giorno, quantity]
    """
    if df.shape[1] < 11:
        raise ValueError(
            f"File Forecast+CPT: attese almeno 11 colonne (A..K), trovate {df.shape[1]}. "
            "Controlla di aver caricato l'export IXD completo."
        )
    c_lane = df.columns[0]    # A
    c_day = df.columns[8]     # I
    c_cpt = df.columns[9]     # J
    c_vol = df.columns[10]    # K

    tmp = pd.DataFrame()
    tmp["dest"] = df[c_lane].map(estrai_dest)
    tmp["giorno_dt"] = pd.to_datetime(df[c_day], errors="coerce")
    tmp["cpt_dt"] = pd.to_datetime(df[c_cpt], errors="coerce")
    tmp["volume"] = pd.to_numeric(df[c_vol], errors="coerce").fillna(0.0)
    tmp = tmp[tmp["dest"] != ""].copy()

    # FORECAST: somma volume per lane + giorno (tutti i tipi transfer)
    tmp["giorno_date"] = tmp["giorno_dt"].dt.normalize()
    forecast = (tmp.dropna(subset=["giorno_date"])
                   .groupby(["dest", "giorno_date"], as_index=False)["volume"].sum())

    # CPT: per ogni (lane, giorno-CPT) l'orario del pull time
    cpt_rows, seen = [], set()
    for _, r in tmp.dropna(subset=["cpt_dt"]).iterrows():
        cpt = r["cpt_dt"]
        key = (r["dest"], cpt.normalize())
        if key in seen:
            continue
        seen.add(key)
        cpt_rows.append({
            "dest": r["dest"],
            "cpt": cpt,
            "cpt_ora": cpt.strftime("%H:%M"),
            "cpt_giorno": cpt.weekday(),
            "quantity": 0,
        })
    cpt_df = pd.DataFrame(cpt_rows) if cpt_rows else pd.DataFrame(
        columns=["dest", "cpt", "cpt_ora", "cpt_giorno", "quantity"])

    return {"forecast": forecast, "cpt": cpt_df}


def forecast_long_da_unico(forecast_agg: pd.DataFrame) -> pd.DataFrame:
    """Converte il forecast (dest, giorno_date, volume) nel formato 'long'
    atteso da calcola_delta: [dest, col_idx, volume, day_header]."""
    if forecast_agg.empty:
        return pd.DataFrame(columns=["dest", "col_idx", "volume", "day_header"])
    giorni_ordinati = sorted(forecast_agg["giorno_date"].unique())
    idx_map = {g: i for i, g in enumerate(giorni_ordinati)}
    out = forecast_agg.copy()
    out["col_idx"] = out["giorno_date"].map(idx_map)
    out["day_header"] = out["giorno_date"]
    return out[["dest", "col_idx", "volume", "day_header"]].reset_index(drop=True)


# ─── Parser: scarico Rodeo (ExSDReport) ───────────────────────────────────────

def parse_rodeo(raw: bytes | str) -> pd.DataFrame:
    """Legge il CSV Rodeo ExSD.

    Colonne attese: 'Destination Warehouse Id', 'ExSD', 'Quantity'.
    Ritorna DataFrame con: dest, cpt (datetime), cpt_ora (HH:MM),
    cpt_giorno (weekday int), quantity.
    """
    if isinstance(raw, bytes):
        text = raw.decode("utf-8-sig", errors="replace")
    else:
        text = raw
    df = pd.read_csv(io.StringIO(text))
    # normalizza nomi colonna
    cols = {c.lower().strip(): c for c in df.columns}

    def find(*names):
        for n in names:
            if n in cols:
                return cols[n]
        return None

    c_dest = find("destination warehouse id", "destination", "dest")
    c_exsd = find("exsd", "cpt")
    c_qty = find("quantity", "qty")
    if not c_dest or not c_exsd:
        raise ValueError(
            "CSV Rodeo non riconosciuto: servono le colonne "
            "'Destination Warehouse Id' e 'ExSD'."
        )

    out = pd.DataFrame()
    out["dest"] = df[c_dest].map(estrai_dest)
    out["cpt"] = df[c_exsd].map(_to_datetime)
    out["quantity"] = pd.to_numeric(df[c_qty], errors="coerce").fillna(0) if c_qty else 0
    out = out[out["dest"] != ""].copy()
    out["cpt_ora"] = out["cpt"].dt.strftime("%H:%M")
    out["cpt_giorno"] = out["cpt"].dt.weekday
    return out.reset_index(drop=True)


# ─── Parser: schedulato SSP ────────────────────────────────────────────────────

def parse_ssp(df: pd.DataFrame) -> pd.DataFrame:
    """Normalizza il report SSP dei truck gia' bookati.

    Colonne usate: Sort/Route (lane), VR ID, SDT, CPT, CPT Loaded Percentage,
    Equipment, Status. Ritorna un DataFrame normalizzato.
    """
    cols = {str(c).lower().strip(): c for c in df.columns}

    def find(*names):
        for n in names:
            if n in cols:
                return cols[n]
        return None

    c_route = find("sort/route", "route", "lane")
    c_vrid = find("vr id", "vrid", "vr_id")
    c_sdt = find("sdt")
    c_cpt = find("cpt")
    c_fill = find("cpt loaded percentage", "loaded percentage", "fill")
    c_equip = find("equipment")
    c_status = find("status")

    if not c_route or not c_vrid:
        raise ValueError(
            "Report SSP non riconosciuto: servono almeno le colonne "
            "'Sort/Route' e 'VR ID'."
        )

    out = pd.DataFrame()
    out["dest"] = df[c_route].map(estrai_dest)
    out["vrid"] = df[c_vrid].astype(str).str.strip()
    out["sdt"] = df[c_sdt].map(_to_datetime) if c_sdt else pd.NaT
    out["cpt"] = df[c_cpt].map(_to_datetime) if c_cpt else pd.NaT
    out["fill"] = pd.to_numeric(df[c_fill], errors="coerce") if c_fill else float("nan")
    out["equipment"] = df[c_equip].astype(str).str.strip() if c_equip else ""
    out["status"] = df[c_status].astype(str).str.strip() if c_status else ""

    out = out[(out["dest"] != "") & (out["vrid"] != "") & (out["vrid"].str.lower() != "nan")].copy()
    # giorno del truck: dal SDT se presente, altrimenti dal CPT
    giorno = out["sdt"].dt.weekday
    giorno = giorno.fillna(out["cpt"].dt.weekday)
    out["giorno"] = giorno
    out["cpt_ora"] = out["cpt"].dt.strftime("%H:%M")
    return out.reset_index(drop=True)


# ─── Parser: forecast a blocco (stile IXD FCST) ───────────────────────────────

def parse_forecast(df: pd.DataFrame) -> pd.DataFrame:
    """Forecast lane x 7 giorni.

    Prima colonna = lane (es. 'MXP5->FCO1' o 'FCO1'); le altre = giorni.
    Le intestazioni giorno possono essere date o etichette; l'ordine e'
    quello delle colonne. Ritorna long: dest, col_idx (0..n), volume.
    L'allineamento giorno->weekday viene fatto altrove usando le date header.
    """
    if df.shape[1] < 2:
        raise ValueError("Forecast: servono almeno una colonna lane + una colonna giorno.")
    df = df.copy()
    lane_col = df.columns[0]
    day_cols = list(df.columns[1:])

    records = []
    scartate = {"", "ROW LABELS", "DESTINATION", "TOTAL", "NAN", "(BLANK)", "BLANK",
                "GRAND TOTAL", "TOTALE"}
    for _, row in df.iterrows():
        dest = estrai_dest(row[lane_col])
        if dest in scartate or dest.upper() in scartate:
            continue
        for idx, dc in enumerate(day_cols):
            vol = pd.to_numeric(row[dc], errors="coerce")
            if pd.isna(vol):
                vol = 0
            records.append({"dest": dest, "col_idx": idx, "volume": float(vol), "day_header": dc})
    out = pd.DataFrame(records)
    return out


def mappa_giorni_da_header(day_headers) -> dict[int, int]:
    """Prova a mappare col_idx -> weekday(0..6) usando le intestazioni.

    Se le intestazioni sono date interpretabili, usa il loro weekday.
    Altrimenti prova a riconoscere nomi (Sun/Mon.. o Dom/Lun..).
    Ritorna {col_idx: weekday} oppure {} se non deducibile.
    """
    mapping = {}
    nomi = {
        "mon": 0, "lun": 0, "tue": 1, "mar": 1, "wed": 2, "mer": 2,
        "thu": 3, "gio": 3, "fri": 4, "ven": 4, "sat": 5, "sab": 5,
        "sun": 6, "dom": 6,
    }
    for idx, h in enumerate(day_headers):
        wd = None
        dt = pd.to_datetime(h, errors="coerce")
        if pd.notna(dt):
            wd = dt.weekday()
        else:
            key = str(h).strip().lower()[:3]
            wd = nomi.get(key)
        if wd is not None:
            mapping[idx] = wd
    return mapping


# ─── Parser: TFR per-lane ──────────────────────────────────────────────────────

def parse_tfr(df: pd.DataFrame) -> dict[str, float]:
    """TFR (unita' per truck) per lane.

    Riconosce le colonne per nome se presenti (es. header
    'Lane'/'destination' + 'TFR Forecast'/'TFR'); altrimenti usa
    prima colonna=lane, seconda=TFR. Se una lane compare piu' volte
    (piu' shipper account) tiene il valore piu' alto.
    """
    if df.shape[1] < 2:
        raise ValueError("TFR: servono almeno due colonne (lane, unita' per truck).")

    cols = {str(c).lower().strip(): c for c in df.columns}

    def find(*names):
        for n in names:
            if n in cols:
                return cols[n]
        return None

    lane_col = find("lane", "destination", "dest", "destination warehouse id")
    tfr_col = find("tfr forecast", "tfr", "tfr_forecast", "units per truck", "tfr units")

    # fallback posizionale se non trovo per nome
    if lane_col is None:
        lane_col = df.columns[0]
    if tfr_col is None:
        # cerca la prima colonna numerica plausibile diversa da lane
        tfr_col = None
        for c in df.columns:
            if c == lane_col:
                continue
            serie = pd.to_numeric(df[c], errors="coerce")
            if serie.notna().sum() >= max(1, len(df) // 2) and serie.fillna(0).gt(0).any():
                tfr_col = c
                break
        if tfr_col is None:
            tfr_col = df.columns[1]

    out: dict[str, float] = {}
    for _, row in df.iterrows():
        dest = estrai_dest(row[lane_col])
        tfr = pd.to_numeric(row[tfr_col], errors="coerce")
        if dest and pd.notna(tfr) and tfr > 0:
            # se la lane si ripete, tieni il valore piu' alto
            out[dest] = max(out.get(dest, 0.0), float(tfr))
    return out


# ─── Motore delta ──────────────────────────────────────────────────────────────

def calcola_delta(
    forecast: pd.DataFrame,
    rodeo: pd.DataFrame,
    ssp: pd.DataFrame,
    tfr_map: dict[str, float],
    tfr_default: float | None = None,
    day_headers: list | None = None,
    week_num: int | None = None,
    anno: int | None = None,
    seed: int | None = 42,
) -> dict:
    """Calcola need giornaliero, applica vincolo CPT e produce i due output.

    Ritorna dict con:
      - 'need'        : DataFrame per lane/giorno (need, scheduled, delta)
      - 'da_chiedere' : DataFrame righe con delta>0
      - 'da_cancellare': DataFrame di VR ID da rimuovere
      - 'warnings'    : list[str]
    """
    warnings: list[str] = []
    rnd = random.Random(seed)

    # ── Filtro sulla settimana di riferimento ─────────────────────────────────
    giorni_ref = None
    if week_num is not None and anno is not None:
        giorni_ref = [pd.Timestamp(d) for d in giorni_settimana(week_num, anno)]
        set_ref = {d.date() for d in giorni_ref}
        inizio, fine = giorni_ref[0].date(), giorni_ref[-1].date()

        # Forecast: tieni solo le colonne-giorno che cadono nella settimana
        if day_headers is not None:
            header_dt = {h: pd.to_datetime(h, errors="coerce") for h in day_headers}
            validi = [h for h, dt in header_dt.items()
                      if pd.notna(dt) and dt.date() in set_ref]
            fuori = [h for h, dt in header_dt.items()
                     if pd.notna(dt) and dt.date() not in set_ref]
            if validi:
                keep = set(validi)
                forecast = forecast[forecast["day_header"].isin(keep)].copy()
                day_headers = validi
                if fuori:
                    warnings.append(
                        f"Forecast: {len(fuori)} colonne fuori dalla WK{week_num} "
                        f"({inizio}..{fine}) sono state ignorate."
                    )
            elif fuori:
                warnings.append(
                    f"ATTENZIONE: nessuna colonna del forecast cade nella WK{week_num} "
                    f"({inizio}..{fine}). Controlla che il forecast sia della settimana giusta. "
                    "Uso tutte le colonne senza filtro."
                )

        # SSP: tieni solo i truck con SDT nella settimana
        if "sdt" in ssp.columns:
            sdt_date = ssp["sdt"].dt.date
            mask = sdt_date.isin(set_ref)
            scartati = int((~mask & ssp["sdt"].notna()).sum())
            if mask.any():
                ssp = ssp[mask | ssp["sdt"].isna()].copy()
                if scartati:
                    warnings.append(
                        f"Schedulato: {scartati} truck con SDT fuori dalla WK{week_num} ignorati."
                    )

        # Rodeo: tieni solo i CPT nella settimana
        if "cpt" in rodeo.columns:
            cpt_date = rodeo["cpt"].dt.date
            mask_r = cpt_date.isin(set_ref)
            scartati_r = int((~mask_r & rodeo["cpt"].notna()).sum())
            if mask_r.any():
                rodeo = rodeo[mask_r].copy()
                if scartati_r:
                    warnings.append(
                        f"Rodeo: {scartati_r} CPT fuori dalla WK{week_num} ignorati."
                    )
            elif scartati_r:
                warnings.append(
                    f"ATTENZIONE: tutti i CPT dello scarico Rodeo sono fuori dalla WK{week_num} "
                    f"({inizio}..{fine}). Il vincolo CPT non verra' applicato: "
                    "riscarica il report Rodeo per la settimana giusta."
                )
                rodeo = rodeo.iloc[0:0].copy()

    # 1) mappa col_idx -> weekday dal forecast header
    if day_headers is None and "day_header" in forecast.columns:
        day_headers = list(dict.fromkeys(forecast["day_header"].tolist()))
    col2wd = mappa_giorni_da_header(day_headers or [])
    if not col2wd:
        warnings.append(
            "Non sono riuscito a dedurre i giorni dalle intestazioni del forecast: "
            "uso l'ordine delle colonne come Lun..Dom."
        )
        # fallback: assume prime 7 colonne = Lun..Dom
        col2wd = {i: i % 7 for i in sorted(forecast["col_idx"].unique())}

    # TFR medio (fallback per lane senza TFR): media dei TFR noti > 0
    tfr_validi = [v for v in tfr_map.values() if v and v > 0]
    tfr_medio = (sum(tfr_validi) / len(tfr_validi)) if tfr_validi else None
    lane_con_media = set()   # per avvisare una sola volta per lane

    # 2) need per lane/giorno = ceil(volume / TFR)
    need_rows = []
    for _, r in forecast.iterrows():
        dest = r["dest"]
        wd = col2wd.get(int(r["col_idx"]))
        if wd is None:
            continue
        vol = r["volume"]
        tfr = tfr_map.get(dest, tfr_default)
        if not tfr or tfr <= 0:
            # fallback: usa la MEDIA dei TFR delle altre lane
            if tfr_medio:
                tfr = tfr_medio
                if vol > 0 and dest not in lane_con_media:
                    lane_con_media.add(dest)
                    warnings.append(
                        f"TFR mancante per lane {dest}: uso la media TFR = {int(round(tfr_medio))}."
                    )
            else:
                if vol > 0:
                    warnings.append(
                        f"TFR mancante per lane {dest} e nessun TFR disponibile per la media: "
                        "righe di need saltate."
                    )
                continue
        need = int(math.ceil(vol / tfr)) if vol > 0 else 0
        need_rows.append({"dest": dest, "giorno": wd, "volume": vol, "tfr": tfr, "need_volume": need})
    need_df = pd.DataFrame(need_rows)
    if need_df.empty:
        need_df = pd.DataFrame(columns=["dest", "giorno", "volume", "tfr", "need_volume"])

    # 3) vincolo CPT (opzione a): il CPT dello scarico Rodeo, per la sua lane/giorno,
    #    richiede almeno 1 truck. Costruiamo un set di (dest, giorno) con CPT orario
    #    e l'ora richiesta.
    cpt_req = {}       # (dest, giorno) -> ora HH:MM
    cpt_dt = {}        # (dest, giorno) -> datetime completo del CPT (per pre-piazzare lo spot)
    for _, r in rodeo.iterrows():
        if pd.isna(r["cpt_giorno"]):
            continue
        key = (r["dest"], int(r["cpt_giorno"]))
        cpt_req[key] = r["cpt_ora"]
        cpt_dt[key] = r["cpt"]

    # 4) schedulato per lane/giorno
    ssp_valid = ssp[ssp["giorno"].notna()].copy()
    ssp_valid["giorno"] = ssp_valid["giorno"].astype(int)
    sched_count = (
        ssp_valid.groupby(["dest", "giorno"]).size().rename("scheduled").reset_index()
    )

    # SDT (HH:MM) dei truck schedulati per (lane, giorno):
    # serve per capire se il CPT (dal forecast) e' gia' coperto da un SDT uguale.
    sdt_per_lg = {}
    for _, r in ssp_valid.iterrows():
        sdt = r.get("sdt")
        if pd.isna(sdt):
            continue
        key = (r["dest"], int(r["giorno"]))
        sdt_per_lg.setdefault(key, set()).add(pd.Timestamp(sdt).strftime("%H:%M"))

    # 4b) lista dei truck schedulati (VR ID) con lane + SDT, per precompilare la griglia
    sched_spots = []
    for _, r in ssp_valid.iterrows():
        sdt = r.get("sdt")
        if pd.isna(sdt):
            continue
        sched_spots.append({
            "lane": r["dest"],
            "vrid": str(r["vrid"]),
            "sdt": pd.Timestamp(sdt),
            "equipment": r.get("equipment", ""),
            "cpt_ora": r.get("cpt_ora", ""),
        })

    # 5) unisci need + scheduled + vincolo CPT su tutte le combinazioni presenti
    chiavi = set(zip(need_df["dest"], need_df["giorno"])) if not need_df.empty else set()
    chiavi |= set(zip(sched_count["dest"], sched_count["giorno"])) if not sched_count.empty else set()
    chiavi |= set(cpt_req.keys())

    need_lookup = {(r.dest, r.giorno): r for r in need_df.itertuples(index=False)} if not need_df.empty else {}
    sched_lookup = {(r.dest, r.giorno): int(r.scheduled) for r in sched_count.itertuples(index=False)} if not sched_count.empty else {}

    righe = []
    for (dest, giorno) in sorted(chiavi):
        nr = need_lookup.get((dest, giorno))
        need_vol = int(nr.need_volume) if nr is not None else 0
        volume = float(nr.volume) if nr is not None else 0.0
        tfr = float(nr.tfr) if nr is not None else tfr_map.get(dest, tfr_default or 0)
        scheduled = sched_lookup.get((dest, giorno), 0)

        cpt_ora = cpt_req.get((dest, giorno))
        # Il CPT si considera SOLO se quel giorno c'e' volume forecastato (>0).
        # CPT con volume 0 -> ignorato (non impone truck).
        if volume <= 0:
            cpt_ora = None

        # CPT SCOPERTO (definizione a): c'e' un CPT (dal forecast) e NESSUN truck
        # schedulato ha SDT uguale a quell'orario. L'SSP conta solo per gli SDT.
        cpt_scoperto = bool(cpt_ora) and (cpt_ora not in sdt_per_lg.get((dest, giorno), set()))
        need_cpt = 1 if cpt_scoperto else 0

        # need finale = truck a volume + eventuale truck CPT dedicato (aggiuntivo)
        need_finale = need_vol + need_cpt

        delta = scheduled - need_finale  # >0 eccesso, <0 mancano
        righe.append({
            "Lane": dest,
            "Giorno": GIORNI_IT[giorno],
            "_wd": giorno,
            "Volume": int(volume),
            "TFR": int(tfr) if tfr else 0,
            "Need volume": need_vol,
            "CPT orario": cpt_ora or "",
            "CPT scoperto": cpt_scoperto,
            "Need finale": need_finale,
            "Schedulati": scheduled,
            "Delta": delta,
        })
    need_out = pd.DataFrame(righe).sort_values(["Lane", "_wd"]).reset_index(drop=True)

    # 6) DA CHIEDERE: delta negativo (mancano truck)
    da_chiedere_rows = []
    spots_cpt = []  # truck CPT gia' con orario fisso (SDT = CPT), da pre-piazzare
    for row in need_out.to_dict("records"):
        dest, wd = row["Lane"], int(row["_wd"])
        cpt_ora = row["CPT orario"]      # CPT di riferimento SOLO dal forecast (col J)
        cpt_scoperto = bool(row["CPT scoperto"])
        cpt_da_chiedere = 1 if cpt_scoperto else 0

        # need residuo a volume dopo aver contato l'eventuale truck CPT dedicato
        manca = -int(row["Delta"])
        vol_da_chiedere = max(0, manca - cpt_da_chiedere) if manca > 0 else 0

        if cpt_da_chiedere:
            dt = cpt_dt.get((dest, wd))
            if dt is not None and pd.notna(dt):
                spots_cpt.append({
                    "lane": dest,
                    "start": pd.Timestamp(dt).strftime("%Y-%m-%dT%H:%M:%S"),
                    "tipo": "CPT",
                })
            da_chiedere_rows.append({
                "Lane": dest,
                "Giorno": row["Giorno"],
                "CPT orario": cpt_ora,
                "Truck da chiedere": 1,
                "Motivo": "CPT (orario fisso)",
            })
        if vol_da_chiedere > 0:
            da_chiedere_rows.append({
                "Lane": dest,
                "Giorno": row["Giorno"],
                "CPT orario": "",
                "Truck da chiedere": vol_da_chiedere,
                "Motivo": "Volume",
            })
    da_chiedere = pd.DataFrame(da_chiedere_rows)

    # 7) DA CANCELLARE: delta positivo (eccesso) -> scegli VR ID random,
    #    ma NON toccare l'ultimo truck che copre un CPT orario obbligatorio.
    da_cancellare_rows = []
    for row in need_out.to_dict("records"):
        eccesso = int(row["Delta"])
        if eccesso <= 0:
            continue
        dest, wd = row["Lane"], int(row["_wd"])
        sched = int(row["Schedulati"])
        need = int(row["Need finale"])
        volume = int(row["Volume"])
        gruppo = ssp_valid[(ssp_valid["dest"] == dest) & (ssp_valid["giorno"] == wd)].copy()
        vrids = gruppo["vrid"].tolist()

        # proteggi un truck che copre il CPT orario richiesto (solo se c'e' volume:
        # row["CPT orario"] e' gia' vuoto per i giorni a volume 0)
        cpt_ora = row["CPT orario"]
        protetto = None
        if cpt_ora:
            coprono = gruppo[gruppo["cpt_ora"] == cpt_ora]["vrid"].tolist()
            if coprono:
                protetto = coprono[0]

        # motivo della cancellazione (schedulati > need di quel giorno)
        if volume <= 0:
            motivo_canc = f"Nessun volume forecastato ({dest} {row['Giorno']}): need 0, schedulati {sched}"
        else:
            motivo_canc = f"Eccesso: schedulati {sched} > need {need} (volume {volume})"

        candidati = [v for v in vrids if v != protetto]
        rnd.shuffle(candidati)
        da_togliere = candidati[:eccesso]
        for v in da_togliere:
            info = gruppo[gruppo["vrid"] == v].iloc[0]
            da_cancellare_rows.append({
                "Lane": dest,
                "Giorno": row["Giorno"],
                "VR ID": v,
                "Equipment": info.get("equipment", ""),
                "Fill %": None if pd.isna(info.get("fill")) else round(float(info.get("fill")) * 100, 1),
                "SDT": info.get("sdt"),
                "Motivo": motivo_canc,
            })
    da_cancellare = pd.DataFrame(da_cancellare_rows)

    return {
        "need": need_out.drop(columns=["_wd"]),
        "da_chiedere": da_chiedere,
        "da_cancellare": da_cancellare,
        "spots_cpt": spots_cpt,
        "sched_spots": sched_spots,
        "warnings": warnings,
    }


# ─── Mappatura lane -> (lane completa, shipper account) per il template ND ────
# Riusa la mappatura concordata per la SIM. Le lane sea passano per SSAV.
SHIP_MAP = {
    "AOI1": ("MXP5->AOI1", "ATSWarehouseTransfers"),
    "BCN1": ("MXP5->BCN1", "ATSWarehouseTransfers"),
    "BCN4": ("MXP5->SSAV->SBCN->BCN4", "ATSSeaWarehouseTransfersGround"),
    "BGY1": ("MXP5->BGY1", "ATSWarehouseTransfers"),
    "BLQ1": ("MXP5->BLQ1", "ATSWarehouseTransfers"),
    "CGN1": ("MXP5->CGN1", "ATSWarehouseTransfers"),
    "FCO1": ("MXP5->FCO1", "ATSWarehouseTransfers"),
    "FRA3": ("MXP5->FRA3", "ATSWarehouseTransfers"),
    "FRA7": ("MXP5->FRA7", "ATSWarehouseTransfers"),
    "HAM2": ("MXP5->HAM2", "ATSWarehouseTransfers"),
    "LIL1": ("MXP5->LIL1", "ATSWarehouseTransfers"),
    "LIN8": ("MXP5->LIN8", "AndromedaWarehouseTransfers"),
    "MAD4": ("MXP5->SSAV->SVCA->MAD4", "ATSSeaWarehouseTransfersGround"),
    "MAD7": ("MXP5->SSAV->SVCA->MAD7", "ATSSeaWarehouseTransfersGround"),
    "MUC3": ("MXP5->MUC3", "ATSWarehouseTransfers"),
    "MXP3": ("MXP5->MXP3", "ATSWarehouseTransfers"),
    "MXP6": ("MXP5->MXP6", "ATSWarehouseTransfers"),
    "NXI1": ("MXP5->XLI8->NXI1", "ATSWarehouseTransfers"),
    "ORY4": ("MXP5->ORY4", "ATSWarehouseTransfers"),
    "OVD1": ("MXP5->SSAV->SVCA->OVD1", "ATSSeaWarehouseTransfersGround"),
    "PAD1": ("MXP5->PAD1", "ATSWarehouseTransfers"),
    "POZ1": ("MXP5->POZ1", "ATSWarehouseTransfers"),
    "PRG2": ("MXP5->PRG2", "ATSWarehouseTransfers"),
    "PSR2": ("MXP5->PSR2", "ATSWarehouseTransfers"),
    "RMU1": ("MXP5->SSAV->SVCA->RMU1", "ATSSeaWarehouseTransfersGround"),
    "SVQ1": ("MXP5->SSAV->SBCN->SVQ1", "ATSSeaWarehouseTransfersGround"),
    "SZZ1": ("MXP5->SZZ1", "ATSWarehouseTransfers"),
    "TRN1": ("MXP5->TRN1", "ATSWarehouseTransfers"),
    "XUKM": ("MXP5->XUKM", "ATSSeaWarehouseTransfersGround"),
}

EQUIP_TYPE_ND = "DETACHED_TRAILER_TL"


def lane_full_e_shipper(lane_code: str) -> tuple[str, str]:
    """Da codice destinazione (es. 'FCO1') -> (lane completa, shipper account).
    Se la lane non e' in mappatura: ('MXP5->LANE', '')."""
    code = str(lane_code).strip().upper()
    if code in SHIP_MAP:
        return SHIP_MAP[code]
    return (f"MXP5->{code}", "")


def _fmt_nd(dt) -> str:
    """Formatta un datetime nel formato richiesto dal template ND: mm/dd/yyyy hh:mm."""
    if dt is None or (isinstance(dt, float)):
        return ""
    ts = pd.to_datetime(dt, errors="coerce")
    if pd.isna(ts):
        return ""
    return ts.strftime("%m/%d/%Y %H:%M")


# Intestazioni esatte dei fogli del template Network Resilience
ND_COLS_ADDITIONS = [
    "Status", "ND Comment", "Lane",
    "Requested First Dock Arrival (mm/dd/yyyy hh:mm)",
    "Requested Dock Departure (mm/dd/yyyy hh:mm)",
    "Corresponding CPT (mm/dd/yyyy hh:mm)",
    "Requested Last Dock Arrival (mm/dd/yyyy hh:mm)",
    "Truck Filter", "Carrier", "Rate", "Currency",
    "Shipper account", "Equipment Type", "Reason",
]
ND_COLS_CHANGE_OB = [
    "Status", "ND Comment", "VRID", "Lane",
    "Current Dock Departure (mm/dd/yyyy hh:mm)",
    "Requested Dock Departure (mm/dd/yyyy hh:mm)",
    "Corresponding CPT (mm/dd/yyyy hh:mm)", "Reason",
]
ND_COLS_CHANGE_IB = [
    "Status", "ND Comment", "VRID", "Lane",
    "Current Last Dock Arrival (mm/dd/yyyy hh:mm)",
    "Requested Last Dock Arrival (mm/dd/yyyy hh:mm)",
    "Corresponding CPT (mm/dd/yyyy hh:mm)", "Reason",
]
ND_COLS_OTHER = ["Status", "ND Comment", "VRID", "Amendment", "Reason"]


def nd_additions(chiedere: list[dict]) -> pd.DataFrame:
    """Truck da chiedere -> formato foglio 'Additions'."""
    rows = []
    for r in chiedere:
        lane_full, shipper = lane_full_e_shipper(r.get("Lane", r.get("lane", "")))
        reason = "CPT truck" if r.get("Reason") == "CPT truck" else "IXD VOLUME"
        rows.append({
            "Status": "", "ND Comment": "", "Lane": lane_full,
            "Requested First Dock Arrival (mm/dd/yyyy hh:mm)": "",
            "Requested Dock Departure (mm/dd/yyyy hh:mm)": _fmt_nd(r.get("SDT")),
            "Corresponding CPT (mm/dd/yyyy hh:mm)": "",
            "Requested Last Dock Arrival (mm/dd/yyyy hh:mm)": "",
            "Truck Filter": "", "Carrier": "", "Rate": "", "Currency": "",
            "Shipper account": shipper, "Equipment Type": EQUIP_TYPE_ND, "Reason": reason,
        })
    return pd.DataFrame(rows, columns=ND_COLS_ADDITIONS)


def nd_change_ob(modificare: list[dict]) -> pd.DataFrame:
    """Truck da modificare -> formato foglio 'Change of Existing Schedule OB'."""
    rows = []
    for r in modificare:
        lane_full, _ = lane_full_e_shipper(r.get("Lane", r.get("lane", "")))
        rows.append({
            "Status": "", "ND Comment": "", "VRID": r.get("VR ID", ""), "Lane": lane_full,
            "Current Dock Departure (mm/dd/yyyy hh:mm)": _fmt_nd(r.get("SDT orig")),
            "Requested Dock Departure (mm/dd/yyyy hh:mm)": _fmt_nd(r.get("SDT nuovo")),
            "Corresponding CPT (mm/dd/yyyy hh:mm)": "", "Reason": "Schedule optimization",
        })
    return pd.DataFrame(rows, columns=ND_COLS_CHANGE_OB)


def nd_other(rimuovere: list[dict]) -> pd.DataFrame:
    """Truck da rimuovere -> formato foglio 'Other'."""
    rows = []
    for r in rimuovere:
        rows.append({
            "Status": "", "ND Comment": "", "VRID": r.get("VR ID", ""),
            "Amendment": "", "Reason": "Cancel VRID - not needed",
        })
    return pd.DataFrame(rows, columns=ND_COLS_OTHER)
