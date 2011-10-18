#  maitch.py - Simple but flexible build system tool
#  Copyright (C) 2011 Tony Houghton <h@realh.co.uk>
#
#  This program is free software; you can redistribute it and/or modify it
#  under the terms of the GNU Lesser General Public License as published by the
#  Free Software Foundation; either version 3 of the License, or (at your
#  option) any later version.
#
#  This program is distributed in the hope that it will be useful, but WITHOUT
#  ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or
#  FITNESS FOR A PARTICULAR PURPOSE.  See the GNU Lesser General Public License
#  for more details.
#
#  You should have received a copy of the GNU Lesser General Public License
#  along with this program; if not, write to the Free Software Foundation,
#  Inc., 59 Temple Place, Suite 330, Boston, MA  02111-1307  USA

import atexit
import fnmatch
import os
import re
import subprocess
import sys
import threading
from curses import ascii

from lockfile import FileLock


# Where to look for sources, may be combined with bitwise or
SRC = 1
TOP = 2


class MaitchError(Exception):
    pass

class MaitchRuleError(MaitchError):
    pass

class MaitchArgError(MaitchError):
    pass

class MaitchChildError(MaitchError):
    pass

class MaitchDirError(MaitchError):
    pass

class MaitchNotFoundError(MaitchError):
    pass

class MaitchRecursionError(MaitchError):
    pass

class MaitchJobError(MaitchError):
    pass



# subst behaviour if a variable isn't found
NOVAR_FATAL = 0     # Raise exception
NOVAR_BLANK = 1     # Replace with empty string
NOVAR_SKIP = 2      # Leave ${} construct untouched



