from flask import Flask, render_template, request, url_for, flash, redirect, abort, send_from_directory
from flask_babel import Babel, _
import os
import requests
import logging
from base64 import b64encode

# ------------------------------------------------------------------
# Flask & Babel
# ------------------------------------------------------------------
# 👇 on précise explicitement où sont les templates et les fichiers statiques
app = Flask(__name__, template_folder="templates", static_folder="static")

# nécessaire pour flash()  ⚠️ remplace en production (ou mets via env SECRET_KEY)
app.secret_key = os.getenv("SECRET_KEY", "change-me-please")

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

logging.basicConfig(level=logging.INFO)

def paypal_access_token():
    """Récupère un access token PayPal (client_credentials)."""
    if not PAYPAL_CLIENT_ID or not PAYPAL_SECRET:
        raise RuntimeError("Clés PayPal manquantes (PAYPAL_CLIENT_ID/SECRET).")
    auth = b64encode(f"{PAYPAL_CLIENT_ID}:{PAYPAL_SECRET}".encode()).decode()
    r = requests.post(
        f"{PAYPAL_BASE}/v1/oauth2/token",
        headers={"Authorization": f"Basic {auth}"},
        data={"grant_type": "client_credentials"},
        timeout=20,
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
        info = requests.get(
            f"{PAYPAL_BASE}/v2/checkout/orders/{order_id}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=20,
        )
        info.raise_for_status()
        order = info.json()
        status = order.get("status", "UNKNOWN")

        # 2) Si approuvée mais non capturée, capturer maintenant
        if status == "APPROVED":
            cap = requests.post(
                f"{PAYPAL_BASE}/v2/checkout/orders/{order_id}/capture",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json"
                },
                timeout=20,
            )
            if cap.status_code in (200, 201):
                status = cap.json().get("status", "COMPLETED")
            elif cap.status_code == 422:
                # déjà capturée
                status = "COMPLETED"

        if status != "COMPLETED":
            app.logger.warning("Statut PayPal inattendu: %s (order %s)", status, order_id)
            return render_template("paiement_erreur.html", message=f"Statut PayPal: {status}"), 400

        # 👉 TODO: marquer ici la réservation liée à order_id comme 'PAID'
        # confirm_booking_paid(order_id)

        return render_template("success.html", order_id=order_id)

    except requests.HTTPError:
        app.logger.exception("Erreur PayPal (HTTP)")
        return render_template("paiement_erreur.html", message="Erreur PayPal"), 500
    except Exception:
        app.logger.exception("Erreur serveur")
        return render_template("paiement_erreur.html", message="Erreur interne"), 500

@app.route("/paiement-annule")
def paiement_annule():
    return render_template("cancel.html")

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
# Entrée locale
# ------------------------------------------------------------------
if __name__ == "__main__":
    app.run(debug=True, use_reloader=False)

