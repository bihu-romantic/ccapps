# Mark this app as an ERP integration module.
# Only one such app can be active on a ccAPPS instance.
ERP_module = True

# Metadata as a ccAPPS app
CCAPPS_app = {
    "summary": "Generic ERP connector",
    "description": """
        <p>This app provides a skeleton for two-way integration
        with your custom ERP system.</p>
        <p>Clone and customize this app to suit your needs.</p>
        """,
    "documentation_url": "https://ccAPPS.com/docs/current/apps/erpconnection.html",
    "ERP_integration": True,
}
