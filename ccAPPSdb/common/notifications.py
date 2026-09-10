from .models import NotificationFactory, User, Bucket, BucketDetail, Parameter


@NotificationFactory.register(User, [User])
def UserNotification(flw, msg):
    return flw.content_type == msg.content_type and flw.object_pk == msg.object_pk


@NotificationFactory.register(Bucket, [Bucket, BucketDetail])
def BucketNotification(flw, msg):
    if flw.content_type == msg.content_type:
        return flw.object_pk == msg.object_pk
    elif msg.content_type.model_class() == BucketDetail and msg.content_object:
        return flw.object_pk == msg.content_object.bucket.name
    else: 
        return False


@NotificationFactory.register(BucketDetail, [BucketDetail])
def BucketDetailNotification(flw, msg):
    return flw.content_type == msg.content_type and flw.object_pk == msg.object_pk


@NotificationFactory.register(Parameter, [Parameter])
def ParameterNotification(flw, msg):
    return flw.content_type == msg.content_type and flw.object_pk == msg.object_pk
