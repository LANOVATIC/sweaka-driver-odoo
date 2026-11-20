{
    "name": "Sweaka Driver Connect",
    "summary": "Sweaka Driver mobile app for your Odoo: van sales, orders, payments, returns, van stock, day close",
    "version": "19.0.0.1.0",
    "category": "Sales",
    "author": "Sweaka",
    "website": "https://sweaka.com",
    "support": "contact@sweaka.com",
    "license": "OPL-1",
    "images": ["static/description/banner.png"],  # Apps store card; price/currency added by build.py --price (T9.6.10.3)
    "depends": ["sale_stock", "account"],
    "data": [
        "security/ir.model.access.csv",
        "views/res_users_views.xml",
        "views/res_company_views.xml",
        "views/driver_docs_views.xml",
        "data/ir_cron.xml",
        "data/ir_sequence.xml",
    ],
    "external_dependencies": {"python": []},  # sweaka_connect_core vendored in lib/ (T9.5.3)
    "installable": True,
    "application": False,
}
