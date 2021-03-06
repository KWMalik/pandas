"""
Data structure for 1-dimensional cross-sectional and time series data
"""

# pylint: disable=E1101,E1103
# pylint: disable=W0703,W0622,W0613,W0201

from itertools import izip
import operator
from distutils.version import LooseVersion
import types

from numpy import nan, ndarray
import numpy as np
import numpy.ma as ma

from pandas.core.common import (isnull, notnull, _is_bool_indexer,
                                _default_index, _maybe_upcast,
                                _asarray_tuplesafe, is_integer_dtype)
from pandas.core.index import (Index, MultiIndex, InvalidIndexError,
                               _ensure_index, _handle_legacy_indexes)
from pandas.core.indexing import _SeriesIndexer
from pandas.tseries.index import DatetimeIndex
from pandas.tseries.period import PeriodIndex, Period
from pandas.util import py3compat
from pandas.util.terminal import get_terminal_size
import pandas.core.common as com
import pandas.core.datetools as datetools
import pandas.core.format as fmt
import pandas.core.generic as generic
import pandas.core.nanops as nanops
import pandas.lib as lib
from pandas.util.decorators import Appender, Substitution, cache_readonly

from pandas.compat.scipy import scoreatpercentile as _quantile

__all__ = ['Series', 'TimeSeries']

_np_version = np.version.short_version
_np_version_under1p6 = LooseVersion(_np_version) < '1.6'

_SHOW_WARNINGS = True

#----------------------------------------------------------------------
# Wrapper function for Series arithmetic methods


def _arith_method(op, name):
    """
    Wrapper function for Series arithmetic operations, to avoid
    code duplication.
    """
    def na_op(x, y):
        try:
            result = op(x, y)
        except TypeError:
            result = np.empty(len(x), dtype=x.dtype)
            if isinstance(y, np.ndarray):
                mask = notnull(x) & notnull(y)
                result[mask] = op(x[mask], y[mask])
            else:
                mask = notnull(x)
                result[mask] = op(x[mask], y)
            np.putmask(result, -mask, np.nan)

        return result

    def wrapper(self, other):
        from pandas.core.frame import DataFrame

        if isinstance(other, Series):
            if self.index.equals(other.index):
                name = _maybe_match_name(self, other)
                return Series(na_op(self.values, other.values),
                              index=self.index, name=name)

            join_idx, lidx, ridx = self.index.join(other.index, how='outer',
                                                   return_indexers=True)

            lvalues = self.values
            rvalues = other.values

            if lidx is not None:
                lvalues = com.take_1d(lvalues, lidx)

            if ridx is not None:
                rvalues = com.take_1d(rvalues, ridx)

            arr = na_op(lvalues, rvalues)

            name = _maybe_match_name(self, other)
            return Series(arr, index=join_idx, name=name)
        elif isinstance(other, DataFrame):
            return NotImplemented
        else:
            # scalars
            return Series(na_op(self.values, other),
                          index=self.index, name=self.name)
    return wrapper


def _comp_method(op, name):
    """
    Wrapper function for Series arithmetic operations, to avoid
    code duplication.
    """
    def na_op(x, y):
        if x.dtype == np.object_:
            if isinstance(y, list):
                y = lib.list_to_object_array(y)

            if isinstance(y, np.ndarray):
                if y.dtype != np.object_:
                    result = lib.vec_compare(x, y.astype(np.object_), op)
                else:
                    result = lib.vec_compare(x, y, op)
            else:
                result = lib.scalar_compare(x, y, op)
        else:
            result = op(x, y)

        return result

    def wrapper(self, other):
        from pandas.core.frame import DataFrame

        if isinstance(other, Series):
            name = _maybe_match_name(self, other)
            return Series(na_op(self.values, other.values),
                          index=self.index, name=name)
        elif isinstance(other, DataFrame):  # pragma: no cover
            return NotImplemented
        elif isinstance(other, np.ndarray):
            return Series(na_op(self.values, np.asarray(other)),
                          index=self.index, name=self.name)
        else:
            values = self.values
            other = lib.convert_scalar(values, other)

            if issubclass(values.dtype.type, np.datetime64):
                values = values.view('i8')

            # scalars
            res = na_op(values, other)
            if np.isscalar(res):
                raise TypeError('Could not compare %s type with Series'
                                % type(other))
            return Series(na_op(values, other),
                          index=self.index, name=self.name)
    return wrapper


def _bool_method(op, name):
    """
    Wrapper function for Series arithmetic operations, to avoid
    code duplication.
    """
    def na_op(x, y):
        try:
            result = op(x, y)
        except TypeError:
            if isinstance(y, list):
                y = lib.list_to_object_array(y)

            if isinstance(y, np.ndarray):
                if (x.dtype == np.bool_ and
                    y.dtype == np.bool_):  # pragma: no cover
                    result = op(x, y)  # when would this be hit?
                else:
                    x = com._ensure_object(x)
                    y = com._ensure_object(y)
                    result = lib.vec_binop(x, y, op)
            else:
                result = lib.scalar_binop(x, y, op)

        return result

    def wrapper(self, other):
        from pandas.core.frame import DataFrame

        if isinstance(other, Series):
            name = _maybe_match_name(self, other)
            return Series(na_op(self.values, other.values),
                          index=self.index, name=name)
        elif isinstance(other, DataFrame):
            return NotImplemented
        else:
            # scalars
            return Series(na_op(self.values, other),
                          index=self.index, name=self.name)
    return wrapper


def _radd_compat(left, right):
    radd = lambda x, y: y + x
    # GH #353, NumPy 1.5.1 workaround
    try:
        output = radd(left, right)
    except TypeError:
        cond = (_np_version_under1p6 and
                left.dtype == np.object_)
        if cond:  # pragma: no cover
            output = np.empty_like(left)
            output.flat[:] = [radd(x, right) for x in left.flat]
        else:
            raise

    return output


def _maybe_match_name(a, b):
    name = None
    if a.name == b.name:
        name = a.name
    return name


def _flex_method(op, name):
    doc = """
    Binary operator %s with support to substitute a fill_value for missing data
    in one of the inputs

    Parameters
    ----------
    other: Series or scalar value
    fill_value : None or float value, default None (NaN)
        Fill missing (NaN) values with this value. If both Series are
        missing, the result will be missing
    level : int or name
        Broadcast across a level, matching Index values on the
        passed MultiIndex level

    Returns
    -------
    result : Series
    """ % name

    @Appender(doc)
    def f(self, other, level=None, fill_value=None):
        return self._binop(other, op, level=level, fill_value=fill_value)

    f.__name__ = name
    return f


def _unbox(func):
    @Appender(func.__doc__)
    def f(self, *args, **kwargs):
        result = func(self, *args, **kwargs)
        if isinstance(result, np.ndarray) and result.ndim == 0:
            # return NumPy type
            return result.dtype.type(result.item())
        else:  # pragma: no cover
            return result
    f.__name__ = func.__name__
    return f

_stat_doc = """
Return %(name)s of values
%(na_action)s

Parameters
----------
skipna : boolean, default True
    Exclude NA/null values
level : int, default None
    If the axis is a MultiIndex (hierarchical), count along a
    particular level, collapsing into a smaller Series
%(extras)s
Returns
-------
%(shortname)s : float (or Series if level specified)
"""
_doc_exclude_na = "NA/null values are excluded"
_doc_ndarray_interface = ("Extra parameters are to preserve ndarray"
                          "interface.\n")


def _make_stat_func(nanop, name, shortname, na_action=_doc_exclude_na,
                    extras=_doc_ndarray_interface):

    @Substitution(name=name, shortname=shortname,
                  na_action=na_action, extras=extras)
    @Appender(_stat_doc)
    def f(self, axis=0, dtype=None, out=None, skipna=True, level=None):
        if level is not None:
            return self._agg_by_level(shortname, level=level, skipna=skipna)
        return nanop(self.values, skipna=skipna)
    f.__name__ = shortname
    return f

#----------------------------------------------------------------------
# Series class


