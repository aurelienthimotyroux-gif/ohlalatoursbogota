from flask import Flask, render_template, request, url_for, flash, redirect, abort, send_from_directory, g
from flask_babel import Babel, _
import os
import requests
import logging
from base64 import b64encode
from typing import Optional, Dict, Any
from datetime import timedelta
from werkzeug.middleware.proxy_fix import ProxyFix
from urllib.parse import urlparse

# ------------------------------------------------------------------
# Flask & Babel
# ------------------------------------------------------------------
# 👇 on précise explicitement où sont les templates et les fichiers statiques
app = Flask(__name__, template_folder="templates", static_folder="static")

# nécessaire pour flash()  ⚠️ remplace en production (ou mets via env SECRET_KEY)
app.secret_key = os.getenv("SECRET_KEY", "change-me-please")

# Durcissement cookies session en prod
app.config.setdefault("SESSION_COOKIE_SAMESITE", "Lax")
app.config.setdefault("SESSION_COOKIE_HTTPONLY", True)
app.config.setdefault("PERMANENT_SESSION_LIFETIME", timedelta(days=7))
# En prod derrière Render (TLS terminé côté proxy)
if os.getenv("FLASK_ENV") == "production":
    app.config.setdefault("SESSION_COOKIE_SECURE", True)

# Fixe les en-têtes X-Forwarded-* de Render pour que request.is_secure soit correct
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

# --- Config i18n ---
app.config["BABEL_DEFAULT_LOCALE"] = "fr"
app.config["BABEL_SUPPORTED_LOCALES"] = ["fr", "en", "es"]
app.config["BABEL_TRANSLATION_DIRECTORIES"] = "translations"

babel = Babel(app)

@babel.localeselector
def get_locale():
    # Priorité à ?lang=fr|en|es
    lang = request.args.get("lang")
    if lang in app.config["BABEL_SUPPORTED_LOCALES"]:
        return lang
    # Sinon, retombe sur la locale par défaut
    return app.config["BABEL_DEFAULT_LOCALE"]

# Helpers Jinja
app.jinja_env.globals["get_locale"] = get_locale

def lang_url(lang_code: str):
    """
    Conserve les paramètres existants et force lang=lang_code
    """
    args = request.args.to_dict(flat=True)
    args["lang"] = lang_code
    endpoint = request.endpoint or "index"
    return url_for(endpoint, **args)

app.jinja_env.globals["lang_url"] = lang_url

# ------------------------------------------------------------------
# Config PayPal (via variables d'environnement Render)
# ------------------------------------------------------------------
PAYPAL_MODE = os.getenv("PAYPAL_MODE", "sandbox")             # 'sandbox' ou 'live'
PAYPAL_CLIENT_ID = os.getenv("PAYPAL_CLIENT_ID", "")
PAYPAL_SECRET = os.getenv("PAYPAL_SECRET", "")
PAYPAL_BASE = (
    "https://api-m.sandbox.paypal.com" if PAYPAL_MODE == "sandbox"
    else "https://api-m.paypal.com"
)

# Webhook (optionnel)
PAYPAL_WEBHOOK_ID = os.getenv("PAYPAL_WEBHOOK_ID", "")

# ------------------------------------------------------------------
# Logging robuste
# ------------------------------------------------------------------
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("app")

# Ajoute un request_id pour corréler les logs
@app.before_request
def _attach_request_id():
    g.request_id = request.headers.get("X-Request-ID") or os.urandom(8).hex()

@app.after_request
def _attach_security_headers(resp):
    # HSTS seulement si HTTPS (Render l'est en prod)
    if request.is_secure:
        resp.headers.setdefault("Strict-Transport-Security", "max-age=63072000; includeSubDomains; preload")
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    # CSP compatible PayPal (boutons/iframes/scripts/images)
    # Ajuste selon tes besoins si d'autres CDN/scripts sont utilisés
    csp = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' https://www.paypal.com https://www.paypalobjects.com; "
        "connect-src 'self' https://api-m.paypal.com https://api-m.sandbox.paypal.com https://www.paypal.com; "
        "img-src 'self' data: https://www.paypalobjects.com https://www.paypal.com; "
        "style-src 'self' 'unsafe-inline'; "
        "frame-src https://www.paypal.com https://www.sandbox.paypal.com;"
    )
    # N'impose pas CSP si tu développes d'autres intégrations incompatibles
    resp.headers.setdefault("Content-Security-Policy", csp)
    # Expose l'id de requête pour le frontend / support
    resp.headers.setdefault("X-Request-ID", g.request_id)
    return resp

