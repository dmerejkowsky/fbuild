import abc
import functools
import hashlib
import io
import itertools
import pickle
import sys
import threading
import time
import types

import fbuild
import fbuild.functools
import fbuild.inspect
import fbuild.path
import fbuild.rpc

# ------------------------------------------------------------------------------

class SRC:
    """An annotation that's used to designate an argument as a source path."""
    @staticmethod
    def convert(src):
        return [src]

class SRCS(SRC):
    """An annotation that's used to designate an argument as a list of source
    paths."""
    @staticmethod
    def convert(srcs):
        return srcs

class DST:
    """An annotation that's used to designate an argument is a destination
    path."""
    @staticmethod
    def convert(dst):
        return [dst]

class DSTS(DST):
    """An annotation that's used to designate an argument is a list of
    destination paths."""
    @staticmethod
    def convert(dsts):
        return dsts

class OPTIONAL_SRC(SRC):
    """An annotation that's used to designate an argument as a source path or
    None."""
    @staticmethod
    def convert(src):
        if src is None:
            return []
        return [src]

class OPTIONAL_DST(DST):
    """An annotation that's used to designate an argument as a destination path
    or None."""
    @staticmethod
    def convert(dst):
        if dst is None:
            return []
        return [dst]

# ------------------------------------------------------------------------------

