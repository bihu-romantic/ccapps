from django.db import DEFAULT_DB_ALIAS


def getERPconnection(database=DEFAULT_DB_ALIAS):
    """
    Customize this method to connect to the ERP database.

    The example here uses Microsoft ADO to connect from a windows machine to a database.
    The site https://www.connectionstrings.com/ has a handy summary of the syntax of the
    connection strings.

    To improve configurability of the connector we can use either:
      a) Parameters stored in the ccAPPS parameter table.
         Benefit is that end user can then easily change these.
      b) Settings in the ccAPPS djangosettings file.
         For storing password and other security sensitive information this file is better.
    """
    import adodbapi

    connectionstring = "Provider=SQLNCLI11;Server=localhost;Database=acutec;User Id=acutec;Password=acutec;"
    return adodbapi.connect(connectionstring, timeout=600)