class Context(object):
    " The fundamental build context. "
    
    def __init__(self, **kwargs):
        """
        All kwargs are added to context's env. They may include:
        
        PACKAGE = package name (compulsory).
        BUILD_DIR = where to output all generated files, defaults to
                "${MSCRIPT_REAL_DIR}/build". ${BUILD_DIR} is used as the cwd for
                all commands and other directories should be specified relative
                to it.
        TOP_DIR = top level of package directory, defaults to ".."
        SRC_DIR = top level of source directory, defaults to ${TOP_DIR}.
        
        
        Special env variables. These are expanded in all modes, not just
        configure. They are not saved in the env file.
        
        MSCRIPT_DIR: The directory containing the executable script running
                the build (sys.argv[0]). Symlinks are not followed.
        MSCRIPT_REAL_DIR: MSCRIPT_DIR with symlinks followed.
        
        
        Useful attributes:
        
        env: context's variables
        mode: mode of build, one of "configure", "build", "install" or "dist";
                taken from sys.argv[1]
        build_dir, src_dir, top_dir: Expanded versions of above variables. Bear
                in mind that these are expanded before you have a chance to add
                any vars to env after constructing the ctx:- they may refer to
                each other (non-recursively) but to no other variables.
        
        Notes:
        
        All "nodes" (sources, targets etc) are expressed as strings.
        See process_nodes() for how they are accepted as arguments.
        """
        self.lock = threading.RLock()
        self.tmpfile_index = 0
        
        # Make sure a package name was specified
        self.package_name = kwargs['PACKAGE']
        
        # Check mode
        syntax = False
        if len(sys.argv) < 2:
            syntax = True
        else:
            self.mode = sys.argv[1]
        if not syntax and not self.mode in \
                "help configure build dist install clean".split():
            syntax = True
        if syntax:
            self.mode == 'help'
        if self.mode == 'help':
            sys.stdout.write("""Help for maitch:
USAGE:
  ./mscript help
  ./mscript configure [ARGS]
  ./mscript build [TARGETS]
  ./mscript install [SOURCES]
  ./mscript clean
  ./mscript clean_fatal

ARGS may be specified in the form VAR=value, or VAR on its own for True.
Special variables may also be specified in the form --var or --var=value.

Predefined variables and their default values:
""")
            for v in _var_repository:
                if v[3]:
                    alt = "/%s" % var_to_arg(v[0])
                else:
                    alt = ""
                if callable(v[1]):
                    default = 'dynamic'
                else:
                    default = v[1]
                sys.stdout.write("  %s%s [%s]: %s\n" %
                        (v[0], alt, default, v[2]))
            self.showed_var_header = False
            if syntax:
                sys.exit(0)
            else:
                return
        
        self.env = {}
        
        # We have to get BUILD_DIR from kwargs early one. Setting it from
        # CLI is disallowed
        self.get_build_dir(kwargs)
        self.build_dir = self.subst(self.env['BUILD_DIR'])
        self.ensure_out_dir(self.build_dir)
        # This message is to help text editors find the cwd in case errors
        # are reported relative to it
        sys.stdout.write('make[0]: Entering directory "%s"\n' % self.build_dir)
        os.chdir(self.build_dir)
        
        # Get lock on BUILD_DIR
        global _lock_file
        f = self.get_lock_file_name()
        self.ensure_out_dir_for_file(f)
        _lock_file = FileLock(f)
        _lock_file.acquire(0)
        atexit.register(lambda x: x.release(), _lock_file)
        
        # If not in configure mode load a previous saved env. 
        if self.mode != 'configure':
            n = self.env_file_name()
            if os.path.exists(n):
                fp = open(n, 'r')
                for l in fp.readlines():
                    k, v = l.split('=', 1)
                    if k != 'BUILD_DIR':
                        self.env[k] = v.rstrip()
                fp.close()
        
        # Get more env vars from kwargs
        for k, v in kwargs.items():
            self.env[k] = v
        
        self.explicit_rules = {}
        self.implicit_rules = {}
        
        self.cli_targets = []
        
        # Process command-line args
        if self.mode == 'configure' and len(sys.argv) > 2:
            special_vars = []
            for v in _var_repository:
                if v[3]:
                    special_vars.append(v[0])
            for a in sys.argv[2:]:
                if '=' in a:
                    k, v = a.split('=', 1)
                else:
                    k = a
                    v = True
                if k.startswith('--'):
                    k_ = k
                    k = arg_to_var(k)
                    if not k in special_vars:
                        raise MaitchArgError("Invalid argument '%s'" % k_)
                if k == 'BUILD_DIR':
                    raise MaitchDirError("BUILD_DIR may only be set "
                            "in Context constructor")
                elif self.var_is_special(k):
                    raise MaitchDirError("%s is a reserved variable" % k)
                self.env[k] = v
        elif self.mode =='build' and len(sys.argv) > 2:
            for a in sys.argv[2:]:
                self.cli_targets.append(a)
        
        # Set defaults
        for v in _var_repository:
            if not self.env.get(v[0]):
                if callable(v[1]):
                    d = v[1](v[0])
                else:
                    d = v[1]
                self.env[v[0]] = d
        
        self.top_dir = self.subst(self.env['TOP_DIR'])
        self.src_dir = self.subst(self.env['SRC_DIR'])
        self.check_build_dir()
        self.dest_dir = self.subst(self.env['DESTDIR'])
        
        self.definitions = {}
    
    
    def define(self, key, value):
        """ Define a variable to be used in the build. At the moment the only
        supported method is to output a C header ${BUILD_DIR}/config.h at the
        end of the configure phase. Definitions are of the form:
        #define key value
        where double quotes are automatically added for string values unless
        already enclosed in single quotes. True and False are converted to 1 and
        0, while a value of None results in:
        #undef key
        being written instead. """
        self.definitions[key] = value
    
    
    def define_from_var(self, key, default = None):
        """ Calls self.define() with env variable's value. """
        self.define(key, self.env.get(key, default))
    
    def __arg(self, help_name, help_body, var, antivar, default):
        if self.mode == 'help':
            if not self.showed_var_header:
                sys.stdout.write("\n%s supports these configure options:\n\n",
                        self.package_name)
                self.showed_var_header = True
            print_formatted(help, 80, help_name, 20)
            sys.stdout.write("\n")
        else:
            if self.env.get(antivar):
                self.setenv(var, False)
            elif self.env.get(var) == None:
                if callable(default):
                    default = default(self, var)
                self.setenv(var, default)
    
    
    def arg_enable(self, name, help, var = None, default = False):
        """ Similar to autoconf's AC_ARG_ENABLE. Adds a variable which may
        be enabled/disabled on the configure command line with --enable-name
        or --disable-name. default may be a callable, in which case it will be
        called as default(context, var) if the argument isn't given on the
        command line, and var will be set to its return value. If var is not
        given, it's generated in the form ENABLE_NAME. Its value in the env will
        be 1 or 0. """
        if default == True:
            prefix = "disable"
        else:
            prefix = "enable"
        arg = "--%s-%s" % (prefix, name)
        if not var:
            var = 'ENABLE_' + s_to_var(name)
        self.__arg(arg, help, var, 'DISABLE' + var[6:], default)


    def arg_disable(self, name, help, var = None, default = True):
        """ Can be used as a shortcut for arg_enable with default = True or
        to force help string to be shown as --disable-name when default is
        callable. """
        if not var:
            var = 'ENABLE_' + s_to_var(name)
        self.__arg("--disable-%s" % name, help, var,
                'DISABLE' + var[6:], default)
    
    
    def arg_with(self, name, help, var = None, default = False):
        """ Like arg_enable but uses --with-name=... and --without-name=... """
        if not var:
            var = 'WITH_' + s_to_var(name)
        self.__arg("--with-%s=VALUE" % name, help, var,
                'WITHOUT' + var[4:], default)
    
    
    def save_if_different(self, filename, content):
        " Like global version but runs self.subst() on filename and content. "
        save_if_different(self.subst(filename), self.subst(content))
    
    
    def output_config_h(self):
        sentinel = "%s__CONFIG_H" % self.package_name.upper()
        keys = self.definitions.keys()
        keys.sort()
        s = "/* Auto-generated by maitch.py for %s */\n" \
                "#ifndef %s\n#define %s\n\n" % \
                (self.package_name, sentinel, sentinel)
        for k in keys:
            v = self.definitions[k]
            if v == None:
                s += "#undef %s\n\n" % k
                continue
            elif v == True:
                v = 1
            elif v == False:
                v = 0
            else:
                if isinstance(v, basestring):
                    if (v[0] != "'" or v[-1] != "'"):
                        v = '"' + v + '"'
                    v = self.subst(v)
            s += "#define %s %s\n\n" % (k, v)
        s += "#endif /* %s */\n" % sentinel
        self.save_if_different(opj(self.build_dir, "config.h"), s)
        
    
    @staticmethod
    def var_is_special(k):
        return k == "MSCRIPT_DIR" or k == "MSCRIPT_REAL_DIR"
        
    
    def check_build_dir(self):
        clash = None
        if self.top_dir == self.build_dir or self.top_dir == ".":
            clash = 'TOP_DIR'
        elif self.src_dir == self.build_dir or self.src_dir == ".":
            clash = 'SRC_DIR'
        if clash:
            sys.stderr.write("WARNING: BUILD_DIR == %s, unable to clean\n" %
                    clash)
            return False
        return True
    
    
    def tmpname(self):
        """ Returns the name of a temporary file in ${BUILD_DIR}/.maitch
        which is unique for this context. """
        self.lock.acquire()
        self.tmpfile_index += 1
        i = self.tmpfile_index
        self.lock.release()
        return opj(self.build_dir, ".maitch", "tmp%03d" % i)
        
    
    def get_build_dir(self, kwargs = None):
        """ Sets env's BUILD_DIR and SRC_DIR, looking in kwargs or self.env,
        setting defaults if necessary. """
        if not kwargs:
            kwargs = self.env
        self.env['MSCRIPT_DIR'] = os.path.abspath(os.path.dirname(sys.argv[0]))
        self.env['MSCRIPT_REAL_DIR'] = \
                os.path.abspath(os.path.dirname(os.path.realpath(sys.argv[0])))
        bd = kwargs.get('BUILD_DIR')
        if not bd:
            bd = opj("${MSCRIPT_REAL_DIR}", "build")
        self.env['BUILD_DIR'] = bd
        td = kwargs.get('TOP_DIR')
        if not td:
            td = os.pardir
        self.env['TOP_DIR'] = td
        sd = kwargs.get('SRC_DIR')
        if not sd:
            sd = "${TOP_DIR}"
        self.env['SRC_DIR'] = sd
    

    def env_file_name(self):
        """ Returns the filename of where env is saved, ensuring its 
        directory exists. """
        n = self.make_out_path(".maitch", "env")
        self.ensure_out_dir_for_file(n)
        return n
        
    
    def get_lock_file_name(self):
        return opj(self.subst("${BUILD_DIR}"), ".maitch", "lock")
        
    
    def add_rule(self, rule):
        " Adds a rule. "
        if isinstance(rule, SuffixRule):
            self.add_implicit_rule(rule)
        else:
            self.add_explicit_rule(rule)
        rule.ctx = self
    
    
    def add_explicit_rule(self, rule):
        " Adds an explicit rule. "
        for t in rule.targets:
            self.explicit_rules[t] = rule
        

    def add_implicit_rule(self, rule):
        " Adds an implicit rule. "
        # There may be more than one implicit rule with the same target
        # suffix.
        for t in rule.targets:
            rules = self.implicit_rules.get(t)
            if not rules:
                rules = []
                self.implicit_rules[t] = rules
            rules.append(rule)
    
    
    def glob(self, pattern, dir, subdir = None):
        """ Returns a list of matching filenames relative to dir. subdir, if
        given, is preserved at the start of each filename. """
        if subdir:
            dir = opj(dir, subdir)
        matches = fnmatch.filter(os.listdir(dir), pattern)
        if subdir:
            for i in range(len(matches)):
                matches[i] = opj(subdir, matches[i])
        return matches
    
    
    def glob_src(self, pattern, subdir = None):
        " Performs glob on src_dir. "
        return self.glob(pattern, self.src_dir, subdir)
    
    
    def glob_all(self, pattern, subdir = None):
        """ Performs glob on both build_dir/src_dir. No pathname component
        is added other than subdir. """
        matches = self.glob(pattern, self.build_dir, subdir)
        for n in self.glob_src(pattern, subdir):
            if not n in matches:
                matches.append(n)
        return matches
        
    
    def run(self):
        " Run whichever stage of the build was specified on CLI. "
        if self.mode == 'configure':
            fp = open(self.env_file_name(), 'w')
            for k, v in self.env.items():
                if not self.var_is_special(k):
                    fp.write("%s=%s\n" % (k, v))
            fp.close()
            if self.definitions:
                self.output_config_h()
        elif self.mode == 'build':
            if len(self.cli_targets):
                tgts = self.cli_targets
            else:
                tgts = self.explicit_rules.keys()
            BuildGroup(self, tgts)
        elif self.mode == 'clean':
            self.clean(False)
        elif self.mode == 'clean_fatal':
            self.clean(True)
    
    
    def clean(self, fatal):
        if not self.check_build_dir():
            return
        recursively_remove(self.build_dir, fatal,
                [opj(self.build_dir, ".maitch")])
        recursively_remove(opj(self.build_dir, ".maitch", "deps"), fatal, [])
                
    
    def ensure_out_dir(self, *args):
        """ Esnures the named directory exists. args is a single string or list
        of strings. Single string may be absolute in which case it isn't
        altered. Otherwise a path is made absolute using BUILD_DIR. """
        if isinstance(args, basestring) and os.path.isabs(args):
            path = args
        else:
            path = self.make_out_path(*args)
        self.lock.acquire()
        if not os.path.isdir(path):
            os.makedirs(path)
        self.lock.release()


    def ensure_out_dir_for_file(self, path):
        """ As above but uses a single string only and acts on its
        parent directory. """
        self.ensure_out_dir(os.path.dirname(path))
    
    
    def make_out_path(self, *args):
        """ Creates an absolute filename from BUILD_DIR and args (single
        string or list). """
        if isinstance(args, basestring):
            args = (args)
        args = (self.env['BUILD_DIR'],) + args
        return self.subst(opj(*args))
    
    
    def find_prog(self, prog, expand = False):
        " Finds a binary in context's PATH. prog not expanded by default. "
        sys.stdout.write("Searching for program %s... " % prog)
        if expand:
            prog = self.subst(prog)
        try:
            p = find_prog(prog, self.env)
            sys.stdout.write("%s\n" % p)
        except:
            sys.stdout.write("not found\n")
            raise
        return p
    
    
    def find_prog_env(self, prog, var = None, expand = False):
        """ Finds a program and sets an env var to its full path. If the var
        is not specified a capitalised (etc) transform of the program name
        is used. """
        if not var:
            var = s_to_var(prog)
        self.env[var] = self.find_prog(prog, expand)
    
    
    def prog_output(self, prog, use_shell = False):
        """ Runs a program and returns its [stdout, stderr]. prog should be a
        list or single string (former will not be split at spaces). Each
        component will be expanded. cwd is set to BUILD_DIR. """
        if isinstance(prog, basestring):
            prog = prog.split()
        for i in range(len(prog)):
            prog[i] = self.subst(prog[i])
        if not os.path.isabs(prog[0]):
            prog[0] = self.find_prog(prog[0], False)
        proc = subprocess.Popen(prog,
                stdout = subprocess.PIPE, stderr = subprocess.PIPE,
                shell = use_shell, cwd = self.build_dir)
        result = proc.communicate()
        if proc.returncode:
            raise MaitchChildError("%s failed: %d: %s" % \
                    (' '.join(prog), proc.returncode, result[1].strip()))
        return result
    
    
    def prog_to_var(self, prog, var, use_shell = False):
        " As prog_output(), storing stripped result into self.env[var]. "
        self.env[var] = self.prog_output(prog, use_shell)[0].strip()
    
    
    def pkg_config(self, pkgs, prefix = None, version = None,
            pkg_config = "${PKG_CONFIG}"):
        """ Runs pkg-config (or optionally a similar tool) and sets
        ${prefix}_CFLAGS to the output of --flags and ${prefix}_LIBS
        to the output of --libs. If prefix is None, derive it from pkg
        eg "gobject-2.0 sqlite3" becomes GOBJECT_2_0_SQLITE3.
        If version is given, also check that package's version is at least
        as new (only works on one package at a time). """
        sys.stdout.write("Checking pkg-config %s..." % pkgs)
        try:
            if version:
                try:
                    pvs = self.prog_output(
                            [pkg_config, '--modversion', pkgs])[0].strip()
                except:
                    sys.stdout.write("not found\n")
                pkg_v = pvs.split('.')
                v = version.split('.')
                new_enough = True
                for n in range(len(v)):
                    if v[n] > pkg_v[n]:
                        new_enough = True
                        break
                    elif v[n] < pkg_v[n]:
                        new_enough = False
                        break
                    if not new_enough:
                        sys.stdout.write("too old\n")
                        raise MaitchPkgError("%s has version %s, "
                                "%s needs at least %s" %
                                (pkgs, pvs, self.package_name, version))
            if not prefix:
                prefix = make_var_name(pkg, True)
            pkgs = pkgs.split()
            self.prog_to_var([pkg_config, '--cflags'] + pkgs,
                    prefix + '_CFLAGS')
            self.prog_to_var([pkg_config, '--libs'] + pkgs,
                    prefix + '_LIBS')
        except:
            sys.stdout.write("error\n")
        else:
            sys.stdout.write("ok\n")
    
    
    def subst(self, s, novar = NOVAR_FATAL, recurse = True):
        " Runs global subst() using self.env. "
        return subst(self.env, s, novar, recurse)
    
    
    def deps_from_cpp(self, sources, cflags = None):
        """ Runs "${CDEP} sources" and returns its dependencies
        (one file per line). filename should be absolute. If cflags is
        None, self.env['CFLAGS'] is used. """
        if not cflags:
            cflags = self.env.get('CFLAGS', "")
        sources = process_nodes(sources)
        sources = [self.find_source(s) for s in sources]
        if not sources:
            sources = []
        prog = (self.subst("${CDEP} %s" % cflags)).split() + sources
        deps = self.prog_output(prog)[0]
        deps = deps.split(':', 1)[1].replace('\\', '').split()
        return deps
    
    
    def find_sys_header(self, header, cflags = None):
        """ Uses deps_from_cpp() to find the full path of a header in the
        include path. Returns None if not found. """
        sys.stdout.write("Looking for header '%s'... " % header)
        tmp = self.tmpname() + ".c"
        fp = open(tmp, 'w')
        fp.write("#include <%s>\n" % header)
        fp.close()
        deps = self.deps_from_cpp(tmp, cflags)
        os.unlink(tmp)
        for d in deps:
            if d.endswith(os.sep + header):
                sys.stdout.write("%s\n" % d)
                return d
        sys.stdout.write("not found\n")
        return None
    
    
    def check_compile(self, code, msg = None, cflags = None, libs = None):
        """ Checks whether the program code can be compiled as C, returns
        True or False. If msg is given prints "Checking msg... ". """
        if msg:
            sys.stdout.write("Checking %s... " % msg)
        if not cflags:
            cflags = self.env.get('CFLAGS', "")
        if not libs:
            libs = self.env.get('LIBS', "")
        tmp = self.tmpname()
        fp = open(tmp + ".c", 'w')
        fp.write(code)
        fp.close()
        prog = self.subst("${CC} %s %s -o %s %s" %
                (cflags, libs, tmp, tmp + ".c")).split()
        try:
            self.prog_output(prog)
        except MaitchChildError:
            result = 'no'
        else:
            result = 'yes'
            try:
                os.unlink(tmp)
            except:
                pass
        os.unlink(tmp + ".c")
        if msg:
            sys.stdout.write("%s\n" % result)
        return result == 'yes'


    def check_header(self, header, cflags = None, libs = None):
        """ Uses check_compile to test whether header is available, calling
        self.define('HAVE_HEADER_H', v) where v is 1 or None and HEADER_H is
        derived from header by make_var_name(). Also sets env var with the
        same name to True or False. """
        if self.check_compile("""#include "%s"
int main() { return 0; }
""" % header,
                "for header '%s'" % header, cflags, libs):
            present = 1
        else:
            present = None
        nm = make_var_name('HAVE_' + header, True)
        self.define(nm, present)
        self.setenv(nm, present == 1)
        return present == 1


    def check_func(self, func, cflags = None, libs = None, includes = None):
        """ Like check_header but for a function. includes is an optional
        list of header names. """
        code = ""
        if includes:
            for i in includes:
                code += '#include "%s"\n' % i
        code += """
int main() { %s(); return 0; }
""" % func
        if self.check_compile(code, "for function %s()" % func, cflags, libs):
            present = 1
        else:
            present = None
        nm = make_var_name('HAVE_' + func, True)
        self.define(nm, present)
        self.setenv(nm, present == 1)
        return present == 1
    
    
    def find_source(self, name, where = SRC):
        """ Finds a source file relative to BUILD_DIR (higher priority) or
        SRC_DIR, returning full path or raising exception if not found. Returns
        absolute paths unchanged. """
        name = self.subst(name)
        if os.path.exists(name):
            return name
        if os.path.isabs(name):
            if os.path.exists(name):
                return name
            else:
                self.not_found(name)
        p = opj(self.build_dir, name)
        if os.path.exists(p):
            return p
        if where & SRC:
            p = opj(self.src_dir, name)
            if os.path.exists(p):
                return p
        if where & TOP:
            p = opj(self.top_dir, name)
            if os.path.exists(p):
                return p
        self.not_found(name)
    
    
    @staticmethod
    def not_found(name):
        raise MaitchNotFoundError("Resource '%s' cannot be found" % name)
        
    
    def setenv(self, k, v):
        " Sets an env var. "
        self.env[k] = v
    
    
    def getenv(self, k, default = None):
        " Returns an env var. "
        return self.env.get(k)
    
    
    def get_stamp(self, name, where = SRC | TOP):
        """ Returns the mtime for named file. Uses find_source() and subst and
        therefore may raise MaitchNotFoundError or KeyError. """
        n = self.find_source(self.subst(name), where)
        return os.stat(name).st_mtime


    def get_extreme_stamp(self, nodes, comparator, where = SRC | TOP):
        """ Like global version, but runs subst() and find_source() on each
        item. Items that don't exist are skipped because some suffix rules
        don't necessarily use all sources or targets (eg gob2 doesn't always
        output a private header). """
        pnodes = []
        for n in nodes:
            try:
                pnodes.append(self.find_source(self.subst(n), where))
            except MaitchNotFoundError:
                pass
        return get_extreme_stamp(pnodes, comparator)
    
    
    def get_oldest(self, nodes, where = SRC | TOP):
        """ Like global version, but runs subst() and find_source() on each
        item, so may raise MaitchNotFoundError or KeyError. """
        return self.get_extreme_stamp(nodes, lambda a, b: a < b, where)
    
    
    def get_newest(self, nodes, where = SRC | TOP):
        """ Like global version, but runs subst() and find_source() on each
        item, so may raise MaitchNotFoundError or KeyError. """
        return self.get_extreme_stamp(nodes, lambda a, b: a > b, where)
    
    
    def subst_file(self, source, target):
        """ As global version, using self.env. """
        subst_file(self.env, source, target)
        
    
    def install(self, directory, sources = None,
            mode = None, other_options = None):
        """ Uses the install program to install files. Default mode is install's
        default mode, which in turn defaults to rwxr-xr-x (but you should
        specify a number). sources may be string with multiple files separated
        by spaces, or a list, or None to create directory. other_options, which
        may also be a string or a list, are additional options for install. """
        cmd = ["${INSTALL}"]
        if self.dest_dir:
            directory = opj(self.dest_dir, directory)
        if isinstance(other_options, basestring):
            cmd += other_options.split()
        elif other_options:
            cmd += list(other_options)
        if mode:
            cmd += ["-m", mode]
        if not sources:
            cmd.append("-d")
        elif isinstance(sources, basestring):
            sources = sources.split()
        if sources and len(sources) > 1:
            cmd += ["-t", directory] + sources
        else:
            cmd += sources + directory
        for n in range(len(cmd)):
            cmd[n] = self.subst(cmnd[n])
        sys.stdout.write("%s\n", ' '.join(cmd))
        if subprocess.call(prog, cwd = self.build_dir) != 0:
            raise MaithChildError("install failed")
    


