"""
Scheduling Delta - Streamlit App
================================
Corregge la schedula truck di WK+1: dato il forecast giornaliero, lo scarico
Rodeo (CPT) e lo schedulato SSP, calcola quanti truck chiedere e quali VR ID
cancellare (eccessi scelti in modo casuale).

Avvio:  streamlit run app.py   (o doppio clic su AVVIA_SCHEDULING.bat)
"""

from io import BytesIO

import pandas as pd
import streamlit as st

import engine
import pianificatore

st.set_page_config(page_title="Scheduling Delta", page_icon="🚚", layout="wide")


# ─── Protezione con password (gate di accesso) ───────────────────────────────
def _check_password() -> bool:
    """Mostra un gate password. La password sta nei secrets di Streamlit Cloud
    (chiave 'app_password'). In locale, se il secret non c'e', l'accesso e' libero."""
    pwd_attesa = None
    try:
        pwd_attesa = st.secrets.get("app_password")
    except Exception:
        pwd_attesa = None
    if not pwd_attesa:
        return True  # nessuna password configurata (uso locale) -> accesso libero

    if st.session_state.get("auth_ok"):
        return True

    st.title("🔒 Scheduling Delta")
    pwd = st.text_input("Password", type="password")
    if st.button("Entra"):
        if pwd == pwd_attesa:
            st.session_state["auth_ok"] = True
            st.rerun()
        else:
            st.error("Password errata.")
    return False


if not _check_password():
    st.stop()

st.title("🚚 Scheduling Delta — correzione truck WK+1")
st.caption(
    "Carica forecast, scarico Rodeo e schedulato SSP: l'app calcola il need "
    "giornaliero, garantisce copertura del CPT orario e dice quali truck "
    "chiedere e quali VR ID cancellare."
)

# ─── Stato ─────────────────────────────────────────────────────────────────
ss = st.session_state
ss.setdefault("fc_forecast", None)   # forecast aggregato dal file unico
ss.setdefault("fc_cpt", None)        # CPT dal file unico
ss.setdefault("ssp_df", None)
ss.setdefault("tfr_df", None)
ss.setdefault("spots", [])          # spot SDT piazzati: list[dict]
ss.setdefault("cal_nonce", 0)       # per forzare il refresh del calendario

# ─── Settimana di riferimento ────────────────────────────────────────────────
_oggi = pd.Timestamp.now()
cA, cB, cC = st.columns([1, 1, 3])
week_num = cA.number_input("Week di riferimento", min_value=1, max_value=53,
                           value=int(ss.get("week_num", _oggi.isocalendar().week)), step=1)
anno = cB.number_input("Anno", min_value=2023, max_value=2100,
                       value=int(ss.get("anno", _oggi.year)), step=1)
ss["week_num"] = int(week_num)
ss["anno"] = int(anno)
_ini, _fin = engine.settimana_retail(int(week_num), int(anno))
cC.info(f"WK{int(week_num)} {int(anno)} → **{_ini.strftime('%d/%m/%Y')} (Dom)** … "
        f"**{_fin.strftime('%d/%m/%Y')} (Sab)**. Tutti i dati vengono filtrati su questa settimana.")


def _pulisci(df: pd.DataFrame) -> pd.DataFrame:
    """Rimuove righe/colonne completamente vuote e colonne 'Unnamed' fantasma."""
    if df is None:
        return df
    # elimina colonne senza nome e completamente vuote (es. colonna finale in piu')
    drop_cols = [c for c in df.columns
                 if (str(c).startswith("Unnamed") and df[c].isna().all())]
    if drop_cols:
        df = df.drop(columns=drop_cols)
    # elimina righe totalmente vuote
    df = df.dropna(how="all").reset_index(drop=True)
    return df


