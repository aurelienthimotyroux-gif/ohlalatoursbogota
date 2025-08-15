from flask import Flask, render_template, request, url_for, flash, redirect
from flask_babel import Babel, _

app = Flask(__name__)

# --- Config i18n ---
app.config["BABEL_DEFAULT_LOCALE"] = "fr"
app.config["BABEL_SUPPORTED_LOCALES"] = ["fr", "en", "es"]
app.config["BABEL_TRANSLATION_DIRECTORIES"] = "translations"

# nécessaire pour flash()
app.secret_key = "change-me-please"  # ⚠️ remplace en production

# Flask-Babel
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

# --- Données tours (utilisable dans réservation si besoin) ---
TOURS_LIST = [
    {"id": "candelaria", "name": "Visite historique de La Candelaria", "price": 20, "currency": "EUR"},
    {"id": "monserrate", "name": "Randonnée à Monserrate", "price": 15, "currency": "EUR"},
    {"id": "zipaquira",  "name": "Excursion à la Cathédrale de sel de Zipaquirá", "price": 40, "currency": "EUR"},
    {"id": "chorrera",   "name": "Cascade de La Chorrera", "price": 55, "currency": "USD"},
    {"id": "finca-cafe", "name": "Visite d’une finca à café", "price": 50, "currency": "USD"},
]

# --- Commentaires (Index) ---
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

# --- Pages principales ---
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

# --- Réservation ---
@app.route("/reservation", methods=["GET", "POST"], endpoint="reservation")
def reservation_page():
    # Pré-sélection depuis ?tour=...
    selected_tour = request.args.get("tour")

    if request.method == "POST":
        # Ici tu peux enregistrer la demande, envoyer un email, etc.
        # Exemple:
        # nom = request.form.get("nom")
        # email = request.form.get("email")
        # ...
        pass

    # Passe la liste des tours (si tu veux générer dynamiquement un <select>)
    return render_template("reservation.html", tours=TOURS_LIST, tour=selected_tour)

# --- Service de transport ---
@app.route("/transport", endpoint="transport")
def transport_page():
    return render_template("transport.html")

# --- Pages de retour PayPal (UNE SEULE FOIS) ---
@app.route("/paiement-reussi")
def paiement_reussi():
    order_id = request.args.get("orderID", "")
    return render_template("success.html", order_id=order_id)

@app.route("/paiement-annule")
def paiement_annule():
    return render_template("cancel.html")

if __name__ == "__main__":
    app.run(debug=True, use_reloader=False)

