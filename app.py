from flask import Flask, render_template, request, url_for, flash, redirect, send_from_directory, session, make_response
from flask_babel import Babel, _
import os, requests, logging, re, secrets
from datetime import datetime
from werkzeug.middleware.proxy_fix import ProxyFix
from flask_sqlalchemy import SQLAlchemy
from functools import wraps
from sqlalchemy import inspect, nullslast  # nullslast laissé si tu veux un tri 100% SQL plus tard

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

# ✅ Tri robuste : created_at > date_str parsée > très ancien
def _sort_ts_model(c):
    return c.created_at or parse_date_str(getattr(c, 'date_str', '')) or datetime.min

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
# SQLAlchemy (models AVANT les routes)
# ------------------------------------------------------------------
raw_db = os.getenv("DATABASE_URL")
if raw_db:
    # Render/Heroku donnent parfois "postgres://"
    raw_db = raw_db.replace("postgres://", "postgresql://", 1)
    # Forcer le driver psycopg v3 (installé) au lieu du défaut psycopg2
    if raw_db.startswith("postgresql://"):
        raw_db = "postgresql+psycopg://" + raw_db.split("://", 1)[1]
    DB_URL = raw_db
else:
    DB_URL = "sqlite:///local.db"

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
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    message = db.Column(db.Text, nullable=False)

class CommentTranslation(db.Model):
    __tablename__ = "comment_translation"
    id = db.Column(db.Integer, primary_key=True)
    comment_id = db.Column(db.Integer, db.ForeignKey('comments.id', ondelete='CASCADE'), nullable=False)
    lang = db.Column(db.String(5), nullable=False)  # 'fr', 'en', 'es'
    text = db.Column(db.Text, nullable=False)
    __table_args__ = (db.UniqueConstraint('comment_id', 'lang', name='uq_comment_lang'),)

# ✅ Modèle manquant : Transferts
class Transfer(db.Model):
    __tablename__ = "transfers"
    id = db.Column(db.Integer, primary_key=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)

    name = db.Column(db.String(200), default="")
    email = db.Column(db.String(200), default="")
    whatsapp = db.Column(db.String(100), default="")
    language = db.Column(db.String(5), default="fr")

    pickup = db.Column(db.String(300), default="")
    dropoff = db.Column(db.String(300), default="")
    flight = db.Column(db.String(120), default="")
    date_str = db.Column(db.String(120), default="")
    time_str = db.Column(db.String(120), default="")
    passengers = db.Column(db.Integer, default=1)
    notes = db.Column(db.Text, default="")
    raw = db.Column(db.Text, default="")

with app.app_context():
    db.create_all()

# ------------------------------------------------------------------
# Traduction côté serveur (désactivée par défaut)
# ------------------------------------------------------------------
TRANSLATE_SERVER_ENABLED = os.getenv("TRANSLATE_SERVER_ENABLED", "0") == "1"
DEEPL_API_KEY = os.getenv("DEEPL_API_KEY")
GOOGLE_TRANSLATE_API_KEY = os.getenv("GOOGLE_TRANSLATE_API_KEY")

if not TRANSLATE_SERVER_ENABLED:
    app.logger.info("Server-side translation DISABLED (use client-side Google Translate button).")