class _Pickler(pickle._Pickler):
    """Create a custom pickler that won't try to pickle the context."""

    def __init__(self, ctx, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.ctx = ctx

    def persistent_id(self, obj):
        if obj is self.ctx:
            return 'ctx'
        else:
            return None

class _Unpickler(pickle._Unpickler):
    """Create a custom unpickler that will substitute the current context."""

    def __init__(self, ctx, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.ctx = ctx

    def persistent_load(self, pid):
        if pid == 'ctx':
            return self.ctx
        else:
            raise pickle.UnpicklingError('unsupported persistent object')

# ------------------------------------------------------------------------------

class Database:
    """L{Database} persistently stores the results of argument calls."""

    def __init__(self, ctx):
        def handle_rpc(msg):
            method, args, kwargs = msg
            return method(*args, **kwargs)

        self._ctx = ctx
        self._backend = DatabaseBackend(ctx)
        self._rpc = fbuild.rpc.RPC(handle_rpc)
        self._rpc.daemon = True
        self.start()

    def start(self):
        """Start the server thread."""
        self._rpc.start()

    def shutdown(self, *args, **kwargs):
        """Inform and wait for the L{DatabaseThread} to shut down."""
        self._rpc.join(*args, **kwargs)

    def save(self, *args, **kwargs):
        """Save the database to the file."""
        return self._rpc.call((self._backend.save, args, kwargs))

    def load(self, *args, **kwargs):
        """Load the database from the file."""
        return self._rpc.call((self._backend.load, args, kwargs))

    def call(self, function, *args, **kwargs):
        """Call the function and return the result, src dependencies, and dst
        dependencies. If the function has been previously called with the same
        arguments, return the cached results.  If we detect that the function
        changed, throw away all the cached values for that function. Similarly,
        throw away all of the cached values if any of the optionally specified
        "srcs" are also modified.  Finally, if any of the filenames in "dsts"
        do not exist, re-run the function no matter what."""

        # Make sure none of the arguments are a generator.
        assert all(not fbuild.inspect.isgenerator(arg)
            for arg in itertools.chain(args, kwargs.values())), \
            "Cannot store generator in database"

        if not fbuild.inspect.ismethod(function):
            function_name = function.__module__ + '.' + function.__name__
        else:
            # If we're caching a PersistentObject creation, use the class's
            # name as our function name.
            if function.__name__ == '__call_super__' and \
                    isinstance(function.__self__, PersistentMeta):
                function_name = '%s.%s' % (
                    function.__self__.__module__,
                    function.__self__.__name__)
            else:
                function_name = '%s.%s.%s' % (
                    function.__module__,
                    function.__self__.__class__.__name__,
                    function.__name__)
            args = (function.__self__,) + args
            function = function.__func__

        if not fbuild.inspect.isroutine(function):
            function = function.__call__

        # Compute the function digest.
        function_digest = self._digest_function(function, args, kwargs)

        # Bind the arguments so that we can look up normal args by name.
        bound = fbuild.functools.bind_args(function, args, kwargs)

        # Check if any of the files changed.
        return_type = None
        srcs = set()
        dsts = set()
        for akey, avalue in function.__annotations__.items():
            if akey == 'return':
                return_type = avalue
            elif issubclass(avalue, SRC):
                srcs.update(avalue.convert(bound[akey]))
            elif issubclass(avalue, DST):
                dsts.update(avalue.convert(bound[akey]))

        # Make sure none of the arguments are a generator.
        for arg in itertools.chain(args, kwargs.values()):
            assert not fbuild.inspect.isgenerator(arg), \
                "Cannot store generator in database"

        function_dirty, call_id, old_result, call_file_digests, \
            external_dirty, external_srcs, external_dsts, external_digests = \
                self._rpc.call((
                    self._backend.prepare,
                    (function_name, function_digest, bound, srcs, dsts),
                    {}))

        # Check if we have a result. If not, then we're dirty.
        if not (function_dirty or \
                call_id is None or \
                call_file_digests or \
                external_digests or \
                external_dirty):
            # If the result is a dst filename, make sure it exists. If not,
            # we're dirty.
            if return_type is not None and issubclass(return_type, DST):
                return_dsts = return_type.convert(old_result)
            else:
                return_dsts = ()

            for dst in itertools.chain(
                    return_dsts,
                    dsts,
                    external_dsts):
                if not fbuild.path.Path(dst).exists():
                    break
            else:
                # The call was not dirty, so return the cached value.
                all_srcs = srcs.union(external_srcs)
                all_dsts = dsts.union(external_dsts)
                all_dsts.update(return_dsts)
                return old_result, all_srcs, all_dsts

        # Clear external srcs and dsts since they'll be recomputed inside
        # the function.
        external_srcs = set()
        external_dsts = set()

        # The call was dirty, so recompute it.
        result = function(*args, **kwargs)

        # Make sure the result is not a generator.
        assert not fbuild.inspect.isgenerator(result), \
            "Cannot store generator in database"

        # Save the results in the database.
        self._rpc.call((
            self._backend.cache,
            (function_dirty, function_name, function_digest,
                call_id, bound, result, call_file_digests, external_srcs,
                external_dsts, external_digests),
            {}))

        if return_type is not None and issubclass(return_type, DST):
            return_dsts = return_type.convert(result)
        else:
            return_dsts = ()

        all_srcs = srcs.union(external_srcs)
        all_dsts = dsts.union(external_dsts)
        all_dsts.update(return_dsts)
        return result, all_srcs, all_dsts

    # Create an in-process cache of the function digests, since they shouldn't
    # change while we're running.
    _digest_function_lock = threading.Lock()
    _digest_function_cache = {}
    def _digest_function(self, function, args, kwargs):
        """Compute the digest for a function or a function object. Cache this
        for this instance."""
        with self._digest_function_lock:
            # If we're caching a PersistentObject creation, use the class's
            # __init__ as our function.
            if fbuild.inspect.isroutine(function) and \
                    len(args) > 0 and \
                    function.__name__ == '__call_super__' and \
                    isinstance(args[0], PersistentMeta):
                function = args[0].__init__

            try:
                digest = self._digest_function_cache[function]
            except KeyError:
                if fbuild.inspect.isroutine(function):
                    # The function is a function, method, or lambda, so digest
                    # the source. If the function is a builtin, we will raise
                    # an exception.
                    src = fbuild.inspect.getsource(function)
                    digest = hashlib.md5(src.encode()).hexdigest()
                else:
                    # The function is a functor so let it digest itself.
                    digest = hash(function)
                self._digest_function_cache[function] = digest

        return digest

# ------------------------------------------------------------------------------

class DatabaseBackend:
    def __init__(self, ctx):
        super().__init__()

        self._ctx = ctx
        self._functions = {}
        self._function_calls = {}
        self._files = {}
        self._call_files = {}
        self._external_srcs = {}
        self._external_dsts = {}

    def save(self, filename):
        """Save the database to the file."""

        f = io.BytesIO()
        pickler = _Pickler(self._ctx, f, pickle.HIGHEST_PROTOCOL)

        pickler.dump((
            self._functions,
            self._function_calls,
            self._files,
            self._call_files,
            self._external_srcs,
            self._external_dsts))

        s = f.getvalue()

        # Try to save the state as atomically as possible. Unfortunately, if
        # someone presses ctrl+c while we're saving, we might corrupt the db.
        # So, we'll write to a temp file, then move the old state file out of
        # the way, then rename the temp file to the filename.
        path = fbuild.path.Path(filename)
        tmp = path + '.tmp'
        old = path + '.old'

        with open(tmp, 'wb') as f:
            f.write(s)

        if path.exists():
            path.rename(old)

        tmp.rename(path)

        if old.exists():
            old.remove()

    def load(self, filename):
        """Load the database from the file."""

        with open(filename, 'rb') as f:
            unpickler = _Unpickler(self._ctx, f)

            self._functions, self._function_calls, self._files, \
                self._call_files, self._external_srcs, \
                self._external_dsts = unpickler.load()

    def prepare(self,
            function_name,
            function_digest,
            bound,
            srcs,
            dsts):
        """Queries all the information needed to cache a function."""

        # Check if the function changed.
        function_dirty = self._check_function(function_name, function_digest)

        # Check if this is a new call and get the index.
        call_id, old_result = self._check_call(function_name, bound)

        # Add the source files to the database.
        call_file_digests = self._check_call_files(
            call_id,
            function_name,
            srcs)

        # Check extra external call files.
        external_dirty, external_srcs, external_dsts, external_digests = \
            self._check_external_files(function_name, call_id)

        return (
            function_dirty,
            call_id,
            old_result,
            call_file_digests,
            external_dirty,
            external_srcs,
            external_dsts,
            external_digests)

    def cache(self,
            function_dirty,
            function_name,
            function_digest,
            call_id,
            bound,
            result,
            call_file_digests,
            external_srcs,
            external_dsts,
            external_digests):
        """Saves the function call into the database."""

        # Lock the db since we're updating data structures.
        if function_dirty:
            self._update_function(function_name, function_digest)

        # Get the real call_id to use in the call files.
        call_id = self._update_call(
            function_name,
            call_id,
            bound,
            result)

        self._update_call_files(
            call_id,
            function_name,
            call_file_digests)

        self._update_external_files(
            function_name,
            call_id,
            external_srcs,
            external_dsts,
            external_digests)

    # --------------------------------------------------------------------------

    def _check_function(self, function_name, function_digest):
        """Returns whether or not the function is dirty. Returns True or false
        as well as the function's digest."""
        try:
            old_digest = self._functions[function_name]
        except KeyError:
            # This is the first time we've seen this function.
            return True

        # Check if the function changed. If it didn't, assume that the function
        # didn't change either (although any sub-functions could have).
        if function_digest == old_digest:
            return False

        return True

    def _update_function(self, function, digest):
        """Insert or update the function's digest."""
        # Since the function changed, clear out all the related data.
        self.clear_function(function)

        self._functions[function] = digest

    def clear_function(self, name):
        """Clear the function from the database."""
        function_existed = False
        try:
            del self._functions[name]
        except KeyError:
            pass
        else:
            function_existed |= True

        # Since the function was removed, all of this function's
        # calls are dirty, so delete them.
        try:
            del self._function_calls[name]
        except KeyError:
            pass
        else:
            function_existed |= True

        try:
            del self._external_srcs[name]
        except KeyError:
            pass
        else:
            function_existed |= True

        try:
            del self._external_dsts[name]
        except KeyError:
            pass
        else:
            function_existed |= True

        # Since _call_files is indexed by filename, we need to search through
        # each item and delete any references to this function. The assumption
        # is that the files will change much less frequently compared to
        # functions, so we can have this be a more expensive call.
        remove_keys = []
        for key, value in self._call_files.items():
            try:
                del value[name]
            except KeyError:
                pass
            else:
                function_existed |= True

            if not value:
                remove_keys.append(key)

        # If any of the _call_files have no values, remove them.
        for key in remove_keys:
            try:
                del self._call_files[key]
            except KeyError:
                pass
            else:
                function_existed = True

        return function_existed

    # --------------------------------------------------------------------------

    def _check_call(self, function, bound):
        """Check if the function has been called before. Return the index if
        the call was cached, or None."""
        try:
            datas = self._function_calls[function]
        except KeyError:
            # This is the first time we've seen this function.
            return None, None

        # We've called this before, so search the data to see if we've called
        # it with the same arguments.
        for index, (old_bound, result) in enumerate(datas):
            if bound == old_bound:
                # We've found a matching call so just return the index.
                return index, result

        # Turns out we haven't called it with these args.
        return None, None

    def _update_call(self, function, call_id, bound, result):
        """Insert or update the function call."""
        try:
            datas = self._function_calls[function]
        except KeyError:
            # The function be new or may have been cleared. So ignore the
            # call_id and just create a new list.
            self._function_calls[function] = [(bound, result)]
            return 0
        else:
            if call_id is None:
                datas.append((bound, result))
                return len(datas) - 1
            else:
                datas[call_id] = (bound, result)
        return call_id

    # --------------------------------------------------------------------------

    def _check_call_files(self, call_id, function_name, filenames):
        """Returns all of the dirty call files."""
        digests = []
        for filename in filenames:
            d, digest = self._check_call_file(call_id, function_name, filename)
            if d:
                digests.append((filename, digest))

        return digests

    def _update_call_files(self, call_id, function_name, digests):
        """Insert or update the call files."""
        for src, digest in digests:
            self._update_call_file(call_id, function_name, src, digest)

    # --------------------------------------------------------------------------

    def _check_external_files(self, call_id, function_name):
        """Returns all of the externally specified call files, and the dirty
        list."""
        external_dirty = False
        digests = []
        try:
            srcs = self._external_srcs[function_name][call_id]
        except KeyError:
            srcs = set()
        else:
            for src in srcs:
                try:
                    d, digest = self._check_call_file(
                        call_id,
                        function_name,
                        src)
                except OSError:
                    external_dirty = True
                else:
                    if d:
                        digests.append((src, digest))

        try:
            dsts = self._external_dsts[function_name][call_id]
        except KeyError:
            dsts = set()

        return external_dirty, srcs, dsts, digests

    def _update_external_files(self,
            call_id,
            function_name,
            srcs,
            dsts,
            digests):
        """Insert or update the externall specified call files."""
        self._external_srcs.setdefault(function_name, {})[call_id] = srcs
        self._external_dsts.setdefault(function_name, {})[call_id] = dsts

        for src, digest in digests:
            self._update_call_file(call_id, function_name, src, digest)

    # --------------------------------------------------------------------------

    def _check_call_file(self, call_id, function_name, filename):
        """Returns if the call file is dirty and the file's digest."""

        # Compute the digest of the file.
        dirty, (mtime, digest) = self._add_file(filename)

        # If we don't have a valid call_id, then it's a new call.
        if call_id is None:
            return True, digest

        try:
            datas = self._call_files[filename]
        except KeyError:
            # This is the first time we've seen this call, so store it and
            # return True.
            return True, digest

        # We've called this before, lets see if we can find the file.
        try:
            old_digest = datas[function_name][call_id]
        except KeyError:
            # This is the first time we've seen this file, so store it and
            # return True.
            return True, digest

        # Now, check if the file changed from the previous run. If it did then
        # return True.
        if digest == old_digest:
            # We're okay, so return if the file's been updated.
            return dirty, digest
        else:
            # The digest's different, so we're dirty.
            return True, digest

    def _update_call_file(self, call_id, function_name, filename, digest):
        """Insert or update the call file."""
        self._call_files. \
            setdefault(filename, {}).\
            setdefault(function_name, {})[call_id] = digest

    # --------------------------------------------------------------------------

    def _add_file(self, filename):
        """Insert or update the file information. Returns True if the content
        of the file is different from what was in the table."""
        mtime = fbuild.path.Path(filename).getmtime()
        try:
            data = old_mtime, old_digest = self._files[filename]
        except KeyError:
            # This is the first time we've seen this file, so store it in the
            # table and return that this is new data.
            data = self._files[filename] = (
                mtime,
                fbuild.path.Path.digest(filename))
            return True, data

        # If the file was modified less than 1.0 seconds ago, recompute the
        # hash since it still could have changed even with the same mtime. If
        # True, then assume the file has not been modified.
        if mtime == old_mtime and time.time() - mtime > 1.0:
            return False, data

        # The mtime changed, but maybe the content didn't.
        digest = fbuild.path.Path.digest(filename)

        # If the file's contents didn't change, just return.
        if digest == old_digest:
            # The timestamp did change, so update the row.
            self._files[filename] = (mtime, old_digest)
            return False, data

        # Since the function changed, all of the calls that used this
        # function are dirty.
        self.clear_file(filename)

        # Now, add the file back to the database.
        data = self._files[filename] = (mtime, digest)

        # Returns True since the file changed.
        return True, data

    # --------------------------------------------------------------------------

    def clear_file(self, filename):
        """Remove the file from the database."""
        file_existed = False
        try:
            del self._files[filename]
        except KeyError:
            pass
        else:
            file_existed |= True

        # And clear all of the related call files.
        try:
            del self._call_files[filename]
        except KeyError:
            pass
        else:
            file_existed |= True

        return file_existed

# ------------------------------------------------------------------------------

class PersistentMeta(abc.ABCMeta):
    """A metaclass that searches the db for an already instantiated class with
    the same arguments.  It subclasses from ABCMeta so that subclasses can
    implement abstract methods."""
    def __call_super__(cls, *args, **kwargs):
        return super().__call__(*args, **kwargs)

    def __call__(cls, ctx, *args, **kwargs):
        result, srcs, objs = ctx.db.call(cls.__call_super__, ctx,
            *args, **kwargs)

        return result

class PersistentObject(metaclass=PersistentMeta):
    """An abstract baseclass that will cache instances in the database."""

    def __init__(self, ctx):
        self.ctx = ctx

# ------------------------------------------------------------------------------

class caches:
    """L{caches} decorates a function and caches the results.  The first
    argument of the function must be an instance of L{database}.

    >>> ctx = fbuild.context.make_default_context()
    >>> @caches
    ... def test(ctx):
    ...     print('running test')
    ...     return 5
    >>> test(ctx)
    running test
    5
    >>> test(ctx)
    5
    """

    def __init__(self, function):
        functools.update_wrapper(self, function)
        self.function = function

    def __call__(self, *args, **kwargs):
        result, srcs, dsts = self.call(*args, **kwargs)
        return result

    def call(self, ctx, *args, **kwargs):
        return ctx.db.call(self.function, ctx, *args, **kwargs)

class cachemethod:
    """L{cachemethod} decorates a method of a class to cache the results.

    >>> ctx = fbuild.context.make_default_context([])
    >>> class C:
    ...     def __init__(self, ctx):
    ...         self.ctx = ctx
    ...     @cachemethod
    ...     def test(self):
    ...         print('running test')
    ...         return 5
    >>> c = C(ctx)
    >>> c.test()
    running test
    5
    >>> c.test()
    5
    """
    def __init__(self, method):
        self.method = method

    def __get__(self, instance, owner):
        if instance is None:
            return self
        return cachemethod_wrapper(types.MethodType(self.method, instance))

class cachemethod_wrapper:
    def __init__(self, method):
        self.method = method

    def __call__(self, *args, **kwargs):
        result, srcs, dsts = self.call(*args, **kwargs)
        return result

    def call(self, *args, **kwargs):
        return self.method.__self__.ctx.db.call(self.method, *args, **kwargs)

class cacheproperty:
    """L{cacheproperty} acts like a normal I{property} but will memoize the
    result in the store.  The first argument of the function it wraps must be a
    store or a class that has has an attribute named I{store}.

    >>> ctx = fbuild.context.make_default_context([])
    >>> class C:
    ...     def __init__(self, ctx):
    ...         self.ctx = ctx
    ...     @cacheproperty
    ...     def test(self):
    ...         print('running test')
    ...         return 5
    >>> c = C(ctx)
    >>> c.test
    running test
    5
    >>> c.test
    5
    """
    def __init__(self, method):
        self.method = method

    def __get__(self, instance, owner):
        if instance is None:
            return self
        result, srcs, dsts = self.call(instance)
        return result

    def call(self, instance):
        return instance.ctx.db.call(types.MethodType(self.method, instance))

# ------------------------------------------------------------------------------

def add_external_dependencies_to_call(ctx, *, srcs=(), dsts=()):
    """When inside a cached method, register additional src dependencies for
    the call. This function can only be called from a cached function and will
    error out if it is called from an uncached function."""
    # Hack in additional dependencies
    i = 2
    try:
        while True:
            frame = fbuild.inspect.currentframe(i)
            try:
                if frame.f_code == ctx.db.call.__code__:
                    function_name = frame.f_locals['function_name']
                    call_id = frame.f_locals['call_id']
                    external_digests = frame.f_locals['external_digests']
                    external_srcs = frame.f_locals['external_srcs']
                    external_dsts = frame.f_locals['external_dsts']

                    for src in srcs:
                        external_srcs.add(src)
                        dirty, digest = ctx.db._rpc.call((
                            ctx.db._backend._check_call_file,
                            (call_id, function_name, src),
                            {}))
                        if dirty:
                            external_digests.append((src, digest))

                    external_dsts.update(dsts)
                i += 1
            finally:
                del frame
    except ValueError:
        pass
