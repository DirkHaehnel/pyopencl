"""CL device arrays."""

from __future__ import division

__copyright__ = "Copyright (C) 2009 Andreas Kloeckner"

__license__ = """
Permission is hereby granted, free of charge, to any person
obtaining a copy of this software and associated documentation
files (the "Software"), to deal in the Software without
restriction, including without limitation the rights to use,
copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the
Software is furnished to do so, subject to the following
conditions:

The above copyright notice and this permission notice shall be
included in all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES
OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT
HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY,
WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR
OTHER DEALINGS IN THE SOFTWARE.
"""




import numpy as np
import pyopencl.elementwise as elementwise
import pyopencl as cl
from pytools import memoize_method
from pyopencl.compyte.array import (
        as_strided as _as_strided,
        f_contiguous_strides as _f_contiguous_strides,
        c_contiguous_strides as _c_contiguous_strides,
        ArrayFlags as _ArrayFlags,
        get_common_dtype as _get_common_dtype_base)
from pyopencl.characterize import has_double_support




def _get_common_dtype(obj1, obj2, queue):
    return _get_common_dtype_base(obj1, obj2,
            has_double_support(queue.device))



# {{{ vector types

class vec:
    pass

def _create_vector_types():
    field_names = ["x", "y", "z", "w"]

    from pyopencl.tools import get_or_register_dtype

    vec.types = {}
    vec.type_to_scalar_and_count = {}

    counts = [2, 3, 4, 8, 16]

    for base_name, base_type in [
        ('char', np.int8),
        ('uchar', np.uint8),
        ('short', np.int16),
        ('ushort', np.uint16),
        ('int', np.int32),
        ('uint', np.uint32),
        ('long', np.int64),
        ('ulong', np.uint64),
        ('float', np.float32),
        ('double', np.float64),
        ]:
        for count in counts:
            name = "%s%d" % (base_name, count)

            titles = field_names[:count]
            if len(titles) < count:
                titles.extend((count-len(titles))*[None])

            dtype = np.dtype(dict(
                names=["s%d" % i for i in range(count)],
                formats=[base_type]*count,
                titles=titles))

            get_or_register_dtype(name, dtype)

            setattr(vec, name, dtype)

            my_field_names = ",".join(field_names[:count])
            my_field_names_defaulted = ",".join(
                    "%s=0" % fn for fn in field_names[:count])
            setattr(vec, "make_"+name,
                    staticmethod(eval(
                        "lambda %s: array((%s), dtype=my_dtype)"
                        % (my_field_names_defaulted, my_field_names),
                        dict(array=np.array, my_dtype=dtype))))

            vec.types[np.dtype(base_type), count] = dtype
            vec.type_to_scalar_and_count[dtype] = np.dtype(base_type), count

_create_vector_types()

# }}}

# {{{ helper functionality

