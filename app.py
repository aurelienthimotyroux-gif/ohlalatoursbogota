from flask import Flask, render_template, request, url_for, flash, redirect, abort, send_from_directory, g, session
from flask_babel import Babel, _
import os, requests, logging, json, re, time
from base64 import b64encode
from datetime import timedelta, datetime
from werkzeug.middleware.proxy_fix import ProxyFix
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from flask_sqlalchemy import SQLAlchemy
from urllib.parse import urlparse

# ------------------------------------------------------------------
# Utils: parser "22 février 2020" / "30 julio 2019" / "17 Aug 2025"
# ------------------------------------------------------------------
_MONTHS = {
    # français
    "janvier":1,"février":2,"fevrier":2,"mars":3,"avril":4,"mai":5,"juin":6,
    "juillet":7,"août":8,"aout":8,"septembre":9,"octobre":10,"novembre":11,"décembre":12,"decembre":12,
    # espagnol
    "enero":1,"febrero":2,"marzo":3,"abril":4,"mayo":5,"junio":6,"julio":7,
    "agosto":8,"septiembre":9,"setiembre":9,"octubre":10,"noviembre":11,"diciembre":12,
    # anglais
    "january":1,"february":2,"march":3,"april":4,"may":5,"june":6,"july":7,
    "august":8,"september":9,"october":10,"november":11,"december":12,
    # abréviations
    "jan":1,"feb":2,"mar":3,"apr":4,"may":5,"jun":6,"jul":7,"aug":8,"sep":9,"sept":9,"oct":10,"nov":11,"dec":12,
}

def parse_date_str(date_str: str):
    if not date_str:
        return None
    s = date_str.strip().lower()
    # formats possibles:
    # "22 février 2020" / "30 julio 2019" / "17 Aug 2025"
    try:
        # jours/mois/années séparés par espace ou / -
        parts = re.split(r"[ \-/]+", s)
        if len(parts) < 3:
            return None
        d = int(re.sub(r"\D+", "", parts[0]))
        mo = parts[1]
        y = int(re.sub(r"\D+", "", parts[2]))
        m = _MONTHS.get(mo, None)
        if not m:
            return None
        return datetime(y, m, d)
    except Exception:
        return None

# ------------------------------------------------------------------
# App
# ------------------------------------------------------------------
app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "dev-secret")

# Proxy headers (Render/ingress)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

# Babel (langues)
app.config["BABEL_DEFAULT_LOCALE"] = "fr"
app.config["BABEL_SUPPORTED_LOCALES"] = ["fr", "en", "es"]
app.config["BABEL_TRANSLATION_DIRECTORIES"] = "translations"
babel = Babel(app)

@babel.localeselector
def get_locale():
    lang = request.args.get("lang")
    return lang if lang in app.config["BABEL_SUPPORTED_LOCALES"] else app.config["BABEL_DEFAULT_LOCALE"]

# Exposer helpers à Jinja
app.jinja_env.globals["get_locale"] = get_locale

def lang_url(lang_code: str):
    args = request.args.to_dict(flat=True)
    args["lang"] = lang_code
    endpoint = request.endpoint or "index"
    return url_for(endpoint, **args)
app.jinja_env.globals["lang_url"] = lang_url

# ------------------------------------------------------------------
# DB (Postgres via DATABASE_URL, sinon SQLite local)
# ------------------------------------------------------------------
def _normalized_db_url():
    raw = os.getenv("DATABASE_URL", "").strip()
    if not raw:
        return "sqlite:///comments.sqlite3"
    # ex: postgres:// -> postgresql://
    raw = re.sub(r"^postgres://", "postgresql://", raw)
    # forcer le driver psycopg (v3)
    raw = raw.replace("+psycopg2", "+psycopg")
    if raw.startswith("postgresql://") and "+psycopg" not in raw:
        raw = raw.replace("postgresql://", "postgresql+psycopg://", 1)
    return raw

app.config["SQLALCHEMY_DATABASE_URI"] = _normalized_db_url()
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {"pool_pre_ping": True}

db = SQLAlchemy(app)

class Comment(db.Model):
    __tablename__ = "comments"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    country = db.Column(db.String(120), default="")
    date_str = db.Column(db.String(32), nullable=False)   # ex: "12 mai 2025"
    rating = db.Column(db.Float, default=5.0)
    message = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)

