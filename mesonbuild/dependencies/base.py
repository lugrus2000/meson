# Copyright 2013-2017 The Meson development team

# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at

#     http://www.apache.org/licenses/LICENSE-2.0

# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# This file contains the detection logic for external
# dependencies. Mostly just uses pkg-config but also contains
# custom logic for packages that don't provide them.

# Currently one file, should probably be split into a
# package before this gets too big.

import sys
import os, stat, glob, shutil
import sysconfig
from enum import Enum
from .. import mlog
from .. import mesonlib
from ..mesonlib import MesonException, flatten, version_compare_many, Popen_safe
from ..environment import detect_cpu_family


class DependencyException(MesonException):
    '''Exceptions raised while trying to find dependencies'''

class DependencyMethods(Enum):
    # Auto means to use whatever dependency checking mechanisms in whatever order meson thinks is best.
    AUTO = 'auto'
    PKGCONFIG = 'pkg-config'
    QMAKE = 'qmake'
    # Just specify the standard link arguments, assuming the operating system provides the library.
    SYSTEM = 'system'
    # Detect using sdl2-config
    SDLCONFIG = 'sdlconfig'
    # This is only supported on OSX - search the frameworks directory by name.
    EXTRAFRAMEWORK = 'extraframework'
    # Detect using the sysconfig module.
    SYSCONFIG = 'sysconfig'

class Dependency:
    def __init__(self, type_name, kwargs):
        self.name = "null"
        self.language = None
        self.is_found = False
        self.type_name = type_name
        method = DependencyMethods(kwargs.get('method', 'auto'))

        # Set the detection method. If the method is set to auto, use any available method.
        # If method is set to a specific string, allow only that detection method.
        if method == DependencyMethods.AUTO:
            self.methods = self.get_methods()
        elif method in self.get_methods():
            self.methods = [method]
        else:
            raise MesonException('Unsupported detection method: {}, allowed methods are {}'.format(method.value, mlog.format_list(map(lambda x: x.value, [DependencyMethods.AUTO] + self.get_methods()))))

    def __repr__(self):
        s = '<{0} {1}: {2}>'
        return s.format(self.__class__.__name__, self.name, self.is_found)

    def get_compile_args(self):
        return []

    def get_link_args(self):
        return []

    def found(self):
        return self.is_found

    def get_sources(self):
        """Source files that need to be added to the target.
        As an example, gtest-all.cc when using GTest."""
        return []

    def get_methods(self):
        return [DependencyMethods.AUTO]

    def get_name(self):
        return self.name

    def get_exe_args(self, compiler):
        return []

    def need_threads(self):
        return False

    def get_pkgconfig_variable(self, variable_name):
        raise MesonException('Tried to get a pkg-config variable from a non-pkgconfig dependency.')

class InternalDependency(Dependency):
    def __init__(self, version, incdirs, compile_args, link_args, libraries, sources, ext_deps):
        super().__init__('internal', {})
        self.version = version
        self.is_found = True
        self.include_directories = incdirs
        self.compile_args = compile_args
        self.link_args = link_args
        self.libraries = libraries
        self.sources = sources
        self.ext_deps = ext_deps

    def get_compile_args(self):
        return self.compile_args

    def get_link_args(self):
        return self.link_args

    def get_version(self):
        return self.version

