
from django.db import router
try:
    # ObjectId has been moved to bson.objectid in newer versions of PyMongo
    from bson.objectid import ObjectId
except ImportError:
    from pymongo.objectid import ObjectId



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
            try:
                # Load referred instance from db and keep in memory
                obj['obj'] = self.rel.to.objects.get(pk=obj['pk'])
            except self.rel.to.DoesNotExist:
                # cheap clean : throw away obj which is not in db
                # Note : obj has a unique pk in the same mongodb instance
                self.objects.remove(obj)
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
                        self.rel_to_name: self.rel_to_instance}
            wrapper = self.appear_as_relationship_model(**args)
            return wrapper
        return obj['obj']

    def __iter__(self):
        for obj in list(self.objects):
            #ignore obj of nowhere
            obj_cached_or_loaded = self._get_obj(obj)
            if not obj_cached_or_loaded is None:
                yield obj_cached_or_loaded

    def __repr__(self):
        from . import REPR_OUTPUT_SIZE
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

    def _clone(self, klass=None, setup=False, **kwargs):
        '''
        return a clone of self queryset
        works similar to django.db.models.query.QuerySet.clone
        '''
        if klass is None:
            klass = self.__class__
        #copy self.objects
        objects = list(self.objects)
        c = klass(rel=self.rel, model=self.model, objects=objects,
                  use_cached=True,
                  appear_as_relationship=(
                      self.appear_as_relationship_model,
                      self.rel_model_instance,
                      self.rel_to_instance,
                      self.rel_model_name, self.rel_to_name)
              )
        c.__dict__.update(kwargs)
        #no use for now
        if setup and hasattr(c, '_setup_query'):
            c._setup_query()
        return c

    def values_list(self, *fields, **kwargs):
        '''
        required by django.contrib.admin
        '''
        flat = kwargs.pop('flat', False)
        if kwargs:
            raise TypeError('Unexpected keyword arguments to values_list: %s'
                    % (list(kwargs),))
        if flat and len(fields) > 1:
            raise TypeError("'flat' is not valid when values_list is called with more than one field.")
        return self._clone(klass=MongoDBM2MValuesListQuerySet, setup=True, flat=flat,
                _fields=fields)

class MongoDBM2MValuesListQuerySet(MongoDBM2MQuerySet):
    '''
    simulate ValuesListQuerySet, using objects instead of query
    '''
    def iterator(self):
        '''
        iterator yield only fields requested
        '''
        for obj in list(self.objects):
            obj_cached_or_loaded = self._get_obj(obj)
            # skip when obj not in cached and db
            if obj_cached_or_loaded is None:
                pass
            else:
                #behavior same as ValuesListQuerySet.iterator
                if self.flat and len(self._fields) == 1:
                    field = self._fields[0]
                    if hasattr(obj['obj'], field):
                        yield obj['obj'].__getattribute__(field)
                    else:
                        yield None
                else:
                    row = list()
                    for field in self._fields:
                        if hasattr(obj['obj'], field):
                            row.append(obj['obj'].__getattribute__(field))
                        else:
                            row.append(None)
                    yield tuple(row)

    def __iter__(self):
        for item in self.iterator():
            yield item

    def _clone(self, *args, **kwargs):
        '''
        override MongoDBM2MQuerySet._clone, clone this query set
        '''
        clone = super(MongoDBM2MValuesListQuerySet, self)._clone(*args, **kwargs)
        if not hasattr(clone, "flat"):
            # Only assign flat if the clone didn't already get it from kwargs
            clone.flat = self.flat
        return clone
