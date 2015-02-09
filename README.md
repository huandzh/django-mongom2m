Django MongoDB ManyToManyField Implementation
=============================================

Created in 2012 by Kenneth Falck, Modified/Extended by:
* Merchant Atlas Inc. 2013

Released under the standard BSD License (see below).

Overview
--------

This is a simple implementation of ManyToManyFields for django-mongodb-engine. The _MongoDBManyToManyField_
stores references to other Django model instances as ObjectIds in a MongoDB list field.

Optionally, _MongoDBManyToManyField_ will also embed a "cached" copy of the instances inside the list. This
allows fast access to the data without having to query each related object from the database separately.

_MongoDBManyToManyField_ attempts to work mostly in the same way as Django's built-in ManyToManyField.
Related objects can be added and removed with the add(), remove(), clear() and create() methods.

To enumerate the objects, the all() method returns a simulated QuerySet object which loads non-embedded
objects automatically from the database when needed.

On the reverse side of the relation, an accessor property is added (usually called OtherModel.modelname\_set,
can be overridden with the related\_name attribute) to return the related objects in the reverse direction.
It uses MongoDB's raw\_query() to find all related model objects. Because of this, any data model that
uses MongoDBManyToManyField() must have a default MongoDBManager() instead of Django's normal Manager().


Django compatibility
--------------------

This implementation has been tweaked to be mostly compatible with Django admin, which means you can use
TabularInlines or filter\_horizontal and filter\_vertical to administer the many-to-many fields.

Don't be surprised, however, if some things don't work, because it's all emulated. There is no real
"through" table in the database to provide the many-to-many association.