class PkgConfigDependency(Dependency):
    # The class's copy of the pkg-config path. Avoids having to search for it
    # multiple times in the same Meson invocation.
    class_pkgbin = None

    def __init__(self, name, environment, kwargs):
        Dependency.__init__(self, 'pkgconfig', kwargs)
        self.is_libtool = False
        self.version_reqs = kwargs.get('version', None)
        self.required = kwargs.get('required', True)
        self.static = kwargs.get('static', False)
        self.silent = kwargs.get('silent', False)
        if not isinstance(self.static, bool):
            raise DependencyException('Static keyword must be boolean')
        # Store a copy of the pkg-config path on the object itself so it is
        # stored in the pickled coredata and recovered.
        self.pkgbin = None
        self.cargs = []
        self.libs = []
        if 'native' in kwargs and environment.is_cross_build():
            self.want_cross = not kwargs['native']
        else:
            self.want_cross = environment.is_cross_build()
        self.name = name
        self.modversion = 'none'

        # When finding dependencies for cross-compiling, we don't care about
        # the 'native' pkg-config
        if self.want_cross:
            if 'pkgconfig' not in environment.cross_info.config['binaries']:
                if self.required:
                    raise DependencyException('Pkg-config binary missing from cross file')
            else:
                pkgname = environment.cross_info.config['binaries']['pkgconfig']
                potential_pkgbin = ExternalProgram(pkgname, silent=True)
                if potential_pkgbin.found():
                    # FIXME, we should store all pkg-configs in ExternalPrograms.
                    # However that is too destabilizing a change to do just before release.
                    self.pkgbin = potential_pkgbin.get_command()[0]
                    PkgConfigDependency.class_pkgbin = self.pkgbin
                else:
                    mlog.debug('Cross pkg-config %s not found.' % potential_pkgbin.name)
        # Only search for the native pkg-config the first time and
        # store the result in the class definition
        elif PkgConfigDependency.class_pkgbin is None:
            self.pkgbin = self.check_pkgconfig()
            PkgConfigDependency.class_pkgbin = self.pkgbin
        else:
            self.pkgbin = PkgConfigDependency.class_pkgbin

        self.is_found = False
        if not self.pkgbin:
            if self.required:
                raise DependencyException('Pkg-config not found.')
            return
        if self.want_cross:
            self.type_string = 'Cross'
        else:
            self.type_string = 'Native'

        mlog.debug('Determining dependency {!r} with pkg-config executable '
                   '{!r}'.format(name, self.pkgbin))
        ret, self.modversion = self._call_pkgbin(['--modversion', name])
        if ret != 0:
            if self.required:
                raise DependencyException('{} dependency {!r} not found'
                                          ''.format(self.type_string, name))
            return
        found_msg = [self.type_string + ' dependency', mlog.bold(name), 'found:']
        if self.version_reqs is None:
            self.is_found = True
        else:
            if not isinstance(self.version_reqs, (str, list)):
                raise DependencyException('Version argument must be string or list.')
            if isinstance(self.version_reqs, str):
                self.version_reqs = [self.version_reqs]
            (self.is_found, not_found, found) = \
                version_compare_many(self.modversion, self.version_reqs)
            if not self.is_found:
                found_msg += [mlog.red('NO'),
                              'found {!r} but need:'.format(self.modversion),
                              ', '.join(["'{}'".format(e) for e in not_found])]
                if found:
                    found_msg += ['; matched:',
                                  ', '.join(["'{}'".format(e) for e in found])]
                if not self.silent:
                    mlog.log(*found_msg)
                if self.required:
                    m = 'Invalid version of dependency, need {!r} {!r} found {!r}.'
                    raise DependencyException(m.format(name, not_found, self.modversion))
                return
        found_msg += [mlog.green('YES'), self.modversion]
        # Fetch cargs to be used while using this dependency
        self._set_cargs()
        # Fetch the libraries and library paths needed for using this
        self._set_libs()
        # Print the found message only at the very end because fetching cflags
        # and libs can also fail if other needed pkg-config files aren't found.
        if not self.silent:
            mlog.log(*found_msg)

    def __repr__(self):
        s = '<{0} {1}: {2} {3}>'
        return s.format(self.__class__.__name__, self.name, self.is_found,
                        self.version_reqs)

    def _call_pkgbin(self, args):
        p, out = Popen_safe([self.pkgbin] + args, env=os.environ)[0:2]
        return p.returncode, out.strip()

    def _set_cargs(self):
        ret, out = self._call_pkgbin(['--cflags', self.name])
        if ret != 0:
            raise DependencyException('Could not generate cargs for %s:\n\n%s' %
                                      (self.name, out))
        self.cargs = out.split()

    def _set_libs(self):
        libcmd = [self.name, '--libs']
        if self.static:
            libcmd.append('--static')
        ret, out = self._call_pkgbin(libcmd)
        if ret != 0:
            raise DependencyException('Could not generate libs for %s:\n\n%s' %
                                      (self.name, out))
        self.libs = []
        for lib in out.split():
            if lib.endswith(".la"):
                shared_libname = self.extract_libtool_shlib(lib)
                shared_lib = os.path.join(os.path.dirname(lib), shared_libname)
                if not os.path.exists(shared_lib):
                    shared_lib = os.path.join(os.path.dirname(lib), ".libs", shared_libname)

                if not os.path.exists(shared_lib):
                    raise DependencyException('Got a libtools specific "%s" dependencies'
                                              'but we could not compute the actual shared'
                                              'library path' % lib)
                lib = shared_lib
                self.is_libtool = True
            self.libs.append(lib)

    def get_pkgconfig_variable(self, variable_name):
        ret, out = self._call_pkgbin(['--variable=' + variable_name, self.name])
        variable = ''
        if ret != 0:
            if self.required:
                raise DependencyException('%s dependency %s not found.' %
                                          (self.type_string, self.name))
        else:
            variable = out.strip()
        mlog.debug('Got pkgconfig variable %s : %s' % (variable_name, variable))
        return variable

    def get_modversion(self):
        return self.modversion

    def get_version(self):
        return self.modversion

    def get_compile_args(self):
        return self.cargs

    def get_link_args(self):
        return self.libs

    def get_methods(self):
        return [DependencyMethods.PKGCONFIG]

    def check_pkgconfig(self):
        evar = 'PKG_CONFIG'
        if evar in os.environ:
            pkgbin = os.environ[evar].strip()
        else:
            pkgbin = 'pkg-config'
        try:
            p, out = Popen_safe([pkgbin, '--version'])[0:2]
            if p.returncode != 0:
                # Set to False instead of None to signify that we've already
                # searched for it and not found it
                pkgbin = False
        except (FileNotFoundError, PermissionError):
            pkgbin = False
        if pkgbin and not os.path.isabs(pkgbin) and shutil.which(pkgbin):
            # Sometimes shutil.which fails where Popen succeeds, so
            # only find the abs path if it can be found by shutil.which
            pkgbin = shutil.which(pkgbin)
        if not self.silent:
            if pkgbin:
                mlog.log('Found pkg-config:', mlog.bold(pkgbin),
                         '(%s)' % out.strip())
            else:
                mlog.log('Found Pkg-config:', mlog.red('NO'))
        return pkgbin

    def found(self):
        return self.is_found

    def extract_field(self, la_file, fieldname):
        with open(la_file) as f:
            for line in f:
                arr = line.strip().split('=')
                if arr[0] == fieldname:
                    return arr[1][1:-1]
        return None

    def extract_dlname_field(self, la_file):
        return self.extract_field(la_file, 'dlname')

    def extract_libdir_field(self, la_file):
        return self.extract_field(la_file, 'libdir')

    def extract_libtool_shlib(self, la_file):
        '''
        Returns the path to the shared library
        corresponding to this .la file
        '''
        dlname = self.extract_dlname_field(la_file)
        if dlname is None:
            return None

        # Darwin uses absolute paths where possible; since the libtool files never
        # contain absolute paths, use the libdir field
        if mesonlib.is_osx():
            dlbasename = os.path.basename(dlname)
            libdir = self.extract_libdir_field(la_file)
            if libdir is None:
                return dlbasename
            return os.path.join(libdir, dlbasename)
        # From the comments in extract_libtool(), older libtools had
        # a path rather than the raw dlname
        return os.path.basename(dlname)


