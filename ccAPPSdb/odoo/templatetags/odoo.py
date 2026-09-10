from django import template

from ccAPPSdb.common.models import Parameter

register = template.Library()


@register.simple_tag(takes_context=True)
def odooContext(context):
    request = context.get("request")
    out = {}
    for param in (
        Parameter.objects.using(request.database)
        .filter(name__in=["odoo.company", "odoo.db", "odoo.url", "odoo.singlecompany"])
        .only("name", "value")
    ):
        out[param.name[5:]] = param.value
    return out
