from abc import ABC, abstractmethod
from copy import deepcopy
import fnmatch
from functools import reduce
import functools
import math
import os
from typing import List, Optional, Tuple, Union, Callable, Type
from inspect import signature
import random

import z3
import torch

from nnsmith.error import SanityCheck, ConstraintCheck
from nnsmith.abstract.loss_func import *
from nnsmith.abstract.proxy_grad import *
from nnsmith.abstract.dtype import (
    DType,
    DTYPE_ALL,
    DTYPE_NON_BOOLS,
    DTYPE_FLOATS,
    DTYPE_INTS,
)
from nnsmith.abstract.arith import *
from nnsmith.abstract.tensor import AbsTensor

# Recommended resources: https://theory.stanford.edu/~nikolaj/programmingz3.html
# Another plausible tool (Interval Analysis): https://simon-rohou.fr/research/tubex-lib/doc/toctree.html
# Please follow the PyTorch API conventions: https://pytorch.org/docs/stable/nn.html

# There are following types of constraints at this point:
# 1. Shape variables must be greater than 0;
# 2. Shape variables must avoid devision by 0;
# 3. Intra-input shape constraints; e.g., add(x, y) where x.shape() must be equal to y.shape();
# 4. Extra constraints introduced by individual operators;

_DEV = torch.device("cpu")
FLOPS_LIM = os.getenv("NNSMITH_FLOPS_LIM", "auto")
if FLOPS_LIM == "auto":  # use predefined value
    FLOPS_LIM = 2**30
elif FLOPS_LIM == "off":
    FLOPS_LIM = None
else:
    FLOPS_LIM = float(FLOPS_LIM)

# control wheter to model FLOPS in z3 too. If not, we will check it after model is concretized.
Z3_CONS_FLOPS = os.getenv("NNSMITH_Z3_CONS_FLOPS", "on")
assert Z3_CONS_FLOPS in [
    "on",
    "off",
], "NNSMITH_Z3_CONS_FLOPS must be either 'on' or 'off'"
Z3_CONS_FLOPS = Z3_CONS_FLOPS == "on"


__MIN_RANK__ = 0
__MAX_RANK__ = 5


ALL_OP_TYPES = []


def leaf(cls):
    if cls not in ALL_OP_TYPES:
        ALL_OP_TYPES.append(cls)
    return cls


def int_from(start):
    return tuple(range(start, __MAX_RANK__ + 1))


def int_range(start, end):
    return tuple(range(start, end + 1))


def int_until(end):
    return tuple(range(__MIN_RANK__, end + 1))


def int_all():
    return tuple(range(__MIN_RANK__, __MAX_RANK__ + 1))


def check_shape_fn(func):
    def wrapper_check_shape_fn(self, input_shapes):
        SanityCheck.true(
            self.out_ranks,
            "Empty output dimensions in {}".format(self.__class__.__name__),
        )
        SanityCheck.eq(
            len(input_shapes),
            len(self.inp_ranks),
            "{} requires {} inputs, but got {}".format(
                self.__class__.__name__, len(self.inp_ranks), len(input_shapes)
            ),
        )
        res = func(self, [s.deepcopy() for s in input_shapes])
        SanityCheck.eq(
            len(res),
            len(self.out_ranks),
            "{} requires {} outputs, but got {}".format(
                self.__class__.__name__, len(self.out_ranks), len(res)
            ),
        )
        return res

    return wrapper_check_shape_fn


def check_require_fn(func):
    def wrapper_check_require_fn(self, input_shapes: List[AbsTensor]):
        SanityCheck.eq(
            len(input_shapes),
            len(self.inp_ranks),
            "{} requires {} inputs, but got {}".format(
                self.__class__.__name__, len(self.inp_ranks), len(input_shapes)
            ),
        )
        return func(self, [s.deepcopy() for s in input_shapes])

    return wrapper_check_require_fn


def _prepend_to(x, max_dim):
    return [1 for i in range(max_dim - len(x))] + x


def z3_bcast(
    x: Union[int, z3.ExprRef], y: Union[int, z3.ExprRef], *args: Union[int, z3.ExprRef]
):
    x, y = align_bvs(x, y)
    return (
        z3.simplify(z3.If(nnsmith_eq(y, 1), x, y))
        if len(args) == 0
        else z3_bcast(z3_bcast(x, y), *args)
    )


def broadcast_shapes(
    *shapes: List[Union[z3.ExprRef, int]]
) -> List[Union[z3.ExprRef, int]]:
    """this function does not check the validity of broadcast. Please always pair it with broadcast_cons"""
    SanityCheck.gt(len(shapes), 0)
    if len(shapes) == 1:
        return shapes[0]
    max_dim = max(map(lambda x: len(x), shapes))
    max_shape = [None] * (max_dim)
    for j in range(max_dim):
        i = -j - 1
        args_dim_sz = [_prepend_to(x, max_dim)[i] for x in shapes]
        if any(isinstance(s, z3.ExprRef) for s in args_dim_sz):
            max_shape[i] = z3.simplify(z3_bcast(*args_dim_sz))
        else:
            max_shape[i] = max(*args_dim_sz)
    return max_shape


def broadcast_cons(*shapes: List[Union[z3.ExprRef, int]]) -> List[z3.ExprRef]:
    tgt_shape = broadcast_shapes(*shapes)
    cons = []
    max_dim = len(tgt_shape)
    for j in range(max_dim):
        i = -j - 1
        if isinstance(tgt_shape[i], z3.ExprRef):
            axis_cons = []
            for x in shapes:
                if len(x) > j:
                    axis_cons.append(
                        z3.Or(nnsmith_eq(x[i], tgt_shape[i]), nnsmith_eq(x[i], 1))
                    )
            axis_cons = z3.simplify(z3.And(*axis_cons))
            cons.append(axis_cons)
        else:
            args_dim_sz = [_prepend_to(x, max_dim)[i] for x in shapes]
            valid = all(
                nnsmith_eq(s, tgt_shape[i]) or nnsmith_eq(s, 1) for s in args_dim_sz
            )
            # TODO(JK): enable this after fixing issue #2
            # assert valid, "Invalid broadcast shapes {}. Specific dim sizes: {}".format(shapes, args_dim_sz)
            cons.append(z3.BoolVal(valid))
    return cons


def broadcast_cons_binary(*shapes: List[Union[z3.ExprRef, int]]) -> List[z3.ExprRef]:
    SanityCheck.eq(len(shapes), 2)
    tgt_shape = broadcast_shapes(*shapes)
    cons = []
    max_dim = len(tgt_shape)
    lhs, rhs = shapes
    lhs = _prepend_to(lhs, max_dim)
    rhs = _prepend_to(rhs, max_dim)
    for j in range(max_dim):
        i = -j - 1
        if isinstance(tgt_shape[i], z3.ExprRef):
            cons.append(
                z3.simplify(
                    z3.Or(
                        nnsmith_eq(lhs[i], 1),
                        nnsmith_eq(rhs[i], 1),
                        nnsmith_eq(lhs[i], rhs[i]),
                    )
                )
            )
        else:
            valid = (
                nnsmith_eq(lhs[i], 1)
                or nnsmith_eq(rhs[i], 1)
                or nnsmith_eq(lhs[i], rhs[i])
            )
            # TODO(JK): enable this after fixing issue #2
            # assert valid, "Invalid broadcast shapes lhs={}, rhs={}".format(lhs, rhs)
            cons.append(z3.BoolVal(valid))
    return cons


def broadcast_to_cons(*shapes: List[Union[z3.ExprRef, int]]) -> List[z3.ExprRef]:
    """Unidirectional broadcast. Last input is the target shape.

    Examples of valid unidirectional broadcast:
    [1, 2, 3] -> [0, 1, 2, 3]
    [1] -> [3]

    Examples of invalid unidirectional broadcast:
    [0, 1, 2, 3] -> [1, 2, 3]
    [3] -> [1]

    Logic: for each dim: src_dim == tgt_dim or src_dim == 1
    """
    srcs, tgt = shapes[:-1], shapes[-1]
    cons = []
    max_dim = len(tgt)
    for src in srcs:
        ConstraintCheck.true(len(src) <= max_dim)
        src = _prepend_to(src, max_dim)
        for i in range(max_dim):
            if isinstance(tgt[i], z3.ExprRef) or isinstance(src[i], z3.ExprRef):
                cons.append(
                    z3.simplify(
                        z3.Or(nnsmith_eq(src[i], 1), nnsmith_eq(src[i], tgt[i]))
                    )
                )
            else:
                valid = nnsmith_eq(src[i], 1) or nnsmith_eq(src[i], tgt[i])
                # TODO(JK): enable this after fixing issue #2
                # assert valid, "Invalid broadcast shapes lhs={}, rhs={}".format(lhs, rhs)
                cons.append(z3.BoolVal(valid))
    return cons


class AbsOpBase(ABC):
    # number of parameters; None means it's fixed that can be inferred through `signature`.
    num_var_param = None
    # whether this op is broadcastable or not
    bcastable = False
    # input dtypes: enumerates all possible input dtype combinations. Size of the list is the number of combinations.
    # Each element is a tuple of allowed input dtypes. NOTE: len(list) can >= the # of inputs, for handling ops with arbitrary arity.
    # For example, [(DType.float32, DType.float32), (DType.float64, DType.float64), (DType.int32, DType.int32)] means that
    # this op can accept one of float32xfloat32, float64xfloat64, and int32xint32 as input dtypes.
    in_dtypes: List[Tuple[DType, ...]] = None  # Overwrite me!
    out_dtypes: List[Tuple[DType, ...]] = None
    # whether to disable the op during graph generation
    _skip = False

    def __init__(self):
        # `[3, 3]` this means this op requires 2 inputs. Where the 1st one has 2 dimensions, and the 2nd one has 3 dimensions.
        # `-1` means arbitrary dimantions; NOTE: but should be concretized during execution.
        # All symbols of correponding operator must be the constructor's parameters.
        # [ <inp0>(support_dim0, support_dim1, ...), <inp1>(...), ... ]
        self.inp_ranks = []
        # NOTE: the concrete values of out_ranks are not useful. Just make sure the length is correct.
        # NOTE: the output shape of input dimensions should be concretized during the execution.
        self.out_ranks = []
        # Require the input dimension sizes to be equivalent.
        self.same_inp_dims = False
        # NOTE: the input of operator constructors are all Union[int, z3.ExprRef].
        self.extra_attrs = {}

    @classmethod
    def get_num_var_param(cls):
        if cls.num_var_param is None:
            return len(signature(cls.__init__).parameters) - 1
        return random.choice(cls.num_var_param)

    @abstractmethod  # Overload me!
    # Exception means rejection.
    def _type_transfer(self, input_shapes: List[AbsTensor]) -> List[AbsTensor]:
        raise NotImplementedError

    @check_shape_fn  # Public API.
    def checked_type_transfer(self, input_shapes: List[AbsTensor]) -> List[AbsTensor]:
        self.last_outs = self._type_transfer(input_shapes)
        return self.last_outs

    # Overload me!
    # Extra constraints for the input tensors.
    # Exception means rejection.
    def _requires(self, input_shapes: List[AbsTensor]) -> List[z3.ExprRef]:
        return []

    @abstractmethod
    def torch(self) -> Callable[..., torch.Tensor]:
        raise NotImplementedError

    def deduct_inp_ranks_and_dtype(
        self, out_abs_tensor: List[AbsTensor]
    ) -> List[Tuple[int, DType]]:
        raise NotImplementedError

    @check_require_fn  # Public API.
    def checked_requires(self, input_shapes):
        return self._requires(input_shapes)

    def n_floats(self, input_shapes: List[AbsTensor]) -> z3.ExprRef:
        return reduce(nnsmith_add, [i.nelement() for i in self.last_outs])

    def flops(self, input_shapes):
        return 0

    def __repr__(self) -> str:
        return self.__class__.__name__

    @classmethod
    def numeric_valid(cls, outputs) -> bool:
        with torch.no_grad():
            return not any(
                [torch.isnan(out).any() or torch.isinf(out).any() for out in outputs]
            )

    @classmethod
    def numeric_unstable(cls, outputs) -> bool:
        return not cls.numeric_valid(outputs)