class Rule(object):
    """
    Standard rule.
    """
    def __init__(self, **kwargs):
        """
        Possible arguments (all optional except targets):
        rule: A function or a string containing a command to be called.
                A string should contain "${TGT}" and/or "${SRC}" for
                targets and sources. This will not work if they contain spaces.
                A function is called as func(ctx, env, [targets], [sources])
                where ctx is the Context and targets and soures are expanded.
                Note env is not the same as ctx.env, it will have ${TGT} and
                ${SRC} added.
                An additional variable, ${TGT_DIR}, is set to the directory
                name of the first target. Note ${TGT_DIR} is absolute but
                ${TGT} is not. 
        sources: See process_nodes() func for what's valid.
        targets: As above.
        deps: Dependencies other than sources; deps are satisfied before
                sources.
        wdeps: "Weak dependencies":- the targets will not be built until after
                all wdeps are built, but the targets don't necessarily depend
                on the wdeps. This makes sure dynamic dependency tracking
                works properly when some headers are generated by other steps
                eg by making C compiler rules wdep on all files generated by
                gob2.
        dep_func: A function to calculate implicit deps; takes a ctx and rule
                as arguments and returns a list of absolute filenames. This is
                used only to work out whether the target is out of date and
                needs to be rebuilt; it is not used when working out the
                dependncy chain.
        use_shell: Whether to use shell if rule is a string (default False).
        env: A dict containing env vars to override those from Context. Each
                var may refer to its own name in its value to derive values
                from the Context.
        quiet: If True don't print the command about to be executed.
        where: Where to look for sources: SRC, TOP or both (default SRC).
        """
        if not 'where' in kwargs:
            kwargs['where'] = SRC
        self.rule = kwargs.get('rule')
        self.targets = kwargs['targets']
        if isinstance(self.targets, basestring):
            self.targets = process_nodes(self.targets)
        self.sources = kwargs.get('sources')
        if self.sources and isinstance(self.sources, basestring):
            self.sources = process_nodes(self.sources)
        self.deps = kwargs.get('deps')
        if self.deps and isinstance(self.deps, basestring):
            self.deps = process_nodes(self.deps)
        self.wdeps = kwargs.get('wdeps')
        if self.wdeps and isinstance(self.wdeps, basestring):
            self.wdeps = process_nodes(self.wdeps)
        self.dep_func = kwargs.get('dep_func')
        self.use_shell = kwargs.get('use_shell')
        self.env = kwargs.get('env')
        self.quiet = kwargs.get('quiet')
        self.blocking = []
        self.blocked_by = []
        self.completed = False
        self.cached_env = None
        self.cached_targets = None
        self.cached_sources = None
        self.cached_deps = []
        self.where = kwargs['where']
    
    
    @staticmethod
    def init_var(kwargs, var):
        """ Call this from subclass' constructor if it uses var (specified in
        lower case). If kwargs includes a key 'var' (lower case) env[VAR_] is
        set to its value. Note the trailing _. This is because a variable
        overridden here would commonly want to refer to itself (as ${VAR}) but
        can't due to recursion. So accompanying variables should refer to one
        set here with the trailing _. If kw var is not present, env[VAR_] is set
        to ${VAR}. See CRule's use of libs and cflags for an example. """
        val = kwargs.get(var)
        if not val:
            val = "${%s}" % var.upper()
        env = kwargs.get('env')
        if not env:
            env = {}
            kwargs['env'] = env
        env[var.upper() + '_'] = val


    def init_cflags(self, kwargs):
        " For rules which use cflags. "
        self.init_var(kwargs, 'cflags')


    def init_libs(self, kwargs):
        " For rules which use libs. "
        self.init_var(kwargs, 'libs')
    
    
    def process_env(self):
        " Merges env with ctx's, with caching. Does not set TGT or SRC. "
        if self.cached_env == None:
            env = dict(self.ctx.env)
            if self.env:
                for k, v in self.env.items():
                    env[k] = v
            self.cached_env = env
        return self.cached_env
        
        
    def process_env_tgt_src(self):
        """ As process_env, but also returns expanded targets and sources
        [env, targets sources] and adds them to env. """
        env = self.process_env()
        if self.cached_targets == None:
            # Expand targets and sources, finding the sources, and prefixing
            # targets with build_dir.
            targets = [subst(env, t) for t in self.targets]
            if self.sources:
                sources = [self.ctx.find_source(subst(env, s), self.where) \
                        for s in self.sources]
            else:
                sources = None
            # Add them to env
            if sources:
                env['SRC'] = ' '.join(sources)
            else:
                env['SRC'] = ''
            if targets:
                env['TGT'] = ' '.join(targets)
                td = os.path.dirname(targets[0])
                if not os.path.isabs(td):
                    td = opj(self.ctx.build_dir, td)
                env['TGT_DIR'] = td
            else:
                env['TGT'] = ''
                env['TGT_DIR'] = ''
            self.cached_targets = targets
            self.cached_sources = sources
        return [self.cached_env, self.cached_targets, self.cached_sources]
    
    
    def run(self):
        " Run a job. "
        if self.is_uptodate():
            return
        env, targets, sources = self.process_env_tgt_src()        
        if callable(self.rule):
            if not self.quiet:
                sys.stdout.write("Internal function: %s(%s, %s)\n" %
                        (self.rule.__name__, str(targets), str(sources)))
            self.rule(self.ctx, env, targets, sources)
        else:
            rule = subst(env, self.rule)
            if not self.quiet:
                sys.stdout.write(rule + '\n')
            if self.use_shell:
                prog = rule
            else:
                prog = rule.split()
            if subprocess.call(prog,
                    shell = self.use_shell, cwd = self.ctx.build_dir):
                raise MaitchChildError("Rule '%s' failed" % rule)
    
    
    def __repr__(self):
        return "JT:%s" % self.targets
        #return "T:%s S:%s" % (self.targets, self.sources)
    
    
    def list_static_deps(self):
        """ Works out a rule's static dependencies ie deps + sources, but not
        dep_func. Caches them and returns list. """
        if self.deps:
            deps = self.deps
        else:
            deps = []
        if self.sources:
            deps += self.sources
        self.cached_deps = deps
        return deps
        
    
    def is_uptodate(self):
        """ Returns False if any target is older than any source, dep, or result
        of dep_func (implicit/dynamic deps). """
        # If there are no dependencies target(s) must be rebuilt every time
        if not self.cached_deps:
            return False
        # First find whether uptodate wrt static deps.
        # If result is True extra variable newest_dep is available
        uptodate = True
        try:
            oldest_target = self.ctx.get_oldest(self.targets, self.where)
        except MaitchNotFoundError, KeyError:
            oldest_target = None
        if not oldest_target:
            uptodate = False
        if uptodate:
            # All sources and deps should be available at this point so
            # OK to let exception propagate
            newest_dep = self.ctx.get_newest(self.cached_deps, self.where)
            if newest_dep and newest_dep > oldest_target:
                uptodate = False
        else:
            newest_dep = None
        
        if self.dep_func:
            # Work out whether cached dynamic deps need updating
            dyn_deps = None
            deps_name = self.ctx.make_out_path(self.ctx.subst(
                    opj(".maitch", "deps", self.targets[0])))
            if os.path.exists(deps_name):
                deps_stamp = os.stat(deps_name).st_mtime
                if not newest_dep:
                    newest_dep = self.ctx.get_newest(self.cached_deps,
                            self.where)
                if newest_dep and newest_dep > deps_stamp:
                    # sources/static deps are newer than dynamics
                    rebuild_deps = True
                else:
                    # see whether cached deps are older than any file they list
                    dyn_deps = load_deps(deps_name)
                    newest_impl_dep = get_newest(dyn_deps)
                    if newest_impl_dep > newest_dep:
                        newest_dep = newest_impl_dep
                    rebuild_deps = newest_impl_dep > deps_stamp
            else:
                rebuild_deps = True
            if rebuild_deps:
                dyn_deps = self.dep_func(self.ctx, self)
                self.ctx.ensure_out_dir_for_file(deps_name)
                fp = open(deps_name, 'w')
                for d in dyn_deps:
                    fp.write(d + '\n')
                fp.close()
            if not dyn_deps:
                dyn_deps = load_deps(deps_name)
            # Now we have up-to-date implicit dyn_deps, check whether targets
            # are older than them
            if get_newest(dyn_deps) > oldest_target:
                uptodate = False
        
        return uptodate
                
        

