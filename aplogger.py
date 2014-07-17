import codecs
import doctest
import os
import sys
import traceback
import re
import inspect
import fcntl
import select
import datetime
from StringIO import StringIO
from time import time
from xml.sax import saxutils

from nose.plugins.base import Plugin
from nose.exc import SkipTest
from nose.pyversion import force_unicode, format_exception

# Invalid XML characters, control characters 0-31 sans \t, \n and \r
CONTROL_CHARACTERS = re.compile(r"[\000-\010\013\014\016-\037]")

TEST_ID = re.compile(r'^(.*?)(\(.*\))$')

def xml_safe(value):
    """Replaces invalid XML characters with '?'."""
    return CONTROL_CHARACTERS.sub('?', value)

def escape_cdata(cdata):
    """Escape a string for an XML CDATA section."""
    return xml_safe(cdata).replace(']]>', ']]>]]&gt;<![CDATA[')

def id_split(idval):
    m = TEST_ID.match(idval)
    if m:
        name, fargs = m.groups()
        head, tail = name.rsplit(".", 1)
        return [head, tail+fargs]
    else:
        return idval.rsplit(".", 1)

def nice_classname(obj):
    """Returns a nice name for class object or class instance.

        >>> nice_classname(Exception()) # doctest: +ELLIPSIS
        '...Exception'
        >>> nice_classname(Exception) # doctest: +ELLIPSIS
        '...Exception'

    """
    if inspect.isclass(obj):
        cls_name = obj.__name__
    else:
        cls_name = obj.__class__.__name__
    mod = inspect.getmodule(obj)
    if mod:
        name = mod.__name__
        # jython
        if name.startswith('org.python.core.'):
            name = name[len('org.python.core.'):]
        return "%s.%s" % (name, cls_name)
    else:
        return cls_name

def exc_message(exc_info):
    """Return the exception's message."""
    exc = exc_info[1]
    if exc is None:
        # str exception
        result = exc_info[0]
    else:
        try:
            result = str(exc)
        except UnicodeEncodeError:
            try:
                result = unicode(exc)
            except UnicodeError:
                # Fallback to args as neither str nor
                # unicode(Exception(u'\xe6')) work in Python < 2.6
                result = exc.args[0]
    result = force_unicode(result, 'UTF-8')
    return xml_safe(result)

def readFromPipe(pipe):
    try:
        data = ''
        while select.select([pipe,],[],[], 2)[0]:
            data += os.read(pipe, 1)
    except select.error:
        data = None
        print('select error')
        raise select.error
    finally:

        return data

class Tee(object):
    def __init__(self, encoding, *args):
        self._encoding = encoding
        self._streams = args

    def write(self, data):
        data = force_unicode(data, self._encoding)
        for s in self._streams:
            s.write(data)

    def writelines(self, lines):
        for line in lines:
            self.write(line)

    def flush(self):
        for s in self._streams:
            s.flush()

    def isatty(self):
        return False


