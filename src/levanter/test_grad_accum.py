import equinox as eqx
import jax
import jax.numpy as jnp
import pytest
from jax.sharding import Mesh

import haliax as hax
import haliax.nn as hnn

from levanter.grad_accum import accumulate_gradients_sharded


class Mlp(eqx.Module):
    """
    Simple 1 hidden layer MLP implementation
    """

    w_in: hax.NamedArray
    w_out: hax.NamedArray
    In: hax.Axis = eqx.static_field()
    Out: hax.Axis = eqx.static_field()
    Mid: hax.Axis = eqx.static_field()

    @staticmethod
    def init(In: hax.Axis, Out: hax.Axis, Mid: hax.Axis, *, key):
        w_in = hax.random.normal(key, hax.concat_axis_specs(In, Mid)) * 0.02
        w_out = hax.random.normal(key, hax.concat_axis_specs(Mid, Out)) * 0.02
        return Mlp(w_in, w_out, In, Out, Mid)

    def __call__(self, x):
        x = hax.dot(self.In, self.w_in, x)
        x = hnn.relu(x)
        x = hax.dot(self.Mid, self.w_out, x)
        return x


@pytest.mark.parametrize("parallelism", [1, 2, 4])
@pytest.mark.parametrize("accum_steps", [1, 3])
def test_accumulate_gradients_sharded(parallelism, accum_steps):
    In = hax.Axis("In", 32)
    Out = hax.Axis("Out", 32)
    Mid = hax.Axis("Mid", 32)
    Batch = hax.Axis("Batch", len(jax.devices()) * parallelism * accum_steps)
    mlp = Mlp.init(In, Out, Mid, key=jax.random.PRNGKey(0))

    def loss_fn(mlp, x):
        return mlp(x).mean().scalar()

    grad_fn = eqx.filter_value_and_grad(loss_fn)

    x = hax.random.normal(jax.random.PRNGKey(0), (Batch, In))

    x = jax.device_put(x, jax.sharding.PositionalSharding(jax.devices()).reshape((-1, 1)))

    axis_mapping = {"Batch": "data"}

    mesh = Mesh(jax.devices(), ("data",))

    @hax.partitioning.named_jit(axis_resources=axis_mapping)
    def jit_grad_accum(mlp, x):
        acc_v, acc_g = accumulate_gradients_sharded(
            grad_fn, Batch, mlp, x, per_device_parallelism=parallelism, parameter_axis_mapping=axis_mapping
        )
        return acc_v, acc_g

    with mesh:
        acc_v, acc_g = jit_grad_accum(mlp, x)
        v, g = grad_fn(mlp, x)

        assert jnp.allclose(acc_v, v)

        for l1, l2 in zip(jax.tree_leaves(acc_g), jax.tree_leaves(g)):
            assert jnp.allclose(l1, l2)
