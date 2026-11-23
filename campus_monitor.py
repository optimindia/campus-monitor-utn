"""
Monitor del Campus Virtual UTN FRM (Moodle) -> avisos por Telegram.

Cada INTERVALO segundos revisa:
  - Foros de todas las materias: discusiones y respuestas nuevas, con el texto
    completo. Si PUBLICAN notas llega con 🚨 y, si hay adjuntos o links a
    Drive, busca tu legajo adentro y te manda tu nota directamente.
  - Material nuevo en cada materia (archivos, páginas, links).
  - Notificaciones del campus y mensajes privados.
Cada NOTAS_CADA segundos además revisa "fuentes de notas":
  - Archivos/páginas de las materias con nombre tipo "notas", "resultado",
    "situación para global", "planilla de notas"... (PDF, Excel, páginas con
    buscador por legajo como las de Paradigmas).
  - Carpetas de Google Drive enlazadas en las materias (planillas de notas).
  - El libro de calificaciones del campus.
  Si tu fila aparece o cambia en alguna, te avisa con tu nota.

Entra con el login web normal (el campus tiene deshabilitada la API de la app
móvil) y usa la API AJAX interna + algunas páginas HTML.

Uso:
  python campus_monitor.py            -> arranca el monitor
  python campus_monitor.py --resumen  -> te manda por Telegram todas tus notas encontradas
  python campus_monitor.py --chat-id  -> muestra tu chat_id de Telegram
  python campus_monitor.py --test     -> manda un mensaje de prueba y sale
"""

import html
import http.client
import http.cookiejar
import io
import json
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
ENV_FILE = BASE_DIR / ".env"
STATE_FILE = BASE_DIR / "estado.json"
LOG_FILE = BASE_DIR / "monitor.log"

CAMPUS_URL = "https://campusvirtual.frm.utn.edu.ar"
NAVEGADOR = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
MAX_ARCHIVO = 15 * 1024 * 1024
ERRORES_RED = (urllib.error.URLError, http.client.HTTPException, TimeoutError, ConnectionError, OSError)


# ----------------------------------------------------------------- utilidades
def log(msg):
    linea = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    try:
        print("\r" + linea.ljust(70), flush=True)
    except UnicodeEncodeError:
        print(linea.encode("ascii", "replace").decode(), flush=True)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(linea + "\n")
    except OSError:
        pass


def cargar_env():
    if not ENV_FILE.exists():
        sys.exit(f"No encuentro {ENV_FILE}. Copiá .env.ejemplo como .env y completalo.")
    env = {}
    for linea in ENV_FILE.read_text(encoding="utf-8-sig").splitlines():
        linea = linea.strip()
        if not linea or linea.startswith("#") or "=" not in linea:
            continue
        k, v = linea.split("=", 1)
        env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def limpiar_html(texto):
    texto = re.sub(r"<br\s*/?>|</p>|</tr>|</li>", "\n", texto or "", flags=re.I)
    texto = re.sub(r"<[^>]+>", "", texto)
    texto = html.unescape(texto).replace("\xa0", " ")
    texto = "\n".join(" ".join(linea.split()) for linea in texto.splitlines())
    return re.sub(r"\n{3,}", "\n\n", texto).strip()


def sin_acentos(texto):
    t = unicodedata.normalize("NFKD", texto or "")
    return "".join(c for c in t if not unicodedata.combining(c)).lower()


