from django.urls import re_path

from ccAPPSdb import mode

# Automatically add these URLs when the application is installed
autodiscover = True

if mode == "WSGI":
    import ccAPPSdb.output.views.buffer
    import ccAPPSdb.output.views.demand
    import ccAPPSdb.output.views.problem
    import ccAPPSdb.output.views.constraint
    import ccAPPSdb.output.views.resource
    import ccAPPSdb.output.views.operation
    import ccAPPSdb.output.views.pegging

    urlpatterns = [
        re_path(
            r"^buffer/item/(.+)/$",
            ccAPPSdb.output.views.buffer.OverviewReport.as_view(),
            name="output_buffer_plandetail_by_item",
        ),
        re_path(
            r"^buffer/(.+)/$",
            ccAPPSdb.output.views.buffer.OverviewReport.as_view(),
            name="output_buffer_plandetail",
        ),
        re_path(
            r"^buffer/$",
            ccAPPSdb.output.views.buffer.OverviewReport.as_view(),
            name="output_buffer_plan",
        ),
        re_path(
            r"^demand/operationplans/$",
            ccAPPSdb.output.views.demand.OperationPlans,
            name="output_demand_operationplans",
        ),
        re_path(
            r"^demand/$",
            ccAPPSdb.output.views.demand.OverviewReport.as_view(),
            name="output_demand_plan",
        ),
        re_path(
            r"^resource/(.+)/$",
            ccAPPSdb.output.views.resource.OverviewReport.as_view(),
            name="output_resource_plandetail",
        ),
        re_path(
            r"^resource/$",
            ccAPPSdb.output.views.resource.OverviewReport.as_view(),
            name="output_resource_plan",
        ),
        re_path(
            r"^operation/(.+)/$",
            ccAPPSdb.output.views.operation.OverviewReport.as_view(),
            name="output_operation_plandetail",
        ),
        re_path(
            r"^operation/$",
            ccAPPSdb.output.views.operation.OverviewReport.as_view(),
            name="output_operation_plan",
        ),
        re_path(
            r"^purchase/$",
            ccAPPSdb.output.views.operation.PurchaseReport.as_view(),
            name="output_purchase",
        ),
        re_path(
            r"^distribution/$",
            ccAPPSdb.output.views.operation.DistributionReport.as_view(),
            name="output_distribution",
        ),
        re_path(
            r"^demandpegging/(.+)/$",
            ccAPPSdb.output.views.pegging.ReportByDemand.as_view(),
            name="output_demand_pegging",
        ),
        re_path(
            r"^problem/$",
            ccAPPSdb.output.views.problem.Report.as_view(),
            name="output_problem",
        ),
        re_path(
            r"^constraint/$",
            ccAPPSdb.output.views.constraint.BaseReport.as_view(),
            name="output_constraint",
        ),
        re_path(
            r"^constraintoperation/(.+)/$",
            ccAPPSdb.output.views.constraint.ReportByOperation.as_view(),
            name="output_constraint_operation",
        ),
        re_path(
            r"^constraintdemand/(.+)/$",
            ccAPPSdb.output.views.constraint.ReportByDemand.as_view(),
            name="output_constraint_demand",
        ),
        re_path(
            r"^constraintbuffer/(.+)/$",
            ccAPPSdb.output.views.constraint.ReportByBuffer.as_view(),
            name="output_constraint_buffer",
        ),
        re_path(
            r"^constraintresource/(.+)/$",
            ccAPPSdb.output.views.constraint.ReportByResource.as_view(),
            name="output_constraint_resource",
        ),
    ]
