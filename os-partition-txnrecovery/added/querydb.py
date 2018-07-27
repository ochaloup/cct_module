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
    GET_RECOVERY_POD_NAMES: return names of recovery pods
    """

    CREATE_SCHEMA = 'create'
    INSERT = 'insert'
    DELETE = 'delete'
    GET_RECOVERY_POD_NAMES = 'get_recovery_pod_names'

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

    def __init__(self, databaseType, host, port, databaseName, userName, password, tableName):
        if(databaseType == DatabaseType.POSTGRESQL):
            self.Connection = dbms.connect.postgres(userName, password, databaseName, host, port)
        elif(databaseType == DatabaseType.MYSQL):
            self.Connection = dbms.connect.mysql(userName, password, databaseName, host, port)
        else:
            logger.critical("Database type '%s' is not supported", databaseType)
            exit(1)
        self.DBType = databaseName
        self.TableName = tableName
        self.Cursor = self.Connection.cursor()

    def createSchema(self):
        print(self.TableName)
        self.Cursor.execute('CREATE TABLE %s (APPLICATION_POD_NAME varchar(255), RECOVERY_POD_NAME varchar(255));' % self.TableName)



# todo:
# ${JDBC_RECOVERY_MARKER_COMMAND} delete ${applicationPodName} ${recoveryPodName}
# ${JDBC_RECOVERY_MARKER_COMMAND} create ${applicationPodName} ${recoveryPodName}
# ${JDBC_RECOVERY_MARKER_COMMAND} get_by_application ${POD_NAME}
# ${JDBC_RECOVERY_MARKER_COMMAND} delete_by_recovery ${recoveryPod}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description = "Commandline script to create and store txn recovery markers in database")
    parser.add_argument("-y", "--type_db", required = False, type = DatabaseType, default = DatabaseType.POSTGRESQL, choices=list(DatabaseType),
      help = "Database type the script will be working with")
    parser.add_argument("-o", "--host", required=False, type=str, default='localhost', help="Hostname where the database runs")
    parser.add_argument("-p", "--port", required=False, type=int, default=5432, help="Port where the database runs")
    parser.add_argument("-d", "--database", required=True, type=str, default=None, help="Databese name to connect to at the host and port")
    parser.add_argument("-u", "--user", required=True, type=str, default=None, help="Username at the database to connect to")
    parser.add_argument("-s", "--password", required=True, type=str, default=None, help="Password for the username at the database to connect to")

    parser.add_argument("-t", "--table_name", required = False, type = str, default = 'JDBC_RECOVERY', help = "Table name to be working with")
    parser.add_argument("-c", "--command", required = False, type = CommandType, default = CommandType.GET_RECOVERY_POD_NAMES, choices=list(CommandType),
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

    logger.debug("Database command is going to be executed with arguments: %s", args)

    db = DatabaseWorker(args.type_db, args.host, args.port, args.database, args.user, args.password, args.table_name)
    if args.command == CommandType.CREATE_SCHEMA:
        db.createSchema()
    else:
        logger.critical('No handler for command %s', args.command)
        exit(1)

    # if args.format == OutputFormat.LIST_SPACE:
    #     print ' '.join(queryResult)
    # elif args.format == OutputFormat.LIST_COMMA:
    #     print ','.join(queryResult)
    # else: # RAW format
    #     print queryResult,

    exit(0)



