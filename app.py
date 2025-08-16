from flask import Flask, render_template, request, url_for, flash, redirect, abort, send_from_directory, g, session
from flask_babel import Babel, _
import os, requests, logging, json, tempfile
from base64 import b64encode
from datetime import timedelta, datetime
from werkzeug.middleware.proxy_fix import ProxyFix
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ------------------------------------------------------------------
# App & i18n
# ------------------------------------------------------------------
app = Flask(__name__, template_folder="templates", static_folder="static")
app.secret_key = os.getenv("SECRET_KEY", "change-me-please")

app.config.setdefault("SESSION_COOKIE_SAMESITE", "Lax")
app.config.setdefault("SESSION_COOKIE_HTTPONLY", True)
app.config.setdefault("PERMANENT_SESSION_LIFETIME", timedelta(days=7))
if os.getenv("FLASK_ENV") == "production":
    app.config.setdefault("SESSION_COOKIE_SECURE", True)

app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

app.config["BABEL_DEFAULT_LOCALE"] = "fr"
app.config["BABEL_SUPPORTED_LOCALES"] = ["fr", "en", "es"]
app.config["BABEL_TRANSLATION_DIRECTORIES"] = "translations"
babel = Babel(app)

@babel.localeselector
def get_locale():
    lang = request.args.get("lang")
    return lang if lang in app.config["BABEL_SUPPORTED_LOCALES"] else app.config["BABEL_DEFAULT_LOCALE"]

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
from flask_sqlalchemy import SQLAlchemy

def _normalized_db_url():
    raw = os.getenv("DATABASE_URL", "").strip()
    if not raw:
        return "sqlite:///comments.sqlite3"
    if raw.startswith("postgres://"):
        raw = raw.replace("postgres://", "postgresql+psycopg2://", 1)
    elif raw.startswith("postgresql://") and "+psycopg2" not in raw:
        raw = raw.replace("postgresql://", "postgresql+psycopg2://", 1)
    return raw

app.config["SQLALCHEMY_DATABASE_URI"] = _normalized_db_url()
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
db = SQLAlchemy(app)

class Comment(db.Model):
    __tablename__ = "comments"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    country = db.Column(db.String(120), default="")
    date_str = db.Column(db.String(32), nullable=False)   # “12 mai 2025” (ton format)
    rating = db.Column(db.Float, default=5.0)
    message = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)

with app.app_context():
    db.create_all()

# ------------------------------------------------------------------
# PayPal & admin
# ------------------------------------------------------------------
PAYPAL_MODE = os.getenv("PAYPAL_MODE", "sandbox")
PAYPAL_CLIENT_ID = os.getenv("PAYPAL_CLIENT_ID", "")
PAYPAL_SECRET = os.getenv("PAYPAL_SECRET", "")
PAYPAL_BASE = "https://api-m.sandbox.paypal.com" if PAYPAL_MODE == "sandbox" else "https://api-m.paypal.com"
PAYPAL_WEBHOOK_ID = os.getenv("PAYPAL_WEBHOOK_ID", "")
ADMIN_DELETE_TOKEN = os.getenv("ADMIN_DELETE_TOKEN", "")

# ------------------------------------------------------------------
# Logging & headers
# ------------------------------------------------------------------
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO),
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("app")

@app.before_request
def _attach_request_id():
    g.request_id = request.headers.get("X-Request-ID") or os.urandom(8).hex()

@app.after_request
def _attach_security_headers(resp):
    if request.is_secure:
        resp.headers.setdefault("Strict-Transport-Security", "max-age=63072000; includeSubDomains; preload")
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    csp = ("default-src 'self'; "
           "script-src 'self' 'unsafe-inline' https://www.paypal.com https://www.paypalobjects.com; "
           "connect-src 'self' https://api-m.paypal.com https://api-m.sandbox.paypal.com https://www.paypal.com; "
           "img-src 'self' data: https://www.paypalobjects.com https://www.paypal.com; "
           "style-src 'self' 'unsafe-inline'; "
           "frame-src https://www.paypal.com https://www.sandbox.paypal.com;")
    resp.headers.setdefault("Content-Security-Policy", csp)
    resp.headers.setdefault("X-Request-ID", g.request_id)
    return resp