def concretize(op: AbsOpBase, model: Optional[z3.ModelRef]) -> AbsOpBase:
    if isinstance(op, Constant) or isinstance(op, Input):
        assert not hasattr(op, "torch_loss")
        ret_op = deepcopy(op)
        values = []

        for idx, s in enumerate(op.abs_tensor.shape):
            if isinstance(s, z3.ExprRef):
                ret_op.abs_tensor.shape[idx] = model.eval(s).as_long()

        return ret_op

    # Non-inp / const types.
    construct_param_dict = signature(op.__init__).parameters
    values = []
    symbolic_idx = []

    if op.num_var_param is not None:
        # input is a variable list.
        key = list(construct_param_dict.keys())[0]
        values = list(getattr(op, key))
        symbolic_idx = [
            i for i in range(len(values)) if isinstance(values[i], z3.ExprRef)
        ]
    else:
        for idx, key in enumerate(construct_param_dict):
            param = getattr(op, key)
            values.append(param)
            if isinstance(param, z3.ExprRef):
                symbolic_idx.append(idx)

    for idx in symbolic_idx:
        values[idx] = model.eval(values[idx]).as_long()

    concrete_op = globals()[op.__class__.__name__](*values)
    concrete_op.inp_ranks = op.inp_ranks
    concrete_op.out_ranks = op.out_ranks
    concrete_op.same_inp_dims = op.same_inp_dims
    concrete_op.extra_attrs = op.extra_attrs

    return concrete_op


class UnaryOpBase(AbsOpBase):
    in_dtypes = [(i,) for i in DTYPE_FLOATS]
    out_dtypes = [(i,) for i in DTYPE_FLOATS]

    def __init__(self):
        super().__init__()
        self.out_ranks = [int_all()]


class BinaryOpBase(AbsOpBase):
    in_dtypes = [(i, i) for i in DTYPE_FLOATS]
    out_dtypes = [(i,) for i in DTYPE_FLOATS]

    def __init__(self):
        super().__init__()
        self.out_ranks = [int_all()]


class TernaryOpBase(AbsOpBase):
    in_dtypes = [(i, i, i) for i in DTYPE_FLOATS]
    out_dtypes = [(i,) for i in DTYPE_FLOATS]

    def __init__(self):
        super().__init__()
        self.out_ranks = [int_all()]


class ElementWiseUnaryOp(UnaryOpBase):
    def __init__(self):
        super().__init__()
        self.inp_ranks = [int_all()]
        self.out_ranks = [int_all()]

    def _type_transfer(self, input_shapes: List[AbsTensor]) -> List[AbsTensor]:
        SanityCheck.eq(len(input_shapes), 1)
        return [input_shapes[0]]

    def deduct_inp_ranks_and_dtype(
        self, out_abs_tensor: List[AbsTensor]
    ) -> List[Tuple[int, DType]]:
        return [
            (out_abs_tensor[0].ndims, out_abs_tensor[0].dtype),
        ]


def bcast_rand_ndims(num_svars, target_ndims):
    res = [random.randint(0, target_ndims) for _ in range(num_svars)]
    res[random.randint(0, num_svars - 1)] = target_ndims
    return res


class BcastBinaryOp(BinaryOpBase):
    bcastable = True
    # by default, output dtype is the same as the first input dtype
    _bcast_out_dtypes = None

    def __init__(self):
        super().__init__()
        self.inp_ranks = [int_all(), int_all()]
        self.same_inp_dims = False
        self.bcastable = True

    def _type_transfer(self, input_shapes: List[AbsTensor]) -> List[AbsTensor]:
        tgt_shape = broadcast_shapes(*(ish.shape for ish in input_shapes))
        dtype = (
            input_shapes[0].dtype
            if self._bcast_out_dtypes is None
            else self._bcast_out_dtypes[0]
        )
        return [AbsTensor(tgt_shape, dtype)]

    def _requires(self, input_shapes):
        return broadcast_cons_binary(*(ish.shape for ish in input_shapes))

    def deduct_inp_ranks_and_dtype(
        self, out_abs_tensor: List[AbsTensor]
    ) -> List[Tuple[int, DType]]:
        x, y = bcast_rand_ndims(2, out_abs_tensor[0].ndims)
        return [
            (x, out_abs_tensor[0].dtype),
            (y, out_abs_tensor[0].dtype),
        ]


class BcastBinaryOp1(BcastBinaryOp):  # +-*/ max min
    in_dtypes = [(i, i) for i in DTYPE_NON_BOOLS]
    out_dtypes = [(i,) for i in DTYPE_NON_BOOLS]
    _bcast_out_dtypes = None


class Comparator(BcastBinaryOp):  # > < =
    in_dtypes = [(i, i) for i in DTYPE_ALL]
    out_dtypes = [(DType.bool,)]
    _bcast_out_dtypes = [DType.bool]

    def deduct_inp_ranks_and_dtype(
        self, out_abs_tensor: List[AbsTensor]
    ) -> List[Tuple[int, DType]]:
        x, y = bcast_rand_ndims(2, out_abs_tensor[0].ndims)
        in_dtypes = random.choice(self.in_dtypes)
        return [
            (x, in_dtypes[0]),
            (y, in_dtypes[1]),
        ]


class Logical(BcastBinaryOp):  # logical and or xor
    in_dtypes = [(DType.bool, DType.bool)]
    out_dtypes = [(DType.bool,)]
    _bcast_out_dtypes = [DType.bool]


@leaf
class Where(TernaryOpBase):
    bcastable = True
    in_dtypes = [(DType.bool, i, i) for i in DTYPE_NON_BOOLS]
    out_dtypes = [(i,) for i in DTYPE_NON_BOOLS]

    def __init__(self):
        super().__init__()
        self.inp_ranks = [int_all(), int_all(), int_all()]
        self.same_inp_dims = False
        self.same_inp_dtypes = True
        self.bcastable = True

    def _type_transfer(self, input_shapes: List[AbsTensor]) -> List[AbsTensor]:
        # assert len(input_shapes[0].shape) == len(input_shapes[1].shape)
        tgt_shape = broadcast_shapes(*(ish.shape for ish in input_shapes))
        dtype = input_shapes[1].dtype
        return [AbsTensor(tgt_shape, dtype)]

    def _requires(self, input_shapes):
        return broadcast_cons(*(ish.shape for ish in input_shapes)) + [
            z3.BoolVal(input_shapes[1].dtype == input_shapes[2].dtype)
        ]

    def torch(self):
        return torch.where

    def deduct_inp_ranks_and_dtype(
        self, out_abs_tensor: List[AbsTensor]
    ) -> List[Tuple[int, DType]]:
        x, y, z = bcast_rand_ndims(3, out_abs_tensor[0].ndims)
        return [
            (x, DType.bool),
            (y, out_abs_tensor[0].dtype),
            (z, out_abs_tensor[0].dtype),
        ]


# bcast binary ops from https://github.com/onnx/onnx/blob/master/docs/Broadcasting.md
# TODO bitwise_and/or/xor?
Add = leaf(
    type(
        "Add",
        (BcastBinaryOp1,),
        {"torch": lambda self: torch.add, "__module__": __name__},
    )
)
Sub = leaf(
    type(
        "Sub",
        (BcastBinaryOp1,),
        {"torch": lambda self: torch.sub, "__module__": __name__},
    )
)
Mul = leaf(
    type(
        "Mul",
        (BcastBinaryOp1,),
        {"torch": lambda self: torch.mul, "__module__": __name__},
    )
)
# NOTE(JK): didn't find multi-input version of Max and Min in torch, so assume binary ops
Max = leaf(
    type(
        "Max",
        (BcastBinaryOp1,),
        {"torch": lambda self: torch.max, "__module__": __name__},
    )
)
Min = leaf(
    type(
        "Min",
        (BcastBinaryOp1,),
        {"torch": lambda self: torch.min, "__module__": __name__},
    )
)

Equal = leaf(
    type(
        "Equal", (Comparator,), {"torch": lambda self: torch.eq, "__module__": __name__}
    )
)
Greater = leaf(
    type(
        "Greater",
        (Comparator,),
        {"torch": lambda self: torch.gt, "__module__": __name__},
    )
)
Less = leaf(
    type(
        "Less", (Comparator,), {"torch": lambda self: torch.lt, "__module__": __name__}
    )
)

And = leaf(
    type(
        "And",
        (Logical,),
        {"torch": lambda self: torch.logical_and, "__module__": __name__},
    )
)
Or = leaf(
    type(
        "Or",
        (Logical,),
        {"torch": lambda self: torch.logical_or, "__module__": __name__},
    )
)
Xor = leaf(
    type(
        "Xor",
        (Logical,),
        {"torch": lambda self: torch.logical_xor, "__module__": __name__},
    )
)

# TODO: support exactly what onnx spec says (e.g., int support in the rhs)
# lhs_dtypes = (DType.int32, DType.int64, DType.float32, DType.float64)
# rhs_dtypes = (DType.int32, DType.int64, DType.float32, DType.float64)
# Pow.in_dtypes = itertools.product(lhs_dtypes, rhs_dtypes)


class StopFoldConst(torch.nn.Module):
    def __init__(self, data: torch.Tensor):
        super().__init__()
        self.dtype = data.dtype
        self.param = torch.nn.parameter.Parameter(
            data, requires_grad=data.is_floating_point()
        )

    @torch.no_grad()
    def forward(self):
        return self.param.to(dtype=self.dtype, device=_DEV)


class Input(AbsOpBase):
    in_dtypes = [()]
    out_dtypes = [(i,) for i in DTYPE_ALL]

    def __init__(self, dim: int):
        super().__init__()
        self.inp_ranks = []
        self.out_ranks = [(dim,)]

    def _type_transfer(self, input_shapes: List[AbsTensor]) -> List[AbsTensor]:
        SanityCheck.eq(len(input_shapes), 0)
        return [self.abs_tensor]

    def _requires(self, input_shapes: List[AbsTensor]) -> List[z3.ExprRef]:
        SanityCheck.eq(len(input_shapes), 0)
        return []

    def torch(self) -> Callable[..., torch.Tensor]:
        raise NotImplementedError()


class Constant(AbsOpBase):
    in_dtypes = [()]
    out_dtypes = [(i,) for i in DTYPE_ALL]

    def __str__(self) -> str:
        return super().__str__() + " " + str(self.extra_attrs)

    def __init__(self, dim: int):
        super().__init__()
        self.dim = dim
        self.inp_ranks = []
        self.out_ranks = [(dim,)]
        self.abs_tensor = None

    def _type_transfer(self, input_shapes: List[AbsTensor]) -> List[AbsTensor]:
        SanityCheck.eq(len(input_shapes), 0)
        return [self.abs_tensor]

    def _requires(self, input_shapes: List[AbsTensor]) -> List[z3.ExprRef]:
        SanityCheck.eq(len(input_shapes), 0)
        return []

    def torch(self) -> Callable[..., torch.Tensor]:
        data = torch.randn(self.abs_tensor.shape, device=_DEV).to(
            self.abs_tensor.dtype.torch()
        )
        return StopFoldConst(data)


