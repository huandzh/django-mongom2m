
from django.db import models, router
from django.db.models import Q
from django.db.models.signals import m2m_changed
from .utils import get_exists_ids

try:
    # ObjectId has been moved to bson.objectid in newer versions of PyMongo
    from bson.objectid import ObjectId
except ImportError:
    from pymongo.objectid import ObjectId

from .query import MongoDBM2MQuerySet, MongoDBM2MQueryError
from .utils import replace_Q, combine_A
import warnings


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

    def _remove_by_id_strings(self, removed_obj_ids):
        '''
        Remove specified objects by list of id strings
        '''
        # Send the pre_remove signal
        m2m_changed.send(self.rel.through, instance=self.model_instance,
                         action='pre_remove', reverse=False, model=self.rel.to,
                         pk_set=removed_obj_ids)

        # Commit the remove
        self.objects = [obj for obj in self.objects if str(obj['pk']) not in removed_obj_ids]

        # Send the post_remove signal
        m2m_changed.send(self.rel.through, instance=self.model_instance,
                         action='post_remove', reverse=False, model=self.rel.to,
                         pk_set=removed_obj_ids)


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
        self._remove_by_id_strings(removed_obj_ids)

        if auto_save:
            self.model_instance.save()

    def remove_nonexists(self, **kwargs):
        """
        remove objects not exist in db

        :param auto_save: See add() above for description
        """
        auto_save = kwargs.pop('auto_save', True)

        exists_ids = [obj['_id'] for obj in get_exists_ids(self.model_instance, self.rel, self.objects)]
        removed_obj_ids = [str(obj['pk']) for obj in self.objects if (not obj['pk'] in exists_ids)]
        self._remove_by_id_strings(removed_obj_ids)

        if auto_save:
            self.model_instance.save()


    def reload_from_db(self, **kwargs):
        """
        Reload all objs from db, and remove objs not exists

        A short cut using all(use_cached=False),.
        TODO: dev a more effiecient method
        """
        auto_save = kwargs.pop('auto_save', True)

        all_objects_from_db = [obj for obj in self.all(use_cached=False)]
        self.clear(auto_save=False)
        self.add(*all_objects_from_db, auto_save=False)

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
                                  **kwargs)

    def _queryset_function_helper(self, method, *args, **kwargs):
        '''
        apply non query args before call queryset method:
        (See _MongoDBM2MQuerySet_)
         * use_cached
         * appear_as_relationship
         * exists_in_db_only
        '''
        if not method in ['filter', 'get']:
            raise NotImplementedError(
                'Not implemented for calling queryset method %s' % method)
        to_all_kwargs = dict()
        for kwarg in ['use_cached',
                      'appear_as_relationship',
                      'exists_in_db_only']:
            kwarg_value = kwargs.pop(kwarg, None)
            if not kwarg_value is None:
                to_all_kwargs[kwarg] = kwarg_value
        return self.all(**to_all_kwargs).__getattribute__(method)(*args, **kwargs)

    def filter(self, *args, **kwargs):
        '''
        return filtered queryset
        '''
        return self._queryset_function_helper('filter', *args, **kwargs)

    def get(self, *args, **kwargs):
        '''
        return matched object
        '''
        return self._queryset_function_helper('get', *args, **kwargs)

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

        Depreciated by all(use_cached=False)
        """
        warnings.warn('MongoDBM2MRelatedManager.objs depreciated by all(use_cached=False)',
                      DeprecationWarning)
        return MongoDBM2MQuerySet(self.rel, self.rel.to, self.objects,
                                  use_cached=False)

    def to_python_embedded_instance(self, embedded_instance):
        """
        Convert a single embedded instance value stored in the database to an
        object we can store in the internal objects list.
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
                if isinstance(values, tuple):
                    # In some versions of django-toolbox, 'values' is a tuple.
                    values = dict(values)
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
        Convert a database value to Django model instances managed by this
        manager.
        """
        if isinstance(values, models.Model):
            # Single value given as parameter
            values = [values]
        self.objects = [self.to_python_embedded_instance(value)
                        for value in values]

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
                status = replace_Q(field, column,
                                    ["pk"] if not embedded else None)
                if status:
                    updated_args.append(field)
                else:
                    raise_query_error()
            else:
                # Anything else should be tuples of two items
                updated_args.append(
                    (self.field.column, combine_A(field[0], field[1])))

        updated_kwargs = []
        # Iterate over the kwargs and combine them into A objects
        for field, value in kwargs.iteritems():
            if not embedded and field != 'pk':
                raise_query_error()

            # Have to build Q objects because all the arguments will have the
            # same key in the kwargs otherwise
            updated_kwargs.append(Q(**{column: combine_A(field, value)}))

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
        #for django.core.management.validation
        self.related_query_name = None
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
