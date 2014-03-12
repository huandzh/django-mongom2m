from django.db import models
from django.db.models.fields.related import add_lazy_relation
from django.db.models.query_utils import DeferredAttribute
from django_mongodb_engine.query import A
from djangotoolbox.fields import ListField, EmbeddedModelField

from .manager import (MongoDBManyToManyRel, MongoDBM2MRelatedManager,
                      MongoDBM2MReverseDescriptor,
                      MongoDBManyToManyRelationDescriptor)
from .utils import create_through


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

    def get_db_prep_save(self, value, connection, prepared=False):
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

        if not isinstance(value, MongoDBM2MRelatedManager) and \
           not isinstance(value, DeferredAttribute):
            manager = MongoDBM2MRelatedManager(self, self.rel, self.rel.embed)
            manager.to_python(value)
            value = manager
        return value