class SuffixRule(Rule):
    """ A rule which will be applied automatically if a source of an
    explicit rule has a suffix matching this rule's target and this rule's
    source exists.
    To simplify maitch's algorithm for working out whether a SuffixRule
    can build a particular explicit target, you must ensure that it processes
    any rules to build its explicit sources first. Do this with deps.
    
    A SuffixRule creates a number of plain Rules, one for each unique target,
    at build time. """
    def __init__(self, **kwargs):
        """ args are similar to those for a standard Rule but sources and
        targets are specified as suffixes only. deps are complete filenames as
        with explicit Rules.
        targets and rule are compulsory.
        
        There is also an optional 'prefix' argument. If the targets have
        directory components the prefix is applied to the leafname.
        
        {prefix}{node}{tsuffix1} ... {prefix}{node}{tsuffixn}
        are built with sources:
        {node}{ssuffix1} ... {node}{ssuffixm}
        
        where any one of the targets on the first line is requested from this
        rule and sources and deps exist.
        
        Suffixes usually start with '.' and prefixes end with '-'. """
        # Preserve kwargs for easy construction of an explicit rule
        if not 'where' in kwargs:
            kwargs['where'] = SRC
        self.kwargs = kwargs
        Rule.__init__(self, **kwargs)
        self.prefix = kwargs.get('prefix', '')
    
    
    def get_sources_from_target(self, target):
        """ Returns names of sources which would build target with this rule.
        All are relative to BUILD_DIR/SRC_DIR but may contain additional
        directory components. Returns None if suffix deosn't match. This means
        rule can't build target and should be distinguished from an empty list
        which means it can build target without sources (rare). """
        if len(self.prefix):
            p, t = os.path.split(target)
            if t.startswith(self.prefix):
                t = t[len(self.prefix):]
            else:
                return None
            if p:
                pat = os.path.join(p, t)
            else:
                pat = t
        else:
            pat = target
        for tgt in self.targets:
            if target.endswith(tgt):
                pat0 = pat[:-len(tgt)]
                sources = []
                for src in self.sources:
                    sources.append(pat0 + src)
                return sources
        return None
        
    
    def get_rule_for_target(self, target, known_sources = None):
        """ Returns an explicit rule to build target or None if suffix doesn't
        match. Prerequisite sources must already exist or be listed in
        known_sources, but deps need not exist yet. """
        sources = self.get_sources_from_target(target)
        if sources == None:
            return None
        if len(sources):
            for src in sources:
                if not (known_sources and src in known_sources):
                    try:
                        self.ctx.find_source(src, self.where)
                    except MaitchNotFoundError:
                        return None
        kwargs = dict(self.kwargs)
        tgts = []
        for t in self.targets:
            if target.endswith(t):
                pat = target[:-len(t)]
                break
        kwargs['targets'] = [pat + t for t in self.targets]
        kwargs['sources'] = sources
        rule = Rule(**kwargs)
        rule.ctx = self.ctx
        return rule



