#!/usr/bin/python3
import unittest
import os
import os.path
import getopt
import sys
import glob
import shutil
from subprocess import Popen, STDOUT, PIPE


debug = False
fix = False


# Directory names for tests and the ccAPPS runtime.
testdir = os.path.abspath(os.path.dirname(sys.argv[0]))


def usage():
    # Print help information and exit
    print(
        """
        Usage to run all tests:
          ./runtest.py [options]

        Usage with list of tests to run:
          ./runtest.py [options] {test1} {test2} ...

        With the following options:
          -d  --debug:
             Verbose output of the test.
          -e  --exclude:'
             Skip a specific test from the suite.
             This option can be specified multiple times.
          -f  --fix:'
             Copy output back to the expect files.
             Handy for a bulk acceptance of massive test result changes.
        """
    )


def runTestSuite():
    global debug, testdir, fix

    # ccAPPS uses the time functions from the C-library, which is sensitive to
    # timezone settings. In particular the daylight saving time of different
    # timezones is of interest: it applies only to some timezones, and different
    # timezones switch to summer time at various dates.
    # The next statement makes sure the test are all running with the same timezone,
    # and in addition a timezone without DST.
    os.environ["TZ"] = "EST"

    # Parse the command line
    opts = []
    tests = []
    excluded = []

    try:
        opts, args = getopt.getopt(
            sys.argv[1:], "fdvhre:", ["fix", "debug", "help", "exclude="]
        )
    except getopt.GetoptError:
        usage()
        sys.exit(1)
    for o, a in opts:
        if o in ("-d", "--debug"):
            debug = True
        elif o in ("-e", "--exclude"):
            excluded.append(a)
        elif o in ("-h", "--help"):
            # Print help information and exit
            usage()
            sys.exit(1)
        elif o in ("-f", "--fix"):
            fix = True
    for i in args:
        tests.extend(
            os.path.relpath(path, testdir)
            for path in glob.glob(os.path.join(testdir, i))
        )

    # Executable to run
    os.environ["ccAPPS_HOME"] = os.path.join(testdir, "..", "bin")
    os.environ["EXECUTABLE"] = os.path.join(testdir, "..", "bin", "ccAPPS")

    # Update the search path for shared libraries, such that the modules
    # can be picked up.
    # Different platforms use different environment variables for this, and we
    # set all.
    for var in ("LD_LIBRARY_PATH", "LIBPATH", "SHLIB_PATH", "PATH"):
        if var in os.environ:
            os.environ[var] += os.pathsep + os.environ["ccAPPS_HOME"]
        else:
            os.environ[var] = os.environ["ccAPPS_HOME"]

    # Define a list with tests to run
    if len(tests) == 0:
        # No tests specified, so run them all
        subdirs = os.listdir(testdir)
        subdirs.sort()
        for i in subdirs:
            if os.path.isdir(os.path.join(testdir, i)):
                tests.append(i)
    else:
        # A list of tests has been specified, and we now validate it
        for i in tests:
            if not os.path.isdir(os.path.join(testdir, i)):
                print("Warning: Test directory " + i + " doesn't exist")
                tests.remove(i)

    # Now define the test suite
    AllTests = unittest.TestSuite()
    for i in tests:

        # Skip excluded tests
        if i in excluded:
            continue

        # Expand to directory names
        i = os.path.normpath(i)
        tmp = os.path.join(testdir, i, i)

        # Check the test type
        if os.path.isfile(os.path.join(testdir, i, "runtest.py")):
            # Type 1: Python script runtest.py available
            AllTests.addTest(ccAPPSTest(i, "runScript"))
        elif os.path.isfile(tmp + ".xml") or os.path.isfile(tmp + ".py"):
            # Type 2: input XML or Python file specified
            AllTests.addTest(ccAPPSTest(i, "runXML"))
        else:
            # Undetermined - not a test directory
            print("Warning: Unrecognized test in directory " + i)

    # Finally, run the test suite now
    if "ccAPPS_HOME" in os.environ:
        print(
            "Running",
            AllTests.countTestCases(),
            "tests from directory",
            testdir,
            "with ccAPPS_HOME",
            os.environ["ccAPPS_HOME"],
        )
    else:
        print("Running", AllTests.countTestCases(), "tests from directory", testdir)
    result = unittest.TextTestRunner(verbosity=2, descriptions=False).run(AllTests)
    if not result.wasSuccessful():
        sys.exit(1)


