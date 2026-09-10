#!/usr/bin/env python3
"""
This test shows how we can use Python to create a ccAPPS model: we can
create objects, access existing objects and change objects.

All other tests are running the Python interpreter embedded in the ccAPPS
executable.
"""

# Add the ccAPPS directory to the Python module search path
import os
import site

if "ccAPPS_HOME" in os.environ:
    site.addsitedir(os.environ["ccAPPS_HOME"])

import ccAPPS
import datetime
import inspect
import types


def printModel(filename):
    """
    A function that prints out all models to a file.
    """
    # Open the output file
    with open(filename, "wt", encoding="utf-8") as output:
        # Global settings
        print("Echoing global settings", file=output)
        print("Plan name:", ccAPPS.settings.name, file=output)
        print("Plan description:", ccAPPS.settings.description, file=output)
        print("Plan current:", ccAPPS.settings.current, file=output)

        # Calendars
        print("\nEchoing calendars:", file=output)
        for b in ccAPPS.calendars():
            print("  Calendar:", b.name, getattr(b, "default", None), file=output)
            for j in b.buckets:
                print(
                    "    Bucket:",
                    getattr(j, "value", None),
                    j.start,
                    j.end,
                    j.priority,
                    file=output,
                )

        # Customers
        print("\nEchoing customers:", file=output)
        for b in ccAPPS.customers():
            print(
                "  Customer:",
                b.name,
                b.description,
                b.category,
                b.subcategory,
                b.owner,
                file=output,
            )

        # Locations
        print("\nEchoing locations:", file=output)
        for b in ccAPPS.locations():
            print(
                "  Location:",
                b.name,
                b.description,
                b.category,
                b.subcategory,
                b.owner,
                file=output,
            )

        # Items
        print("\nEchoing items:", file=output)
        for b in ccAPPS.items():
            print(
                "  Item:",
                b.name,
                b.description,
                b.category,
                b.subcategory,
                b.owner,
                file=output,
            )

        # Resources
        print("\nEchoing resources:", file=output)
        for b in ccAPPS.resources():
            print(
                "  Resource:",
                b.name,
                b.description,
                b.category,
                b.subcategory,
                b.owner,
                file=output,
            )
            for l in b.loads:
                print(
                    "    Load:",
                    l.operation.name,
                    l.quantity,
                    l.effective_start,
                    l.effective_end,
                    file=output,
                )
            for l in b.loadplans:
                print(
                    "    Loadplan:",
                    l.operationplan.reference,
                    l.operationplan.operation.name,
                    l.quantity,
                    l.startdate,
                    l.enddate,
                    file=output,
                )

        # Buffers
        print("\nEchoing buffers:", file=output)
        for b in ccAPPS.buffers():
            print(
                "  Buffer:",
                b.name,
                b.description,
                b.category,
                b.subcategory,
                b.owner,
                file=output,
            )
            for l in b.flows:
                print(
                    "    Flow:",
                    l.operation.name,
                    l.quantity,
                    l.effective_start,
                    l.effective_end,
                    file=output,
                )
            for l in b.flowplans:
                print(
                    "    Flowplan:",
                    l.operationplan.reference,
                    l.operationplan.operation.name,
                    l.quantity,
                    l.date,
                    file=output,
                )

        # Operations
        print("\nEchoing operations:", file=output)
        for b in ccAPPS.operations():
            print(
                "  Operation:",
                b.name,
                b.description,
                b.category,
                b.subcategory,
                file=output,
            )
            for l in b.loads:
                print(
                    "    Load:",
                    l.resource.name,
                    l.quantity,
                    l.effective_start,
                    l.effective_end,
                    file=output,
                )
            for l in b.flows:
                print(
                    "    Flow:",
                    l.buffer.name,
                    l.quantity,
                    l.effective_start,
                    l.effective_end,
                    file=output,
                )
            if isinstance(
                b,
                (
                    ccAPPS.operation_alternate,
                    ccAPPS.operation_routing,
                    ccAPPS.operation_split,
                ),
            ):
                for l in b.suboperations:
                    print(
                        "    Suboperation:",
                        l.operation.name,
                        l.priority,
                        l.effective_start,
                        l.effective_end,
                        file=output,
                    )

        # Demands
        print("\nEchoing demands:", file=output)
        for b in ccAPPS.demands():
            print("  Demand:", b.name, b.due, b.item.name, b.quantity, file=output)
            for i in b.operationplans:
                print(
                    "    Operationplan:",
                    i.id,
                    i.operation.name,
                    i.quantity,
                    i.end,
                    file=output,
                )

        # Operationplans
        print("\nEchoing operationplans:", file=output)
        for b in ccAPPS.operationplans():
            print(
                "  Operationplan:",
                b.operation.name,
                b.quantity,
                b.start,
                b.end,
                file=output,
            )
            for s in b.operationplans:
                print(
                    "       ", s.operation.name, s.quantity, s.start, s.end, file=output
                )

        # Problems
        print("\nPrinting problems", file=output)
        for i in ccAPPS.problems():
            print(
                "  Problem:",
                i.entity,
                i.name,
                i.description,
                i.start,
                i.end,
                file=output,
            )