# Traductions de commentaires (cache)
class CommentTranslation(db.Model):
    __tablename__ = "comment_translation"
    id = db.Column(db.Integer, primary_key=True)
    comment_id = db.Column(db.Integer, db.ForeignKey('comments.id', ondelete='CASCADE'), nullable=False)
    lang = db.Column(db.String(5), nullable=False)  # 'fr', 'en', 'es'
    text = db.Column(db.Text, nullable=False)

    __table_args__ = (db.UniqueConstraint('comment_id', 'lang', name='uq_comment_lang'),)

with app.app_context():
    db.create_all()

# ------------------------------------------------------------------
# PayPal & admin
# ------------------------------------------------------------------
PAYPAL_CLIENT_ID = os.getenv("PAYPAL_CLIENT_ID", "")
PAYPAL_MODE = os.getenv("PAYPAL_MODE", "sandbox")
ADMIN_DELETE_TOKEN = os.getenv("ADMIN_DELETE_TOKEN", "")

# Rendre le client-id dispo dans toutes les templates
@app.context_processor
def inject_paypal_client_id():
    return {
        "paypal_client_id": PAYPAL_CLIENT_ID or "AXcr1vyT3...fQLC94tyH4qoqxNAu-V8vVRMtm4kphjbOupFByJl6cAyyppQmE-YiOU7IaLOzuj"
    }

# ------------------------------------------------------------------
# Logging & headers
# ------------------------------------------------------------------
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO),
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("ohlalatours")

# ------------------------------------------------------------------
# Traduction automatique des commentaires (DeepL / Google) + cache
# ------------------------------------------------------------------
DEEPL_API_KEY = os.getenv("DEEPL_API_KEY")
GOOGLE_TRANSLATE_API_KEY = os.getenv("GOOGLE_TRANSLATE_API_KEY")
# ✅ Fallback gratuit LibreTranslate (aucune clé nécessaire)
LIBRETRANSLATE_URL = os.getenv("LIBRETRANSLATE_URL", "https://libretranslate.com")

# ✅ Cooldown après 429 (rate-limit) pour ne pas spammer l'instance publique
LIBRE_COOLDOWN_SECONDS = int(os.getenv("LIBRE_COOLDOWN_SECONDS", "900"))  # 15 min par défaut
_last_libre_429 = 0

def translate_via_libre(text, target_lang, source_lang=None, timeout=12):
    """
    Fallback gratuit via LibreTranslate (instance publique par défaut).
    Si un 429 est reçu, on active un cooldown global pendant LIBRE_COOLDOWN_SECONDS.
    target_lang/source_lang: 'fr' 'en' 'es'. Retourne (text, None) ou (None, None).
    """
    if not text or not target_lang:
        return None, None

    # ne pas insister si on a récemment été rate-limité
    now = time.time()
    if now - _last_libre_429 < LIBRE_COOLDOWN_SECONDS:
        return None, None

    try:
        url = f"{LIBRETRANSLATE_URL.rstrip('/')}/translate"
        payload = {
            "q": text,
            "source": (source_lang or "auto"),
            "target": target_lang.lower(),
            "format": "text",
        }
        resp = requests.post(
            url,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=timeout
        )

        if resp.status_code == 429:
            # activer le cooldown et ne pas réessayer immédiatement
            global _last_libre_429
            _last_libre_429 = now
            logging.warning("libretranslate_rate_limited: 429; cooldown %ss", LIBRE_COOLDOWN_SECONDS)
            return None, None

        resp.raise_for_status()
        data = resp.json()
        return data.get("translatedText"), None

    except requests.exceptions.HTTPError as e:
        r = getattr(e, "response", None)
        if r is not None and r.status_code == 429:
            global _last_libre_429
            _last_libre_429 = time.time()
            logging.warning("libretranslate_error 429 -> cooldown %ss", LIBRE_COOLDOWN_SECONDS)
            return None, None
        logging.warning("libretranslate_error: %s", e)
        return None, None
    except Exception as e:
        logging.warning("libretranslate_error: %s", e)
        return None, None

