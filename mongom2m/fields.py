from django.db import models, router
from django.db.models import Q
from django.db.models.signals import m2m_changed
from django.db.models.fields.related import add_lazy_relation
from django.utils.translation import ugettext_lazy as _

from django_mongodb_engine.contrib import MongoDBManager
from django_mongodb_engine.query import A
import django_mongodb_engine.query
try:
    # ObjectId has been moved to bson.objectid in newer versions of PyMongo
    from bson.objectid import ObjectId
except ImportError:
    from pymongo.objectid import ObjectId
from djangotoolbox.fields import ListField, EmbeddedModelField

# How much to show when query set is viewed in the Python shell
REPR_OUTPUT_SIZE = 20


class MongoDBM2MQueryError(Exception): pass


class MongoDBM2MQuerySet(object):
    """
    Helper for returning a set of objects from the managers.
    Works similarly to Django's own query set objects.
    Lazily loads non-embedded objects when iterated.
    If embed=False, objects are always loaded from database.
    """
    def __init__(self, rel, model, objects, use_cached,
                 appear_as_relationship=(None, None, None, None, None)):
        self.db = router.db_for_read(rel.model if rel.model else rel.field.model)
        self.rel = rel
        self.objects = list(objects) # make a copy of the list to avoid problems
        self.model = model
        (self.appear_as_relationship_model, self.rel_model_instance,
                self.rel_to_instance, self.rel_model_name, self.rel_to_name) = \
                    appear_as_relationship # appear as an intermediate m2m model
        if self.appear_as_relationship_model:
            self.model = self.appear_as_relationship_model
        if not use_cached:
            # Reset any cached instances
            self.objects = [{'pk': obj['pk'], 'obj': None}
                            for obj in self.objects]
    
    def _get_obj(self, obj):
        if not obj.get('obj'):
            # Load referred instance from db and keep in memory
            obj['obj'] = self.rel.to.objects.get(pk=obj['pk'])
        if self.appear_as_relationship_model:
            # Wrap us in a relationship class
            if self.rel_model_instance:
                args = {'pk': "%s$f$%s" %
                              (self.rel_model_instance.pk, obj['pk']),
                        self.rel_model_name: self.rel_model_instance,
                        self.rel_to_name: obj['obj']}
            else:
                # Reverse
                args = {'pk': "%s$r$%s" % (self.rel_to_instance.pk, obj['pk']),
                        self.rel_model_name: obj['obj'],
                        self.rel_to_name: self.rel_to_instance }
            wrapper = self.appear_as_relationship_model(**args)
            return wrapper
        return obj['obj']
    
    def __iter__(self):
        for obj in self.objects:
            yield self._get_obj(obj)
    
    def __repr__(self):
        # limit list after conversion because mongodb doesn't use integer indices
        data = list(self)[:REPR_OUTPUT_SIZE + 1]
        if len(data) > REPR_OUTPUT_SIZE:
           data[-1] = "...(remaining elements truncated)..."
        return repr(data)
    
    def __getitem__(self, key):
        obj = self.objects[key]
        return self._get_obj(obj)
    
    def ordered(self, *args, **kwargs):
        return self
    
    def __len__(self):
        return len(self.objects)
    
    def using(self, db, *args, **kwargs):
        self.db = db
        return self
    
    def filter(self, *args, **kwargs):
        return self
    
    def get(self, *args, **kwargs):
        if 'pk' in kwargs:
            pk = ObjectId(kwargs['pk'])
            for obj in self.objects:
                if pk == obj['pk']:
                    return self._get_obj(obj)
        return None
    
    def count(self):
        return len(self.objects)