class Series(np.ndarray, generic.PandasObject):
    _AXIS_NUMBERS = {
        'index': 0
    }

    _AXIS_NAMES = dict((v, k) for k, v in _AXIS_NUMBERS.iteritems())

    def __new__(cls, data=None, index=None, dtype=None, name=None,
                copy=False):
        if data is None:
            data = {}

        if index is not None:
            index = _ensure_index(index)

        if isinstance(data, Series):
            if name is None:
                name = data.name

            if index is None:
                index = data.index
            else:
                data = data.reindex(index).values
        elif isinstance(data, dict):
            if index is None:
                index = Index(sorted(data))
            try:
                if isinstance(index, DatetimeIndex):
                    # coerce back to datetime objects for lookup
                    data = lib.fast_multiget(data, index.astype('O'),
                                             default=np.nan)
                elif isinstance(index, PeriodIndex):
                    data = [data.get(i, nan) for i in index]
                else:
                    data = lib.fast_multiget(data, index.values,
                                             default=np.nan)
            except TypeError:
                data = [data.get(i, nan) for i in index]
        elif isinstance(data, types.GeneratorType):
            data = list(data)
        elif isinstance(data, set):
            raise TypeError('Set value is unordered')

        if dtype is not None:
            dtype = np.dtype(dtype)

        subarr = _sanitize_array(data, index, dtype, copy,
                                 raise_cast_failure=True)

        if not isinstance(subarr, np.ndarray):
            return subarr

        if index is None:
            index = _default_index(len(subarr))

        # Change the class of the array to be the subclass type.
        if index.is_all_dates:
            if not isinstance(index, (DatetimeIndex, PeriodIndex)):
                index = DatetimeIndex(index)
            subarr = subarr.view(TimeSeries)
        else:
            subarr = subarr.view(Series)
        subarr.index = index
        subarr.name = name

        return subarr

    @classmethod
    def from_array(cls, arr, index=None, name=None, copy=False):
        """
        Simplified alternate constructor
        """
        if copy:
            arr = arr.copy()

        klass = Series
        if index.is_all_dates:
            if not isinstance(index, (DatetimeIndex, PeriodIndex)):
                index = DatetimeIndex(index)
            klass = TimeSeries

        result = arr.view(klass)
        result.index = index
        result.name = name

        return result

    def __init__(self, data=None, index=None, dtype=None, name=None,
                 copy=False):
        """One-dimensional ndarray with axis labels (including time
series). Labels need not be unique but must be any hashable type. The object
supports both integer- and label-based indexing and provides a host of methods
for performing operations involving the index. Statistical methods from ndarray
have been overridden to automatically exclude missing data (currently
represented as NaN)

Operations between Series (+, -, /, *, **) align values based on their
associated index values-- they need not be the same length. The result
index will be the sorted union of the two indexes.

Parameters
----------
data : array-like, dict, or scalar value
    Contains data stored in Series
index : array-like or Index (1d)

    Values must be unique and hashable, same length as data. Index object
    (or other iterable of same length as data) Will default to
    np.arange(len(data)) if not provided. If both a dict and index sequence
    are used, the index will override the keys found in the dict.

dtype : numpy.dtype or None
    If None, dtype will be inferred copy : boolean, default False Copy
    input data
copy : boolean, default False
        """
        pass

    @property
    def _constructor(self):
        return Series

    @property
    def _can_hold_na(self):
        return not is_integer_dtype(self.dtype)

    def __hash__(self):
        raise TypeError('unhashable type')

    _index = None
    index = lib.SeriesIndex()

    def __array_finalize__(self, obj):
        """
        Gets called after any ufunc or other array operations, necessary
        to pass on the index.
        """
        self._index = getattr(obj, '_index', None)
        self.name = getattr(obj, 'name', None)

    def __contains__(self, key):
        return key in self.index

    def __reduce__(self):
        """Necessary for making this object picklable"""
        object_state = list(ndarray.__reduce__(self))
        subclass_state = (self.index, self.name)
        object_state[2] = (object_state[2], subclass_state)
        return tuple(object_state)

    def __setstate__(self, state):
        """Necessary for making this object picklable"""
        nd_state, own_state = state
        ndarray.__setstate__(self, nd_state)

        # backwards compat
        index, name = own_state[0], None
        if len(own_state) > 1:
            name = own_state[1]

        self.index = _handle_legacy_indexes([index])[0]
        self.name = name

    _ix = None

    @property
    def ix(self):
        if self._ix is None:
            self._ix = _SeriesIndexer(self)

        return self._ix

    def __getitem__(self, key):
        try:
            return self.index.get_value(self, key)
        except InvalidIndexError:
            pass
        except KeyError:
            if isinstance(key, tuple) and isinstance(self.index, MultiIndex):
                # kludge
                pass
            elif key is Ellipsis:
                return self
            else:
                raise
        except Exception:
            raise

        if com.is_iterator(key):
            key = list(key)

        # boolean
        # special handling of boolean data with NAs stored in object
        # arrays. Since we can't represent NA with dtype=bool
        if _is_bool_indexer(key):
            key = self._check_bool_indexer(key)
            key = np.asarray(key, dtype=bool)

        return self._get_with(key)

    def _get_with(self, key):
        # other: fancy integer or otherwise
        if isinstance(key, slice):
            from pandas.core.indexing import _is_index_slice

            idx_type = self.index.inferred_type
            if idx_type == 'floating':
                indexer = self.ix._convert_to_indexer(key, axis=0)
            elif idx_type == 'integer' or _is_index_slice(key):
                indexer = key
            else:
                indexer = self.ix._convert_to_indexer(key, axis=0)
            return self._get_values(indexer)
        else:
            if isinstance(key, tuple):
                try:
                    return self._get_values_tuple(key)
                except:
                    if len(key) == 1:
                        key = key[0]
                        if isinstance(key, slice):
                            return self._get_values(key)
                    raise

            if not isinstance(key, (list, np.ndarray)):  # pragma: no cover
                key = list(key)

            if isinstance(key, Index):
                key_type = lib.infer_dtype(key.values)
            else:
                key_type = lib.infer_dtype(key)

            if key_type == 'integer':
                if self.index.inferred_type == 'integer':
                    return self.reindex(key)
                else:
                    return self._get_values(key)
            elif key_type == 'boolean':
                return self._get_values(key)
            else:
                try:
                    return self.reindex(key)
                except Exception:
                    # [slice(0, 5, None)] will break if you convert to ndarray,
                    # e.g. as requested by np.median
                    # hack
                    if isinstance(key[0], slice):
                        return self._get_values(key)
                    raise

    def _get_values_tuple(self, key):
        # mpl hackaround
        if any(k is None for k in key):
            return self._get_values(key)

        if not isinstance(self.index, MultiIndex):
            raise ValueError('Can only tuple-index with a MultiIndex')

        # If key is contained, would have returned by now
        indexer, new_index = self.index.get_loc_level(key)
        return Series(self.values[indexer], index=new_index, name=self.name)

    def _get_values(self, indexer):
        try:
            return Series(self.values[indexer], index=self.index[indexer],
                          name=self.name)
        except Exception:
            return self.values[indexer]

    def where(self, cond, other=nan, inplace=False):
        """
        Return a Series where cond is True; otherwise values are from other

        Parameters
        ----------
        cond: boolean Series or array
        other: scalar or Series

        Returns
        -------
        wh: Series
        """
        if isinstance(cond, Series):
            cond = cond.reindex(self.index, fill_value=True)
        if not hasattr(cond, 'shape'):
            raise ValueError('where requires an ndarray like object for its '
                             'condition')
        if len(cond) != len(self):
            raise ValueError('condition must have same length as series')

        ser = self if inplace else self.copy()
        if not isinstance(other, (list, tuple, np.ndarray)):
            ser._set_with(~cond, other)
            return ser

        if isinstance(other, Series):
            other = other.reindex(ser.index)
        if len(other) != len(ser):
            raise ValueError('Length of replacements must equal series length')

        np.putmask(ser, ~cond, other)

        return ser

    def mask(self, cond):
        """
        Returns copy of self whose values are replaced with nan if the
        inverted condition is True

        Parameters
        ----------
        cond: boolean Series or array

        Returns
        -------
        wh: Series
        """
        return self.where(~cond, nan)

    def __setitem__(self, key, value):
        try:
            try:
                self.index._engine.set_value(self, key, value)
                return
            except KeyError:
                values = self.values
                values[self.index.get_loc(key)] = value
                return
        except KeyError:
            if (com.is_integer(key)
                and not self.index.inferred_type == 'integer'):

                values[key] = value
                return
            elif key is Ellipsis:
                self[:] = value
                return

            raise KeyError('%s not in this series!' % str(key))
        except TypeError, e:
            # python 3 type errors should be raised
            if 'unorderable' in str(e):  # pragma: no cover
                raise IndexError(key)
            # Could not hash item

        if _is_bool_indexer(key):
            key = self._check_bool_indexer(key)
            key = np.asarray(key, dtype=bool)

        self._set_with(key, value)

    def _set_with(self, key, value):
        # other: fancy integer or otherwise
        if isinstance(key, slice):
            from pandas.core.indexing import _is_index_slice
            if self.index.inferred_type == 'integer' or _is_index_slice(key):
                indexer = key
            else:
                indexer = self.ix._convert_to_indexer(key, axis=0)
            return self._set_values(indexer, value)
        else:
            if isinstance(key, tuple):
                try:
                    self._set_values(key, value)
                except Exception:
                    pass

            if not isinstance(key, (list, np.ndarray)):
                key = list(key)

            if isinstance(key, Index):
                key_type = lib.infer_dtype(key.values)
            else:
                key_type = lib.infer_dtype(key)

            if key_type == 'integer':
                if self.index.inferred_type == 'integer':
                    self._set_labels(key, value)
                else:
                    return self._set_values(key, value)
            elif key_type == 'boolean':
                self._set_values(key, value)
            else:
                self._set_labels(key, value)

    def _set_labels(self, key, value):
        if isinstance(key, Index):
            key = key.values
        else:
            key = _asarray_tuplesafe(key)
        indexer = self.index.get_indexer(key)
        mask = indexer == -1
        if mask.any():
            raise ValueError('%s not contained in the index'
                             % str(key[mask]))
        self._set_values(indexer, value)

    def _set_values(self, key, value):
        values = self.values
        values[key] = lib.convert_scalar(values, value)

    # help out SparseSeries
    _get_val_at = ndarray.__getitem__

    def __getslice__(self, i, j):
        if i < 0:
            i = 0
        if j < 0:
            j = 0
        slobj = slice(i, j)
        return self.__getitem__(slobj)

    def _check_bool_indexer(self, key):
        # boolean indexing, need to check that the data are aligned, otherwise
        # disallowed
        result = key
        if isinstance(key, Series) and key.dtype == np.bool_:
            if not key.index.equals(self.index):
                result = key.reindex(self.index)

        if isinstance(result, np.ndarray) and result.dtype == np.object_:
            mask = isnull(result)
            if mask.any():
                raise ValueError('cannot index with vector containing '
                                 'NA / NaN values')

        return result

    def __setslice__(self, i, j, value):
        """Set slice equal to given value(s)"""
        if i < 0:
            i = 0
        if j < 0:
            j = 0
        slobj = slice(i, j)
        return self.__setitem__(slobj, value)

    def astype(self, dtype):
        """
        See numpy.ndarray.astype
        """
        casted = com._astype_nansafe(self.values, dtype)
        return self._constructor(casted, index=self.index, name=self.name)

    def convert_objects(self):
        """
        Attempt to infer better dtype

        Returns
        -------
        converted : Series
        """
        if self.dtype == np.object_:
            return Series(lib.maybe_convert_objects(self, convert_datetime=1),
                          self.index)
        return self

    def repeat(self, reps):
        """
        See ndarray.repeat
        """
        new_index = self.index.repeat(reps)
        new_values = self.values.repeat(reps)
        return Series(new_values, index=new_index, name=self.name)

    def reshape(self, newshape, order='C'):
        """
        See numpy.ndarray.reshape
        """
        if isinstance(newshape, tuple) and len(newshape) > 1:
            return self.values.reshape(newshape, order=order)
        else:
            return ndarray.reshape(self, newshape, order)

    def get(self, label, default=None):
        """
        Returns value occupying requested label, default to specified
        missing value if not present. Analogous to dict.get

        Parameters
        ----------
        label : object
            Label value looking for
        default : object, optional
            Value to return if label not in index

        Returns
        -------
        y : scalar
        """
        try:
            return self.get_value(label)
        except KeyError:
            return default

    def iget_value(self, i):
        """
        Return the i-th value or values in the Series by location

        Parameters
        ----------
        i : int, slice, or sequence of integers

        Returns
        -------
        value : scalar (int) or Series (slice, sequence)
        """
        try:
            return lib.get_value_at(self, i)
        except IndexError:
            raise
        except:
            if isinstance(i, slice):
                return self[i]
            else:
                label = self.index[i]
                if isinstance(label, Index):
                    return self.reindex(label)
                else:
                    return lib.get_value_at(self, i)

    iget = iget_value
    irow = iget_value

    def get_value(self, label):
        """
        Quickly retrieve single value at passed index label

        Parameters
        ----------
        index : label

        Returns
        -------
        value : scalar value
        """
        return self.index._engine.get_value(self, label)

    def set_value(self, label, value):
        """
        Quickly set single value at passed label. If label is not contained, a
        new object is created with the label placed at the end of the result
        index

        Parameters
        ----------
        label : object
            Partial indexing with MultiIndex not allowed
        value : object
            Scalar value

        Returns
        -------
        series : Series
            If label is contained, will be reference to calling Series,
            otherwise a new object
        """
        try:
            self.index._engine.set_value(self, label, value)
            return self
        except KeyError:
            if len(self.index) == 0:
                new_index = Index([label])
            else:
                new_index = self.index.insert(len(self), label)

            new_values = np.concatenate([self.values, [value]])
            return Series(new_values, index=new_index, name=self.name)

    def reset_index(self, level=None, drop=False, name=None, inplace=False):
        """
        Analogous to the DataFrame.reset_index function, see docstring there.

        Parameters
        ----------
        level : int, str, tuple, or list, default None
            Only remove the given levels from the index. Removes all levels by
            default
        drop : boolean, default False
            Do not try to insert index into dataframe columns
        name : object, default None
            The name of the column corresponding to the Series values
        inplace : boolean, default False
            Modify the Series in place (do not create a new object)

        Returns
        ----------
        resetted : DataFrame, or Series if drop == True
        """
        if drop:
            new_index = np.arange(len(self))
            if level is not None and isinstance(self.index, MultiIndex):
                if not isinstance(level, (tuple, list)):
                    level = [level]
                level = [self.index._get_level_number(lev) for lev in level]
                if len(level) < len(self.index.levels):
                    new_index = self.index.droplevel(level)

            if inplace:
                self.index = new_index
                # set name if it was passed, otherwise, keep the previous name
                self.name = name or self.name
                return self
            else:
                return Series(self.values.copy(), index=new_index,
                              name=self.name)
        elif inplace:
            raise TypeError('Cannot reset_index inplace on a Series '
                            'to create a DataFrame')
        else:
            from pandas.core.frame import DataFrame
            if name is None:
                df = DataFrame(self)
            else:
                df = DataFrame({name: self})

            return df.reset_index(level=level, drop=drop)

    def __repr__(self):
        """Clean string representation of a Series"""
        width, height = get_terminal_size()
        max_rows = (height if fmt.print_config.max_rows == 0
                    else fmt.print_config.max_rows)
        if len(self.index) > (max_rows or 1000):
            result = self._tidy_repr(min(30, max_rows - 4))
        elif len(self.index) > 0:
            result = self._get_repr(print_header=True,
                                    length=len(self) > 50,
                                    name=True)
        else:
            result = '%s' % ndarray.__repr__(self)

        if py3compat.PY3:
            return unicode(result)
        return com.console_encode(result)

    def _tidy_repr(self, max_vals=20):
        num = max_vals // 2
        head = self[:num]._get_repr(print_header=True, length=False,
                                    name=False)
        tail = self[-(max_vals - num):]._get_repr(print_header=False,
                                                  length=False,
                                                  name=False)
        result = head + '\n...\n' + tail
        return '%s\n%s' % (result, self._repr_footer())

    def _repr_footer(self):
        namestr = "Name: %s, " % com.pprint_thing(self.name) if self.name is not None else ""
        return '%sLength: %d' % (namestr, len(self))

    def to_string(self, buf=None, na_rep='NaN', float_format=None,
                  nanRep=None, length=False, name=False):
        """
        Render a string representation of the Series

        Parameters
        ----------
        buf : StringIO-like, optional
            buffer to write to
        na_rep : string, optional
            string representation of NAN to use, default 'NaN'
        float_format : one-parameter function, optional
            formatter function to apply to columns' elements if they are floats
            default None
        length : boolean, default False
            Add the Series length
        name : boolean, default False
            Add the Series name (which may be None)

        Returns
        -------
        formatted : string (if not buffer passed)
        """

        if nanRep is not None:  # pragma: no cover
            import warnings
            warnings.warn("nanRep is deprecated, use na_rep", FutureWarning)
            na_rep = nanRep

        the_repr = self._get_repr(float_format=float_format, na_rep=na_rep,
                                  length=length, name=name)
        if buf is None:
            return the_repr
        else:
            print >> buf, the_repr

    def _get_repr(self, name=False, print_header=False, length=True,
                  na_rep='NaN', float_format=None):
        formatter = fmt.SeriesFormatter(self, name=name, header=print_header,
                                        length=length, na_rep=na_rep,
                                        float_format=float_format)
        return formatter.to_string()

    def __str__(self):
        return repr(self)

    def __iter__(self):
        if np.issubdtype(self.dtype, np.datetime64):
            return (lib.Timestamp(x) for x in self.values)
        else:
            return iter(self.values)

    def iteritems(self, index=True):
        """
        Lazily iterate over (index, value) tuples
        """
        return izip(iter(self.index), iter(self))

    iterkv = iteritems
    if py3compat.PY3:  # pragma: no cover
        items = iteritems

    #----------------------------------------------------------------------
    #   Arithmetic operators

    __add__ = _arith_method(operator.add, '__add__')
    __sub__ = _arith_method(operator.sub, '__sub__')
    __mul__ = _arith_method(operator.mul, '__mul__')
    __truediv__ = _arith_method(operator.truediv, '__truediv__')
    __floordiv__ = _arith_method(operator.floordiv, '__floordiv__')
    __pow__ = _arith_method(operator.pow, '__pow__')

    __radd__ = _arith_method(_radd_compat, '__add__')
    __rmul__ = _arith_method(operator.mul, '__mul__')
    __rsub__ = _arith_method(lambda x, y: y - x, '__sub__')
    __rtruediv__ = _arith_method(lambda x, y: y / x, '__truediv__')
    __rfloordiv__ = _arith_method(lambda x, y: y // x, '__floordiv__')
    __rpow__ = _arith_method(lambda x, y: y ** x, '__pow__')

    # comparisons
    __gt__ = _comp_method(operator.gt, '__gt__')
    __ge__ = _comp_method(operator.ge, '__ge__')
    __lt__ = _comp_method(operator.lt, '__lt__')
    __le__ = _comp_method(operator.le, '__le__')
    __eq__ = _comp_method(operator.eq, '__eq__')
    __ne__ = _comp_method(operator.ne, '__ne__')

    # binary logic
    __or__ = _bool_method(operator.or_, '__or__')
    __and__ = _bool_method(operator.and_, '__and__')
    __xor__ = _bool_method(operator.xor, '__xor__')

    # Inplace operators
    __iadd__ = __add__
    __isub__ = __sub__
    __imul__ = __mul__
    __itruediv__ = __truediv__
    __ifloordiv__ = __floordiv__
    __ipow__ = __pow__

    # Python 2 division operators
    if not py3compat.PY3:
        __div__ = _arith_method(operator.div, '__div__')
        __rdiv__ = _arith_method(lambda x, y: y / x, '__div__')
        __idiv__ = __div__

    #----------------------------------------------------------------------
    # unbox reductions

    all = _unbox(np.ndarray.all)
    any = _unbox(np.ndarray.any)

    #----------------------------------------------------------------------
    # Misc public methods

    def keys(self):
        "Alias for index"
        return self.index

    # alas, I wish this worked
    # values = lib.ValuesProperty()

    @property
    def values(self):
        """
        Return Series as ndarray

        Returns
        -------
        arr : numpy.ndarray
        """
        return self.view(ndarray)

    def copy(self, order='C'):
        """
        Return new Series with copy of underlying values

        Returns
        -------
        cp : Series
        """
        return Series(self.values.copy(order), index=self.index,
                      name=self.name)

    def to_dict(self):
        """
        Convert Series to {label -> value} dict

        Returns
        -------
        value_dict : dict
        """
        return dict(self.iteritems())

    def to_sparse(self, kind='block', fill_value=None):
        """
        Convert Series to SparseSeries

        Parameters
        ----------
        kind : {'block', 'integer'}
        fill_value : float, defaults to NaN (missing)

        Returns
        -------
        sp : SparseSeries
        """
        from pandas.core.sparse import SparseSeries
        return SparseSeries(self, kind=kind, fill_value=fill_value,
                            name=self.name)

    def head(self, n=5):
        """Returns first n rows of Series
        """
        return self[:n]

    def tail(self, n=5):
        """Returns last n rows of Series
        """
        return self[-n:]

    #----------------------------------------------------------------------
    # Statistics, overridden ndarray methods

    # TODO: integrate bottleneck

    def count(self, level=None):
        """
        Return number of non-NA/null observations in the Series

        Parameters
        ----------
        level : int, default None
            If the axis is a MultiIndex (hierarchical), count along a
            particular level, collapsing into a smaller Series

        Returns
        -------
        nobs : int or Series (if level specified)
        """
        if level is not None:
            mask = notnull(self.values)

            if isinstance(level, basestring):
                level = self.index._get_level_number(level)

            level_index = self.index.levels[level]

            if len(self) == 0:
                return Series(0, index=level_index)

            # call cython function
            max_bin = len(level_index)
            labels = com._ensure_int64(self.index.labels[level])
            counts = lib.count_level_1d(mask.view(np.uint8),
                                        labels, max_bin)
            return Series(counts, index=level_index)

        return notnull(self.values).sum()

    def value_counts(self):
        """
        Returns Series containing counts of unique values. The resulting Series
        will be in descending order so that the first element is the most
        frequently-occurring element. Excludes NA values

        Returns
        -------
        counts : Series
        """
        from pandas.core.algorithms import value_counts
        return value_counts(self.values, sort=True, ascending=False)

    def unique(self):
        """
        Return array of unique values in the Series. Significantly faster than
        numpy.unique

        Returns
        -------
        uniques : ndarray
        """
        return nanops.unique1d(self.values)

    def nunique(self):
        """
        Return count of unique elements in the Series

        Returns
        -------
        nunique : int
        """
        return len(self.value_counts())

    sum = _make_stat_func(nanops.nansum, 'sum', 'sum')
    mean = _make_stat_func(nanops.nanmean, 'mean', 'mean')
    median = _make_stat_func(nanops.nanmedian, 'median', 'median', extras='')
    prod = _make_stat_func(nanops.nanprod, 'product', 'prod', extras='')

    @Substitution(name='mean absolute deviation', shortname='mad',
                  na_action=_doc_exclude_na, extras='')
    @Appender(_stat_doc)
    def mad(self, skipna=True, level=None):
        if level is not None:
            return self._agg_by_level('mad', level=level, skipna=skipna)

        demeaned = self - self.mean(skipna=skipna)
        return np.abs(demeaned).mean(skipna=skipna)

    @Substitution(name='minimum', shortname='min',
                  na_action=_doc_exclude_na, extras='')
    @Appender(_stat_doc)
    def min(self, axis=None, out=None, skipna=True, level=None):
        if level is not None:
            return self._agg_by_level('min', level=level, skipna=skipna)
        return nanops.nanmin(self.values, skipna=skipna)

    @Substitution(name='maximum', shortname='max',
                  na_action=_doc_exclude_na, extras='')
    @Appender(_stat_doc)
    def max(self, axis=None, out=None, skipna=True, level=None):
        if level is not None:
            return self._agg_by_level('max', level=level, skipna=skipna)
        return nanops.nanmax(self.values, skipna=skipna)

    @Substitution(name='standard deviation', shortname='stdev',
                  na_action=_doc_exclude_na, extras='')
    @Appender(_stat_doc +
        """
        Normalized by N-1 (unbiased estimator).
        """)
    def std(self, axis=None, dtype=None, out=None, ddof=1, skipna=True,
            level=None):
        if level is not None:
            return self._agg_by_level('std', level=level, skipna=skipna,
                                      ddof=ddof)
        return np.sqrt(nanops.nanvar(self.values, skipna=skipna, ddof=ddof))

    @Substitution(name='variance', shortname='var',
                  na_action=_doc_exclude_na, extras='')
    @Appender(_stat_doc +
        """
        Normalized by N-1 (unbiased estimator).
        """)
    def var(self, axis=None, dtype=None, out=None, ddof=1, skipna=True,
            level=None):
        if level is not None:
            return self._agg_by_level('var', level=level, skipna=skipna,
                                      ddof=ddof)
        return nanops.nanvar(self.values, skipna=skipna, ddof=ddof)

    @Substitution(name='unbiased skewness', shortname='skew',
                  na_action=_doc_exclude_na, extras='')
    @Appender(_stat_doc)
    def skew(self, skipna=True, level=None):
        if level is not None:
            return self._agg_by_level('skew', level=level, skipna=skipna)

        return nanops.nanskew(self.values, skipna=skipna)

    @Substitution(name='unbiased kurtosis', shortname='kurt',
                  na_action=_doc_exclude_na, extras='')
    @Appender(_stat_doc)
    def kurt(self, skipna=True, level=None):
        if level is not None:
            return self._agg_by_level('kurt', level=level, skipna=skipna)

        return nanops.nankurt(self.values, skipna=skipna)

    def _agg_by_level(self, name, level=0, skipna=True, **kwds):
        grouped = self.groupby(level=level)
        if hasattr(grouped, name) and skipna:
            return getattr(grouped, name)(**kwds)
        method = getattr(type(self), name)
        applyf = lambda x: method(x, skipna=skipna, **kwds)
        return grouped.aggregate(applyf)

    def idxmin(self, axis=None, out=None, skipna=True):
        """
        Index of first occurrence of minimum of values.

        Parameters
        ----------
        skipna : boolean, default True
            Exclude NA/null values

        Returns
        -------
        idxmin : Index of minimum of values
        """
        i = nanops.nanargmin(self.values, skipna=skipna)
        if i == -1:
            return np.nan
        return self.index[i]

    def idxmax(self, axis=None, out=None, skipna=True):
        """
        Index of first occurrence of maximum of values.

        Parameters
        ----------
        skipna : boolean, default True
            Exclude NA/null values

        Returns
        -------
        idxmax : Index of minimum of values
        """
        i = nanops.nanargmax(self.values, skipna=skipna)
        if i == -1:
            return np.nan
        return self.index[i]

    def cumsum(self, axis=0, dtype=None, out=None, skipna=True):
        """
        Cumulative sum of values. Preserves locations of NaN values

        Extra parameters are to preserve ndarray interface.

        Parameters
        ----------
        skipna : boolean, default True
            Exclude NA/null values

        Returns
        -------
        cumsum : Series
        """
        arr = self.values.copy()

        do_mask = skipna and not issubclass(self.dtype.type, np.integer)
        if do_mask:
            mask = isnull(arr)
            np.putmask(arr, mask, 0.)

        result = arr.cumsum()

        if do_mask:
            np.putmask(result, mask, np.nan)

        return Series(result, index=self.index)

    def cumprod(self, axis=0, dtype=None, out=None, skipna=True):
        """
        Cumulative product of values. Preserves locations of NaN values

        Extra parameters are to preserve ndarray interface.

        Parameters
        ----------
        skipna : boolean, default True
            Exclude NA/null values

        Returns
        -------
        cumprod : Series
        """
        arr = self.values.copy()

        do_mask = skipna and not issubclass(self.dtype.type, np.integer)
        if do_mask:
            mask = isnull(arr)
            np.putmask(arr, mask, 1.)

        result = arr.cumprod()

        if do_mask:
            np.putmask(result, mask, np.nan)

        return Series(result, index=self.index)

    def cummax(self, axis=0, dtype=None, out=None, skipna=True):
        """
        Cumulative max of values. Preserves locations of NaN values

        Extra parameters are to preserve ndarray interface.

        Parameters
        ----------
        skipna : boolean, default True
            Exclude NA/null values

        Returns
        -------
        cummax : Series
        """
        arr = self.values.copy()

        do_mask = skipna and not issubclass(self.dtype.type, np.integer)
        if do_mask:
            mask = isnull(arr)
            np.putmask(arr, mask, -np.inf)

        result = np.maximum.accumulate(arr)

        if do_mask:
            np.putmask(result, mask, np.nan)

        return Series(result, index=self.index)

    def cummin(self, axis=0, dtype=None, out=None, skipna=True):
        """
        Cumulative min of values. Preserves locations of NaN values

        Extra parameters are to preserve ndarray interface.

        Parameters
        ----------
        skipna : boolean, default True
            Exclude NA/null values

        Returns
        -------
        cummin : Series
        """
        arr = self.values.copy()

        do_mask = skipna and not issubclass(self.dtype.type, np.integer)
        if do_mask:
            mask = isnull(arr)
            np.putmask(arr, mask, np.inf)

        result = np.minimum.accumulate(arr)

        if do_mask:
            np.putmask(result, mask, np.nan)

        return Series(result, index=self.index)

    @Appender(np.ndarray.round.__doc__)
    def round(self, decimals=0, out=None):
        """

        """
        result = self.values.round(decimals, out=out)
        if out is None:
            result = Series(result, index=self.index, name=self.name)

        return result

    def quantile(self, q=0.5):
        """
        Return value at the given quantile, a la scoreatpercentile in
        scipy.stats

        Parameters
        ----------
        q : quantile
            0 <= q <= 1

        Returns
        -------
        quantile : float
        """
        valid_values = self.dropna().values
        if len(valid_values) == 0:
            return np.nan
        return _quantile(valid_values, q * 100)

    def ptp(self, axis=None, out=None):
        return self.values.ptp(axis, out)

    def describe(self, percentile_width=50):
        """
        Generate various summary statistics of Series, excluding NaN
        values. These include: count, mean, std, min, max, and
        lower%/50%/upper% percentiles

        Parameters
        ----------
        percentile_width : float, optional
            width of the desired uncertainty interval, default is 50,
            which corresponds to lower=25, upper=75

        Returns
        -------
        desc : Series
        """
        try:
            from collections import Counter
        except ImportError:  # pragma: no cover
            # For Python < 2.7, we include a local copy of this:
            from pandas.util.counter import Counter

        if self.dtype == object:
            names = ['count', 'unique']
            objcounts = Counter(self.dropna().values)
            data = [self.count(), len(objcounts)]
            if data[1] > 0:
                names += ['top', 'freq']
                top, freq = objcounts.most_common(1)[0]
                data += [top, freq]

        elif issubclass(self.dtype.type, np.datetime64):
            names = ['count', 'unique']
            asint = self.dropna().view('i8')
            objcounts = Counter(asint)
            data = [self.count(), len(objcounts)]
            if data[1] > 0:
                top, freq = objcounts.most_common(1)[0]
                names += ['first', 'last', 'top', 'freq']
                data += [lib.Timestamp(asint.min()),
                         lib.Timestamp(asint.max()),
                         lib.Timestamp(top), freq]
        else:

            lb = .5 * (1. - percentile_width / 100.)
            ub = 1. - lb

            def pretty_name(x):
                x *= 100
                if x == int(x):
                    return '%.0f%%' % x
                else:
                    return '%.1f%%' % x

            names = ['count']
            data = [self.count()]
            names += ['mean', 'std', 'min', pretty_name(lb), '50%',
                    pretty_name(ub), 'max']
            data += [self.mean(), self.std(), self.min(),
                    self.quantile(lb), self.median(), self.quantile(ub),
                    self.max()]

        return Series(data, index=names)

    def corr(self, other, method='pearson'):
        """
        Compute correlation two Series, excluding missing values

        Parameters
        ----------
        other : Series
        method : {'pearson', 'kendall', 'spearman'}
            pearson : standard correlation coefficient
            kendall : Kendall Tau correlation coefficient
            spearman : Spearman rank correlation

        Returns
        -------
        correlation : float
        """
        this, other = self.align(other, join='inner', copy=False)
        return nanops.nancorr(this.values, other.values, method=method)

    def cov(self, other):
        """
        Compute covariance with Series, excluding missing values

        Parameters
        ----------
        other : Series

        Returns
        -------
        covariance : float

        Normalized by N-1 (unbiased estimator).
        """
        this, other = self.align(other, join='inner')
        if len(this) == 0:
            return np.nan
        return nanops.nancov(this.values, other.values)

    def diff(self, periods=1):
        """
        1st discrete difference of object

        Parameters
        ----------
        periods : int, default 1
            Periods to shift for forming difference

        Returns
        -------
        diffed : Series
        """
        result = com.diff(self.values, periods)
        return Series(result, self.index, name=self.name)

    def autocorr(self):
        """
        Lag-1 autocorrelation

        Returns
        -------
        autocorr : float
        """
        return self.corr(self.shift(1))

    def clip(self, lower=None, upper=None, out=None):
        """
        Trim values at input threshold(s)

        Parameters
        ----------
        lower : float, default None
        upper : float, default None

        Returns
        -------
        clipped : Series
        """
        if out is not None:  # pragma: no cover
            raise Exception('out argument is not supported yet')

        result = self
        if lower is not None:
            result = result.clip_lower(lower)
        if upper is not None:
            result = result.clip_upper(upper)

        return result

    def clip_upper(self, threshold):
        """
        Return copy of series with values above given value truncated

        See also
        --------
        clip

        Returns
        -------
        clipped : Series
        """
        return np.where(self > threshold, threshold, self)

    def clip_lower(self, threshold):
        """
        Return copy of series with values below given value truncated

        See also
        --------
        clip

        Returns
        -------
        clipped : Series
        """
        return np.where(self < threshold, threshold, self)

#------------------------------------------------------------------------------
# Combination

    def append(self, to_append, verify_integrity=False):
        """
        Concatenate two or more Series. The indexes must not overlap

        Parameters
        ----------
        to_append : Series or list/tuple of Series
        verify_integrity : boolean, default False
            If True, raise Exception on creating index with duplicates

        Returns
        -------
        appended : Series
        """
        from pandas.tools.merge import concat
        if isinstance(to_append, (list, tuple)):
            to_concat = [self] + to_append
        else:
            to_concat = [self, to_append]
        return concat(to_concat, ignore_index=False,
                      verify_integrity=verify_integrity)

    def _binop(self, other, func, level=None, fill_value=None):
        """
        Perform generic binary operation with optional fill value

        Parameters
        ----------
        other : Series
        func : binary operator
        fill_value : float or object
            Value to substitute for NA/null values. If both Series are NA in a
            location, the result will be NA regardless of the passed fill value
        level : int or name
            Broadcast across a level, matching Index values on the
            passed MultiIndex level

        Returns
        -------
        combined : Series
        """
        if not isinstance(other, Series):
            raise AssertionError('Other operand must be Series')

        new_index = self.index
        this = self

        if not self.index.equals(other.index):
            this, other = self.align(other, level=level, join='outer')
            new_index = this.index

        this_vals = this.values
        other_vals = other.values

        if fill_value is not None:
            this_mask = isnull(this_vals)
            other_mask = isnull(other_vals)
            this_vals = this_vals.copy()
            other_vals = other_vals.copy()

            # one but not both
            mask = this_mask ^ other_mask
            this_vals[this_mask & mask] = fill_value
            other_vals[other_mask & mask] = fill_value

        result = func(this_vals, other_vals)
        name = _maybe_match_name(self, other)
        return Series(result, index=new_index, name=name)

    add = _flex_method(operator.add, 'add')
    sub = _flex_method(operator.sub, 'subtract')
    mul = _flex_method(operator.mul, 'multiply')
    try:
        div = _flex_method(operator.div, 'divide')
    except AttributeError:  # pragma: no cover
        # Python 3
        div = _flex_method(operator.truediv, 'divide')

    def combine(self, other, func, fill_value=nan):
        """
        Perform elementwise binary operation on two Series using given function
        with optional fill value when an index is missing from one Series or
        the other

        Parameters
        ----------
        other : Series or scalar value
        func : function
        fill_value : scalar value

        Returns
        -------
        result : Series
        """
        if isinstance(other, Series):
            new_index = self.index + other.index
            new_name = _maybe_match_name(self, other)
            new_values = np.empty(len(new_index), dtype=self.dtype)
            for i, idx in enumerate(new_index):
                lv = self.get(idx, fill_value)
                rv = other.get(idx, fill_value)
                new_values[i] = func(lv, rv)
        else:
            new_index = self.index
            new_values = func(self.values, other)
            new_name = self.name
        return Series(new_values, index=new_index, name=new_name)

    def combine_first(self, other):
        """
        Combine Series values, choosing the calling Series's values
        first. Result index will be the union of the two indexes

        Parameters
        ----------
        other : Series

        Returns
        -------
        y : Series
        """
        new_index = self.index + other.index
        this = self.reindex(new_index, copy=False)
        other = other.reindex(new_index, copy=False)
        name = _maybe_match_name(self, other)
        return Series(np.where(isnull(this), other, this), index=new_index,
                      name=name)

    def update(self, other):
        """
        Modify Series in place using non-NA values from passed
        Series. Aligns on index

        Parameters
        ----------
        other : Series
        """
        other = other.reindex_like(self)
        mask = notnull(other)
        np.putmask(self.values, mask, other.values)

    #----------------------------------------------------------------------
    # Reindexing, sorting

    def sort(self, axis=0, kind='quicksort', order=None):
        """
        Sort values and index labels by value, in place. For compatibility with
        ndarray API. No return value

        Parameters
        ----------
        axis : int (can only be zero)
        kind : {'mergesort', 'quicksort', 'heapsort'}, default 'quicksort'
            Choice of sorting algorithm. See np.sort for more
            information. 'mergesort' is the only stable algorithm
        order : ignored
        """
        sortedSeries = self.order(na_last=True, kind=kind)

        true_base = self
        while true_base.base is not None:
            true_base = true_base.base

        if (true_base is not None and
            (true_base.ndim != 1 or true_base.shape != self.shape)):
            raise Exception('This Series is a view of some other array, to '
                            'sort in-place you must create a copy')

        self[:] = sortedSeries
        self.index = sortedSeries.index

    def sort_index(self, ascending=True):
        """
        Sort object by labels (along an axis)

        Parameters
        ----------
        ascending : boolean or list, default True
            Sort ascending vs. descending. Specify list for multiple sort
            orders

        Examples
        --------
        >>> result1 = s.sort_index(ascending=False)
        >>> result2 = s.sort_index(ascending=[1, 0])

        Returns
        -------
        sorted_obj : Series
        """
        index = self.index
        if isinstance(index, MultiIndex):
            from pandas.core.groupby import _lexsort_indexer
            indexer = _lexsort_indexer(index.labels, orders=ascending)
            indexer = com._ensure_platform_int(indexer)
            new_labels = index.take(indexer)
        else:
            new_labels, indexer = index.order(return_indexer=True,
                                              ascending=ascending)

        new_values = self.values.take(indexer)
        return Series(new_values, new_labels, name=self.name)

    def argsort(self, axis=0, kind='quicksort', order=None):
        """
        Overrides ndarray.argsort. Argsorts the value, omitting NA/null values,
        and places the result in the same locations as the non-NA values

        Parameters
        ----------
        axis : int (can only be zero)
        kind : {'mergesort', 'quicksort', 'heapsort'}, default 'quicksort'
            Choice of sorting algorithm. See np.sort for more
            information. 'mergesort' is the only stable algorithm
        order : ignored

        Returns
        -------
        argsorted : Series
        """
        values = self.values
        mask = isnull(values)

        if mask.any():
            result = values.copy()
            notmask = -mask
            result[notmask] = np.argsort(values[notmask], kind=kind)
            return Series(result, index=self.index, name=self.name)
        else:
            return Series(np.argsort(values, kind=kind), index=self.index,
                          name=self.name)

    def rank(self, method='average', na_option='keep', ascending=True):
        """
        Compute data ranks (1 through n). Equal values are assigned a rank that
        is the average of the ranks of those values

        Parameters
        ----------
        method : {'average', 'min', 'max', 'first'}
            average: average rank of group
            min: lowest rank in group
            max: highest rank in group
            first: ranks assigned in order they appear in the array
        na_option : {'keep'}
            keep: leave NA values where they are
        ascending : boolean, default True
            False for ranks by high (1) to low (N)

        Returns
        -------
        ranks : Series
        """
        from pandas.core.algorithms import rank
        ranks = rank(self.values, method=method, na_option=na_option,
                     ascending=ascending)
        return Series(ranks, index=self.index, name=self.name)

    def order(self, na_last=True, ascending=True, kind='mergesort'):
        """
        Sorts Series object, by value, maintaining index-value link

        Parameters
        ----------
        na_last : boolean (optional, default=True)
            Put NaN's at beginning or end
        ascending : boolean, default True
            Sort ascending. Passing False sorts descending
        kind : {'mergesort', 'quicksort', 'heapsort'}, default 'mergesort'
            Choice of sorting algorithm. See np.sort for more
            information. 'mergesort' is the only stable algorithm

        Returns
        -------
        y : Series
        """
        def _try_mergesort(arr):
            # easier to ask forgiveness than permission
            try:
                return arr.argsort(kind='mergesort')
            except TypeError:
                # stable sort not available for object dtype
                return arr.argsort()

        arr = self.values
        sortedIdx = np.empty(len(self), dtype=np.int32)

        bad = isnull(arr)

        good = -bad
        idx = np.arange(len(self))

        argsorted = _try_mergesort(arr[good])

        if not ascending:
            argsorted = argsorted[::-1]

        if na_last:
            n = good.sum()
            sortedIdx[:n] = idx[good][argsorted]
            sortedIdx[n:] = idx[bad]
        else:
            n = bad.sum()
            sortedIdx[n:] = idx[good][argsorted]
            sortedIdx[:n] = idx[bad]

        return Series(arr[sortedIdx], index=self.index[sortedIdx],
                      name=self.name)

    def sortlevel(self, level=0, ascending=True):
        """
        Sort Series with MultiIndex by chosen level. Data will be
        lexicographically sorted by the chosen level followed by the other
        levels (in order)

        Parameters
        ----------
        level : int
        ascending : bool, default True

        Returns
        -------
        sorted : Series
        """
        if not isinstance(self.index, MultiIndex):
            raise Exception('can only sort by level with a hierarchical index')

        new_index, indexer = self.index.sortlevel(level, ascending=ascending)
        new_values = self.values.take(indexer)
        return Series(new_values, index=new_index, name=self.name)

    def swaplevel(self, i, j, copy=True):
        """
        Swap levels i and j in a MultiIndex

        Returns
        -------
        swapped : Series
        """
        new_index = self.index.swaplevel(i, j)
        return Series(self.values, index=new_index, copy=copy, name=self.name)

    def reorder_levels(self, order):
        """
        Rearrange index levels using input order. May not drop or duplicate
        levels

        Parameters
        ----------
        order: list of int representing new level order.
               (reference level by number not by key)
        axis: where to reorder levels

        Returns
        -------
        type of caller (new object)
        """
        if not isinstance(self.index, MultiIndex):  # pragma: no cover
            raise Exception('Can only reorder levels on a hierarchical axis.')

        result = self.copy()
        result.index = result.index.reorder_levels(order)
        return result

    def unstack(self, level=-1):
        """
        Unstack, a.k.a. pivot, Series with MultiIndex to produce DataFrame

        Parameters
        ----------
        level : int, string, or list of these, default last level
            Level(s) to unstack, can pass level name

        Examples
        --------
        >>> s
        one  a   1.
        one  b   2.
        two  a   3.
        two  b   4.

        >>> s.unstack(level=-1)
             a   b
        one  1.  2.
        two  3.  4.

        >>> s.unstack(level=0)
           one  two
        a  1.   2.
        b  3.   4.

        Returns
        -------
        unstacked : DataFrame
        """
        from pandas.core.reshape import unstack
        return unstack(self, level)

    #----------------------------------------------------------------------
    # function application

    def map(self, arg, na_action=None):
        """
        Map values of Series using input correspondence (which can be
        a dict, Series, or function)

        Parameters
        ----------
        arg : function, dict, or Series
        na_action : {None, 'ignore'}
            If 'ignore', propagate NA values

        Examples
        --------
        >>> x
        one   1
        two   2
        three 3

        >>> y
        1  foo
        2  bar
        3  baz

        >>> x.map(y)
        one   foo
        two   bar
        three baz

        Returns
        -------
        y : Series
            same index as caller
        """
        values = self.values

        if na_action == 'ignore':
            mask = isnull(values)

            def map_f(values, f):
                return lib.map_infer_mask(values, f, mask.view(np.uint8))
        else:
            map_f = lib.map_infer

        if isinstance(arg, (dict, Series)):
            if isinstance(arg, dict):
                arg = Series(arg)

            indexer = arg.index.get_indexer(values)
            new_values = com.take_1d(arg.values, indexer)
            return Series(new_values, index=self.index, name=self.name)
        else:
            mapped = map_f(values, arg)
            return Series(mapped, index=self.index, name=self.name)

    def apply(self, func, convert_dtype=True, args=(), **kwds):
        """
        Invoke function on values of Series. Can be ufunc, a Python function
        that applies to the entire Series, or a Python function that only
        works on single values

        Parameters
        ----------
        func : function
        convert_dtype : boolean, default True
            Try to find better dtype for elementwise function results. If
            False, leave as dtype=object

        See also
        --------
        Series.map: For element-wise operations

        Notes
        -----
        func is applied to the entire Series at once first. If an exception
        is raised, then apply to each value.

        Returns
        -------
        y : Series
        """
        if kwds or args and not isinstance(func, np.ufunc):
            f = lambda x: func(x, *args, **kwds)
        else:
            f = func

        try:
            result = f(self)
            if isinstance(result, np.ndarray):
                result = Series(result, index=self.index, name=self.name)
            else:
                raise ValueError('Must yield array')
            return result
        except Exception:
            mapped = lib.map_infer(self.values, f, convert=convert_dtype)
            return Series(mapped, index=self.index, name=self.name)

    def align(self, other, join='outer', level=None, copy=True,
              fill_value=None, method=None, inplace=False, limit=None):
        """
        Align two Series object with the specified join method

        Parameters
        ----------
        other : Series
        join : {'outer', 'inner', 'left', 'right'}, default 'outer'
        level : int or name
            Broadcast across a level, matching Index values on the
            passed MultiIndex level
        copy : boolean, default True
            Always return new objects. If copy=False and no reindexing is
            required, the same object will be returned (for better performance)
        fill_value : object, default None
        method : str, default 'pad'
        limit : int, default None
           fill_value, method, inplace, limit are passed to fillna

        Returns
        -------
        (left, right) : (Series, Series)
            Aligned Series
        """
        join_index, lidx, ridx = self.index.join(other.index, how=join,
                                                 level=level,
                                                 return_indexers=True)

        left = self._reindex_indexer(join_index, lidx, copy)
        right = other._reindex_indexer(join_index, ridx, copy)
        fill_na = (fill_value is not None) or (method is not None)
        if fill_na:
            return (left.fillna(fill_value, method=method, limit=limit),
                    right.fillna(fill_value, method=method, limit=limit))
        else:
            return left, right

    def _reindex_indexer(self, new_index, indexer, copy):
        if indexer is not None:
            new_values = com.take_1d(self.values, indexer)
        else:
            if copy:
                result = self.copy()
            else:
                result = self
            return result

        # be subclass-friendly
        return self._constructor(new_values, new_index, name=self.name)

    def reindex(self, index=None, method=None, level=None, fill_value=np.nan,
                limit=None, copy=True):
        """Conform Series to new index with optional filling logic, placing
        NA/NaN in locations having no value in the previous index. A new object
        is produced unless the new index is equivalent to the current one and
        copy=False

        Parameters
        ----------
        index : array-like or Index
            New labels / index to conform to. Preferably an Index object to
            avoid duplicating data
        method : {'backfill', 'bfill', 'pad', 'ffill', None}
            Method to use for filling holes in reindexed Series
            pad / ffill: propagate LAST valid observation forward to next valid
            backfill / bfill: use NEXT valid observation to fill gap
        copy : boolean, default True
            Return a new object, even if the passed indexes are the same
        level : int or name
            Broadcast across a level, matching Index values on the
            passed MultiIndex level
        fill_value : scalar, default np.NaN
            Value to use for missing values. Defaults to NaN, but can be any
            "compatible" value
        limit : int, default None
            Maximum size gap to forward or backward fill

        Returns
        -------
        reindexed : Series
        """
        if index is None:
            raise ValueError('Must pass Index or sequence, not None')

        index = _ensure_index(index)
        if self.index.equals(index):
            if copy:
                result = self.copy()
                result.index = index
                return result
            else:
                return self

        if len(self.index) == 0:
            return Series(nan, index=index, name=self.name)

        new_index, indexer = self.index.reindex(index, method=method,
                                                 level=level, limit=limit)
        new_values = com.take_1d(self.values, indexer, fill_value=fill_value)
        return Series(new_values, index=new_index, name=self.name)

    def reindex_like(self, other, method=None, limit=None):
        """
        Reindex Series to match index of another Series, optionally with
        filling logic

        Parameters
        ----------
        other : Series
        method : string or None
            See Series.reindex docstring
        limit : int, default None
            Maximum size gap to forward or backward fill

        Notes
        -----
        Like calling s.reindex(other.index, method=...)

        Returns
        -------
        reindexed : Series
        """
        return self.reindex(other.index, method=method, limit=limit)

    def take(self, indices, axis=0):
        """
        Analogous to ndarray.take, return Series corresponding to requested
        indices

        Parameters
        ----------
        indices : list / array of ints

        Returns
        -------
        taken : Series
        """
        indices = com._ensure_platform_int(indices)
        new_index = self.index.take(indices)
        new_values = self.values.take(indices)
        return Series(new_values, index=new_index, name=self.name)

    truncate = generic.truncate

    def fillna(self, value=None, method='pad', inplace=False,
               limit=None):
        """
        Fill NA/NaN values using the specified method

        Parameters
        ----------
        value : any kind (should be same type as array)
            Value to use to fill holes (e.g. 0)
        method : {'backfill', 'bfill', 'pad', 'ffill', None}, default 'pad'
            Method to use for filling holes in reindexed Series
            pad / ffill: propagate last valid observation forward to next valid
            backfill / bfill: use NEXT valid observation to fill gap
        inplace : boolean, default False
            If True, fill the Series in place. Note: this will modify any other
            views on this Series, for example a column in a DataFrame. Returns
            a reference to the filled object, which is self if inplace=True
        limit : int, default None
            Maximum size gap to forward or backward fill

        See also
        --------
        reindex, asfreq

        Returns
        -------
        filled : Series
        """
        if not self._can_hold_na:
            return self.copy() if not inplace else self

        if value is not None:
            result = self.copy() if not inplace else self
            mask = isnull(self.values)
            np.putmask(result, mask, value)
        else:
            if method is None:  # pragma: no cover
                raise ValueError('must specify a fill method')

            fill_f = _get_fill_func(method)

            if inplace:
                values = self.values
            else:
                values = self.values.copy()

            fill_f(values, limit=limit)

            if inplace:
                result = self
            else:
                result = Series(values, index=self.index, name=self.name)

        return result

    def replace(self, to_replace, value=None, method='pad', inplace=False,
                limit=None):
        """
        Replace arbitrary values in a Series

        Parameters
        ----------
        to_replace : list or dict
            list of values to be replaced or dict of replacement values
        value : anything
            if to_replace is a list then value is the replacement value
        method : {'backfill', 'bfill', 'pad', 'ffill', None}, default 'pad'
            Method to use for filling holes in reindexed Series
            pad / ffill: propagate last valid observation forward to next valid
            backfill / bfill: use NEXT valid observation to fill gap
        inplace : boolean, default False
            If True, fill the Series in place. Note: this will modify any other
            views on this Series, for example a column in a DataFrame. Returns
            a reference to the filled object, which is self if inplace=True
        limit : int, default None
            Maximum size gap to forward or backward fill

        Notes
        -----
        replace does not distinguish between NaN and None

        See also
        --------
        fillna, reindex, asfreq

        Returns
        -------
        replaced : Series
        """
        result = self.copy() if not inplace else self

        def _rep_one(s, to_rep, v):  # replace single value
            mask = com.mask_missing(s.values, to_rep)
            np.putmask(s.values, mask, v)
            return s

        def _rep_dict(rs, to_rep):  # replace {[src] -> dest}

            all_src = set()
            dd = {}  # group by unique destination value
            for s, d in to_rep.iteritems():
                dd.setdefault(d, []).append(s)
                all_src.add(s)

            if any(d in all_src for d in dd.keys()):
                # don't clobber each other at the cost of temporaries
                masks = {}
                for d, sset in dd.iteritems():  # now replace by each dest
                    masks[d] = com.mask_missing(rs.values, sset)

                for d, m in masks.iteritems():
                    np.putmask(rs.values, m, d)
            else:  # if no risk of clobbering then simple
                for d, sset in dd.iteritems():
                    _rep_one(rs, sset, d)
            return rs

        if np.isscalar(to_replace):
            to_replace = [to_replace]

        if isinstance(to_replace, dict):
            return _rep_dict(result, to_replace)

        if isinstance(to_replace, (list, np.ndarray)):

            if isinstance(value, (list, np.ndarray)):  # check same length
                vl, rl = len(value), len(to_replace)
                if vl == rl:
                    return _rep_dict(result, dict(zip(to_replace, value)))
                raise ValueError('Got %d to replace but %d values' % (rl, vl))

            elif value is not None:  # otherwise all replaced with same value

                return _rep_one(result, to_replace, value)

            else:  # method
                if method is None:  # pragma: no cover
                    raise ValueError('must specify a fill method')
                fill_f = _get_fill_func(method)

                mask = com.mask_missing(result, to_replace)
                fill_f(result.values, limit=limit, mask=mask)

                if not inplace:
                    result = Series(result.values, index=self.index,
                                    name=self.name)
                return result

        raise ValueError('Unrecognized to_replace type %s' %
                         type(to_replace))

    def isin(self, values):
        """
        Return boolean vector showing whether each element in the Series is
        exactly contained in the passed sequence of values

        Parameters
        ----------
        values : sequence

        Returns
        -------
        isin : Series (boolean dtype)
        """
        value_set = set(values)
        result = lib.ismember(self.values, value_set)
        return Series(result, self.index, name=self.name)

    def between(self, left, right, inclusive=True):
        """
        Return boolean Series equivalent to left <= series <= right. NA values
        will be treated as False

        Parameters
        ----------
        left : scalar
            Left boundary
        right : scalar
            Right boundary

        Returns
        -------
        is_between : Series
        """
        if inclusive:
            lmask = self >= left
            rmask = self <= right
        else:
            lmask = self > left
            rmask = self < right

        return lmask & rmask

    @classmethod
    def from_csv(cls, path, sep=',', parse_dates=True, header=None,
                 index_col=0, encoding=None):
        """
        Read delimited file into Series

        Parameters
        ----------
        path : string file path or file handle / StringIO
        sep : string, default ','
            Field delimiter
        parse_dates : boolean, default True
            Parse dates. Different default from read_table
        header : int, default 0
            Row to use at header (skip prior rows)
        index_col : int or sequence, default 0
            Column to use for index. If a sequence is given, a MultiIndex
            is used. Different default from read_table
        encoding : string, optional
            a string representing the encoding to use if the contents are
            non-ascii, for python versions prior to 3

        Returns
        -------
        y : Series
        """
        from pandas.core.frame import DataFrame
        df = DataFrame.from_csv(path, header=header, index_col=index_col,
                                sep=sep, parse_dates=parse_dates,
                                encoding=encoding)
        result = df.ix[:, 0]
        result.index.name = result.name = None
        return result

    def to_csv(self, path, index=True, sep=",", na_rep='',
               float_format=None, header=False,
               index_label=None, mode='w', nanRep=None, encoding=None):
        """
        Write Series to a comma-separated values (csv) file

        Parameters
        ----------
        path : string file path or file handle / StringIO
        na_rep : string, default ''
            Missing data representation
        float_format : string, default None
            Format string for floating point numbers
        header : boolean, default False
            Write out series name
        index : boolean, default True
            Write row names (index)
        index_label : string or sequence, default None
            Column label for index column(s) if desired. If None is given, and
            `header` and `index` are True, then the index names are used. A
            sequence should be given if the DataFrame uses MultiIndex.
        mode : Python write mode, default 'w'
        sep : character, default ","
            Field delimiter for the output file.
        encoding : string, optional
            a string representing the encoding to use if the contents are
            non-ascii, for python versions prior to 3
        """
        from pandas.core.frame import DataFrame
        df = DataFrame(self)
        df.to_csv(path, index=index, sep=sep, na_rep=na_rep,
                  float_format=float_format, header=header,
                  index_label=index_label, mode=mode, nanRep=nanRep,
                  encoding=encoding)

    def dropna(self):
        """
        Return Series without null values

        Returns
        -------
        valid : Series
        """
        return remove_na(self)

    valid = lambda self: self.dropna()

    isnull = isnull
    notnull = notnull

    def first_valid_index(self):
        """
        Return label for first non-NA/null value
        """
        if len(self) == 0:
            return None

        mask = isnull(self.values)
        i = mask.argmin()
        if mask[i]:
            return None
        else:
            return self.index[i]

    def last_valid_index(self):
        """
        Return label for last non-NA/null value
        """
        if len(self) == 0:
            return None

        mask = isnull(self.values[::-1])
        i = mask.argmin()
        if mask[i]:
            return None
        else:
            return self.index[len(self) - i - 1]

    #----------------------------------------------------------------------
    # Time series-oriented methods

    def shift(self, periods=1, freq=None, copy=True, **kwds):
        """
        Shift the index of the Series by desired number of periods with an
        optional time offset

        Parameters
        ----------
        periods : int
            Number of periods to move, can be positive or negative
        freq : DateOffset, timedelta, or offset alias string, optional
            Increment to use from datetools module or time rule (e.g. 'EOM')

        Returns
        -------
        shifted : Series
        """
        if periods == 0:
            return self.copy()

        offset = _resolve_offset(freq, kwds)

        if isinstance(offset, basestring):
            offset = datetools.to_offset(offset)

        def _get_values():
            values = self.values
            if copy:
                values = values.copy()
            return values

        if offset is None:
            new_values = np.empty(len(self), dtype=self.dtype)
            new_values = _maybe_upcast(new_values)

            if periods > 0:
                new_values[periods:] = self.values[:-periods]
                new_values[:periods] = nan
            elif periods < 0:
                new_values[:periods] = self.values[-periods:]
                new_values[periods:] = nan

            return Series(new_values, index=self.index, name=self.name)
        elif isinstance(self.index, PeriodIndex):
            orig_offset = datetools.to_offset(self.index.freq)
            if orig_offset == offset:
                return Series(_get_values(), self.index.shift(periods),
                              name=self.name)
            msg = ('Given freq %s does not match PeriodIndex freq %s' %
                   (offset.rule_code, orig_offset.rule_code))
            raise ValueError(msg)
        else:
            return Series(_get_values(),
                          index=self.index.shift(periods, offset),
                          name=self.name)

    def asof(self, where):
        """
        Return last good (non-NaN) value in TimeSeries if value is NaN for
        requested date.

        If there is no good value, NaN is returned.

        Parameters
        ----------
        where : date or array of dates

        Notes
        -----
        Dates are assumed to be sorted

        Returns
        -------
        value or NaN
        """
        if isinstance(where, basestring):
            where = datetools.to_datetime(where)

        values = self.values

        if not hasattr(where, '__iter__'):
            start = self.index[0]
            if isinstance(self.index, PeriodIndex):
                where = Period(where, freq=self.index.freq).ordinal
                start = start.ordinal

            if where < start:
                return np.nan
            loc = self.index.searchsorted(where, side='right')
            if loc > 0:
                loc -= 1
            while isnull(values[loc]) and loc > 0:
                loc -= 1
            return values[loc]

        if not isinstance(where, Index):
            where = Index(where)

        locs = self.index.asof_locs(where, notnull(values))
        new_values = com.take_1d(values, locs)
        return Series(new_values, index=where, name=self.name)

    def interpolate(self, method='linear'):
        """
        Interpolate missing values (after the first valid value)

        Parameters
        ----------
        method : {'linear', 'time', 'values'}
            Interpolation method.
            'time' interpolation works on daily and higher resolution
            data to interpolate given length of interval
            'values' using the actual index numeric values

        Returns
        -------
        interpolated : Series
        """
        if method == 'time':
            if not isinstance(self, TimeSeries):
                raise Exception('time-weighted interpolation only works'
                                'on TimeSeries')
            method = 'values'
            # inds = np.array([d.toordinal() for d in self.index])

        if method == 'values':
            inds = self.index.values
            # hack for DatetimeIndex, #1646
            if issubclass(inds.dtype.type, np.datetime64):
                inds = inds.view(np.int64)

            if inds.dtype == np.object_:
                inds = lib.maybe_convert_objects(inds)
        else:
            inds = np.arange(len(self))

        values = self.values

        invalid = isnull(values)
        valid = -invalid

        firstIndex = valid.argmax()
        valid = valid[firstIndex:]
        invalid = invalid[firstIndex:]
        inds = inds[firstIndex:]

        result = values.copy()
        result[firstIndex:][invalid] = np.interp(inds[invalid], inds[valid],
                                                 values[firstIndex:][valid])

        return Series(result, index=self.index, name=self.name)

    def rename(self, mapper, inplace=False):
        """
        Alter Series index using dict or function

        Parameters
        ----------
        mapper : dict-like or function
            Transformation to apply to each index

        Notes
        -----
        Function / dict values must be unique (1-to-1)

        Examples
        --------
        >>> x
        foo 1
        bar 2
        baz 3

        >>> x.rename(str.upper)
        FOO 1
        BAR 2
        BAZ 3

        >>> x.rename({'foo' : 'a', 'bar' : 'b', 'baz' : 'c'})
        a 1
        b 2
        c 3

        Returns
        -------
        renamed : Series (new object)
        """
        mapper_f = _get_rename_function(mapper)
        result = self if inplace else self.copy()
        result.index = [mapper_f(x) for x in self.index]

        return result

    @property
    def weekday(self):
        return Series([d.weekday() for d in self.index], index=self.index)

    def tz_convert(self, tz, copy=True):
        """
        Convert TimeSeries to target time zone

        Parameters
        ----------
        tz : string or pytz.timezone object
        copy : boolean, default True
            Also make a copy of the underlying data

        Returns
        -------
        converted : TimeSeries
        """
        new_index = self.index.tz_convert(tz)

        new_values = self.values
        if copy:
            new_values = new_values.copy()

        return Series(new_values, index=new_index, name=self.name)

    def tz_localize(self, tz, copy=True):
        """
        Localize tz-naive TimeSeries to target time zone

        Parameters
        ----------
        tz : string or pytz.timezone object
        copy : boolean, default True
            Also make a copy of the underlying data

        Returns
        -------
        localized : TimeSeries
        """
        from pandas.tseries.index import DatetimeIndex

        if not isinstance(self.index, DatetimeIndex):
            if len(self.index) > 0:
                raise Exception('Cannot tz-localize non-time series')

            new_index = DatetimeIndex([], tz=tz)
        else:
            new_index = self.index.tz_localize(tz)

        new_values = self.values
        if copy:
            new_values = new_values.copy()

        return Series(new_values, index=new_index, name=self.name)

    @cache_readonly
    def str(self):
        from pandas.core.strings import StringMethods
        return StringMethods(self)

_INDEX_TYPES = ndarray, Index, list, tuple

#------------------------------------------------------------------------------
# Supplementary functions


def remove_na(arr):
    """
    Return array containing only true/non-NaN values, possibly empty.
    """
    return arr[notnull(arr)]


def _sanitize_array(data, index, dtype=None, copy=False,
                    raise_cast_failure=False):
    if isinstance(data, ma.MaskedArray):
        mask = ma.getmaskarray(data)
        data = ma.copy(data)
        data[mask] = np.nan

    def _try_cast(arr):
        try:
            subarr = np.array(data, dtype=dtype, copy=copy)
        except (ValueError, TypeError):
            if dtype is not None and raise_cast_failure:
                raise
            else:  # pragma: no cover
                subarr = np.array(data, dtype=object, copy=copy)
        return subarr

    # GH #846
    if isinstance(data, np.ndarray):
        subarr = data
        if dtype is not None:

            # possibility of nan -> garbage
            if com.is_float_dtype(data.dtype) and com.is_integer_dtype(dtype):
                if not isnull(data).any():
                    subarr = _try_cast(data)
                elif copy:
                    subarr = data.copy()
            else:
                if (com.is_datetime64_dtype(data.dtype) and
                    not com.is_datetime64_dtype(dtype)):
                    if dtype == object:
                        ints = np.asarray(data).view('i8')
                        subarr = lib.ints_to_pydatetime(ints)
                    elif raise_cast_failure:
                        raise TypeError('Cannot cast datetime64 to %s' % dtype)
                else:
                    subarr = _try_cast(data)
        elif copy:
            subarr = data.copy()
    elif isinstance(data, list) and len(data) > 0:
        if dtype is not None:
            try:
                subarr = _try_cast(data)
            except Exception:
                if raise_cast_failure:  # pragma: no cover
                    raise
                subarr = np.array(data, dtype=object, copy=copy)
                subarr = lib.maybe_convert_objects(subarr)
        else:
            subarr = lib.list_to_object_array(data)
            subarr = lib.maybe_convert_objects(subarr)
    else:
        subarr = _try_cast(data)

    if subarr.ndim == 0:
        if isinstance(data, list):  # pragma: no cover
            subarr = np.array(data, dtype=object)
        elif index is not None:
            value = data

            # If we create an empty array using a string to infer
            # the dtype, NumPy will only allocate one character per entry
            # so this is kind of bad. Alternately we could use np.repeat
            # instead of np.empty (but then you still don't want things
            # coming out as np.str_!
            if isinstance(value, basestring) and dtype is None:
                dtype = np.object_

            if dtype is None:
                value, dtype = _dtype_from_scalar(value)
                subarr = np.empty(len(index), dtype=dtype)
            else:
                subarr = np.empty(len(index), dtype=dtype)
            subarr.fill(value)
        else:
            return subarr.item()
    elif subarr.ndim > 1:
        if isinstance(data, np.ndarray):
            raise Exception('Data must be 1-dimensional')
        else:
            subarr = _asarray_tuplesafe(data, dtype=dtype)

    # This is to prevent mixed-type Series getting all casted to
    # NumPy string type, e.g. NaN --> '-1#IND'.
    if issubclass(subarr.dtype.type, basestring):
        subarr = np.array(data, dtype=object, copy=copy)

    return subarr


def _dtype_from_scalar(val):
    if isinstance(val, np.datetime64):
        # ugly hacklet
        val = lib.Timestamp(val).value
        return val, np.dtype('M8[ns]')
    return val, type(val)


def _get_rename_function(mapper):
    if isinstance(mapper, (dict, Series)):
        def f(x):
            if x in mapper:
                return mapper[x]
            else:
                return x
    else:
        f = mapper

    return f


def _resolve_offset(freq, kwds):
    if 'timeRule' in kwds or 'offset' in kwds:
        offset = kwds.get('offset', None)
        offset = kwds.get('timeRule', offset)
        if isinstance(offset, basestring):
            offset = datetools.getOffset(offset)
        warn = True
    else:
        offset = freq
        warn = False

    if warn and _SHOW_WARNINGS:  # pragma: no cover
        import warnings
        warnings.warn("'timeRule' and 'offset' parameters are deprecated,"
                      " please use 'freq' instead",
                      FutureWarning)

    return offset


def _get_fill_func(method):
    method = com._clean_fill_method(method)
    if method == 'pad':
        fill_f = com.pad_1d
    elif method == 'backfill':
        fill_f = com.backfill_1d
    return fill_f

#----------------------------------------------------------------------
# Add plotting methods to Series

import pandas.tools.plotting as _gfx

Series.plot = _gfx.plot_series
Series.hist = _gfx.hist_series

# Put here, otherwise monkey-patching in methods fails


class TimeSeries(Series):

    def _repr_footer(self):
        if self.index.freq is not None:
            freqstr = 'Freq: %s, ' % self.index.freqstr
        else:
            freqstr = ''

        namestr = "Name: %s, " % str(self.name) if self.name is not None else ""
        return '%s%sLength: %d' % (freqstr, namestr, len(self))

    def to_timestamp(self, freq=None, how='start', copy=True):
        """
        Cast to datetimeindex of timestamps, at *beginning* of period

        Parameters
        ----------
        freq : string, default frequency of PeriodIndex
            Desired frequency
        how : {'s', 'e', 'start', 'end'}
            Convention for converting period to timestamp; start of period
            vs. end

        Returns
        -------
        ts : TimeSeries with DatetimeIndex
        """
        new_values = self.values
        if copy:
            new_values = new_values.copy()

        new_index = self.index.to_timestamp(freq=freq, how=how)
        return Series(new_values, index=new_index, name=self.name)

    def to_period(self, freq=None, copy=True):
        """
        Convert TimeSeries from DatetimeIndex to PeriodIndex with desired
        frequency (inferred from index if not passed)

        Parameters
        ----------
        freq : string, default

        Returns
        -------
        ts : TimeSeries with PeriodIndex
        """
        new_values = self.values
        if copy:
            new_values = new_values.copy()

        if freq is None:
            freq = self.index.freqstr or self.index.inferred_freq
        new_index = self.index.to_period(freq=freq)
        return Series(new_values, index=new_index, name=self.name)