def printExtensions():
    """
    Echoes all entities in our extension module.
    Useful to create documentation.
    """
    print("  Types:")
    for name, o in inspect.getmembers(ccAPPS):
        if not inspect.isclass(o) or issubclass(o, Exception) or hasattr(o, "__iter__"):
            continue
        print("    %s: %s" % (o.__name__, inspect.getdoc(o)))
    print("  Methods:")
    for name, o in inspect.getmembers(ccAPPS):
        if not inspect.isroutine(o):
            continue
        print("    %s: %s" % (o.__name__, inspect.getdoc(o)))
    print("  Exceptions:")
    for name, o in inspect.getmembers(ccAPPS):
        if not inspect.isclass(o) or not issubclass(o, Exception):
            continue
        print("    %s" % (o.__name__))
    print("  Iterators:")
    for name, o in inspect.getmembers(ccAPPS):
        if not inspect.isclass(o) or not hasattr(o, "__iter__"):
            continue
        print("    %s: %s" % (o.__name__, inspect.getdoc(o)))
    print("  Other:")
    for name, o in inspect.getmembers(ccAPPS):
        # Negating the exact same filters as in the previous blocks
        if not (
            not inspect.isclass(o) or issubclass(o, Exception) or hasattr(o, "__iter__")
        ):
            continue
        if inspect.isroutine(o):
            continue
        if not (not inspect.isclass(o) or not issubclass(o, Exception)):
            continue
        if not (not inspect.isclass(o) or not hasattr(o, "__iter__")):
            continue
        print("    %s: %s" % (name, o))


###
print("\nUpdating global settings")
ccAPPS.settings.name = "demo model"
ccAPPS.settings.description = (
    "unicode А Б В Г Д Е Ё Ж З И Й К Л М Н О П Р С Т У Ф Х Ц Ч Ш Щ Ъ Ы Ь"
)
ccAPPS.settings.current = datetime.datetime(2009, 1, 1)

###
print("\nCreating operations")
shipoper = ccAPPS.operation_fixed_time(name="delivery end item", duration=86400)
choice = ccAPPS.operation_alternate(name="make or buy item")
makeoper = ccAPPS.operation_routing(name="make item")
ccAPPS.suboperation(
    owner=makeoper,
    operation=ccAPPS.operation_fixed_time(
        name="make item - step 1", duration=4 * 86400
    ),
    priority=1,
)
ccAPPS.suboperation(
    owner=makeoper,
    operation=ccAPPS.operation_fixed_time(
        name="make item - step 2", duration=3 * 86400
    ),
    priority=2,
)
buyoper = ccAPPS.operation_fixed_time(name="buy item", duration=86400)
ccAPPS.suboperation(owner=choice, operation=makeoper, priority=1)
ccAPPS.suboperation(owner=choice, operation=buyoper, priority=2)

###
print("\nCreating calendars")
c = ccAPPS.calendar(name="Cal1", default=4.56)
c.setValue(datetime.datetime(2009, 1, 1), datetime.datetime(2009, 3, 1), 1)
c.setValue(datetime.datetime(2009, 2, 1), datetime.datetime(2009, 5, 1), 2)
c.setValue(datetime.datetime(2009, 2, 1), datetime.datetime(2009, 3, 1), 3)
ccAPPS.calendar(name="Cal2", default=1.23)
ccAPPS.calendar(name="Cal3", default=1.23)

###
print("\nTesting the calendar iterator")
print("calendar events:")
for date, value in c.events():
    print("  ", date, value)

###
print("\nDeleting a calendar")
ccAPPS.calendar(name="Cal3", action="R")