class MongoDBM2MReverseManager(object):
    """
    This manager is attached to the other side of M2M relationships
    and will return query sets that fetch related objects.
    """
    def __init__(self, rel_field, model, field, rel, embed):
        self.rel_field = rel_field
        self.model = model
        self.field = field
        self.rel = rel
        self.embed = embed
    
    def all(self):
        """
        Retrieve all related objects.
        """
        name = self.field.column + '.' + self.rel.model._meta.pk.column
        pk = ObjectId(self.rel_field.pk)
        return self.model._default_manager.raw_query({name:pk})
    
    def _relationship_query_set(self, model, to_instance, model_module_name,
                                to_module_name):
        """
        Emulate an intermediate 'through' relationship query set.
        """
        objects = [{'pk':ObjectId(obj.pk), 'obj':obj} for obj in self.all()]
        return MongoDBM2MQuerySet(
                self.rel, self.rel.to, objects, use_cached=True,
                appear_as_relationship=(model, None, to_instance,
                                        model_module_name, to_module_name))

class MongoDBM2MReverseDescriptor(object):
    def __init__(self, model, field, rel, embed):
        self.model = model
        self.field = field
        self.rel = rel
        self.embed = embed
    
    def __get__(self, instance, owner=None):
        if instance is None:
            return self
        return MongoDBM2MReverseManager(instance, self.model, self.field,
                                        self.rel, self.embed)