class TouchRule(Rule):
    """ Use this as a dummy rule rather than use a Rule with no rule parameter.
    The target(s) will be "touched" to update their timestamp. """
    def __init__(self, **kwargs):
        kwargs['rule'] = self.touch
        Rule.__init__(self, **kwargs)
    
    
    def touch(self, ctx, env, tgts, srcs):
        for t in tgts:
            fp = open(t, 'w')
            fp.close()
                


class CRule(SuffixRule):
    " Standard rule for compiling C to an object file. "
    def __init__(self, **kwargs):
        self.init_cflags(kwargs)
        set_default(kwargs, 'rule', "${CC} ${CFLAGS_} -c -o ${TGT} ${SRC}")
        set_default(kwargs, 'targets', ".o")
        set_default(kwargs, 'sources', ".c")
        set_default(kwargs, 'dep_func', self.get_implicit_deps)
        SuffixRule.__init__(self, **kwargs)
    
    
    def get_implicit_deps(self, ctx, rule):
        # rule will be a static rule generated from self with a single source
        env = self.process_env()
        try:
            return ctx.deps_from_cpp(rule.sources, env['CFLAGS_'])
        except MaitchNotFoundError:
            return None



class LibtoolCRule(CRule):
    " libtool version of CRule. "
    def __init__(self, **kwargs):
        """ Additional kwargs:
            libtool_flags. sh_or_st should be
            libtool_mode_arg: -shared, -static etc.
        """
        self.init_var(kwargs, 'libtool_flags')
        self.init_var(kwargs, 'libtool_mode_arg')
        set_default(kwargs, 'targets', ".lo")
        set_default(kwargs, 'rule',
                "${LIBTOOL} --mode=compile --tag=CC ${LIBTOOL_FLAGS_} "
                "gcc ${LIBTOOL_MODE_ARG_} ${CFLAGS_} -c -o ${TGT} ${SRC}")
        CRule.__init__(self, **kwargs)



