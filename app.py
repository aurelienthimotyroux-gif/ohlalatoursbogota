from flask import (
    Flask, render_template, request, url_for, flash, redirect,
    send_from_directory, send_file, session, make_response, Response, abort, jsonify  # ✅ ajout jsonify
)
from flask_babel import Babel, _
import os, requests, logging, re, secrets, unicodedata
from datetime import datetime
from werkzeug.middleware.proxy_fix import ProxyFix
from flask_sqlalchemy import SQLAlchemy
from functools import wraps
from sqlalchemy import inspect, text
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

# ✉️ Email
from flask_mail import Mail, Message
from decimal import Decimal, ROUND_HALF_UP  # 🟡 PayPal: calcul monétaire


# --- CONFIG BDD (PostgreSQL sur Render) ---

# 1) Récupère l'URL depuis l'env
db_url = os.getenv("DATABASE_URL", "").strip()

if not db_url:
    raise RuntimeError("DATABASE_URL est vide ou manquante")

# 2) Normalise le schéma pour SQLAlchemy 2.x + psycopg3
#    (postgres://  -> postgresql+psycopg://)
#    (postgresql:// -> postgresql+psycopg://)
if db_url.startswith("postgres://"):
    db_url = "postgresql+psycopg://" + db_url[len("postgres://"):]
elif db_url.startswith("postgresql://") and not db_url.startswith("postgresql+psycopg://"):
    db_url = "postgresql+psycopg://" + db_url[len("postgresql://"):]

# 3) Ajoute sslmode=require si absent
if "sslmode=" not in db_url:
    db_url += ("&" if "?" in db_url else "?") + "sslmode=require"

# 4) Applique à Flask-SQLAlchemy
app.config["SQLALCHEMY_DATABASE_URI"] = db_url
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
# Options de pool: plus robuste en prod
app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
    "pool_pre_ping": True,
    "pool_recycle": 1800,
}

db = SQLAlchemy(app)

# (optionnel) petite route de test
@app.route("/__dbcheck")
def __dbcheck():
    try:
        with db.engine.connect() as conn:
            return str(conn.exec_driver_sql("SELECT 1").scalar())
    except Exception as e:
        return f"DB ERROR: {e}", 500
# --- FIN CONFIG BDD ---

# ------------------------------------------------------------------
# Helpers (dates, normalisation)
# ------------------------------------------------------------------
_MONTHS = {
    "janvier":1,"février":2,"fevrier":2,"mars":3,"avril":4,"mai":5,"juin":6,
    "juillet":7,"août":8,"aout":8,"septembre":9,"octobre":10,"novembre":11,"décembre":12,"decembre":12,
    "enero":1,"febrero":2,"marzo":3,"abril":4,"mayo":5,"junio":6,"julio":7,"agosto":8,"septiembre":9,"setiembre":9,"octubre":10,"noviembre":11,"diciembre":12,
    "january":1,"february":2,"march":3,"april":4,"may":5,"june":6,"july":7,"august":8,"september":9,"october":10,"november":11,"december":12,
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

def _sort_ts_model(c):
    return c.created_at or parse_date_str(getattr(c, 'date_str', '')) or datetime.min

def _norm(s: str) -> str:
    if not s: return ""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()
    return re.sub(r"\s+", " ", s)

# ------------------------------------------------------------------
# App & Babel
# ------------------------------------------------------------------
app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "dev-secret")
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

app.config["BABEL_DEFAULT_LOCALE"] = "fr"
app.config["BABEL_SUPPORTED_LOCALES"] = ["fr", "en", "es"]
app.config["BABEL_TRANSLATION_DIRECTORIES"] = "translations"
babel = Babel(app)

babel = Babel(app)

@babel.localeselector
def get_locale():
    lang = request.args.get("lang")
    return lang if lang in app.config["BABEL_SUPPORTED_LOCALES"] else app.config["BABEL_DEFAULT_LOCALE"]

# 👉 expose get_locale à Jinja
app.jinja_env.globals["get_locale"] = get_locale

def lang_url(lang_code: str):
    args = request.args.to_dict(flat=True)
    args["lang"] = lang_code
    endpoint = request.endpoint or "index"
    view_args = request.view_args or {}   # garde les paramètres de route (ex: slug)
    return url_for(endpoint, **view_args, **args)

# 👉 expose lang_url à Jinja (cette ligne MANQUAIT chez toi)
app.jinja_env.globals["lang_url"] = lang_url


# ✅ Normalisation d’URL: retirer ?lang=fr / lang invalide
@app.before_request
def _normalize_lang_fr():
    if request.method not in ("GET", "HEAD"):
        return None
    lang = request.args.get("lang")
    if lang == "fr" or (lang and lang not in ("fr","en","es")):
        parts = urlsplit(request.url)
        qs = [(k, v) for (k, v) in parse_qsl(parts.query, keep_blank_values=True) if k.lower() != "lang"]
        new_query = urlencode(qs, doseq=True)
        clean_url = urlunsplit((parts.scheme, parts.netloc, parts.path, new_query, parts.fragment))
        if clean_url != request.url:
            return redirect(clean_url, code=301)
    return None

