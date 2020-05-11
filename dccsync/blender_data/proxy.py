from enum import IntEnum
from functools import lru_cache
import logging
from typing import Any, Mapping, Union
from uuid import uuid4

import bpy
import bpy.types as T  # noqa

from dccsync.blender_data.filter import Context
from dccsync.blender_data.blenddata import BlendData
from dccsync.blender_data.types import is_builtin, is_vector, is_matrix

blenddata = BlendData.instance()
logger = logging.Logger(__name__, logging.INFO)


def debug_check_stack_overflow(func, *args, **kwargs):
    """
    Use as a function decorator to detect probable stack overflow in case of circular references

    Beware : inspect performance is very poor.
    sys.setrecursionlimit cannot be used because it will possibly break the Blender VScode
    plugin and StackOverflowException is not caught by VScode "Raised exceptions" breakpoint.
    """

    def wrapper(*args, **kwargs):
        import inspect

        if len(inspect.stack(0)) > 50:
            raise RuntimeError("Possible stackoverflow")
        return func(*args, **kwargs)

    return wrapper


class LoadElementAs(IntEnum):
    STRUCT = 0
    ID_REF = 1
    ID_DEF = 2


def same_rna(a, b):
    return a.bl_rna == b.bl_rna


@lru_cache(maxsize=None)
def load_as_what(parent, attr_property):
    """
    Determine if we must load an attribute as a struct, a blenddata collection element (ID_DEF)
    or a reference to a BlendData collection element (ID_REF)

    All T.Struct are loaded as struct
    All T.ID are loaded ad IDRef (that is pointer into a D.blendata collection) except
    for specific case. For instance the scene master "collection" is not a D.collections item.

    Arguments
    parent -- the type that contains the attribute names attr_name, for instance T.Scene
    attr_property -- a bl_rna property of a attribute, that can be a CollectionProperty or a "plain" attribute
    """
    # In these types, these members are T.ID that to not link to slots in bpy.data collections
    # so we load them as ID and not as a reference to s also in an bpy.data collection
    # Only include here types that derive from ID

    # TODO use T.Material.bl_rna.properties['node_tree'] ...
    # TODO move to context ?
    force_as_ID_def = {  # noqa N806
        T.Material.bl_rna: ["node_tree"],
        T.Scene.bl_rna: ["collection"],
        T.LayerCollection.bl_rna: ["collection"],
    }
    if same_rna(attr_property, T.CollectionProperty) or same_rna(attr_property, T.PointerProperty):
        element_property = attr_property.fixed_type
    else:
        element_property = attr_property

    is_a_blenddata_ID = element_property.bl_rna in blenddata.types_rna  # noqa N806
    if not is_a_blenddata_ID:
        return LoadElementAs.STRUCT

    if attr_property.identifier in force_as_ID_def.get(parent.bl_rna, []):
        return LoadElementAs.ID_DEF
    else:
        return LoadElementAs.ID_REF


# @debug_check_stack_overflow
def read_attribute(attr: any, attr_property: any, parent_struct, context: Context):
    """
    Load a property into a python object of the appropriate type, be it a Proxy or a native python object


    """
    attr_type = type(attr)

    if is_builtin(attr_type):
        return attr
    if is_vector(attr_type):
        return list(attr)
    if is_matrix(attr_type):
        return [list(col) for col in attr.col]

    # We have tested the types that are usefully reported by the python binding, now harder work.
    # These were implemented first and may be better implemented with the bl_rna property of the parent struct
    if attr_type == T.bpy_prop_array:
        return [e for e in attr]

    if attr_type == T.bpy_prop_collection:
        load_as = load_as_what(parent_struct, attr_property)
        if load_as == LoadElementAs.STRUCT:
            return BpyPropStructCollectionProxy().load(attr, context)
        elif load_as == LoadElementAs.ID_REF:
            # References into Blenddata collection, for instance D.scenes[0].objects
            return BpyPropDataCollectionProxy().load_as_IDref(attr)
        elif load_as == LoadElementAs.ID_DEF:
            # is  BlendData collection, for instance D.objects
            return BpyPropDataCollectionProxy().load_as_ID(attr, context)

    # TODO merge with previous case
    if isinstance(attr_property, T.CollectionProperty):
        return BpyPropStructCollectionProxy().load(attr, context)

    bl_rna = attr_property.bl_rna
    if bl_rna is None:
        logger.warning("Unimplemented attribute %s", attr)
        return None

    assert issubclass(attr_type, T.PropertyGroup) == issubclass(attr_type, T.PropertyGroup)
    if issubclass(attr_type, T.PropertyGroup):
        return BpyPropertyGroupProxy().load(attr, context)

    load_as = load_as_what(parent_struct, attr_property)
    if load_as == LoadElementAs.STRUCT:
        return BpyStructProxy().load(attr, context)
    elif load_as == LoadElementAs.ID_REF:
        return BpyIDRefProxy().load(attr)
    elif load_as == LoadElementAs.ID_DEF:
        return BpyIDProxy().load(attr, context)

    # assert issubclass(attr_type, T.bpy_struct) == issubclass(attr_type, T.bpy_struct)
    raise AssertionError("unexpected code path")
    # should be handled above
    if issubclass(attr_type, T.bpy_struct):
        return BpyStructProxy().load(attr)

    raise ValueError(f"Unsupported attribute type {attr_type} without bl_rna for attribute {attr} ")