class ShlibCRule(LibtoolCRule):
    " Compiles C into an object file suitable for a shared library. "
    def __init__(self, **kwargs):
        kwargs['libtool_mode_arg'] = "-shared %s" % \
                kwargs.get('libtool_mode_arg', '')
        LibtoolCRule.__init__(self, **kwargs)



class StaticLibCRule(LibtoolCRule):
    " Compiles C into an object file suitable for a static library. "
    def __init__(self, **kwargs):
        kwargs['libtool_mode_arg'] = "-static %s" % \
                kwargs.get('libtool_mode_arg', '')
        LibtoolCRule.__init__(self, **kwargs)



class ProgramRule(Rule):
    " Standard rule for linking several object files and libs into a program. "
    def __init__(self, **kwargs):
        self.init_cflags(kwargs)
        self.init_libs(kwargs)
        set_default(kwargs, 'rule',
                "${CC} ${CFLAGS_} ${LIBS_} -o ${TGT} ${SRC}")
        Rule.__init__(self, **kwargs)



class LibtoolProgramRule(ProgramRule):
    " Use libtool during linking. "
    def __init__(self, **kwargs):
        self.init_var(kwargs, 'libtool_flags')
        self.init_var(kwargs, 'libtool_mode_arg')
        set_default(kwargs, 'rule',
                "${LIBTOOL} --mode=link ${LIBTOOL_FLAGS_} "
                "gcc ${LIBTOOL_MODE_ARG_} -rpath ${LIBDIR} "
                "${CFLAGS_} ${LIBS_} -o ${TGT} ${SRC}")
        ProgramRule.__init__(self, **kwargs)



class ShlibRule(LibtoolProgramRule):
    """ Standard rule to create a shared library with libtool. See the
    libtool manual for an explanation of its interface version system.
    -release is not supported (yet). """
    def __init__(self, **kwargs):
        version = kwargs.get('libtool_version')
        if version:
            version = "-version-info %d:%d:%d" % tuple(version)
        else:
            version = ""
        kwargs['libtool_mode_arg'] = "-shared %s %s" % \
                (kwargs.get('libtool_mode_arg', ''), version)
        LibtoolProgramRule.__init__(self, **kwargs)



class StaticLibRule(LibtoolProgramRule):
    """ Standard rule to create a static library with libtool. See the
    libtool manual for an explanation of its interface version system.
    -release is not supported (yet). """
    def __init__(self, **kwargs):
        version = kwargs.get('libtool_version')
        if version:
            version = "-version-info %d:%d:%d" % tuple(version)
        else:
            version = ""
        kwargs['libtool_mode_arg'] = "-static %s %s" % \
                (kwargs.get('libtool_mode_arg', ''), version)
        LibtoolProgramRule.__init__(self, **kwargs)



def print_formatted(body, columns = 80, heading = None, h_columns = 20):
    """ Prints body, wrapped at the specified number of columns. If heading is
    given, h_columns are reserved on the left for it. """
    if heading:
        b_cols = columns - h_columns
    else:
        b_cols = columns
    if len(heading) >= h_columns:
        print_wrapped(heading, columns)
        pre = h_columns
    else:
        sys.stdout.write(heading + ' ' * (h_columns - len(heading)))
        pre = 0
    print_wrapped(body, columns - h_columns, h_columns, pre)


def print_wrapped(s, columns = 80, indent = 0, first_indent = None,
        file = sys.stdout):
    """ Prints s wrapped to fit in columns, optionally indented,
    with an optional alternative indent for the first line. """
    if first_indent == None:
        first_indent = indent
    i = ' ' * first_indent
    while len(s) > columns:
        l = s[:columns]
        s = s[columns:]
        split = l.rsplit(None, 1)
        if split:
            l = split[0]
            s = split[1] + ' ' + s
        file.write("%s%s\n" % (i, l))
        i = ' ' * indent
    if s:
        file.write("%s%s\n" % (i, s))



def subst_file(env, source, target):
    """ Run during configure phase to create a copy of source as target
    with ${} constructs substituted. """
    fp = open(subst(env, source), 'r')
    s = fp.read()
    fp.close()
    fp = open(subst(env, target), 'w')
    fp.write(subst(env, s))
    fp.close()
    
    
