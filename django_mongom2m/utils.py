from django.db import models
from django.db.models import Q
from django.utils.translation import ugettext_lazy as _
from django_mongodb_engine.contrib import MongoDBManager
from django_mongodb_engine.query import A
try:
    # ObjectId has been moved to bson.objectid in newer versions of PyMongo
    from bson.objectid import ObjectId
except ImportError:
    from pymongo.objectid import ObjectId


def create_through(field, model, to):
    """
    Create a dummy 'through' model for MongoDBManyToMany relations. Django assumes there is a real
    database model providing the relationship, so we simulate it. This model has to have
    a ForeignKey relationship to both models. We will also override the save() and delete()
    methods to pass the adding and removing of related objects to the relation manager.
    """
    obj_name = model._meta.object_name + to._meta.object_name + 'Relationship'
    to_module_name = to._meta.module_name
    model_module_name = model._meta.module_name
    class ThroughQuerySet(object):
        def __init__(self, relationship_model, *args, **kwargs):
            self.to = to
            self.model = relationship_model
            self.model_instance = None
            self.related_manager = None
            self.to_instance = None
            self.db = 'default'
        def filter(self, *args, **kwargs):
            if model_module_name in kwargs:
                # Relation, set up for querying by the model
                self.model_instance = kwargs[model_module_name]
                self.related_manager = getattr(self.model_instance, field.name)
                # Now we know enough to retrieve the actual query set
                queryset = self.related_manager.all(appear_as_relationship=(self.model, self.model_instance, None, model_module_name, to_module_name)).using(self.db)
                return queryset
            if to_module_name in kwargs:
                # Reverse relation, set up for querying by the to model
                self.to_instance = kwargs[to_module_name]
                self.reverse_manager = getattr(self.to_instance, field.rel.related_name)
                queryset = self.reverse_manager._relationship_query_set(self.model, self.to_instance, model_module_name, to_module_name).using(self.db)
                return queryset
            return self
        def exists(self, *args, **kwargs):
            return False
        def ordered(self, *args, **kwargs):
            return self
        def using(self, db, *args, **kwargs):
            self.db = db
            return self
        def get(self, *args, **kwargs):
            # Check if it's a magic key
            if 'pk' in kwargs and isinstance(kwargs['pk'], basestring) and '$' in kwargs['pk']:
                model_id, direction, to_id = kwargs['pk'].split('$', 2)
                if direction == 'r':
                    # It's a reverse magic key
                    to_id, model_id = model_id, to_id
                if direction == 'r':
                    # Query in reverse
                    self.to_instance = self.to.objects.get(pk=to_id)
                    self.reverse_manager = getattr(self.to_instance, field.rel.related_name)
                    queryset = self.reverse_manager._relationship_query_set(self.model, self.to_instance, model_module_name, to_module_name).using(self.db)
                    obj = queryset.get(pk=model_id)
                    return obj
                else:
                    self.model_instance = model.objects.get(pk=model_id)
                    self.related_manager = getattr(self.model_instance, field.name)
                    queryset = self.related_manager.all(appear_as_relationship=(self.model, self.model_instance, None, model_module_name, to_module_name)).using(self.db)
                    return queryset.get(pk=to_id)
            # Normal key
            return None
        def __len__(self):
            # Won't work, must be accessed through filter()
            raise Exception('ThroughQuerySet relation unknown (__len__)')
        def __getitem__(self, key):
            # Won't work, must be accessed through filter()
            raise Exception('ThroughQuerySet relation unknown (__getitem__)')
    class ThroughManager(MongoDBManager):
        def get_query_set(self):
            return ThroughQuerySet(self.model)
    class Through(models.Model):
        class Meta:
            auto_created = model
        objects = ThroughManager()
        locals()[to_module_name] = models.ForeignKey(to, null=True, blank=True)
        locals()[model_module_name] = models.ForeignKey(model, null=True, blank=True)
        def __unicode__(self):
            return unicode(getattr(self, model_module_name)) + u' : ' + unicode(getattr(self, to_module_name))
        def save(self, *args, **kwargs):
            # Don't actually save the model, convert to an add() call instead
            obj = getattr(self, model_module_name)
            manager = getattr(obj, field.name)
            manager.add(getattr(self, to_module_name))
            obj.save() # must save parent model because Django admin won't
        def delete(self, *args, **kwargs):
            # Don't actually delete the model, convert to a delete() call instead
            obj = getattr(self, model_module_name)
            manager = getattr(obj, field.name)
            manager.remove(getattr(self, to_module_name))
            obj.save() # must save parent model because Django admin won't
    # Remove old model from Django's model registry, because it would be a duplicate
    from django.db.models.loading import cache
    model_dict = cache.app_models.get(Through._meta.app_label)
    del model_dict[Through._meta.module_name]
    # Rename the model
    Through.__name__ = obj_name
    Through._meta.app_label = model._meta.app_label
    Through._meta.object_name = obj_name
    Through._meta.module_name = obj_name.lower()
    Through._meta.db_table = Through._meta.app_label + '_' + Through._meta.module_name
    Through._meta.verbose_name = _('%(model)s %(to)s relationship') % {'model':model._meta.verbose_name, 'to':to._meta.verbose_name}
    Through._meta.verbose_name_plural = _('%(model)s %(to)s relationships') % {'model':model._meta.verbose_name, 'to':to._meta.verbose_name}
    # Add new model to Django's model registry
    cache.register_models(Through._meta.app_label, Through)
    return Through


