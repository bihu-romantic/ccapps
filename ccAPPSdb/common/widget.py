from django.conf import settings
from django.db import DEFAULT_DB_ALIAS
from django.http import HttpResponse
from django.utils import formats
from django.utils.encoding import force_str
from django.utils.html import escape
from django.utils.text import capfirst
from django.utils.timesince import timesince
from django.utils.translation import gettext_lazy as _

from ccAPPSdb import __version__
from ccAPPSdb.common.dashboard import Dashboard, Widget
from ccAPPSdb.common.models import Notification


class WelcomeWidget(Widget):
    name = "welcome"
    title = _("Welcome")
    tooltip = _("Some links to get started")
    asynchronous = False
    size = 'xl'

    @staticmethod
    def render(request):
        try:
            versionnumber = __version__.split(".", 2)
            docurl = "%s/docs/%s.%s/index.html" % (
                settings.DOCUMENTATION_URL,
                versionnumber[0],
                versionnumber[1],
            )
        except Exception:
            docurl = "%s/docs/current/index.html" % (settings.DOCUMENTATION_URL,)

        try:
            db = request.database
            if not db or db == DEFAULT_DB_ALIAS:
                prefix = ""
            else:
                prefix = "/%s" % db
        except Exception:
            prefix = ""
        return (
            _(
                """Welcome to the world's leading open source production planning tool!<br><br>
How to get started?
<ol>
<li>Check out the <span class="text-decoration-underline"><a href="%(docurl)s" target="_blank" rel="noopener">documentation</a></span></li>
<li>Visit and join the <span class="text-decoration-underline"><a href="https://github.com/ccAPPS/ccAPPS/discussions" target="_blank" rel="noopener">user community</a></span></li>
<li><span class="text-decoration-underline"><a href="https://ccAPPS.com/company/#contact" target="_blank" rel="noopener">Contact us</a></span></li>
</ol>
"""
            )
            % {"docurl": docurl, "prefix": prefix}
        )


Dashboard.register(WelcomeWidget)


class NewsWidget(Widget):
    name = "news"
    title = _("News")
    tooltip = _("Show the latest news items from the ccAPPS website")
    asynchronous = False
    size = 'md'

    @staticmethod
    def render(request):
        return '<iframe style="width:100%; border:none;" src="https://ccAPPS.com/news-summary/"></iframe>'


Dashboard.register(NewsWidget)


class InboxWidget(Widget):
    name = "inbox"
    title = _("inbox")
    tooltip = _("Unread messages from your inbox")
    url = "/inbox/"
    asynchronous = False
    size = 'md'
    limit = 10

    @classmethod
    def render(cls, request):
        notifs = list(
            Notification.objects.using(request.database)
            .filter(user=request.user)
            .order_by("-id")
            .select_related("comment", "user")[: cls.limit]
        )
        if not notifs:
            return """
              <div class="pull-left"><i class="fa fa-5x fa-trophy" style="color: gold"></i>&nbsp;&nbsp;</div>
              <h2>Congrats!</h2>
              Your inbox is empty.
              """
        result = []
        result.append(
            '<div class="table-responsive"><table class="table table-sm table-hover"><tbody>'
        )
        for notif in notifs:
            result.append(
                """<tr><td>
                <a class="text-decoration-underline" href="%s%s">%s</a>&nbsp;<span class="small">%s</span>
                <div class="small pull-right" data-bs-toggle="tooltip" data-bs-title="%s %s">%s%s&nbsp;&nbsp;%s</div>
                <br><p style="padding-left: 10px; display: inline-block">%s</p>"""
                % (
                    request.prefix,
                    notif.comment.getURL(),
                    notif.comment.object_repr,
                    escape(
                        capfirst(force_str(_(notif.comment.content_type.name)))
                        if notif.comment.content_type
                        else ""
                    ),
                    escape(notif.comment.user.get_full_name()),
                    formats.date_format(notif.comment.lastmodified, "DATETIME_FORMAT"),
                    (
                        '<img class="avatar-sm" src="/uploads/%s">&nbsp;'
                        % notif.comment.user.avatar
                        if notif.comment.user.avatar
                        else ""
                    ),
                    escape(notif.comment.user.username),
                    timesince(notif.comment.lastmodified),
                    (
                        notif.comment.comment
                        if notif.comment.safe()
                        else escape(notif.comment.comment)
                    ),
                )
                + "</td></tr>"
            )
        result.append("</tbody></table></div>")
        return "\n".join(result)

    javascript = """
    var hasForecast = %s;
    var hasIP = %s;
    """ % (
        "true" if "ccAPPSdb.forecast" in settings.INSTALLED_APPS else "false",
        "true" if "ccAPPSdb.inventoryplanning" in settings.INSTALLED_APPS else "false",
    )


Dashboard.register(InboxWidget)


class SupplyChainGraphWidget(Widget):
    name = "supplychaingraph"
    title = _("产品路径图")
    tooltip = _("选择一个产品查看其供应链路径")
    asynchronous = True
    size = "xl"

    javascript = r"""
var dropdown = document.getElementById('supplychaingraph-item');
var iframe = document.getElementById('supplychaingraph');
if (!dropdown || !iframe) return;

function loadPath(itemName) {
    var prefix = dropdown.getAttribute('data-prefix') || '';
    iframe.src = prefix + '/supplypath/embed/' + encodeURIComponent(itemName) + '/';
}

if (dropdown.value) loadPath(dropdown.value);

dropdown.addEventListener('change', function() {
    if (this.value) loadPath(this.value);
});
"""

    @classmethod
    def render(cls, request):
        from django.db import connections

        with connections[request.database].cursor() as cursor:
            cursor.execute("SELECT name FROM item ORDER BY name")
            items = [row[0] for row in cursor.fetchall()]

        options = []
        first_item = items[0] if items else ""
        for item in items:
            selected = 'selected' if item == first_item else ''
            options.append(f'<option value="{item}" {selected}>{item}</option>')

        prefix = request.prefix
        html = [
            '<select id="supplychaingraph-item" data-prefix="%s" style="margin-bottom:8px;padding:4px 8px;font-size:14px;border:1px solid #ccc;border-radius:4px;min-width:200px">' % prefix,
        ]
        html.extend(options)
        html.append('</select>')
        html.append('<iframe id="supplychaingraph" src="%s/supplypath/embed/%s/" style="width:100%%;height:800px;border:1px solid #dee2e6;border-radius:4px"></iframe>' % (prefix, first_item))
        return HttpResponse("\n".join(html))


Dashboard.register(SupplyChainGraphWidget)