class Placeholder:
    def __init__(self, out_shape: AbsTensor):
        self.out_shape = out_shape
        self.inp_ranks = []
        self.out_ranks = [(out_shape.ndims,)]

    def __repr__(self):
        return f"Placeholder({self.out_shape})"

    def to_const(self):
        const_node = Constant(self.out_shape.ndims)
        const_node.abs_tensor = self.out_shape
        return const_node

    def to_input(self):
        input_node = Input(self.out_shape.ndims)
        input_node.abs_tensor = self.out_shape
        return input_node


class LegacyConstant0D(Constant):
    def __init__(self):
        super().__init__(0)
        # TODO more dtypes

    @property
    def abs_tensor(self):
        return AbsTensor([], dtype=self.extra_attrs["dtype"])


class LegacyConstant1D(Constant):
    def __init__(self, dim0: Union[int, z3.ExprRef]):
        super().__init__(1)
        self.dim0 = dim0

    @property
    def abs_tensor(self):
        return AbsTensor([self.dim0], dtype=self.extra_attrs["dtype"])


class LegacyConstant2D(Constant):
    def __init__(self, dim0: Union[int, z3.ExprRef], dim1: Union[int, z3.ExprRef]):
        super().__init__(2)
        self.dim0 = dim0
        self.dim1 = dim1

    @property
    def abs_tensor(self):
        return AbsTensor([self.dim0, self.dim1], dtype=self.extra_attrs["dtype"])


class LegacyConstant3D(Constant):
    def __init__(
        self,
        dim0: Union[int, z3.ExprRef],
        dim1: Union[int, z3.ExprRef],
        dim2: Union[int, z3.ExprRef],
    ):
        super().__init__(3)
        self.dim0 = dim0
        self.dim1 = dim1
        self.dim2 = dim2

    @property
    def abs_tensor(self):
        return AbsTensor(
            [self.dim0, self.dim1, self.dim2], dtype=self.extra_attrs["dtype"]
        )


class LegacyConstant4D(Constant):
    def __init__(
        self,
        dim0: Union[int, z3.ExprRef],
        dim1: Union[int, z3.ExprRef],
        dim2: Union[int, z3.ExprRef],
        dim3: Union[int, z3.ExprRef],
    ):
        super().__init__(4)
        self.dim0 = dim0
        self.dim1 = dim1
        self.dim2 = dim2
        self.dim3 = dim3

    @property
    def abs_tensor(self):
        return AbsTensor(
            [self.dim0, self.dim1, self.dim2, self.dim3],
            dtype=self.extra_attrs["dtype"],
        )


# FIXME: Div will cause fuzzing crash.
Div = leaf(
    type(
        "Div",
        (BcastBinaryOp1,),
        {
            "torch": (
                lambda self: lambda x, y: torch.div(
                    x,
                    y,
                    rounding_mode="floor"
                    if DType.from_torch(x.dtype) in DTYPE_INTS
                    else None,
                )
            ),
            "torch_loss": lambda self, x, y: loss_gt_zero(torch.abs(y)),
            "__module__": __name__,
        },
    )
)


@leaf
class Pow(BcastBinaryOp):
    in_dtypes = [(i, i) for i in DTYPE_FLOATS]
    out_dtypes = [(i,) for i in DTYPE_FLOATS]

    def torch(self):
        return torch.pow

    def torch_loss(self, a, b):
        # a >= 0 && b*log(a) <= 20
        l0 = loss_gt_zero(a)
        if torch.any(l0 > 0):
            return ("l0", l0)
        l1 = loss_le(
            b * torch.log(torch.maximum(a, torch.tensor(1e-40, dtype=a.dtype))), 40
        )
        return ("l1", l1)

    @classmethod
    def numeric_unstable(cls, outputs) -> bool:
        with torch.no_grad():
            return any(
                [
                    torch.isnan(out).any()
                    or torch.isinf(out).any()
                    or torch.any(out > math.exp(40))
                    for out in outputs
                ]
            )


@leaf
class GELU(ElementWiseUnaryOp):
    in_dtypes = [(i,) for i in DTYPE_FLOATS]
    out_dtypes = [(i,) for i in DTYPE_FLOATS]

    def torch(self):
        return torch.nn.GELU()


@leaf
class LeakyReLU(ElementWiseUnaryOp):
    in_dtypes = [(i,) for i in DTYPE_FLOATS]
    out_dtypes = [(i,) for i in DTYPE_FLOATS]

    def __init__(self):
        """See https://pytorch.org/docs/stable/generated/torch.nn.LeakyReLU.html"""
        super().__init__()
        self.negative_slope = 0.01

    def torch(self):
        return torch.nn.LeakyReLU(self.negative_slope)


@leaf
class PReLU(ElementWiseUnaryOp):
    in_dtypes = [(DType.float32,)]
    out_dtypes = [(DType.float32,)]

    def torch(self):
        return torch.nn.PReLU(device=_DEV)


@leaf
class Sigmoid(ElementWiseUnaryOp):
    in_dtypes = [(i,) for i in DTYPE_FLOATS]
    out_dtypes = [(i,) for i in DTYPE_FLOATS]

    def torch(self):
        return torch.sigmoid


class TrigonometricOp(ElementWiseUnaryOp):
    pass


@leaf
class Sin(TrigonometricOp):
    in_dtypes = [(i,) for i in DTYPE_FLOATS]
    out_dtypes = [(i,) for i in DTYPE_FLOATS]

    def torch(self):
        return torch.sin


@leaf
class Cos(TrigonometricOp):
    in_dtypes = [(i,) for i in DTYPE_FLOATS]
    out_dtypes = [(i,) for i in DTYPE_FLOATS]

    def torch(self):
        return torch.cos


@leaf
class Asin(TrigonometricOp):
    in_dtypes = [(i,) for i in DTYPE_FLOATS]
    out_dtypes = [(i,) for i in DTYPE_FLOATS]

    def torch(self):
        return torch.asin

    def torch_loss(self, x):
        return loss_le(x.abs(), 1)


@leaf
class Acos(TrigonometricOp):
    in_dtypes = [(i,) for i in DTYPE_FLOATS]
    out_dtypes = [(i,) for i in DTYPE_FLOATS]

    def torch(self):
        return torch.acos

    def torch_loss(self, x):
        return loss_le(x.abs(), 1)


@leaf
class Tan(TrigonometricOp):
    in_dtypes = [(i,) for i in DTYPE_FLOATS]
    out_dtypes = [(i,) for i in DTYPE_FLOATS]

    def torch(self):
        return torch.tan


@leaf
class Atan(TrigonometricOp):
    in_dtypes = [(i,) for i in DTYPE_FLOATS]
    out_dtypes = [(i,) for i in DTYPE_FLOATS]

    def torch(self):
        return torch.atan


@leaf
class Abs(ElementWiseUnaryOp):
    in_dtypes = [(i,) for i in DTYPE_NON_BOOLS]

    def torch(self):
        return torch.abs


@leaf
class ReLU(ElementWiseUnaryOp):
    in_dtypes = [(i,) for i in DTYPE_FLOATS]
    out_dtypes = [(i,) for i in DTYPE_FLOATS]

    def torch(self):
        return torch.nn.ReLU()

    def proxy_grad(self):
        return PGReLU()


@leaf
class Ceil(ElementWiseUnaryOp):
    in_dtypes = [(i,) for i in DTYPE_FLOATS]
    out_dtypes = [(i,) for i in DTYPE_FLOATS]

    def torch(self):
        return torch.ceil

    def proxy_grad(self):
        return PGCeil()


@leaf
class Floor(ElementWiseUnaryOp):
    in_dtypes = [(i,) for i in DTYPE_FLOATS]
    out_dtypes = [(i,) for i in DTYPE_FLOATS]

    def torch(self):
        return torch.floor

    def proxy_grad(self):
        return PGFloor()


@leaf
class Clip(ElementWiseUnaryOp):
    in_dtypes = [(i,) for i in DTYPE_NON_BOOLS]

    def __init__(self):
        super().__init__()
        self.min = -1
        self.max = 1
        self.bias = None

    def _type_transfer(self, input_shapes: List[AbsTensor]) -> List[AbsTensor]:
        if self.bias is None:
            if input_shapes[0].dtype in DTYPE_FLOATS:
                self.bias = 0.5
            else:
                self.bias = 0
            self.min = self.min - self.bias
            self.max = self.max + self.bias
        return super()._type_transfer(input_shapes)

    def torch(self):
        return lambda x: torch.clip(x, self.min, self.max)

    def proxy_grad(self):
        return PGClip(self.min, self.max)


@leaf
class Round(ElementWiseUnaryOp):
    in_dtypes = [(i,) for i in DTYPE_FLOATS]
    out_dtypes = [(i,) for i in DTYPE_FLOATS]

    def torch(self):
        return torch.round

    def proxy_grad(self):
        return PGRound()


@leaf
class Sqrt(ElementWiseUnaryOp):
    in_dtypes = [(i,) for i in DTYPE_FLOATS]
    out_dtypes = [(i,) for i in DTYPE_FLOATS]

    def torch(self):
        return torch.sqrt

    def torch_loss(self, x):
        return loss_ge(x, 0)


@leaf
class Log2(ElementWiseUnaryOp):
    in_dtypes = [(i,) for i in DTYPE_FLOATS]
    out_dtypes = [(i,) for i in DTYPE_FLOATS]

    def torch(self):
        return torch.log2

    def torch_loss(self, x):
        return loss_gt_zero(x)


@leaf
class Neg(ElementWiseUnaryOp):
    in_dtypes = [(i,) for i in DTYPE_NON_BOOLS]
    out_dtypes = [(i,) for i in DTYPE_NON_BOOLS]

    def torch(self):
        return torch.neg


@leaf
class Softmax(ElementWiseUnaryOp):
    in_dtypes = [(i,) for i in DTYPE_FLOATS]
    out_dtypes = [(i,) for i in DTYPE_FLOATS]

    def __init__(self, dim: Union[int, z3.ExprRef]):
        super().__init__()
        self.dim = dim
        self.inp_ranks = [int_from(1)]
        self.out_ranks = [int_from(1)]

    def _requires(self, input_shapes: List[AbsTensor]) -> List[z3.ExprRef]:
        return [nnsmith_lt(self.dim, input_shapes[0].ndims), nnsmith_ge(self.dim, 0)]

    def torch(self) -> Callable[..., torch.Tensor]:
        return torch.nn.Softmax(dim=self.dim)