def _read_tabular(uploaded) -> pd.DataFrame:
    """Legge un file caricato (csv/xlsx) in DataFrame, robusto a colonne extra."""
    name = uploaded.name.lower()
    data = uploaded.read()
    if name.endswith((".xlsx", ".xlsm", ".xls")):
        return _pulisci(pd.read_excel(BytesIO(data)))
    # csv: utf-8-sig, index_col=False evita lo shift quando i dati hanno
    # una colonna in piu' dell'header (caso Outbound Dock Management)
    text = data.decode("utf-8-sig", errors="replace")
    from io import StringIO
    try:
        df = pd.read_csv(StringIO(text), index_col=False)
    except Exception:
        df = pd.read_csv(StringIO(text), sep=";", index_col=False)
    return _pulisci(df)


def _paste_to_df(text: str, has_header: bool = True) -> pd.DataFrame | None:
    """Converte testo incollato (tab/; separatori) in DataFrame."""
    text = (text or "").strip()
    if not text:
        return None
    from io import StringIO
    sep = "\t" if "\t" in text else (";" if ";" in text else ",")
    df = pd.read_csv(StringIO(text), sep=sep, header=0 if has_header else None,
                     index_col=False)
    return _pulisci(df)


# ─── Tab di input ──────────────────────────────────────────────────────────
tab_fcst, tab_ssp, tab_tfr, tab_run, tab_cal, tab_recap = st.tabs(
    ["📈 Forecast + CPT (IXD)", "🗓️ Schedulato (SSP)",
     "⚙️ TFR per lane", "▶️ Calcola", "📅 Pianifica orari (SDT)", "📋 Recap finale"]
)

with tab_fcst:
    st.subheader("Forecast + CPT — file unico IXD")
    st.write(
        "Carica l'export IXD (un'unica tabella): contiene **volumi giornalieri** e **CPT**, "
        "quindi sostituisce sia il vecchio forecast sia lo scarico Rodeo.\n\n"
        "Colonne per posizione: **A**=Lane · **G**=Tipo transfer (sommati) · "
        "**I**=Giorno volume · **J**=CPT · **K**=Volume. Date in formato US (M/G/A)."
    )
    up = st.file_uploader("Carica file Forecast+CPT", type=["csv", "xlsx", "xlsm"], key="up_fc")
    txt = st.text_area("…oppure incolla il contenuto (Ctrl+V da Excel)", height=180, key="txt_fc")
    c1, c2 = st.columns(2)

    def _carica_fc(raw_df):
        try:
            parsed = engine.parse_forecast_cpt(raw_df)
            ss["fc_forecast"] = parsed["forecast"]
            ss["fc_cpt"] = parsed["cpt"]
            st.success(f"Caricato: {parsed['forecast']['dest'].nunique()} lane, "
                       f"{len(parsed['forecast'])} righe volume, {len(parsed['cpt'])} CPT.")
        except Exception as e:
            st.error(f"Errore lettura file: {e}")

    if up is not None and c1.button("Carica da file", key="b_fc_file"):
        _carica_fc(_read_tabular(up))
    if c2.button("Carica da testo", key="b_fc_txt"):
        df = _paste_to_df(txt, has_header=True)
        if df is not None:
            _carica_fc(df)
        else:
            st.warning("Nessun testo incollato.")

    if ss.get("fc_forecast") is not None:
        st.markdown("**Volumi per lane/giorno (aggregati):**")
        prev = ss["fc_forecast"].copy()
        prev["giorno_date"] = pd.to_datetime(prev["giorno_date"]).dt.strftime("%a %d/%m")
        st.dataframe(prev.rename(columns={"dest": "Lane", "giorno_date": "Giorno",
                                          "volume": "Volume"}),
                     use_container_width=True, height=240)
    if ss.get("fc_cpt") is not None and not ss["fc_cpt"].empty:
        st.markdown("**CPT rilevati:**")
        cptv = ss["fc_cpt"][["dest", "cpt", "cpt_ora"]].copy()
        cptv["cpt"] = pd.to_datetime(cptv["cpt"]).dt.strftime("%a %d/%m %H:%M")
        st.dataframe(cptv.rename(columns={"dest": "Lane", "cpt": "CPT", "cpt_ora": "Ora"}),
                     use_container_width=True, height=200)

