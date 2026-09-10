from datetime import datetime
from dateutil.parser import parse


from rest_framework import serializers
from rest_framework_bulk.drf3.serializers import BulkListSerializer, BulkSerializerMixin

from ccAPPSdb.common.api.filters import FilterSet
from ccAPPSdb.common.api.serializers import (
    ModelSerializer,
    getAttributeAPIFilterDefinition,
    getAttributeAPIFields,
    getAttributeAPIReadOnlyFields,
)
from ccAPPSdb.common.api.views import (
    ccAPPSListCreateAPIView,
    ccAPPSRetrieveUpdateDestroyAPIView,
)
from ccAPPSdb.common.models import Parameter
from ccAPPSdb.forecast.models import Forecast, Measure, ForecastPlan


class ForecastFilter(FilterSet):
    class Meta:
        model = Forecast
        fields = dict(
            {
                "name": ["exact", "in", "contains"],
                "description": ["exact", "in", "contains"],
                "category": ["exact", "in", "contains"],
                "subcategory": ["exact", "in", "contains"],
                "batch": ["exact", "in", "contains"],
                "item": ["exact", "in"],
                "customer": ["exact", "in"],
                "location": ["exact", "in"],
                "operation": ["exact", "in"],
                "priority": ["exact", "in", "gt", "gte", "lt", "lte"],
                "minshipment": ["exact", "in", "gt", "gte", "lt", "lte"],
                "maxlateness": ["exact", "in", "gt", "gte", "lt", "lte"],
                "discrete": ["exact"],
                "out_smape": ["exact", "in", "gt", "gte", "lt", "lte"],
                "out_method": ["exact", "in"],
                "out_deviation": ["exact", "in", "gt", "gte", "lt", "lte"],
                "source": ["exact", "in"],
                "lastmodified": ["exact", "in", "gt", "gte", "lt", "lte"],
            },
            **getAttributeAPIFilterDefinition(Forecast),
        )
        filter_fields = fields.keys()


class ForecastSerializer(BulkSerializerMixin, ModelSerializer):
    class Meta:
        model = Forecast
        fields = (
            "name",
            "description",
            "category",
            "subcategory",
            "batch",
            "customer",
            "item",
            "location",
            "operation",
            "method",
            "priority",
            "minshipment",
            "maxlateness",
            "discrete",
            "out_smape",
            "out_method",
            "out_deviation",
            "source",
            "lastmodified",
        ) + getAttributeAPIFields(Forecast)
        read_only_fields = ("lastmodified",) + getAttributeAPIReadOnlyFields(Forecast)
        list_serializer_class = BulkListSerializer
        update_lookup_field = "name"
        partial = True


class ForecastAPI(ccAPPSListCreateAPIView):
    def get_queryset(self):
        return Forecast.objects.using(self.request.database).all()

    serializer_class = ForecastSerializer
    filter_class = ForecastFilter


class ForecastdetailAPI(ccAPPSRetrieveUpdateDestroyAPIView):
    def get_queryset(self):
        return Forecast.objects.using(self.request.database).all()

    serializer_class = ForecastSerializer


class ForecastPlanSerializer(BulkSerializerMixin, ModelSerializer):
    class Meta:
        model = ForecastPlan
        fields = ("item", "location", "customer", "startdate", "enddate", "value")
        read_only_fields = None
        list_serializer_class = BulkListSerializer
        update_lookup_field = "id"
        partial = True


class ForecastPlanFilter(FilterSet):
    pass


class ForecastPlanAPI(ccAPPSListCreateAPIView):
    def get_queryset(self):
        try:
            parameter_currentdate = parse(
                Parameter.objects.get(name="currentdate").value
            )
        except Exception:
            parameter_currentdate = datetime.now()
        return ForecastPlan.objects.using(self.request.database).raw(
            """
            select forecastplan.item_id||' @ '||
            forecastplan.location_id||' @ '||
            forecastplan.customer_id||' @ '||
            forecastplan.startdate as id,
            forecastplan.item_id,
            forecastplan.location_id,
            forecastplan.customer_id,
            forecastplan.startdate,
            forecastplan.enddate,
            jsonb_strip_nulls(
                to_jsonb(forecastplan)
                - array['customer_id', 'item_id', 'location_id', 'startdate', 'enddate']
                ) as value
            FROM forecastplan
            inner join forecast on forecast.item_id = forecastplan.item_id
            and forecast.location_id = forecastplan.location_id
            and forecast.customer_id = forecastplan.customer_id
            and forecast.planned = true
            where
            forecastplan.enddate > to_date('%s','YYYY-MM-DD HH24:MI:SS')
            """
            % (parameter_currentdate.strftime("%Y-%m-%d %H:%M:%S"),)
        )

    serializer_class = ForecastPlanSerializer
    filter_class = None


class MeasureFilter(FilterSet):
    class Meta:
        model = Measure
        fields = {
            "name": ["exact", "in", "contains"],
            "label": ["exact", "in", "contains"],
            "description": ["exact", "in", "contains"],
            "type": ["exact", "in", "contains"],
            "discrete": ["exact"],
            "defaultvalue": ["exact", "in", "gt", "gte", "lt", "lte"],
            "mode_future": ["exact", "in", "contains"],
            "mode_past": ["exact", "in", "gt", "gte", "lt", "lte"],
            "compute_expression": ["exact", "in", "contains"],
            "update_expression": ["exact", "in", "contains"],
            "source": ["exact", "in"],
            "lastmodified": ["exact", "in", "gt", "gte", "lt", "lte"],
        }
        filter_fields = fields.keys()


class MeasureSerializer(BulkSerializerMixin, ModelSerializer):
    def validate_name(self, value):
        if value and not value.isalnum():
            raise serializers.ValidationError("Measure name can only be alphanumeric")

    class Meta:
        model = Measure
        fields = (
            "name",
            "label",
            "description",
            "type",
            "discrete",
            "defaultvalue",
            "mode_future",
            "mode_past",
            "compute_expression",
            "update_expression",
            "overrides",
            "source",
            "lastmodified",
        )
        list_serializer_class = BulkListSerializer
        update_lookup_field = "name"
        partial = True


class MeasureAPI(ccAPPSListCreateAPIView):
    def get_queryset(self):
        return Measure.objects.using(self.request.database).all()

    serializer_class = MeasureSerializer
    filter_class = MeasureFilter


class MeasuredetailAPI(ccAPPSRetrieveUpdateDestroyAPIView):
    def get_queryset(self):
        return Measure.objects.using(self.request.database).all()