class Pool2d(UnaryOpBase):
    # TODO: distinguish stride_h and stride_w
    in_dtypes = [(i,) for i in DTYPE_FLOATS]
    out_dtypes = [(i,) for i in DTYPE_FLOATS]

    def __init__(
        self,
        kernel_h_size: Union[int, z3.ExprRef],
        kernel_w_size: Union[int, z3.ExprRef],
        stride: Union[int, z3.ExprRef],
        padding: Union[int, z3.ExprRef],
    ):
        super().__init__()
        self.kernel_h_size = kernel_h_size
        self.kernel_w_size = kernel_w_size
        self.stride = stride
        self.padding = padding

        self.inp_ranks = [(4,)]  # NCHW
        self.out_ranks = [(4,)]  # NCHW

    def _type_transfer(self, input_shapes: List[AbsTensor]) -> List[AbsTensor]:

        abs_tensor = AbsTensor([], dtype=input_shapes[0].dtype)
        # Batch dim: just copy
        abs_tensor.shape.append(input_shapes[0].shape[0])
        # Output channels
        abs_tensor.shape.append(input_shapes[0].shape[1])
        abs_tensor.shape.append(
            (
                nnsmith_div(
                    nnsmith_add(
                        nnsmith_sub(input_shapes[0].shape[2], self.kernel_h_size),
                        2 * self.padding,
                    ),
                    self.stride,
                )
                + 1
            )
        )
        abs_tensor.shape.append(
            (
                nnsmith_div(
                    nnsmith_add(
                        nnsmith_sub(input_shapes[0].shape[3], self.kernel_w_size),
                        2 * self.padding,
                    ),
                    self.stride,
                )
                + 1
            )
        )
        return [abs_tensor]

    def _requires(self, input_shapes):
        cons = []
        ret = []
        cons.append(nnsmith_ge(self.kernel_h_size, 1))
        cons.append(nnsmith_ge(self.kernel_w_size, 1))
        cons.append(
            nnsmith_le(
                self.kernel_h_size,
                nnsmith_add(input_shapes[0].shape[2], 2 * self.padding),
            )
        )
        cons.append(
            nnsmith_le(
                self.kernel_w_size,
                nnsmith_add(input_shapes[0].shape[3], 2 * self.padding),
            )
        )
        cons.append(nnsmith_ge(self.stride, 1))
        cons.append(nnsmith_ge(self.padding, 0))
        # not too extream to avoid torch exporter issue
        cons.append(nnsmith_le(self.padding, 255))
        cons.append(nnsmith_le(self.padding, nnsmith_div(self.kernel_h_size, 2)))
        cons.append(nnsmith_le(self.padding, nnsmith_div(self.kernel_w_size, 2)))

        # TensorRT rejects PRODUCT(pool size) >= 10000
        cons.append(
            nnsmith_lt(nnsmith_mul(self.kernel_h_size, self.kernel_w_size), 10000)
        )

        # limit FLOPS
        if Z3_CONS_FLOPS:
            cons.append(nnsmith_le(self.flops(input_shapes), FLOPS_LIM))
        for c in cons:
            ret.append(c)
        return ret

    def flops(self, input_shapes):
        return nnsmith_mul(
            nnsmith_mul(
                self.checked_type_transfer(input_shapes)[0].nelement(),
                self.kernel_h_size,
            ),
            self.kernel_w_size,
        )

    def deduct_inp_ranks_and_dtype(
        self, out_abs_tensor: List[AbsTensor]
    ) -> List[Tuple[int, DType]]:
        return [(4, out_abs_tensor[0].dtype)]


@leaf
class MaxPool2d(Pool2d):
    def torch(self) -> Callable[..., torch.Tensor]:
        return torch.nn.MaxPool2d(
            kernel_size=(self.kernel_h_size, self.kernel_w_size),
            stride=self.stride,
            padding=self.padding,
        )


@leaf
class AvgPool2d(Pool2d):
    # TODO: model more
    # self.extra_attrs['ceil_mode'] = random.choice([False, True])
    # self.extra_attrs['count_include_pad'] = random.choice([False, True])
    # self.extra_attrs['divisor_override'] = None  # ignore for now

    def torch(self) -> Callable[..., torch.Tensor]:
        return torch.nn.AvgPool2d(
            kernel_size=(self.kernel_h_size, self.kernel_w_size),
            stride=self.stride,
            padding=self.padding,
        )


@leaf
class Slice(UnaryOpBase):
    # pytorch slice always exported as a stack of single-dim slices, so only model sinlge-dim slice here
    # pytorch slice only supports forward slicing, so only model forward slicing here
    in_dtypes = [(i,) for i in DTYPE_ALL]
    INT_MAX = 2**63 - 1
    INT_MIN = -(2**63)

    def __init__(self, start, end, step):
        super().__init__()
        self.inp_ranks = [int_from(1)]
        self.out_ranks = [int_from(1)]
        self.start = start
        self.end = end
        self.step = step

    def __str__(self) -> str:
        if "axis" in self.extra_attrs:
            tail = {
                "axis": self.extra_attrs["axis"],
                "region": self.extra_attrs["region"],
            }
        else:
            tail = {}
        if isinstance(self.start, int):
            tail["start"] = self.start
        if isinstance(self.end, int):
            tail["end"] = self.end
        if isinstance(self.step, int):
            tail["step"] = self.step
        return super().__str__() + " " + str(tail)

    def _get_attrs(self, ndims):
        ConstraintCheck.true(ndims > 0)
        if "axis" not in self.extra_attrs:
            self.extra_attrs["ndims"] = ndims
            self.extra_attrs["axis"] = random.randint(0, ndims - 1)
            # specifying the region of the start and end pointer.
            # start \in [0, dim_s-1] if region=='right' else [-dim_s, -1]
            # end \in [-dim_s, -1] if region=='left' else [0, dim_s]
            self.extra_attrs["region"] = random.choice(["left", "mid", "right"])
            if random.uniform(0, 1) < 0.1:
                # torch exporter does not support start=INT_MIN
                # if random.uniform(0, 1) < 0.5:
                #     # because pytorch only supports forward slicing,
                #     # start cannot be INT_MAX, otherwise it slices empty tensor
                #     self.start = self.INT_MIN
                # else:
                self.end = self.INT_MAX
        return self.extra_attrs["axis"]

    def _requires(self, input_shapes: List[AbsTensor]):
        inp = input_shapes[0]
        axis = self._get_attrs(inp.ndims)
        reg = self.extra_attrs["region"]
        cons = []
        dim_s = inp.shape[axis]
        # range for start
        l, r = (0, nnsmith_sub(dim_s, 1))
        # range for end
        ll, rr = (0, dim_s)
        assert not isinstance(self.start, int)
        cons.append(
            z3.And(  # start \in [l, r]
                nnsmith_ge(self.start, l), nnsmith_le(self.start, r)
            )
        )
        if not isinstance(self.end, int):
            cons.append(
                z3.And(  # end \in [ll, rr]
                    nnsmith_ge(self.end, ll), nnsmith_le(self.end, rr)
                )
            )
            cons.append(nnsmith_gt(self.end, self.start))
        else:
            assert self.end == self.INT_MAX

        cons.append(nnsmith_ge(self.step, 1))  # forward slicing only
        cons.append(nnsmith_le(self.step, dim_s))
        return cons

    def _type_transfer(self, input_shapes: List[AbsTensor]) -> List[AbsTensor]:
        inp = input_shapes[0]
        axis = self._get_attrs(inp.ndims)
        s = list(inp.shape)
        end = self.end
        if self.end == Slice.INT_MAX:
            end = inp.shape[axis]
        s[axis] = nnsmith_div(
            nnsmith_add(nnsmith_sub(end, self.start), nnsmith_sub(self.step, 1)),
            self.step,
        )
        return [AbsTensor(s, input_shapes[0].dtype)]

    def torch(self):
        reg = self.extra_attrs["region"]

        def _func(x):
            dim_s = x.shape[self.extra_attrs["axis"]]
            start, end = self.start, self.end
            if reg in ["left", "mid"]:
                start -= dim_s
            # actual end would be 0, which is not really 'left'
            if reg == "left" and end < dim_s and end != Slice.INT_MAX:
                end -= dim_s
            s = tuple(
                slice(None, None)
                if i != self.extra_attrs["axis"]
                else slice(start, end, self.step)
                for i in range(self.extra_attrs["ndims"])
            )
            return x[s]

        return _func

    def deduct_inp_ranks_and_dtype(
        self, out_abs_tensor: List[AbsTensor]
    ) -> List[Tuple[int, DType]]:
        return [(out_abs_tensor[0].ndims, out_abs_tensor[0].dtype)]


def _pad_num_var_param(rstart=1, max=None):
    r = rstart  # rank
    ret = []
    while r <= __MAX_RANK__:
        h = r * 2
        if max is not None and h > max:
            break
        ret.append(h)
        r += 1
    return ret


