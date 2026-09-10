from rest_framework_bulk.serializers import BulkListSerializer, BulkSerializerMixin

from ccAPPSdb.common.api.filters import FilterSet
from ccAPPSdb.common.api.serializers import (
    ModelSerializer,
    getAttributeAPIFields,
    getAttributeAPIReadOnlyFields,
)
from ccAPPSdb.common.api.views import (
    ccAPPSListCreateAPIView,
    ccAPPSRetrieveUpdateDestroyAPIView,
)

from . import models


class BucketFilter(FilterSet):
    class Meta:
        model = models.Bucket
        fields = {
            "name": ["exact", "in", "contains"],
            "description": ["exact", "contains"],
            "level": ["exact", "in", "gt", "gte", "lt", "lte"],
            "source": ["exact", "in"],
            "lastmodified": ["exact", "in", "gt", "gte", "lt", "lte"],
        }
        filter_fields = fields.keys()


class BucketSerializer(BulkSerializerMixin, ModelSerializer):
    class Meta:
        model = models.Bucket
        fields = (
            "name",
            "description",
            "level",
            "source",
            "lastmodified",
        ) + getAttributeAPIFields(models.Bucket)
        read_only_fields = ("lastmodified",) + getAttributeAPIReadOnlyFields(
            models.Bucket
        )
        list_serializer_class = BulkListSerializer
        update_lookup_field = "name"
        partial = True


class BucketAPI(ccAPPSListCreateAPIView):
    def get_queryset(self):
        return models.Bucket.objects.using(self.request.database).all()

    serializer_class = BucketSerializer
    filter_class = BucketFilter


class BucketDetailFilter(FilterSet):
    class Meta:
        model = models.BucketDetail
        fields = {
            "bucket": ["exact", "in"],
            "name": ["exact", "in", "contains"],
            "startdate": ["exact", "in", "gt", "gte", "lt", "lte"],
            "enddate": ["exact", "in", "gt", "gte", "lt", "lte"],
            "source": ["exact", "in"],
            "lastmodified": ["exact", "in", "gt", "gte", "lt", "lte"],
        }
        filter_fields = fields.keys()


class BucketdetailAPI(ccAPPSRetrieveUpdateDestroyAPIView):
    def get_queryset(self):
        return models.Bucket.objects.using(self.request.database).all()

    serializer_class = BucketSerializer
    filter_class = BucketFilter


class BucketDetailSerializer(BulkSerializerMixin, ModelSerializer):
    class Meta:
        model = models.BucketDetail
        fields = (
            "bucket",
            "name",
            "startdate",
            "enddate",
            "source",
            "lastmodified",
        ) + getAttributeAPIFields(models.BucketDetail)
        read_only_fields = ("lastmodified",) + getAttributeAPIReadOnlyFields(
            models.BucketDetail
        )


class BucketDetailAPI(ccAPPSListCreateAPIView):
    def get_queryset(self):
        return models.BucketDetail.objects.using(self.request.database).all()

    serializer_class = BucketDetailSerializer
    filter_class = BucketDetailFilter


class BucketDetaildetailAPI(ccAPPSRetrieveUpdateDestroyAPIView):
    def get_queryset(self):
        return models.BucketDetail.objects.using(self.request.database).all()

    serializer_class = BucketDetailSerializer
    filter_class = BucketDetailFilter


class AttributeFilter(FilterSet):
    class Meta:
        model = models.Attribute
        fields = {
            "model": ["exact", "in"],
            "name": ["exact", "in", "contains"],
            "editable": [
                "exact",
            ],
            "initially_hidden": [
                "exact",
            ],
            "source": ["exact", "in"],
            "lastmodified": ["exact", "in", "gt", "gte", "lt", "lte"],
        }
        filter_fields = fields.keys()


class AttributeSerializer(BulkSerializerMixin, ModelSerializer):
    class Meta:
        model = models.Attribute
        fields = (
            "model",
            "name",
            "label",
            "editable",
            "initially_hidden",
            "source",
            "lastmodified",
        )
        read_only_fields = ("lastmodified",)


class AttributeAPI(ccAPPSListCreateAPIView):
    def get_queryset(self):
        return models.Attribute.objects.all()

    serializer_class = AttributeSerializer
    filter_class = AttributeFilter


class AttributedetailAPI(ccAPPSRetrieveUpdateDestroyAPIView):
    def get_queryset(self):
        return models.Attribute.objects.all()

    serializer_class = AttributeSerializer
    filter_class = AttributeFilter


class CommentFilter(FilterSet):
    class Meta:
        model = models.Comment
        fields = {
            "id": ["exact", "in", "gt", "gte", "lt", "lte"],
            "object_pk": ["exact", "in", "contains"],
            "comment": ["exact", "contains"],
            "type": ["exact", "in", "contains"],
            "user": ["exact", "gt", "gte", "lt", "lte"],
            "content_type": ["exact", "gt", "gte", "lt", "lte"],
            "lastmodified": ["exact", "in", "gt", "gte", "lt", "lte"],
        }
        filter_fields = fields.keys()


class CommentSerializer(BulkSerializerMixin, ModelSerializer):
    class Meta:
        model = models.Comment
        fields = (
            "id",
            "object_pk",
            "comment",
            "lastmodified",
            "content_type",
            "user",
            "type",
        )
        read_only_fields = ("lastmodified",)
        list_serializer_class = BulkListSerializer
        update_lookup_field = "id"
        partial = True


class CommentAPI(ccAPPSListCreateAPIView):
    def get_queryset(self):
        return models.Comment.objects.using(self.request.database).all()

    serializer_class = CommentSerializer
    filter_class = CommentFilter


class CommentdetailAPI(ccAPPSRetrieveUpdateDestroyAPIView):
    def get_queryset(self):
        return models.Comment.objects.using(self.request.database).all()

    serializer_class = CommentSerializer
    filter_class = CommentFilter


class ParameterFilter(FilterSet):
    class Meta:
        model = models.Parameter
        fields = {
            "name": ["exact", "in", "contains"],
            "description": ["exact", "contains"],
            "value": ["exact", "in", "contains"],
            "source": ["exact", "in"],
            "lastmodified": ["exact", "in", "gt", "gte", "lt", "lte"],
        }
        filter_fields = fields.keys()


class ParameterSerializer(BulkSerializerMixin, ModelSerializer):
    class Meta:
        model = models.Parameter
        fields = ("name", "source", "lastmodified", "value", "description")
        read_only_fields = ("lastmodified",)
        list_serializer_class = BulkListSerializer
        update_lookup_field = "name"
        partial = True


class ParameterAPI(ccAPPSListCreateAPIView):
    def get_queryset(self):
        return models.Parameter.objects.using(self.request.database).all()

    serializer_class = ParameterSerializer
    filter_class = ParameterFilter


class ParameterdetailAPI(ccAPPSRetrieveUpdateDestroyAPIView):
    def get_queryset(self):
        return models.Parameter.objects.using(self.request.database).all()

    serializer_class = ParameterSerializer
    filter_class = ParameterFilter
