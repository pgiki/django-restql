import copy

from rest_framework.serializers import (
    Serializer, ListSerializer,
    ValidationError
)
from django.db.models import Prefetch
from django.db.models.fields.related import(
    ManyToOneRel, ManyToManyRel
)

from .parser import Parser
from .exceptions import FieldNotFound
from .operations import ADD, CREATE, REMOVE, UPDATE
from .fields import (
    _ReplaceableField, _WritableField,
    DynamicSerializerMethodField
)


class DynamicFieldsMixin(object):
    query_param_name = "query"

    def __init__(self, *args, **kwargs):
        # Don't pass 'query', 'fields' and 'exclude' arg up to the superclass
        self.query = kwargs.pop('query', None)  # Parsed query
        self.allowed_fields = kwargs.pop('fields', None)
        self.excluded_fields = kwargs.pop('exclude', None)
        self.return_pk = kwargs.pop('return_pk', False)

        is_field_set = self.allowed_fields is not None
        is_exclude_set = self.excluded_fields is not None
        msg = "May not set both `fields` and `exclude`"
        assert not(is_field_set and is_exclude_set), msg

        # Instantiate the superclass normally
        super().__init__(*args, **kwargs)

    def to_representation(self, instance):
        if self.return_pk:
            return instance.pk
        return super().to_representation(instance)

    @classmethod
    def has_query_param(cls, request):
        return cls.query_param_name in request.query_params

    @classmethod
    def get_raw_query(cls, request):
        return request.query_params[cls.query_param_name]

    @classmethod
    def get_parsed_query_from_req(cls, request):
        raw_query = cls.get_raw_query(request)
        parser = Parser(raw_query)
        try:
            parsed_query = parser.get_parsed()
            return parsed_query
        except SyntaxError as e:
            msg = (
                "QueryFormatError: " + 
                e.msg + " on " + 
                e.text
            )
            raise ValidationError(msg) from None

    def get_allowed_fields(self):
        fields = super().fields
        if self.allowed_fields is not None:
            # Drop any fields that are not specified on `fields` argument.
            allowed = set(self.allowed_fields)
            existing = set(fields)
            not_allowed = existing.symmetric_difference(allowed)
            for field_name in not_allowed:
                try:
                    fields.pop(field_name)
                except KeyError:
                    msg = "Field `%s` is not found" % field_name
                    raise FieldNotFound(msg) from None

        if self.excluded_fields is not None:
            # Drop any fields that are not specified on `exclude` argument.
            not_allowed = set(self.excluded_fields)
            for field_name in not_allowed:
                try:
                    fields.pop(field_name)
                except KeyError:
                    msg = "Field `%s` is not found" % field_name
                    raise FieldNotFound(msg) from None
        return fields

    @property
    def fields(self):
        fields = self.get_allowed_fields()
        request = self.context.get('request')
        
        is_not_a_request_to_process = (
            request is None or 
            request.method != "GET" or 
            not self.has_query_param(request)
        )

        if is_not_a_request_to_process:
            return fields

        is_top_retrieve_request = (
            self.field_name is None and 
            self.parent is None
        )
        is_top_list_request = (
            isinstance(self.parent, ListSerializer) and 
            self.parent.parent is None and
            self.parent.field_name is None
        )

        if is_top_retrieve_request or is_top_list_request:
            if self.query is None:
                self.query = self.get_parsed_query_from_req(request)
        elif isinstance(self.parent, ListSerializer):
            field_name = self.parent.field_name
            parent = self.parent.parent
            if hasattr(parent, "nested_fields_queries"):
                parent_nested_fields = parent.nested_fields_queries
                self.query = parent_nested_fields.get(field_name, None)
        elif isinstance(self.parent, Serializer):
            field_name = self.field_name
            parent = self.parent
            if hasattr(parent, "nested_fields_queries"):
                parent_nested_fields = parent.nested_fields_queries
                self.query = parent_nested_fields.get(field_name, None)
        else:
            # Unkown scenario
            # No filtering of fields
            return fields

        if self.query is None:
            # No filtering on nested fields
            # Retrieve all nested fields
            return fields
            
        all_fields = list(fields.keys())
        allowed_nested_fields = {}
        allowed_flat_fields = []
        for field in self.query:
            if isinstance(field, dict):
                # Nested field
                for nested_field in field:
                    if nested_field not in all_fields:
                        msg = "'%s' field is not found" % field
                        raise ValidationError(msg)
                    nested_classes = (
                        Serializer, ListSerializer, 
                        DynamicSerializerMethodField
                    )
                    if not isinstance(fields[nested_field], nested_classes):
                        msg = "'%s' is not a nested field" % nested_field
                        raise ValidationError(msg)
                allowed_nested_fields.update(field)
            else:
                # Flat field
                if field not in all_fields:
                    msg = "'%s' field is not found" % field
                    raise ValidationError(msg)
                allowed_flat_fields.append(field)
        self.nested_fields_queries = allowed_nested_fields
        
        all_allowed_fields = (
            allowed_flat_fields + list(allowed_nested_fields.keys())
        )
        for field in all_fields:
            if field not in all_allowed_fields:
                fields.pop(field)

        return fields


