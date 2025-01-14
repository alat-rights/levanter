import re
from dataclasses import fields
from typing import Any, Dict, List, Optional, TypeVar, Union, cast, overload

import equinox as eqx
import jax
import numpy
import numpy as np
import safetensors.numpy
from jax import numpy as jnp
from jax.experimental import multihost_utils
from jax.experimental.multihost_utils import sync_global_devices
from jaxtyping import PyTree

import haliax as hax
import haliax.nn as hnn
from haliax import NamedArray
from haliax.util import ensure_tuple

from levanter.utils.jax_utils import leaf_key_paths


StateDict = Dict[str, Any]
Tensor = Any


@overload
def apply_prefix(prefix: Optional[str], leaf: str) -> str:
    ...


@overload
def apply_prefix(prefix: Optional[str], leaf: None) -> Optional[str]:
    ...


@overload
def apply_prefix(prefix: None, leaf: Optional[str]) -> Optional[str]:
    ...


def apply_prefix(prefix: Optional[str], leaf: Optional[str]) -> Optional[str]:
    if prefix is None:
        return leaf
    elif leaf is None:
        return prefix
    else:
        return f"{prefix}.{leaf}"


Mod = TypeVar("Mod", bound=eqx.Module)


class StateDictSerializationMixin:
    """An eqx.Module that can be serialized to a torch-style state dict."""

    def to_state_dict(self, prefix: Optional[str] = None) -> StateDict:
        return jax_tree_to_state_dict(self, prefix)

    def from_state_dict(self: Mod, state_dict: StateDict, prefix: Optional[str] = None) -> Mod:
        return default_eqx_module_from_state_dict(self, state_dict, prefix)

    def update_state_dict(self, state_dict: StateDict, prefix: Optional[str] = None) -> StateDict:
        return default_update_state_dict_with_eqx_module(state_dict, self, prefix)

    def _state_dict_key_map(self) -> Dict[str, Optional[str]]:
        """Returns a dict mapping eqx.Module keys to torch keys that need to be renamed for serialization"""
        return {}


def jax_tree_from_state_dict(tree: PyTree, state_dict: StateDict, prefix: Optional[str] = None) -> PyTree:
    # TODO: assert compatibility of old and new values (type, shape, etc.)
    if isinstance(tree, eqx.Module):
        if hasattr(tree, "from_state_dict"):
            return tree.from_state_dict(state_dict, prefix)
        else:
            return default_eqx_module_from_state_dict(tree, state_dict, prefix)
    elif isinstance(tree, list):
        return [
            jax_tree_from_state_dict(item, state_dict, apply_prefix(prefix, str(i))) for i, item in enumerate(tree)
        ]
    elif isinstance(tree, dict):
        return {k: jax_tree_from_state_dict(v, state_dict, prefix=apply_prefix(prefix, k)) for k, v in tree.items()}
    elif isinstance(tree, NamedArray):
        # TODO: where's the best place to put this logic for NamedArrays
        if prefix is None:
            raise ValueError("Cannot extract a leaf value from a torch dict without a prefix")
        return NamedArray(jnp.array(state_dict[prefix]), axes=tree.axes)
    elif tree is None:
        if prefix is None:
            return None
        return state_dict.get(prefix, None)
    else:
        if prefix is None:
            raise ValueError("Cannot extract a leaf value from a state dict without a prefix")
        # TODO: add "strict" flag so we can return None in cases where it's just missing
        return jnp.array(state_dict[prefix])


def update_state_dict_with_jax_tree(tree: PyTree, state_dict: StateDict, prefix: Optional[str] = None) -> None:
    if isinstance(tree, eqx.Module):
        if hasattr(tree, "update_state_dict"):
            tree.update_state_dict(state_dict, prefix)
        else:
            default_update_state_dict_with_eqx_module(state_dict, tree, prefix)
    elif isinstance(tree, list):
        for i, item in enumerate(tree):
            update_state_dict_with_jax_tree(item, state_dict, prefix=apply_prefix(prefix, str(i)))
    elif isinstance(tree, dict):
        for k, v in tree.items():
            update_state_dict_with_jax_tree(v, state_dict, prefix=apply_prefix(prefix, k))
    elif isinstance(tree, NamedArray):
        # TODO: where's the best place to put this logic for NamedArrays
        assert prefix is not None
        state_dict[prefix] = tree.array
    else:
        if prefix is not None:
            if tree is not None:
                state_dict[prefix] = tree  # type: ignore
        else:
            raise ValueError("Cannot update torch dict with a leaf value.")


def jax_tree_to_state_dict(tree: PyTree, prefix: Optional[str] = None) -> StateDict:
    state_dict: StateDict = {}
    update_state_dict_with_jax_tree(tree, state_dict, prefix)
    return state_dict