class ccAPPSTest(unittest.TestCase):
    def __init__(self, directoryname, methodName):
        self.subdirectory = directoryname
        super().__init__(methodName)

    def setUp(self):
        global testdir
        os.chdir(os.path.join(testdir, self.subdirectory))

    def __str__(self):
        """Use the directory name as the test name."""
        return self.subdirectory

    def runProcess(self, cmd):
        """Run a child process."""
        global debug, testdir
        try:
            if debug:
                o = None
                print("\nOutput:")
            else:
                o = PIPE
            proc = Popen(cmd, bufsize=0, stdout=o, stderr=STDOUT, shell=True)
            if not debug:
                # Because the process doesn't stop until we've read the pipe.
                proc.communicate()
            res = proc.wait()
            if res:
                self.assertFalse("Exit code non-zero")
        except KeyboardInterrupt:
            # The test has been interupted, which counts as a failure
            self.assertFalse("Interrupted test")

    def runScript(self):
        """Running a test script"""
        self.runProcess("python3 %s" % os.path.join(".", "runtest.py"))

    def runXML(self):
        """Running the command line tool with an XML or Python file as argument."""
        global debug

        # Delete previous output
        self.output = glob.glob("output.*.xml")
        self.output.extend(glob.glob("output.*.txt"))
        self.output.extend(glob.glob("output.*.tmp"))

        try:
            for i in self.output:
                os.remove(i)

            # Run the executable
            if os.path.isfile(self.subdirectory + ".xml"):
                self.runProcess(
                    os.environ["EXECUTABLE"]
                    + " -validate "
                    + self.subdirectory
                    + ".xml"
                )
            else:
                self.runProcess(
                    os.environ["EXECUTABLE"] + " -validate " + self.subdirectory + ".py"
                )

            # Now check the output file, if there is an expected output given
            nr = 1
            while os.path.isfile(f"{self.subdirectory}.{nr}.expect"):
                expect = f"{self.subdirectory}.{nr}.expect"
                output = f"output.{nr}.xml"
                if os.path.isfile(output):
                    if fix:
                        shutil.copy(output, expect)
                    if debug:
                        print("Comparing expected and actual output", nr)
                    if diff(expect, output):
                        self.assertFalse(
                            "Difference in output " + str(nr),
                            "Difference in output " + str(nr),
                        )
                else:
                    self.assertFalse(
                        "Missing ccAPPS output file " + str(nr),
                        "Missing ccAPPS output file " + str(nr),
                    )
                nr += 1
        except Exception as e:
            # Skip excluded tests
            if self.subdirectory not in ["setup_1", "setup_2", "setup_3"]:
                raise e
            else:
                print("expectedFailure", end="")
                self.__unittest_expecting_failure__ = True
                # raise unittest.expectedFailure


def diff(f1, f2):
    """
    Compares 2 text files and returns True if they are different.
    The default one in the package isn't doing the job for us: we want to
    ignore differences in the file ending.
    """
    fp1 = open(f1, "rt", encoding="utf-8")
    fp2 = open(f2, "rt", encoding="utf-8")
    while True:
        b1 = fp1.readline()
        b2 = fp2.readline()
        if b1.strip() != b2.strip():
            return True
        if not b1:
            return False


# If the file is processed as a script, run the test suite.
# Otherwise, only define the methods.
if __name__ == "__main__":
    runTestSuite()