def post(url, datos, timeout=30):
    body = urllib.parse.urlencode(datos).encode()
    req = urllib.request.Request(url, data=body, headers={"User-Agent": "campus-monitor"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def leer_respuesta(r):
    largo = int(r.headers.get("Content-Length") or 0)
    if largo > MAX_ARCHIVO:
        raise ValueError(f"archivo demasiado grande ({largo} bytes)")
    data = r.read(MAX_ARCHIVO + 1)
    if len(data) > MAX_ARCHIVO:
        raise ValueError("archivo demasiado grande")
    return data


# ------------------------------------------------------------------- telegram
class Telegram:
    def __init__(self, token, chat_id):
        self.token = token
        self.chat_id = chat_id

    def enviar(self, texto):
        # Telegram corta en 4096 caracteres
        for i in range(0, len(texto), 4000):
            try:
                post(
                    f"https://api.telegram.org/bot{self.token}/sendMessage",
                    {
                        "chat_id": self.chat_id,
                        "text": texto[i:i + 4000],
                        "parse_mode": "HTML",
                        "disable_web_page_preview": "true",
                    },
                )
            except Exception as e:  # noqa: BLE001
                log(f"Error enviando a Telegram: {e}")

    def enviar_documento(self, nombre, data, epigrafe=""):
        limite = 1000
        frontera = uuid.uuid4().hex
        partes = []
        for k, v in {"chat_id": self.chat_id, "caption": epigrafe[:limite], "parse_mode": "HTML"}.items():
            partes.append(
                f'--{frontera}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
            )
        nombre = nombre.replace('"', "'")
        partes.append(
            f'--{frontera}\r\nContent-Disposition: form-data; name="document"; filename="{nombre}"\r\n'
            f"Content-Type: application/octet-stream\r\n\r\n".encode() + data + b"\r\n"
        )
        partes.append(f"--{frontera}--\r\n".encode())
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{self.token}/sendDocument",
            data=b"".join(partes),
            headers={"Content-Type": f"multipart/form-data; boundary={frontera}", "User-Agent": "campus-monitor"},
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                r.read()
        except Exception as e:  # noqa: BLE001
            log(f"Error enviando archivo a Telegram: {e}")

    def mostrar_chat_id(self):
        url = f"https://api.telegram.org/bot{self.token}/getUpdates"
        with urllib.request.urlopen(url, timeout=30) as r:
            data = json.loads(r.read().decode("utf-8"))
        chats = {}
        for upd in data.get("result", []):
            msg = upd.get("message") or upd.get("edited_message") or {}
            chat = msg.get("chat")
            if chat:
                chats[chat["id"]] = chat.get("first_name") or chat.get("title") or ""
        if not chats:
            print("No hay mensajes. Mandale cualquier cosa a tu bot en Telegram y volvé a correr esto.")
        for cid, nombre in chats.items():
            print(f"chat_id = {cid}   ({nombre})")


# --------------------------------------------------------------------- moodle
class ErrorLogin(Exception):
    pass


class Moodle:
    """Entra al campus como un navegador (cookie de sesión) y usa la API AJAX
    interna de Moodle. El campus tiene deshabilitado el servicio de la app móvil,
    así que /login/token.php no sirve."""

    def __init__(self, usuario, clave):
        self.usuario = usuario
        self.clave = clave
        self.sesskey = None
        self.userid = None
        self.opener = None
        self._cache_estado = {}

    def _pedir(self, url, datos=None, headers=None, timeout=45, intentos=3):
        """-> (bytes, url_final, content_type). El campus a veces corta la
        respuesta a la mitad: reintentar."""
        for i in range(intentos):
            try:
                req = urllib.request.Request(url, data=datos, headers=headers or {})
                with self.opener.open(req, timeout=timeout) as r:
                    return leer_respuesta(r), r.geturl(), r.headers.get("Content-Type", "")
            except urllib.error.HTTPError:
                raise  # 404/403: reintentar no sirve
            except (http.client.HTTPException, TimeoutError, urllib.error.URLError, ConnectionError):
                if i == intentos - 1:
                    raise
                time.sleep(5 * (i + 1))

    def _abrir(self, url, datos=None, headers=None, timeout=45):
        return self._pedir(url, datos, headers, timeout)[0].decode("utf-8", errors="replace")

    def login(self):
        self.sesskey = None
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
        )
        self.opener.addheaders = [("User-Agent", NAVEGADOR)]
        pagina = self._abrir(f"{CAMPUS_URL}/login/index.php")
        tok = re.search(r'name="logintoken" value="([^"]*)"', pagina)
        datos = {"username": self.usuario, "password": self.clave, "anchor": ""}
        if tok:
            datos["logintoken"] = tok.group(1)
        pagina = self._abrir(f"{CAMPUS_URL}/login/index.php", urllib.parse.urlencode(datos).encode())
        if "login/logout.php" not in pagina:
            err = re.search(r'id="loginerrormessage"[^>]*>(.*?)<', pagina, re.S)
            raise ErrorLogin(limpiar_html(err.group(1)) if err else "usuario o contraseña incorrectos")
        pagina = self._abrir(f"{CAMPUS_URL}/my/")
        sk = re.search(r'"sesskey":"([^"]+)"', pagina)
        uid = re.search(r'data-userid="(\d+)"', pagina) or re.search(r'"userid":(\d+)', pagina)
        if not sk or not uid:
            raise ErrorLogin("entré al campus pero no encontré la sesión (sesskey/userid)")
        self.sesskey = sk.group(1)
        self.userid = int(uid.group(1))
        log(f"Login OK (userid {self.userid})")

    def pagina(self, url):
        """Abre una página del campus; si la sesión venció, vuelve a entrar."""
        p = self._abrir(url)
        if "login/logout.php" not in p:
            self.login()
            p = self._abrir(url)
        return p

    def descargar(self, url):
        """-> (nombre, bytes) de un archivo del campus (sigue redirecciones de recursos)."""
        data, final, tipo = self._pedir(url, timeout=90)
        if "text/html" in tipo and "pluginfile.php" not in final:
            # recurso mostrado dentro de una página: buscar el link al archivo
            m = re.search(r'(https?://[^"\']+/pluginfile\.php/[^"\']+)', data.decode("utf-8", "replace"))
            if m:
                data, final, tipo = self._pedir(html.unescape(m.group(1)), timeout=90)
        nombre = urllib.parse.unquote(final.split("?")[0].rstrip("/").split("/")[-1]) or "archivo"
        return nombre, data

    def llamar(self, funcion, _reintento=True, **params):
        if not self.sesskey:
            self.login()
        body = json.dumps([{"index": 0, "methodname": funcion, "args": params}]).encode()
        texto = self._abrir(
            f"{CAMPUS_URL}/lib/ajax/service.php?sesskey={self.sesskey}&info={funcion}",
            body,
            {
                "Content-Type": "application/json",
                "Accept": "application/json",
                "X-Requested-With": "XMLHttpRequest",
                "Origin": CAMPUS_URL,
                "Referer": f"{CAMPUS_URL}/my/",
            },
        )
        r = json.loads(texto)
        r = r[0] if isinstance(r, list) else r
        if r.get("error"):
            exc = r.get("exception") or {}
            code = exc.get("errorcode", "")
            if code in ("servicerequireslogin", "invalidsesskey", "requireloginerror") and _reintento:
                log("Sesión vencida, vuelvo a entrar")
                self.login()
                return self.llamar(funcion, _reintento=False, **params)
            raise RuntimeError(f"{funcion}: {code} - {exc.get('message', '')}")
        return r.get("data")

    def cursos(self):
        r = self.llamar(
            "core_course_get_enrolled_courses_by_timeline_classification",
            classification="inprogress", limit=0, offset=0, sort="fullname",
        )
        return r.get("courses", [])

    def actividades(self, courseid, ciclo):
        """Todas las actividades visibles de la materia (índice del curso).
        Se cachea por ciclo para no pedirlo dos veces en la misma vuelta."""
        clave = (courseid, ciclo)
        if clave not in self._cache_estado:
            st = json.loads(self.llamar("core_courseformat_get_state", courseid=courseid))
            secciones = {s["id"]: s.get("title", "") for s in st.get("section", [])}
            self._cache_estado = {k: v for k, v in self._cache_estado.items() if k[1] == ciclo}
            self._cache_estado[clave] = [
                {
                    "id": str(cm["id"]),
                    "tipo": cm.get("module", ""),
                    "nombre": limpiar_html(cm.get("name", "")),
                    "seccion": limpiar_html(secciones.get(cm.get("sectionid"), "")),
                    "url": cm.get("url") or "",
                }
                for cm in st.get("cm", [])
                if cm.get("uservisible", True)
            ]
        return self._cache_estado[clave]

    def notas_curso(self, courseid):
        """Lee el reporte de calificaciones (HTML) -> {itemid: (nombre, nota)}"""
        pagina = self.pagina(f"{CAMPUS_URL}/grade/report/user/index.php?id={courseid}")
        nombres = {}
        for iid, celda in re.findall(
            r'<th[^>]*column-itemname[^>]*id="row_(\d+)_\d+"[^>]*>(.*?)</th>', pagina, re.S
        ):
            t = re.search(r'class="gradeitemheader[^"]*"[^>]*>(.*?)</', celda, re.S)
            nombres[iid] = limpiar_html(t.group(1) if t else celda)
        notas = {}
        for iid, valor in re.findall(
            r'<td[^>]*column-grade[^>]*headers="[^"]*row_(\d+)_\d+[^"]*"[^>]*>(.*?)</td>', pagina, re.S
        ):
            valor = limpiar_html(valor).strip()
            notas[iid] = (nombres.get(iid, "Ítem"), "" if valor in ("", "-") else valor)
        return notas

    def discusiones(self, cmid):
        """-> (nombre del foro, {discusion_id: (titulo, id_ultimo_post)})"""
        pagina = self.pagina(f"{CAMPUS_URL}/mod/forum/view.php?id={cmid}")
        t = re.search(r"<title>(.*?)</title>", pagina, re.S)
        nombre = limpiar_html(t.group(1)).split("|")[0].strip() if t else "Foro"
        res = {}
        for fila in re.split(r'<tr[^>]*data-region="discussion-list-item"', pagina)[1:]:
            did = re.search(r'data-discussionid="(\d+)"', fila)
            if not did:
                continue
            did = did.group(1)
            tit = re.search(r'discuss\.php\?d=\d+"[^>]*title="([^"]*)"', fila)
            ult = re.findall(rf"discuss\.php\?d={did}&(?:amp;)?parent=(\d+)", fila)
            res[did] = (
                html.unescape(tit.group(1)) if tit else "Discusión",
                int(ult[-1]) if ult else 0,
            )
        return nombre, res

    def posts(self, did):
        """-> lista de dicts {id, asunto, autor, texto, adjuntos, links} de una discusión."""
        pagina = self.pagina(f"{CAMPUS_URL}/mod/forum/discuss.php?d={did}")
        res = []
        for bloque in re.split(r"<article\b", pagina)[1:]:
            pid = re.search(r'data-post-id="(\d+)"', bloque)
            if not pid:
                continue
            tit = re.search(r'data-region-content="forum-post-core-subject"[^>]*>(.*?)</h', bloque, re.S)
            autor = re.search(r'user/view\.php\?id=\d+[^"]*">(.*?)</a>', bloque, re.S)
            cuerpo = ""
            if 'class="post-content-container">' in bloque:
                cuerpo = bloque.split('class="post-content-container">', 1)[1]
                for fin in ('data-region="post-actions-container"', "<footer", "</article>"):
                    cuerpo = cuerpo.split(fin, 1)[0]
                cuerpo = re.sub(r"<div[^>]*$", "", cuerpo)  # resto de etiqueta cortada
            urls = [html.unescape(u) for u in re.findall(r'(?:href|src)="(https?://[^"]+)"', bloque)]
            res.append({
                "id": int(pid.group(1)),
                "asunto": limpiar_html(tit.group(1)) if tit else "",
                "autor": limpiar_html(autor.group(1)) if autor else "",
                "texto": limpiar_html(cuerpo),
                "adjuntos": sorted({
                    u for u in urls
                    if "pluginfile.php" in u and re.search(r"\.(pdf|xlsx|csv|txt|html?)(\?|$)", u, re.I)
                }),
                "links": sorted({u for u in urls if re.search(r"(docs|drive)\.google\.com/", u)}),
            })
        return res


# ------------------------------------------------- buscar MI nota en archivos
class Yo:
    def __init__(self, legajo, nombre):
        self.legajo = re.sub(r"\D", "", legajo or "")
        self.palabras = [p for p in sin_acentos(nombre).split() if len(p) > 2]

    def __bool__(self):
        return bool(self.legajo or self.palabras)

    def coincide(self, texto):
        if self.legajo and re.search(rf"(?<!\d){self.legajo}(?!\d)", texto or ""):
            return True
        t = sin_acentos(texto)
        return bool(self.palabras) and all(p in t for p in self.palabras)


def _num(v):
    v = (v or "").strip()
    if re.fullmatch(r"-?\d+\.\d+", v):
        f = float(v)
        return str(int(f)) if f.is_integer() else f"{f:.2f}".rstrip("0").rstrip(".")
    return v


def filas_xlsx(data):
    """Lee un .xlsx con la librería estándar -> (hoja, {columna: valor})"""
    ns = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    z = zipfile.ZipFile(io.BytesIO(data))
    compartidos = []
    if "xl/sharedStrings.xml" in z.namelist():
        for si in ET.fromstring(z.read("xl/sharedStrings.xml")).iter(f"{ns}si"):
            compartidos.append("".join(t.text or "" for t in si.iter(f"{ns}t")))
    hojas = sorted(
        (n for n in z.namelist() if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", n)),
        key=lambda n: int(re.search(r"\d+", n.rsplit("/", 1)[1]).group()),
    )
    for hoja in hojas:
        for fila in ET.fromstring(z.read(hoja)).iter(f"{ns}row"):
            celdas = {}
            for c in fila.iter(f"{ns}c"):
                v, tipo = c.find(f"{ns}v"), c.get("t")
                if tipo == "inlineStr":
                    val = "".join(t.text or "" for t in c.iter(f"{ns}t"))
                elif v is None:
                    val = ""
                elif tipo == "s":
                    val = compartidos[int(v.text)]
                else:
                    val = v.text or ""
                celdas[re.sub(r"\d", "", c.get("r", ""))] = val.strip()
            yield hoja, celdas


def buscar_en_xlsx(data, yo):
    res = []
    titulos, en_datos, hoja_actual = {}, False, None
    for hoja, col in filas_xlsx(data):
        if hoja != hoja_actual:
            titulos, en_datos, hoja_actual = {}, False, hoja
        valores = [v for v in col.values() if v]
        es_dato = any(re.fullmatch(r"\d{4,6}(\.0)?", v) for v in valores) and any(
            re.search(r"[A-Za-z]{3}", v) for v in valores
        )
        if not es_dato and any("legajo" in sin_acentos(v) for v in valores):
            titulos, en_datos = {}, False  # fila de encabezado de la tabla
        if not es_dato and not en_datos:
            for k, v in col.items():
                if v and not re.fullmatch(r"[\d.,]+", v):
                    titulos[k] = (titulos.get(k, "") + " " + " ".join(v.split())).strip()
        en_datos = en_datos or es_dato
        if valores and yo.coincide(" ".join(valores)):
            partes = []
            for k, v in col.items():
                if not v:
                    continue
                t = titulos.get(k, "")
                ts = sin_acentos(t)
                if "legajo" in ts or "apellido" in ts or "nombre" in ts or (not t and k == "A"):
                    continue
                partes.append(f"{t or k}: <b>{html.escape(_num(v))}</b>")
            if partes:
                res.append(" · ".join(partes))
    return res


def buscar_en_pdf(data, yo):
    try:
        from pypdf import PdfReader
    except ImportError:
        log("Falta pypdf (pip install pypdf): no puedo leer PDFs")
        return []
    lineas = []
    for p in PdfReader(io.BytesIO(data)).pages:
        lineas.extend((p.extract_text() or "").splitlines())
    res = []
    for i, linea in enumerate(lineas):
        if yo.coincide(linea):
            # el nombre a veces ocupa varias líneas y las notas quedan abajo:
            # juntar hasta tener números o hasta que empiece el próximo alumno
            j = i + 1
            while (
                j < min(i + 5, len(lineas))
                and len(re.findall(r"(?<![\d])\d{1,3}(?:[.,]\d+)?(?![\d])", linea.split(yo.legajo or "\0")[-1])) < 2
                and not re.match(r"\s*(\d{1,3}\s+)?\d{5}\b", lineas[j])
            ):
                linea += " " + lineas[j]
                j += 1
            enc = next((x for x in reversed(lineas[:i]) if "legajo" in sin_acentos(x)), "")
            txt = html.escape(" ".join(linea.split()))
            if enc:
                txt += f"\n<i>Columnas: {html.escape(' '.join(enc.split()))}</i>"
            res.append(txt)
    return res[:6]


def buscar_en_texto(texto, yo):
    es_html = re.search(r"<(html|body|div)\b", texto[:5000], re.I)
    if es_html:
        # solo el contenido principal (el menú de arriba tiene tu nombre) y solo por legajo
        if 'id="region-main"' in texto:
            texto = texto.split('id="region-main"', 1)[1].split('id="page-footer"', 1)[0]
        if not yo.legajo:
            return []
        yo = Yo(yo.legajo, "")
    # páginas tipo Paradigmas: datos = ["12345,Apellido, Nombre, P1=80", ...]
    res = [
        html.escape(e.split(",", 1)[1].strip() if "," in e else e)
        for e in re.findall(r'"([^"\n]{5,300})"', texto)
        if yo.coincide(e)
    ]
    if not res:
        res = [html.escape(" ".join(l.split())) for l in limpiar_html(texto).splitlines() if yo.coincide(l)]
    return res[:6]


def buscar_mi_nota(data, yo):
    """Busca tu legajo/nombre en un archivo -> lista de líneas (HTML para Telegram)."""
    if not yo or not data:
        return []
    try:
        if data[:4] == b"%PDF":
            return buscar_en_pdf(data, yo)
        if data[:2] == b"PK":
            z = zipfile.ZipFile(io.BytesIO(data))
            if any(n.startswith("xl/") for n in z.namelist()):
                return buscar_en_xlsx(data, yo)
            return []
        return buscar_en_texto(data.decode("utf-8", errors="replace"), yo)
    except Exception as e:  # noqa: BLE001 - archivo raro: no frenar el monitor
        log(f"No pude leer un archivo: {type(e).__name__}: {e}")
        return []


# -------------------------------------------------------------- google drive
def _google(url):
    req = urllib.request.Request(url, headers={"User-Agent": NAVEGADOR})
    with urllib.request.urlopen(req, timeout=60) as r:
        return leer_respuesta(r)


def archivos_google(url, solo_notas=True):
    """De un link de Google (carpeta, planilla, archivo) -> lista de (nombre, url_descarga).
    Solo funciona si está compartido como 'cualquiera con el enlace'."""
    m = re.search(r"/folders/([\w-]+)", url)
    if m:
        p = _google(f"https://drive.google.com/embeddedfolderview?id={m.group(1)}").decode("utf-8", "replace")
        res = []
        for href, nombre in re.findall(r'<a href="([^"]+)"[^>]*>.*?flip-entry-title">(.*?)</div>', p, re.S):
            nombre = html.unescape(nombre)
            if "/folders/" in href or (solo_notas and not NOMBRE_NOTAS.search(nombre)):
                continue
            res.extend((nombre, u) for _, u in archivos_google(html.unescape(href), solo_notas=False))
        return res
    m = re.search(r"/spreadsheets/d/([\w-]+)", url)
    if m:
        return [("planilla.xlsx", f"https://docs.google.com/spreadsheets/d/{m.group(1)}/export?format=xlsx")]
    m = re.search(r"/document/d/([\w-]+)", url)
    if m:
        return [("documento.txt", f"https://docs.google.com/document/d/{m.group(1)}/export?format=txt")]
    m = re.search(r"/file/d/([\w-]+)", url) or re.search(r"[?&]id=([\w-]+)", url)
    if m:
        return [("archivo", f"https://drive.google.com/uc?export=download&id={m.group(1)}")]
    return []


# ------------------------------------------------- ¿es un aviso de notas?
PALABRAS_NOTAS = re.compile(
    r"\bnotas?\b|calificaci|resultados?\b|aprobad|desaprobad|promoci[oó]n|\bregulares\b"
    r"|situaci[oó]n (pre.?global|final|para)|\bactas?\b|\bpromedios?\b",
    re.I,
)
# verbos/indicios de que efectivamente las publicaron (sacado del historial de avisos)
PUBLICACION = re.compile(
    r"publica|\bsub[oi]\b|subid|se ha subido|env[ií]o|les paso|compart|adjunt|se encuentran"
    r"|est[aá]n (en|public|dispon|cargad)|ya est[aá]n|planilla de notas|drive|mosaico|consulta de notas"
    r"|ingresando su",
    re.I,
)
# nombres de archivos/páginas que suelen tener notas
NOMBRE_NOTAS = re.compile(
    r"\bnotas?\b|calificaci|resultado|situaci[oó]n|promedio|\bactas?\b|\bcondici[oó]n(es)?\b|regulares|aprobad",
    re.I,
)


def es_pregunta(texto):
    return "?" in texto and re.search(r"\bprofe\b|quer[ií]a|consultarle|podr[ií]a", texto, re.I)


def clasificar(asunto, texto, hay_archivos=False):
    """-> 'publicadas' | 'menciona' | None"""
    t = f"{asunto}\n{texto}"
    if not PALABRAS_NOTAS.search(t):
        # ej. "2k1 - PRACTICA - Parcial 2 - recuperatorio" + PDF con la lista
        if hay_archivos and re.search(r"parcial|recuperatorio|examen|global|coloquio|integrador", asunto, re.I):
            return "publicadas"
        return None
    if es_pregunta(texto):
        return "menciona"
    if hay_archivos or PUBLICACION.search(t):
        return "publicadas"
    return "menciona"


# -------------------------------------------------------------------- chequeos
DESACTIVADOS = set()


class Contexto:
    def __init__(self, yo, notas_cada):
        self.yo = yo
        self.notas_cada = notas_cada
        self.ciclo = 0


def buscar_mi_nota_en_post(m, ctx, post):
    """Descarga adjuntos y links de Drive del post y busca tu nota."""
    encontrados, archivo = [], None
    for url in post["adjuntos"]:
        try:
            nombre, data = m.descargar(url)
        except (ValueError, *ERRORES_RED) as e:
            log(f"No pude bajar adjunto {url}: {e}")
            continue
        lineas = buscar_mi_nota(data, ctx.yo)
        if lineas:
            encontrados.append((nombre, lineas))
            if data[:4] == b"%PDF" and archivo is None:
                archivo = (nombre, data)
    for link in post["links"]:
        try:
            for nombre, url in archivos_google(link, solo_notas=False)[:10]:
                data = _google(url)
                lineas = buscar_mi_nota(data, ctx.yo)
                if lineas:
                    encontrados.append((nombre, lineas))
        except (ValueError, *ERRORES_RED) as e:
            log(f"No pude abrir link de Google {link[:60]}: {e}")
    return encontrados, archivo


def chequear_cursos(m, estado, avisos, ctx):
    """Foros (posts nuevos) y material nuevo de cada materia."""
    foros = estado.setdefault("foros", {})          # cmid -> {discusion: ultimo_post}
    materiales = estado.setdefault("materiales", {})  # courseid -> [cmid]
    for c in m.cursos():
        cid, materia = str(c["id"]), limpiar_html(c.get("fullname", ""))
        acts = m.actividades(c["id"], ctx.ciclo)

        # --- material nuevo
        conocido = cid in materiales
        previos = set(materiales.get(cid, []))
        for a in acts:
            if conocido and a["id"] not in previos and a["tipo"] not in ("label", "forum"):
                es_nota = NOMBRE_NOTAS.search(a["nombre"])
                avisos.append(
                    ("🚨🚨 <b>¡POSIBLES NOTAS!</b> 🚨🚨\n" if es_nota else "")
                    + f"🆕 <b>Nuevo en {html.escape(materia)}</b>\n"
                    f"{html.escape(a['nombre'])} <i>({html.escape(a['tipo'])} · {html.escape(a['seccion'])})</i>\n"
                    f"<a href=\"{CAMPUS_URL}/mod/{a['tipo']}/view.php?id={a['id']}\">Abrir en el campus</a>"
                )
        materiales[cid] = sorted({a["id"] for a in acts} | previos, key=int)

        # --- foros
        for a in acts:
            if a["tipo"] != "forum":
                continue
            cmid = a["id"]
            nombre_foro, discusiones = m.discusiones(cmid)
            foro_conocido = cmid in foros
            previas = foros.setdefault(cmid, {})
            for did, (titulo, ultimo) in discusiones.items():
                antes = previas.get(did)
                previas[did] = max(ultimo, antes or 0)
                if not foro_conocido or (antes is not None and ultimo <= antes):
                    continue  # foro recién descubierto o sin novedades
                for p in m.posts(did):
                    if antes is not None and p["id"] <= antes:
                        continue
                    avisos.append(armar_aviso_foro(m, ctx, materia, nombre_foro, did, p, titulo))


def armar_aviso_foro(m, ctx, materia, foro, did, p, titulo):
    asunto = p["asunto"] or titulo
    tipo = clasificar(asunto, p["texto"], bool(p["adjuntos"]))
    msg = "🚨🚨 <b>¡PUBLICARON NOTAS!</b> 🚨🚨\n" if tipo == "publicadas" else ""
    msg += (
        f"{'📝' if tipo else '📢'} <b>{html.escape(asunto)}</b>\n"
        f"<b>Materia:</b> {html.escape(materia)}\n"
        f"<b>Foro:</b> {html.escape(foro)}"
    )
    if p["autor"]:
        msg += f" · {html.escape(p['autor'])}"
    if p["texto"]:
        msg += f"\n\n{html.escape(p['texto'][:1500])}"
    archivo = None
    if tipo == "publicadas" and ctx.yo and (p["adjuntos"] or p["links"]):
        encontrados, archivo = buscar_mi_nota_en_post(m, ctx, p)
        if encontrados:
            msg += "\n\n🎯 <b>TU NOTA:</b>"
            for nombre, lineas in encontrados:
                msg += f"\n<u>{html.escape(nombre)}</u>\n" + "\n".join(lineas)
        else:
            msg += "\n\n🔎 No encontré tu legajo en los adjuntos (¿será de otra comisión?)."
    msg += f"\n<a href=\"{CAMPUS_URL}/mod/forum/discuss.php?d={did}#p{p['id']}\">Abrir en el campus</a>"
    return (msg, archivo) if archivo else msg


def fuentes_de_notas(m, ctx):
    """Archivos/páginas/carpetas que suelen tener notas -> lista de
    (clave, materia, nombre, funcion_que_devuelve_[(nombre_archivo, bytes)])"""
    fuentes = []
    for c in m.cursos():
        materia = limpiar_html(c.get("fullname", ""))
        for a in m.actividades(c["id"], ctx.ciclo):
            nombre_completo = f"{a['nombre']} {a['seccion']}"
            if a["tipo"] == "resource" and NOMBRE_NOTAS.search(nombre_completo):
                url = f"{CAMPUS_URL}/mod/resource/view.php?id={a['id']}&redirect=1"
                fuentes.append((f"cm{a['id']}", materia, a["nombre"], lambda u=url: [m.descargar(u)]))
            elif a["tipo"] == "page" and NOMBRE_NOTAS.search(nombre_completo):
                url = f"{CAMPUS_URL}/mod/page/view.php?id={a['id']}"
                fuentes.append((
                    f"cm{a['id']}", materia, a["nombre"],
                    lambda u=url, n=a["nombre"]: [(n, m.pagina(u).encode())],
                ))
            elif a["tipo"] == "url":
                data, final, _ = m._pedir(f"{CAMPUS_URL}/mod/url/view.php?id={a['id']}")
                pagina = final if CAMPUS_URL not in final else data.decode("utf-8", "replace")
                for link in sorted(set(re.findall(r'https://(?:drive|docs)\.google\.com/[^"\'<\s]+', pagina))):
                    link = html.unescape(link)
                    es_carpeta = "/folders/" in link
                    if not es_carpeta and not NOMBRE_NOTAS.search(nombre_completo):
                        continue
                    for nombre, u in archivos_google(link, solo_notas=es_carpeta):
                        fuentes.append((
                            f"g:{u}", materia, nombre if es_carpeta else a["nombre"],
                            lambda u=u, n=nombre: [(n, _google(u))],
                        ))
    return fuentes


def chequear_fuentes_notas(m, estado, avisos, ctx):
    if time.time() - estado.get("fuentes_ts", 0) < ctx.notas_cada:
        return
    if not ctx.yo:
        return
    mis = estado.setdefault("mis_notas", {})
    for clave, materia, nombre, bajar in fuentes_de_notas(m, ctx):
        try:
            archivos = bajar()
        except (ValueError, *ERRORES_RED) as e:
            log(f"No pude leer '{nombre}' ({materia}): {e}")
            continue
        lineas, adjunto = [], None
        for nom_arch, data in archivos:
            encontradas = buscar_mi_nota(data, ctx.yo)
            lineas += encontradas
            if encontradas and data[:4] == b"%PDF":
                adjunto = (nom_arch, data)
        texto = "\n".join(lineas)
        anterior = mis.get(clave, {}).get("texto")
        mis[clave] = {"materia": materia, "nombre": nombre, "texto": texto}
        if texto and texto != anterior:
            msg = (
                f"📊 <b>{'Cambió tu nota' if anterior else 'Apareció tu nota'}</b>\n"
                f"<b>Materia:</b> {html.escape(materia)}\n"
                f"<b>Dónde:</b> {html.escape(nombre)}\n\n🎯 {texto}"
            )
            avisos.append((msg, adjunto) if adjunto else msg)
    estado["fuentes_ts"] = time.time()


def chequear_notificaciones(m, estado, avisos, ctx):
    r = m.llamar(
        "message_popup_get_popup_notifications",
        useridto=m.userid, newestfirst=1, limit=30, offset=0,
    )
    vistas = set(estado.setdefault("notificaciones", []))
    for n in reversed(r.get("notifications", [])):
        if n["id"] in vistas:
            continue
        vistas.add(n["id"])
        if n.get("component") == "mod_forum" and "cursos" not in DESACTIVADOS:
            continue  # ya lo avisa chequear_cursos, con el texto completo
        titulo = limpiar_html(n.get("subject", "Notificación"))
        cuerpo = limpiar_html(n.get("smallmessage") or n.get("fullmessage") or "")
        texto = f"🔔 <b>{html.escape(titulo)}</b>"
        if clasificar(titulo, cuerpo) == "publicadas":
            texto = "🚨🚨 <b>¡POSIBLES NOTAS!</b> 🚨🚨\n" + texto
        if cuerpo and cuerpo != titulo:
            texto += f"\n{html.escape(cuerpo[:800])}"
        if n.get("contexturl"):
            texto += f"\n<a href=\"{html.escape(n['contexturl'])}\">Abrir en el campus</a>"
        avisos.append(texto)
    estado["notificaciones"] = sorted(vistas)[-500:]


def chequear_calificaciones(m, estado, avisos, ctx):
    """Libro de calificaciones del campus (algunas cátedras lo usan)."""
    if time.time() - estado.get("calificaciones_ts", 0) < ctx.notas_cada:
        return
    notas = estado.setdefault("notas", {})
    vistos = set(estado.setdefault("cursos_vistos", []))
    for c in m.cursos():
        try:
            items = m.notas_curso(c["id"])
        except (urllib.error.HTTPError, RuntimeError) as e:
            log(f"No pude leer notas de {c.get('shortname')}: {e}")
            continue
        curso_conocido = str(c["id"]) in vistos
        vistos.add(str(c["id"]))
        for iid, (nombre, valor) in items.items():
            clave = f"{c['id']}:{iid}"
            # ítem nuevo en una materia ya conocida = antes no tenía nota
            anterior = notas.get(clave, "" if curso_conocido else None)
            notas[clave] = valor
            if valor and anterior is not None and valor != anterior:
                materia = limpiar_html(c.get("fullname") or c.get("shortname", ""))
                accion = "Nota nueva" if not anterior else f"Nota modificada (antes: {html.escape(anterior)})"
                avisos.append(
                    f"📝 <b>{accion}</b>\n"
                    f"<b>Materia:</b> {html.escape(materia)}\n"
                    f"<b>Ítem:</b> {html.escape(nombre)}\n"
                    f"<b>Nota:</b> {html.escape(valor)}\n"
                    f"<a href=\"{CAMPUS_URL}/grade/report/user/index.php?id={c['id']}\">Ver calificaciones</a>"
                )
    estado["cursos_vistos"] = sorted(vistos)
    estado["calificaciones_ts"] = time.time()


def chequear_mensajes(m, estado, avisos, ctx):
    r = m.llamar("core_message_get_conversations", userid=m.userid, limitfrom=0, limitnum=20)
    ultimos = estado.setdefault("mensajes", {})
    for conv in r.get("conversations", []):
        cid = str(conv["id"])
        miembros = {mb["id"]: mb.get("fullname", "") for mb in conv.get("members", [])}
        msgs = sorted(conv.get("messages", []), key=lambda x: x["id"])
        if not msgs:
            continue
        previo = ultimos.get(cid)
        ultimos[cid] = msgs[-1]["id"]
        if previo is None:
            continue
        for msg in msgs:
            if msg["id"] <= previo or msg.get("useridfrom") == m.userid:
                continue
            de = miembros.get(msg.get("useridfrom")) or conv.get("name") or "Alguien"
            avisos.append(
                f"💬 <b>Mensaje de {html.escape(de)}</b>\n"
                f"{html.escape(limpiar_html(msg.get('text', ''))[:800])}\n"
                f"<a href=\"{CAMPUS_URL}/message/index.php\">Abrir mensajes</a>"
            )


CHEQUEOS = [
    ("cursos", chequear_cursos),
    ("fuentes", chequear_fuentes_notas),
    ("notificaciones", chequear_notificaciones),
    ("calificaciones", chequear_calificaciones),
    ("mensajes", chequear_mensajes),
]


# ---------------------------------------------------------------------- main
def enviar_avisos(tg, avisos):
    for a in avisos:
        if isinstance(a, tuple):
            texto, (nombre, data) = a
            tg.enviar(texto)
            tg.enviar_documento(nombre, data, "📎 Archivo original")
        else:
            tg.enviar(a)


def resumen(tg, m, ctx):
    estado = {}
    chequear_fuentes_notas(m, estado, [], ctx)
    encontrados = [v for v in estado.get("mis_notas", {}).values() if v["texto"]]
    if not encontrados:
        tg.enviar("📊 No encontré tu legajo en ninguna planilla de notas de tus materias actuales.")
        return
    partes = ["📊 <b>Resumen de tus notas encontradas en el campus</b>"]
    for v in sorted(encontrados, key=lambda v: (v["materia"], v["nombre"])):
        partes.append(f"\n<b>{html.escape(v['materia'])}</b> — {html.escape(v['nombre'])}\n{v['texto']}")
    tg.enviar("\n".join(partes))
    print(f"Resumen enviado ({len(encontrados)} fuentes con tu nota).")


def main():
    env = cargar_env()
    tg = Telegram(env.get("TELEGRAM_TOKEN", ""), env.get("TELEGRAM_CHAT_ID", ""))

    if "--chat-id" in sys.argv:
        tg.mostrar_chat_id()
        return
    if "--test" in sys.argv:
        tg.enviar("✅ Prueba OK: el bot está conectado al monitor del campus.")
        print("Mensaje de prueba enviado.")
        return

    for k in ("CAMPUS_USUARIO", "CAMPUS_CLAVE", "TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID"):
        if not env.get(k):
            sys.exit(f"Falta {k} en el archivo .env")

    intervalo = int(env.get("INTERVALO", "120"))
    ctx = Contexto(Yo(env.get("LEGAJO", ""), env.get("NOMBRE", "")), int(env.get("NOTAS_CADA", "600")))
    if not ctx.yo:
        log("Sin LEGAJO/NOMBRE en .env: aviso de notas publicadas, pero no busco tu nota adentro")
    m = Moodle(env["CAMPUS_USUARIO"], env["CAMPUS_CLAVE"])
    try:
        m.login()
    except ErrorLogin as e:
        tg.enviar(f"❌ No pude entrar al campus: {html.escape(str(e))}")
        sys.exit(f"Login fallido: {e}")

    if "--resumen" in sys.argv:
        resumen(tg, m, ctx)
        return

    primera_vez = not STATE_FILE.exists()
    estado = json.loads(STATE_FILE.read_text(encoding="utf-8")) if not primera_vez else {}
    errores_seguidos = 0
    logins_fallidos = 0

    tg.enviar(
        f"🟢 Monitor del campus iniciado. Reviso foros y material cada {intervalo // 60} min "
        f"y las planillas de notas cada {ctx.notas_cada // 60} min."
    )
    log("Monitor iniciado")

    while True:
        ctx.ciclo += 1
        avisos = []
        for nombre, fn in CHEQUEOS:
            if nombre in DESACTIVADOS:
                continue
            try:
                fn(m, estado, avisos, ctx)
                errores_seguidos = 0
                logins_fallidos = 0
            except ErrorLogin as e:
                # puede ser una página cortada por el servidor: tolerar algunos
                logins_fallidos += 1
                log(f"Login fallido ({logins_fallidos}): {e}")
                if logins_fallidos >= 5:
                    tg.enviar(f"❌ El campus rechaza el login: {html.escape(str(e))}. Monitor detenido.")
                    sys.exit(1)
            except RuntimeError as e:
                if "accessexception" in str(e) or "invalidrecord" in str(e) or "servicenotavailable" in str(e):
                    log(f"El campus no permite '{nombre}', lo desactivo: {e}")
                    DESACTIVADOS.add(nombre)
                else:
                    log(f"Error en {nombre}: {e}")
            except ERRORES_RED as e:
                errores_seguidos += 1
                log(f"Sin conexión ({nombre}): {e}")
                if errores_seguidos == 10:
                    tg.enviar("⚠️ Hace rato que no puedo conectarme al campus. Sigo intentando.")
            except Exception as e:  # noqa: BLE001 - que un error raro no tire abajo el monitor
                log(f"Error inesperado en {nombre}: {type(e).__name__}: {e}")

        if primera_vez:
            # La primera pasada solo guarda lo que ya existe, para no avisar de todo lo viejo
            log(f"Primera pasada: {len(avisos)} ítems existentes registrados sin avisar")
            primera_vez = False
        else:
            enviar_avisos(tg, avisos)
            if avisos:
                log(f"{len(avisos)} aviso(s) enviado(s)")

        STATE_FILE.write_text(json.dumps(estado, ensure_ascii=False, indent=1), encoding="utf-8")
        # línea de estado que se va pisando (solo en una ventana, no en el VPS/systemd)
        proxima = datetime.fromtimestamp(time.time() + intervalo)
        if sys.stdout.isatty():
            print(
                f"\r[{datetime.now():%H:%M:%S}] ✔ Revisión hecha. Próxima: {proxima:%H:%M:%S}   ",
                end="", flush=True,
            )
        else:
            log("Revisión hecha")  # en el VPS: una línea por vuelta para saber que está vivo
        time.sleep(intervalo)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("Monitor detenido por el usuario")
