from flask import Flask, render_template, request, url_for, flash, redirect, send_from_directory
from flask_babel import Babel, _
import os, requests, logging, json, re
from datetime import datetime
from werkzeug.middleware.proxy_fix import ProxyFix
from flask_sqlalchemy import SQLAlchemy

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
    try:
        parts = re.split(r"[ \-/]+", s)
        if len(parts) < 3:
            return None
        d = int(re.sub(r"\D+", "", parts[0]))
        m_token = parts[1]
        if m_token.isdigit():
            m = int(m_token)
        else:
            m = _MONTHS.get(m_token, _MONTHS.get(m_token.strip(".,"), None))
        y = int(re.sub(r"\D+", "", parts[2]))
        if 0 < d <= 31 and 1 <= m <= 12 and 1900 <= y <= 2100:
            return datetime(y, m, d)
    except Exception:
        return None
    return None

def format_date_human(d: datetime, locale="fr"):
    if not d: return ""
    if locale == "fr":
        months = ["janv.","févr.","mars","avr.","mai","juin","juil.","août","sept.","oct.","nov.","déc."]
        return f"{d.day} {months[d.month-1]} {d.year}"
    if locale == "es":
        months = ["ene.","feb.","mar.","abr.","may.","jun.","jul.","ago.","sept.","oct.","nov.","dic."]
        return f"{d.day} {months[d.month-1]} {d.year}"
    months = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]
    return f"{months[d.month-1]} {d.day}, {d.year}"

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
# SQLAlchemy (comments)
# ------------------------------------------------------------------
DB_URL = os.getenv("DATABASE_URL", "").replace("postgres://", "postgresql://", 1) if os.getenv("DATABASE_URL") else "sqlite:///local.db"
app.config["SQLALCHEMY_DATABASE_URI"] = DB_URL
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
db = SQLAlchemy(app)

class Comment(db.Model):
    __tablename__ = "comments"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), default="")
    country = db.Column(db.String(120), default="")
    rating = db.Column(db.Float, default=5.0)
    date_str = db.Column(db.String(120), default="")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    message = db.Column(db.Text, nullable=False)

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
# Traduction côté serveur (optionnelle)
# ------------------------------------------------------------------
# ✅ Par défaut OFF (gratuit, pas d'appels API).
TRANSLATE_SERVER_ENABLED = os.getenv("TRANSLATE_SERVER_ENABLED", "0") == "1"

# Clés optionnelles si tu veux activer plus tard
DEEPL_API_KEY = os.getenv("DEEPL_API_KEY")
GOOGLE_TRANSLATE_API_KEY = os.getenv("GOOGLE_TRANSLATE_API_KEY")

if not TRANSLATE_SERVER_ENABLED:
    app.logger.info("Server-side translation DISABLED (use client-side Google Translate button).")

def translate_text_auto(text, target_lang, source_lang=None, timeout=12):
    """
    Traduit `text` vers `target_lang` via DeepL/Google si activé et clés dispo.
    Retourne (translated_text, detected_source_lang) ou (None, None).
    """
    if not text or not target_lang:
        return None, None

    # 🚫 Couper toute traduction serveur si désactivée
    if not TRANSLATE_SERVER_ENABLED:
        return None, None

    # DeepL (si clé)
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
            return tr['text'], tr.get('detected_source_language', None)
        except Exception as e:
            logging.warning("deepl_error: %s", e)

    # Google Cloud Translate (si clé)
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

    return None, None

class CommentView:
    """Objet passé au template avec message potentiellement traduit + méta."""
    def __init__(self, c, display_message, translated=False, source_lang=None):
        self.id = c.id
        self.name = getattr(c, 'name', '')
        self.country = getattr(c, 'country', '')
        self.date_str = getattr(c, 'date_str', '')
        self.rating = getattr(c, 'rating', 5.0)
        self.created_at = getattr(c, 'created_at', None)
        self.message = display_message
        self.translated = translated
        self.source_lang = source_lang

# ------------------------------------------------------------------
# Paypal
# ------------------------------------------------------------------
PAYPAL_MODE = os.getenv("PAYPAL_MODE", "sandbox")  # "live" ou "sandbox"
PAYPAL_CLIENT_ID = os.getenv("PAYPAL_CLIENT_ID", "")

@app.context_processor
def inject_globals():
    return {
        "paypal_client_id": PAYPAL_CLIENT_ID,
    }

# ------------------------------------------------------------------
# Routes
# ------------------------------------------------------------------
@app.route("/")
def index():
    comments = Comment.query.order_by(Comment.created_at.desc()).limit(100).all()
    target = get_locale()

    views = []
    for c in comments:
        # 1) cache DB
        cached = CommentTranslation.query.filter_by(comment_id=c.id, lang=target).first()
        if cached:
            views.append(CommentView(c, cached.text, translated=True))
            continue

        # 2) tentative traduction serveur (si activée + clé)
        translated, detected = translate_text_auto(c.message, target_lang=target)
        if translated:
            try:
                db.session.add(CommentTranslation(comment_id=c.id, lang=target, text=translated))
                db.session.commit()
            except Exception as e:
                logging.warning("cache_insert_failed: %s", e)
                db.session.rollback()
            views.append(CommentView(c, translated, translated=True, source_lang=detected))
        else:
            # 3) sinon texte original (bouton client fera la traduction si besoin)
            views.append(CommentView(c, c.message, translated=False))

    return render_template("index.html", comments=views)

@app.route("/reservation")
def reservation():
    return render_template("reservation.html")

@app.route("/tours")
def tours():
    return render_template("tours.html")

@app.route("/transport")
def transport():
    return render_template("transport.html")

# Form POST pour ajouter un commentaire
@app.post("/comments")
def submit_comment():
    name = (request.form.get("name") or "").strip()
    message = (request.form.get("message") or "").strip()
    country = (request.form.get("country") or "").strip()
    rating = request.form.get("rating") or "5"
    date_str = request.form.get("date") or ""

    if not message:
        flash(_("Merci d'écrire un petit message 😇"), "error")
        return redirect(url_for("index", lang=get_locale()))

    created_at = None
    if date_str:
        d = parse_date_str(date_str)
        if d:
            created_at = d

    try:
        rating_f = float(rating)
    except Exception:
        rating_f = 5.0

    c = Comment(
        name=name[:120],
        country=country[:120],
        rating=rating_f,
        date_str=date_str[:120],
        created_at=created_at or datetime.utcnow(),
        message=message
    )
    db.session.add(c)
    db.session.commit()

    # Invalider les caches de traduction éventuels pour ce commentaire
    try:
        CommentTranslation.query.filter_by(comment_id=c.id).delete()
        db.session.commit()
    except Exception:
        db.session.rollback()

    flash(_("Merci pour votre adorable commentaire 💛"), "success")
    return redirect(url_for("index", lang=get_locale()))

# ------------------------------------------------------------------
# Statique & SEO
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
    return render_template("500.html"), 500


