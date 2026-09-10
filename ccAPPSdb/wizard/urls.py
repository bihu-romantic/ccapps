from django.urls import path

from ccAPPSdb import mode

# Automatically add these URLs when the application is installed
autodiscover = True

if mode == "WSGI":
    from . import views

    urlpatterns = [
        path(r"wizard/quickstart/forecast/", views.QuickStartForecast.as_view()),
        path(r"wizard/quickstart/production/", views.QuickStartProduction.as_view()),
        path(r"wizard/supplypath/", views.CheckSupplyPath),
        path(r"wizard/load/", views.WizardLoad),
        path(r"wizard/load/<str:mode>/", views.WizardLoad),
        path(r"wizard/sendsurveymail/", views.SendSurveyMail.action),
        path(r"wizard/features/", views.FeatureDashboard.as_view()),
    ]