# ------------------------------------------------------------------
# HTTP client (timeouts + retry)
# ------------------------------------------------------------------
_SESSION = requests.Session()
_RETRY = Retry(total=3, backoff_factor=0.3, status_forcelist=(429, 500, 502, 503, 504), allowed_methods=("GET", "POST"))
_SESSION.mount("https://", HTTPAdapter(max_retries=_RETRY))
_DEFAULT_TIMEOUT = int(os.getenv("HTTP_TIMEOUT", "20"))
def _http_get(url: str, **kwargs):
    kwargs.setdefault("timeout", _DEFAULT_TIMEOUT)
    return _SESSION.get(url, **kwargs)
def _http_post(url: str, **kwargs):
    kwargs.setdefault("timeout", _DEFAULT_TIMEOUT)
    return _SESSION.post(url, **kwargs)
def paypal_access_token() -> str:
    if not PAYPAL_CLIENT_ID or not PAYPAL_SECRET:
        raise RuntimeError("Clés PayPal manquantes (PAYPAL_CLIENT_ID/SECRET).")
    auth = b64encode(f"{PAYPAL_CLIENT_ID}:{PAYPAL_SECRET}".encode()).decode()
    r = _http_post(f"{PAYPAL_BASE}/v1/oauth2/token",
                   headers={"Authorization": f"Basic {auth}"},
                   data={"grant_type": "client_credentials"})
    r.raise_for_status()
    return r.json()["access_token"]