def splay(queue, n, kernel_specific_max_wg_size=None):
    dev = queue.device
    max_work_items = _builtin_min(128, dev.max_work_group_size)

    if kernel_specific_max_wg_size is not None:
        from __builtin__ import min
        max_work_items = min(max_work_items, kernel_specific_max_wg_size)

    min_work_items = _builtin_min(32, max_work_items)
    max_groups = dev.max_compute_units * 4 * 8
    # 4 to overfill the device
    # 8 is an Nvidia constant--that's how many
    # groups fit onto one compute device

    if n < min_work_items:
        group_count = 1
        work_items_per_group = min_work_items
    elif n < (max_groups * min_work_items):
        group_count = (n + min_work_items - 1) // min_work_items
        work_items_per_group = min_work_items
    elif n < (max_groups * max_work_items):
        group_count = max_groups
        grp = (n + min_work_items - 1) // min_work_items
        work_items_per_group = ((grp + max_groups -1) // max_groups) * min_work_items
    else:
        group_count = max_groups
        work_items_per_group = max_work_items

    #print "n:%d gc:%d wipg:%d" % (n, group_count, work_items_per_group)
    return (group_count*work_items_per_group,), (work_items_per_group,)




def elwise_kernel_runner(kernel_getter):
    """Take a kernel getter of the same signature as the kernel
    and return a function that invokes that kernel.

    Assumes that the zeroth entry in *args* is an :class:`Array`.
    """
    # (Note that the 'return a function' bit is done by @decorator.)

    def kernel_runner(*args, **kwargs):
        repr_ary = args[0]
        queue = kwargs.pop("queue", None) or repr_ary.queue
        wait_for = kwargs.pop("wait_for", None)

        # wait_for must be a copy, because we modify it in-place below
        if wait_for is None:
            wait_for = []
        else:
            wait_for = list(wait_for)

        knl = kernel_getter(*args)

        gs, ls = repr_ary.get_sizes(queue,
                knl.get_work_group_info(
                    cl.kernel_work_group_info.WORK_GROUP_SIZE,
                    queue.device))

        assert isinstance(repr_ary, Array)

        actual_args = []
        for arg in args:
            if isinstance(arg, Array):
                if not arg.flags.forc:
                    raise RuntimeError("only contiguous arrays may "
                            "be used as arguments to this operation")
                actual_args.append(arg.data)
                wait_for.extend(arg.events)
            else:
                actual_args.append(arg)
        actual_args.append(repr_ary.size)

        return knl(queue, gs, ls, *actual_args, **dict(wait_for=wait_for))

    try:
       from functools import update_wrapper
    except ImportError:
        return kernel_runner
    else:
       return update_wrapper(kernel_runner, kernel_getter)




class DefaultAllocator(cl.tools.DeferredAllocator):
    def __init__(self, *args, **kwargs):
        from warnings import warn
        warn("pyopencl.array.DefaultAllocator is deprecated. "
                "It will be continue to exist throughout the 2013.x "
                "versions of PyOpenCL.",
                DeprecationWarning, 2)
        cl.tools.DeferredAllocator.__init__(self, *args, **kwargs)

def _make_strides(itemsize, shape, order):
    if order == "F":
        return _f_contiguous_strides(itemsize, shape)
    elif order == "C":
        return _c_contiguous_strides(itemsize, shape)
    else:
        raise ValueError("invalid order: %s" % order)

# }}}

# {{{ array class

class ArrayHasOffsetError(ValueError):
    """
    .. versionadded:: 2013.1
    """

class Array(object):
    """A :class:`numpy.ndarray` work-alike that stores its data and performs its
    computations on the compute device.  *shape* and *dtype* work exactly as in
    :mod:`numpy`.  Arithmetic methods in :class:`Array` support the
    broadcasting of scalars. (e.g. `array+5`)

    *cqa* must be a :class:`pyopencl.CommandQueue`. *cqa*
    specifies the queue in which the array carries out its
    computations by default. *cqa* will at some point be renamed *queue*,
    so it should be considered 'positional-only'.

    *allocator* may be `None` or a callable that, upon being called with an
    argument of the number of bytes to be allocated, returns an
    :class:`pyopencl.Buffer` object. (A :class:`pyopencl.tools.MemoryPool`
    instance is one useful example of an object to pass here.)

    .. versionchanged:: 2011.1
        Renamed *context* to *cqa*, made it general-purpose.

        All arguments beyond *order* should be considered keyword-only.

    .. attribute :: data

        The :class:`pyopencl.MemoryObject` instance created for the memory that backs
        this :class:`Array`.

        .. versionchanged:: 2013.1

            If a non-zero :attr:`offset` has been specified for this array,
            this will fail with :exc:`ArrayHasOffsetError`.

    .. attribute :: base_data

        The :class:`pyopencl.MemoryObject` instance created for the memory that backs
        this :class:`Array`. Unlike :attr:`data`, the base address of *base_data*
        is allowed to be different from the beginning of the array. The actual
        beginning is the base address of *base_data* plus :attr:`offset` in units
        of :attr:`dtype`.

        Unlike :attr:`data`, retrieving :attr:`base_data` always succeeds.

        .. versionadded:: 2013.1

    .. attribute :: offset

        See :attr:`base_data`.

        .. versionadded:: 2013.1

    .. attribute :: shape

        The tuple of lengths of each dimension in the array.

    .. attribute :: dtype

        The :class:`numpy.dtype` of the items in the GPU array.

    .. attribute :: size

        The number of meaningful entries in the array. Can also be computed by
        multiplying up the numbers in :attr:`shape`.

    .. attribute :: nbytes

        The size of the entire array in bytes. Computed as :attr:`size` times
        ``dtype.itemsize``.

    .. attribute :: strides

        Tuple of bytes to step in each dimension when traversing an array.

    .. attribute :: flags

        Return an object with attributes `c_contiguous`, `f_contiguous` and `forc`,
        which may be used to query contiguity properties in analogy to
        :attr:`numpy.ndarray.flags`.
    """

    __array_priority__ = 10

    def __init__(self, cqa, shape, dtype, order="C", allocator=None,
            data=None, offset=0, queue=None, strides=None, events=None):
        # {{{ backward compatibility

        from warnings import warn
        if queue is not None:
            warn("Passing the queue to the array through anything but the "
                    "first argument of the Array constructor is deprecated. "
                    "This will be continue to be accepted throughout the 2013.[0-6] "
                    "versions of PyOpenCL.",
                    DeprecationWarning, 2)

        if isinstance(cqa, cl.CommandQueue):
            if queue is not None:
                raise TypeError("can't specify queue in 'cqa' and "
                        "'queue' arguments")
            queue = cqa

        elif isinstance(cqa, cl.Context):
            warn("Passing a context for the 'cqa' parameter is deprecated. "
                    "This usage will be continue to be accepted throughout the 2013.[0-6] "
                    "versions of PyOpenCL.",
                    DeprecationWarning, 2)

            if queue is not None:
                raise TypeError("may not pass a context and a queue "
                        "(just pass the queue)")
            if allocator is not None:
                raise TypeError("may not pass a context and an allocator "
                        "(just pass the queue)")

        else:
            # cqa is assumed to be an allocator
            warn("Passing an allocator for the 'cqa' parameter is deprecated. "
                    "This usage will be continue to be accepted throughout the 2013.[0-6] "
                    "versions of PyOpenCL.",
                    DeprecationWarning, 2)
            if allocator is not None:
                raise TypeError("can't specify allocator in 'cqa' and "
                        "'allocator' arguments")

            allocator = cqa

        if queue is None:
            warn("Queue-less arrays are deprecated. "
                    "They will continue to work throughout the 2013.[0-6] "
                    "versions of PyOpenCL.",
                    DeprecationWarning, 2)

        # }}}

        # invariant here: allocator, queue set

        # {{{ determine shape and strides
        dtype = np.dtype(dtype)

        try:
            s = 1
            for dim in shape:
                s *= dim
        except TypeError:
            import sys
            if sys.version_info >= (3,):
                admissible_types = (int, np.integer)
            else:
                admissible_types = (int, long, np.integer)

            if not isinstance(shape, admissible_types):
                raise TypeError("shape must either be iterable or "
                        "castable to an integer")
            s = shape
            shape = (shape,)

        if isinstance(s, np.integer):
            # bombs if s is a Python integer
            s = np.asscalar(s)

        if strides is None:
            strides = _make_strides(dtype.itemsize, shape, order)

        else:
            # FIXME: We should possibly perform some plausibility
            # checking on 'strides' here.

            strides = tuple(strides)

        # }}}

        self.queue = queue
        self.shape = shape
        self.dtype = dtype
        self.strides = strides
        if events is None:
            self.events = []
        else:
            self.events = events

        self.size = s
        alloc_nbytes = self.nbytes = self.dtype.itemsize * self.size

        self.allocator = allocator

        if data is None:
            if not alloc_nbytes:
                # Work around CL not allowing zero-sized buffers.
                alloc_nbytes = 1

            if allocator is None:
                # FIXME remove me when queues become required
                if queue is not None:
                    context = queue.context

                self.base_data = cl.Buffer(context, cl.mem_flags.READ_WRITE, alloc_nbytes)
            else:
                self.base_data = self.allocator(alloc_nbytes)
        else:
            self.base_data = data

        self.offset = offset

    @property
    def context(self):
        return self.base_data.context

    @property
    def data(self):
        if self.offset:
            raise ArrayHasOffsetError()
        else:
            return self.base_data

    @property
    @memoize_method
    def flags(self):
        return _ArrayFlags(self)

    def _new_with_changes(self, data, shape=None, dtype=None,
            strides=None, offset=None, queue=None):
        if shape is None:
            shape = self.shape
        if dtype is None:
            dtype = self.dtype
        if strides is None:
            strides = self.strides
        if offset is None:
            offset = self.offset
        if queue is None:
            queue = self.queue

        if queue is not None:
            return Array(queue, shape, dtype, allocator=self.allocator,
                    strides=strides, data=data, offset=offset,
                    events=self.events)
        elif self.allocator is not None:
            return Array(self.allocator, shape, dtype, queue=queue,
                    strides=strides, data=data, offset=offset,
                    events=self.events)
        else:
            return Array(self.context, shape, dtype,
                    strides=strides, data=data, offset=offset,
                    events=self.events)

    #@memoize_method FIXME: reenable
    def get_sizes(self, queue, kernel_specific_max_wg_size=None):
        if not self.flags.forc:
            raise NotImplementedError("cannot operate on non-contiguous array")
        return splay(queue, self.size,
                kernel_specific_max_wg_size=kernel_specific_max_wg_size)

    def set(self, ary, queue=None, async=False):
        """Transfer the contents the :class:`numpy.ndarray` object *ary*
        onto the device.

        *ary* must have the same dtype and size (not necessarily shape) as *self*.
        """

        assert ary.size == self.size
        assert ary.dtype == self.dtype

        if not ary.flags.forc:
            raise RuntimeError("cannot set from non-contiguous array")

            ary = ary.copy()

        if ary.strides != self.strides:
            from warnings import warn
            warn("Setting array from one with different strides/storage order. "
                    "This will cease to work in 2013.x.",
                    stacklevel=2)

        if self.size:
            cl.enqueue_copy(queue or self.queue, self.base_data, ary,
                    device_offset=self.offset,
                    is_blocking=not async)

    def get(self, queue=None, ary=None, async=False):
        """Transfer the contents of *self* into *ary* or a newly allocated
        :mod:`numpy.ndarray`. If *ary* is given, it must have the right
        size (not necessarily shape) and dtype.
        """

        if ary is None:
            ary = np.empty(self.shape, self.dtype)

            ary = _as_strided(ary, strides=self.strides)
        else:
            if ary.size != self.size:
                raise TypeError("'ary' has non-matching size")
            if ary.dtype != self.dtype:
                raise TypeError("'ary' has non-matching type")

        assert self.flags.forc, "Array in get() must be contiguous"

        if self.size:
            cl.enqueue_copy(queue or self.queue, ary, self.base_data,
                    device_offset=self.offset,
                    is_blocking=not async)

        return ary

    def copy(self, queue=None):
        """.. versionadded:: 2013.1"""

        queue = queue or self.queue
        result = self._new_like_me()
        cl.enqueue_copy(queue, result.base_data, self.base_data,
                src_offset=self.offset, byte_count=self.nbytes)

        return result

    def __str__(self):
        return str(self.get())

    def __repr__(self):
        return repr(self.get())

    def __hash__(self):
        raise TypeError("pyopencl arrays are not hashable.")

    # {{{ kernel invocation wrappers

    @staticmethod
    @elwise_kernel_runner
    def _axpbyz(out, afac, a, bfac, b, queue=None):
        """Compute ``out = selffac * self + otherfac*other``,
        where `other` is a vector.."""
        assert out.shape == a.shape

        return elementwise.get_axpbyz_kernel(
                out.context, a.dtype, b.dtype, out.dtype)

    @staticmethod
    @elwise_kernel_runner
    def _axpbz(out, a, x, b, queue=None):
        """Compute ``z = a * x + b``, where `b` is a scalar."""
        a = np.array(a)
        b = np.array(b)
        return elementwise.get_axpbz_kernel(out.context,
                a.dtype, x.dtype, b.dtype, out.dtype)

    @staticmethod
    @elwise_kernel_runner
    def _elwise_multiply(out, a, b, queue=None):
        return elementwise.get_multiply_kernel(
                a.context, a.dtype, b.dtype, out.dtype)

    @staticmethod
    @elwise_kernel_runner
    def _rdiv_scalar(out, ary, other, queue=None):
        other = np.array(other)
        return elementwise.get_rdivide_elwise_kernel(
                out.context, ary.dtype, other.dtype, out.dtype)

    @staticmethod
    @elwise_kernel_runner
    def _div(out, self, other, queue=None):
        """Divides an array by another array."""

        assert self.shape == other.shape

        return elementwise.get_divide_kernel(self.context,
                self.dtype, other.dtype, out.dtype)

    @staticmethod
    @elwise_kernel_runner
    def _fill(result, scalar):
        return elementwise.get_fill_kernel(result.context, result.dtype)

    @staticmethod
    @elwise_kernel_runner
    def _abs(result, arg):
        if arg.dtype.kind == "c":
            from pyopencl.elementwise import complex_dtype_to_name
            fname = "%s_abs" % complex_dtype_to_name(arg.dtype)
        elif arg.dtype.kind == "f":
            fname = "fabs"
        elif arg.dtype.kind in ["u", "i"]:
            fname = "abs"
        else:
            raise TypeError("unsupported dtype in _abs()")

        return elementwise.get_unary_func_kernel(
                arg.context, fname, arg.dtype, out_dtype=result.dtype)

    @staticmethod
    @elwise_kernel_runner
    def _real(result, arg):
        from pyopencl.elementwise import complex_dtype_to_name
        fname = "%s_real" % complex_dtype_to_name(arg.dtype)
        return elementwise.get_unary_func_kernel(
                arg.context, fname, arg.dtype, out_dtype=result.dtype)

    @staticmethod
    @elwise_kernel_runner
    def _imag(result, arg):
        from pyopencl.elementwise import complex_dtype_to_name
        fname = "%s_imag" % complex_dtype_to_name(arg.dtype)
        return elementwise.get_unary_func_kernel(
                arg.context, fname, arg.dtype, out_dtype=result.dtype)

    @staticmethod
    @elwise_kernel_runner
    def _conj(result, arg):
        from pyopencl.elementwise import complex_dtype_to_name
        fname = "%s_conj" % complex_dtype_to_name(arg.dtype)
        return elementwise.get_unary_func_kernel(
                arg.context, fname, arg.dtype, out_dtype=result.dtype)

    @staticmethod
    @elwise_kernel_runner
    def _pow_scalar(result, ary, exponent):
        exponent = np.array(exponent)
        return elementwise.get_pow_kernel(result.context,
                ary.dtype, exponent.dtype, result.dtype,
                is_base_array=True, is_exp_array=False)

    @staticmethod
    @elwise_kernel_runner
    def _rpow_scalar(result, base, exponent):
        base = np.array(base)
        return elementwise.get_pow_kernel(result.context,
                base.dtype, exponent.dtype, result.dtype,
                is_base_array=False, is_exp_array=True)

    @staticmethod
    @elwise_kernel_runner
    def _pow_array(result, base, exponent):
        return elementwise.get_pow_kernel(
                result.context, base.dtype, exponent.dtype, result.dtype,
                is_base_array=True, is_exp_array=True)

    @staticmethod
    @elwise_kernel_runner
    def _reverse(result, ary):
        return elementwise.get_reverse_kernel(result.context, ary.dtype)

    @staticmethod
    @elwise_kernel_runner
    def _copy(dest, src):
        return elementwise.get_copy_kernel(
                dest.context, dest.dtype, src.dtype)

    def _new_like_me(self, dtype=None, queue=None):
        strides = None
        if dtype is None:
            dtype = self.dtype
        else:
            if dtype == self.dtype:
                strides = self.strides

        queue = queue or self.queue
        if queue is not None:
            return self.__class__(queue, self.shape, dtype,
                    allocator=self.allocator, strides=strides)
        elif self.allocator is not None:
            return self.__class__(self.allocator, self.shape, dtype,
                    strides=strides)
        else:
            return self.__class__(self.context, self.shape, dtype,
                    strides=strides)

    # }}}

    # {{{ operators

    def mul_add(self, selffac, other, otherfac, queue=None):
        """Return `selffac * self + otherfac*other`.
        """
        result = self._new_like_me(
                _get_common_dtype(self, other, queue or self.queue))
        self._axpbyz(result, selffac, self, otherfac, other)
        return result

    def __add__(self, other):
        """Add an array with an array or an array with a scalar."""

        if isinstance(other, Array):
            # add another vector
            result = self._new_like_me(_get_common_dtype(self, other, self.queue))
            self._axpbyz(result,
                    self.dtype.type(1), self,
                    other.dtype.type(1), other)
            return result
        else:
            # add a scalar
            if other == 0:
                return self
            else:
                common_dtype = _get_common_dtype(self, other, self.queue)
                result = self._new_like_me(common_dtype)
                self._axpbz(result, self.dtype.type(1), self, common_dtype.type(other))
                return result

    __radd__ = __add__

    def __sub__(self, other):
        """Substract an array from an array or a scalar from an array."""

        if isinstance(other, Array):
            result = self._new_like_me(_get_common_dtype(self, other, self.queue))
            self._axpbyz(result,
                    self.dtype.type(1), self,
                    other.dtype.type(-1), other)
            return result
        else:
            # subtract a scalar
            if other == 0:
                return self
            else:
                result = self._new_like_me(_get_common_dtype(self, other, self.queue))
                self._axpbz(result, self.dtype.type(1), self, -other)
                return result

    def __rsub__(self,other):
        """Substracts an array by a scalar or an array::

           x = n - self
        """
        common_dtype = _get_common_dtype(self, other, self.queue)
        # other must be a scalar
        result = self._new_like_me(common_dtype)
        self._axpbz(result, self.dtype.type(-1), self, common_dtype.type(other))
        return result

    def __iadd__(self, other):
        if isinstance(other, Array):
            self._axpbyz(self,
                    self.dtype.type(1), self,
                    other.dtype.type(1), other)
            return self
        else:
            self._axpbz(self, self.dtype.type(1), self, other)
            return self

    def __isub__(self, other):
        if isinstance(other, Array):
            self._axpbyz(self, self.dtype.type(1), self, other.dtype.type(-1), other)
            return self
        else:
            self._axpbz(self, self.dtype.type(1), self, -other)
            return self

    def __neg__(self):
        result = self._new_like_me()
        self._axpbz(result, -1, self, 0)
        return result

    def __mul__(self, other):
        if isinstance(other, Array):
            result = self._new_like_me(_get_common_dtype(self, other, self.queue))
            self._elwise_multiply(result, self, other)
            return result
        else:
            common_dtype = _get_common_dtype(self, other, self.queue)
            result = self._new_like_me(common_dtype)
            self._axpbz(result, common_dtype.type(other), self, self.dtype.type(0))
            return result

    def __rmul__(self, scalar):
        common_dtype = _get_common_dtype(self, scalar, self.queue)
        result = self._new_like_me(common_dtype)
        self._axpbz(result, common_dtype.type(scalar), self, self.dtype.type(0))
        return result

    def __imul__(self, other):
        if isinstance(other, Array):
            self._elwise_multiply(self, self, other)
        else:
            # scalar
            self._axpbz(self, other, self, self.dtype.type(0))

        return self

    def __div__(self, other):
        """Divides an array by an array or a scalar, i.e. ``self / other``.
        """
        if isinstance(other, Array):
            result = self._new_like_me(_get_common_dtype(self, other, self.queue))
            self._div(result, self, other)
        else:
            if other == 1:
                return self
            else:
                # create a new array for the result
                common_dtype = _get_common_dtype(self, other, self.queue)
                result = self._new_like_me(common_dtype)
                self._axpbz(result,
                        common_dtype.type(1/other), self, self.dtype.type(0))

        return result

    __truediv__ = __div__

    def __rdiv__(self,other):
        """Divides an array by a scalar or an array, i.e. ``other / self``.
        """

        if isinstance(other, Array):
            result = self._new_like_me(_get_common_dtype(self, other, self.queue))
            other._div(result, self)
        else:
            # create a new array for the result
            common_dtype = _get_common_dtype(self, other, self.queue)
            result = self._new_like_me(common_dtype)
            self._rdiv_scalar(result, self, common_dtype.type(other))

        return result

    __rtruediv__ = __rdiv__

    def fill(self, value, queue=None, wait_for=None):
        """Fill the array with *scalar*.

        :returns: *self*.
        """
        self.events.append(
                self._fill(self, value, queue=queue, wait_for=wait_for))

        return self

    def __len__(self):
        """Returns the size of the leading dimension of *self*."""
        if len(self.shape):
            return self.shape[0]
        else:
            return TypeError("scalar has no len()")

    def __abs__(self):
        """Return a `Array` of the absolute values of the elements
        of *self*.
        """

        result = self._new_like_me(self.dtype.type(0).real.dtype)
        self._abs(result, self)
        return result

    def __pow__(self, other):
        """Exponentiation by a scalar or elementwise by another
        :class:`Array`.
        """

        if isinstance(other, Array):
            assert self.shape == other.shape

            result = self._new_like_me(_get_common_dtype(self, other, self.queue))
            self._pow_array(result, self, other)
        else:
            result = self._new_like_me(_get_common_dtype(self, other, self.queue))
            self._pow_scalar(result, self, other)

        return result

    def __rpow__(self, other):
        # other must be a scalar
        common_dtype = _get_common_dtype(self, other, self.queue)
        result = self._new_like_me(common_dtype)
        self._rpow_scalar(result, common_dtype.type(other), self)
        return result

    # }}}

    def reverse(self, queue=None):
        """Return this array in reversed order. The array is treated
        as one-dimensional.
        """

        result = self._new_like_me()
        self._reverse(result, self)
        return result

    def astype(self, dtype, queue=None):
        """Return *self*, cast to *dtype*."""
        if dtype == self.dtype:
            return self

        result = self._new_like_me(dtype=dtype)
        self._copy(result, self, queue=queue)
        return result

    # {{{ rich comparisons (or rather, lack thereof)

    def __eq__(self, other):
        raise NotImplementedError

    def __ne__(self, other):
        raise NotImplementedError

    def __le__(self, other):
        raise NotImplementedError

    def __ge__(self, other):
        raise NotImplementedError

    def __lt__(self, other):
        raise NotImplementedError

    def __gt__(self, other):
        raise NotImplementedError

    # }}}

    # {{{ complex-valued business

    def real(self):
        if self.dtype.kind == "c":
            result = self._new_like_me(self.dtype.type(0).real.dtype)
            self._real(result, self)
            return result
        else:
            return self
    real = property(real, doc=".. versionadded:: 2012.1")

    def imag(self):
        if self.dtype.kind == "c":
            result = self._new_like_me(self.dtype.type(0).real.dtype)
            self._imag(result, self)
            return result
        else:
            return zeros_like(self)
    imag = property(imag, doc=".. versionadded:: 2012.1")

    def conj(self):
        """.. versionadded:: 2012.1"""
        if self.dtype.kind == "c":
            result = self._new_like_me()
            self._conj(result, self)
            return result
        else:
            return self

    # }}}

    # {{{ views

    def reshape(self, *shape, **kwargs):
        """Returns an array containing the same data with a new shape."""

        order = kwargs.pop("order", "C")
        if kwargs:
            raise TypeError("unexpected keyword arguments: %s"
                    % kwargs.keys())

        # TODO: add more error-checking, perhaps
        if isinstance(shape[0], tuple) or isinstance(shape[0], list):
            shape = tuple(shape[0])
        size = reduce(lambda x, y: x * y, shape, 1)
        if size != self.size:
            raise ValueError("total size of new array must be unchanged")

        return self._new_with_changes(data=self.data, shape=shape,
                strides=_make_strides(self.dtype.itemsize, shape, order))

    def ravel(self):
        """Returns flattened array containing the same data."""
        return self.reshape(self.size)

    def view(self, dtype=None):
        """Returns view of array with the same data. If *dtype* is different from
        current dtype, the actual bytes of memory will be reinterpreted.
        """

        if dtype is None:
            dtype = self.dtype

        old_itemsize = self.dtype.itemsize
        itemsize = np.dtype(dtype).itemsize

        if self.shape[-1] * old_itemsize % itemsize != 0:
            raise ValueError("new type not compatible with array")

        shape = self.shape[:-1] + (self.shape[-1] * old_itemsize // itemsize,)
        strides = tuple(
                s * itemsize // old_itemsize
                for s in self.strides)

        return self._new_with_changes(data=self.data, shape=shape, dtype=dtype,
                strides=strides)

    # }}

    def finish(self):
        # undoc
        cl.wait_for_events(self.events)
        del self.events[:]

    def __getitem__(self, index):
        if not isinstance(index, tuple):
            index = (index,)

        if len(index) > len(self.shape):
            raise IndexError("too many axes in index (have: %d, got: %d)" % (len(self.shape), len(index)))

        new_shape = []
        new_offset = self.offset
        new_strides = []

        for i, (subidx, shape_i, strides_i) in enumerate(
                zip(index, self.shape, self.strides)):
            if isinstance(subidx, slice):
                start, stop, stride = subidx.indices(shape_i)
                new_shape.append((stop-start)//stride)
                new_strides.append(stride)
                new_offset += strides_i*start
            elif isinstance(subidx, (int, np.integer)):
                if subidx < 0:
                    subidx += shape_i

                if not (0 <= subidx < shape_i):
                    raise IndexError("subindex in axis %d out of range" % i)

                new_offset += strides_i*subidx
            else:
                raise IndexError("invalid subindex in axis %d" % i)

        while i + 1 < len(self.shape):
            i += 1
            new_shape.append(self.shape[i])
            new_strides.append(self.strides[i])

        return self._new_with_changes(
                data=self.base_data,
                shape=tuple(new_shape),
                strides=tuple(new_strides),
                offset=new_offset,
                )

# }}}

def as_strided(ary, shape=None, strides=None):
    """Make an :class:`Array` from the given array with the given
    shape and strides.
    """

    # undocumented for the moment

    shape = shape or ary.shape
    strides = strides or ary.strides

    return Array(ary.queue, shape, ary.dtype, allocator=ary.allocator,
            data=ary.data, strides=strides)

# }}}

# {{{ creation helpers

def to_device(queue, ary, allocator=None, async=False):
    """Return a :class:`Array` that is an exact copy of the :class:`numpy.ndarray`
    instance *ary*.

    See :class:`Array` for the meaning of *allocator*.

    .. versionchanged:: 2011.1
        *context* argument was deprecated.
    """

    if ary.dtype == object:
        raise RuntimeError("to_device does not work on object arrays.")

    result = Array(queue, ary.shape, ary.dtype,
                    allocator=allocator, strides=ary.strides)
    result.set(ary, async=async)
    return result




empty = Array

def zeros(queue, shape, dtype, order="C", allocator=None):
    """Same as :func:`empty`, but the :class:`Array` is zero-initialized before
    being returned.

    .. versionchanged:: 2011.1
        *context* argument was deprecated.
    """

    result = Array(queue, shape, dtype,
            order=order, allocator=allocator)
    zero = np.zeros((), dtype)
    result.fill(zero)
    return result

def empty_like(ary):
    """Make a new, uninitialized :class:`Array` having the same properties
    as *other_ary*.
    """

    return ary._new_with_changes(data=None)

def zeros_like(ary):
    """Make a new, zero-initialized :class:`Array` having the same properties
    as *other_ary*.
    """

    result = empty_like(ary)
    zero = np.zeros((), ary.dtype)
    result.fill(zero)
    return result


@elwise_kernel_runner
def _arange_knl(result, start, step):
    return elementwise.get_arange_kernel(
            result.context, result.dtype)

def arange(queue, *args, **kwargs):
    """Create a :class:`Array` filled with numbers spaced `step` apart,
    starting from `start` and ending at `stop`.

    For floating point arguments, the length of the result is
    `ceil((stop - start)/step)`.  This rule may result in the last
    element of the result being greater than `stop`.

    *dtype*, if not specified, is taken as the largest common type
    of *start*, *stop* and *step*.

    .. versionchanged:: 2011.1
        *context* argument was deprecated.

    .. versionchanged:: 2011.2
        *allocator* keyword argument was added.
    """

    # argument processing -----------------------------------------------------

    # Yuck. Thanks, numpy developers. ;)
    from pytools import Record
    class Info(Record):
        pass

    explicit_dtype = False

    inf = Info()
    inf.start = None
    inf.stop = None
    inf.step = None
    inf.dtype = None
    inf.allocator = None
    inf.wait_for = []

    if isinstance(args[-1], np.dtype):
        dtype = args[-1]
        args = args[:-1]
        explicit_dtype = True

    argc = len(args)
    if argc == 0:
        raise ValueError, "stop argument required"
    elif argc == 1:
        inf.stop = args[0]
    elif argc == 2:
        inf.start = args[0]
        inf.stop = args[1]
    elif argc == 3:
        inf.start = args[0]
        inf.stop = args[1]
        inf.step = args[2]
    else:
        raise ValueError, "too many arguments"

    admissible_names = ["start", "stop", "step", "dtype", "allocator"]
    for k, v in kwargs.iteritems():
        if k in admissible_names:
            if getattr(inf, k) is None:
                setattr(inf, k, v)
                if k == "dtype":
                    explicit_dtype = True
            else:
                raise ValueError, "may not specify '%s' by position and keyword" % k
        else:
            raise ValueError, "unexpected keyword argument '%s'" % k

    if inf.start is None:
        inf.start = 0
    if inf.step is None:
        inf.step = 1
    if inf.dtype is None:
        inf.dtype = np.array([inf.start, inf.stop, inf.step]).dtype

    # actual functionality ----------------------------------------------------
    dtype = np.dtype(inf.dtype)
    start = dtype.type(inf.start)
    step = dtype.type(inf.step)
    stop = dtype.type(inf.stop)
    wait_for = inf.wait_for

    if not explicit_dtype:
        raise TypeError("arange requires a dtype argument")

    from math import ceil
    size = int(ceil((stop-start)/step))

    result = Array(queue, (size,), dtype, allocator=inf.allocator)
    result.events.append(
            _arange_knl(result, start, step, queue=queue, wait_for=wait_for))
    return result

# }}}

# {{{ take/put

@elwise_kernel_runner
def _take(result, ary, indices):
    return elementwise.get_take_kernel(
            result.context, result.dtype, indices.dtype)




def take(a, indices, out=None, queue=None, wait_for=None):
    """Return the :class:`Array` ``[a[indices[0]], ..., a[indices[n]]]``.
    For the moment, *a* must be a type that can be bound to a texture.
    """

    queue = queue or a.queue
    if out is None:
        out = Array(queue, indices.shape, a.dtype, allocator=a.allocator)

    assert len(indices.shape) == 1
    out.events.append(
            _take(out, a, indices, queue=queue, wait_for=wait_for))
    return out




def multi_take(arrays, indices, out=None, queue=None):
    if not len(arrays):
        return []

    assert len(indices.shape) == 1

    from pytools import single_valued
    a_dtype = single_valued(a.dtype for a in arrays)
    a_allocator = arrays[0].dtype
    context = indices.context
    queue = queue or indices.queue

    vec_count = len(arrays)

    if out is None:
        out = [Array(context, queue, indices.shape, a_dtype,
            allocator=a_allocator)
                for i in range(vec_count)]
    else:
        if len(out) != len(arrays):
            raise ValueError("out and arrays must have the same length")

    chunk_size = _builtin_min(vec_count, 10)

    def make_func_for_chunk_size(chunk_size):
        knl = elementwise.get_take_kernel(
                indices.context, a_dtype, indices.dtype,
                vec_count=chunk_size)
        knl.set_block_shape(*indices._block)
        return knl

    knl = make_func_for_chunk_size(chunk_size)

    for start_i in range(0, len(arrays), chunk_size):
        chunk_slice = slice(start_i, start_i+chunk_size)

        if start_i + chunk_size > vec_count:
            knl = make_func_for_chunk_size(vec_count-start_i)

        gs, ls = indices.get_sizes(queue,
                knl.get_work_group_info(
                    cl.kernel_work_group_info.WORK_GROUP_SIZE,
                    queue.device))

        knl(queue, gs, ls,
                indices.data,
                *([o.data for o in out[chunk_slice]]
                    + [i.data for i in arrays[chunk_slice]]
                    + [indices.size]))

    return out




def multi_take_put(arrays, dest_indices, src_indices, dest_shape=None,
        out=None, queue=None, src_offsets=None):
    if not len(arrays):
        return []

    from pytools import single_valued
    a_dtype = single_valued(a.dtype for a in arrays)
    a_allocator = arrays[0].allocator
    context = src_indices.context
    queue = queue or src_indices.queue

    vec_count = len(arrays)

    if out is None:
        out = [Array(queue, dest_shape, a_dtype, allocator=a_allocator)
                for i in range(vec_count)]
    else:
        if a_dtype != single_valued(o.dtype for o in out):
            raise TypeError("arrays and out must have the same dtype")
        if len(out) != vec_count:
            raise ValueError("out and arrays must have the same length")

    if src_indices.dtype != dest_indices.dtype:
        raise TypeError("src_indices and dest_indices must have the same dtype")

    if len(src_indices.shape) != 1:
        raise ValueError("src_indices must be 1D")

    if src_indices.shape != dest_indices.shape:
        raise ValueError("src_indices and dest_indices must have the same shape")

    if src_offsets is None:
        src_offsets_list = []
    else:
        src_offsets_list = src_offsets
        if len(src_offsets) != vec_count:
            raise ValueError("src_indices and src_offsets must have the same length")

    max_chunk_size = 10

    chunk_size = _builtin_min(vec_count, max_chunk_size)

    def make_func_for_chunk_size(chunk_size):
        return elementwise.get_take_put_kernel(context,
                a_dtype, src_indices.dtype,
                with_offsets=src_offsets is not None,
                vec_count=chunk_size)

    knl = make_func_for_chunk_size(chunk_size)

    for start_i in range(0, len(arrays), chunk_size):
        chunk_slice = slice(start_i, start_i+chunk_size)

        if start_i + chunk_size > vec_count:
            knl = make_func_for_chunk_size(vec_count-start_i)

        gs, ls = src_indices.get_sizes(queue,
                knl.get_work_group_info(
                    cl.kernel_work_group_info.WORK_GROUP_SIZE,
                    queue.device))

        knl(queue, gs, ls,
                *([o.data for o in out[chunk_slice]]
                    + [dest_indices.data, src_indices.data]
                    + [i.data for i in arrays[chunk_slice]]
                    + src_offsets_list[chunk_slice]
                    + [src_indices.size]))

    return out




def multi_put(arrays, dest_indices, dest_shape=None, out=None, queue=None):
    if not len(arrays):
        return []

    from pytools import single_valued
    a_dtype = single_valued(a.dtype for a in arrays)
    a_allocator = arrays[0].allocator
    context = dest_indices.context
    queue = queue or dest_indices.queue

    vec_count = len(arrays)

    if out is None:
        out = [Array(context, dest_shape, a_dtype, allocator=a_allocator, queue=queue)
                for i in range(vec_count)]
    else:
        if a_dtype != single_valued(o.dtype for o in out):
            raise TypeError("arrays and out must have the same dtype")
        if len(out) != vec_count:
            raise ValueError("out and arrays must have the same length")

    if len(dest_indices.shape) != 1:
        raise ValueError("dest_indices must be 1D")

    chunk_size = _builtin_min(vec_count, 10)

    def make_func_for_chunk_size(chunk_size):
        knl = elementwise.get_put_kernel(
                context,
                a_dtype, dest_indices.dtype, vec_count=chunk_size)
        return knl

    knl = make_func_for_chunk_size(chunk_size)

    for start_i in range(0, len(arrays), chunk_size):
        chunk_slice = slice(start_i, start_i+chunk_size)

        if start_i + chunk_size > vec_count:
            knl = make_func_for_chunk_size(vec_count-start_i)

        gs, ls = dest_indices.get_sizes(queue,
                knl.get_work_group_info(
                    cl.kernel_work_group_info.WORK_GROUP_SIZE,
                    queue.device))

        knl(queue, gs, ls,
                *([o.data for o in out[chunk_slice]]
                    + [dest_indices.data]
                    + [i.data for i in arrays[chunk_slice]]
                    + [dest_indices.size]))

    return out

# }}}

# {{{ conditionals

@elwise_kernel_runner
def _if_positive(result, criterion, then_, else_):
    return elementwise.get_if_positive_kernel(
            result.context, criterion.dtype, then_.dtype)




def if_positive(criterion, then_, else_, out=None, queue=None):
    """Return an array like *then_*, which, for the element at index *i*,
    contains *then_[i]* if *criterion[i]>0*, else *else_[i]*.
    """

    if not (criterion.shape == then_.shape == else_.shape):
        raise ValueError("shapes do not match")

    if not (then_.dtype == else_.dtype):
        raise ValueError("dtypes do not match")

    if out is None:
        out = empty_like(then_)
    _if_positive(out, criterion, then_, else_)
    return out

def maximum(a, b, out=None, queue=None):
    """Return the elementwise maximum of *a* and *b*."""

    # silly, but functional
    return if_positive(a.mul_add(1, b, -1, queue=queue), a, b,
            queue=queue, out=out)

def minimum(a, b, out=None, queue=None):
    """Return the elementwise minimum of *a* and *b*."""
    # silly, but functional
    return if_positive(a.mul_add(1, b, -1, queue=queue), b, a,
            queue=queue, out=out)

# }}}

# {{{ reductions
_builtin_sum = sum
_builtin_min = min
_builtin_max = max

def sum(a, dtype=None, queue=None):
    """
    .. versionadded:: 2011.1
    """
    from pyopencl.reduction import get_sum_kernel
    krnl = get_sum_kernel(a.context, dtype, a.dtype)
    return krnl(a, queue=queue)

def dot(a, b, dtype=None, queue=None):
    """
    .. versionadded:: 2011.1
    """
    from pyopencl.reduction import get_dot_kernel
    krnl = get_dot_kernel(a.context, dtype, a.dtype, b.dtype)
    return krnl(a, b, queue=queue)

def subset_dot(subset, a, b, dtype=None, queue=None):
    """
    .. versionadded:: 2011.1
    """
    from pyopencl.reduction import get_subset_dot_kernel
    krnl = get_subset_dot_kernel(a.context, dtype, subset.dtype, a.dtype, b.dtype)
    return krnl(subset, a, b, queue=queue)

def _make_minmax_kernel(what):
    def f(a, queue=None):
        from pyopencl.reduction import get_minmax_kernel
        krnl = get_minmax_kernel(a.context, what, a.dtype)
        return krnl(a,  queue=queue)

    return f

min = _make_minmax_kernel("min")
min.__doc__ = """
    .. versionadded:: 2011.1
    """

max = _make_minmax_kernel("max")
max.__doc__ = """
    .. versionadded:: 2011.1
    """

def _make_subset_minmax_kernel(what):
    def f(subset, a, queue=None):
        from pyopencl.reduction import get_subset_minmax_kernel
        krnl = get_subset_minmax_kernel(a.context, what, a.dtype, subset.dtype)
        return krnl(subset, a,  queue=queue)

    return f

subset_min = _make_subset_minmax_kernel("min")
subset_min.__doc__ = """.. versionadded:: 2011.1"""
subset_max = _make_subset_minmax_kernel("max")
subset_max.__doc__ = """.. versionadded:: 2011.1"""

# }}}

# {{{ scans

def cumsum(a, output_dtype=None, queue=None, wait_for=None, return_event=False):
    # undocumented for now

    """
    .. versionadded:: 2013.1
    """

    if output_dtype is None:
        output_dtype = a.dtype

    result = a._new_like_me(output_dtype)

    from pyopencl.scan import get_cumsum_kernel
    krnl = get_cumsum_kernel(a.context, a.dtype, output_dtype)
    evt = krnl(a, result, queue=queue, wait_for=wait_for)

    if return_event:
        return evt, result
    else:
        return result

# }}}

# vim: foldmethod=marker
