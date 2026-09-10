from importlib import import_module

from django.conf import settings
from django.utils.translation import gettext_lazy as _

from ccAPPSdb.common.menus import Menu

# Create the navigation menu.
# This is the one and only menu object in the application.
menu = Menu()

# Add our default topics.
# Supply chain combines sales, inventory, capacity, purchasing, and manufacturing.
menu.addGroup("supply_chain", label=_("数据管理"), index=100)
menu.addGroup("distribution", label=_("distribution"), index=200)
menu.addGroup("execute", label=_("Execute"), index=250)
menu.addGroup("admin", label=_("admin"), index=300)
menu.addGroup("help", label=_("help"), index=800)
menu.addItem("supply_chain", "data", separator=True, index=1000)
menu.addItem("distribution", "data", separator=True, index=1000)
menu.addItem("execute", "data", separator=True, index=1000)
menu.addItem("admin", "data", separator=True, index=1000)

# Adding the menu modules of each installed application.
# Note that the menus of the apps are processed in reverse order.
# This is required to allow the first apps to override the entries
# of the later ones.
for app in reversed(settings.INSTALLED_APPS):
    try:
        mod = import_module("%s.menu" % app)
    except ImportError as e:
        # Silently ignore if it's the menu module which isn't found
        if str(e) not in (
            "No module named %s.menu" % app,
            "No module named '%s.menu'" % app,
        ):
            raise e