# ------------------------------------------------------------------
# Database
# ------------------------------------------------------------------
raw_db = os.getenv("DATABASE_URL")
if raw_db:
    raw_db = raw_db.replace("postgres://", "postgresql://", 1)
    if raw_db.startswith("postgresql://"):
        raw_db = "postgresql+psycopg://" + raw_db.split("://", 1)[1]
    DB_URL = raw_db
else:
    DB_URL = "sqlite:///local.db"

app.config["SQLALCHEMY_DATABASE_URI"] = DB_URL
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
db = SQLAlchemy(app)

class Comment(db.Model):
    __tablename__="comments"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), default="")
    country = db.Column(db.String(120), default="")
    rating = db.Column(db.Float, default=5.0)
    date_str = db.Column(db.String(120), default="")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    message = db.Column(db.Text, nullable=False)

class CommentTranslation(db.Model):
    __tablename__="comment_translation"
    id = db.Column(db.Integer, primary_key=True)
    comment_id = db.Column(db.Integer, db.ForeignKey('comments.id', ondelete='CASCADE'), nullable=False)
    lang = db.Column(db.String(5), nullable=False)
    text = db.Column(db.Text, nullable=False)
    __table_args__ = (db.UniqueConstraint('comment_id','lang',name='uq_comment_lang'),)

