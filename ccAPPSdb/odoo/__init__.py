# Mark this app as an ERP integration module.
# Only one such app can be active on a ccAPPS instance.
ERP_module = True

# Metadata as a ccAPPS app
CCAPPS_app = {
    "summary": "Odoo integration",
    "description": """
        <p>This app enables two-way integration with the Odoo, a popular
        open source ERP and business management suite.</p>
        """,
    "documentation_url": "https://ccAPPS.com/docs/current/apps/odoo.html",
    "ERP_integration": True,
}
