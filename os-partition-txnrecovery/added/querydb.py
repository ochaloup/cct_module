#!/bin/python
"""
Copyright 2018 Red Hat, Inc.

Red Hat licenses this file to you under the Apache License, version
2.0 (the "License"); you may not use this file except in compliance
with the License.  You may obtain a copy of the License at

http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
implied.  See the License for the specific language governing
permissions and limitations under the License.
"""

import argparse
import dbms
import logging
from enum import Enum


class DatabaseType(Enum):
    """
    What is database type we work with
    POSTGRESQL: postgresql database
    MYSQL: mysql database
    """

    POSTGRESQL = 'postgresql'
    MYSQL = 'mysql'

    def __str__(self):
        return self.value

class CommandType(Enum):
    """
    What is command to be run on database
    CREATE_SCHEMA: create db tables, if they do not exist otherwise ignored
    INSERT: insert the recovery marker record
    DELETE: delete recovery marker record
    SELECT_RECOVERY: select recovery pod names
    SELECT_APPLICATION: select application pod names
    """

    CREATE_SCHEMA = 'create'
    INSERT = 'insert'
    DELETE = 'delete'
    SELECT_RECOVERY = 'select_recovery'
    SELECT_APPLICATION = 'select_application'

    def __str__(self):
        return self.value

class OutputFormat(Enum):
    """
    Represents output format of this script.
    RAW: no formatting
    LIST_SPACE: if possible values are delimited with space and returned
    LIST_COMMA: comma separated list
    """

    RAW = "raw"
    LIST_SPACE = "list_space"
    LIST_COMMA = "list_comma"

    def __str__(self):
        return self.value

class DatabaseWorker():
    """
    Providing database commands to manage recovery markers records
    """

    DBType = None
    TableName = None
    Connection = None
    Cursor = None

    ApplicationPodNameColumnName = 'APPLICATION_POD_NAME'
    RecoveryPodNameColumnName = 'RECOVERY_POD_NAME'

    def __init__(self, databaseType, host, port, databaseName, userName, password, tableName):
        if(databaseType == DatabaseType.POSTGRESQL):
            if port is None: port = 5432
            self.Connection = dbms.connect.postgres(userName, password, databaseName, host, port)
        elif(databaseType == DatabaseType.MYSQL):
            if port is None: port = 3306
            self.Connection = dbms.connect.mysql(userName, password, databaseName, host, port)
        else:
            logger.critical("Database type '%s' is not supported", databaseType)
            exit(1)
        self.DBType = databaseName
        self.TableName = tableName
        self.Cursor = self.Connection.cursor()

    def __str__(self):
        return str(self.__dict__)

    def createSchema(self):
        self.Cursor.execute('CREATE TABLE %s (%s varchar(255), %s varchar(255))'
            % (self.TableName, self.ApplicationPodNameColumnName, self.RecoveryPodNameColumnName))
        self.Connection.commit()

    def insertRecord(self, applicationPodName, recoveryPodName):
        self.Cursor.execute("INSERT INTO %s (%s, %s) VALUES ('%s', '%s')"
            % (self.TableName, self.ApplicationPodNameColumnName, self.RecoveryPodNameColumnName, applicationPodName, recoveryPodName))
        self.Connection.commit()

    def deleteRecord(self, applicationPodName, recoveryPodName):
        whereClause = self._getWhereClause(applicationPodName, recoveryPodName)
        self.Cursor.execute("DELETE FROM %s%s" % (self.TableName, whereClause))
        self.Connection.commit()

    def selectRecord(self, applicationPodName, recoveryPodName):
        whereClause = self._getWhereClause(applicationPodName, recoveryPodName)
        self.Cursor.execute("SELECT * FROM %s%s" % (self.TableName, whereClause))
        return self.Cursor

    def _getWhereClause(self, applicationPodName, recoveryPodName):
        whereClause = ''
        if applicationPodName is not None:
            whereClause += "%s = '%s'" % (self.ApplicationPodNameColumnName, applicationPodName)
        if recoveryPodName is not None:
            if whereClause != '': whereClause += ' AND '
            whereClause += "%s = '%s'" % (self.RecoveryPodNameColumnName, recoveryPodName)
        return whereClause if whereClause == '' else ' WHERE ' + whereClause


