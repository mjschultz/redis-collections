# -*- coding: utf-8 -*-
"""
lists
~~~~~

Collections based on list interface.
"""
from __future__ import division, print_function, unicode_literals

import collections
import itertools
import uuid

import six
from redis import ResponseError

from .base import RedisCollection


class List(RedisCollection, collections.MutableSequence):
    """Mutable **sequence** collection aiming to have the same API as the
    standard sequence type, :class:`list`. See `list
    <http://docs.python.org/2/library/functions.html#list>`_ for
    further details. The Redis implementation is based on the
    `list <http://redis.io/commands#list>`_ type.

    .. warning::
        In comparing with original :class:`list` type, :class:`List` does not
        implement methods :func:`sort` and :func:`reverse`.

    .. note::
        Some operations, which are usually not used so often, can be more
        efficient than their "popular" equivalents. Some operations are
        shortened in their capabilities and can raise
        :exc:`NotImplementedError` for some inputs (e.g. most of the slicing
        functionality).
    """

    def __init__(self, *args, **kwargs):
        """
        :param data: Initial data.
        :type data: iterable
        :param redis: Redis client instance. If not provided, default Redis
                      connection is used.
        :type redis: :class:`redis.StrictRedis`
        :param key: Redis key of the collection. Collections with the same key
                    point to the same data. If not provided, default random
                    string is generated.
        :type key: str
        :param writeback: If ``True`` keep a local cache of changes for storing
                          modifications to mutable values. Changes will be
                          written to Redis after calling the ``sync`` method.
        :type key: bool

        .. note::
            :func:`uuid.uuid4` is used for default key generation.
            If you are not satisfied with its `collision
            probability <http://stackoverflow.com/a/786541/325365>`_,
            make your own implementation by subclassing and overriding
            internal method :func:`_create_key`.
        """
        data = args[0] if args else kwargs.pop('data', None)
        writeback = kwargs.pop('writeback', False)
        super(List, self).__init__(*args, **kwargs)

        self.__marker = uuid.uuid4().hex
        self.writeback = writeback
        self.cache = {}

        if data:
            self.extend(data)

    def _normalize_index(self, index, pipe=None):
        pipe = pipe or self.redis
        len_self = pipe.llen(self.key)
        positive_index = index if index >= 0 else len_self + index

        return len_self, positive_index

    def _normalize_slice(self, index, pipe=None):
        if index.step == 0:
            raise ValueError
        pipe = pipe or self.redis

        len_self = pipe.llen(self.key)

        step = index.step or 1
        forward = step > 0
        step = abs(step)

        if index.start is None:
            start = 0 if forward else len_self - 1
        elif index.start < 0:
            start = max(len_self + index.start, 0)
        else:
            start = min(index.start, len_self)

        if index.stop is None:
            stop = len_self if forward else -1
        elif index.stop < 0:
            stop = max(len_self + index.stop, 0)
        else:
            stop = min(index.stop, len_self)

        if not forward:
            start, stop = min(stop + 1, len_self), min(start + 1, len_self)

        return start, stop, step, forward, len_self

    def _pop_left(self):
        pickled_value = self.redis.lpop(self.key)
        if pickled_value is None:
            raise IndexError
        value = self.cache.get(0, self._unpickle(pickled_value))

        if self.writeback:
            items = six.iteritems(self.cache)
            self.cache = {i - 1: v for i, v in items if i != 0}

        return value

    def _pop_right(self):
        def pop_right_trans(pipe):
            len_self, cache_index = self._normalize_index(-1, pipe)
            if len_self == 0:
                raise IndexError
            pickled_value = pipe.rpop(self.key)
            value = self._unpickle(pickled_value)

            if self.writeback:
                value = self.cache.get(cache_index, value)
                items = six.iteritems(self.cache)
                self.cache = {i: v for i, v in items if i != cache_index}

            return value

        return self._transaction(pop_right_trans)

    def _pop_middle(self, index):
        def pop_middle_trans(pipe):
            len_self, cache_index = self._normalize_index(index, pipe)
            if (cache_index < 0) or (cache_index >= len_self):
                raise IndexError

            value = self._unpickle(pipe.lindex(self.key, index))
            pipe.multi()
            pipe.lset(self.key, index, self.__marker)
            pipe.lrem(self.key, 1, self.__marker)

            if self.writeback:
                value = self.cache.get(cache_index, value)
                new_cache = {}
                for i, v in six.iteritems(self.cache):
                    if i < cache_index:
                        new_cache[i] = v
                    elif i > cache_index:
                        new_cache[i - 1] = v
                self.cache = new_cache

            return value

        return self._transaction(pop_middle_trans)

    def _del_slice(self, index):
        # Steps are not supported
        if index.step is not None:
            raise NotImplementedError

        def del_slice_trans(pipe):
            start, stop, step, forward, len_self = self._normalize_slice(
                index, pipe
            )

            # Empty slice: nothing to do
            if start == stop:
                return

            # Write back the cache before making changes
            if self.writeback:
                self._sync_helper(pipe)

            # Slice covers entire range: delete the whole list
            if start == 0 and stop == len_self:
                self.clear(pipe)
            elif start == 0 and stop != len_self:
                pipe.ltrim(self.key, stop + 1, -1)
            elif start != 0 and stop == len_self:
                pipe.ltrim(self.key, 0, start - 1)
            else:
                left_values = pipe.lrange(self.key, 0, max(start - 2, 0))
                right_values = pipe.lrange(self.key, stop + 1, -1)
                pipe.delete(self.key)
                all_values = itertools.chain(left_values, right_values)
                pipe.rpush(self.key, *all_values)

        return self._transaction(del_slice_trans)

    def __delitem__(self, index):
        if isinstance(index, slice):
            return self._del_slice(index)

        if index == 0:
            self._pop_left()
        elif index == -1:
            self._pop_right()
        else:
            self._pop_middle(index)

    def _get_slice(self, index):
        def get_slice_trans(pipe):
            start, stop, step, forward, len_self = self._normalize_slice(
                index, pipe
            )

            if start == stop:
                return []

            ret = []
            redis_values = pipe.lrange(self.key, start, max(stop - 1, 0))
            for i, v in enumerate(redis_values, start):
                ret.append(self.cache.get(i, self._unpickle(v)))

            if not forward:
                ret = reversed(ret)

            if step != 1:
                ret = itertools.islice(ret, None, None, step)

            return list(ret)

        return self._transaction(get_slice_trans)

    def __getitem__(self, index):
        if isinstance(index, slice):
            return self._get_slice(index)

        # If writeback is off we can just query Redis, since its indexing
        # scheme matches Python's. If the index is out of range we get None.
        if not self.writeback:
            pickled_value = self.redis.lindex(self.key, index)
            if pickled_value is None:
                raise IndexError

            return self._unpickle(pickled_value)

        # If writeback is on we'll need to know the size of the list,
        # so we'll need to use a transaction
        def getitem_trans(pipe):
            len_self, cache_index = self._normalize_index(index, pipe)

            if (cache_index < 0) or (cache_index >= len_self):
                raise IndexError

            if cache_index in self.cache:
                return self.cache[cache_index]

            value = self._unpickle(pipe.lindex(self.key, index))
            self.cache[cache_index] = value
            return value

        return self._transaction(getitem_trans)

    def _data(self, pipe=None):
        pipe = pipe or self.redis
        return [self._unpickle(v) for v in pipe.lrange(self.key, 0, -1)]

    def __iter__(self, pipe=None):
        return (self.cache.get(i, v) for i, v in enumerate(self._data(pipe)))

    def __len__(self):
        return self.redis.llen(self.key)

    def __reversed__(self):
        return reversed(list(self.__iter__()))

    def _set_slice(self, index, value):
        # Steps are not supported
        if index.step is not None:
            raise NotImplementedError

        def set_slice_trans(pipe):
            start, stop, step, forward, len_self = self._normalize_slice(
                index, pipe
            )

            # Write back the cache before making changes
            if self.writeback:
                self._sync_helper(pipe)

            # Get everything from 0 to before the beginning of the slice
            if start == 0:
                left_values = []
            else:
                left_values = pipe.lrange(self.key, 0, start - 1)
            # Pickle what's been passed
            middle_values = (self._pickle(v) for v in value)
            # Get everything from where the slice ends to the end of the list
            if stop == len_self:
                right_values = []
            else:
                right_values = pipe.lrange(self.key, stop, -1)
            # Delete everything
            pipe.delete(self.key)
            # Push everything back, with the new values in the middle
            all_values = itertools.chain(
                left_values, middle_values, right_values
            )
            pipe.rpush(self.key, *all_values)

        return self._transaction(set_slice_trans)

    def __setitem__(self, index, value):
        if isinstance(index, slice):
            return self._set_slice(index, value)

        with self.redis.pipeline() as pipe:
            if self.writeback:
                __, cache_index = self._normalize_index(index, pipe)

            pipe.lset(self.key, index, self._pickle(value))

            try:
                pipe.execute()
            except ResponseError:
                raise IndexError

            if self.writeback:
                self.cache[cache_index] = value

    def append(self, value):
        len_self = self.redis.rpush(self.key, self._pickle(value))

        if self.writeback:
            self.cache[len_self - 1] = value

    def clear(self, pipe=None):
        pipe = pipe or self.redis
        pipe.delete(self.key)

        if self.writeback:
            self.cache.clear()

    def count(self, value):
        return sum(1 for v in self.__iter__() if v == value)

    def insert(self, index, value):
        # Easy case: insert on the left
        if index == 0:
            self.redis.lpush(self.key, self._pickle(value))
            if self.writeback:
                self.cache = {k + 1: v for k, v in six.iteritems(self.cache)}
                self.cache[0] = value
        # Almost as easy case: insert on the right
        elif index == -1:
            len_self = self.redis.rpush(self.key, self._pickle(value))
            if self.writeback:
                cache_index = index if index >= 0 else len_self + index
                self.cache[cache_index] = value
        # Difficult case: insert in the middle.
        # Retrieve everything from ``index`` up to the end, insert ``value``
        # on the right, then re-insert the retrieved items on the right
        else:
            def insert_middle_trans(pipe):
                __, cache_index = self._normalize_index(index, pipe)
                right_values = pipe.lrange(self.key, cache_index, -1)
                pipe.ltrim(self.key, 0, cache_index - 1)
                pipe.rpush(self.key, self._pickle(value), *right_values)
                if self.writeback:
                    new_cache = {}
                    for i, v in six.iteritems(self.cache):
                        if i < cache_index:
                            new_cache[i] = v
                        elif i >= cache_index:
                            new_cache[i + 1] = v
                    new_cache[cache_index] = value
                    self.cache = new_cache
            self._transaction(insert_middle_trans)

    def extend(self, other):
        def extend_trans(pipe):
            if use_redis:
                values = list(other.__iter__(pipe))
            else:
                values = other
            len_self = pipe.rpush(self.key, *(self._pickle(v) for v in values))
            if self.writeback:
                for i, v in enumerate(values, len_self - len(values)):
                    self.cache[i] = v

        if isinstance(other, RedisCollection):
            use_redis = True
            self._transaction(extend_trans, other.key)
        else:
            use_redis = False
            self._transaction(extend_trans)

    def index(self, value, start=None, stop=None):
        def index_trans(pipe):
            len_self, normal_start = self._normalize_index(start or 0, pipe)
            __, normal_stop = self._normalize_index(stop or len_self, pipe)
            for i, v in enumerate(self.__iter__(pipe=pipe)):
                if v == value:
                    if i < normal_start:
                        continue
                    if i >= normal_stop:
                        break
                    return i
            raise ValueError

        return self._transaction(index_trans)

    def pop(self, index=-1):
        if index == 0:
            return self._pop_left()
        elif index == -1:
            return self._pop_right()
        else:
            return self._pop_middle(index)

    def remove(self, value):
        """Remove the first occurence of *value*."""
        # If we're caching, we'll need to synchronize before removing.
        with self.redis.pipeline() as pipe:
            if self.writeback:
                self._sync_helper(pipe)
            pipe.lrem(self.key, 1, self._pickle(value))
            pipe.execute()

    def reverse(self):
        def reverse_trans(pipe):
            n = pipe.llen(self.key)
            for i in six.moves.xrange(n // 2):
                left = pipe.lindex(self.key, i)
                right = pipe.lindex(self.key, n - i - 1)
                pipe.lset(self.key, i, right)
                pipe.lset(self.key, n - i - 1, left)

        self._transaction(reverse_trans)

    def __iadd__(self):
        pass

    def _repr_data(self, data):
        return repr(list(data))

    def __enter__(self):
        self.writeback = True
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.sync()

    def _sync_helper(self, pipe):
        for i, v in six.iteritems(self.cache):
            pipe.lset(self.key, i, self._pickle(v))

        self.cache = {}

    def sync(self):
        def sync_trans(pipe):
            pipe.multi()
            self._sync_helper(pipe)

        self._transaction(sync_trans)