Supported version: [django-nonrel-1.6](https://github.com/django-nonrel/django/tree/nonrel-1.6)

Usage
-----

### Define a field
Example model using a many-to-many field:

    from django.db import models
    from django_mongom2m.fields import MongoDBManyToManyField
    from django_mongodb_engine.contrib import MongoDBManager

    class Category(models.Model):
        objects = MongoDBManager()
        title = models.CharField(max_length=254)

    class Article(models.Model):
        objects = MongoDBManager()
        categories = MongoDBManyToManyField(Category)
        title = models.CharField(max_length=254)
        text = models.TextField()

### Add an instance
To store categories in the field, you would first create the category and then add it:

    category = Category(title='foo')
    category.save()

    article = Article(title='bar')
    article.categories.add(category)

    for cat in article.categories.all():
        print cat.title

    for art in category.article_set.all():
        print art.title

### Basic Querying
Querying with _MongoDBManyToManyField_ is similar to normal SQL Django, but with some caveats.
In order to have true Django behavior, we would have had to change some combination of: Django-nonrel,
Djangotoolbox, and mongodb-engine; so instead, we went a different route.

How Django does it:

    Article.objects.filter(categories=category)
    [<Article: Article object>, <Article: Article object>]

    article.categories.all()
    [<Category: Category object>, <Category: Category object>]

How _MongoDBManyToManyField_ does it:

    Article.categories.filter(pk=category)
    [<Article: Article object>, <Article: Article object>]

    article.categories.all()
    [<Category: Category object>, <Category: Category object>]

### Embed Models for Performance and Querying
To enable embedding, just add the embed=True keyword argument to the field:

    class Article(models.Model):
        categories = _MongoDBManyToManyField_(Category, embed=True)

**Note about embedding**: If you change a _MongoDBManyToManyField_ from `embed=False` to
`embed=True` (or vice versa), the host model objects will not be automatically updated.
You will need to re-save every object of that model.
Example:

    for article in Article.objects.all():
        article.save() # Re-saving will now embed the categories automatically

### Query with or without cache
To query with or without cache, just passing `use_cached=<True or False>` argument to supported query.

Examples:

    # return all instances in cache
    article.categories.all(use_cached=False)
	# return a list of ids
	# to be compatible with admin site, values_list use `use_cached=False` by default
	article.categories.values_list('pk', flat=True)

### Refresh cache
To remove instances already deleted by other other actions:

    article.categories.remove_nonexists()

To refresh cache and remove instances already deleted:

    article.categories.reload_from_db()

### Advanced Querying (Embedded models)
If you use `embed=True`, _MongoDBManyToManyField_ can do more than just query on 'pk'.
You can do any of: get, filter, and exclude; while using Q objects and A objects
(from mongodb-engine). _MongoDBManyToManyField_ will automatically convert them to
query correctly on the embedded document.
Note: The models have to be embedded because MongoDB doesn't support joins.

    Article.categories.filter(title="shirts")
    Article.categories.filter(Q(title="shirts") | Q(title="hats"))
    Article.categories.filter(Q(title="shirts") & ~Q(title="men's"))

    # If categories had an embedded model 'em', you could even query it with A()
    Article.categories.filter(em=A("name", "em1"))

### Limitations
There are some things that won't work with _MongoDBManyToManyField_:
#### Chaining multiple filters or excludes together
Under the covers, the initial filter is being called on the Article QuerySet, so what
gets returned is an Article QuerySet. That means calling filter() again will not get
any of the magic provided by _MongoDBManyToManyField_.

    # filter(title="hats").exclude(title="") will act on Article, not categories
    Article.categories.filter(title="shirts").filter(title="hats").exclude(title="")
    # However, the same can be accomplished with Q objects
    Article.categories.filter(Q(title="shirts") & Q(title="hats") & ~Q(title=""))

#### Double-underscore commands
This is actually an issue with djangotoolbox/mongobd-engine. Under the covers,
_MongoDBManyToManyField_ uses A() objects to generate the queries. Double-underscore
commands from A() objects don't work.

    # Will query the field 'title__contains', which doesn't exist
    Article.categories.filter(title__contains="men")

    # Unfortunately, the only solution for this would be to do a raw_query
    import re
    Article.objects.raw_query({"categories.title": re.compile("men")})


Signals
-------

_MongoDBManyToManyField_ supports Django's m2m\_changed signal, where the action can be:

* pre\_add (triggered before adding object(s) to the field)
* post\_add (triggered after adding object(s) to the field)
* pre\_remove (triggered before removing object(s) from the field)
* post\_remove (triggered after removing object(s) from the field)
* pre\_clear (triggered before clearing all object(s) from the field)
* post\_clear (triggered after clearing all object(s) from the field)

The only difference is that the instance argument of the signal is (at least currently)
not an intermediate 'through' model instance, but the actual model instance that contains
the many-to-many field. Also, currently, reverse relationship signals are not sent.


Indexing
--------

Many-to-many related querying will use the "id" field the embedded model fields,
whether full embedding is used or not. If full embedding is not used, then those
fields will be sub-objects containing only an "id" field.

In either case, you should index the "id" fields properly. This can be done as follows:

    from django.db import connection
    connection.get_collection('blog_article').ensure_index([('categories.id', 1)])

(Replacing, of course, 'blog\_article' and 'categories' with the appropriate collection
and field names.)


Migrating
---------

If you have an old model that's using something like this:

    categories = ListField(EmbeddedField(Category))

You can normally change it to:

    categories = MongoDBManyToManyField(Category)

The many-to-many field's data is almost identical to that of the embedded field,
except that MongoDB object ids are stored as ObjectIds instead of strings. When
the field is loaded from the datatabase, the id strings are automatically converted
to ObjectIds. So the next time the model containing the field is saved, the ids
are written correctly.

This basically means that you may need to do a migration like this:

    for article in Article.objects.all():
        article.save()

Also make sure that the "id" field is properly indexed (see previous section).


BSD License
-----------
Copyright (c) 2013 Merchant Atlas Inc. http://www.merchantatlas.com

[Original Work] Copyright (c) 2012 Kenneth Falck <kennu@iki.fi> http://kfalck.net
All rights reserved.

Redistribution and use in source and binary forms, with or without modification, are permitted provided that the following conditions are met:

Redistributions of source code must retain the above copyright notice, this list of conditions and the following disclaimer.
Redistributions in binary form must reproduce the above copyright notice, this list of conditions and the following disclaimer in the documentation and/or other materials provided with the distribution.
THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