class Proxy:
    def __eq__(self, other):
        if self.__class__ is not other.__class__:
            return False
        if len(self._data) != len(other._data):
            return False

        for k, v in self._data.items():
            if k not in other._data.keys():
                return False
            if v != other._data[k]:
                return False
        return True

    def save(self, bl_instance: any):
        """
        Save this proxy into a blender object
        """
        logging.warning(f"save : skipped {bl_instance}")


class StructLikeProxy(Proxy):
    """
    Holds a copy of a Blender bpy_struct
    """

    # TODO limit depth like in multiuser. Anyhow, there are circular references in f-curves
    def __init__(self):

        # We care for some non readonly properties. Collection object are tagged read_only byt can be updated with

        # Beware :

        # >>> bpy.types.Scene.bl_rna.properties['collection']
        # <bpy_struct, PointerProperty("collection")>

        # TODO is_readonly may be only interesting for "base types". FOr Collections it seems always set to true
        # meaning that the collection property slot cannot be updated although the object is mutable
        # TODO we also care for some readonly properties that are in fact links to data collections

        # The property information are taken from the containing class, not from the attribute.
        # So we get :
        #   T.Scene.bl_rna.properties['collection']
        #       <bpy_struct, PointerProperty("collection")>
        #   T.Scene.bl_rna.properties['collection'].fixed_type
        #       <bpy_struct, Struct("Collection")>
        # But if we take the information in the attribute we get information for the dereferenced
        # data
        #   D.scenes[0].collection.bl_rna
        #       <bpy_struct, Struct("Collection")>
        #
        # We need the former to make a difference between T.Scene.collection and T.Collection.children.
        # the former is a pointer
        self._data = {}
        pass

    def load(self, bl_instance: any, context: Context):
        """
        Load a Blender object into this proxy
        """
        self._data.clear()
        for name, bl_rna_property in context.properties(bl_instance):
            attr = getattr(bl_instance, name)
            attr_value = read_attribute(attr, bl_rna_property, bl_instance, context)
            if attr_value is not None:
                self._data[name] = attr_value
        return self

    def save(self, bl_instance: any):
        """
        Load a Blender object into this proxy
        """
        for k, v in self._data.items():
            write_attribute(k, v, bl_instance)


class BpyPropertyGroupProxy(StructLikeProxy):
    pass


class BpyStructProxy(StructLikeProxy):
    pass


class BpyIDProxy(BpyStructProxy):
    """
    Holds a copy of a Blender ID, i.e a type stored in bpy.data, like Object and Material
    """

    def __init__(self):
        super().__init__()

    def load(self, bl_instance, context: Context):
        # TODO check that bl_instance class derives from ID
        super().load(bl_instance, context)
        self.dccsync_uuid = bl_instance.dccsync_uuid
        return self


class BpyIDRefProxy(Proxy):
    """
    A reference to an item of bpy_prop_collection in bpy.data member
    """

    def __init__(self):
        pass

    def load(self, bl_instance):
        # Nothing to filter here, so we do not need the context/filter

        # Walk up to child of ID
        class_bl_rna = bl_instance.bl_rna
        while class_bl_rna.base is not None and class_bl_rna.base != bpy.types.ID.bl_rna:
            class_bl_rna = class_bl_rna.base

        # TODO for easier access could keep a ref to the BpyBlendProxy
        # TODO maybe this information does not belong to _data and _data should be reserved to "fields"
        self._data = (
            class_bl_rna.identifier,  # blenddata collection
            bl_instance.name_full,  # key in blenddata collection
        )
        return self


def ensure_uuid(item: bpy.types.ID):
    if item.get("dccsync_uuid") is None:
        item.dccsync_uuid = str(uuid4())


class BpyPropStructCollectionProxy(Proxy):
    """
    Proxy to a bpy_prop_collection of non-ID in bpy.data
    """

    def __init__(self):
        self._data: Mapping[Union[str, int], BpyIDProxy] = {}

    def load(self, bl_collection: bpy.types.bpy_prop_collection, context: Context):
        """
        in bl_collection : a bpy.types.bpy_prop_collection
        """
        # TODO If the key type is an int, load as a a list and check that no indices are missing
        for key, item in bl_collection.items():
            self._data[key] = BpyStructProxy().load(item, context)

        return self

    def save(self, bl_instance: any):
        """
        Load a Blender object into this proxy
        """
        for k, v in self._data.items():
            write_attribute(k, v, bl_instance)