def requireArgument(value, commandName, argumentName):
    if value is None and str(value):
        logger.critical("Required argument '%s' for the command '%s' was not specified", argumentName, commandName)
        exit(1)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description = "Commandline script to create and store txn recovery markers in database")
    parser.add_argument("-y", "--type_db", required = False, type = DatabaseType, default = DatabaseType.POSTGRESQL, choices=list(DatabaseType),
      help = "Database type the script will be working with")
    parser.add_argument("-o", "--host", required=False, type=str, default='localhost', help="Hostname where the database runs")
    parser.add_argument("-p", "--port", required=False, type=int, help="Port where the database runs")
    parser.add_argument("-d", "--database", required=True, type=str, default=None, help="Databese name to connect to at the host and port")
    parser.add_argument("-u", "--user", required=True, type=str, default=None, help="Username at the database to connect to")
    parser.add_argument("-s", "--password", required=True, type=str, default=None, help="Password for the username at the database to connect to")

    parser.add_argument("-t", "--table_name", required = False, type = str, default = 'JDBC_RECOVERY', help = "Table name to be working with")
    parser.add_argument("-c", "--command", required = False, type = CommandType, default = CommandType.SELECT_RECOVERY, choices=list(CommandType),
      help = "Command to run in database,\navailable options are to create db schema, to insert a record, to delete the record and list recovery pod names")
    parser.add_argument("-a", "--application_pod_name", required = False, type = str, default = None, help = "Application pod name which\n"
      + " will be either inserted/deleted onto database or by which query will be filtered")
    parser.add_argument("-r", "--recovery_pod_name", required = False, type = str, default = None, help = "Recovery pod name which\n"
      + " will be either inserted/deleted onto database or by which query will be filtered")
    parser.add_argument("-f", "--format", required = False, type = OutputFormat, default = OutputFormat.LIST_SPACE, choices=list(OutputFormat), help = "Output format")
    parser.add_argument("-l", "--loglevel", default="CRITICAL", help="Log level",
        choices=["debug", "DEBUG", "info", "INFO", "warning", "WARNING", "error", "ERROR", "critical", "CRITICAL"])
    parser.add_argument("args", nargs = argparse.REMAINDER, help = "Arguments of the query (each query type has different)")

    args = parser.parse_args()

    # don't spam warnings (e.g. when not verifying ssl connections)
    logging.captureWarnings(True)
    logging.basicConfig(level = args.loglevel.upper())
    logger = logging.getLogger(__name__)

    logger.debug("Changing database based on arguments: %s", args)

    resultsList = []

    db = DatabaseWorker(args.type_db, args.host, args.port, args.database, args.user, args.password, args.table_name)
    if args.command == CommandType.CREATE_SCHEMA:
        logger.debug("Creating database table '%s', at: '%s'", args.table_name, db)
        db.createSchema()
    elif args.command == CommandType.INSERT:
        requireArgument(args.application_pod_name, args.command, 'application_pod_name')
        requireArgument(args.recovery_pod_name, args.command, 'recovery_pod_name')
        logger.debug("Inserting a record [%s, %s],  at: '%s'", args.application_pod_name, args.recovery_pod_name, db)
        db.insertRecord(args.application_pod_name, args.recovery_pod_name)
    elif args.command == CommandType.DELETE:
        logger.debug("Deleting record [%s, %s],  at: '%s'", args.application_pod_name, args.recovery_pod_name, db)
        db.deleteRecord(args.application_pod_name, args.recovery_pod_name)
    elif 'select' in str(args.command):
        logger.debug("Selecting recovery pod names filtered on [%s, %s],  at: '%s'", args.application_pod_name, args.recovery_pod_name, db)
        cursor = db.selectRecord(args.application_pod_name, args.recovery_pod_name)
        rowIndex = 0 if args.command == CommandType.SELECT_APPLICATION else  1
        for results in cursor.fetchall():
            resultsList.append(results[rowIndex])
    else:
        logger.critical('No handler for command %s', args.command)
        exit(1)

    if resultsList: # result list is not empty
        if args.format == OutputFormat.LIST_SPACE:
            print(' '.join(resultsList))
        elif args.format == OutputFormat.LIST_COMMA:
            print(','.join(resultsList))
        else: # RAW format
            print(resultsList,)