class Transfer(db.Model):
    __tablename__="transfers"
    id = db.Column(db.Integer, primary_key=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    name = db.Column(db.String(160), default="")
    email = db.Column(db.String(160), default="")
    whatsapp = db.Column(db.String(80), default="")
    language = db.Column(db.String(8), default="")
    pickup = db.Column(db.String(240), default="")
    dropoff = db.Column(db.String(240), default="")
    flight = db.Column(db.String(80), default="")
    date_str = db.Column(db.String(80), default="")
    time_str = db.Column(db.String(80), default="")
    passengers = db.Column(db.Integer, default=1)
    notes = db.Column(db.Text, default="")
    raw = db.Column(db.Text, default="")

# ✅ Réservation avec téléphone + pays + nombre de personnes
class Reservation(db.Model):
    __tablename__="reservations"
    id = db.Column(db.Integer, primary_key=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    fullname = db.Column(db.String(160), default="")
    email = db.Column(db.String(160), default="")
    phone = db.Column(db.String(40), default="")
    country = db.Column(db.String(120), default="")
    date_str = db.Column(db.String(80), default="")
    persons = db.Column(db.Integer, default=1)
    tour_slug = db.Column(db.String(80), default="")
    message = db.Column(db.Text, default="")
    language = db.Column(db.String(8), default="")
    # 🟡 Nouveau : associer le paiement
    paypal_capture_id = db.Column(db.String(80), default="")

with app.app_context():
    db.create_all()
    # auto-migrate douce : ajoute colonnes manquantes sans casser
    insp = inspect(db.engine)
    try:
        cols = [c["name"] for c in insp.get_columns("reservations")]
        with db.engine.begin() as con:
            if "phone" not in cols:
                con.execute(text("ALTER TABLE reservations ADD COLUMN phone VARCHAR(40)"))
            if "persons" not in cols:
                con.execute(text("ALTER TABLE reservations ADD COLUMN persons INTEGER DEFAULT 1"))
            if "country" not in cols:
                con.execute(text("ALTER TABLE reservations ADD COLUMN country VARCHAR(120)"))
            # 🟡 Ajout auto si manquant
            if "paypal_capture_id" not in cols:
                con.execute(text("ALTER TABLE reservations ADD COLUMN paypal_capture_id VARCHAR(80)"))
    except Exception as e:
        app.logger.warning("auto_migrate_reservations_failed: %s", e)

# ------------------------------------------------------------------
# Email (Flask-Mail) — config via env vars
# ------------------------------------------------------------------
app.config["MAIL_SERVER"] = os.getenv("MAIL_SERVER", "smtp.gmail.com")
app.config["MAIL_PORT"] = int(os.getenv("MAIL_PORT", "587"))
app.config["MAIL_USE_TLS"] = os.getenv("MAIL_USE_TLS", "1") in ("1","true","True")
app.config["MAIL_USE_SSL"] = os.getenv("MAIL_USE_SSL", "0") in ("1","true","True")
app.config["MAIL_USERNAME"] = os.getenv("MAIL_USERNAME", "")
app.config["MAIL_PASSWORD"] = os.getenv("MAIL_PASSWORD", "")
app.config["MAIL_DEFAULT_SENDER"] = os.getenv("MAIL_DEFAULT_SENDER", app.config["MAIL_USERNAME"])
ADMIN_NOTIFY_EMAIL = os.getenv("ADMIN_NOTIFY_EMAIL", app.config["MAIL_DEFAULT_SENDER"] or app.config["MAIL_USERNAME"] or "")

mail = Mail(app)

# ------------------------------------------------------------------
# Admin minimal
# ------------------------------------------------------------------
ADMIN_USER = os.getenv("ADMIN_USER","admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD")

def admin_required(fn):
    @wraps(fn)
    def _wrap(*args, **kwargs):
        if session.get("is_admin"):
            return fn(*args, **kwargs)
        return redirect(url_for("admin_login", next=request.url))
    return _wrap

def _inline_html(title, body):
    return f"""<!doctype html>
<html lang="fr"><meta charset="utf-8"><title>{title}</title>
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

def _ensure_tables():
    try:
        with app.app_context():
            insp = inspect(db.engine)
            needed = ["comments","comment_translation","transfers","reservations"]
            for t in needed:
                if not insp.has_table(t):
                    db.create_all()
            # assure colonnes
            cols = [c["name"] for c in insp.get_columns("reservations")]
            with db.engine.begin() as con:
                if "phone" not in cols:
                    con.execute(text("ALTER TABLE reservations ADD COLUMN phone VARCHAR(40)"))
                if "persons" not in cols:
                    con.execute(text("ALTER TABLE reservations ADD COLUMN persons INTEGER DEFAULT 1"))
                if "country" not in cols:
                    con.execute(text("ALTER TABLE reservations ADD COLUMN country VARCHAR(120)"))
                if "paypal_capture_id" not in cols:
                    con.execute(text("ALTER TABLE reservations ADD COLUMN paypal_capture_id VARCHAR(80)"))
    except Exception as e:
        app.logger.warning("ensure_tables_failed: %s", e)

@app.route("/admin/")
@admin_required
def admin_home():
    _ensure_tables()
    body = f"""
      <h1>Panneau d’administration</h1>
      <p class="small">Base: <code>{DB_URL}</code></p>
      <table>
        <tr><th>Commentaires</th><td><span class="badge">{Comment.query.count()}</span> – <a class="btn" href="{url_for('admin_comments')}">Gérer les commentaires</a></td></tr>
        <tr><th>Traductions en cache</th><td><span class="badge">{CommentTranslation.query.count()}</span></td></tr>
        <tr><th>Transferts</th><td><span class="badge">{Transfer.query.count()}</span> – <a class="btn" href="{url_for('admin_transfers')}">Voir la liste</a></td></tr>
        <tr><th>Réservations</th><td><span class="badge">{Reservation.query.count()}</span> – <a class="btn" href="{url_for('admin_reservations')}">Voir la liste</a></td></tr>
      </table>
      <p style="margin-top:16px">
        <a class="btn" href="{url_for('admin_import_legacy')}">Importer les anciens avis</a>
        &nbsp; <a class="btn" href="{url_for('admin_logout')}" style="background:#4b5563">Se déconnecter</a>
        &nbsp; <a href="{url_for('index', lang=get_locale())}">← Retour au site</a>
      </p>
    """
    return _inline_html("Admin", body)

# ✅ Endpoint AJOUTÉ : évite le BuildError sur admin_import_legacy
@app.get("/admin/import-legacy", endpoint="admin_import_legacy")
@admin_required
def admin_import_legacy():
    body = f"""
      <h1>Import des anciens avis</h1>
      <p class="small">Fonction à venir. Ce bouton existe maintenant pour éviter le 500.</p>
      <p><a href="{url_for('admin_home')}">← Retour admin</a></p>
    """
    return _inline_html("Import anciens avis — Admin", body)

@app.route("/_routes")
def _routes():
    lines = sorted(str(r) for r in app.url_map.iter_rules())
    return make_response("<pre>" + "\n".join(lines) + "</pre>", 200)

def _csrf_get():
    tok = session.get("_csrf")
    if not tok:
        tok = secrets.token_hex(16)
        session["_csrf"] = tok
    return tok

def _csrf_check(tok: str) -> bool:
    return bool(tok) and tok == session.get("_csrf")

@app.get("/admin/comments")
@admin_required
def admin_comments():
    items = Comment.query.limit(1000).all()
    items.sort(key=lambda c: (_sort_ts_model(c), c.id), reverse=True)
    csrf = _csrf_get()
    rows = []
    for c in items:
        snippet = (c.message or "")
        # ✅ correction ici : on mesure la longueur de la chaîne, pas d'une comparaison
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
            <th>Date/Heure</th><th>Trajet</th><th>Trajet</th><th>PAX</th><th>Détails</th><th>Action</th>
          </tr>
        </thead>
        <tbody>{"".join(rows)}</tbody>
      </table>
    """
    return _inline_html("Transferts — Admin", body)

# ✅ Admin — liste des réservations
@app.get("/admin/reservations")
@admin_required
def admin_reservations():
    _ensure_tables()
    items = Reservation.query.order_by(Reservation.created_at.desc()).all()
    csrf = _csrf_get()
    if not items:
        body = f"""
          <h1>Réservations</h1>
          <p><a href="{url_for('admin_home')}">← Retour admin</a></p>
          <p>Aucune réservation.</p>
        """
        return _inline_html("Réservations — Admin", body)
    rows = []
    for r in items:
        rows.append(f"""
        <tr>
          <td>{r.id}</td>
          <td>{r.created_at:%Y-%m-%d %H:%M}</td>
          <td>{(r.fullname or '').replace('<','&lt;')}</td>
          <td>{(r.email or '').replace('<','&lt;')}</td>
          <td>{(r.phone or '').replace('<','&lt;')}</td>
          <td>{(r.country or '').replace('<','&lt;')}</td>
          <td>{(r.date_str or '').replace('<','&lt;')}</td>
          <td>{r.persons}</td>
          <td>{(r.tour_slug or '').replace('<','&lt;')}</td>
          <td style="max-width:420px">{(r.message or '').replace('<','&lt;')}</td>
          <td>{(r.language or '').replace('<','&lt;')}</td>
          <td>{(r.paypal_capture_id or '—').replace('<','&lt;')}</td>
          <td>
            <form method="post" action="{url_for('admin_reservation_delete', reservation_id=r.id)}"
                  onsubmit="return confirm('Supprimer cette réservation ?');">
              <input type="hidden" name="csrf" value="{csrf}">
              <button class="btn" type="submit" style="background:#dc2626">Supprimer</button>
            </form>
          </td>
        </tr>
        """)
    body = f"""
      <h1>Réservations</h1>
      <p><a href="{url_for('admin_home')}">← Retour admin</a></p>
      <table>
        <thead>
          <tr>
            <th>#</th><th>Créé</th><th>Nom</th><th>Email</th><th>Téléphone</th><th>Pays</th>
            <th>Date</th><th>PAX</th><th>Tour</th><th>Message</th><th>Langue</th><th>PayPal</th><th>Action</th>
          </tr>
        </thead>
        <tbody>{"".join(rows)}</tbody>
      </table>
    """
    return _inline_html("Réservations — Admin", body)

@app.post("/admin/reservations/<int:reservation_id>/delete")
@admin_required
def admin_reservation_delete(reservation_id: int):
    if not _csrf_check(request.form.get("csrf")):
        flash(_("Session expirée, réessaie."), "error")
        return redirect(url_for("admin_reservations"))
    try:
        Reservation.query.filter_by(id=reservation_id).delete(synchronize_session=False)
        db.session.commit()
        flash(_("Réservation supprimée ✅"), "success")
    except Exception as e:
        db.session.rollback()
        app.logger.warning("delete_reservation_failed: %s", e)
        flash(_("Suppression impossible."), "error")
    return redirect(url_for("admin_reservations"))

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
# Données de démo + pages publiques
# ------------------------------------------------------------------
@app.route("/")
def index():
    comments_db = Comment.query.limit(1000).all()
    comments_db.sort(key=lambda c: (_sort_ts_model(c), c.id), reverse=True)
    return render_template("index.html", comments=[c for c in comments_db])

# ------------------------------------------------------------------
# Déduction de langue (fallback si ui_lang absent)
# ------------------------------------------------------------------
def _infer_lang_from_request(req, country_text: str = "", email_text: str = "", phone_text: str = "") -> str:
    """
    Renvoie 'fr' / 'en' / 'es' en se basant sur:
    1) ?lang=..., sinon
    2) header Accept-Language, sinon
    3) heuristiques sur le pays/adresse mail/phone, sinon 'fr'
    """
    qlang = (req.args.get("lang") or "").lower()
    if qlang in ("fr", "en", "es"):
        return qlang

    al = (req.headers.get("Accept-Language") or "").lower()
    for code in ("fr", "en", "es"):
        if code in al:
            return code

    txt = " ".join([(country_text or ""), (email_text or ""), (phone_text or "")]).lower()

    en_tokens = [
        "uk","u.k","united kingdom","england","angleterre","royaume-uni",
        "scotland","wales","ireland","irlande",
        "usa","united states","etats-unis","états-unis","us",
        "canada","australia","australie","new zealand","nouvelle-zélande"
    ]
    if any(t in txt for t in en_tokens):
        return "en"

    es_tokens = [
        "espagne","españa","spain","colombie","colombia","mexique","méxique","mexico",
        "argentine","argentina","pérou","peru","chili","chile","équateur","equateur","ecuador",
        "bolivie","bolivia","uruguay","paraguay","costa rica","panama","guatemala","honduras",
        "el salvador","nicaragua","republica dominicana","république dominicaine","dominican republic"
    ]
    if any(t in txt for t in es_tokens):
        return "es"

    return "fr"

# ✅✅✅ AJOUT — Grille tarifs USD + API devis (groupes max 6)
# ------------------------------------------------------------------
PRICES_USD = {
    "monserrate": {
        "rules": [
            (1, 1, 65),
            (2, 6, 55),
        ],
        "max_group": 6,
    },
    "zipaquira": {
        "rules": [
            (1, 1, 120),
            (2, 2, 100),
            (3, 6, 90),
        ],
        "max_group": 6,
    },
    "finca-cafe": {
        "rules": [
            (1, 1, 150),
            (2, 2, 105),
            (3, 6, 95),
        ],
        "max_group": 6,
    },
    "chorrera": {
        "rules": [
            (1, 1, 125),
            (2, 3, 115),
            (4, 6, 100),
        ],
        "max_group": 6,
    },
    "candelaria": {
        "rules": [
            (1, 1, 40),
            (2, 3, 35),
            (4, 6, 33),
        ],
        "max_group": 6,
    },
}

def quote_tour_usd(slug: str, people: int):
    """
    Retourne un dict: { 'ok': bool, 'per_person': Decimal, 'total': Decimal, 'currency': 'USD', ... }
    - ok=False si la taille du groupe dépasse la limite (max 6)
    """
    conf = PRICES_USD.get((slug or "").strip().lower())
    if not conf:
        return {"ok": False, "reason": "unknown_tour"}

    try:
        n = int(people)
    except Exception:
        n = 1
    if n < 1: n = 1

    max_g = conf.get("max_group")
    if max_g and n > max_g:
        return {"ok": False, "reason": "group_too_large", "max": max_g}

    ppp = None
    for (mn, mx, price) in conf["rules"]:
        if mn <= n <= mx:
            ppp = Decimal(price)
            break
    if ppp is None:
        return {"ok": False, "reason": "no_rule"}

    total = (ppp * Decimal(n)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    ppp = ppp.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return {"ok": True, "per_person": ppp, "total": total, "currency": "USD", "people": n}

@app.get("/api/quote")
def api_quote():
    slug = (request.args.get("tour") or "").strip().lower()
    people = request.args.get("people", type=int) or 1
    q = quote_tour_usd(slug, people)
    if not q.get("ok"):
        if q.get("reason") == "group_too_large":
            return jsonify({
                "ok": False,
                "message": _("Groupe trop nombreux pour ce tour en réservation en ligne (max. %(max)d). Contactez-nous.", max=q.get("max", 6))
            }), 400
        return jsonify({"ok": False, "message": _("Tarif indisponible pour cette configuration.")}), 400
    return jsonify({
        "ok": True,
        "per_person": str(q["per_person"]),
        "total": str(q["total"]),
        "currency": q["currency"],
        "people": q["people"],
    }), 200
# ------------------------------------------------------------------

# ✅ Réservation GET/POST + mails langue auto
@app.route("/reservation", methods=["GET","POST"])
def reservation():
    # --- GET: si ?tour= présent, redirige 301 vers l'URL propre /reservation/<slug> ---
    if request.method == "GET":
        tour_qs = (request.args.get("tour") or "").strip().lower()
        if tour_qs:
            return redirect(
                url_for("reservation_clean", slug=tour_qs, lang=request.args.get("lang")),
                code=301
            )
        # pas de ?tour= -> page générique
        return render_template("reservation.html")

    # --- POST: logique existante inchangée ---
    fullname = (request.form.get("nom") or "").strip()
    email    = (request.form.get("email") or "").strip()
    phone    = (request.form.get("phone") or "").strip()
    country  = (request.form.get("country") or "").strip()
    date_str = (request.form.get("date") or "").strip()
    persons  = request.form.get("persons") or "1"
    tour     = (request.form.get("tour") or "").strip().lower()
    message  = (request.form.get("message") or "").strip()
    # 🟡 ID de capture transmis par le front après paiement
    capture_id = (request.form.get("paypal_capture_id") or "").strip()

    # langue UI envoyée par le formulaire (champ hidden ui_lang)
    ui_lang  = (request.form.get("ui_lang") or "").lower()
    if ui_lang in ("fr","en","es"):
        lang = ui_lang
    else:
        lang = _infer_lang_from_request(request, country_text=country, email_text=email, phone_text=phone)

    # Validation PAX (1..6)
    try:
        persons = int(persons)
        if persons < 1: persons = 1
        if persons > 6: persons = 6
    except Exception:
        persons = 1

    if not fullname or not email or not date_str or not tour:
        flash(_("Merci de remplir nom, email, date et tour."), "error")
        return render_template("reservation.html", tour=tour)

    # 🟡 Vérifier le paiement AVANT d'enregistrer
    if not verify_paypal_capture(capture_id):
        flash(_("Le paiement PayPal n'a pas été confirmé. Merci d'effectuer le paiement avant d'envoyer la réservation."), "error")
        return render_template("reservation.html", tour=tour)

    # Enregistrer en DB
    try:
        r = Reservation(
            fullname=fullname[:160],
            email=email[:160],
            phone=phone[:40],
            country=country[:120],
            date_str=date_str[:80],
            persons=persons,
            tour_slug=tour[:80],
            message=message,
            language=lang[:8],
            paypal_capture_id=capture_id[:80]  # 🟡 on garde la trace
        )
        db.session.add(r)
        db.session.commit()
    except Exception as e:
        app.logger.error("reservation_db_error: %s", e)
        db.session.rollback()
        flash(_("Petit souci technique, réessaie dans quelques secondes."), "error")
        return render_template("reservation.html", tour=tour)

    # Emails (langue auto fr/en/es)
    try:
        if app.config["MAIL_USERNAME"] and (app.config["MAIL_PASSWORD"] or app.config["MAIL_USE_SSL"] or app.config["MAIL_USE_TLS"]):
            subjects = {
                "fr": "Confirmation de réservation — Oh La La Tours Bogotá",
                "en": "Booking confirmation — Oh La La Tours Bogotá",
                "es": "Confirmación de reserva — Oh La La Tours Bogotá",
            }
            bodies = {
                "fr": f"""Bonjour {fullname},

Nous avons bien reçu votre réservation.
• Tour : {tour}
• Date : {date_str}
• Nombre de personnes : {persons}
• Téléphone : {phone or '—'}
• Pays : {country or '—'}
• Message : {message or '—'}
• Paiement PayPal (capture) : {capture_id or '—'}

Nous revenons vers vous très rapidement pour l’organisation.

Oh La La Tours Bogotá
""",
                "en": f"""Hello {fullname},

We’ve received your booking request.
• Tour: {tour}
• Date: {date_str}
• Number of people: {persons}
• Phone: {phone or '—'}
• Country: {country or '—'}
• Message: {message or '—'}
• PayPal payment (capture): {capture_id or '—'}

We’ll get back to you shortly to arrange the details.

Oh La La Tours Bogotá
""",
                "es": f"""Hola {fullname},

Hemos recibido tu reserva.
• Tour: {tour}
• Fecha: {date_str}
• Número de personas: {persons}
• Teléfono: {phone or '—'}
• País: {country or '—'}
• Mensaje: {message or '—'}
• Pago PayPal (captura): {capture_id or '—'}

En breve nos pondremos en contacto para organizar los detalles.

Oh La La Tours Bogotá
"""
            }

            subject_cli = subjects.get(lang, subjects["fr"])
            body_cli    = bodies.get(lang, bodies["fr"])
            app.logger.info("reservation_email_lang=%s", lang)

            # Client
            mail.send(Message(subject=subject_cli, recipients=[email], body=body_cli))

            # Interne (FR par défaut)
            notify_to = ADMIN_NOTIFY_EMAIL or app.config["MAIL_DEFAULT_SENDER"] or app.config["MAIL_USERNAME"]
            if notify_to:
                subject_admin = f"[Réservation] {fullname} — {tour} — {date_str} — {persons}p"
                body_admin = f"""Nouvelle réservation

Nom: {fullname}
Email: {email}
Téléphone: {phone or '—'}
Pays: {country or '—'}
Date: {date_str}
Personnes: {persons}
Tour: {tour}
Langue: {lang}
Paiement PayPal (capture): {capture_id or '—'}

Message:
{message or '—'}
"""
                mail.send(Message(subject=subject_admin, recipients=[notify_to], body=body_admin))
        else:
            app.logger.warning("Mail non configuré: aucune confirmation envoyée. Configure MAIL_* env vars.")
    except Exception as e:
        app.logger.error("reservation_mail_error: %s", e)

    # ✅ On reste sur la page de réservation, avec le message flash affiché dans reservation.html
    flash(_("Merci ! Votre réservation a bien été prise en compte. Un email de confirmation vous a été envoyé."), "success")
    return render_template("reservation.html", tour=tour)

# ✅ Nouvelle URL propre : /reservation/<slug>
@app.route("/reservation/<slug>", methods=["GET"])
def reservation_clean(slug):
    # Rend la même page avec le tour pré-sélectionné (HTTP 200)
    return render_template("reservation.html", tour=slug, slug=slug)

# ------------------------------------------------------------------
# Statique & SEO
# ------------------------------------------------------------------
@app.route('/robots.txt')
def robots_txt():
    resp = make_response(send_from_directory(app.static_folder, 'robots.txt', mimetype='text/plain'))
    resp.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate, max-age=0'
    return resp

@app.get("/sitemap.xml")
def sitemap_xml():
    resp = make_response(send_from_directory(app.static_folder, "sitemap.xml", mimetype="application/xml"))
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate, max-age=0"
    return resp

@app.get("/healthz")
def healthz():
    return {"status": "ok"}, 200

@app.errorhandler(404)
def not_found(e):
    return render_template("404.html"), 404

@app.errorhandler(500)
def server_error(e):
    return render_template("500.html"), 500

@app.route('/favicon.ico')
def favicon():
    return send_from_directory(
        os.path.join(app.static_folder, 'img', 'favicon'),
        'favicon.ico',
        mimetype='image/vnd.microsoft.icon'
    )

# ------------------------------------------------------------------
# 🟡 PAYPAL — unified (bloc unique à remplacer tel quel)
# ------------------------------------------------------------------
import time, requests

# Config
PAYPAL_MODE = os.getenv("PAYPAL_MODE", "sandbox").lower()  # 'sandbox' ou 'live'
PAYPAL_CLIENT_ID = os.getenv("PAYPAL_CLIENT_ID", "")
# accepte PAYPAL_CLIENT_SECRET ou PAYPAL_SECRET (fallback)
PAYPAL_CLIENT_SECRET = os.getenv("PAYPAL_CLIENT_SECRET") or os.getenv("PAYPAL_SECRET", "")
PAYPAL_CURRENCY = os.getenv("PAYPAL_CURRENCY", "USD").upper()
PAYPAL_API_BASE = "https://api-m.sandbox.paypal.com" if PAYPAL_MODE == "sandbox" else "https://api-m.paypal.com"

# 🔁 Tarifs USD par personne (paliers selon la taille du groupe 1..6)
PRICES_USD_PAYPAL = {
    "monserrate": [(1, 1, Decimal("65")), (2, 6, Decimal("55"))],
    "zipaquira":  [(1, 1, Decimal("120")), (2, 2, Decimal("100")), (3, 6, Decimal("90"))],
    "finca-cafe": [(1, 1, Decimal("150")), (2, 2, Decimal("105")), (3, 6, Decimal("95"))],
    "chorrera":   [(1, 1, Decimal("125")), (2, 3, Decimal("115")), (4, 6, Decimal("100"))],
    "candelaria": [(1, 1, Decimal("40")),  (2, 3, Decimal("35")),  (4, 6, Decimal("33"))],
}

# FX interne: 1 unité devise = X COP (utilisé si tu encaisses en COP)
COP_PER_UNIT = Decimal(os.getenv("COP_PER_UNIT", "3800"))

HTTP_TIMEOUT = 60  # laisser le temps à PayPal

def _money2(q: Decimal) -> str:
    return str(q.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))

def compute_price(tour: str, persons: int):
    """
    Retourne (amount_str_en_PAYPAL_CURRENCY, description)
    - Base tarifaire en USD (paliers / personne)
    - Si PAYPAL_CURRENCY == 'USD' ➜ montant en USD
      Sinon si 'COP' ➜ conversion via COP_PER_UNIT
      Sinon ➜ fallback USD
    """
    key = (tour or "").strip().lower()
    if key not in PRICES_USD_PAYPAL:
        raise ValueError("Tour non tarifé")

    try:
        n = int(persons)
    except Exception:
        n = 1
    n = max(1, min(n, 6))

    # prix / personne selon palier
    per_person_usd = None
    for (mn, mx, price) in PRICES_USD_PAYPAL[key]:
        if mn <= n <= mx:
            per_person_usd = Decimal(price)
            break
    if per_person_usd is None:
        # si pas de règle, on refuse
        raise ValueError("Aucune règle de prix pour ce nombre de personnes")

    total_usd = (per_person_usd * Decimal(n))

    # conversion selon la devise PayPal
    if PAYPAL_CURRENCY == "USD":
        total_unit = total_usd
    elif PAYPAL_CURRENCY == "COP":
        total_unit = (total_usd * COP_PER_UNIT)
    else:
        # fallback : on garde USD
        total_unit = total_usd

    amount = _money2(total_unit)
    desc = f"Reservation {key} x{n} — {per_person_usd} USD/pers"
    return amount, desc

def paypal_access_token() -> str:
    r = requests.post(
        f"{PAYPAL_API_BASE}/v1/oauth2/token",
        headers={"Accept": "application/json", "Accept-Language": "en_US"},
        data={"grant_type": "client_credentials"},
        auth=(PAYPAL_CLIENT_ID, PAYPAL_CLIENT_SECRET),
        timeout=HTTP_TIMEOUT
    )
    r.raise_for_status()
    js = r.json()
    return js["access_token"]

# ---- Routes (un seul exemplaire de chacune) ----

@app.get("/paypal-config")
def paypal_config():
    # Utile pour vérifier en prod la cohérence front/serveur
    return {
        "client_id": PAYPAL_CLIENT_ID,
        "currency": PAYPAL_CURRENCY,
        "mode": PAYPAL_MODE,
        "api_base": PAYPAL_API_BASE,
        "client_id_last6": PAYPAL_CLIENT_ID[-6:] if PAYPAL_CLIENT_ID else None,
    }

@app.post("/create-paypal-order")
def create_paypal_order():
    data = request.get_json(silent=True) or {}
    tour = (data.get("tour") or "").strip()
    persons = data.get("persons") or 1

    try:
        amount, description = compute_price(tour, persons)
    except ValueError as e:
        return {"error": str(e)}, 400

    token = paypal_access_token()
    payload = {
        "intent": "CAPTURE",
        "purchase_units": [{
            "amount": {"currency_code": PAYPAL_CURRENCY, "value": amount},
            "description": description
        }],
        "application_context": {
            "shipping_preference": "NO_SHIPPING",
            "user_action": "PAY_NOW",
        }
    }

    r = requests.post(
        f"{PAYPAL_API_BASE}/v2/checkout/orders",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json=payload, timeout=HTTP_TIMEOUT
    )
    if r.status_code >= 400:
        try:
            err = r.json()
        except Exception:
            err = {"error": r.text}
        app.logger.error("paypal_create_error: %s", err)
        return {"error": err}, 400

    order = r.json()
    oid = order.get("id")
    if not oid:
        app.logger.error("paypal_create_no_id: %s", order)
        return {"error": "CREATE_NO_ID", "raw": order}, 400
    return {"id": oid}

@app.get("/paypal-order/<order_id>")
def paypal_order(order_id):
    token = paypal_access_token()
    r = requests.get(
        f"{PAYPAL_API_BASE}/v2/checkout/orders/{order_id}",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        timeout=HTTP_TIMEOUT
    )
    data = r.json() if r.content else {}
    try:
        payee_mid = data["purchase_units"][0]["payee"].get("merchant_id")
    except Exception:
        payee_mid = None
    return {
        "summary": {
            "status": data.get("status"),
            "payee_merchant_id": payee_mid,
            "server_client_id_last6": PAYPAL_CLIENT_ID[-6:] if PAYPAL_CLIENT_ID else None
        },
        "raw": data
    }, r.status_code

@app.post("/capture-paypal-order/<order_id>")
def capture_paypal_order(order_id):
    token = paypal_access_token()

    # (A) Lecture de l'order avant capture (debug mismatch de compte)
    pre = {}
    try:
        r0 = requests.get(
            f"{PAYPAL_API_BASE}/v2/checkout/orders/{order_id}",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            timeout=HTTP_TIMEOUT
        )
        j0 = r0.json() if r0.content else {}
        pre["pre_status"] = j0.get("status")
        try:
            pre["payee_merchant_id"] = j0["purchase_units"][0]["payee"].get("merchant_id")
        except Exception:
            pre["payee_merchant_id"] = None
    except Exception as e:
        app.logger.warning("paypal_pre_read_failed: %s", e)

    # (B) Capture
    r = requests.post(
        f"{PAYPAL_API_BASE}/v2/checkout/orders/{order_id}/capture",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        timeout=HTTP_TIMEOUT
    )
    if r.status_code >= 400:
        try:
            err = r.json()
        except Exception:
            err = {"error": r.text}
        app.logger.error("paypal_capture_error: %s", err)
        return {
            "error": "CAPTURE_FAILED",
            "reason": err,
            "hint": {
                "mode": PAYPAL_MODE,
                "server_client_id_last6": PAYPAL_CLIENT_ID[-6:] if PAYPAL_CLIENT_ID else None,
                "order_payee_merchant_id": pre.get("payee_merchant_id"),
                "explain": "Si merchant_id ≠ ton compte, ou si client_id_last6 ≠ celui chargé côté front, PayPal renvoie 403 PERMISSION_DENIED."
            }
        }, 400

    data = r.json() if r.content else {}
    status = data.get("status", "UNKNOWN")

    # capture id possible à 2 endroits selon les cas
    cap_id = None
    try:
        cap_id = data["purchase_units"][0]["payments"]["captures"][0]["id"]
    except Exception:
        cap_id = data.get("id")

    return {"id": cap_id, "status": status, "pre": pre, "raw": data}

def verify_paypal_capture(capture_id: str) -> bool:
    if not capture_id:
        return False
    token = paypal_access_token()
    r = requests.get(
        f"{PAYPAL_API_BASE}/v2/payments/captures/{capture_id}",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        timeout=HTTP_TIMEOUT
    )
    if r.status_code >= 400:
        app.logger.error("paypal_verify_error: %s", r.text)
        return False
    return (r.json().get("status") == "COMPLETED")
# ------------------------------------------------------------------

# --- ALIAS D’ENDPOINTS (secours) : ne fait rien si déjà présents ---
if "tours" not in app.view_functions:
    @app.route("/tours", endpoint="tours", methods=["GET"])
    def __tours_alias():
        # Rend le template attendu par index.html
        return render_template("tours.html")

if "transport" not in app.view_functions:
    @app.route("/transport", endpoint="transport", methods=["GET"])
    def __transport_alias():
        return render_template("transport.html")
# -------------------------------------------------------------------

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT","10000")), debug=bool(os.getenv("DEBUG","0")=="1"))