class Pad(UnaryOpBase):
    num_var_param = _pad_num_var_param()
    in_dtypes = [(i,) for i in DTYPE_FLOATS]
    out_dtypes = [(i,) for i in DTYPE_FLOATS]

    def __str__(self) -> str:
        attr = {"padding_list": self.padding_list}
        return super().__str__() + " " + str(attr)

    def __init__(self, padding_list, pad_t):
        super().__init__()
        self.padding_list = padding_list
        self.extra_attrs["type"] = pad_t
        self.inp_ranks = [int_from(len(padding_list) // 2)]
        self.out_ranks = [int_from(len(padding_list) // 2)]
        assert (
            len(self.padding_list) % 2 == 0
        ), f"padding_list must be even, got {self.padding_list}"

    def _requires(self, input_shapes: List[AbsTensor]) -> List[z3.ExprRef]:
        pad = self.padding_list
        isv = input_shapes[0].shape
        cons = []
        for i in range(len(pad) // 2):
            j = len(isv) - 1 - i
            # When using negative padding, neither side should erase more than the original size
            cons.append(nnsmith_gt(nnsmith_add(pad[i * 2], isv[j]), 0))
            cons.append(nnsmith_gt(nnsmith_add(pad[i * 2 + 1], isv[j]), 0))
            cons.append(
                nnsmith_gt(
                    nnsmith_add(pad[i * 2 + 1], nnsmith_add(pad[i * 2], isv[j])), 0
                )
            )
        return cons

    def _type_transfer(self, input_shapes: List[AbsTensor]) -> List[AbsTensor]:
        isv = input_shapes[0].shape
        pad = self.padding_list
        s = list(isv)
        for i in range(len(pad) // 2):
            j = len(isv) - 1 - i
            s[j] = nnsmith_add(nnsmith_add(s[j], pad[i * 2]), pad[i * 2 + 1])
        return [AbsTensor(s, input_shapes[0].dtype)]

    def torch(self) -> Callable[..., torch.Tensor]:
        if self.extra_attrs["type"] == "constant":
            # 0 easily cause division by zero...
            # 1 easily cause false positives (sqrt(1) = 0.99999... != 1 in ORT, so floor(sqrt(1))=0)
            return lambda x: torch.nn.functional.pad(
                x, self.padding_list, "constant", value=0.5
            )
        elif (
            self.extra_attrs["type"] == "replicate"
            or self.extra_attrs["type"] == "reflect"
        ):
            return lambda x: torch.nn.functional.pad(
                x, self.padding_list, self.extra_attrs["type"]
            )

    def deduct_inp_ranks_and_dtype(
        self, out_abs_tensor: List[AbsTensor]
    ) -> List[Tuple[int, DType]]:
        return [(out_abs_tensor[0].ndims, out_abs_tensor[0].dtype)]


@leaf
class ConstPad(Pad):
    def __init__(self, *padding_list):
        super().__init__(padding_list, "constant")


@leaf
class ReplicatePad(Pad):
    num_var_param = _pad_num_var_param(2, max=6)

    def __init__(self, *padding_list):
        super().__init__(padding_list, "replicate")
        self.inp_ranks = [int_range(len(padding_list) // 2 + 1, 4)]
        self.out_ranks = [int_range(len(padding_list) // 2 + 1, 4)]


@leaf
class ReflectPad(Pad):
    num_var_param = _pad_num_var_param(2, max=6)

    def __init__(self, *padding_list):
        super().__init__(padding_list, "reflect")
        self.inp_ranks = [int_range(len(padding_list) // 2 + 1, 4)]
        self.out_ranks = [int_range(len(padding_list) // 2 + 1, 4)]

    def _requires(self, input_shapes: List[AbsTensor]) -> List[z3.ExprRef]:
        cons = super()._requires(input_shapes)
        pad = self.padding_list
        isv = input_shapes[0].shape
        for i in range(len(pad) // 2):
            j = len(isv) - 1 - i
            # per torch's complaint: Padding size should be less than the corresponding input dimension
            cons.append(nnsmith_lt(pad[i * 2], isv[j]))
            cons.append(nnsmith_lt(pad[i * 2 + 1], isv[j]))
            # same sign to avoid ORT bugs
            cons.append(nnsmith_ge(pad[i * 2] * pad[i * 2 + 1], 0))
        return cons


class Expand(UnaryOpBase, ABC):
    in_dtypes = [(i,) for i in DTYPE_ALL]
    out_dtypes = [(i,) for i in DTYPE_ALL]
    # expand_dim cannot be symbolic. So just expand it.

    def __init__(self, expand_last_dim: int, expand_n: Union[int, z3.ExprRef]):
        """See https://pytorch.org/docs/stable/generated/torch.Tensor.expand.html"""
        super().__init__()
        self.inp_ranks = [int_all()]
        SanityCheck.ge(expand_last_dim, 1)
        self.expand_last_dim = expand_last_dim
        self.expand_n = expand_n

    def _type_transfer(self, input_shapes: List[AbsTensor]) -> List[AbsTensor]:
        if self.expand_last_dim <= len(input_shapes[0].shape):
            # NOTE: Werid, deepcopy is useless here.
            shape = AbsTensor(
                shape=[*input_shapes[0].shape], dtype=input_shapes[0].dtype
            )
            shape.shape[-self.expand_last_dim] = self.expand_n
            return [shape]
        else:  # expand it;
            # for example. we have:
            #       input shape [u, v]
            #       expand_last_dim <- 4
            #       return [expand_n, 1, u, v] where `1` is padded.
            dtype = input_shapes[0].dtype
            return [
                AbsTensor(
                    [
                        self.expand_n,
                        *(
                            [1]
                            * (self.expand_last_dim - len(input_shapes[0].shape) - 1)
                        ),
                        *input_shapes[0].shape,
                    ],
                    dtype,
                )
            ]

    def _requires(self, input_shapes):
        SanityCheck.ge(self.expand_last_dim, 1)

        input_shape = input_shapes[0].shape
        if self.expand_last_dim <= len(input_shape):  # index valid
            cons = [
                nnsmith_eq(input_shape[-self.expand_last_dim], 1),
                nnsmith_ge(self.expand_n, 1),
            ]
            return cons
        return [nnsmith_ge(self.expand_n, 1)]

    def torch(self):
        return lambda x: x.expand(
            *self._type_transfer([AbsTensor.from_torch(x)])[0].shape
        )

    def deduct_inp_ranks_and_dtype(
        self, out_abs_tensor: List[AbsTensor]
    ) -> List[Tuple[int, DType]]:
        inp_rank = (
            self.expand_last_dim
            if out_abs_tensor[0].ndims < self.expand_last_dim
            else out_abs_tensor[0].ndims
        )
        ConstraintCheck.ge(out_abs_tensor[0].ndims, self.expand_last_dim)
        return [(inp_rank, out_abs_tensor[0].dtype)]


@leaf
class ExpandLast1(Expand):
    def __init__(self, expand_n: Union[int, z3.ExprRef]):
        super().__init__(expand_last_dim=1, expand_n=expand_n)


@leaf
class ExpandLast2(Expand):
    def __init__(self, expand_n: Union[int, z3.ExprRef]):
        super().__init__(expand_last_dim=2, expand_n=expand_n)


@leaf
class ExpandLast3(Expand):
    def __init__(self, expand_n: Union[int, z3.ExprRef]):
        super().__init__(expand_last_dim=3, expand_n=expand_n)


@leaf
class ExpandLast4(Expand):
    def __init__(self, expand_n: Union[int, z3.ExprRef]):
        super().__init__(expand_last_dim=4, expand_n=expand_n)


@leaf
class BatchNorm2d(ElementWiseUnaryOp):
    in_dtypes = [(DType.float32,)]
    out_dtypes = [(DType.float32,)]

    def __init__(self, nfeat):
        super().__init__()
        self.inp_ranks = [(4,)]
        self.out_ranks = [(4,)]
        self.nfeat = nfeat

    def deduct_inp_ranks_and_dtype(
        self, out_abs_tensor: List[AbsTensor]
    ) -> List[Tuple[int, DType]]:
        return [(4, DType.float32)]

    def _requires(self, input_shapes: List[AbsTensor]) -> List[z3.ExprRef]:
        return [
            nnsmith_eq(self.nfeat, input_shapes[0].shape[1]),
            nnsmith_ge(input_shapes[0].shape[0], 2),
        ]  # batch size = 1 -> fail training.

    def torch(self) -> Callable[..., torch.Tensor]:
        return torch.nn.BatchNorm2d(num_features=self.nfeat)


@leaf
class Conv1d(UnaryOpBase):
    in_dtypes = [(DType.float32,)]
    out_dtypes = [(DType.float32,)]

    def __init__(
        self,
        in_channels: Union[int, z3.ExprRef],
        out_channels: Union[int, z3.ExprRef],
        kernel_size: Union[int, z3.ExprRef],
        stride: Union[int, z3.ExprRef],
        padding: Union[int, z3.ExprRef],
        dilation: Union[int, z3.ExprRef],
    ):
        super().__init__()
        self.in_channels = in_channels
        self.out_channels = out_channels
        self.kernel_size = kernel_size
        self.stride = stride
        self.padding = padding
        self.dilation = dilation

        self.inp_ranks = [(3,)]  # NCL
        self.out_ranks = [(3,)]  # NCL

    def _type_transfer(self, input_shapes: List[AbsTensor]) -> List[AbsTensor]:
        abs_tensor = AbsTensor(
            [input_shapes[0].shape[0], self.out_channels], dtype=input_shapes[0].dtype
        )
        mimic_k = self.kernel_size + (self.dilation - 1) * (self.kernel_size - 1)
        abs_tensor.shape.append(
            (
                nnsmith_div(
                    nnsmith_add(
                        nnsmith_sub(input_shapes[0].shape[2], mimic_k), 2 * self.padding
                    ),
                    self.stride,
                )
                + 1
            )
        )

        return [abs_tensor]

    def _requires(self, input_shapes):
        # FIXME: Handling flops.
        cons = []
        cons.append(nnsmith_eq(self.in_channels, input_shapes[0].shape[1]))
        cons.append(nnsmith_ge(self.out_channels, 1))
        cons.append(nnsmith_ge(self.dilation, 1))
        mimic_k = self.kernel_size + (self.dilation - 1) * (self.kernel_size - 1)
        cons.append(nnsmith_ge(mimic_k, 1))
        cons.append(nnsmith_ge(self.stride, 1))
        cons.append(nnsmith_ge(self.padding, 0))
        cons.append(
            nnsmith_le(mimic_k, nnsmith_add(input_shapes[0].shape[2], 2 * self.padding))
        )
        # not too extream to avoid torch exporter issue
        cons.append(nnsmith_le(self.padding, 255))
        return cons

    def deduct_inp_ranks_and_dtype(
        self, out_abs_tensor: List[AbsTensor]
    ) -> List[Tuple[int, DType]]:
        return [(3, out_abs_tensor[0].dtype)]

    def __repr__(self) -> str:
        repr = f"Conv1d({self.in_channels}, {self.out_channels}, k={self.kernel_size}"
        if not isinstance(self.stride, int) or self.stride != 1:
            repr += f", s={self.stride}"
        if not isinstance(self.padding, int) or self.padding != 0:
            repr += f", p={self.padding}"
        if not isinstance(self.dilation, int) or self.dilation != 1:
            repr += f", d={self.dilation}"
        repr += ")"
        return repr

    def torch(self):
        return torch.nn.Conv1d(
            in_channels=self.in_channels,
            out_channels=self.out_channels,
            kernel_size=self.kernel_size,
            stride=self.stride,
            padding=self.padding,
            dilation=self.dilation,
        )


@leaf
class NCHWConv2d(UnaryOpBase):
    # FIXME: torch exporter does not support float64, may miss bugs
    in_dtypes = [(DType.float32,)]
    out_dtypes = [(DType.float32,)]

    def __init__(
        self,
        in_channels: Union[int, z3.ExprRef],
        out_channels: Union[int, z3.ExprRef],
        kernel_h_size: Union[int, z3.ExprRef],
        kernel_w_size: Union[int, z3.ExprRef],
        stride: Union[int, z3.ExprRef],
        padding: Union[int, z3.ExprRef],
        dilation_h: Union[int, z3.ExprRef],
        dilation_w: Union[int, z3.ExprRef],
    ):
        """See https://pytorch.org/docs/stable/generated/torch.nn.Conv2d.html"""
        super().__init__()
        self.in_channels = in_channels
        self.out_channels = out_channels
        self.kernel_h_size = kernel_h_size
        self.kernel_w_size = kernel_w_size
        self.stride = stride
        self.padding = padding
        self.dilation_h = dilation_h
        self.dilation_w = dilation_w

        self.inp_ranks = [(4,)]  # NC(H,)W
        self.out_ranks = [(4,)]  # NC(H,)W

    def _type_transfer(self, input_shapes: List[AbsTensor]) -> List[AbsTensor]:
        abs_tensor = AbsTensor(
            [input_shapes[0].shape[0], self.out_channels], dtype=input_shapes[0].dtype
        )

        mimic_kh = self.kernel_h_size + (self.dilation_h - 1) * (self.kernel_h_size - 1)
        mimic_kw = self.kernel_w_size + (self.dilation_w - 1) * (self.kernel_w_size - 1)

        abs_tensor.shape.append(
            (
                nnsmith_div(
                    nnsmith_add(
                        nnsmith_sub(input_shapes[0].shape[2], mimic_kh),
                        2 * self.padding,
                    ),
                    self.stride,
                )
                + 1
            )
        )
        abs_tensor.shape.append(
            (
                nnsmith_div(
                    nnsmith_add(
                        nnsmith_sub(input_shapes[0].shape[3], mimic_kw),
                        2 * self.padding,
                    ),
                    self.stride,
                )
                + 1
            )
        )
        return [abs_tensor]

    def _requires(self, input_shapes):
        cons = []
        # TODO: Use eager mode for debugging.
        cons.append(nnsmith_eq(self.in_channels, input_shapes[0].shape[1]))
        cons.append(nnsmith_ge(self.out_channels, 1))
        cons.append(nnsmith_ge(self.dilation_h, 1))
        cons.append(nnsmith_ge(self.dilation_w, 1))
        mimic_kh = self.kernel_h_size + (self.dilation_h - 1) * (self.kernel_h_size - 1)
        mimic_kw = self.kernel_w_size + (self.dilation_w - 1) * (self.kernel_w_size - 1)
        cons.append(nnsmith_ge(mimic_kh, 1))
        cons.append(nnsmith_ge(mimic_kw, 1))
        cons.append(nnsmith_ge(self.stride, 1))
        cons.append(nnsmith_ge(self.padding, 0))
        cons.append(
            nnsmith_le(
                mimic_kh, nnsmith_add(input_shapes[0].shape[2], 2 * self.padding)
            )
        )
        cons.append(
            nnsmith_le(
                mimic_kw, nnsmith_add(input_shapes[0].shape[3], 2 * self.padding)
            )
        )
        # not too extream to avoid torch exporter issue
        cons.append(nnsmith_le(self.padding, 255))
        # limit FLOPS
        if Z3_CONS_FLOPS:
            cons.append(nnsmith_le(self.flops(input_shapes), FLOPS_LIM))
        return cons

    def torch(self):
        return torch.nn.Conv2d(
            self.in_channels,
            self.out_channels,
            kernel_size=(self.kernel_h_size, self.kernel_w_size),
            stride=self.stride,
            padding=self.padding,
            device=_DEV,
        )

    def flops(self, input_shapes):
        w = AbsTensor(
            [
                self.out_channels,
                self.in_channels,
                self.kernel_h_size,
                self.kernel_w_size,
            ],
            dtype=input_shapes[0].dtype,
        )
        return nnsmith_mul(
            nnsmith_mul(
                nnsmith_mul(
                    self._type_transfer(input_shapes)[0].nelement(), self.in_channels
                ),
                self.kernel_h_size,
            ),
            self.kernel_w_size,
        )

    def n_floats(self, input_shapes):
        # FIXME: maybe need to take dilation into account?
        padded_data = AbsTensor(input_shapes[0].shape, dtype=input_shapes[0].dtype)
        padded_data.shape[2] = nnsmith_add(
            padded_data.shape[2], nnsmith_mul(2, self.padding)
        )
        padded_data.shape[3] = nnsmith_add(
            padded_data.shape[3], nnsmith_mul(2, self.padding)
        )
        w = AbsTensor(
            [
                self.out_channels,
                self.in_channels,
                self.kernel_h_size,
                self.kernel_w_size,
            ],
            dtype=input_shapes[0].dtype,
        )
        outs = super().n_floats(input_shapes)
        return nnsmith_add(nnsmith_add(w.nelement(), padded_data.nelement()), outs)

    def deduct_inp_ranks_and_dtype(
        self, out_abs_tensor: List[AbsTensor]
    ) -> List[Tuple[int, DType]]:
        return [(4, out_abs_tensor[0].dtype)]

    def __repr__(self) -> str:
        repr = f"Conv2d({self.in_channels}, {self.out_channels}, k=({self.kernel_h_size},{self.kernel_w_size})"
        if not isinstance(self.stride, int) or self.stride != 1:
            repr += f", s={self.stride}"
        if not isinstance(self.padding, int) or self.padding != 0:
            repr += f", p={self.padding}"
        if (
            not isinstance(self.dilation_h, int)
            or self.dilation_h != 1
            or self.dilation_w != 1
        ):
            repr += f", d=({self.dilation_h}, {self.dilation_w})"
        repr += ")"
        return repr


def random_group(n, k):
    xs = sorted([random.randint(0, n - k) for _ in range(k - 1)])
    xs = [0] + xs + [n - k]
    ret = []
    perm = list(range(n))
    random.shuffle(perm)
    for i in range(k):
        st = xs[i] + i
        ed = xs[i + 1] + i + 1
        assert st < ed, (xs, st, ed)
        assert ed <= n, (st, ed, n)
        assert st >= 0, (st, ed, n)
        ret.append([perm[j] for j in range(st, ed)])
    return ret


@leaf
class Reshape(UnaryOpBase):
    num_var_param = int_range(1, 4)
    in_dtypes = [(i,) for i in DTYPE_ALL]
    out_dtypes = [(i,) for i in DTYPE_ALL]

    def __init__(self, *target_shape):
        super().__init__()
        self.inp_ranks = [int_range(1, 4)]
        self.out_ranks = [(len(target_shape),)]
        self.target_shape: List[Union[int, z3.ExprRef]] = target_shape

    def _type_transfer(self, input_shapes: List[AbsTensor]) -> List[AbsTensor]:
        __MAX_SOLVE_SYMBOL__ = 8
        # otherwise OOM.
        ConstraintCheck.le(
            input_shapes[0].ndims + len(self.target_shape), __MAX_SOLVE_SYMBOL__
        )

        if -1 not in self.target_shape:
            return [AbsTensor(self.target_shape, dtype=input_shapes[0].dtype)]
        # else
        abs_tensor = AbsTensor(self.target_shape, dtype=input_shapes[0].dtype)
        auto_dim = -1
        accum = 1
        for i, v in enumerate(self.target_shape):
            # TODO: What to do about bitvectors here?
            if v == -1:
                SanityCheck.eq(auto_dim, -1)
                auto_dim = i
            else:
                accum = nnsmith_mul(accum, v)

        abs_tensor.shape[auto_dim] = nnsmith_div(
            reduce(lambda x, y: nnsmith_mul(x, y), input_shapes[0].shape, 1), accum
        )

        return [abs_tensor]

    def _requires(self, input_shapes):
        ret = []

        inp = input_shapes[0]
        src_len, dst_len = inp.ndims, len(self.target_shape)
        if src_len == 0:
            src_len = 1  # special handling for scalar
        if dst_len == 0:
            dst_len = 1  # special handling for scalar
        gres_config = os.getenv("NNSMITH_GRES", "4")
        if gres_config == "5":
            ng = 1
        elif gres_config == "3":
            ng = min(src_len, dst_len)
        elif gres_config == "4":
            ub = min(src_len, dst_len)
            ng = random.choices(
                range(1, ub + 1), k=1, weights=[2**i for i in range(ub)]
            )[0]
        else:
            raise ValueError(f"NNSMITH_GRES={gres_config} is not recognized")
        src_group = random_group(src_len, ng)
        dst_group = random_group(dst_len, ng)
        self.ng = ng
        self.src_group = src_group
        self.dst_group = dst_group
        assert len(src_group) == len(dst_group) == ng, (src_group, dst_group)

        # group constraints
        src_vars = inp.shape
        dst_vars = self.target_shape
        if len(src_vars) == 0:
            src_vars = [1]  # special handling for scalar
        if len(dst_vars) == 0:
            dst_vars = [1]  # special handling for scalar
        cons_group = []
        for gid in range(ng):
            src_idx = src_group[gid]
            dst_idx = dst_group[gid]
            src_prod = reduce(nnsmith_mul, [src_vars[i] for i in src_idx], 1)
            dst_prod = reduce(nnsmith_mul, [dst_vars[i] for i in dst_idx], 1)
            cons_group.append(nnsmith_eq(src_prod, dst_prod))

        ret.extend(cons_group)
        if os.getenv("NNSMITH_CONS_RESHAPE", "off") != "off":
            # should not be too extreme!
            __DIM_LIMIT__ = 4096
            lim = __DIM_LIMIT__
            for s in self.target_shape[::-1]:
                ret.append(nnsmith_le(s, lim))
                lim //= 2
                lim = max(lim, 1)
        assert -1 not in self.target_shape
        return ret

    def torch(self):
        return lambda x: x.reshape(*self.target_shape)

    def deduct_inp_ranks_and_dtype(
        self, out_abs_tensor: List[AbsTensor]
    ) -> List[Tuple[int, DType]]:
        return [(-1, out_abs_tensor[0].dtype)]


@leaf
class Flatten(Reshape):
    num_var_param = None
    # Inputs are target shape.

    def __init__(self, dim0: Union[int, z3.ExprRef]):
        super().__init__(1, dim0)
        self.dim0 = dim0

    def torch(self):
        # See https://github.com/pytorch/pytorch/issues/74142
        return lambda x: x.flatten().unsqueeze(0)


@leaf
class Transpose(UnaryOpBase):
    in_dtypes = [(i,) for i in DTYPE_ALL]

    def __init__(self):
        """See https://pytorch.org/docs/stable/generated/torch.transpose.html"""
        super().__init__()
        self.inp_ranks = [int_from(2)]
        self.out_ranks = [int_from(2)]

    def _init_swap_dims(self, input_shape: List[Union[int, z3.ExprRef]]):
        ConstraintCheck.ge(len(input_shape), 2)
        self.inp_ranks = [len(input_shape)]
        if "dim0" not in self.extra_attrs or "dim1" not in self.extra_attrs:
            max_dim = len(input_shape) - 1
            self.extra_attrs["dim0"] = random.randint(0, max_dim)
            self.extra_attrs["dim1"] = (
                random.randint(1, max_dim) + self.extra_attrs["dim0"]
            ) % (1 + max_dim)
        return self.extra_attrs["dim0"], self.extra_attrs["dim1"]

    def _type_transfer(self, input_shapes: List[AbsTensor]) -> List[AbsTensor]:
        dim0, dim1 = self._init_swap_dims(input_shapes[0].shape)
        shape = list(input_shapes[0].shape)
        shape[dim0], shape[dim1] = shape[dim1], shape[dim0]
        return [AbsTensor(shape, input_shapes[0].dtype)]

    def _requires(self, input_shapes):
        dim0, dim1 = self._init_swap_dims(input_shapes[0].shape)
        SanityCheck.ge(
            len(input_shapes[0].shape),
            max(dim0, dim1) + 1,
            f"dim={len(input_shapes[0].shape)}.transpose({dim0},{dim1})",
        )
        return []

    def torch(self):
        def f(x: torch.Tensor):
            dim0, dim1 = self._init_swap_dims(list(x.shape))
            return x.transpose(dim0, dim1)

        return f

    def deduct_inp_ranks_and_dtype(
        self, out_abs_tensor: List[AbsTensor]
    ) -> List[Tuple[int, DType]]:
        return [(out_abs_tensor[0].ndims, out_abs_tensor[0].dtype)]


# Sum, Min, Max, Mean, ArgMin, ArgMax, Squeeze, Size


class InterpBase(UnaryOpBase):
    num_var_param = int_range(1, 3)

    in_dtypes = [(i,) for i in DTYPE_FLOATS]
    out_dtypes = [(i,) for i in DTYPE_FLOATS]

    def __init__(self, *size):
        super().__init__()
        self.size = size
        self.inp_ranks = [(len(size) + 2,)]
        self.out_ranks = [(len(size) + 2,)]

    def _requires(self, input_shapes: List[AbsTensor]):
        return [nnsmith_gt(v, 0) for v in self.size]

    def _type_transfer(self, input_shapes: List[AbsTensor]) -> List[AbsTensor]:
        shape = list(input_shapes[0].shape)
        for i in range(len(self.size)):
            shape[-(1 + i)] = self.size[-(1 + i)]
        return [AbsTensor(shape, input_shapes[0].dtype)]

    def deduct_inp_ranks_and_dtype(
        self, out_abs_tensor: List[AbsTensor]
    ) -> List[Tuple[int, DType]]:
        return [(out_abs_tensor[0].ndims, out_abs_tensor[0].dtype)]


@leaf
class NearestInterp(InterpBase):
    def torch(self) -> Callable[..., torch.Tensor]:
        return lambda x: torch.nn.functional.interpolate(
            x, size=self.size, mode="nearest"
        )


@leaf
class LinearInterp(InterpBase):
    num_var_param = [1]

    def torch(self) -> Callable[..., torch.Tensor]:
        return lambda x: torch.nn.functional.interpolate(
            x, size=self.size, mode="linear"
        )


@leaf
class BilinearInterp(InterpBase):
    num_var_param = [2]

    def torch(self) -> Callable[..., torch.Tensor]:
        return lambda x: torch.nn.functional.interpolate(
            x, size=self.size, mode="bilinear"
        )


@leaf
class BicubicInterp(InterpBase):
    num_var_param = [2]

    def torch(self) -> Callable[..., torch.Tensor]:
        return lambda x: torch.nn.functional.interpolate(
            x, size=self.size, mode="bicubic"
        )


@leaf
class TrilinearInterp(InterpBase):
    num_var_param = [3]

    def torch(self) -> Callable[..., torch.Tensor]:
        return lambda x: torch.nn.functional.interpolate(
            x, size=self.size, mode="trilinear"
        )


class ReduceBase(UnaryOpBase, ABC):
    _reduce_out_dtype = None  # None means same as input dtype

    def __init__(self):
        super().__init__()
        self.inp_ranks = [int_from(1)]  # TVM bug ~ crash on scalar.min()
        self.out_ranks = [int_range(0, __MAX_RANK__ - 1)]

    def __str__(self) -> str:
        return (
            super().__str__()
            + f'(dim={self.extra_attrs["reduce_dim"] if "reduce_dim" in self.extra_attrs else None})'
        )

    def _init_reduce_dim(self, input_shape: List[Union[int, z3.ExprRef]]):
        if "reduce_dim" not in self.extra_attrs:
            if len(input_shape) == 0:
                self.extra_attrs["reduce_dim"] = None
            else:
                self.extra_attrs["reduce_dim"] = random.randint(0, len(input_shape) - 1)
        return self.extra_attrs["reduce_dim"]

    def _type_transfer(self, input_shapes: List[AbsTensor]) -> List[AbsTensor]:
        svar_list = []
        for i, v in enumerate(input_shapes[0].shape):
            if i != self._init_reduce_dim(input_shapes[0].shape):
                svar_list.append(v)
        return [
            AbsTensor(
                svar_list,
                input_shapes[0].dtype
                if self._reduce_out_dtype is None
                else self._reduce_out_dtype,
            )
        ]

    def _requires(self, input_shapes: List[AbsTensor]):
        reduce_dim = self._init_reduce_dim(input_shapes[0].shape)
        return []

    def _get_irank(self, orank):
        # if orank == 0:  # TVM bug ~ crash on scalar.min()
        #     return random.randint(0, 1)
        return orank + 1

    def deduct_inp_ranks_and_dtype(
        self, out_abs_tensor: List[AbsTensor]
    ) -> List[Tuple[int, DType]]:
        return [(self._get_irank(out_abs_tensor[0].ndims), out_abs_tensor[0].dtype)]


@leaf
class Squeeze(ReduceBase):
    in_dtypes = [(i,) for i in DTYPE_ALL]

    def _requires(self, input_shapes):
        reduce_dim = self._init_reduce_dim(input_shapes[0].shape)
        if reduce_dim is None:
            return []
        return [nnsmith_eq(input_shapes[0].shape[reduce_dim], 1)]

    def torch(self):
        if self.extra_attrs["reduce_dim"] is not None:
            return lambda x: x.squeeze(self.extra_attrs["reduce_dim"])
        else:
            return lambda x: x.squeeze()


@leaf
class ReduceSum(ReduceBase):
    # pytorch exporter doesn't support int32
    in_dtypes = [(i,) for i in DTYPE_NON_BOOLS if i != DType.int32]
    out_dtypes = [(i,) for i in DTYPE_NON_BOOLS if i != DType.int32]

    def torch(self):
        if self.extra_attrs["reduce_dim"] is not None:
            return lambda x: x.sum(self.extra_attrs["reduce_dim"])
        return lambda x: x.sum()


@leaf
class ReduceMin(ReduceBase):
    in_dtypes = [(i,) for i in DTYPE_NON_BOOLS]
    out_dtypes = [(i,) for i in DTYPE_NON_BOOLS]

    def torch(self):
        if self.extra_attrs["reduce_dim"] is not None:
            return lambda x: x.min(self.extra_attrs["reduce_dim"]).values
        return lambda x: x.min()


@leaf
class ReduceMax(ReduceBase):
    in_dtypes = [(i,) for i in DTYPE_NON_BOOLS]
    out_dtypes = [(i,) for i in DTYPE_NON_BOOLS]

    def torch(self):
        if self.extra_attrs["reduce_dim"] is not None:
            return lambda x: x.max(self.extra_attrs["reduce_dim"]).values
        return lambda x: x.max()


@leaf
class ReduceMean(ReduceBase):
    in_dtypes = [(i,) for i in DTYPE_FLOATS]
    out_dtypes = [(i,) for i in DTYPE_FLOATS]

    def torch(self):
        if self.extra_attrs["reduce_dim"] is not None:
            return lambda x: x.mean(self.extra_attrs["reduce_dim"])
        return lambda x: x.mean()


@leaf
class ArgMin(ReduceBase):
    # FIXME(JK): ints are somehow not supported in onnxruntime, which we use to gen inputs.
    # Make it include ints once we use other backends other than onnxruntime.
    in_dtypes = [(i,) for i in DTYPE_FLOATS]
    out_dtypes = [(DType.int64,)]
    _reduce_out_dtype = DType.int64

    def torch(self):
        if self.extra_attrs["reduce_dim"] is not None:
            return lambda x: x.argmin(self.extra_attrs["reduce_dim"])
        return lambda x: x.argmin()

    def deduct_inp_ranks_and_dtype(
        self, out_abs_tensor: List[AbsTensor]
    ) -> List[Tuple[int, DType]]:
        return [
            (self._get_irank(out_abs_tensor[0].ndims), random.choice(self.in_dtypes)[0])
        ]


@leaf
class ArgMax(ReduceBase):
    # FIXME(JK): ints are somehow not supported in onnxruntime, which we use to gen inputs.
    # Make it include ints once we use other backends other than onnxruntime.
    in_dtypes = [(i,) for i in DTYPE_FLOATS]
    out_dtypes = [(DType.int64,)]
    _reduce_out_dtype = DType.int64

    def torch(self):
        if self.extra_attrs["reduce_dim"] is not None:
            return lambda x: x.argmax(self.extra_attrs["reduce_dim"])
        return lambda x: x.argmax()

    def deduct_inp_ranks_and_dtype(
        self, out_abs_tensor: List[AbsTensor]
    ) -> List[Tuple[int, DType]]:
        return [
            (self._get_irank(out_abs_tensor[0].ndims), random.choice(self.in_dtypes)[0])
        ]


class TriBase(UnaryOpBase):
    in_dtypes = [(i,) for i in DTYPE_ALL]
    out_dtypes = [(i,) for i in DTYPE_ALL]

    def __init__(self, diagonal: Union[int, z3.ExprRef]):
        super().__init__()
        self.diagonal = diagonal
        # tril is only for 2-D matrix
        self.inp_ranks = [(2,)]
        self.out_ranks = [(2,)]

    def _type_transfer(self, input_shapes: List[AbsTensor]) -> List[AbsTensor]:
        SanityCheck.eq(len(input_shapes), 1)
        return [input_shapes[0]]

    def deduct_inp_ranks_and_dtype(
        self, out_abs_tensor: List[AbsTensor]
    ) -> List[Tuple[int, DType]]:
        return [(2, out_abs_tensor[0].dtype)]


@leaf
class Tril(TriBase):
    def _requires(self, input_shapes: List[AbsTensor]) -> List[z3.ExprRef]:
        ConstraintCheck.true(input_shapes[0].ndims == 2)
        nrow = input_shapes[0].shape[0]
        ncol = input_shapes[0].shape[1]
        return [z3.And(self.diagonal >= -nrow, (ncol - 1) >= self.diagonal)]

    def torch(self):
        return lambda x: x.tril(self.diagonal)


@leaf
class Triu(TriBase):
    def _requires(self, input_shapes: List[AbsTensor]) -> List[z3.ExprRef]:
        ConstraintCheck.true(input_shapes[0].ndims == 2)
        nrow = input_shapes[0].shape[0]
        ncol = input_shapes[0].shape[1]
        return [z3.And(self.diagonal >= -(nrow - 1), ncol >= self.diagonal)]

    def torch(self):
        return lambda x: x.triu(self.diagonal)


@leaf
class Linear(UnaryOpBase):
    in_dtypes = [(DType.float32,)]
    out_dtypes = [(DType.float32,)]

    def __init__(self, ifeat: Union[int, z3.ExprRef], ofeat: Union[int, z3.ExprRef]):
        super().__init__()
        self.ifeat = ifeat
        self.ofeat = ofeat
        self.inp_ranks = [int_from(1)]
        # at least one dim. cannot be zranks_all()
        self.out_ranks = [int_from(1)]

    def _type_transfer(self, input_shapes: List[AbsTensor]) -> List[AbsTensor]:
        assert len(input_shapes) == 1, "Linear only takes one input, but got {}".format(
            len(input_shapes)
        )
        return [
            AbsTensor(
                shape=[*input_shapes[0].shape[:-1], self.ofeat], dtype=DType.float32
            )
        ]

    def _requires(self, input_shapes: List[AbsTensor]) -> List[z3.ExprRef]:
        ConstraintCheck.true(input_shapes[0].ndims >= 1)
        return [
            nnsmith_ge(self.ifeat, 1),
            nnsmith_ge(self.ofeat, 1),
            nnsmith_eq(input_shapes[0].shape[-1], self.ifeat),
        ]

    def torch(self) -> Callable[..., torch.Tensor]:
        return torch.nn.Linear(in_features=self.ifeat, out_features=self.ofeat)

    def deduct_inp_ranks_and_dtype(
        self, out_abs_tensor: List[AbsTensor]
    ) -> List[Tuple[int, DType]]:
        return [(out_abs_tensor[0].ndims, DType.float32)]


def partialclass(cls, name, *args, **kwds) -> Type[AbsOpBase]:
    return type(
        name, (cls,), {"__init__": functools.partialmethod(cls.__init__, *args, **kwds)}
    )


class Concat(AbsOpBase):
    MAX_ARITY = 5
    MAX_RANK = 5
    out_dtypes = [(i,) for i in DTYPE_ALL]

    def __str__(self) -> str:
        return "Concat " + str(self.extra_attrs)

    def __init__(self, arity):
        super().__init__()
        SanityCheck.le(arity, Concat.MAX_ARITY)
        self.arity = arity
        self.inp_ranks = [(int_from(1))] * arity
        self.out_ranks = [(int_from(1))]
        self.same_inp_dims = True

    def _init_concat_axis(self, input_shapes: List[AbsTensor]) -> int:
        if "axis" not in self.extra_attrs:
            self.extra_attrs["axis"] = random.randint(0, input_shapes[0].ndims - 1)
        return self.extra_attrs["axis"]

    def _requires(self, input_shapes: List[AbsTensor]) -> List[z3.ExprRef]:
        ndims = input_shapes[0].ndims
        SanityCheck.gt(ndims, self._init_concat_axis(input_shapes))
        for s in input_shapes:
            SanityCheck.eq(s.ndims, ndims)
        cons = []
        for d in range(ndims):
            if d != self._init_concat_axis(input_shapes):
                cons.extend(
                    nnsmith_eq(s.shape[d], input_shapes[0].shape[d])
                    for s in input_shapes
                )
        return cons

    def _type_transfer(self, input_shapes: List[AbsTensor]) -> List[AbsTensor]:
        SanityCheck.true(input_shapes[0].ndims > 0)
        axis = self._init_concat_axis(input_shapes)
        os = AbsTensor(input_shapes[0].shape, input_shapes[0].dtype)
        os.shape[axis] = reduce(nnsmith_add, [s.shape[axis] for s in input_shapes])
        return [os]

    def torch(self):
        axis = self.extra_attrs["axis"]
        return lambda *args: torch.cat(args, dim=axis)

    def deduct_inp_ranks_and_dtype(
        self, out_abs_tensor: List[AbsTensor]
    ) -> List[Tuple[int, DType]]:
        return [
            (out_abs_tensor[0].ndims, out_abs_tensor[0].dtype)
            for _ in range(self.arity)
        ]


# the semantic of `in_dtypes` is not possible dtypes in "max rank". but simply in "rank". don't mess up the definition.
@leaf
class Concat1(Concat):
    in_dtypes = [(i,) for i in DTYPE_ALL]

    def __init__(self):
        super().__init__(1)


@leaf
class Concat2(Concat):
    in_dtypes = [(i, i) for i in DTYPE_ALL]

    def __init__(self):
        super().__init__(2)


@leaf
class Concat3(Concat):
    in_dtypes = [(i, i, i) for i in DTYPE_ALL]

    def __init__(self):
        super().__init__(3)


@leaf
class Concat4(Concat):
    in_dtypes = [(i, i, i, i) for i in DTYPE_ALL]

    def __init__(self):
        super().__init__(4)


@leaf
class Concat5(Concat):
    in_dtypes = [(i, i, i, i, i) for i in DTYPE_ALL]

    def __init__(self):
        super().__init__(5)


class Cast(ElementWiseUnaryOp, ABC):
    in_dtypes = [(i,) for i in DTYPE_ALL]

    def __init__(self, dtype):
        super().__init__()
        self.inp_ranks = [int_all()]
        self.out_ranks = [int_all()]
        self.extra_attrs = {"to": dtype}

    def __str__(self) -> str:
        return "Cast " + str(self.extra_attrs)

    def _requires(self, input_shapes: List[AbsTensor]) -> List[z3.ExprRef]:
        return []

    def _type_transfer(self, input_shapes: List[AbsTensor]) -> List[AbsTensor]:
        assert len(input_shapes) == 1
        return [AbsTensor(input_shapes[0].shape, self.extra_attrs["to"])]

    def deduct_inp_ranks_and_dtype(
        self, out_abs_tensor: List[AbsTensor]
    ) -> List[Tuple[int, DType]]:
        return [(out_abs_tensor[0].ndims, self.extra_attrs["to"])]

    def torch(self):
        return lambda x: x.to(dtype=self.extra_attrs["to"].torch())


@leaf
class CastF32(Cast):
    out_dtypes = [(DType.float32,)]

    def __init__(self):
        super().__init__(DType.float32)


@leaf
class CastF64(Cast):
    out_dtypes = [(DType.float64,)]

    def __init__(self):
        super().__init__(DType.float64)


@leaf
class CastI32(Cast):
    out_dtypes = [(DType.int32,)]

    def __init__(self):
        super().__init__(DType.int32)


@leaf
class CastI64(Cast):
    out_dtypes = [(DType.int64,)]

    def __init__(self):
        super().__init__(DType.int64)


@leaf
class CastBool(Cast):
    out_dtypes = [(DType.bool,)]

    def __init__(self):
        super().__init__(DType.bool)


@leaf
class Gemm(TernaryOpBase):
    # https://pytorch.org/docs/stable/generated/torch.addmm.html?highlight=addmm#torch.addmm
    in_dtypes = [(i, i, i) for i in DTYPE_NON_BOOLS]
    out_dtypes = [(i,) for i in DTYPE_NON_BOOLS]

    def __init__(self):
        super().__init__()
        self.inp_ranks = [int_until(2), (2,), (2,)]
        self.out_ranks = [(2,)]

    def _set_or_get_extra_attrs(self, dtype=None):
        if "alpha" not in self.extra_attrs:
            assert (
                dtype is not None
            ), "dtype must be specified at the first time of this call"
            alpha = random.uniform(-2, 2)
            beta = random.uniform(-2, 2)
            if dtype in DTYPE_INTS:
                beta, alpha = int(beta), int(alpha)
            self.extra_attrs["alpha"] = alpha
            self.extra_attrs["beta"] = beta
        return self.extra_attrs

    def _requires(self, input_shapes: List[AbsTensor]):
        ConstraintCheck.true(input_shapes[0].ndims <= 2)
        out_shape = self.checked_type_transfer(input_shapes)[0]
        cons = broadcast_to_cons(input_shapes[0].shape, out_shape.shape)

        # matmul constraint
        mat1, mat2 = input_shapes[1], input_shapes[2]
        cons.append(mat1.shape[1] == mat2.shape[0])
        self._set_or_get_extra_attrs(input_shapes[0].dtype.torch())
        if Z3_CONS_FLOPS:
            cons.append(nnsmith_le(self.flops(input_shapes), FLOPS_LIM))
        return cons

    def _type_transfer(self, input_shapes: List[AbsTensor]) -> List[AbsTensor]:
        mat1, mat2 = input_shapes[1], input_shapes[2]
        return [AbsTensor([mat1.shape[0], mat2.shape[1]], input_shapes[0].dtype)]

    def torch(self):
        extra_attrs = self._set_or_get_extra_attrs()
        return lambda *args: torch.addmm(
            *args, beta=extra_attrs["beta"], alpha=extra_attrs["alpha"]
        )

    def flops(self, input_shapes):
        mat1, mat2 = input_shapes[1], input_shapes[2]
        return mat1.shape[0] * mat1.shape[1] * mat2.shape[1]

    def deduct_inp_ranks_and_dtype(
        self, out_abs_tensor: List[AbsTensor]
    ) -> List[Tuple[int, DType]]:
        return [
            (random.randint(0, 2), out_abs_tensor[0].dtype),
            (2, out_abs_tensor[0].dtype),
            (2, out_abs_tensor[0].dtype),
        ]


ALL_OP_STR2TYPE = {c.__name__: c for c in ALL_OP_TYPES}
EXPANDED_OP_V0 = [Cast, Expand, TrigonometricOp, Comparator, Logical, InterpBase]
# may also consider Concat, BcastBinaryOp1
EXPANDED_OP = EXPANDED_OP_V0  # points to latest version


def config_skip_op(skip_config):
    SKIP_FOR_BKEND = {
        "trt": [
            # unsupported
            "Xor",
            "Equal:bool,bool",
            "Gemm:int32,int32,int32",
            # 'Acos:float64', 'Asin:float64', 'Atan:float64', 'Ceil:float64',
            # 'Cos:float64', 'Sin:float64', 'Tan:float64', 'GELU:float64', 'LeakyReLU:float64',
            # 'Abs:int64', 'Abs:int32',
            # # buggy, see https://github.com/NVIDIA/TensorRT/issues/1781
            # 'Less', 'Greater', 'Equal',
            # buggy
            "LegacyConstant*",
        ],
        "tvm": [],
        "tvm-cuda": [],
        "ort": [],
        "ort-cpu": [],
        "xla": [],
        "tch": [],
        "dummy": [],
    }
    print("skip config:", skip_config)
    skip_config = skip_config.split(",")
    skip = []
    for op in skip_config:
        if op.startswith("backend:"):
            skip.extend(SKIP_FOR_BKEND[op[len("backend:") :]])
        else:
            skip.append(op)
    for op_name_pattern in skip:
        skip_comb = None
        if op_name_pattern.find(":") != -1:
            op_name_pattern, skip_comb = op_name_pattern.split(":")
            skip_comb = skip_comb.split(",")
        op_name_pattern = op_name_pattern.lower()
        for op_name in fnmatch.filter(
            map(lambda x: x.__name__.lower(), ALL_OP_TYPES), op_name_pattern
        ):
            op_id = [i.__name__.lower() for i in ALL_OP_TYPES].index(op_name)
            op = ALL_OP_TYPES[op_id]
            msg = ["skip op:", op_name]
            if skip_comb is not None:  # only skip some dtype combinations
                skip_comb = tuple(map(DType.from_str, skip_comb))
                msg += ["skip dtype combination:", skip_comb]
                assert (
                    skip_comb in op.in_dtypes
                ), "combination {} not found in op({}).in_dtypes: {}".format(
                    skip_comb, op_name, op.in_dtypes
                )
                op.in_dtypes.remove(skip_comb)
            else:  # skip entire op
                msg += ["skip entire"]
                op._skip = True
            print(*msg)


def main():
    # Test shape functions
    print(len(ALL_OP_TYPES), "operators supported:")
    print(ALL_OP_STR2TYPE.keys())
    assert Reshape in ALL_OP_TYPES

    # Reshape from scalar
    lhs = AbsTensor([], DType.float32)
    s = z3.Solver()
    op = Reshape(1)
    rhs = op.checked_type_transfer([lhs])
    assert all(rhs[0].eq(AbsTensor([1], DType.float32))), (lhs, rhs)
    s.add(*op.checked_requires([lhs]))
    assert s.check() == z3.sat
    # Reduce rank 0
    abs_op = Squeeze()
    scalar = AbsTensor.from_torch(torch.tensor(10))
    assert abs_op.checked_type_transfer([scalar])[0].ndims == 0
    abs_op.checked_requires([scalar])

    # ReLU
    lhs = torch.relu(torch.randn(1, 1, 1, 1)).shape
    rhs = torch.Size(
        ReLU().checked_type_transfer([AbsTensor([1, 1, 1, 1], DType.float32)])[0].shape
    )
    assert lhs == rhs, f"{lhs} != {rhs}"

    # Add
    a = torch.randn(2, 3, 4, 5)
    b = torch.randn(2, 3, 4, 5)
    c = a + b
    assert c.shape == torch.Size(
        Add()
        .checked_type_transfer(
            [
                AbsTensor([2, 3, 4, 5], DType.float32),
                AbsTensor([2, 3, 4, 5], DType.float32),
            ]
        )[0]
        .shape
    )

    # Expand
    source_shape = (4, 1)
    a = torch.randn(source_shape)
    abs_op = ExpandLast4(expand_n=2)
    assert a.expand(2, 1, *source_shape).shape == torch.Size(
        abs_op.checked_type_transfer([AbsTensor(source_shape, DType.float32)])[0].shape
    )

    abs_op = ExpandLast1(expand_n=2)
    rhs = torch.Size(
        abs_op.checked_type_transfer([AbsTensor(list(source_shape), DType.float32)])[
            0
        ].shape
    )
    lhs = a.expand(4, 2).shape
    assert lhs == rhs, f"{lhs} != {rhs}"

    # NCHWConv2d
    source_shape = (2, 3, 24, 24)
    a = torch.randn(*source_shape)
    out = torch.conv2d(a, torch.randn(3, 3, 3, 4), stride=1, padding=1)
    assert (
        out.shape
        == NCHWConv2d(3, 3, 3, 4, 1, 1)
        .checked_type_transfer([AbsTensor(source_shape, DType.float32)])[0]
        .torch()
    )
    print(
        NCHWConv2d(3, 3, 3, 4, 1, 1).checked_type_transfer(
            [AbsTensor([2, *z3.Ints("c h w")], DType.float32)]
        )[0]
    )

    # Reshape
    source_shape = (2, 3, 4)
    target_shape = (1, 2, 3, 2, 2)
    a = torch.randn(*source_shape)
    assert (
        a.reshape(*target_shape).shape
        == Reshape(*target_shape)
        .checked_type_transfer([AbsTensor(source_shape, DType.float32)])[0]
        .torch()
    )

    # Dirty fix for z3 bug by wrapping the context using seprated functions.
    def test_reshape_symbol():  # See https://github.com/Z3Prover/z3/issues/989
        s = z3.Solver()
        v = z3.Ints("a b c d e")
        abs_op = Reshape(*v)
        cons = abs_op.checked_requires([AbsTensor(source_shape, DType.float32)])
        for c in cons:
            s.add(c)
        for c in abs_op.checked_type_transfer([AbsTensor(source_shape, DType.float32)])[
            0
        ].gt_zero():
            s.add(c)
        assert s.check() == z3.sat
        print(s.model())

    test_reshape_symbol()

    # Test `concrete` function.
    p0, p1, p2, p3, p4, p5 = z3.Ints("p0 p1 p2 p3 p4 p5")
    op = NCHWConv2d(p0, p1, p2, p3, p4, p5)
    s = z3.Solver()
    shape = AbsTensor([1, 3, 224, 224], DType.float32)
    for c in op.checked_requires([shape]):
        s.add(c)
    for c in op.checked_type_transfer([shape])[0].gt_zero():
        s.add(c)
    assert s.check() == z3.sat
    model = s.model()
    concrete_op = concretize(op, model)
    assert concrete_op.in_channels == model[p0].as_long()
    assert concrete_op.out_channels == model[p1].as_long()
    assert concrete_op.kernel_h_size == model[p2].as_long()
    assert concrete_op.kernel_w_size == model[p3].as_long()
    assert concrete_op.stride == model[p4].as_long()
    assert concrete_op.padding == model[p5].as_long()

    # Test `concrete` function.
    p0, p1, p2, p3 = z3.Ints("p0 p1 p2 p3")
    op = AvgPool2d(p0, p1, p2, p3)
    s = z3.Solver()
    shape = AbsTensor([1, 3, 224, 224], DType.float32)
    for c in op.checked_requires([shape]):
        s.add(c)
    for c in op.checked_type_transfer([shape])[0].gt_zero():
        s.add(c)
    assert s.check() == z3.sat
    model = s.model()
    concrete_op = concretize(op, model)
    assert concrete_op.kernel_h_size == model[p0].as_long()
    assert concrete_op.kernel_w_size == model[p1].as_long()
    assert concrete_op.stride == model[p2].as_long()
    assert concrete_op.padding == model[p3].as_long()


if __name__ == "__main__":
    main()
