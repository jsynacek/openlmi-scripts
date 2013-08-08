# Copyright (c) 2013, Red Hat, Inc. All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#
# 1. Redistributions of source code must retain the above copyright notice,
#    this list of conditions and the following disclaimer.
# 2. Redistributions in binary form must reproduce the above copyright notice,
#    this list of conditions and the following disclaimer in the documentation
#    and/or other materials provided with the distribution.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
# ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT OWNER OR CONTRIBUTORS BE
# LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
# CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
# SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
# INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
# CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
# ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.
#
# The views and conclusions contained in the software and documentation are
# those of the authors and should not be interpreted as representing official
# policies, either expressed or implied, of the FreeBSD Project.
#
# Authors: Michal Minar <miminar@redhat.com>
#
"""
Subpackage containing functionality of lmi meta-command.
"""

import argparse
import logging
import sys

from lmi.scripts import common
from lmi.scripts.common import errors
from lmi.scripts._metacommand import util
from lmi.scripts._metacommand.help import Help
from lmi.scripts._metacommand.manager import CommandManager
from lmi.scripts._metacommand.interactive import Interactive
from lmi.scripts._metacommand.toplevel import TopLevelCommand
from lmi.scripts.common.command import LmiCommandMultiplexer, LmiBaseCommand
from lmi.scripts.common.configuration import Configuration
from lmi.scripts.common.session import Session
from lmi.shell import LMIUtil

LOG = common.get_logger(__name__)

# ignore any message before the logging is configured
logging.getLogger('').addHandler(logging.NullHandler())

def parse_hosts_file(hosts_file):
    """
    Parse file with hostnames to connect to. Return list of parsed hostnames.

    :param hosts_file: (``file``) File object openned for read.
        It containes hostnames. Each hostname occupies single line.
    :rtype: (``list``)
    """
    res = []
    for line in hosts_file.readlines():
        hostname = line.strip()
        res.append(hostname)
    return res

class NullFile(object):
    """
    Mock class implementing toilette for any message passed to it. It mocks
    file object representing standard output stream.

    It implements only the minimum set of methods of ``file`` interface used
    in this application.
    """
    def write(self, *args, **kwargs):
        """ Let's totally ignore what we are given. """
        pass

