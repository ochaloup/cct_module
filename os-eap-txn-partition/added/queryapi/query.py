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
import json
import logging
import urllib2

from enum import Enum


class QueryType(Enum):
    """
    Represents what could be queried.
    PODS: list of pods
    PODS_LIVING: list of living pods
    LOG: log from particular pod
    CM_KEYS: list of config map keys
    CM_STORE: store value as part of the config map
    """

    PODS = 'pods'
    PODS_LIVING = 'pods_living'
    LOG = 'log'
    CM_KEYS = 'cm_keys'
    CM_STORE = 'cm_store'
    CM_REMOVE = 'cm_remove'

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


class OpenShiftQuery():
    """
    Utility class to help query OpenShift api. Declares constant
    to get token and uri of the query. Having methods doing the query etc.
    """

    API_URL = 'https://openshift.default.svc'
    TOKEN_FILE_PATH = '/var/run/secrets/kubernetes.io/serviceaccount/token'
    NAMESPACE_FILE_PATH = '/var/run/secrets/kubernetes.io/serviceaccount/namespace'
    CERT_FILE_PATH = '/var/run/secrets/kubernetes.io/serviceaccount/ca.crt'
    STATUS_LIVING_PODS = ['Pending', 'Running', 'Unknown']

    @staticmethod
    def __readFile(fileToRead):
        with open(fileToRead, 'r') as readingfile:
            return readingfile.read().strip()

    @staticmethod
    def getToken():
        return OpenShiftQuery.__readFile(OpenShiftQuery.TOKEN_FILE_PATH)

    @staticmethod
    def getNameSpace():
        return OpenShiftQuery.__readFile(OpenShiftQuery.NAMESPACE_FILE_PATH)

    @staticmethod
    def queryApi(urlSuffix, isPretty = False):
        prettyPrintParam = '?pretty=true' if isPretty else ''
        request = urllib2.Request(OpenShiftQuery.API_URL + urlSuffix + prettyPrintParam,
            headers = {'Authorization': 'Bearer ' + OpenShiftQuery.getToken(), 'Accept': 'application/json'})
        logger.debug('query for: "%s"', request.get_full_url())
        try:
            return urllib2.urlopen(request, cafile = OpenShiftQuery.CERT_FILE_PATH).read()
        except:
            logger.critical('Cannot query OpenShift API for "%s"', request.get_full_url())
            raise

    @staticmethod
    def patchApi(urlSuffix, contentType, jsonDataToSend):
        request = urllib2.Request(OpenShiftQuery.API_URL + urlSuffix + '?pretty=true',
            headers = {'Authorization': 'Bearer ' + OpenShiftQuery.getToken(),
                       'Accept': 'application/json',
                       'Content-Type': contentType},
            data = jsonDataToSend)
        request.get_method = lambda: 'PATCH'
        logger.debug('query for: "%s"', request.get_full_url())
        try:
            return urllib2.urlopen(request, cafile = OpenShiftQuery.CERT_FILE_PATH).read()
        except:
            logger.critical('Cannot call PATCH to OpenShift API for "%s"', request.get_full_url())
            raise



def getPodsJsonData():
    jsonText = OpenShiftQuery.queryApi('/api/v1/namespaces/{}/pods'.format(OpenShiftQuery.getNameSpace()))
    return json.loads(jsonText)

def getPods():
    jsonPodsData = getPodsJsonData()
    pods = []
    for pod in jsonPodsData["items"]:
        logger.debug('query pod %s of status %s', pod["metadata"]["name"], pod["status"]["phase"])
        pods.append(pod["metadata"]["name"])
    return pods

def getLivingPods():
    jsonPodsData = getPodsJsonData()

    pods = []
    for pod in jsonPodsData["items"]:
        logger.debug('query pod %s of status %s', pod["metadata"]["name"], pod["status"]["phase"])
        if pod["status"]["phase"] in OpenShiftQuery.STATUS_LIVING_PODS:
            pods.append(pod["metadata"]["name"])
    return pods

def getLog(podName):
    jsonText = OpenShiftQuery.queryApi('/api/v1/namespaces/{}/pods/{}/log'
            .format(OpenShiftQuery.getNameSpace(), podName))
    return jsonText