class NestedCreateMixin(object):
    """ Create Mixin """
    def create_writable_foreignkey_related(self, data):
        # data format {field: {sub_field: value}}
        objs = {}
        for field, value in data.items():
            # Get serializer class for nested field
            SerializerClass = type(self.get_fields()[field])
            serializer = SerializerClass(data=value, context=self.context)
            serializer.is_valid()
            obj = serializer.save()
            objs.update({field: obj})
        return objs

    def bulk_create_objs(self, field, data):
        model = self.get_fields()[field].child.Meta.model
        SerializerClass = type(self.get_fields()[field].child)
        pks = []
        for values in data:
            serializer = SerializerClass(data=values, context=self.context)
            serializer.is_valid()
            obj = serializer.save()
            pks.append(obj.pk)
        return pks

    def create_many_to_one_related(self, instance, data):
        # data format {field: {
        # foreignkey_name: name,
        # data: {
        # ADD: [pks], 
        # CREATE: [{sub_field: value}]
        # }}
        field_pks = {}
        for field, values in data.items():
            model = self.Meta.model
            foreignkey = getattr(model, field).field.name
            for operation in values:
                if operation == ADD:
                    pks = values[operation]
                    model = self.get_fields()[field].child.Meta.model
                    qs = model.objects.filter(pk__in=pks)
                    qs.update(**{foreignkey: instance.pk})
                    field_pks.update({field: pks})
                elif operation == CREATE:
                    for v in values[operation]:
                        v.update({foreignkey: instance.pk})
                    pks = self.bulk_create_objs(field, values[operation])
                    field_pks.update({field: pks})
        return field_pks

    def create_many_to_many_related(self, instance, data):
        # data format {field: {
        # ADD: [pks], 
        # CREATE: [{sub_field: value}]
        # }}
        field_pks = {}
        for field, values in data.items():
            for operation in values:
                if operation == ADD:
                    obj = getattr(instance, field)
                    pks = values[operation]
                    obj.set(pks)
                    field_pks.update({field: pks})
                elif operation == CREATE:
                    obj = getattr(instance, field)
                    pks = self.bulk_create_objs(field, values[operation])
                    obj.set(pks)
                    field_pks.update({field: pks})
        return field_pks

    def create(self, validated_data):
        fields = {
            "foreignkey_related": { 
                "replaceable": {},
                "writable": {}
            }, 
            "many_to": {
                "many_related": {},
                "one_related": {}
            }
        }

        # Make a partal copy of validated_data so that we can
        # iterate and alter it
        data = copy.copy(validated_data)
        for field in data:
            field_serializer = self.get_fields()[field]
            if isinstance(field_serializer, Serializer):
                if isinstance(field_serializer, _ReplaceableField):
                    value = validated_data.pop(field)
                    fields["foreignkey_related"]["replaceable"] \
                        .update({field: value})
                elif isinstance(field_serializer, _WritableField):
                    value = validated_data.pop(field)
                    fields["foreignkey_related"]["writable"]\
                        .update({field: value})
            elif (isinstance(field_serializer, ListSerializer) and 
                    (isinstance(field_serializer, _WritableField) or 
                    isinstance(field_serializer, _ReplaceableField))):

                model = self.Meta.model
                rel = getattr(model, field).rel
    
                if isinstance(rel, ManyToOneRel):
                    value = validated_data.pop(field)
                    fields["many_to"]["one_related"].update({field: value})
                elif isinstance(rel, ManyToManyRel):
                    value = validated_data.pop(field)
                    fields["many_to"]["many_related"].update({field: value})
            else:
                pass

        foreignkey_related = {
            **fields["foreignkey_related"]["replaceable"],
            **self.create_writable_foreignkey_related(
                fields["foreignkey_related"]["writable"]
            )
        }

        instance = super().create({**validated_data, **foreignkey_related})
        
        self.create_many_to_many_related(
            instance, 
            fields["many_to"]["many_related"]
        )

        self.create_many_to_one_related(
            instance, 
            fields["many_to"]["one_related"]
        )
        
        return instance


