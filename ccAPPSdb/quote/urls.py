"""
URL routing for Smart Quote API.
"""

from django.urls import re_path

from . import views

urlpatterns = [
    re_path(
        r"^api/quote/analyze/(?P<demand_name>.+)/$",
        views.SmartQuoteAnalyzeView.as_view(),
        name="quote_analyze",
    ),
    re_path(
        r"^api/quote/history/(?P<item_id>.+)/$",
        views.SmartQuoteHistoryView.as_view(),
        name="quote_history",
    ),
    re_path(
        r"^api/quote/compare/(?P<demand_name>.+)/$",
        views.SmartQuoteCompareView.as_view(),
        name="quote_compare",
    ),
    re_path(
        r"^api/quote/similar/(?P<demand_name>.+)/$",
        views.SmartQuoteSimilarOrdersView.as_view(),
        name="quote_similar_orders",
    ),
    re_path(
        r"^api/quote/volume-discount/(?P<item_id>.+)/$",
        views.SmartQuoteVolumeDiscountView.as_view(),
        name="quote_volume_discount",
    ),
    re_path(
        r"^api/quote/customer/(?P<customer_id>.+)/$",
        views.SmartQuoteCustomerView.as_view(),
        name="quote_customer",
    ),
    re_path(
        r"^api/quote/apply/$",
        views.SmartQuoteApplyView.as_view(),
        name="quote_apply",
    ),
]

# Automatically add these URLs when the application is installed
autodiscover = True
