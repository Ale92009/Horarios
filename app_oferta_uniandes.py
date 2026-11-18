"""Oferta de cursos Uniandes: Streamlit + pandas.

Instalar: python -m pip install streamlit pandas requests
Ejecutar: python -m streamlit run app_oferta_uniandes.py

API pública identificada en el JavaScript del sitio y comprobada en vivo:
GET /api/terms
GET /api/programs?term=202710&ptrm=
GET /api/courses?term=202710&prefix=ISIS&offset=0&limit=200
Solo se ofrecen períodos publicados por la fuente. No requiere credenciales.
"""

from datetime import datetime, timedelta, timezone
import json
import base64
import re
import unicodedata
import tempfile
from pathlib import Path
from html import escape

import pandas as pd
import requests
import streamlit as st
import streamlit.components.v1 as components
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

BASE = "https://ofertadecursos.uniandes.edu.co/api"
DIAS = {"l": "Lunes", "m": "Martes", "i": "Miércoles", "j": "Jueves",
        "v": "Viernes", "s": "Sábado", "d": "Domingo"}


def consultar(endpoint, **params):
    """GET con tiempo límite, reintentos acotados y validación de respuesta."""
    retry = Retry(total=3, backoff_factor=0.5,
                  status_forcelist=[429, 500, 502, 503, 504], allowed_methods=["GET"])
    with requests.Session() as session:
        session.mount("https://", HTTPAdapter(max_retries=retry))
        response = session.get(f"{BASE}/{endpoint}", params=params, timeout=(10, 45))
        response.raise_for_status()
        data = response.json()
    if not isinstance(data, list) or any(not isinstance(x, dict) for x in data):
        raise ValueError(f"Respuesta inesperada de /{endpoint}; la API pudo cambiar.")
    return data


@st.cache_data(ttl=300, show_spinner=False)
def periodos():
    return consultar("terms")


@st.cache_data(ttl=300, show_spinner=False)
def programas(term):
    return consultar("programs", term=term, ptrm="")


@st.cache_data(ttl=300, show_spinner=False)
def secciones_programa(term, prefix, page_size=200, freshness="", name_input="", profesor_name=""):
    """Sin filtro de duración: incluye todos los ciclos y secciones del programa.

    Se consulta hasta una página vacía, incluso si el servidor limita el tamaño
    solicitado. Una página repetida o mal formada impide entregar datos parciales.
    """
    rows, seen, offset = [], set(), 0
    for _ in range(500):
        page = consultar("courses", term=term, prefix=prefix, offset=offset,
                         limit=page_size, ptrm="", nameInput=name_input, profesorName=profesor_name)
        if not page:
            return rows, datetime.now(timezone(timedelta(hours=-5))).isoformat(timespec="seconds")
        for row in page:
            if not all(row.get(k) is not None for k in ("term", "nrc", "class", "course")):
                raise ValueError("La API devolvió una sección sin identificadores.")
            if str(row["term"]) != str(term) or (prefix and row["class"] != prefix):
                raise ValueError("La API no respetó el período o programa solicitado.")
        keys = {(str(r["term"]), str(r["nrc"])) for r in page}
        if keys & seen or len(keys) != len(page):
            raise ValueError("La paginación devolvió secciones repetidas. Actualiza e intenta de nuevo.")
        seen.update(keys)
        rows.extend(page)
        offset += len(page)
    raise ValueError("Se excedió el límite de paginación; no se mostrarán datos incompletos.")


def hora(value):
    value = str(value or "").strip()
    return f"{value.zfill(4)[:2]}:{value.zfill(4)[2:]}" if value.isdigit() else value or "—"


def normalizar(value):
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(c for c in text if not unicodedata.combining(c))
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text.lower()).split())


def coincide(text, query):
    """Todas las palabras, en cualquier orden, sin tildes ni puntuación."""
    normalized = normalizar(text)
    return all(word in normalized for word in normalizar(query).split())


def curso_coincide(row, query):
    compact = re.sub(r"[^A-Z0-9]", "", query.upper())
    if re.fullmatch(r"[A-Z]{4}\d{4}[A-Z0-9]*", compact):
        return compact == f"{row['class']}{row['course']}".upper()
    if re.fullmatch(r"\d{4}[A-Z0-9]*", compact):
        return compact == str(row["course"]).upper()
    return coincide(f"{row['class']} {row['course']} {row.get('title', '')}", query)


def filtro_api(text, curso=False):
    """Prefiltro amplio: una palabra, sin imponer orden ni perder las tildes.

    La API usa LIKE y requiere mayúsculas en nameInput. '_' admite una letra
    acentuada en la fuente. La comprobación local aplica luego todas las palabras.
    En cursos, las palabras de hasta cuatro letras y los códigos podrían
    pertenecer al prefijo/número en vez del título y no se envían como título.
    """
    words = [w for w in normalizar(text).split()
             if w.isalpha() and len(w) >= (5 if curso else 2)]
    if not words:
        return ""
    word = max(words, key=lambda w: (sum(c not in "aeioun" for c in w), len(w)))
    pattern = "".join("_" if c in "aeioun" else c.upper() for c in word)
    return pattern if any(c != "_" for c in pattern) else ""


def cargar_busqueda(term, prefix, q, freshness):
    return secciones_programa(term, prefix, freshness=freshness,
                              name_input=filtro_api(q["curso"], curso=True),
                              profesor_name=filtro_api(q["profesor"]))