def default_eqx_module_from_state_dict(mod: Mod, state_dict: StateDict, prefix: Optional[str] = None) -> Mod:
    key_map: Dict[str, Optional[str]] = getattr(mod, "_state_dict_key_map", lambda: {})()  # type: ignore
    names = []
    values = []
    for field in fields(mod):
        if field.metadata.get("static", False):
            continue
        key = key_map.get(field.name, field.name)
        value = getattr(mod, field.name)
        # TODO: might want to add a flag that allows missing keys?
        new = jax_tree_from_state_dict(value, state_dict, apply_prefix(prefix, key))
        names.append(field.name)
        values.append(new)
    return eqx.tree_at(lambda m: [getattr(m, name) for name in names], mod, values)


def default_eqx_module_to_state_dict(mod: eqx.Module, prefix: Optional[str] = None) -> StateDict:
    state_dict: StateDict = {}
    default_update_state_dict_with_eqx_module(state_dict, mod, prefix)
    return state_dict


def default_update_state_dict_with_eqx_module(
    state_dict: StateDict, mod: eqx.Module, prefix: Optional[str] = None
) -> StateDict:
    key_map: Dict[str, Optional[str]] = getattr(mod, "_state_dict_key_map", lambda: {})()  # type: ignore
    for field in fields(mod):
        if field.metadata.get("static", False):
            continue
        key = key_map.get(field.name, field.name)
        value = getattr(mod, field.name)
        update_state_dict_with_jax_tree(value, state_dict, apply_prefix(prefix, key))
    return state_dict


def flatten_linear_layers(prefix: Optional[str], tree: PyTree, out_dims_first_in_dict: Optional[bool]) -> StateDict:
    """
    In PyTorch, linear layers are stored as a 2d weight matrix and a 1d bias vector. In Haliax,
    linear layers can have arbitrary dimensions, grouped into input and output axes. This function
    flattens the linear layers in a state dict into a 2d weight matrix and a 1d bias vector.

    :param prefix: prefix to apply to the keys in the state dict
    :param tree:
    :param out_dims_first_in_dict: if True, the output dimensions will be the first axis in the flattened weight matrix.
    If False, the input dimensions will be the first axis. If None, the weight's axes will be left as-is.
    This is the default in PyTorch, but not in Haliax.
    """

    ret_dict: StateDict = {}

    def _flatten_linear(layer, prefix):
        if not isinstance(layer, hnn.Linear):
            return layer

        weight = layer.weight
        bias = layer.bias

        if weight.array is not None:
            weight = weight.flatten_axes(layer.Out, "__OUT__").flatten_axes(layer.In, "__IN__")
            if bias is not None:
                bias = bias.flatten_axes(layer.Out, "__OUT__")

            if out_dims_first_in_dict is True:
                weight = weight.rearrange((..., "__OUT__", "__IN__"))
            elif out_dims_first_in_dict is False:
                weight = weight.rearrange((..., "__IN__", "__OUT__"))
            else:
                pass

        ret_dict[apply_prefix(prefix, "weight")] = weight.array

        if bias is not None:
            ret_dict[apply_prefix(prefix, "bias")] = bias.array

        return ret_dict

    tree_prefixes = leaf_key_paths(tree, prefix, is_leaf=lambda x: isinstance(x, hnn.Linear), use_state_dict_keys=True)
    jax.tree_map(_flatten_linear, tree, tree_prefixes, is_leaf=lambda x: isinstance(x, hnn.Linear))
    return ret_dict


def unflatten_linear_layers(
    prefix, statedict: StateDict, layer: hnn.Linear, out_dims_first_in_dict: Optional[bool]
) -> StateDict:
    """
    In PyTorch, linear layers are stored as a 2d weight matrix and a 1d bias vector. In Haliax,
    linear layers can have arbitrary dimensions, grouped into input and output axes. This function
    unflattens the linear layers in a state dict into a 2d weight matrix and a 1d bias vector.

    :param prefix: prefix to apply to the keys in the state dict
    :param statedict: the state dict to source the flattened weights from
    :param layer: the exemplar layer to use for unflattening
    :param out_dims_first_in_dict: if True, the output dimensions will be the first axis in the flattened weight matrix.
    If False, the input dimensions will be the first axis. If None, the weight's axes will be inferred from the linear
    :return:
    """
    ret_dict: StateDict = {}

    def _unflatten_linear(layer, prefix):
        nonlocal out_dims_first_in_dict

        if not isinstance(layer, hnn.Linear):
            return layer

        weight = statedict[apply_prefix(prefix, "weight")]
        bias = statedict.get(apply_prefix(prefix, "bias"), None)

        Out = ensure_tuple(layer.Out)
        In = ensure_tuple(layer.In)
        InOut = In + Out
        extra_dims = tuple(ax for ax in layer.weight.axes if ax not in InOut)

        if out_dims_first_in_dict is None:
            out_dims_first_in_dict = layer.out_first

        if out_dims_first_in_dict:
            weight = hax.named(weight, hax.concat_axis_specs(extra_dims, ("__OUT__", "__IN__")))
        else:
            weight = hax.named(weight, hax.concat_axis_specs(extra_dims, ("__IN__", "__OUT__")))

        if layer.out_first:
            weight = weight.rearrange((..., "__OUT__", "__IN__"))
        else:
            weight = weight.rearrange((..., "__IN__", "__OUT__"))

        # now unflatten
        weight = weight.unflatten_axis("__OUT__", layer.Out).unflatten_axis("__IN__", layer.In)

        if bias is not None:
            bias = hax.named(bias, hax.concat_axis_specs(extra_dims, ("__OUT__",)))
            bias = bias.unflatten_axis("__OUT__", layer.Out)

        # tree_structure = jax.tree_structure(layer)
        # return jax.tree_unflatten(tree_structure, (weight, bias))

        ret_dict[apply_prefix(prefix, "weight")] = weight.array
        if bias is not None:
            ret_dict[apply_prefix(prefix, "bias")] = bias.array

        return ret_dict

    tree_prefixes = leaf_key_paths(
        layer, prefix, is_leaf=lambda x: isinstance(x, hnn.Linear), use_state_dict_keys=True
    )
    jax.tree_map(_unflatten_linear, layer, tree_prefixes, is_leaf=lambda x: isinstance(x, hnn.Linear))
    return ret_dict