class NestedUpdateMixin(object):
    """ Update Mixin """
    def constrain_error_prefix(self, field):
        return "Error on %s field: " % (field,)

    def update_replaceable_foreignkey_related(self, instance, data):
        # data format {field: obj}
        objs = {}
        for field, nested_obj in data.items():
            setattr(instance, field, nested_obj)
            instance.save()
            objs.update({field: instance})
        return objs

    def update_writable_foreignkey_related(self, instance, data):
        # data format {field: {sub_field: value}}
        objs = {}
        for field, values in data.items():
            # Get serializer class for nested field
            SerializerClass = type(self.get_fields()[field])
            nested_obj = getattr(instance, field)
            serializer = SerializerClass(
                nested_obj, 
                data=values, 
                context=self.context,
                partial=self.partial
            )
            serializer.is_valid()
            serializer.save()
            objs.update({field: nested_obj})
        return objs

    def bulk_create_many_to_many_related(self, field, nested_obj, data):
        # Get serializer class for nested field
        SerializerClass = type(self.get_fields()[field].child)
        pks = []
        for values in data:
            serializer = SerializerClass(data=values, context=self.context)
            serializer.is_valid()
            obj = serializer.save()
            pks.append(obj.pk)
        nested_obj.add(*pks)
        return pks

    def bulk_create_many_to_one_related(self, field, nested_obj, data):
        # Get serializer class for nested field
        SerializerClass = type(self.get_fields()[field].child)
        pks = []
        for values in data:
            serializer = SerializerClass(data=values, context=self.context)
            serializer.is_valid()
            obj = serializer.save()
            pks.append(obj.pk)
        return pks

    def bulk_update_many_to_many_related(self, field, nested_obj, data):
        # {pk: {sub_field: values}}
        objs = []

        # Get serializer class for nested field
        SerializerClass = type(self.get_fields()[field].child)
        for pk, values in data.items():
            obj = nested_obj.get(pk=pk)
            serializer = SerializerClass(
                obj, 
                data=values, 
                context=self.context, 
                partial=self.partial
            )
            serializer.is_valid()
            obj = serializer.save()
            objs.append(obj)
        return objs

    def bulk_update_many_to_one_related(self, field, instance, data):
        # {pk: {sub_field: values}}
        objs = []

        # Get serializer class for nested field
        SerializerClass = type(self.get_fields()[field].child)
        model = self.Meta.model
        foreignkey = getattr(model, field).field.name
        nested_obj = getattr(instance, field)
        for pk, values in data.items():
            obj = nested_obj.get(pk=pk)
            values.update({foreignkey: instance.pk})
            serializer = SerializerClass(
                obj, 
                data=values, 
                context=self.context, 
                partial=self.partial
            )
            serializer.is_valid()
            obj = serializer.save()
            objs.append(obj)
        return objs

    def update_many_to_one_related(self, instance, data):
        # data format {field: {
        # foreignkey_name: name:
        # data: {
        # ADD: [{sub_field: value}], 
        # CREATE: [{sub_field: value}], 
        # REMOVE: [pk],
        # UPDATE: {pk: {sub_field: value}} 
        # }}}

        for field, values in data.items():
            nested_obj = getattr(instance, field)
            model = self.Meta.model
            foreignkey = getattr(model, field).field.name
            for operation in values:
                if operation == ADD:
                    pks = values[operation]
                    model = self.get_fields()[field].child.Meta.model
                    qs = model.objects.filter(pk__in=pks)
                    qs.update(**{foreignkey: instance.pk})
                elif operation == CREATE:
                    for v in values[operation]:
                        v.update({foreignkey: instance.pk})
                    self.bulk_create_many_to_one_related(
                        field, 
                        nested_obj, 
                        values[operation]
                    )
                elif operation == REMOVE:
                    qs = nested_obj.all()
                    qs.filter(pk__in=values[operation]).delete()
                elif operation == UPDATE:
                    self.bulk_update_many_to_one_related(
                        field, 
                        instance,
                        values[operation]
                    )
                else:
                    message = (
                        "%s is an invalid operation, " % (operation,)
                    )
                    raise ValidationError(message)
        return instance

    def update_many_to_many_related(self, instance, data):
        # data format {field: {
        # ADD: [{sub_field: value}], 
        # CREATE: [{sub_field: value}], 
        # REMOVE: [pk],
        # UPDATE: {pk: {sub_field: value}} 
        # }}
        for field, values in data.items():
            nested_obj = getattr(instance, field)
            for operation in values:
                if operation == ADD:
                    pks = values[operation]
                    try:
                        nested_obj.add(*pks)
                    except Exception as e:
                        msg = self.constrain_error_prefix(field) + str(e)
                        raise ValidationError(msg)
                elif operation == CREATE:
                    self.bulk_create_many_to_many_related(
                        field, 
                        nested_obj, 
                        values[operation]
                    )
                elif operation == REMOVE:
                    pks = values[operation]
                    try:
                        nested_obj.remove(*pks)
                    except Exception as e:
                        msg = self.constrain_error_prefix(field) + str(e)
                        raise ValidationError(msg)
                elif operation == UPDATE:
                    self.bulk_update_many_to_many_related(
                        field, 
                        nested_obj, 
                        values[operation]
                    )
                else:
                    message = (
                        "%s is an invalid operation, " % (operation,)
                    )
                    raise ValidationError(message)
        return instance

    def update(self, instance, validated_data):
        fields = {
            "foreignkey_related": { 
                "replaceable": {},
                "writable": {}
            },
            "many_to": {
                "many_related": {},
                "one_related": {}
            }
        }

        # Make a partal copy of validated_data so that we can
        # iterate and alter it
        data = copy.copy(validated_data)
        for field in data:
            field_serializer = self.get_fields()[field]
            if isinstance(field_serializer, Serializer):
                if isinstance(field_serializer, _ReplaceableField):
                    value = validated_data.pop(field)
                    fields["foreignkey_related"]["replaceable"] \
                        .update({field: value})
                elif isinstance(field_serializer, _WritableField):
                    value = validated_data.pop(field)
                    fields["foreignkey_related"]["writable"] \
                        .update({field: value})
            elif (isinstance(field_serializer, ListSerializer) and
                    (isinstance(field_serializer, _WritableField) or 
                    isinstance(field_serializer, _ReplaceableField))):
                model = self.Meta.model
                rel = getattr(model, field).rel
    
                if isinstance(rel, ManyToOneRel):
                    value = validated_data.pop(field)
                    fields["many_to"]["one_related"].update({field: value})
                elif isinstance(rel, ManyToManyRel):
                    value = validated_data.pop(field)
                    fields["many_to"]["many_related"].update({field: value})
            else:
                pass

        self.update_replaceable_foreignkey_related(
            instance,
            fields["foreignkey_related"]["replaceable"]
        )

        self.update_writable_foreignkey_related(
            instance,
            fields["foreignkey_related"]["writable"]
        )

        self.update_many_to_many_related(
            instance,
            fields["many_to"]["many_related"]
        )

        self.update_many_to_one_related(
            instance,
            fields["many_to"]["one_related"]
        )

        return super().update(instance, validated_data)