# Load some XML data
ccAPPS.readXMLdata(
    """<?xml version="1.0" encoding="UTF-8" ?>
<plan xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <resources>
    <resource name="Resource">
      <maximum_calendar name="Capacity">
        <buckets>
          <bucket start="2009-01-01T00:00:00">
            <value>1</value>
          </bucket>
        </buckets>
      </maximum_calendar>
      <loads>
        <load>
          <operation name="make item - step 1" />
        </load>
        <load>
          <operation name="make item - step 2" />
        </load>
      </loads>
    </resource>
  </resources>
  <flows>
    <flow xsi:type="flow_start">
      <operation name="delivery end item" />
      <buffer name="end item" />
      <quantity>-1</quantity>
    </flow>
    <flow xsi:type="flow_end">
      <operation name="make item" />
      <buffer name="end item" />
      <quantity>1</quantity>
    </flow>
    <flow xsi:type="flow_end">
      <operation name="buy item" />
      <buffer name="end item" />
      <quantity>1</quantity>
    </flow>
  </flows>
</plan>
"""
)

###
print("\nCreating operationplans")
opplan = ccAPPS.operationplan(
    operation=ccAPPS.operation(name="make item"),
    quantity=9,
    end=datetime.datetime(2011, 1, 1),
)
opplan.status = "confirmed"

###
print("\nCreating items")
item = ccAPPS.item(name="end item")
itemlist = [ccAPPS.item(name="item %d" % i) for i in range(10)]

###
print("\nTesting the comparison operator")
print("makeoper < shipoper", makeoper < shipoper)
print("shipoper < makeoper", shipoper < makeoper)
print("shipoper != makeoper", shipoper != makeoper)
print("shipoper == makeoper", shipoper == makeoper)
print("shipoper == shipoper", shipoper == shipoper)
try:
    print("makeoper == item", makeoper == item)
except Exception as e:
    print("Catching exception %s: %s" % (e.__class__.__name__, e))

###
print("\nCreating a resource")
ccAPPS.resource(name="machine", maximum_calendar=ccAPPS.calendar(name="Cal2"))

###
print("\nCreating customers")
mycustomer = ccAPPS.customer(name="client")

###
print("\nCreating locations")
locA = ccAPPS.location(name="locA")
locB = ccAPPS.location(name="locB")

###
print("\nCreating some buffers")

buf = ccAPPS.buffer(name="end item", producing=choice, item=item)

buf1 = ccAPPS.buffer(
    name="buffer1",
    description="My description",
    category="My category",
    location=locA,
    item=itemlist[1],
)
print(
    buf1,
    buf1.__class__,
    buf1.location,
    isinstance(buf1, ccAPPS.buffer),
    isinstance(buf1, ccAPPS.buffer_default),
    isinstance(buf1, ccAPPS.buffer_infinite),
)

buf2 = ccAPPS.buffer(name="buffer2", owner=buf1)
print(
    buf2,
    buf2.__class__,
    buf2.location,
    isinstance(buf2, ccAPPS.buffer),
    isinstance(buf2, ccAPPS.buffer_default),
    isinstance(buf2, ccAPPS.buffer_infinite),
)

###
print("\nCatching some exceptions")
try:
    print(buf1.myfield)
except Exception as e:
    print("Catching exception %s: %s" % (e.__class__.__name__, e))

try:
    buf1.myfield = "my custom field"
except Exception as e:
    print("Catching exception %s: %s" % (e.__class__.__name__, e))

try:
    buf1.owner = buf2
except Exception as e:
    print("Catching exception %s: %s" % (e.__class__.__name__, e))

###
print("\nCreating demands")
order1 = ccAPPS.demand(
    name="order 1",
    item=item,
    quantity=10,
    priority=1,
    due=datetime.datetime(2009, 3, 2, 9),
    customer=mycustomer,
    maxlateness=0,
    operation=shipoper,
)
order2 = ccAPPS.demand(
    name="order 2",
    item=item,
    quantity=10,
    priority=2,
    due=datetime.datetime(2009, 3, 2, 8, 30, 0),
    customer=mycustomer,
    maxlateness=0,
    operation=shipoper,
)
order3 = ccAPPS.demand(
    name="order 3",
    item=item,
    quantity=10,
    priority=3,
    due=datetime.datetime(2009, 3, 2, 20, 0, 0),
    customer=mycustomer,
    maxlateness=0,
    operation=shipoper,
)

###
print("\nCreating a solver and running it")
ccAPPS.solver_mrp(constraints=7, loglevel=0).solve()

###
print("\nEchoing the model to a file")
printModel("output.1.xml")

###
print("\nSaving the model to an XML-file")
ccAPPS.saveXMLfile("output.2.xml")

###
print("\nPrinting some models in XML format")
print(mycustomer.toXML())
print(locA.toXML())
print(opplan.toXML())
print(item.toXML())
print(order1.toXML())
print(buf1.toXML())
print(buf2.toXML())
print(makeoper.toXML())
for i in ccAPPS.problems():
    print(i.toXML())

