#!/usr/bin/env python3
r"""
This command is the wrapper for all administrative actions on ccAPPS.
"""

import os
import sys
import site

if __name__ == "__main__":
    try:
        # Autodetect Python virtual enviroment
        venv = os.environ.get("VIRTUAL_ENV", None)
        if not venv:
            curdir = os.path.dirname(os.path.realpath(__file__))
            for candidate in (
                # Development layout
                os.path.join(curdir, "venv"),
                # Linux install layout
                os.path.join(curdir, "..", "share", "ccAPPS", "venv"),
            ):
                if os.path.isfile(
                    os.path.join(candidate, "bin", "python3")
                ) and os.path.isfile(os.path.join(candidate, "bin", "activate")):
                    os.environ["VIRTUAL_ENV"] = candidate
                    venv = candidate
                    break

        # Activate Python virtual environment
        if venv:
            prev_length = len(sys.path)
            os.environ["PATH"] = os.pathsep.join(
                [os.path.join(venv, "bin")]
                + os.environ.get("PATH", "").split(os.pathsep)
            )
            path = os.path.realpath(
                os.path.join(
                    venv,
                    "lib",
                    "python%d.%d" % sys.version_info[:2],
                    "site-packages",
                )
            )
            site.addsitedir(path)
            sys.path[:] = sys.path[prev_length:] + sys.path[0:prev_length]
            sys.real_prefix = sys.prefix
            sys.prefix = venv

        # Initialize django
        os.environ.setdefault("DJANGO_SETTINGS_MODULE", "ccAPPSdb.settings")
        import django

        django.setup()

        # Synchronize the scenario table with the settings
        from ccAPPSdb.common.models import Scenario

        Scenario.syncWithSettings()

        if "version" in sys.argv or "--version" in sys.argv:
            # Display the version and stop
            import ccAPPSdb

            sys.stdout.write(ccAPPSdb.__version__ + "\n")
        else:
            # Run the command
            from django.core.management import execute_from_command_line

            execute_from_command_line(sys.argv)

    except KeyboardInterrupt:
        print("\nInterrupted with Ctrl-C")
        sys.exit(1)