class ExternalProgram:
    windows_exts = ('exe', 'msc', 'com', 'bat')

    def __init__(self, name, command=None, silent=False, search_dir=None):
        self.name = name
        if command is not None:
            if not isinstance(command, list):
                self.command = [command]
            else:
                self.command = command
        else:
            self.command = self._search(name, search_dir)
        if not silent:
            if self.found():
                mlog.log('Program', mlog.bold(name), 'found:', mlog.green('YES'),
                         '(%s)' % ' '.join(self.command))
            else:
                mlog.log('Program', mlog.bold(name), 'found:', mlog.red('NO'))

    def __repr__(self):
        r = '<{} {!r} -> {!r}>'
        return r.format(self.__class__.__name__, self.name, self.command)

    @staticmethod
    def _shebang_to_cmd(script):
        """
        Check if the file has a shebang and manually parse it to figure out
        the interpreter to use. This is useful if the script is not executable
        or if we're on Windows (which does not understand shebangs).
        """
        try:
            with open(script) as f:
                first_line = f.readline().strip()
            if first_line.startswith('#!'):
                commands = first_line[2:].split('#')[0].strip().split()
                if mesonlib.is_windows():
                    # Windows does not have UNIX paths so remove them,
                    # but don't remove Windows paths
                    if commands[0].startswith('/'):
                        commands[0] = commands[0].split('/')[-1]
                    if len(commands) > 0 and commands[0] == 'env':
                        commands = commands[1:]
                    # Windows does not ship python3.exe, but we know the path to it
                    if len(commands) > 0 and commands[0] == 'python3':
                        commands[0] = sys.executable
                return commands + [script]
        except Exception:
            pass
        return False

    def _is_executable(self, path):
        suffix = os.path.splitext(path)[-1].lower()[1:]
        if mesonlib.is_windows():
            if suffix in self.windows_exts:
                return True
        elif os.access(path, os.X_OK):
            return not os.path.isdir(path)
        return False

    def _search_dir(self, name, search_dir):
        if search_dir is None:
            return False
        trial = os.path.join(search_dir, name)
        if os.path.exists(trial):
            if self._is_executable(trial):
                return [trial]
            # Now getting desperate. Maybe it is a script file that is
            # a) not chmodded executable, or
            # b) we are on windows so they can't be directly executed.
            return self._shebang_to_cmd(trial)
        else:
            if mesonlib.is_windows():
                for ext in self.windows_exts:
                    trial_ext = '{}.{}'.format(trial, ext)
                    if os.path.exists(trial_ext):
                        return [trial_ext]
        return False

    def _search(self, name, search_dir):
        '''
        Search in the specified dir for the specified executable by name
        and if not found search in PATH
        '''
        commands = self._search_dir(name, search_dir)
        if commands:
            return commands
        # Do a standard search in PATH
        command = shutil.which(name)
        if not mesonlib.is_windows():
            # On UNIX-like platforms, shutil.which() is enough to find
            # all executables whether in PATH or with an absolute path
            return [command]
        # HERE BEGINS THE TERROR OF WINDOWS
        if command:
            # On Windows, even if the PATH search returned a full path, we can't be
            # sure that it can be run directly if it's not a native executable.
            # For instance, interpreted scripts sometimes need to be run explicitly
            # with an interpreter if the file association is not done properly.
            name_ext = os.path.splitext(command)[1]
            if name_ext[1:].lower() in self.windows_exts:
                # Good, it can be directly executed
                return [command]
            # Try to extract the interpreter from the shebang
            commands = self._shebang_to_cmd(command)
            if commands:
                return commands
        else:
            # Maybe the name is an absolute path to a native Windows
            # executable, but without the extension. This is technically wrong,
            # but many people do it because it works in the MinGW shell.
            if os.path.isabs(name):
                for ext in self.windows_exts:
                    command = '{}.{}'.format(name, ext)
                    if os.path.exists(command):
                        return [command]
            # On Windows, interpreted scripts must have an extension otherwise they
            # cannot be found by a standard PATH search. So we do a custom search
            # where we manually search for a script with a shebang in PATH.
            search_dirs = os.environ.get('PATH', '').split(';')
            for search_dir in search_dirs:
                commands = self._search_dir(name, search_dir)
                if commands:
                    return commands
        return [None]

    def found(self):
        return self.command[0] is not None

    def get_command(self):
        return self.command[:]

    def get_path(self):
        if self.found():
            # Assume that the last element is the full path to the script or
            # binary being run
            return self.command[-1]
        return None

    def get_name(self):
        return self.name

