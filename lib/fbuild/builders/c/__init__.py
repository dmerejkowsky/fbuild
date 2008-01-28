import os
import io
import textwrap
import contextlib

import yaml

import fbuild
import fbuild.temp
import fbuild.builders

# -----------------------------------------------------------------------------

@contextlib.contextmanager
def tempfile(code=None, headers=[], name='temp', suffix='.c'):
    code = code or 'int main(int argc, char** argv) { return 0; }'
    src = io.StringIO()

    for header in headers: print('#include <%s>' % header, file=src)
    print('#ifdef __cplusplus', file=src)
    print('extern "C" {', file=src)
    print('#endif', file=src)
    print(textwrap.dedent(code), file=src)
    print('#ifdef __cplusplus', file=src)
    print('}', file=src)
    print('#endif', file=src)

    with fbuild.temp.tempdir() as dirname:
        name = os.path.join(dirname, name + suffix)
        with open(name, 'w') as f:
            print(src.getvalue(), file=f)

        yield name

# -----------------------------------------------------------------------------

class Builder:
    def __init__(self, system, *, src_suffix):
        self.system = system
        self.src_suffix = src_suffix

    # -------------------------------------------------------------------------

    def check(self, *args, **kwargs):
        return self.system.check(*args, **kwargs)

    def log(self, *args, **kwargs):
        return self.system.log(*args, **kwargs)

    # -------------------------------------------------------------------------

    def compile(self, *args, **kwargs):
        raise NotImplemented


    def link_lib(self, *args, **kwargs):
        raise NotImplemented


    def link_exe(self, *args, **kwargs):
        raise NotImplemented

    # -------------------------------------------------------------------------

    def check_header_exists(self, header, *, **kwargs):
        self.check('checking if header %r exists' % header)
        if self.try_compile(headers=[header], **kwargs):
            self.log('yes', color='green')
            return True
        else:
            self.log('no', color='yellow')
            return False


    def check_macro_exists(self, macro, *, **kwargs):
        code = '''
            #ifndef %s
            #error %s
            #endif
        ''' % (macro, macro)

        self.check('checking if macro %r exists' % macro)
        if self.try_compile(code, **kwargs):
            self.log('yes', color='green')
            return True
        else:
            self.log('no', color='yellow')
            return False

    def check_type_exists(self, typename, *, **kwargs):
        code = '%s x;' % typename

        self.check('checking if type %r exists' % typename)
        if self.try_compile(code, **kwargs):
            self.log('yes', color='green')
            return True
        else:
            self.log('no', color='yellow')
            return False

    # -------------------------------------------------------------------------

    def try_compile(self, code=None, headers=[], quieter=1, **kwargs):
        with tempfile(code, headers, suffix=self.src_suffix) as src:
            try:
                self.compile([src], quieter=quieter, **kwargs)
            except fbuild.ExecutionError:
                self.log(code, verbose=1)
                return False
            else:
                return True


    def try_link_lib(self, code,
            headers=[],
            quieter=1,
            cflags={},
            lflags={}):
        with tempfile(code, headers, suffix=self.src_suffix) as src:
            dst = os.path.join(os.path.dirname(src), 'temp')
            try:
                objects = self.compile([src], quieter=quieter, **cflags)
                self.link_lib(dst, objects, quieter=quieter, **lflags)
            except fbuild.ExecutionError:
                return False
            else:
                return True


    def try_link_exe(self, code,
            headers=[],
            quieter=1,
            cflags={},
            lflags={}):
        with tempfile(code, headers, suffix=self.src_suffix) as src:
            dst = os.path.join(os.path.dirname(src), 'temp')
            try:
                objects = self.compile([src], quieter=quieter, **cflags)
                self.link_exe(dst, objects, quieter=quieter, **lflags)
            except fbuild.ExecutionError:
                return False
            else:
                return True


    def tempfile_run(self, code,
            headers=[],
            quieter=1,
            cflags={},
            lflags={}):
        with tempfile(code, headers, suffix=self.src_suffix) as src:
            dst = os.path.join(os.path.dirname(src), 'temp')
            objects = self.compile([src], quieter=quieter, **cflags)
            exe = self.link_exe(dst, objects, quieter=quieter, **lflags)
            return self.system.execute([exe], quieter=quieter)


    def try_run(self, *args, **kwargs):
        try:
            self.tempfile_run(*args, **kwargs)
        except fbuild.ExecutionError:
            return False
        else:
            return True


    def check_compile(self, code, msg, *args, **kwargs):
        self.check(msg)
        if self.try_compile(code, *args, **kwargs):
            self.log('yes', color='green')
            return True
        else:
            self.log('no', color='yellow')
            return False


    def check_run(self, code, msg, *args, **kwargs):
        self.check(msg)
        if self.try_run(code, *args, **kwargs):
            self.log('yes', color='green')
            return True
        else:
            self.log('no', color='yellow')
            return False