class APLogger(Plugin):
    """This plugin provides ArduPilot test results in the standard XUnit XML format."""
    name = 'aplogger'
    score = 1500
    encoding = 'UTF-8'
    error_report_file = None

    def __init__(self):
        super(APLogger, self).__init__()
        self._capture_stack = []
        self._currentStdout = None
        self._currentStderr = None
        self._currentMavpipe = None
        self._currentJSBpipe = None

    def _timeTaken(self):
        if hasattr(self, '_timer'):
            taken = time() - self._timer
        else:
            # test died before it ran (probably error in setup())
            # or success/failure added before test started probably 
            # due to custom TestResult munging
            taken = 0.0
        return taken

    def _quoteattr(self, attr):
        """Escape an XML attribute. Value can be unicode."""
        attr = xml_safe(attr)
        return saxutils.quoteattr(attr)

    def options(self, parser, env):
        """Sets additional command line options."""
        Plugin.options(self, parser, env)
        #parser.add_option(
            # '--xunit-file', action='store',
            # dest='xunit_file', metavar="FILE",
            # default=env.get('NOSE_XUNIT_FILE', 'nosetests.xml'),
            # help=("Path to xml file to store the xunit report in. "
            #       "Default is nosetests.xml in the working directory "
            #       "[NOSE_XUNIT_FILE]"))
        parser.add_option(
            '--aplogger-jsbpipe', action='store',
            dest='jsbpipe', metavar="FILE",
            default='jsb_pipe',
            help=("Path to the JSBSim output named pipe. "
                  "Default is jsb_pipe in the working directory."))
        parser.add_option(
            '--aplogger-mavpipe', action='store',
            dest='mavpipe', metavar="FILE",
            default='mavproxy_pipe',
            help=("Path to the MAVProxy output named pipe. "
                  "Default is mavproxy_pipe in the working directory."))

    def configure(self, options, config):
        """Configures the xunit plugin."""
        Plugin.configure(self, options, config)
        self.config = config
        if self.enabled:
            self.stats = {'errors': 0,
                          'failures': 0,
                          'passes': 0,
                          'skipped': 0
                          }
            self.errorlist = []
            self.error_report_file_name = os.path.realpath(options.xunit_file)
            self.jsb_pipe = options.jsbpipe
            self.mav_pipe = options.mavpipe
            self.jsb_pipename = options.jsbpipe
            self.mav_pipename = options.mavpipe

    def report(self, stream):
        """Writes an Xunit-formatted XML file

        The file includes a report of test errors and failures.

        """
        self.error_report_file = codecs.open(self.error_report_file_name, 'w',
                                             self.encoding, 'replace')

        self.stats['encoding'] = self.encoding
        self.stats['total'] = (self.stats['errors'] + self.stats['failures']
                               + self.stats['passes'] + self.stats['skipped'])
        self.error_report_file.write(
            u'<?xml version="1.0" encoding="%(encoding)s"?>'
            u'<TestRun project_name="Ender\'s Game" tests="%(total)d" '
            u'errors="%(errors)d" failures="%(failures)d" '
            u'passed="%(passes)d" skip="%(skipped)d">' % self.stats)

        self.error_report_file.write(u''.join([force_unicode(e, self.encoding)
                                               for e in self.errorlist]))

        self.error_report_file.write(u'</TestRun>')
        self.error_report_file.close()
        if self.config.verbosity > 1:
            stream.writeln("-" * 70)
            stream.writeln("XML: %s" % self.error_report_file.name)

    def _startCapture(self):
        self._capture_stack.append((sys.stdout, sys.stderr, self.jsb_pipe, self.mav_pipe))

        self._currentStdout = StringIO()
        self._currentStderr = StringIO()

        self._currentJSBpipe = os.open(self.jsb_pipename, os.O_NONBLOCK)
        self._currentMavpipe = os.open(self.mav_pipename, os.O_NONBLOCK)

        fcntl.fcntl(self._currentJSBpipe, fcntl.F_SETFL, os.O_NONBLOCK)
        fcntl.fcntl(self._currentMavpipe, fcntl.F_SETFL, os.O_NONBLOCK)

        sys.stdout = Tee(self.encoding, self._currentStdout, sys.stdout)
        sys.stderr = Tee(self.encoding, self._currentStderr, sys.stderr)

        self.jsb_pipe = Tee(self.encoding, self._currentJSBpipe, self.jsb_pipe)
        self.mav_pipe = Tee(self.encoding, self._currentMavpipe, self.mav_pipe)

    def startContext(self, context):
        self._startCapture()

    def stopContext(self, context):
        self._endCapture()

    def beforeTest(self, test):
        """Initializes a timer before starting a test."""
        self._timer = time()
        self._startCapture()

    def _endCapture(self):
        if self._capture_stack:
            sys.stdout, sys.stderr, self.jsb_pipe, self.mav_pipe = self._capture_stack.pop()

    def afterTest(self, test):
        self._endCapture()
        self._currentStdout = None
        self._currentStderr = None
        self._currentJSBpipe = None
        self._currentMavpipe = None

    def finalize(self, test):
        while self._capture_stack:
            self._endCapture()

    def _getCapturedStdout(self):
        if self._currentStdout:
            value = self._currentStdout.getvalue()
            if value:
                return '<system-out><![CDATA[%s]]></system-out>' % escape_cdata(
                        value)
        return ''

    def _getCapturedStderr(self):
        if self._currentStderr:
            value = self._currentStderr.getvalue()
            if value:
                return '<system-err><![CDATA[%s]]></system-err>' % escape_cdata(
                        value)
        return ''

    def _getCapturedJSB(self):

        data = readFromPipe(self._currentJSBpipe)

        if data:
            return '<JSBSim-out><![CDATA[%s]]></JSBSim-out>' % escape_cdata(data)
        else:
            return 'No JSBSim output'

    def _getCapturedMAV(self):

        data = readFromPipe(self._currentMavpipe)

        if data:
            return '<MAVProxy-out><![CDATA[%s]]></MAVProxy-out>' % escape_cdata(data)
        else:
            return 'No mavproxy output'

    def addError(self, test, err, capt=None):
        """Add error output to Xunit report.
        """
        taken = self._timeTaken()

        if issubclass(err[0], SkipTest):
            type = 'skipped'
            self.stats['skipped'] += 1
        else:
            type = 'error'
            self.stats['errors'] += 1

        tb = format_exception(err, self.encoding)
        id = test.id()

        self.errorlist.append(
            u'<TestCase status="ERROR" classname=%(cls)s name=%(name)s time="%(taken).3f" datestamp=%(datestamp)s>'
            u'<%(type)s type=%(errtype)s message=%(message)s><![CDATA[%(tb)s]]>'
            u'</%(type)s>%(systemout)s%(systemerr)s'
            u'%(MAVProxyout)s%(JSBsimout)s</TestCase>' %
            {'cls': self._quoteattr(id_split(id)[0]),
             'datestamp': self._quoteattr(str(datetime.datetime.now()).split('.')[0]),
             'name': self._quoteattr(id_split(id)[-1]),
             'taken': taken,
             'type': type,
             'errtype': self._quoteattr(nice_classname(err[0])),
             'message': self._quoteattr(exc_message(err)),
             'tb': escape_cdata(tb),
             'systemout': self._getCapturedStdout(),
             'systemerr': self._getCapturedStderr(),
             'JSBsimout': self._getCapturedJSB(),
             'MAVProxyout': self._getCapturedMAV(),
             })

    def addFailure(self, test, err, capt=None, tb_info=None):
        """Add failure output to Xunit report.
        """
        taken = self._timeTaken()
        tb = format_exception(err, self.encoding)
        self.stats['failures'] += 1
        id = test.id()

        self.errorlist.append(
            u'<TestCase status="FAIL" classname=%(cls)s name=%(name)s time="%(taken).3f" datestamp=%(datestamp)s>'
            u'<failure type=%(errtype)s message=%(message)s><![CDATA[%(tb)s]]>'
            u'</failure>%(systemout)s%(systemerr)s'
            u'%(MAVProxyout)s%(JSBsimout)s</TestCase>' %
            {'cls': self._quoteattr(id_split(id)[0]),
             'datestamp': self._quoteattr(str(datetime.datetime.now()).split('.')[0]),
             'name': self._quoteattr(id_split(id)[-1]),
             'taken': taken,
             'errtype': self._quoteattr(nice_classname(err[0])),
             'message': self._quoteattr(exc_message(err)),
             'tb': escape_cdata(tb),
             'systemout': self._getCapturedStdout(),
             'systemerr': self._getCapturedStderr(),
             'JSBsimout': self._getCapturedJSB(),
             'MAVProxyout': self._getCapturedMAV(),
             })

    def addSuccess(self, test, capt=None):
        """Add success output to Xunit report.
        """
        taken = self._timeTaken()
        self.stats['passes'] += 1
        id = test.id()
        self.errorlist.append(
            u'<TestCase status="PASS" classname=%(cls)s name=%(name)s '
            u'time="%(taken).3f" datestamp=%(datestamp)s>%(systemout)s%(systemerr)s'
            u'%(MAVProxyout)s%(JSBsimout)s</TestCase>' %
            {'cls': self._quoteattr(id_split(id)[0]),
             'datestamp': self._quoteattr(str(datetime.datetime.now()).split('.')[0]),
             'name': self._quoteattr(id_split(id)[-1]),
             'taken': taken,
             'systemout': self._getCapturedStdout(),
             'systemerr': self._getCapturedStderr(),
             'JSBsimout': self._getCapturedJSB(),
             'MAVProxyout': self._getCapturedMAV(),
             })