class ExternalLibrary(Dependency):
    # TODO: Add `language` support to all Dependency objects so that languages
    # can be exposed for dependencies that support that (i.e., not pkg-config)
    def __init__(self, name, link_args, language, silent=False):
        super().__init__('external', {})
        self.name = name
        self.language = language
        self.is_found = False
        self.link_args = []
        self.lang_args = []
        if link_args:
            self.is_found = True
            if not isinstance(link_args, list):
                link_args = [link_args]
            self.lang_args = {language: link_args}
            # We special-case Vala for now till the Dependency object gets
            # proper support for exposing the language it was written in.
            # Without this, vala-specific link args will end up in the C link
            # args list if you link to a Vala library.
            # This hack use to be in CompilerHolder.find_library().
            if language != 'vala':
                self.link_args = link_args
        if not silent:
            if self.is_found:
                mlog.log('Library', mlog.bold(name), 'found:', mlog.green('YES'))
            else:
                mlog.log('Library', mlog.bold(name), 'found:', mlog.red('NO'))

    def found(self):
        return self.is_found

    def get_name(self):
        return self.name

    def get_link_args(self):
        return self.link_args

    def get_lang_args(self, lang):
        if lang in self.lang_args:
            return self.lang_args[lang]
        return []

class BoostDependency(Dependency):
    # Some boost libraries have different names for
    # their sources and libraries. This dict maps
    # between the two.
    name2lib = {'test': 'unit_test_framework'}

    def __init__(self, environment, kwargs):
        Dependency.__init__(self, 'boost', kwargs)
        self.name = 'boost'
        self.environment = environment
        self.libdir = ''
        if 'native' in kwargs and environment.is_cross_build():
            self.want_cross = not kwargs['native']
        else:
            self.want_cross = environment.is_cross_build()
        try:
            self.boost_root = os.environ['BOOST_ROOT']
            if not os.path.isabs(self.boost_root):
                raise DependencyException('BOOST_ROOT must be an absolute path.')
        except KeyError:
            self.boost_root = None
        if self.boost_root is None:
            if self.want_cross:
                if 'BOOST_INCLUDEDIR' in os.environ:
                    self.incdir = os.environ['BOOST_INCLUDEDIR']
                else:
                    raise DependencyException('BOOST_ROOT or BOOST_INCLUDEDIR is needed while cross-compiling')
            if mesonlib.is_windows():
                self.boost_root = self.detect_win_root()
                self.incdir = self.boost_root
            else:
                if 'BOOST_INCLUDEDIR' in os.environ:
                    self.incdir = os.environ['BOOST_INCLUDEDIR']
                else:
                    self.incdir = '/usr/include'
        else:
            self.incdir = os.path.join(self.boost_root, 'include')
        self.boost_inc_subdir = os.path.join(self.incdir, 'boost')
        mlog.debug('Boost library root dir is', self.boost_root)
        self.src_modules = {}
        self.lib_modules = {}
        self.lib_modules_mt = {}
        self.detect_version()
        self.requested_modules = self.get_requested(kwargs)
        module_str = ', '.join(self.requested_modules)
        if self.version is not None:
            self.detect_src_modules()
            self.detect_lib_modules()
            self.validate_requested()
            if self.boost_root is not None:
                info = self.version + ', ' + self.boost_root
            else:
                info = self.version
            mlog.log('Dependency Boost (%s) found:' % module_str, mlog.green('YES'), info)
        else:
            mlog.log("Dependency Boost (%s) found:" % module_str, mlog.red('NO'))
        if 'cpp' not in self.environment.coredata.compilers:
            raise DependencyException('Tried to use Boost but a C++ compiler is not defined.')
        self.cpp_compiler = self.environment.coredata.compilers['cpp']

    def detect_win_root(self):
        globtext = 'c:\\local\\boost_*'
        files = glob.glob(globtext)
        if len(files) > 0:
            return files[0]
        return 'C:\\'

    def get_compile_args(self):
        args = []
        include_dir = ''
        if self.boost_root is not None:
            if mesonlib.is_windows():
                include_dir = self.boost_root
            else:
                include_dir = os.path.join(self.boost_root, 'include')
        else:
            include_dir = self.incdir

        # Use "-isystem" when including boost headers instead of "-I"
        # to avoid compiler warnings/failures when "-Werror" is used

        # Careful not to use "-isystem" on default include dirs as it
        # breaks some of the headers for certain gcc versions

        # For example, doing g++ -isystem /usr/include on a simple
        # "int main()" source results in the error:
        # "/usr/include/c++/6.3.1/cstdlib:75:25: fatal error: stdlib.h: No such file or directory"

        # See https://gcc.gnu.org/bugzilla/show_bug.cgi?id=70129
        # and http://stackoverflow.com/questions/37218953/isystem-on-a-system-include-directory-causes-errors
        # for more details

        # TODO: The correct solution would probably be to ask the
        # compiler for it's default include paths (ie: "gcc -xc++ -E
        # -v -") and avoid including those with -isystem

        # For now, use -isystem for all includes except for some
        # typical defaults (which don't need to be included at all
        # since they are in the default include paths)
        if include_dir != '/usr/include' and include_dir != '/usr/local/include':
            args.append("".join(self.cpp_compiler.get_include_args(include_dir, True)))
        return args

    def get_requested(self, kwargs):
        candidates = kwargs.get('modules', [])
        if isinstance(candidates, str):
            return [candidates]
        for c in candidates:
            if not isinstance(c, str):
                raise DependencyException('Boost module argument is not a string.')
        return candidates

    def validate_requested(self):
        for m in self.requested_modules:
            if m not in self.src_modules:
                raise DependencyException('Requested Boost module "%s" not found.' % m)

    def found(self):
        return self.version is not None

    def get_version(self):
        return self.version

    def detect_version(self):
        try:
            ifile = open(os.path.join(self.boost_inc_subdir, 'version.hpp'))
        except FileNotFoundError:
            self.version = None
            return
        with ifile:
            for line in ifile:
                if line.startswith("#define") and 'BOOST_LIB_VERSION' in line:
                    ver = line.split()[-1]
                    ver = ver[1:-1]
                    self.version = ver.replace('_', '.')
                    return
        self.version = None

    def detect_src_modules(self):
        for entry in os.listdir(self.boost_inc_subdir):
            entry = os.path.join(self.boost_inc_subdir, entry)
            if stat.S_ISDIR(os.stat(entry).st_mode):
                self.src_modules[os.path.split(entry)[-1]] = True

    def detect_lib_modules(self):
        if mesonlib.is_windows():
            return self.detect_lib_modules_win()
        return self.detect_lib_modules_nix()

    def detect_lib_modules_win(self):
        arch = detect_cpu_family(self.environment.coredata.compilers)
        # Guess the libdir
        if arch == 'x86':
            gl = 'lib32*'
        elif arch == 'x86_64':
            gl = 'lib64*'
        else:
            # Does anyone do Boost cross-compiling to other archs on Windows?
            gl = None
        # See if the libdir is valid
        if gl:
            libdir = glob.glob(os.path.join(self.boost_root, gl))
        else:
            libdir = []
        # Can't find libdir, bail
        if not libdir:
            return
        libdir = libdir[0]
        self.libdir = libdir
        globber = 'boost_*-gd-*.lib' # FIXME
        for entry in glob.glob(os.path.join(libdir, globber)):
            (_, fname) = os.path.split(entry)
            base = fname.split('_', 1)[1]
            modname = base.split('-', 1)[0]
            self.lib_modules_mt[modname] = fname

    def detect_lib_modules_nix(self):
        if mesonlib.is_osx():
            libsuffix = 'dylib'
        else:
            libsuffix = 'so'

        globber = 'libboost_*.{}'.format(libsuffix)
        if 'BOOST_LIBRARYDIR' in os.environ:
            libdirs = [os.environ['BOOST_LIBRARYDIR']]
        elif self.boost_root is None:
            libdirs = mesonlib.get_library_dirs()
        else:
            libdirs = [os.path.join(self.boost_root, 'lib')]
        for libdir in libdirs:
            for entry in glob.glob(os.path.join(libdir, globber)):
                lib = os.path.basename(entry)
                name = lib.split('.')[0].split('_', 1)[-1]
                # I'm not 100% sure what to do here. Some distros
                # have modules such as thread only as -mt versions.
                if entry.endswith('-mt.so'):
                    self.lib_modules_mt[name] = True
                else:
                    self.lib_modules[name] = True

    def get_win_link_args(self):
        args = []
        if self.boost_root:
            args.append('-L' + self.libdir)
        for module in self.requested_modules:
            module = BoostDependency.name2lib.get(module, module)
            if module in self.lib_modules_mt:
                args.append(self.lib_modules_mt[module])
        return args

    def get_link_args(self):
        if mesonlib.is_windows():
            return self.get_win_link_args()
        args = []
        if self.boost_root:
            args.append('-L' + os.path.join(self.boost_root, 'lib'))
        elif 'BOOST_LIBRARYDIR' in os.environ:
            args.append('-L' + os.environ['BOOST_LIBRARYDIR'])
        for module in self.requested_modules:
            module = BoostDependency.name2lib.get(module, module)
            libname = 'boost_' + module
            # The compiler's library detector is the most reliable so use that first.
            default_detect = self.cpp_compiler.find_library(libname, self.environment, [])
            if default_detect is not None:
                if module == 'unit_testing_framework':
                    emon_args = self.cpp_compiler.find_library('boost_test_exec_monitor')
                else:
                    emon_args = None
                args += default_detect
                if emon_args is not None:
                    args += emon_args
            elif module in self.lib_modules or module in self.lib_modules_mt:
                linkcmd = '-l' + libname
                args.append(linkcmd)
                # FIXME a hack, but Boost's testing framework has a lot of
                # different options and it's hard to determine what to do
                # without feedback from actual users. Update this
                # as we get more bug reports.
                if module == 'unit_testing_framework':
                    args.append('-lboost_test_exec_monitor')
            elif module + '-mt' in self.lib_modules_mt:
                linkcmd = '-lboost_' + module + '-mt'
                args.append(linkcmd)
                if module == 'unit_testing_framework':
                    args.append('-lboost_test_exec_monitor-mt')
        return args

    def get_sources(self):
        return []

    def need_threads(self):
        return 'thread' in self.requested_modules


