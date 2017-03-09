""" Nose Plugin for finding leaked memory """

__author__ = "Andrew S. Brown (asbrown@nextdoor.com)"

from builtins import *
import collections
import functools
import gc
import operator
import re

try:
    from unittest import mock
    from unittest.mock import Base
except ImportError:
    import mock
    try:
        from mock.mock import Base
    except ImportError:
        from mock import Base

import resource
import sys
import traceback
import weakref

from nose.plugins import Plugin
from pympler import muppy
from pympler import summary
import termcolor

KnownMock = collections.namedtuple('KnownMock', ['mock_ref', 'test', 'traceback'])
ReportDetail = collections.namedtuple('ReportDetail', ['title', 'color'])

LEVEL_DIR = 1
LEVEL_MODULE = 2
LEVEL_CLASS = 3
LEVEL_TEST = 4


class LeakDetected(Exception):
    pass


class LeakDetectorTestCase(object):
    def __init__(self, test, detector):
        self.test = test
        self.detector = detector

    def __call__(self, result):
        return self.detector.run_test(self.test, result)


class LeakDetectorPlugin(Plugin):
    name = 'leak-detector'
    detect_leaked_mocks = True
    fail_fast = True

    REPORT_DETAILS = {
        LEVEL_DIR: ReportDetail(title='Directory', color='magenta'),
        LEVEL_MODULE: ReportDetail(title='Module', color='green'),
        LEVEL_CLASS: ReportDetail(title='Class', color='blue'),
        LEVEL_TEST: ReportDetail(title='Test', color='red'),
    }
    CLASS_NAME = 'LeakDetector'

    def __init__(self):
        super(LeakDetectorPlugin, self).__init__()
        self.reporting_level = 0
        self.check_for_leaks_before_next_test = True
        self.report_delta = False
        self.save_traceback = False
        self.ignore_patterns = []

        self.patch_mock = False
        self.last_test_name = None
        self.last_test_module_name = None
        self.last_test_type = None
        self.last_test_class_name = None
        self.level_name = {}
        self.previous_summaries = {}
        self.current_summary = None
        self.skip_next_check = False
        self.current_test_errored = False
        self.last_test_errored = False

        self.mock_patch = None
        self._final_exc_info = None
        self.known_mocks = []
        self.previous_mock_refs = []

    def options(self, parser, env):
        """
        Add options to command line.
        """
        super(LeakDetectorPlugin, self).options(parser, env)
        parser.add_option("--leak-detector-level", action="store",
                          default=env.get('NOSE_LEAK_DETECTOR_LEVEL'),
                          dest="leak_detector_level",
                          help="Level at which to detect leaks and report memory deltas "
                               "(0=None, 1=Dir, 2=Module, 3=TestCaseClass, 4=Test)")

        parser.add_option("--leak-detector-report-delta", action="store_true",
                          default=env.get('NOSE_LEAK_DETECTOR_REPORT_DELTA'),
                          dest="leak_detector_report_delta",
                          help="")

        parser.add_option("--leak-detector-patch-mock", action="store_true",
                          default=env.get('NOSE_LEAK_DETECTOR_PATCH_MOCK', True),
                          dest="leak_detector_patch_mock",
                          help="")

        parser.add_option("--leak-detector-add-traceback", action="store_true",
                          default=env.get('NOSE_LEAK_DETECTOR_SAVE_TRACEBACK', False),
                          dest="leak_detector_save_traceback",
                          help="")

        parser.add_option("--leak-detector-ignore-pattern", action="append",
                          default=(list(filter(operator.truth,
                                               env.get('NOSE_LEAK_DETECTOR_IGNORE_PATTERNS',
                                                       '').split(','))) or
                                   ['NOSE_LEAK_DETECTOR_IGNORE']),
                          dest="leak_detector_ignore_patterns",
                          help="")

    def configure(self, options, conf):
        """
        Configure plugin.
        """
        super(LeakDetectorPlugin, self).configure(options, conf)
        if options.leak_detector_level:
            self.reporting_level = int(options.leak_detector_level)
        self.report_delta = options.leak_detector_report_delta
        self.patch_mock = options.leak_detector_patch_mock
        self.ignore_patterns = options.leak_detector_ignore_patterns
        self.save_traceback = options.leak_detector_save_traceback
        self.multiprocessing_enabled = bool(getattr(options, 'multiprocess_workers', False))

    def setOutputStream(self, stream):
        self.stream = stream
        return None  # We aren't changing the output stream, just capturing it

    def begin(self):
        self.create_initial_summary()

        if self.detect_leaked_mocks:

            # Record pre-existing mocks
            gc.collect()
            self.known_mocks = list(KnownMock(weakref.ref(m), None, None)
                                    for m in gc.get_objects() if isinstance(m, mock.Mock))
            self.previous_mock_refs = list(m.mock_ref for m in self.known_mocks)

            if self.patch_mock:
                detector = self

                def decorator(f):
                    @functools.wraps(f)
                    def wrapper(new_mock, *args, **kwargs):
                        f(new_mock, *args, **kwargs)
                        detector.register_mock(new_mock, detector.level_name.get(LEVEL_TEST))
                    return wrapper

                Base.__init__ = decorator(Base.__init__)

    def prepareTestCase(self, test):
        return LeakDetectorTestCase(test, detector=self)

    def create_initial_summary(self):
        # forget the current summary now that we are starting a new test
        self.current_summary = None

        if self.report_delta:
            initial_summary = self.get_summary()

        # Before any tests are run record a memory summary
        if not self.previous_summaries and self.reporting_level and self.report_delta:
            for i in range(1, LEVEL_TEST + 1):
                self.previous_summaries[i] = initial_summary

    def beforeTest(self, test):
        self.last_test_errored = self.current_test_errored
        self.current_test_errored = False

        test_type = type(test.test)
        test_module_name = test.test.__class__.__module__

        if self.last_test_name:
            if self.last_test_type is not test_type:
                self.finished_level(LEVEL_CLASS, self.last_test_class_name)

            if self.last_test_module_name != test_module_name:
                self.finished_level(LEVEL_MODULE, self.last_test_module_name)

        if not self.last_test_name or self.last_test_module_name != test_module_name:
            self.started_level(LEVEL_MODULE, test_module_name)

        if not self.last_test_name or self.last_test_type is not test_type:
            self.started_level(LEVEL_CLASS, test.test.__class__.__name__)

        self.started_level(LEVEL_TEST, str(test))

    def afterTest(self, test):
        self.last_test_name = str(test.test)
        self.last_test_class_name = test.test.__class__.__name__
        self.last_test_module = test.test.__class__.__module__

        self.finished_level(LEVEL_TEST, str(test))

    def run_test(self, test, result):
        self.current_summary = None

        def do_check(before):
            try:
                self.check_for_leaks()
            except LeakDetected as e:
                exception_class, value, _ = sys.exc_info()
                message = str(value)
                if self.last_test_name:
                    if self.last_test_errored:
                        message = ('Leak detected after test '
                                   '(details suppressed due to earlier error)')
                    else:
                        message = "After test '%s': %s" % (self.last_test_name, message)
                else:
                    if before:
                        message = 'Before test: %s' % message
                    else:
                        message = 'Before any tests were run: %s' % message
                result.addError(test, (exception_class, message, None))
                if not self.fail_fast:
                    result.stop()

        if self.check_for_leaks_before_next_test:
            do_check(before=True)
            self.check_for_leaks_before_next_test = False

        test.test(result)

        if self.reporting_level >= LEVEL_TEST:
            do_check(before=False)

    def get_level_path(self):
        name = u''
        for i in range(1, self.reporting_level + 1):
            default_name = '<unknown %s>' % self.REPORT_DETAILS[i].title.lower()
            name += '/' + (self.level_name.get(i, default_name) or default_name)
        return name

    # TODO(asbrown): nose plugins report changes in mdule and directory at load time so we'll
    # have to save this information with each test to detect changes in module and directory
    # def beforeContext(self):
    #     self.started_level(LEVEL_MODULE)
    #
    # def afterContext(self):
    #     self.finished_level(LEVEL_MODULE,
    #                         self.last_test.test.__module__ if self.last_test else None)

    # def beforeDirectory(self, path):
    #     self.started_level(LEVEL_DIR, path)
    #
    # def afterDirectory(self, path):
    #     self.finished_level(LEVEL_DIR, path)

    def handleError(self, test, err):
        self.current_test_errored = True

    def started_level(self, level, name=None):
        self.level_name[level] = name

    def finished_level(self, level, name):
        if level <= int(self.reporting_level):
            self.check_for_leaks_before_next_test = True

        if level > int(self.reporting_level):
            return

        if self.report_delta:
            color = self.REPORT_DETAILS[level].color
            report = u'Memory Delta Report for %s: %s\n' % (
                self.REPORT_DETAILS[level].title.upper(), name)
            memory_usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss

            self.stream.write('Peak memory usage for %s: %s\n' % (name, memory_usage))

            old_summary = self.previous_summaries[level]

            if not self.current_summary:
                self.current_summary = self.get_summary()

            diff = self._fast_get_summary_diff(old_summary, self.current_summary)
            filtered_diff = [row for row in diff if row[1] or row[2]]
            if filtered_diff:
                self.stream.write(termcolor.colored(report, color))
                self.stream.write('\n'.join(summary.format_(filtered_diff)))
                self.stream.write('\n')
            else:
                report += 'No changes\n'
                report += 'Peak memory usage: %s\n' % memory_usage
                self.stream.write(termcolor.colored(report, color))

            self.previous_summaries[level] = self.current_summary

    def final_check(self):
        if self.detect_leaked_mocks and not self._final_exc_info:
            self.current_summary = None
            self.previous_summaries.clear()

            try:
                self.check_for_leaks()
            except LeakDetected:
                self._final_exc_info = sys.exc_info()[:2]

    def report(self, stream):
        # Let each worker speak for itself when multiprocessing is enabled
        if self.multiprocessing_enabled:
            return

        self.final_check()

        msg = u'Leak Detector Report: '
        if self._final_exc_info:
            msg += 'FAILED: '
            color = 'red'
            msg += str(self._final_exc_info[1])
        else:
            color = 'green'
            msg += 'PASSED: All mocks have been reset or garbage collected.'
        msg += '\n'
        stream.write(termcolor.colored(msg, color))

    def finalize(self, result):
        self.final_check()

        if self.patch_mock and hasattr(Base.__init__, '__wrapped__'):
            Base.__init__ = Base.__init__.__wrapped__

        # Guarantee a test failure if we saw an exception during the report phase
        if self._final_exc_info and self.last_test_name:
            exception_class, value = self._final_exc_info
            del self._final_exc_info  # ensure that we remove the reference to the failed mock
            raise value

    def register_mock(self, new_mock, test_name):
        # Save the traceback on the patch so we can see where it was created
        if self.save_traceback:
            frames = [f for f in traceback.format_stack(limit=10)[:-1] if 'mock.py' not in f]
            # Reversing these makes them easier to see
            tb = list(reversed(frames))
        else:
            tb = None

        # Save the mock to our list of mocks
        self.known_mocks.append(KnownMock(weakref.ref(new_mock), test_name, tb))

    def check_for_leaks(self):
        live_mocks = list(filter(lambda m: m.mock_ref() is not None,
                                 self.known_mocks))

        previous_mock_ids = set(id(r()) for r in self.previous_mock_refs)

        def get_new_mocks():
            # Use list so that we don't keep around a generate that isn't gc'd
            return list(m for m in live_mocks if m.mock_ref() and
                        id(m.mock_ref()) not in previous_mock_ids and
                        not any(re.search(pattern, repr(m.mock_ref()))
                                for pattern in self.ignore_patterns))

        def get_called_mocks():
            return list(filter(lambda m: m.mock_ref() is not None and m.mock_ref().called and
                               not any(re.search(pattern, repr(m.mock_ref()))
                                       for pattern in self.ignore_patterns),
                               self.known_mocks))

        self.previous_mock_refs = list([weakref.ref(m.mock_ref()) for m in live_mocks])

        if get_new_mocks() or get_called_mocks():
            # Try again after garbage collecting
            gc.collect()
            new_mocks = get_new_mocks()
            called_mocks = get_called_mocks()
            if not (new_mocks or called_mocks):
                return

            def error_message(bad_mock):
                data = vars(bad_mock.mock_ref())
                msg = ' --> '.join(bad_mock.traceback or
                    ["No traceback available.  Consider setting '--leak-detector-add-traceback' "
                     "to see where this mock was created."])
                if bad_mock.test:
                    msg = "Created in test '%s' [%s]" % (bad_mock.test, msg)
                return msg + ' : ' + str(data)

            def number(l):
                return ' '.join(['%d) %s' % (i + 1, v) for i, v in enumerate(l)])

            msg = ''
            if new_mocks:
                msg += ('Found %d new mock(s) that have not been garbage collected:\n%s' %
                        (len(new_mocks), number(map(error_message, new_mocks))))

            if called_mocks:
                msg += ('Found %d dirty mock(s) that have not been garbage collected or reset:\n%s' %
                        (len(called_mocks),
                         number(map(error_message,
                                  [m for m in called_mocks if id(m.mock_ref())
                                   not in [id(n.mock_ref()) for n in new_mocks]]))))

            # Ensure hard references to the mocks are no longer on the stack
            del live_mocks[:], new_mocks[:], called_mocks[:]
            raise LeakDetected(msg)

    def get_summary(self):
        gc.collect()
        # exclude everything in this object itself
        excluded = set(id(o) for o in muppy.get_referents(self))
        return summary.summarize(o for o in muppy.get_objects() if not id(o) in excluded)

    @staticmethod
    def is_called_mock(obj):
        return isinstance(obj, mock.Mock) and obj.called

    # from https://github.com/pympler/pympler/pull/6
    @staticmethod
    def _fast_get_summary_diff(left, right):

        objects_key = lambda object_footprint: object_footprint[0]
        val_neg = lambda lval: [lval[0], -lval[1], -lval[2]]

        def next_safe(it):
            try:
                val = it.next()
                return val, False
            except StopIteration:
                return None, True

        lsorted = sorted(left, key=objects_key)
        rsorted = sorted(right, key=objects_key)

        lit = iter(lsorted)
        rit = iter(rsorted)
        lval = None
        rval = None
        lend = False
        rend = False
        ret = []
        while not lend or not rend:
            if lval is None:
                if lend:
                    if rval:
                        ret.extend([rval] + [x for x in rit])
                    break
                else:
                    lval, lend = next_safe(lit)

            if rval is None:
                if rend:
                    if lval:
                        ret.extend([val_neg(lval)] + [val_neg(x) for x in lit])
                    break
                else:
                    rval, rend = next_safe(rit)

            if lval is None or rval is None:
                continue

            if objects_key(lval) == objects_key(rval):
                ret.append([rval[0], rval[1] - lval[1], rval[2] - lval[2]])
                lval, lend = next_safe(lit)
                rval, rend = next_safe(rit)
            elif objects_key(lval) < objects_key(rval):
                ret.append(val_neg(lval))
                lval, lend = next_safe(lit)
            else:
                ret.append(rval)
                rval, rend = next_safe(rit)

        return ret


# Register this plugin with multiprocess plugin if applicable.
try:
    from nose.plugins import multiprocess
    multiprocess._instantiate_plugins = multiprocess._instantiate_plugins or []
    multiprocess._instantiate_plugins.append(LeakDetectorPlugin)
except ImportError:
    pass