def translate_text_auto(text, target_lang, source_lang=None, timeout=12):
    """
    Traduit `text` vers `target_lang` en utilisant DeepL ou Google si des clés sont configurées.
    Fallback gratuit sur LibreTranslate si aucune clé n'est dispo.
    Retourne (translated_text, detected_source_lang) ou (None, None) si non dispo/erreur.
    """
    if not text or not target_lang:
        return None, None

    # DeepL (prioritaire si dispo)
    if DEEPL_API_KEY:
        try:
            resp = requests.post(
                'https://api-free.deepl.com/v2/translate',
                data={
                    'auth_key': DEEPL_API_KEY,
                    'text': text,
                    'target_lang': target_lang.upper(),
                    **({'source_lang': source_lang.upper()} if source_lang else {})
                },
                timeout=timeout
            )
            resp.raise_for_status()
            j = resp.json()
            tr = j['translations'][0]
            return tr['text'], tr.get('detected_source_language')
        except Exception as e:
            logging.warning("deepl_translate_error: %s", e)

    # Google Cloud Translate
    if GOOGLE_TRANSLATE_API_KEY:
        try:
            resp = requests.post(
                'https://translation.googleapis.com/language/translate/v2',
                params={'key': GOOGLE_TRANSLATE_API_KEY},
                json={
                    'q': text,
                    'target': target_lang.lower(),
                    **({'source': source_lang.lower()} if source_lang else {})
                },
                timeout=timeout
            )
            resp.raise_for_status()
            j = resp.json()
            tr = j['data']['translations'][0]
            return tr['translatedText'], tr.get('detectedSourceLanguage')
        except Exception as e:
            logging.warning("google_translate_error: %s", e)

    # ✅ Fallback gratuit: LibreTranslate (si pas de clés ou échec)
    tr, det = translate_via_libre(text, target_lang, source_lang=source_lang, timeout=timeout)
    if tr:
        return tr, det

    return None, None

class CommentView:
    """Wrapper pour exposer les mêmes attributs qu'un Comment, avec message (potentiellement) traduit."""
    def __init__(self, c, display_message, translated=False, source_lang=None):
        self.id = c.id
        self.name = getattr(c, 'name', '')
        self.country = getattr(c, 'country', '')
        self.date_str = getattr(c, 'date_str', '')
        self.rating = getattr(c, 'rating', 5.0)
        self.created_at = getattr(c, 'created_at', None)
        self.message = display_message
        # meta facultative
        self._translated = translated
        self._source_lang = source_lang

def get_comment_message_for_lang(comment, target_lang):
    """Renvoie (message, source_lang, translated_bool) en utilisant le cache DB si possible."""
    target = (target_lang or 'fr')[:2].lower()

    # 1) déjà en cache ?
    cached = CommentTranslation.query.filter_by(comment_id=comment.id, lang=target).first()
    if cached:
        return cached.text, None, True

    # 2) traduire si API dispo
    translated, detected = translate_text_auto(comment.message, target_lang=target)
    if translated:
        try:
            db.session.add(CommentTranslation(comment_id=comment.id, lang=target, text=translated))
            db.session.commit()
        except Exception as e:
            logging.warning("cache_insert_failed: %s", e)
            db.session.rollback()
        return translated, detected, True

    # 3) pas de traducteur/config: renvoyer l'original
    return comment.message, None, False

# ------------------------------------------------------------------
# Sécurité basique (Content-Security-Policy)
# ------------------------------------------------------------------
@app.after_request
def set_headers(resp):
    # Autoriser automatiquement l'origine définie par LIBRETRANSLATE_URL
    lt_origin = "https://libretranslate.com"
    try:
        p = urlparse(LIBRETRANSLATE_URL)
        if p.scheme and p.netloc:
            lt_origin = f"{p.scheme}://{p.netloc}"
    except Exception:
        lt_origin = "https://libretranslate.com"

    csp = (
        "default-src 'self'; "
        "img-src 'self' data: https:; "
        "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://fonts.googleapis.com; "
        # ⬇️ data: et jsDelivr déjà autorisés pour les fonts (Swiper)
        "font-src 'self' data: https://fonts.gstatic.com https://cdn.jsdelivr.net; "
        "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://www.paypal.com; "
        # ✅ autorise l'endpoint de traduction configuré
        f"connect-src 'self' https://api-free.deepl.com https://translation.googleapis.com https://www.paypal.com {lt_origin}; "
        "frame-src 'self' https://www.paypal.com; "
        "base-uri 'self'; form-action 'self' https://www.paypal.com"
    )
    resp.headers["Content-Security-Policy"] = csp
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    return resp

# ------------------------------------------------------------------
# Données de tours (pour SEO / JSON-LD, etc.)
# ------------------------------------------------------------------
TOURS = [
    {"id": "candelaria", "name": "Visite historique de La Candelaria", "price": 40, "currency": "USD"},
    {"id": "monserrate", "name": "Randonnée à Monserrate", "price": 45, "currency": "USD"},
    {"id": "zipaquira",  "name": "Excursion à la Cathédrale de sel de Zipaquirá", "price": 40, "currency": "EUR"},
    {"id": "chorrera",   "name": "Cascade de La Chorrera", "price": 55, "currency": "USD"},
    {"id": "finca-cafe", "name": "Visite d’une finca à café", "price": 50, "currency": "USD"},
]