# TODO derive from BpyIDProxy
class BpyPropDataCollectionProxy(Proxy):
    """
    Proxy to a bpy_prop_collection of ID in bpy.data. May not work as is for bpy_prop_collection on non-ID
    """

    def __init__(self):
        self._data: Mapping[str, BpyIDProxy] = {}

    def load_as_ID(self, bl_collection: bpy.types.bpy_prop_collection, context: Context):  # noqa N802
        """
        Load bl_collection elements as plain IDs, with all element properties. Use this lo load from bpy.data
        """
        for name, item in bl_collection.items():
            ensure_uuid(item)
            self._data[name] = BpyIDProxy().load(item, context)
        return self

    def load_as_IDref(self, bl_collection: bpy.types.bpy_prop_collection):  # noqa N802
        """
        Load bl_collection elements as referenced into bpy.data
        """
        for name, item in bl_collection.items():
            self._data[name] = BpyIDRefProxy().load(item)
        return self

    def update(self, diff):
        # TODO with context
        """
        Update the proxy according to the diff
        """
        for name, bl_collection in diff.items_added.items():
            item = bl_collection[name]
            self._data[name] = BpyIDProxy().load(item)
        for name in diff.items_removed:
            del self._data[name]
        for old_name, new_name in diff.items_renamed:
            self._data[new_name] = self._data[old_name]
            del self._data[old_name]
        for name, delta in diff.items_updated:
            self._data[name].update(delta)


class BpyBlendProxy(Proxy):
    def __init__(self, *args, **kwargs):
        self._data: Mapping[str, BpyPropDataCollectionProxy] = {}

    def load(self, context: Context):
        for name, _ in context.properties(bpy_type=T.BlendData):
            collection = getattr(bpy.data, name)
            self._data[name] = BpyPropDataCollectionProxy().load_as_ID(collection, context)
        return self

    def update(self, diff):
        for name in self.iter_all():
            deltas = diff.deltas.get(name)
            if deltas is not None:
                self._data[name].update(diff.deltas[name])


proxy_classes = [
    BpyIDProxy,
    BpyIDRefProxy,
    BpyStructProxy,
    BpyPropertyGroupProxy,
    BpyPropStructCollectionProxy,
    BpyPropDataCollectionProxy,
]


def write_attribute(key: Union[str, int], value: Any, bl_instance):
    """
    Load a property into a python object of the appropriate type, be it a Proxy or a native python object


    """
    type_ = type(value)
    if type_ not in proxy_classes:
        # TEMP we should not have readonly items
        assert type(key) is str
        if not bl_instance.bl_rna.properties[key].is_readonly:
            try:
                setattr(bl_instance, key, value)
            except Exception as e:
                logging.warning(f"write attribute skipped {key} for {bl_instance}...")
                logging.warning(f" ...Error: {repr(e)}")
        return
    else:
        if type(key) is int:
            # Collection with int key (vertices, points, ...)
            if len(bl_instance):
                attr = bl_instance[key]
            else:
                attr = None
        else:
            # Collection with a string key (T.BlendataObjects, T.ViewLayers)
            # or a mapping (T.bpy_struct)
            attr = getattr(bl_instance, key, None)

        if attr is not None:
            value.save(attr)
        else:
            logging.warning(f"write_attribute skipped attribute {key} for {bl_instance}")
        return
    raise NotImplementedError
    """
    # We have tested the types that are usefully reported by the python binding, now harder work.
    # These were implemented first and may be better implemented with the bl_rna property of the parent struct
    if attr_type == T.bpy_prop_array:
        return [e for e in attr]

    if attr_type == T.bpy_prop_collection:
        load_as = load_as_what(parent_struct, attr_property)
        if load_as == LoadElementAs.STRUCT:
            return BpyPropStructCollectionProxy().load(attr, context)
        elif load_as == LoadElementAs.ID_REF:
            # References into Blenddata collection, for instance D.scenes[0].objects
            return BpyPropDataCollectionProxy().load_as_IDref(attr)
        elif load_as == LoadElementAs.ID_DEF:
            # is  BlendData collection, for instance D.objects
            return BpyPropDataCollectionProxy().load_as_ID(attr, context)

    # TODO merge with previous case
    if isinstance(attr_property, T.CollectionProperty):
        return BpyPropStructCollectionProxy().load(attr, context)

    bl_rna = attr_property.bl_rna
    if bl_rna is None:
        logger.warning("Unimplemented attribute %s", attr)
        return None

    assert issubclass(attr_type, T.PropertyGroup) == issubclass(attr_type, T.PropertyGroup)
    if issubclass(attr_type, T.PropertyGroup):
        return BpyPropertyGroupProxy().load(attr, context)

    load_as = load_as_what(parent_struct, attr_property)
    if load_as == LoadElementAs.STRUCT:
        return BpyStructProxy().load(attr, context)
    elif load_as == LoadElementAs.ID_REF:
        return BpyIDRefProxy().load(attr)
    elif load_as == LoadElementAs.ID_DEF:
        return BpyIDProxy().load(attr, context)

    # assert issubclass(attr_type, T.bpy_struct) == issubclass(attr_type, T.bpy_struct)
    raise AssertionError("unexpected code path")
    # should be handled above
    if issubclass(attr_type, T.bpy_struct):
        return BpyStructProxy().load(attr)

    raise ValueError(f"Unsupported attribute type {attr_type} without bl_rna for attribute {attr} ")
    """