def translate_text_auto(text, target_lang, source_lang=None, timeout=12):
    """Retourne (translated_text, detected_source_lang) ou (None, None) si OFF/sans clé."""
    if not text or not target_lang:
        return None, None
    if not TRANSLATE_SERVER_ENABLED:
        return None, None

    # DeepL si clé
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

    # Google Cloud si clé
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
# ADMIN (unique, sans doublons d’endpoints)
# ------------------------------------------------------------------
ADMIN_USER = os.getenv("ADMIN_USER", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD")  # à définir en prod

def admin_required(fn):
    @wraps(fn)
    def _wrap(*args, **kwargs):
        if session.get("is_admin"):
            return fn(*args, **kwargs)
        return redirect(url_for("admin_login", next=request.url))
    return _wrap

def _inline_html(title, body):
    return f"""<!doctype html>
<html lang="fr"><meta charset="utf-8">
<title>{title}</title>
<style>
body{{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;background:#0f172a;color:#e5e7eb;margin:0;padding:32px}}
.card{{background:#111827;border:1px solid #1f2937;border-radius:12px;padding:20px;max-width:1060px;margin:auto}}
h1{{margin:0 0 10px;font-size:22px}}
a{{color:#93c5fd;text-decoration:none}} a:hover{{text-decoration:underline}}
.btn{{display:inline-block;background:#2563eb;color:#fff;padding:10px 14px;border-radius:10px}}
.small{{opacity:.8;font-size:14px}}
table{{width:100%;margin-top:10px;border-collapse:collapse;font-size:15px}}
td,th{{padding:8px 10px;border-bottom:1px solid #1f2937;text-align:left;vertical-align:top}}
.badge{{display:inline-block;background:#1f2937;border:1px solid #334155;border-radius:8px;padding:2px 8px}}
details summary{{cursor:pointer}}
</style>
<div class="card">{body}</div></html>"""

# CSRF helpers
def _csrf_get():
    tok = session.get("_csrf")
    if not tok:
        tok = secrets.token_hex(16)
        session["_csrf"] = tok
    return tok

def _csrf_check(tok: str) -> bool:
    return bool(tok) and tok == session.get("_csrf")

def _ensure_tables():
    """Crée les tables manquantes (dont 'transfers') si besoin, sans casser l'app."""
    try:
        with app.app_context():
            db.create_all()
    except Exception as e:
        app.logger.warning("ensure_tables_failed: %s", e)

@app.route("/admin/login", methods=["GET","POST"])
def admin_login():
    if request.method == "POST":
        user = (request.form.get("user") or "").strip()
        pw = request.form.get("password") or ""
        if ADMIN_PASSWORD and user == ADMIN_USER and pw == ADMIN_PASSWORD:
            session["is_admin"] = True
            return redirect(request.args.get("next") or url_for("admin_home"))
        flash(_("Identifiants invalides"), "error")
    if os.path.exists(os.path.join(app.root_path, "templates", "admin_login.html")):
        return render_template("admin_login.html")
    body = """
      <h1>Connexion admin</h1>
      <form method="post">
        <p><label>Utilisateur:<br><input name="user" required></label></p>
        <p><label>Mot de passe:<br><input name="password" type="password" required></label></p>
        <p><button class="btn" type="submit">Se connecter</button></p>
        <p class="small">Définis les variables d'environnement <code>ADMIN_USER</code> (optionnel) et <code>ADMIN_PASSWORD</code> (obligatoire)</p>
      </form>
    """
    return _inline_html("Login admin", body)

@app.route("/admin/logout")
def admin_logout():
    session.pop("is_admin", None)
    return redirect(url_for("index", lang=get_locale()))

@app.route("/admin")
def admin_root():
    return redirect(url_for("admin_home"))

@app.route("/admin/")
@admin_required
def admin_home():
    _ensure_tables()
    comments_count     = Comment.query.count()
    translations_count = CommentTranslation.query.count()
    transfers_count    = Transfer.query.count()
    body = f"""
      <h1>Panneau d’administration</h1>
      <p class="small">Base: <code>{DB_URL}</code></p>
      <table>
        <tr><th>Commentaires</th><td><span class="badge">{comments_count}</span> – <a class="btn" href="{url_for('admin_comments')}">Gérer les commentaires</a></td></tr>
        <tr><th>Traductions en cache</th><td><span class="badge">{translations_count}</span></td></tr>
        <tr><th>Transferts</th><td><span class="badge">{transfers_count}</span> – <a class="btn" href="{url_for('admin_transfers')}">Voir la liste</a></td></tr>
        <tr><th>Mode PayPal</th><td>{PAYPAL_MODE}</td></tr>
      </table>
      <p style="margin-top:16px">
        <a class="btn" href="{url_for('admin_logout')}" style="background:#4b5563">Se déconnecter</a>
        &nbsp; <a href="{url_for('index', lang=get_locale())}">← Retour au site</a>
      </p>
    """
    return _inline_html("Admin", body)

@app.route("/_routes")
def _routes():
    lines = sorted(str(r) for r in app.url_map.iter_rules())
    return make_response("<pre>" + "\n".join(lines) + "</pre>", 200)

# ---------------- ADMIN : Commentaires ----------------
@app.get("/admin/comments")
@admin_required
def admin_comments():
    items = Comment.query.limit(1000).all()
    items.sort(key=lambda c: (_sort_ts_model(c), c.id), reverse=True)
    csrf = _csrf_get()
    rows = []
    for c in items:
        snippet = (c.message or "")
        snippet = (snippet[:120] + "…") if len(snippet) > 120 else snippet
        snippet = snippet.replace("<", "&lt;")
        rows.append(f"""
        <tr>
          <td>{c.id}</td>
          <td>{(c.name or '').replace('<','&lt;')}</td>
          <td>{(c.country or '').replace('<','&lt;')}</td>
          <td>{c.rating:.1f}</td>
          <td>{(c.created_at.strftime('%Y-%m-%d') if c.created_at else '')}</td>
          <td style="max-width:420px">{snippet}</td>
          <td>
            <form method="post" action="{url_for('admin_delete_comment', comment_id=c.id)}"
                  onsubmit="return confirm('Supprimer ce commentaire ?');">
              <input type="hidden" name="csrf" value="{csrf}">
              <button class="btn" type="submit" style="background:#dc2626">Supprimer</button>
            </form>
          </td>
        </tr>
        """)
    body = f"""
      <h1>Commentaires</h1>
      <p><a href="{url_for('admin_home')}">← Retour admin</a></p>
      <table>
        <thead>
          <tr>
            <th>ID</th><th>Nom</th><th>Pays</th><th>⭐</th><th>Date</th><th>Message</th><th>Action</th>
          </tr>
        </thead>
        <tbody>{"".join(rows) if rows else '<tr><td colspan="7">Aucun commentaire</td></tr>'}</tbody>
      </table>
    """
    return _inline_html("Commentaires — Admin", body)

@app.post("/admin/comments/<int:comment_id>/delete")
@admin_required
def admin_delete_comment(comment_id: int):
    if not _csrf_check(request.form.get("csrf")):
        flash(_("Session expirée, réessaie."), "error")
        return redirect(url_for("admin_comments"))
    try:
        CommentTranslation.query.filter_by(comment_id=comment_id).delete(synchronize_session=False)
        Comment.query.filter_by(id=comment_id).delete(synchronize_session=False)
        db.session.commit()
        flash(_("Commentaire supprimé ✅"), "success")
    except Exception as e:
        db.session.rollback()
        app.logger.warning("delete_comment_failed: %s", e)
        flash(_("Suppression impossible."), "error")
    return redirect(url_for("admin_comments"))

# ---------------- PUBLIC : soumission d’un transfert ----------------
@app.post("/transfer")
def submit_transfer():
    f = request.form

    def g(*keys, default=""):
        for k in keys:
            v = f.get(k)
            if v:
                return v
        return default

    try:
        pax = int(g("passengers","pax","persons","people","personnes", default="1"))
    except Exception:
        pax = 1

    try:
        _ensure_tables()  # évite "relation transfers does not exist"
        t = Transfer(
            name=g("name","nom","full_name"),
            email=g("email","mail"),
            whatsapp=g("whatsapp","phone","telephone","tel"),
            language=get_locale(),
            pickup=g("pickup","from","depart","departure","pickup_address"),
            dropoff=g("dropoff","to","destination","arrivee","dropoff_address"),
            flight=g("flight","flight_number","vol"),
            date_str=g("date","jour","fecha"),
            time_str=g("time","pickup_time","heure","hora"),
            passengers=pax,
            notes=g("notes","message","comment"),
            raw=str({k:v for k,v in f.items()}),
        )
        db.session.add(t)
        db.session.commit()
        flash(_("Merci ! Nous confirmons votre transfert très vite par WhatsApp / e-mail."), "success")
    except Exception as e:
        app.logger.error("submit_transfer_failed: %s", e)
        db.session.rollback()
        _ensure_tables()
        flash(_("Petit souci technique, réessaie dans quelques secondes."), "error")

    return redirect(request.referrer or url_for("transport", lang=get_locale()))

# ---------------- ADMIN : Transferts ----------------
@app.get("/admin/transfers")
@admin_required
def admin_transfers():
    csrf = _csrf_get()
    try:
        _ensure_tables()
        items = Transfer.query.order_by(Transfer.created_at.desc()).all()
    except Exception as e:
        app.logger.error("admin_transfers_failed: %s", e)
        flash(_("La table des transferts vient d’être créée. Réessaie."), "error")
        return redirect(url_for("admin_home"))

    if not items:
        body = f"""
          <h1>Transferts</h1>
          <p><a href="{url_for('admin_home')}">← Retour admin</a></p>
          <p>Aucun transfert.</p>
        """
        return _inline_html("Transferts — Admin", body)

    rows = []
    for t in items:
        rows.append(f"""
        <tr>
          <td>{t.id}</td>
          <td>{t.created_at:%Y-%m-%d %H:%M}</td>
          <td>{(t.name or '').replace('<','&lt;')}</td>
          <td>{(t.whatsapp or '').replace('<','&lt;')}</td>
          <td>{(t.email or '').replace('<','&lt;')}</td>
          <td>{(t.date_str or '')} {(t.time_str or '')}</td>
          <td>{(t.pickup or '').replace('<','&lt;')} → {(t.dropoff or '').replace('<','&lt;')}</td>
          <td>{t.passengers}</td>
          <td>
            <details>
              <summary>Voir</summary>
              <div style="margin-top:6px">
                <div><b>Vol</b> : {(t.flight or '').replace('<','&lt;')}</div>
                <div><b>Notes</b> : {(t.notes or '').replace('<','&lt;')}</div>
                <pre style="white-space:pre-wrap;background:#0b1220;padding:8px;border-radius:6px">{(t.raw or '').replace('<','&lt;')[:2000]}</pre>
              </div>
            </details>
          </td>
          <td>
            <form method="post" action="{url_for('admin_transfer_delete', transfer_id=t.id)}"
                  onsubmit="return confirm('Supprimer ce transfert ?');">
              <input type="hidden" name="csrf" value="{csrf}">
              <button class="btn" type="submit" style="background:#dc2626">Supprimer</button>
            </form>
          </td>
        </tr>
        """)

    body = f"""
      <h1>Transferts</h1>
      <p><a href="{url_for('admin_home')}">← Retour admin</a></p>
      <table>
        <thead>
          <tr>
            <th>#</th><th>Créé</th><th>Nom</th><th>WhatsApp</th><th>Email</th>
            <th>Date/Heure</th><th>Trajet</th><th>PAX</th><th>Détails</th><th>Action</th>
          </tr>
        </thead>
        <tbody>{"".join(rows)}</tbody>
      </table>
    """
    return _inline_html("Transferts — Admin", body)

@app.post("/admin/transfers/<int:transfer_id>/delete")
@admin_required
def admin_transfer_delete(transfer_id: int):
    if not _csrf_check(request.form.get("csrf")):
        flash(_("Session expirée, réessaie."), "error")
        return redirect(url_for("admin_transfers"))
    try:
        Transfer.query.filter_by(id=transfer_id).delete(synchronize_session=False)
        db.session.commit()
        flash(_("Transfert supprimé ✅"), "success")
    except Exception as e:
        db.session.rollback()
        app.logger.warning("delete_transfer_failed: %s", e)
        flash(_("Suppression impossible."), "error")
    return redirect(url_for("admin_transfers"))

# ------------------------------------------------------------------
# Routes publiques
# ------------------------------------------------------------------
@app.route("/")
def index():
    # ✅ Récupère un lot large et applique un tri robuste même si created_at est NULL
    comments = Comment.query.limit(1000).all()
    comments.sort(key=lambda c: (_sort_ts_model(c), c.id), reverse=True)

    target = get_locale()
    views = []
    for c in comments:
        cached = CommentTranslation.query.filter_by(comment_id=c.id, lang=target).first()
        if cached:
            views.append(CommentView(c, cached.text, translated=True))
            continue

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

    # Invalider éventuels caches de traduction
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


