
import django_mongodb_engine.query

from . import fields
from . import manager
from . import query


# How much to show when query set is viewed in the Python shell
REPR_OUTPUT_SIZE = 20

# Sort of hackish, but they left me no choice! Without this, 'A' objects are
# rejected for this field because it's not in "DJANGOTOOLBOX_FIELDS"
django_mongodb_engine.query.DJANGOTOOLBOX_FIELDS += \
                                (fields.MongoDBManyToManyField,)