# -----------------------------------------------------------------------------

def check_builder(builder):
    builder.check('checking if "%s" can make objects' % builder.compiler)
    code = 'int main(int argc, char** argv) { return 0; }'
    if builder.try_tempfile_compile(code):
        builder.log('ok', color='green')
    else:
        raise fbuild.ConfigFailed('compiler failed')


    builder.check('checking if "%s" can make libraries' % builder.lib_linker)
    code = 'int foo() { return 5; }'
    if builder.try_tempfile_link_lib(code):
        builder.log('ok', color='green')
    else:
        raise fbuild.ConfigFailed('lib linker failed')


    builder.check('checking if "%s" can make exes' % builder.exe_linker)
    code = 'int main(int argc, char** argv) { return 0; }'
    if builder.try_tempfile_run(code):
        builder.log('ok', color='green')
    else:
        raise fbuild.ConfigFailed('exe linker failed')


    builder.check('Checking linking lib to exe')
    with fbuild.temp.tempdir() as dirname:
        src_lib = os.path.join(dirname, 'templib' + builder.src_suffix)
        with open(src_lib, 'w') as f:
            print('int foo() { return 5; }', file=f)

        src_exe = os.path.join(dirname, 'tempexe' + builder.src_suffix)
        with open(src_exe, 'w') as f:
            print('#include <stdio.h>', file=f)
            print('extern int foo();', file=f)
            print('int main(int argc, char** argv) {', file=f)
            print('  printf("%d\\n", foo());', file=f)
            print('  return 0;', file=f)
            print('}', file=f)


        objs = builder.compile([src_lib], quieter=1)
        lib = builder.link_lib(os.path.join(dirname, 'temp'), objs, quieter=1)

        objs = builder.compile([src_exe], quieter=1)
        exe = builder.link_exe(os.path.join(dirname, 'temp'), objs,
            libs=[lib],
            quieter=1)

        try:
            stdout, stderr = builder.system.execute([exe], quieter=1)
        except fbuild.ExecutionError:
            raise fbuild.ConfigFailed('failed to link lib to exe')
        else:
            if stdout != b'5\n':
                raise fbuild.ConfigFailed('failed to link lib to exe')
            builder.log('ok', color='green')

# -----------------------------------------------------------------------------

def make_builder(system, Compiler, LibLinker, ExeLinker,
        src_suffix, obj_suffix,
        lib_prefix, lib_suffix,
        exe_suffix,
        compile_flags=[],
        lib_link_flags=[],
        exe_link_flags=[]):
    builder = Builder(system, src_suffix,
        Compiler(system,
            suffix=obj_suffix,
        ),
        LibLinker(system,
            prefix=lib_prefix,
            suffix=lib_suffix,
            lib_prefix=lib_prefix,
            lib_suffix=lib_suffix,
        ),
        ExeLinker(system,
            suffix=exe_suffix,
            lib_prefix=lib_prefix,
            lib_suffix=lib_suffix,
        ),
    )

    check_builder(builder)

    return builder

# -----------------------------------------------------------------------------

def config_compile_flags(builder, flags):
    builder.check('checking if "%s" supports %s' % (builder.compiler, flags))

    if builder.try_tempfile_compile(flags=flags):
        builder.log('ok', color='green')
        return True

    builder.log('failed', color='yellow')
    return False

# -----------------------------------------------------------------------------

def check_compiler(compiler, suffix):
    compiler.check('checking if "%s" can make objects' % compiler)

    with compiler.tempfile(suffix=suffix) as f:
        try:
            compiler([f], quieter=1)
        except fbuild.ExecutionError as e:
            raise fbuild.ConfigFailed('compiler failed') from e

    compiler.log('ok', color='green')

# -----------------------------------------------------------------------------

def detect_little_endian(builder):
    code = '''
        #include <stdio.h>

        enum enum_t {e_tag};
        typedef void (*fp_t)(void);

        union endian_t {
            unsigned long x;
            unsigned char y[sizeof(unsigned long)];
        } endian;

        int main(int argc, char** argv) {
            endian.x = 1ul;
            printf("%d\\n", endian.y[0]);
            return 0;
        }
    '''

    builder.check('checking if little endian')
    try:
        stdout = 1 == int(builder.tempfile_run(code)[0])
    except fbuild.ExecutionError:
        builder.log('failed', color='yellow')
        raise ConfigFailed('failed to detect endianness')

    little_endian = int(stdout) == 1

    builder.log(little_endian, color='green')

    return little_endian


def config_little_endian(conf, builder):
    conf.configure('little_endian', detect_little_endian, builder)
