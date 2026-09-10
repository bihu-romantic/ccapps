import asyncio
import os
import portend
import sys

from django.conf import settings
from django.db import DEFAULT_DB_ALIAS

from ccAPPSdb.common.auth import getWebserviceAuthorization
from ccAPPSdb.common.commands import PlanTaskRegistry
from ccAPPSdb.common.models import Parameter
from ccAPPSdb.common.utils import get_databases

# Only a single service can be making updates at the same time
try:
    lock = asyncio.Lock()
except Exception:
    lock = None

# Solvers reusable for all services
mrp_solver = None
clean_solver = None
fcst_solver = None


def useWebService(database=DEFAULT_DB_ALIAS):
    if "CCAPPS_TEST" in os.environ:
        # Tests run without the webservice by default
        return os.environ["CCAPPS_TEST"] == "webservice"
    else:
        param = Parameter.getValue("plan.webservice", database, "true")
        return param.lower() == "true"


def checkRunning(database=DEFAULT_DB_ALIAS, timeout=1.0):
    """
    Returns True if the web service is running.
    """
    if "CCAPPS_TEST" in os.environ:
        (host, port) = get_databases()[database]["TEST"]["CCAPPS_PORT"].split(":")
    else:
        (host, port) = get_databases()[database]["CCAPPS_PORT"].split(":")
    try:
        portend.free(host.replace("0.0.0.0", "localhost"), port, timeout=timeout)
        return False
    except Exception:
        return True


def waitTillRunning(database=DEFAULT_DB_ALIAS, timeout=180):
    """
    Raise an exception if the service isn't running within the specified time.
    """
    if "CCAPPS_TEST" in os.environ:
        (host, port) = get_databases()[database]["TEST"]["CCAPPS_PORT"].split(":")
    else:
        (host, port) = get_databases()[database]["CCAPPS_PORT"].split(":")
    try:
        portend.occupied(host.replace("0.0.0.0", "localhost"), port, timeout=timeout)
    except Exception:
        raise Exception("Web service not running within within %s seconds" % timeout)


def waitTillNotRunning(database=DEFAULT_DB_ALIAS, timeout=60):
    """
    Raise an exception if the web service isn't stopped within the specified time.
    """
    if "CCAPPS_TEST" in os.environ:
        (host, port) = get_databases()[database]["TEST"]["CCAPPS_PORT"].split(":")
    else:
        (host, port) = get_databases()[database]["CCAPPS_PORT"].split(":")
    try:
        portend.free(host.replace("0.0.0.0", "localhost"), port, timeout=timeout)
    except Exception:
        raise Exception("Web service not stopped within %s seconds" % timeout)


def getWebServiceContext(request):
    if "CCAPPS_TEST" in os.environ:
        port = get_databases()[request.database]["TEST"].get("CCAPPS_PORT", None)
    else:
        port = get_databases()[request.database].get("CCAPPS_PORT", None)
    proxied = get_databases()[request.database].get(
        "CCAPPS_PORT_PROXIED",
        not settings.DEBUG
        and not ("ccAPPSservice" in sys.argv or "runserver" in sys.argv)
        and "CCAPPS_TEST" not in os.environ,
    )
    if "runserver" in sys.argv and not proxied:
        port = request.get_host()
    elif port and not proxied:
        port = port.replace("0.0.0.0", "localhost")
    return {
        "token": getWebserviceAuthorization(
            user=request.user.username, sid=request.user.id, exp=3600
        ),
        "port": port,
        "proxied": proxied,
    }


def createSolvers(loglevel=2, database=DEFAULT_DB_ALIAS):
    import ccAPPS

    global clean_solver, mrp_solver, fcst_solver

    try:
        from ccAPPSdb.execute.management.commands.runplan import parseConstraints

        constraint = parseConstraints(os.environ["CCAPPS_CONSTRAINT"])
    except Exception:
        constraint = 4 + 16 + 32  # Default is with all constraints enabled
    clean_solver = ccAPPS.solver_delete(loglevel=loglevel, constraint=constraint)
    from ccAPPSdb.common.models import Parameter
    solver_mode = Parameter.getValue("plan.solver", database, "aco").lower()
    use_aco = solver_mode not in ("heuristic", "mrp")
    mrp_solver = ccAPPS.solverACO(
        loglevel=loglevel,
        constraints=constraint,
        erasePreviousFirst=False,
        plantype=1,
        lazydelay=int(Parameter.getValue("lazydelay", database, "86400")),
        minimumdelay=int(Parameter.getValue("plan.minimumdelay", database, "3600")),
        rotateresources=(
            Parameter.getValue("plan.rotateResources", database, "true").lower()
            == "true"
        ),
        iterationmax=int(Parameter.getValue("plan.iterationmax", database, "0")),
    )
    supplyplanningtask = PlanTaskRegistry.getTask(sequence=200)
    if supplyplanningtask:
        if hasattr(supplyplanningtask, "debugResource"):
            mrp_solver.userexit_resource = supplyplanningtask.debugResource
        if hasattr(supplyplanningtask, "debugDemand"):
            mrp_solver.userexit_demand = supplyplanningtask.debugDemand
        if hasattr(supplyplanningtask, "debugOperation"):
            mrp_solver.userexit_operation = supplyplanningtask.debugOperation

    if "ccAPPSdb.forecast" in settings.INSTALLED_APPS:
        from ccAPPSdb.forecast.commands import createForecastSolver

        fcst_solver = createForecastSolver(database)
        if fcst_solver:
            fcst_solver.loglevel = loglevel
