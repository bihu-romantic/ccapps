from django.db import connections
from django.urls import re_path
from django.views.generic.base import RedirectView

from ccAPPSdb import mode

# Automatically add these URLs when the application is installed
autodiscover = True

if mode == "WSGI":
    import ccAPPSdb.common.views
    import ccAPPSdb.common.serializers
    import ccAPPSdb.common.dashboard

    from ccAPPSdb.common.api.views import APIIndexView
    from ccAPPSdb.common.registration.views import (
        ResetPasswordRequestView,
        PasswordResetConfirmView,
    )

    urlpatterns = [
        re_path(r"^uploads/(.+)$", ccAPPSdb.common.views.uploads, name="uploads"),
        re_path(r"^inbox/$", ccAPPSdb.common.views.inbox, name="inbox"),
        re_path(r"^follow/$", ccAPPSdb.common.views.follow, name="follow"),
        re_path(r"^$", ccAPPSdb.common.views.cockpit, name="cockpit"),
        re_path(
            r"^preferences/$", ccAPPSdb.common.views.preferences, name="preferences"
        ),
        re_path(r"^horizon/$", ccAPPSdb.common.views.horizon, name="horizon"),
        re_path(r"^settings/$", ccAPPSdb.common.views.saveSettings),
        re_path(
            r"^widget/(.+)/",
            ccAPPSdb.common.dashboard.Dashboard.dispatch,
            name="dashboard",
        ),
        # Model list reports, which override standard admin screens
        re_path(r"^data/login/$", ccAPPSdb.common.views.login),
        re_path(
            r"^data/auth/group/$",
            ccAPPSdb.common.views.GroupList.as_view(),
            name="auth_group_changelist",
        ),
        re_path(
            r"^data/common/user/$",
            ccAPPSdb.common.views.UserList.as_view(),
            name="common_user_changelist",
        ),
        re_path(
            r"^data/common/follower/$",
            ccAPPSdb.common.views.FollowerList.as_view(),
            name="common_follower_changelist",
        ),
        re_path(
            r"^data/common/bucket/$",
            ccAPPSdb.common.views.BucketList.as_view(),
            name="common_bucket_changelist",
        ),
        re_path(
            r"^data/common/bucketdetail/$",
            ccAPPSdb.common.views.BucketDetailList.as_view(),
            name="common_bucketdetail_changelist",
        ),
        re_path(
            r"^data/common/parameter/$",
            ccAPPSdb.common.views.ParameterList.as_view(),
            name="common_parameter_changelist",
        ),
        re_path(
            r"^data/common/attribute/$",
            ccAPPSdb.common.views.AttributeList.as_view(),
            name="common_attribute_changelist",
        ),
        re_path(
            r"^data/common/apikey/$",
            ccAPPSdb.common.views.APIKeyList.as_view(),
            name="common_apikey_changelist",
        ),
        re_path(
            r"^data/common/comment/$",
            ccAPPSdb.common.views.CommentList.as_view(),
            name="common_comment_changelist",
        ),
        # Special case of the next line for user password changes in the user edit screen
        re_path(
            r"detail/common/user/(?P<id>.+)/password/$",
            RedirectView.as_view(url="/data/common/user/%(id)s/password/"),
        ),
        # Detail URL for an object, which internally redirects to the view for the last opened tab
        re_path(r"^detail/([^/]+)/([^/]+)/(.+)/$", ccAPPSdb.common.views.detail),
        # REST API framework
        re_path(
            r"^api/common/bucket/$", ccAPPSdb.common.serializers.BucketAPI.as_view()
        ),
        re_path(
            r"^api/common/bucketdetail/$",
            ccAPPSdb.common.serializers.BucketDetailAPI.as_view(),
        ),
        re_path(
            r"^api/common/bucketdetail/$",
            ccAPPSdb.common.serializers.BucketDetailAPI.as_view(),
        ),
        re_path(
            r"^api/common/parameter/$",
            ccAPPSdb.common.serializers.ParameterAPI.as_view(),
        ),
        re_path(
            r"^api/common/attribute/$",
            ccAPPSdb.common.serializers.AttributeAPI.as_view(),
        ),
        re_path(
            r"^api/common/comment/$", ccAPPSdb.common.serializers.CommentAPI.as_view()
        ),
        re_path(
            r"^api/common/bucket/(?P<pk>(.+))/$",
            ccAPPSdb.common.serializers.BucketdetailAPI.as_view(),
        ),
        re_path(
            r"^api/common/bucketdetail/(?P<pk>(.+))/$",
            ccAPPSdb.common.serializers.BucketDetaildetailAPI.as_view(),
        ),
        re_path(
            r"^api/common/parameter/(?P<pk>(.+))/$",
            ccAPPSdb.common.serializers.ParameterdetailAPI.as_view(),
        ),
        re_path(
            r"^api/common/attribute/(?P<pk>(.+))/$",
            ccAPPSdb.common.serializers.AttributedetailAPI.as_view(),
        ),
        re_path(
            r"^api/common/comment/(?P<pk>(.+))/$",
            ccAPPSdb.common.serializers.CommentdetailAPI.as_view(),
        ),
        re_path(r"^api/$", APIIndexView),
        re_path(r"^apps/$", ccAPPSdb.common.views.AppsView.as_view(), name="apps"),
        re_path(r"^about/$", ccAPPSdb.common.views.AboutView, name="about"),
        re_path(r"^scenarios/$", ccAPPSdb.common.views.ScenarioView, name="scenarios"),
        # Supply chain path data API for dashboard widget
        re_path(r"^supplychain-path-data/$", ccAPPSdb.common.views.SupplyChainPathData),
        # Forgotten password
        re_path(
            r"^reset_password_confirm/(?P<uidb64>[0-9A-Za-z]+)-(?P<token>.+)/$",
            PasswordResetConfirmView.as_view(),
            name="reset_password_confirm",
        ),
        re_path(
            r"^reset_password/$",
            ResetPasswordRequestView.as_view(),
            name="reset_password",
        ),
    ]


# Monkeypatching to work around a DRF inefficiency
def _drf_set_rollback():
    for db in connections.all(initialized_only=True):
        if db.settings_dict["ATOMIC_REQUESTS"] and db.in_atomic_block:
            db.set_rollback(True)


from rest_framework import views

views.set_rollback = _drf_set_rollback