# ------------------------------------------------------------------
# Client HTTP Requests avec Retry/Timeout globaux
# ------------------------------------------------------------------
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

_SESSION = requests.Session()
_RETRY = Retry(
    total=3,
    backoff_factor=0.3,
    status_forcelist=(429, 500, 502, 503, 504),
    allowed_methods=("GET", "POST"),
)
_SESSION.mount("https://", HTTPAdapter(max_retries=_RETRY))
_DEFAULT_TIMEOUT = int(os.getenv("HTTP_TIMEOUT", "20"))


def _http_get(url: str, **kwargs):
    kwargs.setdefault("timeout", _DEFAULT_TIMEOUT)
    return _SESSION.get(url, **kwargs)


def _http_post(url: str, **kwargs):
    kwargs.setdefault("timeout", _DEFAULT_TIMEOUT)
    return _SESSION.post(url, **kwargs)


def paypal_access_token() -> str:
    """Récupère un access token PayPal (client_credentials)."""
    if not PAYPAL_CLIENT_ID or not PAYPAL_SECRET:
        raise RuntimeError("Clés PayPal manquantes (PAYPAL_CLIENT_ID/SECRET).")
    auth = b64encode(f"{PAYPAL_CLIENT_ID}:{PAYPAL_SECRET}".encode()).decode()
    r = _http_post(
        f"{PAYPAL_BASE}/v1/oauth2/token",
        headers={"Authorization": f"Basic {auth}"},
        data={"grant_type": "client_credentials"},
    )
    r.raise_for_status()
    return r.json()["access_token"]

# ------------------------------------------------------------------
# Données tours (utilisable dans réservation si besoin)
# ------------------------------------------------------------------
TOURS_LIST = [
    {"id": "candelaria", "name": "Visite historique de La Candelaria", "price": 20, "currency": "EUR"},
    {"id": "monserrate", "name": "Randonnée à Monserrate", "price": 15, "currency": "EUR"},
    {"id": "zipaquira",  "name": "Excursion à la Cathédrale de sel de Zipaquirá", "price": 40, "currency": "EUR"},
    {"id": "chorrera",   "name": "Cascade de La Chorrera", "price": 55, "currency": "USD"},
    {"id": "finca-cafe", "name": "Visite d’une finca à café", "price": 50, "currency": "USD"},
]

# ------------------------------------------------------------------
# Commentaires (Index)
# ------------------------------------------------------------------
@app.route("/submit_comment", methods=["POST"])
def submit_comment():
    name = request.form.get("name", "").strip()
    message = request.form.get("message", "").strip()
    rating_raw = request.form.get("rating", "1")
    try:
        rating = int(rating_raw)
    except ValueError:
        rating = 1

    if not name or not message:
        flash(_("Merci d’indiquer un nom et un message."), "error")
        return redirect(url_for("index", lang=get_locale()))

    # Exemple d'utilisation du request_id dans les logs
    logger.info("comment_submitted name=%s rating=%s req_id=%s", name, rating, g.request_id)

    flash(_("Merci pour votre joli commentaire ! ❤️"), "success")
    return redirect(url_for("index", lang=get_locale()))

# ------------------------------------------------------------------
# Pages principales
# ------------------------------------------------------------------
@app.route("/", endpoint="index")
def index():
    return render_template("index.html")

@app.route("/a-propos", endpoint="about")
def about():
    return render_template("about.html")

@app.route("/tours", endpoint="tours")
def tours_page():
    return render_template("tours.html")

@app.route("/contact", endpoint="contact")
def contact_page():
    return render_template("contact.html")

# ------------------------------------------------------------------
# Réservation
# ------------------------------------------------------------------
@app.route("/reservation", methods=["GET", "POST"], endpoint="reservation")
def reservation_page():
    # Pré-sélection depuis ?tour=...
    selected_tour = request.args.get("tour")

    if request.method == "POST":
        # Ici tu peux enregistrer la demande, envoyer un email, etc.
        # nom = request.form.get("nom")
        # email = request.form.get("email")
        # ...
        logger.info("reservation_post req_id=%s", g.request_id)
        pass

    return render_template("reservation.html", tours=TOURS_LIST, tour=selected_tour)