class MongoDBM2MRelatedManager(object):
    """
    This manager manages the related objects stored in a MongoDBManyToManyField.
    They can be embedded or stored as relations (ObjectIds) only.
    Internally, we store the objects as dicts that contain keys pk and obj.
    The obj key is None when the object has not yet been loaded from the db.
    """
    def __init__(self, field, rel, embed, objects=[], model_instance=None):
        self.model_instance = model_instance
        self.field = field
        self.rel = rel
        self.embed = embed
        self.objects = list(objects) # make copy of the list to avoid problems
    
    def _with_model_instance(self, model_instance):
        """
        Create a new copy of this manager for a specific model instance. This
        is called when the field is being accessed through a model instance.
        """
        return MongoDBM2MRelatedManager(
                        self.field, self.rel, self.embed,
                        self.objects, model_instance=model_instance)
    
    def __call__(self):
        """
        This is used when creating a default value for the field
        """
        return MongoDBM2MRelatedManager(self.field, self.rel, self.embed, self.objects)
    
    def count(self):
        return len(self.objects)
    
    def add(self, *objs, **kwargs):
        """
        Add model instance(s) to the M2M field. The objects can be real
        Model instances or just ObjectIds (or strings representing ObjectIds).

        Only supported kwarg is 'auto_save'
        :param auto_save: Defaults to True. When a model is added to the M2M,
                the behavior of Django is to create an entry in the
                through-table, which essentially saves the list. In order to do
                the equivalent, we need to save the model. However, that
                behavior is not the same as Django either because Django doesn't
                save the whole model object, so that's why this is optional.
                Swings and Roundabouts.
        """
        auto_save = kwargs.pop('auto_save', True)
        using = router.db_for_write(self.model_instance if self.model_instance
                                                        else self.field.model)
        add_objs = []
        for obj in objs:
            if isinstance(obj, (ObjectId, basestring)):
                # It's an ObjectId
                pk = ObjectId(obj)
                instance = None
            else:
                # It's a model object
                pk = ObjectId(obj.pk)
                instance = obj
            if not pk in (obj['pk'] for obj in self.objects):
                add_objs.append({'pk':pk, 'obj':instance})
        
        # Calculate list of object ids that are being added
        add_obj_ids = [str(obj['pk']) for obj in add_objs]
        
        # Send pre_add signal (instance should be Through instance but it's the
        #  manager instance for now)
        m2m_changed.send(self.rel.through, instance=self.model_instance,
                         action='pre_add', reverse=False, model=self.rel.to,
                         pk_set=add_obj_ids, using=using)
        
        # Commit the add
        for obj in add_objs:
            self.objects.append({'pk':obj['pk'], 'obj':obj['obj']})

        # Send post_add signal (instance should be Through instance but it's
        # the manager instance for now)
        m2m_changed.send(self.rel.through, instance=self.model_instance,
                         action='post_add', reverse=False, model=self.rel.to,
                         pk_set=add_obj_ids, using=using)

        if auto_save:
            self.model_instance.save()

    def create(self, **kwargs):
        """
        Create new model instance and add to the M2M field.
        """
        # See add() above for description of auto_save
        auto_save = kwargs.pop('auto_save', True)

        obj = self.rel.to(**kwargs)
        self.add(obj, auto_save=auto_save)
        return obj
    
    def remove(self, *objs, **kwargs):
        """
        Remove the specified object from the M2M field.
        The object can be a real model instance or an ObjectId or
        a string representing an ObjectId. The related object is
        not deleted, it's only removed from the list.

        Only supported kwarg is 'auto_save'
        :param auto_save: See add() above for description
        """
        auto_save = kwargs.pop('auto_save', True)

        obj_ids = set([ObjectId(obj) if isinstance(obj, (ObjectId, basestring))
                                     else ObjectId(obj.pk) for obj in objs])
        
        # Calculate list of object ids that will be removed
        removed_obj_ids = [str(obj['pk']) for obj in self.objects if obj['pk'] in obj_ids]
        
        # Send the pre_remove signal
        m2m_changed.send(self.rel.through, instance=self.model_instance,
                         action='pre_remove', reverse=False, model=self.rel.to,
                         pk_set=removed_obj_ids)
        
        # Commit the remove
        self.objects = [obj for obj in self.objects if obj['pk'] not in obj_ids]
        
        # Send the post_remove signal
        m2m_changed.send(self.rel.through, instance=self.model_instance,
                         action='post_remove', reverse=False, model=self.rel.to,
                         pk_set=removed_obj_ids)

        if auto_save:
            self.model_instance.save()

    def clear(self, auto_save=True):
        """
        Clear all objects in the list. The related objects are not
        deleted from the database.

        :param auto_save: See add() above for description
        """
        # Calculate list of object ids that will be removed
        removed_obj_ids = [str(obj['pk']) for obj in self.objects]
        
        # Send the pre_clear signal
        m2m_changed.send(self.rel.through, instance=self.model_instance,
                         action='pre_clear', reverse=False, model=self.rel.to,
                         pk_set=removed_obj_ids)
        
        # Commit the clear
        self.objects = []
        
        # Send the post_clear signal
        m2m_changed.send(self.rel.through, instance=self.model_instance,
                         action='post_clear', reverse=False, model=self.rel.to,
                         pk_set=removed_obj_ids)

        if auto_save:
            self.model_instance.save()
        
    def __contains__(self, obj):
        """
        Helper to enable 'object in container' by comparing IDs.
        """
        if hasattr(obj, 'pk'): obj = obj.pk
        elif hasattr(obj, 'id'): obj = obj.id
        return ObjectId(obj) in [ObjectId(o['pk']) for o in self.objects]
    
    def __iter__(self):
        """
        Iterator is used by Django admin's ModelMultipleChoiceField.
        """
        for obj in self.objects:
            if not obj['obj']:
                # Load referred instance from db and keep in memory
                obj['obj'] = self.rel.to.objects.get(pk=obj['pk'])
            yield obj['obj']
    
    def all(self, **kwargs):
        """
        Return all the related objects as a query set. If embedding
        is enabled, returns embedded objects. Otherwise the query set
        will retrieve the objects from the database as needed.
        """
        return MongoDBM2MQuerySet(self.rel, self.rel.to, self.objects,
                                  use_cached=True, **kwargs)
    
    def ids(self):
        """
        Return a list of ObjectIds of all the related objects.
        """
        return [obj['pk'] for obj in self.objects]
    
    def objs(self):
        """
        Return the actual related model objects, loaded fresh from
        the database. This won't use embedded objects even if they
        exist.
        """
        return MongoDBM2MQuerySet(self.rel, self.rel.to, self.objects,
                                  use_cached=False)
    
    def to_python_embedded_instance(self, embedded_instance):
        """
        Convert a single embedded instance value stored in the database to an object
        we can store in the internal objects list.
        """
        if isinstance(embedded_instance, ObjectId):
            # It's an object id, probably from a ListField(ForeignKey) migration
            return {'pk': embedded_instance, 'obj': None}
        elif isinstance(embedded_instance, basestring):
            # Assume it's a string formatted object id, probably from a
            # ListField(ForeignKey) migration
            return {'pk': ObjectId(embedded_instance), 'obj': None}

        elif isinstance(embedded_instance, tuple):
            # This is the typical path for embedded instances (embed=True)
            # The tuples is format: (<embedded model class>, <kwarg dict>)
            cls, values = embedded_instance
            if len(values) == 1 and 'id' in values and \
                        len(cls._meta.fields) > 1:
                # In this case, the user most likely just switched from
                # embed=False to embed=True. We need to treat is as such
                return self.to_python_embedded_instance(
                                {"id": ObjectId(values['id'])})
            else:
                # Otherwise it's been embedded previously
                instance = cls(**values)
                return {'pk': ObjectId(instance.pk), 'obj': instance}

        elif self.embed:
            # Try to load the embedded object contents if possible
            if isinstance(embedded_instance, dict):
                # Convert the embedded value from dict to model
                data = {}
                for field in self.rel.to._meta.fields:
                    try:
                        data[str(field.attname)] = \
                                                embedded_instance[field.column]
                    except KeyError:
                        pass

                # If we only got the id, give up to avoid creating an
                # invalid/empty model instance
                if len(data) <= 1:
                    column = self.rel.to._meta.pk.column
                    return {'pk': ObjectId(embedded_instance[column]),
                            'obj': None}
                else:
                    # Otherwise create the model instance from the fields
                    obj = self.rel.to(**data)
                    # Make sure the pk in the model instance is a string
                    # (not ObjectId) to be compatible with django-mongodb-engine
                    if isinstance(obj.pk, ObjectId):
                        obj.pk = str(obj.pk)
                    return {'pk': ObjectId(obj.pk), 'obj': obj}
            else:
                # Assume it's already a model
                obj = embedded_instance
                # Make sure the pk is a string (not ObjectId) to be compatible
                # with django-mongodb-engine
                if isinstance(obj.pk, ObjectId):
                    obj.pk = str(obj.pk)
                return {'pk': ObjectId(obj.pk), 'obj': obj}
        else:
            # No embedded value, only ObjectId
            if isinstance(embedded_instance, dict):
                # Get the id value from the dict
                column = self.rel.to._meta.pk.column
                return {'pk': ObjectId(embedded_instance[column]), 'obj': None}
            else:
                # Assume it's already a model
                return {'pk': ObjectId(embedded_instance.pk), 'obj': None}
    
    def to_python(self, values):
        """
        Convert a database value to Django model instances managed by this manager.
        """
        if isinstance(values, models.Model):
            # Single value given as parameter
            values = [values]
        self.objects = [self.to_python_embedded_instance(value) for value in values]
    
    def get_db_prep_value_embedded_instance(self, obj, connection):
        """
        Convert an internal object value to database representation.
        """
        if not obj: return None
        pk = obj['pk']
        if not self.embed:
            # If we're not embedding, store only the ID
            return {self.rel.to._meta.pk.column: pk}
        if not obj['obj']:
            # Retrieve the object from db for storing as embedded data
            obj['obj'] = self.rel.to.objects.get(pk=pk)
        embedded_instance = obj['obj']
        values = {}

        for field in embedded_instance._meta.fields:
            value = field.pre_save(embedded_instance, add=True)
            value = field.get_db_prep_save(value, connection=connection)
            values[field] = value
        # Convert primary key into an ObjectId so it's stored correctly
        # values[self.rel.to._meta.pk] = ObjectId(values[self.rel.to._meta.pk])
        return values
    
    def get_db_prep_value(self, connection, prepared=False):
        """Convert the Django model instances managed by this manager into a
        special list that can be stored in MongoDB.
        """
        values = [self.get_db_prep_value_embedded_instance(obj, connection)
                  for obj in self.objects]
        return values


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

