
"""Initialize the main blueprint and import its routes.

The blueprint owns its own templates/, static/ and data/ folders, so
the whole feature moves as one directory.
"""

from flask import Blueprint

bp_main = Blueprint(
    'main',
    __name__,
    template_folder='templates',
    static_folder='static',
    static_url_path='/static/main'
)

from main import routes