class AppleFrameworks(Dependency):
    def __init__(self, environment, kwargs):
        Dependency.__init__(self, 'appleframeworks', kwargs)
        modules = kwargs.get('modules', [])
        if isinstance(modules, str):
            modules = [modules]
        if not modules:
            raise DependencyException("AppleFrameworks dependency requires at least one module.")
        self.frameworks = modules

    def get_link_args(self):
        args = []
        for f in self.frameworks:
            args.append('-framework')
            args.append(f)
        return args

    def found(self):
        return mesonlib.is_osx()

    def get_version(self):
        return 'unknown'


class ExtraFrameworkDependency(Dependency):
    def __init__(self, name, required, path, kwargs):
        Dependency.__init__(self, 'extraframeworks', kwargs)
        self.name = None
        self.detect(name, path)
        if self.found():
            mlog.log('Dependency', mlog.bold(name), 'found:', mlog.green('YES'),
                     os.path.join(self.path, self.name))
        else:
            mlog.log('Dependency', name, 'found:', mlog.red('NO'))

    def detect(self, name, path):
        lname = name.lower()
        if path is None:
            paths = ['/Library/Frameworks']
        else:
            paths = [path]
        for p in paths:
            for d in os.listdir(p):
                fullpath = os.path.join(p, d)
                if lname != d.split('.')[0].lower():
                    continue
                if not stat.S_ISDIR(os.stat(fullpath).st_mode):
                    continue
                self.path = p
                self.name = d
                return

    def get_compile_args(self):
        if self.found():
            return ['-I' + os.path.join(self.path, self.name, 'Headers')]
        return []

    def get_link_args(self):
        if self.found():
            return ['-F' + self.path, '-framework', self.name.split('.')[0]]
        return []

    def found(self):
        return self.name is not None

    def get_version(self):
        return 'unknown'

