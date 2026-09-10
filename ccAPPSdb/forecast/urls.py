from django.urls import re_path, path

from ccAPPSdb import mode

# Automatically add these URLs when the application is installed
autodiscover = True

if mode == "ASGI":
    from . import services

    svcpatterns = [
        re_path(r"^forecast/detail/$", services.ForecastService.as_asgi()),
        re_path(r"^flush/manual/$", services.FlushService.as_asgi()),
        re_path(r"^flush/auto/$", services.FlushService.as_asgi()),
    ]

else:
    from . import views
    from . import serializers

    urlpatterns = [
        # Forecast editor screen
        path("forecast/editor/", views.ForecastEditor.planning),
        path("forecast/editor/<str:item>/", views.ForecastEditor.planning),
        path("forecast/detail/", views.ForecastEditor.detail),
        path("forecast/itemtree/", views.ForecastEditor.itemtree),
        path("forecast/locationtree/", views.ForecastEditor.locationtree),
        path("forecast/customertree/", views.ForecastEditor.customertree),
        path("data/forecast/forecast/", views.ForecastList.as_view()),
        path("data/forecast/measure/", views.MeasureList.as_view()),
        path("forecast/demand/", views.OrderReport.as_view()),
        re_path(
            "forecast/(.+)/$", views.OverviewReport.as_view(), name="forecast_plan"
        ),
        path(r"forecast/", views.OverviewReport.as_view()),
        re_path(
            r"^constraintforecast/(.+)/$",
            views.ConstraintReport.as_view(),
            name="forecast_constraint",
        ),
        re_path(
            r"^supplypath/forecast/(.+)/$",
            views.UpstreamForecastPath.as_view(),
            name="supplypath_forecast",
        ),
        re_path(
            r"^demandpegging/forecast/(.+)/$",
            views.PeggingReport.as_view(),
            name="forecast_plandetail",
        ),
        # REST API framework
        path("api/forecast/forecast/", serializers.ForecastAPI.as_view()),
        path("api/forecast/forecastplan/", serializers.ForecastPlanAPI.as_view()),
        re_path(
            r"^api/forecast/forecast/(?P<pk>(.+))/$",
            serializers.ForecastdetailAPI.as_view(),
        ),
        path("api/forecast/measure/", serializers.MeasureAPI.as_view()),
        re_path(
            r"^api/forecast/measure/(?P<pk>(.+))/$",
            serializers.MeasuredetailAPI.as_view(),
        ),
    ]