# ------------------------------------------------------------------
# Service de transport
# ------------------------------------------------------------------
@app.route("/transport", endpoint="transport")
def transport_page():
    return render_template("transport.html")

# ------------------------------------------------------------------
# Pages de retour PayPal
#   - Renforcé: accepte GET, récupère orderID/token, vérifie statut,
#     capture si besoin, et gère les erreurs proprement.
# ------------------------------------------------------------------
@app.route("/paiement-reussi", methods=["GET"])
def paiement_reussi():
    order_id = request.args.get("orderID") or request.args.get("token")
    if not order_id:
        abort(400, "orderID manquant")

    try:
        token = paypal_access_token()

        # 1) Lire le statut actuel de la commande
        info = _http_get(
            f"{PAYPAL_BASE}/v2/checkout/orders/{order_id}",
            headers={"Authorization": f"Bearer {token}"},
        )
        info.raise_for_status()
        order = info.json()
        status = order.get("status", "UNKNOWN")

        # 2) Si approuvée mais non capturée, capturer maintenant
        if status == "APPROVED":
            cap = _http_post(
                f"{PAYPAL_BASE}/v2/checkout/orders/{order_id}/capture",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json"
                },
            )
            if cap.status_code in (200, 201):
                status = cap.json().get("status", "COMPLETED")
            elif cap.status_code == 422:
                # déjà capturée
                status = "COMPLETED"

        if status != "COMPLETED":
            logger.warning("paypal_unexpected_status status=%s order=%s req_id=%s", status, order_id, g.request_id)
            return render_template("paiement_erreur.html", message=f"Statut PayPal: {status}"), 400

        # 👉 TODO: marquer ici la réservation liée à order_id comme 'PAID'
        # confirm_booking_paid(order_id)
        logger.info("paypal_completed order=%s req_id=%s", order_id, g.request_id)

        return render_template("success.html", order_id=order_id)

    except requests.HTTPError:
        logger.exception("Erreur PayPal (HTTP) req_id=%s", g.request_id)
        return render_template("paiement_erreur.html", message="Erreur PayPal"), 500
    except Exception:
        logger.exception("Erreur serveur req_id=%s", g.request_id)
        return render_template("paiement_erreur.html", message="Erreur interne"), 500

@app.route("/paiement-annule")
def paiement_annule():
    return render_template("cancel.html")

# ------------------------------------------------------------------
# Webhook PayPal (optionnel) – sécurisé avec vérification de signature
# ------------------------------------------------------------------
@app.post("/paypal/webhook")
def paypal_webhook():
    if not PAYPAL_WEBHOOK_ID:
        # Webhook non configuré
        return ("webhook not configured", 501)

    try:
        body = request.get_data(as_text=True)
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
        vr = _http_post(
            f"{PAYPAL_BASE}/v1/notifications/verify-webhook-signature",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json=verify_payload,
        )
        vr.raise_for_status()
        if vr.json().get("verification_status") != "SUCCESS":
            logger.warning("paypal_webhook_bad_sig req_id=%s", g.request_id)
            return ("invalid signature", 400)

        event_type = event.get("event_type")
        resource = event.get("resource", {})
        logger.info("paypal_webhook type=%s id=%s req_id=%s", event_type, event.get("id"), g.request_id)

        # TODO: idempotence + persistance
        # TODO: router selon event_type et mettre à jour vos réservations

        return ("", 200)
    except Exception:
        logger.exception("paypal_webhook_error req_id=%s", g.request_id)
        return ("error", 500)

# ------------------------------------------------------------------
# sitemaps & robots
# ------------------------------------------------------------------
@app.route('/sitemap.xml')
def sitemap_xml():
    return send_from_directory(app.static_folder, 'sitemap.xml', mimetype='application/xml')

@app.route('/robots.txt')
def robots_txt():
    return send_from_directory(app.static_folder, 'robots.txt', mimetype='text/plain')

# ------------------------------------------------------------------
# Healthcheck & erreurs
# ------------------------------------------------------------------
@app.get("/healthz")
def healthz():
    return {"status": "ok", "mode": PAYPAL_MODE}, 200

@app.errorhandler(404)
def not_found(e):
    return render_template("404.html"), 404

@app.errorhandler(500)
def server_error(e):
    return render_template("500.html"), 500

# ------------------------------------------------------------------
# Entrée locale
# ------------------------------------------------------------------
if __name__ == "__main__":
    # En local uniquement : debug
    app.run(debug=True, use_reloader=False)