class ThreadDependency(Dependency):
    def __init__(self, environment, kwargs):
        super().__init__('threads', {})
        self.name = 'threads'
        self.is_found = True
        mlog.log('Dependency', mlog.bold(self.name), 'found:', mlog.green('YES'))

    def need_threads(self):
        return True

    def get_version(self):
        return 'unknown'

class Python3Dependency(Dependency):
    def __init__(self, environment, kwargs):
        super().__init__('python3', kwargs)
        self.name = 'python3'
        self.is_found = False
        # We can only be sure that it is Python 3 at this point
        self.version = '3'
        if DependencyMethods.PKGCONFIG in self.methods:
            try:
                pkgdep = PkgConfigDependency('python3', environment, kwargs)
                if pkgdep.found():
                    self.cargs = pkgdep.cargs
                    self.libs = pkgdep.libs
                    self.version = pkgdep.get_version()
                    self.is_found = True
                    return
            except Exception:
                pass
        if not self.is_found:
            if mesonlib.is_windows() and DependencyMethods.SYSCONFIG in self.methods:
                self._find_libpy3_windows(environment)
            elif mesonlib.is_osx() and DependencyMethods.EXTRAFRAMEWORK in self.methods:
                # In OSX the Python 3 framework does not have a version
                # number in its name.
                fw = ExtraFrameworkDependency('python', False, None, kwargs)
                if fw.found():
                    self.cargs = fw.get_compile_args()
                    self.libs = fw.get_link_args()
                    self.is_found = True
        if self.is_found:
            mlog.log('Dependency', mlog.bold(self.name), 'found:', mlog.green('YES'))
        else:
            mlog.log('Dependency', mlog.bold(self.name), 'found:', mlog.red('NO'))

    def _find_libpy3_windows(self, env):
        '''
        Find python3 libraries on Windows and also verify that the arch matches
        what we are building for.
        '''
        pyarch = sysconfig.get_platform()
        arch = detect_cpu_family(env.coredata.compilers)
        if arch == 'x86':
            arch = '32'
        elif arch == 'x86_64':
            arch = '64'
        else:
            # We can't cross-compile Python 3 dependencies on Windows yet
            mlog.log('Unknown architecture {!r} for'.format(arch),
                     mlog.bold(self.name))
            self.is_found = False
            return
        # Pyarch ends in '32' or '64'
        if arch != pyarch[-2:]:
            mlog.log('Need', mlog.bold(self.name),
                     'for {}-bit, but found {}-bit'.format(arch, pyarch[-2:]))
            self.is_found = False
            return
        inc = sysconfig.get_path('include')
        platinc = sysconfig.get_path('platinclude')
        self.cargs = ['-I' + inc]
        if inc != platinc:
            self.cargs.append('-I' + platinc)
        # Nothing exposes this directly that I coulf find
        basedir = sysconfig.get_config_var('base')
        vernum = sysconfig.get_config_var('py_version_nodot')
        self.libs = ['-L{}/libs'.format(basedir),
                     '-lpython{}'.format(vernum)]
        self.version = sysconfig.get_config_var('py_version_short')
        self.is_found = True

    def get_compile_args(self):
        return self.cargs

    def get_link_args(self):
        return self.libs

    def get_methods(self):
        if mesonlib.is_windows():
            return [DependencyMethods.PKGCONFIG, DependencyMethods.SYSCONFIG]
        elif mesonlib.is_osx():
            return [DependencyMethods.PKGCONFIG, DependencyMethods.EXTRAFRAMEWORK]
        else:
            return [DependencyMethods.PKGCONFIG]

    def get_version(self):
        return self.version