with tab_ssp:
    st.subheader("Schedulato SSP — truck già bookati WK+1")
    st.write(
        "Carica il report SSP. Colonne usate: `Sort/Route`, `VR ID`, `SDT`, "
        "`CPT`, `CPT Loaded Percentage`, `Equipment`, `Status`."
    )
    up = st.file_uploader("Carica SSP", type=["csv", "xlsx", "xlsm"], key="up_ssp")
    txt = st.text_area("…oppure incolla il contenuto", height=150, key="txt_ssp")
    c1, c2 = st.columns(2)
    if up is not None and c1.button("Carica SSP da file", key="b_ssp_file"):
        try:
            raw_df = _read_tabular(up)
            ss.ssp_df = engine.parse_ssp(raw_df)
            st.success(f"SSP caricato: {len(ss.ssp_df)} truck.")
        except Exception as e:
            st.error(f"Errore lettura SSP: {e}")
    if c2.button("Carica SSP da testo", key="b_ssp_txt"):
        try:
            raw_df = _paste_to_df(txt, has_header=True)
            ss.ssp_df = engine.parse_ssp(raw_df)
            st.success(f"SSP caricato: {len(ss.ssp_df)} truck.")
        except Exception as e:
            st.error(f"Errore lettura SSP: {e}")
    if ss.ssp_df is not None:
        st.dataframe(ss.ssp_df.head(30), use_container_width=True)

with tab_tfr:
    st.subheader("TFR — unità per truck (per lane)")
    st.write(
        "Due colonne: **lane** e **unità per truck**. È l'input fisso che "
        "converte i volumi in numero di truck (need = volume ÷ TFR)."
    )
    up = st.file_uploader("Carica TFR", type=["csv", "xlsx", "xlsm"], key="up_tfr")
    txt = st.text_area("…oppure incolla (lane <tab> TFR)", height=150, key="txt_tfr")
    tfr_default = st.number_input(
        "TFR di default (per lane senza valore, 0 = ignora)", min_value=0, value=0, step=50
    )
    c1, c2 = st.columns(2)
    if up is not None and c1.button("Carica TFR da file", key="b_tfr_file"):
        ss.tfr_df = _read_tabular(up)
        st.success(f"TFR caricato: {ss.tfr_df.shape[0]} lane.")
    if c2.button("Carica TFR da testo", key="b_tfr_txt"):
        df = _paste_to_df(txt, has_header=True)
        if df is not None:
            ss.tfr_df = df
            st.success(f"TFR caricato: {df.shape[0]} lane.")
        else:
            st.warning("Nessun testo incollato.")
    if ss.tfr_df is not None:
        st.dataframe(ss.tfr_df.head(30), use_container_width=True)
    ss["tfr_default"] = tfr_default