def crear_dataframe(rows):
    """Una fila por día y encuentro; NRC y sección agrupan las filas."""
    result = []
    for r in rows:
        base = {
            "Período": str(r["term"]), "Programa": r["class"],
            "Curso": f"{r['class']}-{r['course']}", "Nombre": r.get("title", ""),
            "NRC": str(r["nrc"]), "Sección": str(r.get("section", "")),
            "Créditos": r.get("credits"), "Capacidad": r.get("maxenrol"),
            "Campus": r.get("campus") or "Sin información",
            "Duración": r.get("ptrmdesc") or "Sin información",
            "Docentes": "; ".join(i.get("name", "") for i in r.get("instructors") or []) or "Por asignar",
        }
        for schedule in r.get("schedules") or [{}]:
            days = [v for k, v in DIAS.items() if schedule.get(k)] or ["Sin día publicado"]
            for day in days:
                result.append({**base, "Día": day, "Inicio": hora(schedule.get("time_ini")),
                               "Fin": hora(schedule.get("time_fin")),
                               "Salón": schedule.get("classroom") or "Sin asignar",
                               "Edificio": schedule.get("building") or "Sin asignar",
                               "Fecha inicial": str(schedule.get("date_ini") or "")[:10],
                               "Fecha final": str(schedule.get("date_fin") or "")[:10]})
    df = pd.DataFrame(result)
    if not df.empty:
        for col in ["Créditos", "Capacidad"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


def tabla_unificada(df, filtros=None, estados=None, borradas=0, seleccion=None):
    """HTML accesible con rowspan por sección; escapa todos los datos externos."""
    filtros, estados = filtros or {}, estados or {}
    seleccion = set(seleccion or [])
    shared = ["Curso", "Nombre", "NRC", "Sección", "Docentes", "Créditos",
              "Período", "Programa", "Capacidad", "Campus", "Duración"]
    meetings = ["Día", "Inicio", "Fin", "Salón", "Edificio", "Fecha inicial", "Fecha final"]
    # Primero los identificadores y encuentros; los detalles adicionales van al final.
    order = shared[:6] + meetings + shared[6:]
    columns = [c for c in order if c in df.columns]

    def cell(value):
        return escape("—" if pd.isna(value) else str(value))

    parts = ['''<!doctype html><html lang="es"><head><meta charset="utf-8">
    <meta name="color-scheme" content="dark"><style>
    :root {color-scheme:dark;}
    html, body {margin:0; padding:0; background:#0e1117; color:#e6edf3;
        font-family:Arial,sans-serif;}
    * {box-sizing:border-box;}
    .oferta-scroll {overflow:auto; max-height:620px; border:1px solid #3b4758;
        border-radius:10px;}
    .oferta-table {border-collapse:separate; border-spacing:0; width:100%; font-size:14px;
        background:#0e1117; color:#e6edf3;}
    .oferta-table th {position:sticky; top:0; background:#17324d; color:#ffffff;
        z-index:1; text-align:left; white-space:nowrap;}
    .oferta-table th, .oferta-table td {padding:10px 12px; border-bottom:1px solid #303b49;
        border-right:1px solid #303b49; vertical-align:top; line-height:1.5;}
    .oferta-table td {background:#0e1117; color:#e6edf3;}
    .oferta-table .section-start td {border-top:2px solid #64748b;}
    .oferta-table td.shared {background:#1b2432; color:#e6edf3; min-width:90px;
        max-width:270px; overflow-wrap:break-word;}
    .oferta-table td.meeting {white-space:nowrap;}
    .oferta-table input[type=checkbox] {width:18px; height:18px; accent-color:#78b9ff; cursor:pointer;}
    .filter-arrow {background:#284763; color:white; border:1px solid #66829f; border-radius:3px;
        cursor:pointer; margin-left:8px; padding:2px 5px;}
    .filter-arrow.filtered {background:#1b7754; border-color:#8ae5bd;}
    .oferta-table tbody.seleccionada td {outline:1px solid #5caaff; outline-offset:-1px;}
    .oferta-table tbody.descartada td {background:#282d35; color:#89919c;}
    .toolbar {display:flex; flex-wrap:wrap; gap:8px; align-items:center; margin:0 0 8px;}
    .toolbar button {background:#1b2432; color:#e6edf3; border:1px solid #66829f;
        border-radius:5px; padding:7px 10px; cursor:pointer;}
    .toolbar span {color:#a7b6c9; font-size:13px;}
    </style></head><body><div class="toolbar">
    <button type="button" data-action="clear_filters">Limpiar filtros de columnas</button>''']
    if borradas:
        parts.append(f'<button type="button" data-action="restore">Recuperar secciones borradas ({borradas})</button>')
    parts.append('<span>Filtra con ▼. Marca secciones y haz clic derecho para incluir, descartar o borrar.</span></div>'
                 '<div class="oferta-scroll" role="region" aria-label="Encuentros agrupados por sección" '
                 'tabindex="0"><table class="oferta-table"><thead><tr><th scope="col">'
                 '<input type="checkbox" data-select-all aria-label="Seleccionar todas las secciones visibles"></th>')
    parts.extend(f'<th scope="col">{escape(c)}<button type="button" '
                 f'class="filter-arrow{" filtered" if c in filtros else ""}" '
                 f'data-filter-column="{escape(c, quote=True)}" '
                 f'aria-label="Filtrar {escape(c, quote=True)}" title="Filtrar {escape(c, quote=True)}">▼</button></th>' for c in columns)
    parts.append("</tr></thead>")
    # No combina NRC iguales pertenecientes a períodos distintos.
    for (term, nrc), group in df.groupby(["Período", "NRC"], sort=False, dropna=False):
        section_key = f"{term}|{nrc}"
        status = estados.get(section_key, "incluir")
        classes = ("descartada " if status == "descartar" else "") + ("seleccionada" if section_key in seleccion else "")
        parts.append(f'<tbody data-section-key="{escape(section_key, quote=True)}" class="{classes.strip()}">')
        for index, (_, row) in enumerate(group.iterrows()):
            parts.append('<tr class="section-start">' if index == 0 else "<tr>")
            if index == 0:
                checked = " checked" if section_key in seleccion else ""
                parts.append(f'<td rowspan="{len(group)}"><input type="checkbox" data-section="{escape(section_key, quote=True)}" '
                             f'aria-label="Seleccionar sección {escape(str(group.iloc[0]["Sección"]), quote=True)} NRC {escape(str(nrc), quote=True)}"{checked}></td>')
            for column in columns:
                if column in shared:
                    if index == 0:
                        parts.append(f'<td class="shared" rowspan="{len(group)}">{cell(row[column])}</td>')
                else:
                    parts.append(f'<td class="meeting">{cell(row[column])}</td>')
            parts.append("</tr>")
        parts.append("</tbody>")
    if df.empty:
        parts.append(f'<tbody><tr><td colspan="{len(columns)+1}">No hay secciones visibles. '
                     'Limpia los filtros o recupera las secciones borradas.</td></tr></tbody>')
    parts.append("</table></div></body></html>")
    return "".join(parts)


def filtrar_columnas(df, filtros, estados):
    result = df.copy()
    keys = result["Período"].astype(str) + "|" + result["NRC"].astype(str)
    result = result[keys.map(lambda key: estados.get(key) != "borrar")]
    for column, query in filtros.items():
        if column in result.columns and isinstance(query, list):
            result = result[result[column].map(valor_celda).isin(query)]
        elif column in result.columns and str(query).strip():
            result = result[result[column].map(lambda value: coincide("—" if pd.isna(value) else value, query))]
    return result


def valor_celda(value):
    return "—" if pd.isna(value) else str(value)


def minutos(value):
    if not re.fullmatch(r"\d{2}:\d{2}", str(value)):
        return None
    h, m = map(int, value.split(":"))
    return h * 60 + m if 0 <= h < 24 and 0 <= m < 60 else None


def filtrar_encuentros(df, q):
    if df.empty:
        return df
    mask = pd.Series(True, index=df.index)
    if q["dias"]:
        mask &= df["Día"].isin(q["dias"])
    if q["salon"].strip():
        mask &= df.apply(lambda r: coincide(f"{r['Edificio']} {r['Salón']}", q["salon"]), axis=1)
    if q["horario"]:
        lower, upper = minutos(q["desde"]), minutos(q["hasta"])
        start, end = df["Inicio"].map(minutos), df["Fin"].map(minutos)
        if q["modo"] == "Dentro del intervalo":
            mask &= start.ge(lower) & end.le(upper) & start.notna() & end.notna()
        else:
            mask &= start.lt(upper) & end.gt(lower) & start.notna() & end.notna()
    if q["completa"]:
        # Los criterios deben cumplirse juntos en el mismo encuentro.
        nrcs = df.loc[mask, "NRC"]
        return df[df["NRC"].isin(nrcs)].copy()
    return df.loc[mask].copy()


DEFAULTS = {"programa": "", "curso": "", "profesor": "", "salon": "", "dias": [],
            "horario": False, "desde": "07:00", "hasta": "22:00",
            "modo": "Dentro del intervalo", "completa": False}


def validar_consulta(q):
    if not isinstance(q, dict) or not isinstance(q.get("term"), str):
        raise ValueError("La consulta debe incluir un período.")
    for key, default in DEFAULTS.items():
        if key not in q or type(q[key]) is not type(default):
            raise ValueError(f"Campo inválido: {key}")
    if any(day not in DIAS.values() for day in q["dias"]):
        raise ValueError("Días inválidos.")
    if q["modo"] not in ["Dentro del intervalo", "Se cruza con el intervalo"]:
        raise ValueError("Modo de horario inválido.")
    if q["horario"]:
        a, b = minutos(q["desde"]), minutos(q["hasta"])
        if a is None or b is None or a >= b:
            raise ValueError("Usa horas HH:MM válidas y una hora final posterior a la inicial.")
    return {k: q[k] for k in ["term", *DEFAULTS]}


def restaurar(q):
    for key, value in q.items():
        st.session_state[key] = value
    st.session_state["consulta"] = q.copy()
    guardar_enlace()


def codificar_consulta(q):
    return base64.urlsafe_b64encode(json.dumps(q, ensure_ascii=False).encode("utf-8")).decode("ascii")


def decodificar_consulta(value):
    if len(value) > 16000:
        raise ValueError("El enlace de búsqueda es demasiado largo.")
    return validar_consulta(json.loads(base64.b64decode(value, altchars=b"-_", validate=True).decode("utf-8")))


def guardar_enlace():
    q = st.session_state.get("consulta")
    if st.session_state.get("recordar", True) and q:
        st.query_params["busqueda"] = codificar_consulta(q)
    elif "busqueda" in st.query_params:
        del st.query_params["busqueda"]
    viewer = st.session_state.get("visor", {})
    if st.session_state.get("recordar", True) and viewer:
        st.query_params["visor"] = codificar_consulta(list(viewer.values()))
    elif "visor" in st.query_params:
        del st.query_params["visor"]


def render_busqueda():
    st.set_page_config(page_title="Explorar cursos · Uniandes", page_icon="📚", layout="wide")
    st.title("Explora tu próximo semestre")
    st.caption("Oferta Uniandes · Busca por programa, curso o docente y afina los encuentros por día, hora y salón.")
    for key, value in DEFAULTS.items():
        st.session_state.setdefault(key, value)
    st.session_state.setdefault("historial", [])
    if "entrada_iniciada" not in st.session_state:
        st.session_state.entrada_iniciada = True
        # Forma parte de la clave de caché: una entrada nueva consulta la fuente,
        # mientras que las interacciones de esta sesión conservan la caché habitual.
        st.session_state.freshness = datetime.now(timezone.utc).isoformat()
        st.session_state.recordar = True
        if "visor" in st.query_params:
            try:
                raw = st.query_params["visor"]
                if len(raw) > 60000:
                    raise ValueError("Visor demasiado grande")
                saved = json.loads(base64.b64decode(raw, altchars=b"-_", validate=True).decode("utf-8"))
                if not isinstance(saved, list) or len(saved) > 30:
                    raise ValueError("Visor inválido")
                restored = {}
                for value in saved:
                    item = validar_consulta(value)
                    if not re.fullmatch(r"[A-Z]{4}-\d{4}[A-Z0-9]*", item["curso"]):
                        raise ValueError("Curso inválido")
                    restored[f"{item['term']}|{item['curso']}"] = item
                st.session_state.visor = restored
            except (ValueError, TypeError, KeyError, UnicodeError):
                st.warning("No se pudo recuperar el visor del enlace.")
        if "busqueda" in st.query_params:
            try:
                q = decodificar_consulta(st.query_params["busqueda"])
                restaurar(q)
                st.session_state.historial = [q.copy()]
            except (ValueError, TypeError, KeyError, UnicodeError):
                st.warning("No se pudo recuperar la búsqueda del enlace. Puedes crear una nueva.")
    with st.sidebar:
        st.header("Tus búsquedas")
        st.checkbox("Recordar y actualizar mi última búsqueda al volver", key="recordar", on_change=guardar_enlace)
        st.caption("La última búsqueda queda guardada en la dirección de esta página. "
                   "Guarda este enlace en favoritos y ábrelo para recuperar los filtros y consultar datos actualizados. "
                   "El enlace incluye los criterios de búsqueda; quien lo reciba puede verlos.")
        st.caption("El historial se conserva durante esta sesión. Descárgalo para recuperarlo otro día o en otro equipo.")
        for index, item in enumerate(st.session_state.historial):
            label = f"{item['term']} · {item['curso'] or item['programa'] or item['profesor'] or 'Todos los cursos'}"
            st.button(label, key=f"hist_{index}", on_click=restaurar, args=(item,), use_container_width=True)
        st.download_button("Guardar historial", json.dumps(st.session_state.historial, ensure_ascii=False, indent=2),
                           "historial_uniandes.json", "application/json")
        upload = st.file_uploader("Recuperar historial (.json)", type="json")
        if st.button("Importar historial", disabled=upload is None):
            try:
                data = json.loads(upload.getvalue())
                if not isinstance(data, list) or len(data) > 100:
                    raise ValueError("Se espera una lista de hasta 100 consultas.")
                st.session_state.historial = [validar_consulta(q) for q in data]
                st.rerun()
            except (ValueError, TypeError, KeyError) as exc:
                st.error(f"No se pudo importar: {exc}")
        if st.button("Borrar historial"):
            st.session_state.historial = []
            st.rerun()
        st.divider()
        if st.button("Actualizar datos de la fuente"):
            st.cache_data.clear()
            st.rerun()
        st.markdown("[Abrir oferta oficial](https://ofertadecursos.uniandes.edu.co/)")
    try:
        terms = periodos()
        if not terms:
            st.info("No hay períodos publicados.")
            return
        labels = {str(t["term_desc"]): f"{t['term_name']} ({t['term_desc']})" for t in terms}
        if st.session_state.get("term") not in labels:
            st.session_state.term = next(iter(labels))
        st.selectbox("Período", list(labels), format_func=labels.get, key="term")
        programs = programas(st.session_state.term)
        with st.form("busqueda"):
            left, right = st.columns(2)
            left.text_input("Programa o prefijo", key="programa", placeholder="Sistemas, ISIS, matematicas…")
            right.text_input("Código o nombre del curso", key="curso", placeholder="ISIS1221, ISIS-1221, ISIS 1221…")
            left.text_input("Profesor: nombres y/o apellidos", key="profesor", placeholder="Sin tildes y en cualquier orden")
            right.text_input("Salón o edificio", key="salon", placeholder="ML 603, virtual…")
            st.multiselect("Días de clase", list(DIAS.values()), key="dias")
            with st.expander("Horario y presentación", expanded=True):
                st.checkbox("Filtrar por horario", key="horario")
                a, b, c = st.columns([1, 1, 2])
                a.text_input("Desde (HH:MM)", key="desde")
                b.text_input("Hasta (HH:MM)", key="hasta")
                c.selectbox("Relación con el horario", ["Dentro del intervalo", "Se cruza con el intervalo"], key="modo")
                st.checkbox("Mostrar todos los días de las secciones que coincidan", key="completa")
                st.caption("Por defecto se muestran solo los encuentros que cumplen día, horario y salón a la vez. "
                           "Los filtros vacíos permiten cualquier valor. El filtro de profesor exige que una misma persona coincida con todas las palabras.")
            submitted = st.form_submit_button("Buscar secciones", type="primary", use_container_width=True)
        if submitted:
            q = validar_consulta({k: st.session_state[k] for k in ["term", *DEFAULTS]})
            st.session_state.consulta = q
            history = [h for h in st.session_state.historial if h != q]
            st.session_state.historial = [q.copy(), *history][:100]
            guardar_enlace()
            st.rerun()
        q = st.session_state.get("consulta")
        if not q:
            st.info("Completa los campos que necesites y pulsa Buscar secciones. Puedes buscar un docente en todo el período dejando el programa vacío.")
            return
        if q["term"] not in labels:
            st.warning("El período guardado ya no está publicado. Selecciona otro período y vuelve a buscar.")
            return
        # La consulta guardada tiene su propio período aunque se edite el formulario.
        available = programas(q["term"])
        prefixes = [p["prefix"] for p in available if coincide(f"{p['prefix']} {p['name']}", q["programa"])]
        if q["programa"].strip() and not prefixes:
            st.info("Ningún programa coincide con la búsqueda.")
            return
        code = re.sub(r"[^A-Z0-9]", "", q["curso"].upper())
        if re.fullmatch(r"[A-Z]{4}\d{4}[A-Z0-9]*", code):
            prefixes = [p for p in prefixes if p == code[:4]]
            if not prefixes:
                st.info("El código del curso no coincide con el programa indicado.")
                return
        elif not q["programa"].strip():
            prefixes = [""]  # API sin prefijo: búsqueda global en el período.
        rows = []
        with st.spinner("Consultando todas las páginas de resultados… Una búsqueda global puede tardar."):
            for prefix in prefixes:
                batch, updated = cargar_busqueda(q["term"], prefix, q, st.session_state.freshness)
                rows.extend(batch)
        rows = [r for r in rows if curso_coincide(r, q["curso"])]
        if q["profesor"].strip():
            rows = [r for r in rows if any(coincide(i.get("name", ""), q["profesor"])
                                          for i in r.get("instructors") or [])]
        df = filtrar_encuentros(crear_dataframe(rows), q)
        st.divider()
        st.subheader(f"Resultados · {labels[q['term']]}")
        a, b = st.columns(2)
        a.metric("Secciones", df["NRC"].nunique() if not df.empty else 0)
        b.metric("Cursos", df["Curso"].nunique() if not df.empty else 0)
        if df.empty:
            st.info("No se encontraron encuentros con estos criterios. Prueba ampliando los filtros.")
            return
        st.caption(f"Datos consultados: {updated}. Caché de 5 minutos. Cada fila corresponde a un día y horario; "
                   "el mismo NRC identifica las filas de una sección. Los resultados corresponden a la última búsqueda enviada.")
        courses = sorted(df["Curso"].unique())
        # Solo se agrega directamente cuando la consulta identifica un único curso.
        if len(courses) == 1 and st.button("Agregar al visor", type="primary"):
            course = courses[0]
            new_keys = {f"{q['term']}|{course}"}
            if len(set(st.session_state.visor) | new_keys) > 30:
                st.warning("El visor permite hasta 30 cursos. Quita alguno antes de agregar más.")
                return
            item = {**q, "curso": course, "programa": course.split("-")[0]}
            key = f"{q['term']}|{course}"
            st.session_state.visor[key] = item
            st.session_state.visor_activo = key
            guardar_enlace()
            st.rerun()
        if len(courses) == 1:
            st.caption("El curso conserva los filtros usados al agregarlo. Agregarlo otra vez actualiza sus filtros.")
        else:
            st.caption("Busca un curso específico para agregarlo al visor.")
        visible = render_tabla(df, key="tabla_resultados", context=q)
        st.download_button("Descargar resultados CSV", visible.to_csv(index=False).encode("utf-8-sig"),
                           f"oferta_{q['term']}.csv", "text/csv")
    except (requests.RequestException, ValueError, KeyError) as exc:
        st.error(f"No fue posible completar la búsqueda: {exc}")
        st.info("Revisa los filtros o actualiza los datos de la fuente e intenta de nuevo.")


TABS_HTML = r'''<!doctype html><html><head><meta charset="utf-8"><style>
html,body {margin:0;background:#0e1117;color:#e6edf3;font:14px Arial,sans-serif;color-scheme:dark}
#tabs {display:flex;gap:6px;overflow-x:auto;padding:8px 2px 12px}
.tab {display:flex;align-items:center;border:1px solid #3b4758;border-radius:8px;background:#1b2432;flex-shrink:0}
.tab.active {border-color:#78b9ff;background:#17324d}
button {color:inherit;background:transparent;border:0;cursor:pointer;padding:10px;font:inherit}
.label {cursor:pointer;user-select:none;padding-left:4px}
.grip {cursor:grab;touch-action:none;user-select:none;padding:10px 6px;color:#a9c6e3}
.grip:active {cursor:grabbing}.close {visibility:hidden;color:#ffb4b4;padding:8px}
.tab:hover .close,.tab:focus-within .close {visibility:visible}
.tab.dragging {opacity:.4}.tab:focus-within {outline:2px solid #78b9ff}
@media(hover:none){.close{visibility:visible}}
</style></head><body><div id="tabs" role="tablist" aria-label="Cursos del visor"></div><script>
let items=[],active='';
function send(type,payload={}){window.parent.postMessage({isStreamlitMessage:true,type,...payload},'*')}
function emit(action,extra={}){send('streamlit:setComponentValue',{value:{action,...extra,id:Date.now()+':'+Math.random()},dataType:'json'})}
function render(){const root=document.getElementById('tabs');root.replaceChildren();
 items.forEach((item,index)=>{let tab=document.createElement('div');tab.className='tab'+(item.key===active?' active':'');
 tab.dataset.key=item.key;
 const label=document.createElement('button');label.textContent=item.code;label.className='label';
 label.title=item.term+' · Ver curso';label.setAttribute('role','tab');label.setAttribute('aria-selected',String(item.key===active));
 // La selección usa un clic normal y nunca captura el puntero.
 label.onclick=()=>emit('select',{key:item.key});
 const grip=document.createElement('button');grip.textContent='⠿';grip.className='grip';
 grip.title='Arrastra para mover '+item.code;grip.setAttribute('aria-label','Mover '+item.code);
 label.onkeydown=e=>{if(e.altKey&&(e.key==='ArrowLeft'||e.key==='ArrowRight')){e.preventDefault();let order=items.map(i=>i.key);let j=index+(e.key==='ArrowLeft'?-1:1);if(j>=0&&j<order.length){[order[index],order[j]]=[order[j],order[index]];emit('reorder',{order})}}};
 let gesture=null;
 grip.onpointerdown=e=>{if(e.button!==0)return;e.preventDefault();gesture={id:e.pointerId,x:e.clientX,moved:false};
  // Captura en el contenedor estable: mover una pestaña no cancela el gesto.
  root.onpointermove=move;root.onpointerup=e=>finish(e);root.onpointercancel=e=>finish(e,true);
  root.onlostpointercapture=e=>{if(gesture)finish(e,true)};root.setPointerCapture(e.pointerId)};
 function move(e){if(!gesture||e.pointerId!==gesture.id)return;
  if(!gesture.moved&&Math.abs(e.clientX-gesture.x)<6)return;
  gesture.moved=true;tab.classList.add('dragging');
  const others=Array.from(root.children).filter(el=>el!==tab);
  const before=others.find(el=>{const rect=el.getBoundingClientRect();return e.clientX<rect.left+rect.width/2});
  root.insertBefore(tab,before||null);
  const bounds=root.getBoundingClientRect();
  if(e.clientX>bounds.right-25)root.scrollLeft+=18;else if(e.clientX<bounds.left+25)root.scrollLeft-=18;
 }
 function finish(e,cancel=false){if(!gesture||e.pointerId!==gesture.id)return;
  const moved=gesture.moved;gesture=null;tab.classList.remove('dragging');
  if(root.hasPointerCapture(e.pointerId))root.releasePointerCapture(e.pointerId);
  root.onpointermove=root.onpointerup=root.onpointercancel=root.onlostpointercapture=null;
  if(moved){const order=Array.from(root.children).map(el=>el.dataset.key);
   if(!cancel){items=order.map(k=>items.find(i=>i.key===k));emit('reorder',{order})}else render()}
 }
 const close=document.createElement('button');close.textContent='×';close.className='close';close.title='Quitar '+item.code;
 close.setAttribute('aria-label','Quitar '+item.code);close.onclick=()=>emit('remove',{key:item.key});tab.append(grip,label,close);root.append(tab)});
 send('streamlit:setFrameHeight',{height:root.scrollHeight+4})}
window.addEventListener('message',e=>{if(e.data.type==='streamlit:render'){items=e.data.args.items;active=e.data.args.active;render()}});
send('streamlit:componentReady',{apiVersion:1});send('streamlit:setFrameHeight',{height:70});
</script></body></html>'''


def aplicar_evento_visor(viewer, active, event):
    """Valida eventos del componente antes de cambiar cursos u orden."""
    viewer = dict(viewer)
    if not isinstance(event, dict):
        return viewer, active
    action, key = event.get("action"), event.get("key")
    if action == "remove" and isinstance(key, str) and key in viewer:
        del viewer[key]
    elif action == "select" and isinstance(key, str) and key in viewer:
        active = key
    elif action == "reorder":
        order = event.get("order")
        if isinstance(order, list) and all(isinstance(k, str) for k in order) and len(order) == len(viewer) and set(order) == set(viewer):
            viewer = {k: viewer[k] for k in order}
    if active not in viewer:
        active = next(iter(viewer), None)
    return viewer, active


def procesar_evento_visor():
    """Aplica la selección antes de reconstruir la página, sin otro rerun."""
    event = st.session_state.get("pestanas_cursos_v2")
    if not isinstance(event, dict) or event.get("id") == st.session_state.get("ultimo_evento_visor"):
        return
    viewer, active = aplicar_evento_visor(st.session_state.visor,
                                         st.session_state.get("visor_activo"), event)
    st.session_state.visor = viewer
    st.session_state.visor_activo = active
    st.session_state.ultimo_evento_visor = event.get("id")
    guardar_enlace()


TABLE_HTML = r'''<!doctype html><html lang="es"><head><meta charset="utf-8">
<style>
html,body{margin:0;padding:0;background:#0e1117;color:#e6edf3;font:14px Arial,sans-serif}#root{display:flow-root}
.popup{position:fixed;z-index:10000;background:#1b2432;color:#e6edf3;border:1px solid #66829f;border-radius:7px;
 padding:10px;box-shadow:0 6px 20px #0008;width:260px;box-sizing:border-box}
.popup button{background:#284763;color:white;border:1px solid #66829f;border-radius:4px;padding:7px;cursor:pointer}
.popup input[type=search]{box-sizing:border-box;width:100%;background:#0e1928;color:white;border:1px solid #66829f;padding:7px;margin:8px 0}
.popup .values{max-height:170px;overflow:auto;margin:8px 0}.popup label{display:flex;gap:8px;align-items:center;padding:5px;overflow-wrap:anywhere}
.popup input[type=checkbox]{accent-color:#78b9ff}.popup .buttons{display:flex;gap:6px;flex-wrap:wrap}
.popup.menu{width:210px}.popup.menu button{display:block;width:100%;text-align:left;margin:4px 0}
</style></head><body><div id="root"></div><div id="popup" class="popup" hidden></div><script>
const root=document.getElementById('root'),popup=document.getElementById('popup');
let lastHeight=0,options={},filters={},selected=new Set(),openColumn=null;
function send(type,payload={}){window.parent.postMessage({isStreamlitMessage:true,type,...payload},'*')}
function emit(action,extra={}){send('streamlit:setComponentValue',{value:{action,...extra,id:Date.now()+':'+Math.random()},dataType:'json'})}
function normalize(s){return String(s).normalize('NFD').replace(/[\u0300-\u036f]/g,'').toLowerCase()}
function resize(){const base=root.getBoundingClientRect().height;const menu=popup.hidden?0:popup.getBoundingClientRect().bottom;
 const height=Math.min(650,Math.max(40,Math.ceil(Math.max(base,menu))+6));
 if(height!==lastHeight){lastHeight=height;send('streamlit:setFrameHeight',{height})}}
function hide(){popup.hidden=true;openColumn=null;resize()}
function place(x,y){popup.hidden=false;popup.style.left=Math.max(0,Math.min(x,window.innerWidth-270))+'px';
 popup.style.top=Math.max(0,Math.min(y,250))+'px';requestAnimationFrame(resize)}
function makeButton(text,fn){const b=document.createElement('button');b.type='button';b.textContent=text;b.onclick=fn;return b}
function reflect(){root.querySelectorAll('input[data-section]').forEach(i=>{i.checked=selected.has(i.dataset.section);i.closest('tbody').classList.toggle('seleccionada',i.checked)});
 const all=root.querySelector('[data-select-all]');const boxes=[...root.querySelectorAll('input[data-section]')];
 if(all){const count=boxes.filter(i=>i.checked).length;all.checked=boxes.length>0&&count===boxes.length;all.indeterminate=count>0&&count<boxes.length}}
function showFilter(button){const column=button.dataset.filterColumn,values=options[column]||[];
 const chosen=new Set(Array.isArray(filters[column])?filters[column]:values);openColumn=column;
 popup.className='popup';popup.replaceChildren();const title=document.createElement('strong');title.textContent=column;
 const search=document.createElement('input');search.type='search';search.placeholder='Buscar valores…';search.setAttribute('aria-label','Buscar valores de '+column);
 const allLabel=document.createElement('label'),all=document.createElement('input');all.type='checkbox';allLabel.append(all,document.createTextNode('Seleccionar todos los valores mostrados'));
 const list=document.createElement('div');list.className='values';
 function draw(){list.replaceChildren();const tokens=normalize(search.value).split(/\s+/).filter(Boolean);
  const matches=values.filter(v=>tokens.every(t=>normalize(v).includes(t)));
  for(const value of matches){const label=document.createElement('label'),box=document.createElement('input');box.type='checkbox';box.checked=chosen.has(value);
   box.onchange=()=>{if(box.checked)chosen.add(value);else chosen.delete(value);sync(matches)};
   label.append(box,document.createTextNode(value));list.append(label)}sync(matches);resize()}
 function sync(matches){all.checked=matches.length>0&&matches.every(v=>chosen.has(v));all.indeterminate=matches.some(v=>chosen.has(v))&&!all.checked}
 all.onchange=()=>{const tokens=normalize(search.value).split(/\s+/).filter(Boolean);values.filter(v=>tokens.every(t=>normalize(v).includes(t))).forEach(v=>{if(all.checked)chosen.add(v);else chosen.delete(v)});draw()};
 search.oninput=draw;const buttons=document.createElement('div');buttons.className='buttons';
 buttons.append(makeButton('Aplicar',()=>{const next={...filters};if(values.every(v=>chosen.has(v)))delete next[column];else next[column]=values.filter(v=>chosen.has(v));emit('filters',{filters:next});hide()}),
 makeButton('Quitar filtro',()=>{const next={...filters};delete next[column];emit('filters',{filters:next});hide()}),makeButton('Cancelar',hide));
 popup.append(title,search,allLabel,list,buttons);const rect=button.getBoundingClientRect();place(rect.left,rect.bottom+3);draw();search.focus()}
root.addEventListener('click',e=>{const filter=e.target.closest('[data-filter-column]');if(filter){showFilter(filter);return}
 const button=e.target.closest('button[data-action]');if(button){hide();emit(button.dataset.action)}});
root.addEventListener('change',e=>{const checkbox=e.target.closest('input[data-section]');
 if(checkbox){if(checkbox.checked)selected.add(checkbox.dataset.section);else selected.delete(checkbox.dataset.section);reflect();emit('selection',{sections:[...selected]})}
 if(e.target.matches('[data-select-all]')){root.querySelectorAll('input[data-section]').forEach(i=>{if(e.target.checked)selected.add(i.dataset.section);else selected.delete(i.dataset.section)});reflect();emit('selection',{sections:[...selected]})}});
root.addEventListener('contextmenu',e=>{const body=e.target.closest('tbody[data-section-key]');if(!body)return;e.preventDefault();
 const key=body.dataset.sectionKey;if(!selected.has(key))selected=new Set([key]);reflect();popup.className='popup menu';popup.replaceChildren();openColumn=null;
 const title=document.createElement('strong');title.textContent=selected.size+' sección(es) seleccionada(s)';popup.append(title);
 for(const [value,label] of [['incluir','Incluir'],['descartar','Descartar (gris)'],['borrar','Borrar']])popup.append(makeButton(label,()=>{emit('status',{sections:[...selected],value});hide()}));place(e.clientX,e.clientY)});
document.addEventListener('pointerdown',e=>{if(!popup.hidden&&!popup.contains(e.target)&&!e.target.closest('[data-filter-column]'))hide()});
document.addEventListener('keydown',e=>{if(e.key==='Escape')hide()});
new ResizeObserver(resize).observe(root);window.addEventListener('resize',resize);
window.addEventListener('message',e=>{if(e.data.type!=='streamlit:render')return;
 options=e.data.args.options||{};filters=e.data.args.filters||{};selected=new Set(e.data.args.selected||[]);
 const scroller=root.querySelector('.oferta-scroll'),scroll=scroller?[scroller.scrollLeft,scroller.scrollTop]:[0,0];
 const parsed=new DOMParser().parseFromString(e.data.args.html,'text/html');
 document.querySelectorAll('style[data-table]').forEach(s=>s.remove());
 parsed.querySelectorAll('style').forEach(s=>{const style=document.createElement('style');style.dataset.table='1';style.textContent=s.textContent;document.head.append(style)});
 root.replaceChildren(...Array.from(parsed.body.childNodes).map(n=>document.importNode(n,true)));
 const next=root.querySelector('.oferta-scroll');if(next){next.scrollLeft=scroll[0];next.scrollTop=scroll[1]}reflect();hide();requestAnimationFrame(resize);
});send('streamlit:componentReady',{apiVersion:1});
</script></body></html>'''


@st.cache_resource
def crear_componente(nombre, contenido):
    # El .py contiene todo: crea únicamente el recurso de interfaz en una carpeta temporal.
    import hashlib
    version = hashlib.sha256(contenido.encode()).hexdigest()[:12]
    folder = Path(tempfile.gettempdir()) / ("uniandes_" + version)
    folder.mkdir(exist_ok=True)
    (folder / "index.html").write_text(contenido, encoding="utf-8")
    # Cambiar la ruta del recurso no recarga un iframe ya abierto. Versionar
    # también el nombre obliga a cargar sus nuevos manejadores de eventos.
    return components.declare_component(f"{nombre}_{version}", path=str(folder))


def procesar_evento_tabla(widget_key, scope, columns, section_keys):
    event = st.session_state.get(widget_key)
    if not isinstance(event, dict) or event.get("id") == st.session_state.get(f"ultimo:{widget_key}"):
        return
    st.session_state[f"ultimo:{widget_key}"] = event.get("id")
    filters = st.session_state.setdefault("filtros_columnas", {}).setdefault(scope, {})
    states = st.session_state.setdefault("estados_secciones", {})
    action = event.get("action")
    if action == "filters" and isinstance(event.get("filters"), dict):
        filters.clear()
        filters.update({column: [v[:500] for v in value if isinstance(v, str)][:20000]
                        for column, value in event["filters"].items()
                        if column in columns and isinstance(value, list)})
    elif action == "clear_filters":
        filters.clear()
    elif action == "selection" and isinstance(event.get("sections"), list):
        st.session_state["seleccion_secciones"] = [key for key in event["sections"]
                                                   if isinstance(key, str) and key in section_keys]
    elif action == "status" and isinstance(event.get("sections"), list):
        if event.get("value") in ["incluir", "descartar", "borrar"]:
            selected = [key for key in event["sections"] if isinstance(key, str) and key in section_keys]
            for key in selected:
                states[key] = event["value"]
            st.session_state["seleccion_secciones"] = [] if event["value"] == "borrar" else selected
    elif action == "restore":
        for section in section_keys:
            if states.get(section) == "borrar":
                states.pop(section)


def render_tabla(df, key, context=None):
    # El navegador mide la altura real, incluyendo celdas unificadas y texto largo.
    # Las tablas grandes conservan desplazamiento; las pequeñas ocupan solo lo necesario.
    import hashlib
    scope = key + hashlib.sha256(json.dumps(context, sort_keys=True).encode()).hexdigest()[:12]
    widget_key = f"interactiva:{scope}"
    filters = st.session_state.setdefault("filtros_columnas", {}).setdefault(scope, {})
    states = st.session_state.setdefault("estados_secciones", {})
    section_keys = set(df["Período"].astype(str) + "|" + df["NRC"].astype(str))
    visible = filtrar_columnas(df, filters, states)
    deleted = sum(states.get(section) == "borrar" for section in section_keys)
    selected = [section for section in st.session_state.get("seleccion_secciones", []) if section in section_keys]
    options = {column: sorted(df[column].map(valor_celda).unique(), key=normalizar) for column in df.columns}
    crear_componente("uniandes_interactive_table", TABLE_HTML)(
        html=tabla_unificada(visible, filters, states, deleted, selected),
        options=options, filters=filters, selected=selected, key=widget_key, default=None,
        on_change=lambda: procesar_evento_tabla(widget_key, scope, list(df.columns), section_keys))
    exported = visible.copy()
    exported["Estado"] = ["Descartada" if states.get(f"{row['Período']}|{row['NRC']}") == "descartar"
                           else "Incluida" for _, row in exported.iterrows()]
    return exported


def render_visor():
    st.divider()
    st.subheader("Visor general")
    viewer = st.session_state.visor
    if not viewer:
        st.info("Agrega cursos desde los resultados para reunirlos aquí.")
        return
    st.caption("Selecciona una pestaña para ver el curso. Pasa el cursor para quitarlo con ×; arrastra desde ⠿ para reordenar. "
               "También puedes mover una pestaña con Alt + flechas al seleccionarla con el teclado.")
    active = st.session_state.get("visor_activo")
    if active not in viewer:
        active = next(iter(viewer))
    st.session_state.visor_activo = active
    crear_componente("uniandes_course_tabs_v2", TABS_HTML)(
        items=[{"key": k, "code": q["curso"], "term": q["term"]} for k, q in viewer.items()],
        active=active, key="pestanas_cursos_v2", default=None, on_change=procesar_evento_visor)
    q = viewer[active]
    try:
        with st.spinner("Actualizando curso del visor…"):
            rows, updated = cargar_busqueda(q["term"], q["programa"], q, st.session_state.freshness)
            rows = [r for r in rows if curso_coincide(r, q["curso"]) and
                    (not q["profesor"].strip() or any(coincide(i.get("name", ""), q["profesor"])
                                                    for i in r.get("instructors") or []))]
            df = filtrar_encuentros(crear_dataframe(rows), q)
        st.caption(f"{q['curso']} · Período {q['term']} · Datos consultados: {updated}")
        if df.empty:
            st.info("Este curso ya no tiene resultados con los filtros guardados.")
        else:
            visible = render_tabla(df, key="tabla_visor", context=q)
            st.download_button("Descargar este curso", visible.to_csv(index=False).encode("utf-8-sig"),
                               f"{q['term']}_{q['curso']}.csv", "text/csv", key="csv_visor")
    except (requests.RequestException, ValueError, KeyError) as exc:
        st.error(f"No se pudo actualizar el curso del visor: {exc}")


def main():
    st.session_state.setdefault("visor", {})
    render_busqueda()
    render_visor()


if __name__ == "__main__":
    main()