class MongoDBManyToManyRelationDescriptor(object):
    """
    This descriptor returns the 'through' model used in Django admin to access the
    ManyToManyField objects for inlines. It's implemented by the MongoDBManyToManyThrough
    class, which simulates a data model. This class also handles the attribute assignment
    from the MongoDB raw fields, which must be properly converted to Python objects.
    
    In other words, when you have a many-to-many field called categories on model Article,
    this descriptor is the value of Article.categories. When you access the value
    Article.categories.through, you get the through attribute of this object.
    """
    def __init__(self, field, through):
        self.field = field
        self.through = through
    
    def __get__(self, obj, type=None):
        """
        A field is being accessed on a model instance. Add the model instance to the
        related manager so we can use it for signals etc.
        """
        if obj:
            manager = obj.__dict__[self.field.name]
            if not manager.model_instance:
                manager = manager._with_model_instance(obj)
                # Store it in the model for future reference
                obj.__dict__[self.field.name] = manager
            return manager
        else:
            return type.__dict__[self.field.name]
    
    def __set__(self, obj, value):
        """
        Attributes are being assigned to model instance. We redirect the
        assignments to the model instance's fields instances.
        """
        obj.__dict__[self.field.name] = self.field.to_python(value)

    def _filter_or_exclude(self, negate, *args, **kwargs):
        """Enables queries on the host-model-level for contents of this field.
        That means calling this filter will return instances of the
        MongoDBManyToManyField host model, not instances of the related model.

        If embed=True, anything can be queried. If embed=False, then only
        model objects (or ids) can be compared. In this case, the only accepted
        argument is 'pk'. The reason for this is because related models are
        stored by pk.

        Warning: Only the first call to this method will actually behave
                 correctly. If you string multiple calls to filter together, the
                 remaining filters after the first will all act on the fields
                 of the host model.

        Example:
        >>>class M2MModel(models.Model):
        >>>    name = models.CharField()
        >>>
        >>>class Host(models.Model):
        >>>    m2m = MongoDBManyToManyField(M2MModel, embed=True)
        >>>
        >>> m = M2MModel.objects.get(name="foo")
        >>> Host.m2m.filter(pk=m)
        [<Host: Host object>]
        >>> Host.m2m.filter(name="foo")
        [<Host: Host object>]

        Important Distinction:
        The above example is acting on the Host model *class*, not instance.
        Calling filter on an *instance* of Host model would return instances of
        the M2M related model. Example:

        >>> h = Host.objects.get(id=1)
        >>> h.m2m.filter(name="foo")
        [<M2MModel: M2MModel instance>]

        Author's Note:
        I very much dislike this solution, but a better solution eludes me
        right now. In order to get the behavior Django has, django-nonrel or
        djangotoolbox need to be changed to support manytomany fields.
        """
        def raise_query_error():
            raise MongoDBM2MQueryError(
                "Invalid query paramaters: '%s; %s'. M2M Fields not using the "
                "'embed=True' option can only filter on 'pk' because only "
                "the related model's pk is stored for non-embedded M2Ms. "
                "Note: M2M fields that are converted to 'embed=True' do "
                "not convert the stored values automatically. Every "
                "instance of the host-model must be re-saved after "
                "converting the field." % (args, kwargs))

        embedded = self.field._mm2m_embed
        column = self.field.column

        updated_args = []
        # Iterate over the arguments and replace them with A objects
        for field in args:
            if isinstance(field, Q):
                # Some args may be Qs. This function replaces the Q children
                # with A() objects.
                status = _replace_Q(field, column,
                                    ["pk"] if not embedded else None)
                if status:
                    updated_args.append(field)
                else:
                    raise_query_error()
            else:
                # Anything else should be tuples of two items
                updated_args.append(
                    (self.field.column, _combine_A(field[0], field[1])))

        updated_kwargs = []
        # Iterate over the kwargs and combine them into A objects
        for field, value in kwargs.iteritems():
            if not embedded and field != 'pk':
                raise_query_error()

            # Have to build Q objects because all the arguments will have the
            # same key in the kwargs otherwise
            updated_kwargs.append(Q(**{column: _combine_A(field, value)}))

        query_args = updated_args + updated_kwargs
        if negate:
            return self.field.model.objects.exclude(*query_args)
        else:
            return self.field.model.objects.filter(*query_args)

    def filter(self, *args, **kwargs):
        """See _filter_or_exclude() above for description"""
        return self._filter_or_exclude(False, *args, **kwargs)

    def exclude(self, *args, **kwargs):
        """See _filter_or_exclude() above for description"""
        return self._filter_or_exclude(True, *args, **kwargs)

    def get(self, *args, **kwargs):
        """Return a single object matching the query.
        See _filter_or_exclude() above for more details.
        """
        results = self.filter(*args, **kwargs)
        num = len(results)
        if num == 1:
            return results[0]
        elif num < 1:
            raise self.field.model.DoesNotExist(
                            "%s matching query does not exist."
                            % self.field.model._meta.object_name)
        else:
            raise self.field.model.MultipleObjectsReturned(
                        "get() returned more than one %s -- it returned %s! "
                        "Lookup parameters were %s"
                        % (self.field.model._meta.object_name, num, kwargs))


