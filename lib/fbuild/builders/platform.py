import os

from fbuild import logger, execute, ConfigFailed, ExecutionError

class UnknownPlatform(ConfigFailed):
    def __init__(self, platform=None):
        self.platform = platform

    def __str__(self):
        if self.platform is None:
            return 'cannot determine platform'
        else:
            return 'unknown platform: "%s"' % self.platform

# -----------------------------------------------------------------------------

archmap = {
    'irix':      {'posix', 'irix'},
    'irix64':    {'posix', 'irix', 'irix64'},
    'unix':      {'posix'},
    'posix':     {'posix'},
    'linux':     {'posix', 'linux'},
    'gnu/linux': {'posix', 'linux'},
    'solaris':   {'posix', 'solaris'},
    'sunos':     {'posix', 'solaris', 'sunos'},
    'cygwin':    {'posix', 'cygwin'},
    'nocygwin':  {'posix', 'cygwin', 'nocygwin'},
    'mingw':     {'posix', 'mingw'},
    'windows':   {'windows', 'win32'},
    'nt':        {'windows', 'win32', 'nt'},
    'win32':     {'windows', 'win32'},
    'win64':     {'windows', 'win64'},
    'freebsd':   {'posix', 'bsd', 'freebsd'},
    'netbsd':    {'posix', 'bsd', 'netbsd'},
    'openbsd':   {'posix', 'bsd', 'openbsd'},
    'darwin':    {'posix', 'bsd', 'darwin'},
    'osx':       {'posix', 'bsd', 'darwin'},
}

# -----------------------------------------------------------------------------

def config(conf, platform=None):
    try:
        return conf['platform']
    except KeyError:
        pass

    logger.check('determining platform')
    if platform is None:
        try:
            stdout, stderr = execute(('uname', '-s'), quieter=1)
        except ExecutionError:
            platform = os.name
        else:
            platform = stdout.decode('utf-8').strip().lower()

    try:
        conf['platform'] = archmap[platform]
    except KeyError:
        logger.log('failed', color='yellow')
        raise UnknownPlatform(platform)
    else:
        logger.log(conf['platform'], color='green')

    return conf['platform']