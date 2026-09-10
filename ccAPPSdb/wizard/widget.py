from django.middleware.csrf import get_token
from django.utils.translation import gettext_lazy as _
from django.utils.encoding import force_str

from ccAPPSdb.common.dashboard import Dashboard, Widget


class WizardWidget(Widget):
    name = "wizard"
    title = _("Loading your first data")
    asynchronous = False
    size = "xl"

    def render(self, request=None):
        from ccAPPSdb.common.middleware import _thread_locals

        return (
            '<div class="row pt-3 pb-3">'
            '    <div class="col-auto mx-auto">'
            '        <h1 class="heading wizard-heading fw-bold">' + force_str(_("Two ways to get started quickly")) + '</h1>'
            '    </div>'
            '</div>'
            '<div class="row pb-3 gy-5" id="wizard">'
            '    <div class="col-md-6">'
            '        <div class="text-center">'
            '            <h2 class="heading wizard-heading">'
            '                <span class="fa-stack">'
            '                    <i class="fa fa-circle-o fa-stack-2x"></i>'
            '                    <strong class="fa-stack-1x">A</strong>'
            '                </span>'
            '            </h2>'
            '            <h1 class="heading wizard-heading">' + force_str(_("Start with one item")) + '</h1>'
            '            <div class="dropdown-center">'
            '                <button class="btn btn-primary btn-lg" type="button" data-bs-toggle="dropdown" aria-expanded="false">'
            + force_str(_("QUICKSTART")) +
            '                </button>'
            '                <ul class="dropdown-menu bg-primary">'
            '                <li><a href="%s/wizard/quickstart/forecast/" class="btn btn-primary w-100">' + force_str(_("FORECAST")) + '</a></li>'
            '                <li><a href="%s/wizard/quickstart/production/" class="btn btn-primary w-100">' + force_str(_("PRODUCTION")) + '</a></li>'
            '                </ul>'
            '            </div>'
            '        </div>'
            '    </div>'
            '    <div class="col-md-6">'
            '        <div class="text-center">'
            '        <h2 class="heading wizard-heading">'
            '            <span class="fa-stack">'
            '                <i class="fa fa-circle-o fa-stack-2x"></i>'
            '                <strong class="fa-stack-1x">B</strong>'
            '            </span>'
            '        </h2>'
            '            <h1 class="heading wizard-heading">' + force_str(_("Upload more data")) + '</h1>'
            '            <div class="dropdown-center">'
            '                <button class="btn btn-primary btn-lg" type="button" data-bs-toggle="dropdown" aria-expanded="false">'
            + force_str(_("UPLOAD")) +
            '                </button>'
            '                <ul class="dropdown-menu bg-primary">'
            '                <li><a class="btn btn-primary w-100" href="%s/wizard/load/forecast/">' + force_str(_("FORECAST")) + '</a></li>'
            '                <li><a class="btn btn-primary w-100" href="%s/wizard/load/production/">' + force_str(_("PRODUCTION")) + '</a></li>'
            '                </ul>'
            '            </div>'
            '        </div>'
            '    </div>'
            '</div>') % (
            _thread_locals.request.prefix,
            _thread_locals.request.prefix,
            _thread_locals.request.prefix,
            _thread_locals.request.prefix,
        )


Dashboard.register(WizardWidget)