def save_if_different(filename, content):
    """ Saves content to filename, only if file doesn't already contain
    identical content. """
    if os.path.exists(filename):
        fp = open(filename, 'r')
        old = fp.read()
        fp.close()
    else:
        old = None
    if content != old:
        fp = open(filename, 'w')
        fp.write(content)
        fp.close()
            


def set_default(d, k, v):
    " Sets d[k] = v only if d doesn't already contain k. "
    if not d.get(k):
        d[k] = v



def opj(*args):
    " Convenience shorthand for os.path.join "
    return os.path.join(*args)
    
    

_subst_re = re.compile(r"(\$\{-?([a-zA-Z0-9_]+)\})")

def subst(env, s, novar = NOVAR_FATAL, recurse = True):
    """
    Processes string s, substituting ${var} with values from dict env
    with var as the key. To prevent substitution put a '-' after the
    opening brace. It will be removed. Most variables are expanded
    recursively immediately before use.
    If fatal is False, bad matches are left unexpanded.
    """
    def ms(match):
        s = match.group(2)
        if s[0] == '-':
            return "${%s}" % s[1:]
        elif novar == NOVAR_FATAL:
            return env[s]
        else:
            if novar == NOVAR_BLANK:
                dr = ''
            else:
                dr = match.group(0)
            return env.get(s, dr)
    result, n = _subst_re.subn(ms, s)
    if recurse and n:
        return subst(env, result, novar)
    return result



def process_nodes(nodes):
    """
    nodes may be a string with a single node filename, several names
    separated by spaces, or a list of strings - one per node, each may
    contain spaces. Returns a list of nodes.
    """
    if hasattr(nodes, "append"):
        return nodes
    elif ' ' in nodes:
        return nodes.split()
    else:
        return [nodes]


def recursively_remove(path, fatal, excep):
    """ Deletes path and all its contents, leaving behind exceptions.
    Returns False if path could not be deleted because it contains an
    exception. If something can not be deleted only propagate exception
    if fatal is True. """
    if fatal:
        contents = os.listdir(path)
    else:
        try:
            contents = os.listdir(path)
        except:
            return True
    removable = True
    if contents:
        for f in contents:
            f = opj(path, f)
            if f in excep:
                removable = False
            elif os.path.isdir(f):
                if not recursively_remove(f, fatal, excep):
                    removable = False
            elif fatal:
                os.unlink(f)
            else:
                try:
                    os.unlink(f)
                except:
                    removable = False
    if removable:
        if fatal:
            os.rmdir(path)
        else:
            try:
                os.rmdir(path)
            except:
                removable = False
    return removable
        

def find_prog(name, env = os.environ):
    if os.path.isabs(name):
        return name
    path = env.get('PATH', '/usr/bin:/usr/local/bin')
    for p in path.split(':'):
        n = opj(p, name)
        if os.path.exists(n):
            return n
    raise MaitchNotFoundError("%s program not found in PATH" % name)


_var_repository = []

def add_var(name, default = "", desc = "", as_arg = False):
    """
    Registers a variable name with a default value which will be used if the
    variable isn't specified on the command-line. If as_arg is True it can
    be specified as a -- option ie --foo-bar=baz is equivalent to the usual
    form FOO_BAR=baz. default may be a function, called as function(name)
    to calculate the default value dynamically.
    """
    global _var_repository
    _var_repository.append([name, default, desc, as_arg])


_prog_var_repository = {}

def add_prog(var, prog):
    global _prog_var_repository
    _prog_var_repository[var] = prog


def find_prog_by_var(var):
    p = _prog_var_repository.get(var)
    if not p:
        p = var_to_s(var)
    return find_prog(p)


def s_to_var(s):
    return s.upper().replace('-', '_').replace('.', '_').replace(' ', '_')

def var_to_s(s):
    return s.lower().replace('_', '-')

def arg_to_var(s):
    return s_to_var(s[2:])

def var_to_arg(s):
    return '--' + var_to_s(s)



def make_var_name(template, upper = False):
    """ Makes an eligible variable name from template by replacing special
    characters with '_' and also prepending a '_' if template has a leading
    digit. ALso optionally converts to upper case. """
    if ascii.isdigit(template[0]):
        s = "_"
    else:
        s = ""
    for c in template:
        if c != '_' and not ascii.isalnum(c):
            s += "_"
        else:
            s += c
    if upper:
        s = s.upper()
    return s
    
    

def change_suffix(files, old, new):
    """ Returns a new list of files with old suffix replaced with new
    (include the '.' in the parameters). files may be a space-separated string
    or a list/tuple of strings. Non-matching suffixes are left unchanged. """
    changed = []
    if isinstance(files, basestring):
        files = files.split()
    l = len(old)
    for f in files:
        if f.endswith(old):
            f = f[:-l] + new
        changed.append(f)
    return changed


def add_prefix(files, prefix):
    """ Returns a new list of files with prefix added to leafname.
    files may be a space-separated string or a list/tuple of strings. """
    changed = []
    if isinstance(files, basestring):
        files = files.split()
    changed = []
    for f in files:
        head, tail = os.path.split(f)
        changed.append(opj(head, prefix + tail))
    return changed


def change_suffix_with_prefix(files, old, new, prefix):
    return add_prefix(change_suffix(files, old, new), prefix)



def get_extreme_stamp(nodes, comparator):
    """ Gets stamp for each item in nodes and returns the highest
    according to comparator. Returns None for an empty list or raises
    MaitchNotFoundError if any member is not found. """
    stamp = None
    for n in nodes:
        if not os.path.exists(n):
            raise MaitchNotFoundError("Can't find '%s' to get timestamp" % n)
        s = os.stat(n).st_mtime
        if stamp == None:
            stamp = s
        else:
            r = comparator(s, stamp)
            if r == True or r > 0:
                stamp = s
    return stamp


def get_oldest(nodes):
    """ See get_extreme_stamp(). """
    return get_extreme_stamp(nodes, lambda a, b: a < b)


def get_newest(nodes):
    """ See get_extreme_stamp(). """
    return get_extreme_stamp(nodes, lambda a, b: a > b)


def load_deps(f):
    """ Loads a list of filenames from file with absolute name f. """
    fp = open(f, 'r')
    deps = []
    while 1:
        l = fp.readline()
        if l:
            deps.append(l.strip())
        else:
            break
    fp.close()
    return deps



