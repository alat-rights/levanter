import abc
from typing import Generic, Optional, Type, TypeVar

import draccus
import equinox as eqx
from jax.random import PRNGKey

import haliax as hax
from haliax import Axis, NamedArray
from haliax.nn import cross_entropy_loss


LmConfigT = TypeVar("LmConfigT", bound="LmConfig")
LmT = TypeVar("LmT", bound="LmHeadModel")


class LmExample(eqx.Module):
    tokens: hax.NamedArray
    targets: hax.NamedArray
    attn_mask: hax.NamedArray
    loss_mask: hax.NamedArray


# TODO: for some reason, mypy doesn't like the discover_packages_path argument?
class LmConfig(draccus.PluginRegistry, abc.ABC, Generic[LmT], discover_packages_path="levanter.models"):  # type: ignore
    @property
    @abc.abstractmethod
    def model_type(cls) -> Type[LmT]:
        pass

    @property
    @abc.abstractmethod
    def KeyPos(self) -> Axis:
        pass

    @property
    @abc.abstractmethod
    def Pos(self) -> Axis:
        pass

    def build(self, Vocab: Axis, *, key: PRNGKey) -> "LmT":
        return self.model_type.init(Vocab, self, key=key)  # type: ignore


class LmHeadModel(Generic[LmConfigT], abc.ABC):
    """
    Superclass for models with a language modeling head.
    """

    @property
    @abc.abstractmethod
    def config(self) -> LmConfigT:
        pass

    @property
    @abc.abstractmethod
    def Vocab(self) -> Axis:
        pass

    @property
    @abc.abstractmethod
    def Pos(self) -> Axis:
        pass

    @classmethod
    @abc.abstractmethod
    def init(cls, Vocab: Axis, config: LmConfigT, *, key: PRNGKey) -> "LmHeadModel[LmConfigT]":
        pass

    @abc.abstractmethod
    def __call__(
        self, input_ids: NamedArray, attn_mask: Optional[NamedArray] = None, *, inference: bool, key=None
    ) -> NamedArray:
        pass

    def compute_loss(
        self,
        example: LmExample,
        *,
        inference: bool,
        key=None,
        reduction: Optional[hax.ReductionFunction] = hax.mean,
        reduction_axis: Optional[hax.AxisSelection] = None,
    ) -> NamedArray:
        """
        Computes the cross-entropy loss for a language modeling example. If reduction is not None, the loss is reduced
        across the reduction axis (with reduction_axis=None meaning all axes). If reduction is None, the loss is not
        reduced, and the result is a named array with axes (*batch axes, sequence_length).
        """
        logits = self(example.tokens, example.attn_mask, inference=inference, key=key)
        target_y = hax.nn.one_hot(example.targets, self.Vocab, dtype=logits.dtype)
        return cross_entropy_loss(
            logits, self.Vocab, target_y, reduction, reduction_axis=reduction_axis, where=example.loss_mask
        )
