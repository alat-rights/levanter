import equinox as eqx
import jax
import jax.numpy as jnp
import numpy as np
from equinox.custom_types import Array
from jax.interpreters import pxla
from jax.interpreters.pxla import PartitionSpec, Replicated, ShardedAxis
from utils import skip_if_not_enough_devices

import haliax as hax
from haliax import Axis, NamedArray
from levanter.axis_names import ResourceAxis, infer_resource_partitions, named_pjit_init


class MyModule(eqx.Module):
    named: NamedArray
    unnamed1: Array
    static_field: int = eqx.static_field()


dim1 = Axis("dim1", 8)
dim2 = Axis("dim2", 16)
dim3 = Axis("dim3", 32)

resource_map = {
    "dim2": ResourceAxis.DATA,
    "dim3": ResourceAxis.MODEL,
}


def test_infer_named_axes():
    mod = MyModule(named=hax.ones((dim1, dim2, dim3)), unnamed1=jnp.ones(dim2.size), static_field=1)

    axes: MyModule = infer_resource_partitions(mod, resource_map)

    assert axes.named.array == PartitionSpec(None, ResourceAxis.DATA, ResourceAxis.MODEL)
    assert axes.unnamed1 is None


class MyModuleInit(eqx.Module):
    named: NamedArray
    unnamed1: Array
    named2: NamedArray
    static_field: int = eqx.static_field()

    def __init__(self):
        self.named = hax.ones((dim2, dim3))
        self.unnamed1 = jnp.ones(())
        self.named2 = hax.ones(dim3)
        self.static_field = 1


@skip_if_not_enough_devices(4)
def test_pjit_class_init():
    devices = jax.devices()
    with pxla.Mesh(np.array(devices).reshape(-1, 2), (ResourceAxis.DATA, ResourceAxis.MODEL)):
        mod = named_pjit_init(MyModuleInit, axis_resources=resource_map)()

    assert mod.named.array.shape == (dim2.size, dim3.size)
    assert mod.named.array.sharding_spec.mesh_mapping == (
        ShardedAxis(0),
        ShardedAxis(1),
    )

    assert mod.unnamed1.shape == ()
    assert mod.unnamed1.sharding_spec.mesh_mapping == (
        Replicated(len(devices) // 2),
        Replicated(2),
    )
    assert mod.named2.array.shape == (dim3.size,)
    assert mod.named2.array.sharding_spec.mesh_mapping == (
        Replicated(len(devices) // 2),
        ShardedAxis(0),
    )


@skip_if_not_enough_devices(4)
def test_xmap_class_nested_init():
    class Mod2(eqx.Module):
        inner: MyModuleInit

        def __init__(self):
            self.inner = MyModuleInit()

    devices = jax.devices()
    with pxla.Mesh(np.array(devices).reshape(-1, 2), (ResourceAxis.DATA, ResourceAxis.MODEL)):
        mod2 = named_pjit_init(Mod2, axis_resources=resource_map)()

    mod = mod2.inner
    assert mod.named.array.shape == (dim2.size, dim3.size)
    assert mod.unnamed1.shape == ()
    assert mod.named2.array.shape == (dim3.size,)


def test_pjit_class_init_with_args():
    class ModWithArgs(eqx.Module):
        array: NamedArray
        array2: NamedArray

        def __init__(self, in_array: NamedArray):
            self.array = in_array
            self.array2 = hax.zeros(dim3)

    devices = jax.devices()
    with pxla.Mesh(np.array(devices).reshape(-1, 1), (ResourceAxis.DATA, ResourceAxis.MODEL)):
        mod = named_pjit_init(ModWithArgs, axis_resources=resource_map)(hax.ones((dim1, dim2)))
    assert isinstance(mod, ModWithArgs)
    assert mod.array.array.shape == (dim1.size, dim2.size)
    assert mod.array2.array.shape == (dim3.size,)