def replace_Q(q, column, allowed_fields=None):
    """Replace the fields in the Q object with A() objects from 'column'

    :param q: The Q object to work on
    :param column: The name of the column the A() objects should be attached to.
    :param allowed_fields: If defined, only fields names listed in
            'allowed_fields' are allowed.
            E.g. allowed_fields=["pk"]: Q(pk=1) is good, Q(name="tom") fails.
    :returns: Boolean; False if 'allowed_fields' missed. True otherwise

    Example:
     M2M field is called 'users'
    _replace_Q(Q(name="Tom"), "users") would modify the given Q to be:
        Q(users=A("name", "Tom"))

    That would generate the query: {"users.name":"Tom"}
    """
    if not isinstance(q, Q):
        raise ValueError("'q' must be of type Q, not: '%s'" % type(q))

    # Iterate over the Q object's children. The children are either another Q,
    # or a tuple of (<field>,<value>)
    for child in q.children:
        if isinstance(child, Q):
            # If we have a Q in the children, let's recurse to fix it too
            replace_Q(child, column, allowed_fields)
        elif isinstance(child, tuple):
            # Otherwise we need to build an A(). Doing the index, remove,
            # and insert to maintain the order of the children. I'm not sure
            # changing the order matters, but I don't want to risk it.
            index = q.children.index(child)
            q.children.remove(child)

            # If allowed_fields is defined, this verifies that only those
            # fields are present. E.g. ['pk']
            if allowed_fields and child[0] not in allowed_fields:
                return False
            # If all is well, build an A(), and insert back into the children
            q.children.insert(index, (column, combine_A(child[0], child[1])))
        else:
            raise TypeError("Unknown type in Q.children")
    return True


def combine_A(field, value):
    # The pk is actually stored as "id", so change it, we also need extract the
    # pk from and models and wrap any IDs in an ObjectId,
    if field in ('pk', 'id'):
        field = "id"
        if isinstance(value, models.Model):
            # Specifically getattr field because we don't know if it's 'pk'
            # or 'id' and they might not be the same thing.
            value = getattr(value, field)

        # If value is None, we want to leave it as None, otherwise wrap it
        if value is not None and not isinstance(value, ObjectId):
            value = ObjectId(value)

    # If 'value' is already an A(), we need to extract the field part out
    if isinstance(value, A):
        field = "%s.%s" % (field, value.op)
        value = value.val
    return A(field, value)