###
print("\nPrinting some models in XML format to a file")
with open("output.3.xml", "wt") as output:
    mycustomer.toXML("P", output)
    locA.toXML("P", output)
    opplan.toXML("P", output)
    item.toXML("P", output)
    order1.toXML("P", output)
    buf1.toXML("P", output)
    makeoper.toXML("P", output)
    for i in ccAPPS.problems():
        i.toXML("P", output)

###
print("\nDocumenting all available Python entities defined by ccAPPS:")
printExtensions()

###
print("\nPrinting memory consumption estimate:")
ccAPPS.printsize()

### Verifying PO python API
itm1 = ccAPPS.item(name="itm1")
itm2 = ccAPPS.item(name="itm2")
loc1 = ccAPPS.location(name="loc1")
loc2 = ccAPPS.location(name="loc2")
loc3 = ccAPPS.location(name="loc3")
sup1 = ccAPPS.supplier(name="sup1")
sup2 = ccAPPS.supplier(name="sup2")
oper1 = ccAPPS.operation_fixed_time(
    name="make itm1 @ loc2 ",
    duration=86400,
    location=loc2,
    item=itm1,
)
oper2 = ccAPPS.operation_time_per(
    name="make itm1 @ loc1",
    duration=86400,
    duration_per=86400,
    location=loc1,
    item=itm1,
)

replenishments = [
    ccAPPS.itemsupplier(item=itm1, location=loc1, supplier=sup1, leadtime=4 * 86400),
    ccAPPS.itemsupplier(item=itm1, location=loc2, supplier=sup1, leadtime=5 * 86400),
    ccAPPS.itemsupplier(item=itm1, location=loc2, supplier=sup2, leadtime=6 * 86400),
    ccAPPS.itemsupplier(item=itm2, location=loc2, supplier=sup2, leadtime=7 * 86400),
    ccAPPS.itemdistribution(item=itm1, origin=loc2, location=loc1, leadtime=7 * 86400),
    ccAPPS.itemdistribution(item=itm1, origin=loc3, location=loc1, leadtime=3 * 86400),
]

po = ccAPPS.operationplan(
    ordertype="PO",
    reference="PO#1",
    end=datetime.date(2024, 2, 1),
    quantity=1,
    location=loc1,
    item=itm1,
    supplier=sup1,
)

mo = ccAPPS.operationplan(
    reference="MO#1",
    end=datetime.date(2024, 2, 1),
    quantity=1,
    operation=oper1,
)

do = ccAPPS.operationplan(
    ordertype="DO",
    reference="DO#1",
    end=datetime.date(2024, 2, 1),
    quantity=1,
    item=itm1,
    location=loc1,
    origin=loc2,
)

with open("output.4.xml", "wt") as output:

    def printPO():
        print(
            po.reference,
            po.item.name,
            po.location.name,
            po.supplier.name,
            po.quantity,
            po.start,
            po.end,
            po.status,
            file=output,
        )

    def printMO():
        print(
            mo.reference,
            mo.operation.name,
            mo.quantity,
            mo.start,
            mo.end,
            mo.status,
            file=output,
        )

    def printDO():
        print(
            do.reference,
            do.item.name,
            do.origin.name,
            do.location.name,
            do.quantity,
            do.start,
            do.end,
            do.status,
            file=output,
        )

    print("\nUpdating purchase order")
    printPO()
    po.location = loc2
    printPO()
    po.supplier = sup2
    printPO()
    po.item = itm2
    printPO()
    po.supplier = sup1
    printPO()

    print("\nUpdating manufacturing order")
    printMO()
    mo.operation = oper2
    printMO()

    print("\nUpdating distribution order")
    printDO()
    do.origin = loc3
    printDO()
    do.location = loc2
    printDO()
    do.item = itm2
    printDO()

mo = po = None

# Test the API to manipulate fences
print("Update fence to 5 days")
oper1.fence = datetime.timedelta(days=5)
print("fence duration: ", oper1.fence)
print("fence fence date: ", oper1.getFence())
if oper1.fence != 5 * 3600 * 24 or oper1.getFence() != datetime.datetime(2009, 1, 6):
    raise AssertionError("Wrong fence")
d = ccAPPS.settings.current + datetime.timedelta(days=7)
print("Update fence to %s" % d)
oper1.setFence(d)
print("fence duration: ", oper1.fence)
print("fence fence date: ", oper1.getFence())
if oper1.fence != 7 * 3600 * 24 or oper1.getFence() != datetime.datetime(2009, 1, 8):
    raise AssertionError("Wrong fence")