class BuildGroup(object):
    " Class to organise the build, arranging tasks in order of dependency. "
    def __init__(self, ctx, targets):
        """ ctx is the context containing info for the build. targets is a
        list of targets. """
        self.ctx = ctx
        
        # cond is used to allow builders to wait until a new task is ready
        # and to access the queue in a thread-safe way
        self.cond = threading.Condition(ctx.lock)
        
        # ready_queue isn't really ordered, jobs are just appended as they
        # become unblocked. This is nice and simple.
        # Additionally all jobs are added to ready_queue before checking
        # up-to-dateness; this is checked just before they would run and
        # up-to-date jobs are skipped and handled as if they were just run.
        self.ready_queue = []
        
        # quick way to find whether a target is due to be, or has been, built
        self.queued = {}
        
        # Jobs are added here until unblocked, then moved to ready_queue
        self.pending_jobs = []
        
        # Add jobs
        for tgt in targets:
            self.add_target(tgt)
        
        # Start builders
        self.cancelled = False
        self.threads = []
        n = int(self.ctx.env.get('PARALLEL', 1))
        if n < 1:
            n = 1
        for n in range(n):
            t = Builder(self)
            self.threads.append(t)
            t.start()
        
        # Wait for threads; doesn't matter what order they finish in
        for t in self.threads:
            t.join()
        
    
    def add_target(self, target):
        if not target in self.queued:
            self.add_job(self.ctx.explicit_rules[target])
    
    
    def add_job(self, job):
        if job in self.pending_jobs:
            return
        job.calculating = True
        self.cond.acquire()
        self.pending_jobs.append(job)
        self.cond.release()
        for t in job.targets:
            self.queued[t] = job
        self.satisfy_deps(job)
        job.calculating = False
        if not len(job.blocked_by):
            self.do_while_locked(self.make_job_ready, job)
    
    
    def do_while_locked(self, func, *args, **kwargs):
        """ Lock cond and run func(*args, **kwargs), handling errors in a
        thread-safe way. Returns result of func. """
        self.cond.acquire()
        try:
            result = func(*args, **kwargs)
        except:
            self.cancel_all_jobs()
            self.cond.release()
            raise
        self.cond.release()
        return result
    
    
    def cancel_all_jobs(self):
        self.cond.acquire()
        if not self.cancelled:
            self.cancelled = True
            self.ready_queue = []
            self.pending_jobs = []
            self.cond.notify_all()
        self.cond.release()
        
    
    def make_job_ready(self, job):
        " Moves a job from pending_jobs to ready_queue. "
        self.cond.acquire()
        self.pending_jobs.remove(job)
        self.ready_queue.append(job)
        self.cond.notify()
        self.cond.release()
    
    
    def satisfy_deps(self, job):
        if job.wdeps:
            deps = job.wdeps
        else:
            deps = []
        sdeps = job.list_static_deps()
        if sdeps:
            deps += sdeps
        if not len(deps):
            return
        for dep in deps:
            dep = self.ctx.subst(dep)
            # Has a rule to build this dep already been queued?
            rule = self.queued.get(dep)
            if rule:
                if rule.calculating:
                    raise MaitchRecursionError("%s and %s have circular "
                            "dependencies" % (rule, dep))
                self.mark_blocking(job, rule)
                continue
                
            # Is there an explicit rule for it?
            rule = self.ctx.explicit_rules.get(dep)
            # Is there a suffix rule?
            if not rule:
                # Attempt quick lookup if dep has . suffix
                r0 = None
                if '.' in dep:
                    suffix = '.' + dep.rsplit('.', 1)[1]
                    rl = self.ctx.implicit_rules.get(suffix)
                    if rl:
                        for r in rl:
                            rule = r.get_rule_for_target(dep, self.queued)
                            if rule:
                                break
            if not rule:
                # Might be some other eligible suffix rule
                for rl in self.ctx.implicit_rules.values():
                    for r in rl:
                        rule = r.get_rule_for_target(dep, self.queued)
                        if rule:
                            break
                    if rule:
                        break
            if rule:
                self.mark_blocking(job, rule)
                self.add_job(rule)
                continue
            
            # Does file already exist?
            self.ctx.find_source(dep, job.where)
            
    
    def mark_blocking(self, blocked, blocker):
        self.cond.acquire()
        if not blocked in blocker.blocking:
            blocker.blocking.append(blocked)
        if not blocker in blocked.blocked_by:
            blocked.blocked_by.append(blocker)
        self.cond.release()
    
    
    def job_done(self, job):
        for j in job.blocking:
            self.do_while_locked(self.unblock_job, j, job)
        if not len(self.ready_queue) and not len(self.pending_jobs):
            self.cond.acquire()
            self.cond.notify_all()
            self.cond.release()
    
    
    def unblock_job(self, blocked, blocker):
        """ 'blocked' is no longer blocked by 'blocker'. Call while locked.
        blocker is not altered, but if blocked has no remaining blockers it's
        moved to ready_queue. """
        try:
            blocked.blocked_by.remove(blocker)
        except ValueError:
            raise MaitchError(
                    "%s was supposed to be blocked by %s but wasn't" %
                    (blocked, blocker))
        if not len(blocked.blocked_by):
            self.make_job_ready(blocked)



class Builder(threading.Thread):
    """ A Builder starts a new thread which waits for jobs to be added to
    BuildGroup's ready_queue, pops one at a time and runs it. """
    def __init__(self, build_group):
        self.bg = build_group
        threading.Thread.__init__(self)
    
    
    def run(self):
        try:
            while 1:
                job = None
                self.bg.cond.acquire()
                if not len(self.bg.ready_queue):
                    if len(self.bg.pending_jobs):
                        self.bg.cond.wait()
                # Check both queues again, because they could have both
                # changed while we were waiting
                if not len(self.bg.ready_queue) and \
                        not len(self.bg.pending_jobs):
                    # No jobs left, we're done
                    self.bg.cond.release()
                    break
                if len(self.bg.ready_queue):
                    job = self.bg.ready_queue.pop()
                self.bg.cond.release()
                if job:
                    job.run()
                    self.bg.job_done(job)
        except:
            self.bg.cancel_all_jobs()
            raise
            


# Commonly used variables

add_var('PARALLEL', '0', "Number of build operations to run at once", True)
add_var('PREFIX', '/usr/local', "Base directory for installation", True)
add_var('BINDIR', '${PREFIX}/bin', "Installation directory for binaries", True)
add_var('LIBDIR', '${PREFIX}/lib',
        "Installation directory for shared libraries "
        "(you should use multiarch where possible)", True)
add_var('SYSCONFDIR', '/etc',
        "Installation directory for system config files", True)
add_var('DATADIR', '${PREFIX}/share',
        "Installation directory for data files", True)
add_var('PKGDATADIR', '${PREFIX}/share/${PACKAGE}',
        "Installation directory for this package's data files", True)
add_var('DOCDIR', '${PREFIX}/share/doc/${PACKAGE}',
        "Installation directory for this package's documentation", True)
add_var('HTMLDIR', '${DOCDIR}',
        "Installation directory for this package's HTML documentation", True)
add_var('DESTDIR', '',
        "Prepended to prefix at installation (for packaging)", True)

add_var('CC', '${GCC}', "C compiler")
add_var('CXX', '${GCC}', "C++ compiler")
add_var('GCC', find_prog_by_var, "GNU C compiler")
add_var('CPP', find_prog_by_var, "C preprocessor")
add_var('LIBTOOL', find_prog_by_var, "libtool compiler frontend for libraries")
add_var('CDEP', '${CPP} -M', "C preprocessor with option to print deps")
add_var('CFLAGS', '-O2 -g -Wall -I${SRC_DIR}', "C compiler flags")
add_var('LIBS', '', "C libraries and linker options")
add_var('CXXFLAGS', '${CFLAGS}', "C++ compiler flags")
add_var('LIBTOOL_MODE_ARG', '', "Default libtool mode argument(s)")
add_var('LIBTOOL_FLAGS', '', "Additional libtool flags")
add_var('PKG_CONFIG', find_prog_by_var, "pkg-config")
add_var('INSTALL', find_prog_by_var, "install program")