class RestQLViewMixin(object):
    @property
    def restql_query(self):
        serializer_class = self.get_serializer_class()

        if hasattr(serializer_class, "query_param_name"):
            return serializer_class.get_parsed_query_from_req(self.request)

    def get_queryset(self):
        queryset = super().get_queryset()

        if self.restql_query is not None:
            queryset = self.get_restql_queryset(queryset)
        return queryset

    def get_prefetch_related_mapping(self):
        serializer_class = self.get_serializer_class()
        if hasattr(self, "prefetch_related"):
            return self.prefetch_related
        elif (
            serializer_class
            and hasattr(serializer_class, "Meta")
            and hasattr(serializer_class.Meta, "prefetch_related")
        ):
            return self.get_serializer_class().Meta.prefetch_related

        return {}

    def get_select_related_mapping(self):
        serializer_class = self.get_serializer_class()
        if hasattr(self, "select_related"):
            return self.select_related
        elif (
            serializer_class
            and hasattr(serializer_class, "Meta")
            and hasattr(serializer_class.Meta, "select_related")
        ):
            return self.get_serializer_class().Meta.select_related

        return {}

    def get_restql_query_dict(self, data=None):
        """
        Returns the RestQL query as a dict.
        """
        keys = {}
        if not data:
            data = self.restql_query

        for item in data:
            if isinstance(item, str):
                keys[item] = None
            elif isinstance(item, dict):
                for key, nested_items in item.items():
                    key_base = key
                    nested_keys = self.get_restql_query_dict(nested_items)
                    keys[key_base] = nested_keys

        return keys

    def get_restql_queryset(self, queryset):
        if self.restql_query is not None:
            queryset = self.apply_restql_orm_mapping(queryset)
        return queryset

    def get_all_dict_values(self, dict_to_parse):
        """
        Helper function to get *all* values from a dict and it's nested dicts.
        """
        values = []

        for value in dict_to_parse.values():
            if isinstance(value, dict):
                values.extend(self.get_all_dict_values(value))
            else:
                values.append(value)

        return values

    def get_mapping_values(self, parsed, mapping):
        """
        Returns the mapping value (or nested mapping values as needed) of a particular parsed dict
        against the mapping provided. Parsed input expected to come from get_dict from the parser.
        """
        values = []

        for parsed_key, parsed_value in parsed.items():
            if parsed_key in mapping.keys():
                mapping_value = mapping[parsed_key]

                if isinstance(mapping_value, dict):
                    base = mapping_value.get("base")
                    nested = mapping_value.get("nested")
                else:
                    base = mapping_value
                    nested = None

                # This should never be a falsy value, but we're being safe here.
                if base:
                    values.append(base)

                if nested:
                    # If we're given a dict, we only want keys that were mapped as needed, so
                    # recursively call with the smaller map.
                    if isinstance(parsed_value, dict):
                        nested_values = self.get_mapping_values(parsed_value, nested)
                        values.extend(nested_values)
                    else:
                        # If we don't have a dict, we want every nested value, since it's assumed
                        # all of them will be present. We recursively get all values from here.
                        nested_values = self.get_all_dict_values(nested)
                        values.extend(nested_values)
        return values

    def apply_restql_orm_mapping(self, queryset):
        """
        Applies appropriate select_related and prefetch_related calls on a
        queryset based on the passed on dictionaries provided.
        """
        parsed_keys = self.get_restql_query_dict()
        select = self.get_select_related_mapping()
        prefetch = self.get_prefetch_related_mapping()

        select_mapped = self.get_mapping_values(parsed_keys, select)
        prefetch_mapped = self.get_mapping_values(parsed_keys, prefetch)

        for value in select_mapped:
            if isinstance(value, str):
                queryset = queryset.select_related(value)
            elif isinstance(value, list):
                for select_value in value:
                    queryset = queryset.select_related(select_value)

        for value in prefetch_mapped:
            if isinstance(value, str) or isinstance(value, Prefetch):
                queryset = queryset.prefetch_related(value)
            elif isinstance(value, list):
                for prefetch_value in value:
                    queryset = queryset.prefetch_related(prefetch_value)

        return queryset