def get_dep_identifier(name, kwargs, want_cross):
    # Need immutable objects since the identifier will be used as a dict key
    version_reqs = flatten(kwargs.get('version', []))
    if isinstance(version_reqs, list):
        version_reqs = frozenset(version_reqs)
    identifier = (name, version_reqs, want_cross)
    for key, value in kwargs.items():
        # 'version' is embedded above as the second element for easy access
        # 'native' is handled above with `want_cross`
        # 'required' is irrelevant for caching; the caller handles it separately
        # 'fallback' subprojects cannot be cached -- they must be initialized
        if key in ('version', 'native', 'required', 'fallback',):
            continue
        # All keyword arguments are strings, ints, or lists (or lists of lists)
        if isinstance(value, list):
            value = frozenset(flatten(value))
        identifier += (key, value)
    return identifier

def find_external_dependency(name, environment, kwargs):
    required = kwargs.get('required', True)
    if not isinstance(required, bool):
        raise DependencyException('Keyword "required" must be a boolean.')
    if not isinstance(kwargs.get('method', ''), str):
        raise DependencyException('Keyword "method" must be a string.')
    lname = name.lower()
    if lname in packages:
        dep = packages[lname](environment, kwargs)
        if required and not dep.found():
            raise DependencyException('Dependency "%s" not found' % name)
        return dep
    pkg_exc = None
    pkgdep = None
    try:
        pkgdep = PkgConfigDependency(name, environment, kwargs)
        if pkgdep.found():
            return pkgdep
    except Exception as e:
        pkg_exc = e
    if mesonlib.is_osx():
        fwdep = ExtraFrameworkDependency(name, required, None, kwargs)
        if required and not fwdep.found():
            m = 'Dependency {!r} not found, tried Extra Frameworks ' \
                'and Pkg-Config:\n\n' + str(pkg_exc)
            raise DependencyException(m.format(name))
        return fwdep
    if pkg_exc is not None:
        raise pkg_exc
    mlog.log('Dependency', mlog.bold(name), 'found:', mlog.red('NO'))
    return pkgdep


# This has to be at the end so the classes it references
# are defined.
packages = {'boost': BoostDependency,
            'appleframeworks': AppleFrameworks,
            'threads': ThreadDependency,
            'python3': Python3Dependency,
            }