with tab_run:
    st.subheader("Calcola il delta")
    pronti = all([
        ss.get("fc_forecast") is not None,
        ss.ssp_df is not None,
        ss.tfr_df is not None,
    ])
    stato = {
        "Forecast + CPT": ss.get("fc_forecast") is not None,
        "Schedulato SSP": ss.ssp_df is not None,
        "TFR": ss.tfr_df is not None,
    }
    cols = st.columns(len(stato))
    for c, (k, v) in zip(cols, stato.items()):
        c.metric(k, "✓ pronto" if v else "— manca")

    if not pronti:
        st.info("Carica i tre input nelle tab precedenti (Forecast+CPT, SSP, TFR).")
    else:
        if st.button("▶️ Calcola need e delta", type="primary"):
            try:
                forecast = engine.forecast_long_da_unico(ss["fc_forecast"])
                tfr_map = engine.parse_tfr(ss.tfr_df)
                day_headers = list(dict.fromkeys(forecast["day_header"].tolist()))
                res = engine.calcola_delta(
                    forecast=forecast,
                    rodeo=ss["fc_cpt"],
                    ssp=ss.ssp_df,
                    tfr_map=tfr_map,
                    tfr_default=ss.get("tfr_default") or None,
                    day_headers=day_headers,
                    week_num=ss.get("week_num"),
                    anno=ss.get("anno"),
                )
                ss["result"] = res
                # pre-piazza automaticamente gli spot CPT (orario fisso) e
                # riparte con solo quelli: i truck a volume li piazza l'utente
                ss["spots"] = [dict(s) for s in res.get("spots_cpt", [])]
                ss["cal_nonce"] += 1
                # INVALIDA le griglie salvate: vanno rigenerate col nuovo calcolo
                # (altrimenti i nuovi CPT scoperti non entrano in griglia)
                for k in [key for key in ss.keys()
                          if key.startswith("grid_") or key.startswith("precomp_")]:
                    ss.pop(k, None)
            except Exception as e:
                st.error(f"Errore nel calcolo: {e}")
                st.exception(e)

    res = ss.get("result")
    if res:
        for w in res["warnings"]:
            st.warning(w)

        st.markdown("### 🟢 Truck da CHIEDERE")
        if res["da_chiedere"].empty:
            st.success("Nessun truck da chiedere: copertura ok.")
        else:
            st.dataframe(res["da_chiedere"], use_container_width=True)

        st.markdown("### 🔴 VR ID da CANCELLARE (eccessi, scelti a caso)")
        if res["da_cancellare"].empty:
            st.success("Nessun truck in eccesso da cancellare.")
        else:
            st.dataframe(res["da_cancellare"], use_container_width=True)

        with st.expander("📊 Dettaglio need per lane/giorno"):
            st.dataframe(res["need"], use_container_width=True)

        # download
        def _to_excel(res_dict) -> bytes:
            buf = BytesIO()
            with pd.ExcelWriter(buf, engine="openpyxl") as xl:
                res_dict["da_chiedere"].to_excel(xl, sheet_name="Da chiedere", index=False)
                res_dict["da_cancellare"].to_excel(xl, sheet_name="Da cancellare", index=False)
                res_dict["need"].to_excel(xl, sheet_name="Need dettaglio", index=False)
            return buf.getvalue()

        st.download_button(
            "⬇️ Scarica risultato (Excel)",
            data=_to_excel(res),
            file_name="scheduling_delta.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )




# ─── Helpers condivisi pianificazione ────────────────────────────────────────
_WD_NAMES = ["Lun", "Mar", "Mer", "Gio", "Ven", "Sab", "Dom"]
_NOME2WD = {"Lun": 0, "Mar": 1, "Mer": 2, "Gio": 3, "Ven": 4, "Sab": 5, "Dom": 6}


def _arrotonda30(ts):
    """Arrotonda un timestamp alla fascia di 30 min piu' vicina."""
    import pandas as _pd
    ts = _pd.Timestamp(ts)
    minuti = ts.hour * 60 + ts.minute
    q = int(round(minuti / 30.0)) * 30
    q = min(q, 21 * 60 + 30)   # non oltre 21:30 (ultima fascia)
    q = max(q, 6 * 60)         # non prima di 06:00
    return ts.normalize() + _pd.Timedelta(minutes=q)


def _token_lane(tok):
    """Interpreta un token di cella.

    'MXP6#113AB' -> ('MXP6','113AB',False)  schedulato (con VRID)
    'MXP6*'      -> ('MXP6', None, True)     CPT truck (nuovo, orario fisso)
    'MXP6'       -> ('MXP6', None, False)    nuovo a volume
    Ritorna (lane, vrid, is_cpt).
    """
    tok = tok.strip().upper()
    is_cpt = tok.endswith("*")
    if is_cpt:
        tok = tok[:-1].strip()
    if "#" in tok:
        lane, vrid = tok.split("#", 1)
        return lane.strip(), vrid.strip(), is_cpt
    return tok, None, is_cpt


def _split_celle(cella):
    """Separa una cella in token con separatori '-', ',', ';'."""
    import re as _re
    parti = _re.split(r"[-,;]+", str(cella))
    return [p.strip() for p in parti if p.strip()]


# ─── Tab: Pianifica orari (SDT) — griglia con schedulati precompilati ──────────
with tab_cal:
    st.subheader("📅 Pianifica orari (SDT) — griglia settimanale")
    res = ss.get("result")
    if not res:
        st.info("Prima calcola il delta nella tab ▶️ Calcola.")
    else:
        from datetime import datetime, timedelta

        _lun = engine.settimana_retail(ss["week_num"], ss["anno"])[0]
        giorni = [(pd.Timestamp(_lun) + pd.Timedelta(days=i)).date() for i in range(7)]
        col_giorni = [f"{_WD_NAMES[g.weekday()]} {g.strftime('%d/%m')}" for g in giorni]
        date_by_col = {col_giorni[i]: giorni[i] for i in range(7)}

        # fasce 30' 06:00-21:30
        fasce = []
        t = datetime(2000, 1, 1, 6, 0)
        while t.hour < 22:
            fasce.append(t.strftime("%H:%M"))
            t += timedelta(minutes=30)

        # ── SSP originale: VR ID -> (lane, sdt arrotondato) — solo settimana ──
        set_giorni = {g for g in giorni}
        ssp_orig = {}   # vrid -> {"lane","dt","equipment"}
        for s in res.get("sched_spots", []):
            dt = _arrotonda30(s["sdt"])
            if dt.date() not in set_giorni:
                continue
            ssp_orig[s["vrid"]] = {"lane": s["lane"], "dt": dt, "equipment": s.get("equipment", "")}

        # ── Griglia: init con schedulati precompilati come LANE#VRID ──
        grid_key = f"grid_{ss['week_num']}_{ss['anno']}"
        precomp_key = f"precomp_{ss['week_num']}_{ss['anno']}"
        if grid_key not in ss:
            base = {"Ora": fasce}
            cell = {(f, cg): "" for f in fasce for cg in col_giorni}
            for f in fasce:
                hh, mm = map(int, f.split(":"))
                for gi, g in enumerate(giorni):
                    dt = datetime(g.year, g.month, g.day, hh, mm)
                    if pianificatore.in_pausa(dt):
                        cell[(f, col_giorni[gi])] = "🔴"
                    elif not pianificatore.in_turno(dt):
                        cell[(f, col_giorni[gi])] = "⬛"
            # inserisci schedulati; traccia QUALI VR ID sono stati davvero piazzati
            precompilati = {}   # vrid -> {lane, dt}  (solo quelli messi in griglia)
            non_piazzati = []   # vrid che non entrano (pausa/fuori turno) -> restano in griglia in coda
            for vrid, info in ssp_orig.items():
                dt = info["dt"]
                cg = f"{_WD_NAMES[dt.weekday()]} {dt.strftime('%d/%m')}"
                fa = dt.strftime("%H:%M")
                tok = f"{info['lane']}#{vrid}"
                if (fa, cg) in cell and cell[(fa, cg)] not in ("🔴", "⬛"):
                    cur = cell[(fa, cg)]
                    cell[(fa, cg)] = tok if cur == "" else cur + "-" + tok
                    precompilati[vrid] = {"lane": info["lane"], "dt": dt}
                else:
                    # SDT su pausa/fuori-turno: mettilo comunque nella fascia valida piu' vicina
                    # cosi' non risulta "rimosso" per sbaglio. Scendo alla prima fascia libera in turno.
                    piazzato = False
                    for f2 in fasce:
                        h2, m2 = map(int, f2.split(":"))
                        dt2 = datetime(dt.year, dt.month, dt.day, h2, m2)
                        if pianificatore.in_pausa(dt2) or not pianificatore.in_turno(dt2):
                            continue
                        key2 = (f2, cg)
                        if key2 in cell and cell[key2] not in ("🔴", "⬛"):
                            cur = cell[key2]
                            cell[key2] = tok if cur == "" else cur + "-" + tok
                            precompilati[vrid] = {"lane": info["lane"],
                                                  "dt": pd.Timestamp(dt).normalize() + pd.Timedelta(hours=h2, minutes=m2)}
                            piazzato = True
                            break
                    if not piazzato:
                        non_piazzati.append(vrid)

            # ── Auto-piazza i CPT scoperti (SDT = orario CPT). Token 'LANE*' = CPT truck.
            #    Un CPT truck parte all'orario CPT: lo metto alla fascia = CPT (arrotondata a 30'),
            #    anche se cade in pausa/fuori-turno (il CPT e' vincolante).
            for sp in res.get("spots_cpt", []):
                dt_cpt = _arrotonda30(pd.Timestamp(sp["start"]))
                if dt_cpt.date() not in set_giorni:
                    continue
                cg = f"{_WD_NAMES[dt_cpt.weekday()]} {dt_cpt.strftime('%d/%m')}"
                fa = dt_cpt.strftime("%H:%M")
                if (fa, cg) not in cell:
                    continue
                tok = f"{sp['lane']}*"      # asterisco = CPT truck
                cur = cell[(fa, cg)]
                if cur in ("🔴", "⬛", ""):
                    cell[(fa, cg)] = tok      # sovrascrivo pausa/fuori-turno: il CPT vince
                else:
                    cell[(fa, cg)] = cur + "-" + tok

            for cg in col_giorni:
                base[cg] = [cell[(f, cg)] for f in fasce]
            ss[grid_key] = pd.DataFrame(base)
            ss[precomp_key] = precompilati   # base per il diff: cosa c'era in griglia all'inizio

        # ── Lettura griglia -> stato finale dei truck ──
        def _leggi_griglia(df):
            """Ritorna:
              finale_vrid: {vrid: dt}        (schedulati ancora presenti, con eventuale nuovo dt)
              nuovi: [{"lane","dt"}]         (token senza VRID = da chiedere)
              errori: [str]
            """
            finale_vrid = {}
            nuovi = []
            errori = []
            piazzati_lg = {}   # (lane, weekday) -> n truck totali piazzati (sched + nuovi)
            for _, riga in df.iterrows():
                ora = riga["Ora"]
                hh, mm = map(int, ora.split(":"))
                for cg in col_giorni:
                    cella = str(riga[cg]).strip()
                    if not cella or cella.startswith("🔴") or cella.startswith("⬛"):
                        continue
                    g = date_by_col[cg]
                    dt = datetime(g.year, g.month, g.day, hh, mm)
                    ok, motivo = pianificatore.valida_sdt(dt)
                    for tok in _split_celle(cella):
                        lane, vrid, is_cpt = _token_lane(tok)
                        # i CPT truck partono all'orario CPT (vincolante): non validano turno/pausa
                        if not ok and not is_cpt:
                            errori.append(f"{tok} {cg} {ora}: orario non valido ({motivo})")
                            continue
                        if vrid:
                            finale_vrid[vrid] = pd.Timestamp(dt)
                        else:
                            nuovi.append({"lane": lane, "dt": pd.Timestamp(dt),
                                          "tipo": "CPT truck" if is_cpt else "Volume"})
                        piazzati_lg[(lane, g.weekday())] = piazzati_lg.get((lane, g.weekday()), 0) + 1
            return finale_vrid, nuovi, errori, piazzati_lg

        # ── Diff: base = ciò che era precompilato in griglia (non l'SSP teorico) ──
        base_grid = ss.get(precomp_key, {})   # vrid -> {lane, dt}
        def _diff(finale_vrid, nuovi):
            da_chiedere, da_rimuovere, da_modificare = [], [], []
            for v in nuovi:
                da_chiedere.append({"Lane": v["lane"], "SDT": v["dt"],
                                    "Reason": v.get("tipo", "Volume")})
            for vrid, info in base_grid.items():
                if vrid not in finale_vrid:
                    da_rimuovere.append({"Lane": info["lane"], "VR ID": vrid,
                                         "SDT orig": info["dt"]})
                else:
                    nuovo_dt = finale_vrid[vrid]
                    if pd.Timestamp(nuovo_dt) != pd.Timestamp(info["dt"]):
                        da_modificare.append({"Lane": info["lane"], "VR ID": vrid,
                                              "SDT orig": info["dt"], "SDT nuovo": nuovo_dt})
            return da_chiedere, da_rimuovere, da_modificare

        # ── Callback live: applica le modifiche dell'editor alla griglia salvata ──
        def _on_grid_change():
            state = ss.get(f"editor_{grid_key}", {})
            df = ss[grid_key].copy()
            for ridx, changes in state.get("edited_rows", {}).items():
                for colname, val in changes.items():
                    df.at[int(ridx), colname] = val
            ss[grid_key] = df   # lo stato aggiornato: il cruscotto sopra lo leggera' al rerun

        # ── Calcolo live sullo stato corrente della griglia (aggiornato dal callback) ──
        finale_vrid, nuovi, errori, piazzati_lg = _leggi_griglia(ss[grid_key])
        da_chiedere, da_rimuovere, da_modificare = _diff(finale_vrid, nuovi)

        n_ssp = len(base_grid)
        n_rim = len(da_rimuovere)
        n_add = len(da_chiedere)
        n_mod = len(da_modificare)
        tot_finale = n_ssp - n_rim + n_add

        # ── Cruscotto numerico live IN CIMA (si aggiorna a ogni modifica) ──
        c1, c2, c3, c4, c5 = st.columns(5)
        c1.metric("Schedulati (SSP)", n_ssp)
        c2.metric("Da chiedere", f"+{n_add}")
        c3.metric("Da rimuovere", f"-{n_rim}")
        c4.metric("Da modificare", n_mod)
        c5.metric("TOTALE finale", tot_finale, delta=f"{tot_finale - n_ssp:+d} vs SSP")

        if errori:
            st.warning("Problemi nella griglia:\n\n- " + "\n- ".join(errori))

        # ── MATRICE lane×giorno: piazzati / need (live) ──
        need_df = res["need"]
        need_lg = {}   # (lane, weekday) -> need finale
        for _, r in need_df.iterrows():
            wd = _NOME2WD.get(r["Giorno"][:3])
            if wd is not None:
                need_lg[(r["Lane"], wd)] = int(r["Need finale"])
        # lane da mostrare: quelle con need>0 in qualche giorno OPPURE con truck piazzati
        lanes_matrice = sorted(
            {l for (l, _), n in need_lg.items() if n > 0} |
            {l for (l, _) in piazzati_lg}
        )
        st.markdown("#### Piazzati / Need per lane e giorno (live)")
        if lanes_matrice:
            matrice = []
            for lane in lanes_matrice:
                row = {"Lane": lane}
                for gi, g in enumerate(giorni):
                    wd = g.weekday()
                    need = need_lg.get((lane, wd), 0)
                    fatti = piazzati_lg.get((lane, wd), 0)
                    if need == 0 and fatti == 0:
                        row[col_giorni[gi]] = ""
                    else:
                        flag = "🟢" if fatti == need else ("🟡" if fatti < need else "🔴")
                        row[col_giorni[gi]] = f"{fatti}/{need} {flag}"
                matrice.append(row)
            st.dataframe(pd.DataFrame(matrice), use_container_width=True, hide_index=True)
            st.caption("Ogni cella = **piazzati / need** di quel giorno. "
                       "🟢 ok · 🟡 mancano truck · 🔴 troppi. Si aggiorna mentre editi la griglia.")
        else:
            st.info("Nessuna lane con need o truck piazzati.")

        st.caption(
            "Gli schedulati sono precompilati come **LANE#VRID** (es. `MXP6#113AB6BT`) e sono "
            "modificabili: spostali per **modificare** l'SDT, cancellali per **rimuoverli**. "
            "Aggiungi una lane senza # (es. `FCO1`) per un truck **nuovo da chiedere**. "
            "Più truck nella stessa fascia: separa con `-` (es. `MXP6-FCO1-BLQ1`). "
            "Il cruscotto sopra si aggiorna appena confermi una cella (Invio o clic fuori). "
            "🔴 pausa · ⬛ fuori turno."
        )

        # ── Editor con callback live ──
        col_cfg = {"Ora": st.column_config.TextColumn("Ora", disabled=True, width="small")}
        st.data_editor(
            ss[grid_key], use_container_width=True, height=560, hide_index=True,
            column_config=col_cfg, key=f"editor_{grid_key}", on_change=_on_grid_change,
        )

        if st.button("↩️ Ripristina griglia agli schedulati originali"):
            ss.pop(grid_key, None)
            ss.pop(precomp_key, None)
            st.rerun()

        # salva i risultati per il recap
        ss["plan_chiedere"] = da_chiedere
        ss["plan_rimuovere"] = da_rimuovere
        ss["plan_modificare"] = da_modificare
        ss["plan_totale"] = tot_finale
        ss["plan_nssp"] = n_ssp


# ─── Tab: Recap finale ────────────────────────────────────────────────────────
with tab_recap:
    st.subheader("📋 Recap finale")
    res = ss.get("result")
    if not res:
        st.info("Prima calcola il delta nella tab ▶️ Calcola.")
    elif "plan_totale" not in ss:
        st.info("Apri la tab 📅 Pianifica per generare il piano.")
    else:
        n_ssp = ss["plan_nssp"]
        chiedere = ss["plan_chiedere"]
        rimuovere = ss["plan_rimuovere"]
        modificare = ss["plan_modificare"]
        tot = ss["plan_totale"]

        c1, c2, c3, c4, c5 = st.columns(5)
        c1.metric("Schedulati (SSP)", n_ssp)
        c2.metric("Da chiedere", f"+{len(chiedere)}")
        c3.metric("Da rimuovere", f"-{len(rimuovere)}")
        c4.metric("Da modificare", len(modificare))
        c5.metric("TOTALE finale", tot, delta=f"{tot - n_ssp:+d} vs SSP")

        st.caption("Le tabelle seguono il formato dei fogli del template Network Resilience: "
                   "copiale direttamente nel template, o scarica il file già pronto.")

        # DataFrame nel formato dei fogli del template ND
        df_add = engine.nd_additions(chiedere)        # -> Additions
        df_ob = engine.nd_change_ob(modificare)        # -> Change of Existing Schedule OB
        df_oth = engine.nd_other(rimuovere)            # -> Other
        df_ib = pd.DataFrame(columns=engine.ND_COLS_CHANGE_IB)  # IB vuoto (flusso solo OB)

        st.markdown("### 🟢 Additions — Truck da CHIEDERE")
        if not df_add.empty:
            st.dataframe(df_add, use_container_width=True, hide_index=True)
        else:
            st.success("Nessuno.")

        st.markdown("### 🟠 Change of Existing Schedule OB — Truck da MODIFICARE")
        if not df_ob.empty:
            st.dataframe(df_ob, use_container_width=True, hide_index=True)
        else:
            st.success("Nessuno.")

        st.markdown("### 🔴 Other — Truck da RIMUOVERE")
        if not df_oth.empty:
            st.dataframe(df_oth, use_container_width=True, hide_index=True)
        else:
            st.success("Nessuno.")

        # ── Download: file con i 4 fogli del template ND ──
        def _recap_excel() -> bytes:
            buf = BytesIO()
            with pd.ExcelWriter(buf, engine="openpyxl") as xl:
                df_add.to_excel(xl, sheet_name="Additions", index=False)
                df_ob.to_excel(xl, sheet_name="Change of Existing Schedule OB", index=False)
                df_ib.to_excel(xl, sheet_name="Change of Existing Schedule IB", index=False)
                df_oth.to_excel(xl, sheet_name="Other", index=False)
            return buf.getvalue()

        st.download_button(
            "⬇️ Scarica (formato template Network Resilience)",
            data=_recap_excel(),
            file_name=f"NR_SIM_MXP5_WK{ss.get('week_num')}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
