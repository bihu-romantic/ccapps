from django.conf import settings
from django.db.utils import DEFAULT_DB_ALIAS
from django.utils.translation import gettext_lazy as _

import ccAPPSdb.common.views
from ccAPPSdb.common.models import (
    APIKey,
    Attribute,
    User,
    Bucket,
    BucketDetail,
    Parameter,
    Comment,
)
from ccAPPSdb.menu import menu
from ccAPPSdb import __version__


def isDefaultDatabase(request):
    return getattr(request, "database", DEFAULT_DB_ALIAS) == DEFAULT_DB_ALIAS


def hasInstallableApps(request):
    return (
        request.user.is_superuser
        and hasattr(settings, "INSTALLABLE_APPS")
        and getattr(request, "database", DEFAULT_DB_ALIAS) == DEFAULT_DB_ALIAS
    )


# Settings menu
menu.addItem(
    "admin",
    "parameter admin",
    url="/data/common/parameter/",
    report=ccAPPSdb.common.views.ParameterList,
    index=1100,
    model=Parameter,
    admin=True,
)
menu.addItem(
    "admin",
    "attribute admin",
    url="/data/common/attribute/",
    report=ccAPPSdb.common.views.AttributeList,
    index=1150,
    model=Attribute,
    admin=True,
    dependencies=[
        isDefaultDatabase,
    ],
)
menu.addItem(
    "admin",
    "apps",
    url="/apps/",
    label=_("Apps"),
    index=1160,
    dependencies=[
        hasInstallableApps,
    ],
)
menu.addItem(
    "admin",
    "apikey admin",
    url="/data/common/apikey/",
    report=ccAPPSdb.common.views.APIKeyList,
    index=1170,
    model=APIKey,
    admin=True,
    dependencies=[
        isDefaultDatabase,
    ],
)
menu.addItem(
    "admin",
    "bucket admin",
    url="/data/common/bucket/",
    report=ccAPPSdb.common.views.BucketList,
    index=1200,
    model=Bucket,
    admin=True,
)
menu.addItem(
    "admin",
    "bucketdetail admin",
    url="/data/common/bucketdetail/",
    report=ccAPPSdb.common.views.BucketDetailList,
    index=1300,
    model=BucketDetail,
    admin=True,
)
menu.addItem(
    "admin",
    "comment admin",
    url="/data/common/comment/",
    report=ccAPPSdb.common.views.CommentList,
    index=1400,
    model=Comment,
    admin=True,
)

# User maintenance
menu.addItem("admin", "users", separator=True, index=2000)
menu.addItem(
    "admin",
    "user admin",
    url="/data/common/user/",
    report=ccAPPSdb.common.views.UserList,
    index=2100,
    model=User,
    admin=True,
)
menu.addItem(
    "admin",
    "group admin",
    url="/data/auth/group/",
    report=ccAPPSdb.common.views.GroupList,
    index=2200,
    permission="auth.change_group",
    admin=True,
)

# Help menu
try:
    versionnumber = __version__.split(".", 2)
    docurl = "%s/docs/%s.%s/index.html" % (
        settings.DOCUMENTATION_URL,
        versionnumber[0],
        versionnumber[1],
    )
except Exception:
    docurl = "%s/docs/current/index.html" % (settings.DOCUMENTATION_URL,)
menu.addItem(
    "help",
    "documentation",
    url=docurl,
    label=_("Documentation"),
    window=True,
    prefix=False,
    index=300,
)
menu.addItem(
    "help",
    "API",
    url="/api/",
    label=_("REST API help"),
    window=False,
    prefix=True,
    index=400,
)
# menu.addItem(
#     "help",
#     "website",
#     url="https://ccAPPS.com",
#     window=True,
#     label=_("ccAPPS website"),
#     prefix=False,
#     index=500,
# )
# menu.addItem(
#     "help",
#     "release notes",
#     url="https://ccAPPS.com/docs/current/release-notes.html",
#     window=True,
#     label=_("Release notes"),
#     prefix=False,
#     index=501,
# )

# menu.addItem(
#     "help", "about", javascript="about_show()", label=_("About ccAPPS"), index=600
# )