class MetaCommand(object):
    """
    Main application class. It instantiates configuration object, logging and
    then it passes control to commands.

    Example usage:

        MetaCommand().run()
    """

    def __init__(self):
        # allow exceptions in lmi shell
        LMIUtil.lmi_set_use_exceptions(True)
        # instance of CommandManager, created when first needed
        self._command_manager = None
        self.stdout = sys.stdout
        self.stderr = sys.stderr
        self.stdin = sys.stdin
        # instance of Session, created when needed
        self._session = None
        # instance of Configuration, created in setup()
        self.config = None
        # dictionary of not yet processed options, it's created in setup()
        self._options = None

    def _configure_logging(self):
        """
        Setup logging. It expects Configuration object to be already
        initialized.

        Logging can be tuned in various ways:

            * In configuration file with options:
                * [Log] OutputFile
                * [Log] FileFormat
                * [Log] ConsoleFormat
            * With command line options:
                * -v flags - each such flag increases logging level of
                  what is logged into console
                * -q - supress any output made to stdout
                * --trace - whether exception tracebacks are shown

        Implicitly only warnings and errors are logged to the standard
        error stream without any tracebacks.
        """
        root_logger = logging.getLogger('')
        # make a reference to null handlers (one should be installed)
        null_handlers = [  h for h in root_logger.handlers
                        if isinstance(h, logging.NullHandler)]
        try:
            logging_level = getattr(logging, self.config.logging_level.upper())
        except KeyError:
            logging_level = logging.ERROR

        # Set up logging to a file
        log_file = self.config.get_safe('Log', 'OutputFile')
        if log_file is not None:
            file_handler = logging.FileHandler(filename=log_file)
            formatter = logging.Formatter(
                    self.config.get_safe('Log', 'FileFormat', raw=True))
            file_handler.setFormatter(formatter)
            root_logger.addHandler(file_handler)

        # Always send higher-level messages to the console via stderr
        console = logging.StreamHandler(self.stderr)
        console_level_default = logging.ERROR if log_file else logging_level
        console_level = {
                Configuration.OUTPUT_SILENT  : logging.ERROR,
                Configuration.OUTPUT_WARNING : logging.WARNING,
                Configuration.OUTPUT_INFO    : logging.INFO,
                Configuration.OUTPUT_DEBUG   : logging.DEBUG,
            }.get(self.config.verbosity, console_level_default)
        console.setLevel(console_level)
        formatter = logging.Formatter(
                self.config.get_safe('Log', 'ConsoleFormat', raw=True))
        console.setFormatter(formatter)
        root_logger.addHandler(console)
        root_logger.setLevel(min(logging_level, console_level))

        # remove all null_handlers
        for handler in null_handlers:
            root_logger.removeHandler(handler)
        if self.config.silent:
            self.stdout = NullFile()

    @property
    def command_manager(self):
        """
        Return instance of ``CommandManager``. It's initialized when first
        needed.

        :rtype: (``CommandManager``)
        """
        if self._command_manager is None:
            self._command_manager = CommandManager()
            self._command_manager.add_command('help', Help)
        return self._command_manager

    @property
    def session(self):
        """
        Return instance of Session. Instantiated when first needed.

        :rtype: (``Session``)
        """
        if self._session is None:
            if (   not self._options['--host']
               and not self._options['--hosts-file']):
                LOG().critical(
                        "missing one of (--host | --hosts-file) arguments")
                sys.exit(1)
            hosts = []
            if self._options['--hosts-file']:
                hosts_file = self._options['--hosts-file']
                try:
                    with open(self._options['--hosts-file'], 'rt'):
                        hosts.extend(parse_hosts_file(
                            self._options['--hosts-file']))
                except (OSError, IOError) as err:
                    LOG().critical('could not read hosts file "%s": %s',
                            hosts_file, err)
                    sys.exit(1)
            hosts.extend(self._options['--host'])
            if self._options['--user']:
                credentials = {h: (self._options['--user'], '') for h in hosts}
            else:
                credentials = None
            self._session = Session(self, hosts, credentials)
        return self._session

    def print_version(self):
        """ Print version of this egg to stdout. """
        self.stdout.write("%s\n" % util.get_version())

    def setup(self, options):
        """
        Initialise global Configuration object and set up logging.

        :param options: (``dict``) Dictionary of options parsed from command
            line by docopt.
        """
        conf_kwargs = {}
        if options['--config-file']:
            conf_kwargs['user_config_file_path'] = options.pop('--config-file')
        self.config = Configuration.get_instance(**conf_kwargs)
        self.config.trace = options.pop('--trace', False)
        if options['--quiet']:
            self.config.verbosity = Configuration.OUTPUT_SILENT
        elif options['-v'] and options['-v'] > 0:
            self.config.verbosity = options['-v']
        self._configure_logging()
        del options['--quiet']
        del options['-v']
        self._options = options

    def run(self, argv):
        """
        Equivalent to the main program for the application.

        :param argv: (``list``) Input arguments and options.
            Contains all arguments but the application name.
        """
        cmd = TopLevelCommand(self)
        try:
            return cmd.run(argv)
        except Exception as exc:
            trace = True if self.config is None else self.config.trace
            if isinstance(exc, errors.LmiError) or not trace:
                LOG().error(exc)
            else:
                LOG().exception("fatal")
            return 1

def main(argv=sys.argv[1:]):
    """
    Main entry point function. It just passes arguments to instantiated
    ``MetaCommand``.
    """
    return MetaCommand().run(argv)