def unstack_state_dict(state_dict: StateDict, prefix: Optional[str] = None) -> StateDict:
    """
    Unstack all keys matching prefix in a new state dict, returning a state dict that has all keys matching
    prefix unstacked, but otherwise the same.

    Unstacked in this case means roughly "compatible with a torch.nn.Sequential", which means that the
    keys are of the form "<prefix>.0.<key>", "<prefix>.1.<key>", etc.
    :param state_dict:
    :param prefix:
    :return:
    """
    new_dict: StateDict = {}
    prefix = apply_prefix(prefix, "")
    assert prefix is not None

    for k, v in state_dict.items():
        if k.startswith(prefix) and v is not None:
            for i, v_i in enumerate(v):
                new_dict[f"{prefix}{i}.{k[len(prefix):]}"] = v_i
        else:
            new_dict[k] = v

    return new_dict


def stack_state_dict(state_dict: StateDict, prefix: Optional[str] = None) -> StateDict:
    """
    Stack all keys matching prefix in a new state dict, returning a state dict that has all keys matching
    prefix stacked, but otherwise the same.

    Stacked in this case means roughly "compatible with a torch.nn.Sequential", which means that the
    keys are of the form "<prefix>.0.<key>", "<prefix>.1.<key>", etc.
    :param state_dict:
    :param prefix:
    :return:
    """
    vectorized_dict: StateDict = {}

    tensors_to_vectorize: Dict[str, List[Optional[Any]]] = {}
    escaped = re.escape(prefix or "")
    pattern = re.compile(rf"{escaped}\.(\d+)\.(.*)")

    for k, v in state_dict.items():
        match = pattern.match(k)
        if match:
            block_idx = int(match.group(1))
            block_key = match.group(2)
            tensors = tensors_to_vectorize.setdefault(block_key, [])
            if len(tensors) <= block_idx:
                tensors.extend([None] * (block_idx - len(tensors) + 1))
            assert tensors[block_idx] is None, f"Duplicate key {k}"
            tensors[block_idx] = v
        else:
            vectorized_dict[k] = v

    # now we have to vectorize the tensors
    for k, tensors in tensors_to_vectorize.items():
        vectorized_dict[cast(str, apply_prefix(prefix, k))] = numpy.stack(tensors, axis=0)

    return vectorized_dict


def to_numpy_state_dict(model, prefix: Optional[str] = None) -> StateDict:
    """
    Convert a model to a state dict by first creating desharded copies of all parameters that reside in CPU
    memory.

    This method is especially useful for saving models distributed across multiple hosts.
    """

    def get_to_cpu(arr: Union[jnp.ndarray, np.ndarray]):
        if isinstance(arr, np.ndarray):
            return arr
        elif "cpu" in arr.device().device_kind:
            return np.array(arr)
        elif arr.is_fully_addressable:
            r = np.array(arr)
            return r
        else:
            return np.array(jax.device_get(multihost_utils.process_allgather(arr, tiled=True)))

    # need to make sure the model is on *this machine* and *this machine's CPU* before saving
    model = jax.tree_map(lambda arr: get_to_cpu(arr), model)
    # TODO: it's be nice if safetensors supported an iterator or something so we could do the allgather one at a time
    state_dict = model.to_state_dict(prefix=prefix)
    return state_dict


_GLOBAL_SAVE_COUNT = 0


def save_state_dict(state_dict: StateDict, path):
    """
    Save a model's state dict to a file, bringing all tensors to the CPU first and then converting to numpy.
    This will save using safetensors format
    """
    state_dict = {k: v for k, v in state_dict.items() if v is not None}
    # now that we've moved the model to the CPU, we don't need to do this on all processes
    if jax.process_index() == 0:
        # the "pt" is a lie but it doesn't seem to actually matter and HF demands it
        safetensors.numpy.save_file(state_dict, path, metadata={"format": "pt"})
    global _GLOBAL_SAVE_COUNT
    sync_global_devices(f"local {_GLOBAL_SAVE_COUNT}")
    _GLOBAL_SAVE_COUNT += 1
