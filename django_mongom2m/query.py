
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
                        self.rel_to_name: self.rel_to_instance}
            wrapper = self.appear_as_relationship_model(**args)
            return wrapper
        return obj['obj']

    def __iter__(self):
        for obj in self.objects:
            yield self._get_obj(obj)

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