def getConfigMapData(configMapName):
    namespace = OpenShiftQuery.getNameSpace()
    jsonText = OpenShiftQuery.queryApi('/api/v1/namespaces/{}/configmaps/{}'.format(namespace, configMapName), True)
    logger.debug('querying the config map %s at namespace %s got output %s', configMapName, namespace, jsonText)
    jsonConfigMapData = json.loads(jsonText)
    return jsonConfigMapData.get('data', dict()) # no data then empty dictionary

def storeConfigMap(configMapName, recordToStore):
    jsonDataToStore = json.dumps({
        "kind": "ConfigMap",
        "apiVersion": "v1",
        "metadata": {
            "name": configMapName
        },
        "data": {
            recordToStore: recordToStore
        }
    })
    jsonText = OpenShiftQuery.patchApi('/api/v1/namespaces/{}/configmaps/{}'.format(OpenShiftQuery.getNameSpace(), configMapName),
                                       'application/merge-patch+json', jsonDataToStore)
    logger.debug('on storing config map %s of value %s the returned value is %s', configMapName, recordToStore, jsonText)


def deleteConfigMap(configMapName, recordToDelete):
    jsonDataToStore = json.dumps([
        {
            "op": "remove",
            "path": "/data/%s" % recordToDelete
        }
    ])
    try:
        jsonText = OpenShiftQuery.patchApi('/api/v1/namespaces/{}/configmaps/{}'.format(OpenShiftQuery.getNameSpace(), configMapName),
                                           'application/json-patch+json', jsonDataToStore)
        logger.debug('on removing config map %s of value %s the returned value is %s', configMapName, recordToDelete, jsonText)
    except:
        # the delete failed probably because there was not the value we wanted to delete but we want to ignore such situation
        # still we want to end with failure when api is not available thus here we do second check on availability
        jsonText = getConfigMapData(configMapName)
        logger.debug('on removing config map %s the value %s did not exist, the data content is %s', configMapName, recordToDelete, jsonText)

def _checkArguments(minNumber, errorMessage):
    if len(args.args) < minNumber:
        logger.critical(errorMessage)
        exit(1)
    for argCheck in args.args:
        if argCheck is None:
            logger.critical(errorMessage)
            exit(1)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description = "Queries OpenShift API, gathering the json and parsing it to get specific info from it")
    parser.add_argument("-q", "--query", required = False, type = QueryType, default = QueryType.PODS, choices=list(QueryType), help = "Query type/what to query")
    parser.add_argument("-f", "--format", required = False, type = OutputFormat, default = OutputFormat.RAW, choices=list(OutputFormat), help = "Output format")
    parser.add_argument("-l", "--loglevel", default="CRITICAL", help="Log level",
        choices=["debug", "DEBUG", "info", "INFO", "warning", "WARNING", "error", "ERROR", "critical", "CRITICAL"])
    parser.add_argument("args", nargs = argparse.REMAINDER, help = "Arguments of the query (each query type has different)")
    
    args = parser.parse_args()
    
    # don't spam warnings (e.g. when not verifying ssl connections)
    logging.captureWarnings(True)
    logging.basicConfig(level = args.loglevel.upper())
    logger = logging.getLogger(__name__)

    logger.debug("Starting query openshift api with args: %s", args)

    if args.query == QueryType.PODS:
        queryResult = getPods()
    elif args.query == QueryType.PODS_LIVING:
        queryResult = getLivingPods()
    elif args.query == QueryType.LOG:
        _checkArguments(1, 'query of type "log" requires one argument to be an existing pod name')
        queryResult = getLog(args.args[0])
    elif args.query == QueryType.CM_KEYS:
        _checkArguments(1, 'query of type "cm" (config map) requires one argument to be an existing config map')
        queryResult = getConfigMapData(args.args[0]).keys()
    elif args.query == QueryType.CM_STORE:
        _checkArguments(2, 'patching of type "cm" (config map) requires two arguments [an existing config map, value to store]')
        storeConfigMap(args.args[0], args.args[1])
        queryResult = ''
    elif args.query == QueryType.CM_REMOVE:
        _checkArguments(2, 'removing of type "cm" (config map) requires two arguments [an existing config map, value to remove]')
        deleteConfigMap(args.args[0], args.args[1])
        queryResult = ''
    else:
        logger.critical('No handler for query type %s', args.query)
        exit(1)

    if args.format == OutputFormat.LIST_SPACE:
        print ' '.join(queryResult)
    elif args.format == OutputFormat.LIST_COMMA:
        print ','.join(queryResult)
    else: # RAW format
        print queryResult

    exit(0)
