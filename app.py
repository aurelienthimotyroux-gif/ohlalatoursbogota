from flask import (
    Flask, render_template, request, url_for, flash, redirect,
    send_from_directory, send_file, session, make_response, Response, abort
)
from flask_babel import Babel, _
import os, logging, re, secrets, unicodedata
from datetime import datetime
from werkzeug.middleware.proxy_fix import ProxyFix
from flask_sqlalchemy import SQLAlchemy
from functools import wraps
from sqlalchemy import inspect
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

# ------------------------------------------------------------------
# Utils
# ------------------------------------------------------------------
_MONTHS = {...}  # (idem, inchangé)

# parse_date_str, format_date_human, etc. (inchangé)
# ...

# ------------------------------------------------------------------
# App
# ------------------------------------------------------------------
app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "dev-secret")
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

# lang_url etc. (inchangé)
# ...

# ------------------------------------------------------------------
# DB Models
# ------------------------------------------------------------------
db = SQLAlchemy(app)

class Comment(db.Model):
    # ...

class CommentTranslation(db.Model):
    # ...

class Transfer(db.Model):
    # ...

with app.app_context():
    db.create_all()

# ------------------------------------------------------------------
# Admin / Comments / Transferts (inchangé)
# ------------------------------------------------------------------

# ------------------------------------------------------------------
# Routes publiques
# ------------------------------------------------------------------
@app.route("/")
def index():
    # ...
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

@app.post("/comments")
def submit_comment():
    # ...

@app.post("/transfer")
def submit_transfer():
    # ...

# ------------------------------------------------------------------
# Statique & SEO
# ------------------------------------------------------------------
@app.route('/robots.txt')
def robots_txt():
    return send_from_directory(app.static_folder, 'robots.txt', mimetype='text/plain')

@app.get("/sitemap.xml")
def sitemap_xml():
    return send_from_directory(app.static_folder, "sitemap.xml", mimetype="application/xml")

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
der via API, créer la réservation en DB et envoyer les emails.
    return {"ok": True}, 200