# ------------------------------------------------------------------
# Données tours (affichage)
# ------------------------------------------------------------------
TOURS_LIST = [
    {"id": "candelaria", "name": "Visite historique de La Candelaria", "price": 20, "currency": "EUR"},
    {"id": "monserrate", "name": "Randonnée à Monserrate", "price": 15, "currency": "EUR"},
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

    c = Comment(
        name=name,
        country="",
        date_str=datetime.now().strftime("%d %b %Y"),
        rating=rating,
        message=message,
    )
    db.session.add(c)
    db.session.commit()

    flash(_("Merci pour votre joli commentaire ! ❤️"), "success")
    return redirect(url_for("index", lang=get_locale()))

# ------------------------------------------------------------------
# Admin
# ------------------------------------------------------------------
@app.get("/admin")
def admin_login():
    key = request.args.get("key", "")
    if ADMIN_DELETE_TOKEN and key == ADMIN_DELETE_TOKEN:
        session["is_admin"] = True
        flash(_("Mode administrateur activé."), "success")
    else:
        flash(_("Clé admin invalide."), "error")
    return redirect(url_for("index", lang=get_locale()))

@app.get("/admin/logout")
def admin_logout():
    session.pop("is_admin", None)
    flash(_("Mode administrateur désactivé."), "success")
    return redirect(url_for("index", lang=get_locale()))

@app.post("/delete_comment")
def delete_comment():
    if not session.get("is_admin"):
        abort(403)
    cid_raw = request.form.get("id")
    try:
        cid = int(cid_raw)
    except (TypeError, ValueError):
        flash(_("Identifiant invalide."), "error")
        return redirect(url_for("index", lang=get_locale()))
    c = db.session.get(Comment, cid)
    if not c:
        flash(_("Commentaire introuvable."), "error")
        return redirect(url_for("index", lang=get_locale()))
    db.session.delete(c)
    db.session.commit()
    logger.info("comment_deleted id=%s name=%s req_id=%s", c.id, c.name, g.request_id)
    flash(_("Commentaire supprimé."), "success")
    return redirect(url_for("index", lang=get_locale()))

# ------------------------------------------------------------------
# Pages
# ------------------------------------------------------------------
@app.route("/", endpoint="index")
def index():
    comments = Comment.query.order_by(Comment.created_at.desc()).all()
    return render_template("index.html", comments=comments)

@app.route("/a-propos", endpoint="about")
def about():
    return render_template("about.html")

@app.route("/tours", endpoint="tours")
def tours_page():
    return render_template("tours.html")

@app.route("/contact", endpoint="contact")
def contact_page():
    return render_template("contact.html")

@app.route("/reservation", methods=["GET", "POST"], endpoint="reservation")
def reservation_page():
    selected_tour = request.args.get("tour")
    if request.method == "POST":
        logger.info("reservation_post req_id=%s", g.request_id)
    return render_template("reservation.html", tours=TOURS_LIST, tour=selected_tour)

@app.route("/transport", endpoint="transport")
def transport_page():
    return render_template("transport.html")

# ------------------------------------------------------------------
# PayPal return pages
# ------------------------------------------------------------------
@app.get("/paiement-reussi")
def paiement_reussi():
    order_id = request.args.get("orderID") or request.args.get("token")
    if not order_id:
        abort(400, "orderID manquant")
    try:
        token = paypal_access_token()
        info = _http_get(f"{PAYPAL_BASE}/v2/checkout/orders/{order_id}",
                         headers={"Authorization": f"Bearer {token}"})
        info.raise_for_status()
        order = info.json()
        status = order.get("status", "UNKNOWN")
        if status == "APPROVED":
            cap = _http_post(f"{PAYPAL_BASE}/v2/checkout/orders/{order_id}/capture",
                             headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
            if cap.status_code in (200, 201):
                status = cap.json().get("status", "COMPLETED")
            elif cap.status_code == 422:
                status = "COMPLETED"
        if status != "COMPLETED":
            logger.warning("paypal_unexpected_status status=%s order=%s req_id=%s", status, order_id, g.request_id)
            return render_template("paiement_erreur.html", message=f"Statut PayPal: {status}"), 400
        logger.info("paypal_completed order=%s req_id=%s", order_id, g.request_id)
        return render_template("success.html", order_id=order_id)
    except requests.HTTPError:
        logger.exception("Erreur PayPal (HTTP) req_id=%s", g.request_id)
        return render_template("paiement_erreur.html", message="Erreur PayPal"), 500
    except Exception:
        logger.exception("Erreur serveur req_id=%s", g.request_id)
        return render_template("paiement_erreur.html", message="Erreur interne"), 500

@app.get("/paiement-annule")
def paiement_annule():
    return render_template("cancel.html")

# ------------------------------------------------------------------
# Webhook PayPal
# ------------------------------------------------------------------
@app.post("/paypal/webhook")
def paypal_webhook():
    if not PAYPAL_WEBHOOK_ID:
        return ("webhook not configured", 501)
    try:
        event = request.get_json(silent=True) or {}
        token = paypal_access_token()
        verify_payload = {
            "transmission_id": request.headers.get("Paypal-Transmission-Id"),
            "transmission_time": request.headers.get("Paypal-Transmission-Time"),
            "cert_url": request.headers.get("Paypal-Cert-Url"),
            "auth_algo": request.headers.get("Paypal-Auth-Algo"),
            "transmission_sig": request.headers.get("Paypal-Transmission-Sig"),
            "webhook_id": PAYPAL_WEBHOOK_ID,
            "webhook_event": event,
        }
        vr = _http_post(f"{PAYPAL_BASE}/v1/notifications/verify-webhook-signature",
                        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                        json=verify_payload)
        vr.raise_for_status()
        if vr.json().get("verification_status") != "SUCCESS":
            logger.warning("paypal_webhook_bad_sig req_id=%s", g.request_id)
            return ("invalid signature", 400)
        logger.info("paypal_webhook type=%s id=%s req_id=%s", event.get("event_type"), event.get("id"), g.request_id)
        return ("", 200)
    except Exception:
        logger.exception("paypal_webhook_error req_id=%s", g.request_id)
        return ("error", 500)

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
    return render_template("500.html"), 500

if __name__ == "__main__":
    app.run(debug=True, use_reloader=False)