# ------------------------------------------------------------------
# Commentaires (DB)
# ------------------------------------------------------------------
@app.post("/submit_comment")
def submit_comment():
    name = request.form.get("name", "").strip()
    message = request.form.get("message", "").strip()
    rating_raw = request.form.get("rating", "5")
    try:
        rating = float(rating_raw)
    except ValueError:
        rating = 5.0

    if not name or not message:
        flash(_("Merci d’indiquer un nom et un message."), "error")
        return redirect(url_for("index", lang=get_locale()))

    logger.info("comment_submitted name=%s rating=%s req_id=%s", name, rating, g.request_id)

    # date locale (si fournie) ou aujourd'hui
    date_str = request.form.get("date") or datetime.utcnow().strftime("%d %b %Y")

    c = Comment(
        name=name,
        country=request.form.get("country","").strip(),
        date_str=date_str,
        rating=rating,
        message=message
    )
    db.session.add(c)
    db.session.commit()

    flash(_("Merci pour votre commentaire !"), "success")
    return redirect(url_for("index", lang=get_locale()))

@app.post("/admin/delete_comment/<int:cid>")
def admin_delete_comment(cid):
    token = request.form.get("token","")
    if token != ADMIN_DELETE_TOKEN:
        abort(403)
    c = db.session.get(Comment, cid)
    if not c:
        flash(_("Commentaire introuvable."), "error")
        return redirect(url_for("index", lang=get_locale()))
    db.session.delete(c)
    db.session.commit()
    flash(_("Commentaire supprimé."), "success")
    return redirect(url_for("index", lang=get_locale()))

# Seed local facultatif
@app.route("/seed")
def seed():
    if Comment.query.count() > 0:
        return "already seeded", 200
    samples = [
        ("Jean", "France", "12 mai 2025", 5, "L’expérience est super. Alejandra prend son temps pour expliquer."),
        ("María", "Colombia", "30 julio 2024", 5, "Recomendado. Comunicación excelente antes y durante."),
        ("John", "USA", "17 Aug 2025", 4.5, "Amazing day in Zipaquirá. Highly recommended!"),
    ]
    for name, country, date_str, rating, message in samples:
        db.session.add(Comment(
            name=name, country=country, date_str=date_str, rating=rating, message=message
        ))
    db.session.commit()
    return "ok", 201

# ------------------------------------------------------------------
# Routes pages
# ------------------------------------------------------------------
@app.route("/", endpoint="index")
def index():
    comments = Comment.query.all()

    # tri: parse date_str sinon created_at ; du plus récent au plus ancien
    def sort_key(c):
        dt = parse_date_str(c.date_str)
        if dt is None:
            dt = c.created_at or datetime.min
        return dt

    comments = sorted(comments, key=sort_key, reverse=True)

    # Traduire si nécessaire en fonction de la langue courante
    try:
        current_lang = (get_locale() or 'fr')[:2].lower()
    except Exception:
        current_lang = 'fr'

    comments_ui = []
    for c in comments:
        msg, src, translated = get_comment_message_for_lang(c, current_lang)
        comments_ui.append(CommentView(c, msg, translated=translated, source_lang=src))

    return render_template("index.html", comments=comments_ui)

@app.route("/a-propos", endpoint="about")
def about_page():
    return render_template("about.html")

@app.route("/tours", endpoint="tours")
def tours_page():
    return render_template("tours.html")

@app.route("/contact", endpoint="contact")
def contact_page():
    return render_template("contact.html")

@app.route("/transport", endpoint="transport")
def transport_page():
    return render_template("transport.html")

@app.route("/reservation", methods=["GET","POST"], endpoint="reservation")
def reservation_page():
    selected_tour = request.values.get("tour")  # GET ou POST
    return render_template("reservation.html", tour=selected_tour)

# ------------------------------------------------------------------
# sitemap / robots / health & errors
# ------------------------------------------------------------------
@app.route('/sitemap.xml')
def sitemap_xml():
    return send_from_directory(app.static_folder, 'sitemap.xml', mimetype='application/xml')

@app.route('/robots.txt')
def robots_txt():
    return send_from_directory(app.static_folder, 'robots.txt', mimetype='text/plain')

@app.get("/healthz")
def healthz():
    return {"status": "ok", "mode": PAYPAL_MODE}, 200

@app.errorhandler(404)
def not_found(e):
    return render_template("404.html"), 404

@app.errorhandler(500)
def server_error(e):
    # tu peux logger e si besoin
    return render_template("500.html"), 500



