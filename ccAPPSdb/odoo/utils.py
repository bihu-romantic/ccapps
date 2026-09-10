import os


def getOdooVersion(
    dockerfile=os.path.join(os.path.dirname(__file__), "odoo_addon", "dockerfile")
):
    """
    We look into the dockerfile to figure out odoo version.
    You can pass the path to the dockerfile, or we pick it up from odoo_addon subfolder.
    """
    try:
        with open(dockerfile, mode="rt") as f:
            for l in f.read().splitlines():
                if l.startswith("FROM "):
                    return l.split(":", 1)[-1]
    except Exception:
        raise Exception("Can't determine odoo version")
