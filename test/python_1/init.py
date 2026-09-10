import csv


def read_csv_file():
    # This function reads a CSV-formatted file, creates an XML string and
    # then passes the string to ccAPPS for processing
    x = [
        '<?xml version="1.0" encoding="UTF-8" ?><plan xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">\n<items>'
    ]
    for row in csv.reader(open("items.csv", "rt")):
        x.append('<item name="%s" description="%s"/>' % (row[0], row[1]))
    x.append("</items>\n</plan>")
    ccAPPS.readXMLdata("\n".join(x), False, False)
    return


def read_csv_file_direct():
    # This function reads a CSV file and calls a function that accesses the
    # ccAPPS C++ API directly, without an intermediate XML format.
    for row in csv.reader(open("items.csv", "rt")):
        ccAPPS.item(name=row[0], description=row[1])
    return


def create_files(cnt):
    # This function writes out 2 data files: a first one is CSV-formatted, while
    # second one is XML-formatted
    csvout = open("items.csv", "w")
    xmlout = open("items.xml", "w")
    try:
        xmlout.write(
            '<?xml version="1.0" encoding="UTF-8" ?><plan xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">\n<items>\n'
        )
        for i in range(cnt):
            csvout.write("item%i,oper%i\n" % (i, i % 100))
            xmlout.write('<item name="item%i" description="oper%i"/>\n' % (i, i % 100))
        xmlout.write("</items>\n</plan>")
    finally:
        csvout.close()
        xmlout.close()
    return


def my_function(a):
    print("in my function with argument %s" % a, end="")
    return "OK"


print("0. Initialization ccAPPS version:", ccAPPS.version)