class MongoDBManyToManyRel(object):
    """
    This object holds the information of the M2M relationship.
    It's accessed by Django admin/forms in various contexts, and we also
    use it internally. We try to simulate what's needed by Django.
    """
    def __init__(self, field, to, related_name, embed):
        self.model = None # added later from contribute_to_class
        self.through = None # added later from contribute_to_class
        self.field = field
        self.to = to
        self.related_name = related_name
        self.embed = embed
        self.field_name = self.to._meta.pk.name
        # Required for Django admin/forms to work.
        self.multiple = True
        self.limit_choices_to = {}
    
    def is_hidden(self):
        return False
    
    def get_related_field(self, *args, **kwargs):
        return self.field

class MongoDBManyToManyField(models.ManyToManyField, ListField):
    """
    A generic MongoDB many-to-many field that can store embedded copies of
    the referenced objects. Inherits from djangotoolbox.fields.ListField.
    
    The field's value is a MongoDBM2MRelatedManager object that works similarly to Django's
    RelatedManager objects, so you can add(), remove(), creaet() and clear() on it.
    To access the related object instances, all() is supported. It will return
    all the related instances, using the embedded copies if available.
    
    If you want the 'real' related (non-embedded) model instances, call all_objs() instead.
    If you want the list of related ObjectIds, call all_refs() instead.
    
    The related model will also gain a new accessor method xxx_set() to make reverse queries.
    That accessor is a MongoDBM2MReverseManager that provides an all() method to return
    a QuerySet of related objects.
    
    For example, if you have an Article model with a MongoDBManyToManyField 'categories'
    that refers to Category objects, you will have these methods:
    
    article.categories.all() - Returns all the categories that belong to the article
    category.article_set.all() - Returns all the articles that belong to the category
    """
    description = 'ManyToMany field with references and optional embedded objects'
    
    def __init__(self, to, related_name=None, embed=False, *args, **kwargs):
        # Call Field, not super, to skip Django's ManyToManyField extra stuff
        # we don't need
        self._mm2m_to_or_name = to
        self._mm2m_related_name = related_name
        self._mm2m_embed = embed
        if embed:
            item_field = EmbeddedModelField(to)
        else:
            item_field = None
        ListField.__init__(self, item_field, *args, **kwargs)

    def contribute_after_resolving(self, field, to, model):
        # Setup the main relation helper
        self.rel = MongoDBManyToManyRel(self, to, self._mm2m_related_name,
                                        self._mm2m_embed)
        # The field's default value will be an empty MongoDBM2MRelatedManager
        # that's not connected to a model instance
        self.default = MongoDBM2MRelatedManager(self, self.rel,
                                                self._mm2m_embed)
        self.rel.model = model
        self.rel.through = create_through(self, self.rel.model, self.rel.to)
        # Determine related name automatically unless set
        if not self.rel.related_name:
            self.rel.related_name = model._meta.object_name.lower() + '_set'

        # Add the reverse relationship
        setattr(self.rel.to, self.rel.related_name,
                MongoDBM2MReverseDescriptor(model, self, self.rel,
                                            self.rel.embed))
        # Add the relationship descriptor to the model class for Django
        # admin/forms to work
        setattr(model, self.name,
                MongoDBManyToManyRelationDescriptor(self, self.rel.through))
    
    def contribute_to_class(self, model, name):
        self.__m2m_name = name
        # Call Field, not super, to skip Django's ManyToManyField extra stuff
        # we don't need
        ListField.contribute_to_class(self, model, name)
        # Do the rest after resolving the 'to' relation
        add_lazy_relation(model, self, self._mm2m_to_or_name,
                          self.contribute_after_resolving)
    
    def db_type(self, *args, **kwargs):
        return 'list'

    def get_internal_type(self):
        return 'ListField'

    def formfield(self, **kwargs):
        from django import forms
        db = kwargs.pop('using', None)
        defaults = {
            'form_class': forms.ModelMultipleChoiceField,
            'queryset': self.rel.to._default_manager.using(db).complex_filter(
                self.rel.limit_choices_to)
        }
        defaults.update(kwargs)
        # If initial is passed in, it's a list of related objects, but the
        # MultipleChoiceField takes a list of IDs.
        if defaults.get('initial') is not None:
            initial = defaults['initial']
            if callable(initial):
                initial = initial()
            defaults['initial'] = [i._get_pk_val() for i in initial]
        return models.Field.formfield(self, **defaults)

    def pre_save(self, model_instance, add):
        return self.to_python(getattr(model_instance, self.attname))

    def get_db_prep_lookup(self, lookup_type, value, connection,
                           prepared=False):
        # This is necessary because the ManyToManyField.get_db_prep_lookup will
        # convert 'A' objects into a unicode string. We don't want that.
        if isinstance(value, A):
            return value
        else:
            return models.ManyToManyField.get_db_prep_lookup(
                        self, lookup_type, value, connection, prepared)

    def get_db_prep_value(self, value, connection, prepared=False):
        # The Python value is a MongoDBM2MRelatedManager, and we'll store the
        #  models it contains as a special list.
        if not isinstance(value, MongoDBM2MRelatedManager):
            # Convert other values to manager objects first
            value = MongoDBM2MRelatedManager(self, self.rel,
                                             self.rel.embed, value)
        # Let the manager to the conversion
        return value.get_db_prep_value(connection, prepared)
    
    def to_python(self, value):
        # The database value is a custom MongoDB list of ObjectIds and embedded
        # models (if embed is enabled). We convert it into a
        # MongoDBM2MRelatedManager object to hold the Django models.
        if not isinstance(value, MongoDBM2MRelatedManager):
            manager = MongoDBM2MRelatedManager(self, self.rel, self.rel.embed)
            manager.to_python(value)
            value = manager
        return value


def _replace_Q(q, column, allowed_fields=None):
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
            _replace_Q(child, column, allowed_fields)
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
            q.children.insert(index, (column, _combine_A(child[0], child[1])))
        else:
            raise TypeError("Unknown type in Q.children")
    return True


def _combine_A(field, value):
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



# Sort of hackish, but they left me no choice! Without this, 'A' objects are
# rejected for this field because it's not in "DJANGOTOOLBOX_FIELDS"
django_mongodb_engine.query.DJANGOTOOLBOX_FIELDS += \
                                (MongoDBManyToManyField,)


