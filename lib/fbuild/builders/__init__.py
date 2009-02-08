import abc
import os
import sys

import fbuild
import fbuild.db
import fbuild.path
import fbuild.temp

# ------------------------------------------------------------------------------

class MissingProgram(fbuild.ConfigFailed):
    def __init__(self, programs=None):
        self.programs = programs

    def __str__(self):
        if self.programs is None:
            return 'cannot find program'
        else:
            return 'cannot find any of the programs %s' % \
                ' '.join(repr(str(p)) for p in self.programs)

# ------------------------------------------------------------------------------

@fbuild.db.caches
def find_program(names, paths=None, *, quieter=0):
    """L{find_program} is a test that searches the paths for one of the
    programs in I{name}.  If one is found, it is returned.  If not, the next
    name in the list is searched for."""

    if paths is None:
        paths = os.environ['PATH'].split(os.pathsep)

    # If we're running on windows, we need to append '.exe' to the filenames
    # that we're searching for.
    if sys.platform == 'win32':
        new_names = []
        for name in names:
            new_names.append(name)
            if not name.endswith('.exe'):
                new_names.append(name + '.exe')
        names = new_names

    for name in names:
        fbuild.logger.check('looking for program ' + name, verbose=quieter)

        filename = fbuild.path.Path(name)
        if filename.exists():
            fbuild.logger.passed('ok %s' % filename, verbose=quieter)
            return filename
        else:
            for path in paths:
                filename = fbuild.path.Path(path, name)
                if filename.exists():
                    fbuild.logger.passed('ok %s' % filename, verbose=quieter)
                    return filename

        fbuild.logger.failed(verbose=quieter)

    raise MissingProgram(names)

# ------------------------------------------------------------------------------

class AbstractCompiler(fbuild.db.PersistentObject):
    def __init__(self, *, src_suffix):
        self.src_suffix = src_suffix

    @abc.abstractmethod
    def compile(self, src, *args, **kwargs):
        pass

    @abc.abstractmethod
    def uncached_compile(self, src, *args, **kwargs):
        pass

    @abc.abstractmethod
    def build_objects(self, srcs, *args, **kwargs):
        pass

    # --------------------------------------------------------------------------

    def tempfile(self, code):
        return fbuild.temp.tempfile(code, self.src_suffix)

    def try_compile(self, code='', *, quieter=1, **kwargs):
        with self.tempfile(code) as src:
            try:
                self.uncached_compile(src, quieter=quieter, **kwargs)
            except fbuild.ExecutionError:
                return False
            else:
                return True

    def check_compile(self, code, msg, *args, **kwargs):
        fbuild.logger.check(msg)
        if self.try_compile(code, *args, **kwargs):
            fbuild.logger.passed()
            return True
        else:
            fbuild.logger.failed()
            return False

# ------------------------------------------------------------------------------

class AbstractLibLinker(AbstractCompiler):
    @abc.abstractmethod
    def link_lib(self, *args, **kwargs):
        pass

    @abc.abstractmethod
    def uncached_link_lib(self, *args, **kwargs):
        pass

    @abc.abstractmethod
    def build_lib(self, dst, srcs, *args, **kwargs):
        pass

    # --------------------------------------------------------------------------

    def try_link_lib(self, code='', *, quieter=1, ckwargs={}, lkwargs={}):
        with self.tempfile(code) as src:
            dst = src.parent / 'temp'
            try:
                obj = self.uncached_compile(src, quieter=quieter, **ckwargs)
                self.uncached_link_lib(dst, [obj], quieter=quieter, **lkwargs)
            except fbuild.ExecutionError:
                return False
            else:
                return True

    def check_link_lib(self, code, msg, *args, **kwargs):
        fbuild.logger.check(msg)
        if self.try_link_lib(code, *args, **kwargs):
            fbuild.logger.passed()
            return True
        else:
            fbuild.logger.failed()
            return False

# ------------------------------------------------------------------------------

class AbstractRunner(fbuild.db.PersistentObject):
    @abc.abstractmethod
    def tempfile_run(self, *args, **kwargs):
        pass

    def try_run(self, code='', quieter=1, **kwargs):
        try:
            self.tempfile_run(code, quieter=quieter, **kwargs)
        except fbuild.ExecutionError:
            return False
        else:
            return True

    def check_run(self, code, msg, *args, **kwargs):
        fbuild.logger.check(msg)
        if self.try_run(code, *args, **kwargs):
            fbuild.logger.passed()
            return True
        else:
            fbuild.logger.failed()
            return False

# ------------------------------------------------------------------------------

class AbstractExeLinker(AbstractCompiler, AbstractRunner):
    @abc.abstractmethod
    def link_exe(self, *args, **kwargs):
        pass

    @abc.abstractmethod
    def uncached_link_exe(self, *args, **kwargs):
        pass

    @abc.abstractmethod
    def build_exe(self, dst, srcs, *args, **kwargs):
        pass

    # --------------------------------------------------------------------------

    def try_link_exe(self, code='', *, quieter=1, ckwargs={}, lkwargs={}):
        with self.tempfile(code) as src:
            dst = src.parent / 'temp'
            try:
                obj = self.uncached_compile(src, quieter=quieter, **ckwargs)
                self.uncached_link_exe(dst, [obj], quieter=quieter, **lkwargs)
            except fbuild.ExecutionError:
                return False
            else:
                return True

    def check_link_exe(self, code, msg, *args, **kwargs):
        fbuild.logger.check(msg)
        if self.try_link_exe(code, *args, **kwargs):
            fbuild.logger.passed()
            return True
        else:
            fbuild.logger.failed()
            return False

    def tempfile_run(self, code='', *, quieter=1, ckwargs={}, lkwargs={},
            **kwargs):
        with self.tempfile(code) as src:
            dst = src.parent / 'temp'
            obj = self.uncached_compile(src, quieter=quieter, **ckwargs)
            exe = self.uncached_link_exe(dst, [obj], quieter=quieter, **lkwargs)
            return fbuild.execute([exe], quieter=quieter, **kwargs)

# ------------------------------------------------------------------------------

class AbstractCompilerBuilder(AbstractLibLinker, AbstractExeLinker):
    pass
