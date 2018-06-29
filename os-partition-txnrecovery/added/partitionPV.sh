#!/bin/sh

[ "x${SCRIPT_DEBUG}" = "xtrue" ] && DEBUG_QUERY_API_PARAM="-l debug"

# parameters
# - needle to search in array
# - array passed as: "${ARRAY_VAR[@]}"
function arrContains() {
  local element match="$1"
  shift
  for element; do
    [[ "$element" == "$match" ]] && return 0
  done
  return 1
}

function init_pod_name() {
  # when POD_NAME is non-zero length using that given name

  # docker sets up container_uuid
  [ -z "${POD_NAME}" ] && POD_NAME="${container_uuid}"
  # openshift sets up the node id as host name
  [ -z "${POD_NAME}" ] && POD_NAME="${HOSTNAME}"

  # having set the POD_NAME is crucial as the processing depends on unique
  # pod name being used as identifier for migration
  if [ -z "${POD_NAME}" ]; then
    >&2 echo "Cannot proceed further as failed to get unique POD_NAME as identifier of the server to be started"
    exit 1
  fi
}

# used to redefine starting jboss.node.name as identifier of jboss container
#   need to be restricted to 23 characters (CLOUD-427)
function truncate_jboss_node_name() {
  local NODE_NAME_TRUNCATED="$1"
  if [ ${#1} -gt 23 ]; then
    NODE_NAME_TRUNCATED=${1: -23}
  fi
  NODE_NAME_TRUNCATED=${NODE_NAME_TRUNCATED##-} # do not start the identifier with '-', it makes bash issues
  echo "${NODE_NAME_TRUNCATED}"
}

# parameters
# - base directory
function partitionPV() {
  local podsDir="$1"

  mkdir -p "${podsDir}"

  init_pod_name

  local waitCounter=0
  # 2) while any file matching, sleep
  while true; do
    local isRecoveryInProgress=false
    # is there an existing RECOVERY descriptor that means a recovery is in progress
    find "${podsDir}" -maxdepth 1 -type f -name "${POD_NAME}-RECOVERY-*" 2>/dev/null | grep -q .
    [ $? -eq 0 ] && isRecoveryInProgress=true

    # we are free to start the app container
    if ! $isRecoveryInProgress; then
      break
    fi

    if $isRecoveryInProgress; then
      echo "Waiting to start pod ${POD_NAME} as recovery process '$(echo ${podsDir}/${POD_NAME}-RECOVERY-*)' is currently cleaning data directory."
    fi

    sleep 1
    echo "`date`: waiting for recovery process to clean the environment for the pod to start"
  done

  # 3) obtaining split directory /pods/split-# by creating a application pod naming marker file
  local applicationPodDir
  # 3a) trying to get split-n directory from the previous run (owned by this pod)
  for alreadyExistingApplicationPodDir in "${podsDir}/split-"*; do
      if [ -f "${alreadyExistingApplicationPodDir}/${POD_NAME}.marker" ]; then
        applicationPodDir="${alreadyExistingApplicationPodDir}"
          echo "[INFO] found current pod marker '$POD_NAME' at '${applicationPodDir}/${POD_NAME}.marker'. Will use '${applicationPodDir}' as data directory"
        break
    fi
  done
  # 3b) finding empty split directory to use
  if [ "x$applicationPodDir" = "x" ]; then
    local splitCounter=1
    while true; do
      local splitDir="${podsDir}/split-${splitCounter}"
      local numberOfFiles=$(find "${splitDir}" -maxdepth 1 -type f -name '*' 2>/dev/null | wc -l)
      if [ $numberOfFiles -eq 0 ]; then
        echo "[INFO] No data found at '${splitDir}' taking it for the data dir and creating marker file '${splitDir}/${POD_NAME}.marker'"
        mkdir -p "${splitDir}"
        echo "${POD_NAME}" > "${splitDir}/${POD_NAME}.marker" # creating our marker at the split directory
        sync
      else # there is some data in the split directory
        echo "[INFO] Unfinished data found at '${splitDir}/${POD_NAME}.marker' going for another data dir, directory listing:"
        ls -l -a "${splitDir}"
        splitCounter=$((splitCounter+1))
        continue
      fi

      # error on marker creation let's try it again
      if [ ! -f "${splitDir}/${POD_NAME}.marker" ]; then
        echo "[ERROR] Cannot create marker file at '${splitDir}/${POD_NAME}.marker' going to try it again"
        continue
      fi

      # check we are the only one who created the marker
      local numberOfMarkers=$(find "${splitDir}" -maxdepth 1 -type f -name '*.marker' 2>/dev/null | wc -l)
      if [ $numberOfMarkers -eq 1 ]; then
        applicationPodDir="${splitDir}"
        echo "[INFO] grabbed the directory '${applicationPodDir}' as data directory"
        break
      else
        rm -f "${splitDir}/${POD_NAME}.marker"
        # continue at the same directory or go to the next one, simple livelock avoidance
        [ $((RANDOM%2)) -eq 0 ] && splitCounter=$((splitCounter+1))
        echo "[ERROR] there was more than only one marker at the '${splitDir}'. Continue once again at 'split-${splitCounter}. Directory content:"
        ls -la  "${splitDir}"
      fi
    done
  fi

  SERVER_DATA_DIR="${applicationPodDir}/serverData"
  mkdir -p "${SERVER_DATA_DIR}"
  if [ ! -f "${SERVER_DATA_DIR}/../data_initialized" ]; then
    init_data_dir ${SERVER_DATA_DIR}
    touch "${SERVER_DATA_DIR}/../data_initialized"
  fi

  # 4) launch server with node name as pod name (openshift-node-name.sh uses the node name value)
  NODE_NAME=$(truncate_jboss_node_name "${POD_NAME}") runServer "${SERVER_DATA_DIR}" &

  PID=$!

  trap "echo Received TERM of pid ${PID} of pod name ${POD_NAME}; kill -TERM $PID" TERM

  wait $PID 2>/dev/null
  STATUS=$?
  trap - TERM
  wait $PID 2>/dev/null

  echo "Server terminated with status $STATUS ($(kill -l $STATUS 2>/dev/null))"

  if [ "$STATUS" -eq 255 ] ; then
    echo "Server returned 255, changing to 254"
    STATUS=254
  fi

  exit $STATUS
}

# parameters
# - base directory
# - migration pause between cycles
function migratePV() {
  local podsDir="$1"
  local applicationPodDir
  MIGRATION_PAUSE="${2:-30}"
  MIGRATED=false

  init_pod_name
  local recoveryPodName="${POD_NAME}"

  while true ; do

    # 1) Periodically, for each /pods/split-#
    for applicationPodDir in "${podsDir}"/split-*; do
      # check if the found file is type of directory, if not directory move to the next item
      [ ! -d "$applicationPodDir" ] && continue
      COUNT=${applicationPodDir##*-} # expecting number to be last character at split-#

      # obtaining the application pod name via application pod namimg marker files
      local numberOfMarkers=$(find "${applicationPodDir}" -maxdepth 1 -type f -name '*.marker' 2>/dev/null | wc -l)
      # no marker file - there is nothing we know how to process in the directory
      [ $numberOfMarkers -eq 0 ] && continue
      # more markers - an attempt of more application pods staring with this data directory, that can't work thus it was left alone
      if [ $numberOfMarkers -gt 1 ]; then
        echo "[INFO] multiple markers were found at '${applicationPodDir}', need to verify if they are alive. Directory listing:"
        ls -la  "${applicationPodDir}"
        LIVING_PODS=($($(dirname ${BASH_SOURCE[0]})/query.py -q pods_living -f list_space ${DEBUG_QUERY_API_PARAM}))
        for markerFile in "${applicationPodDir}"/*.marker; do
          local markerFile=$(basename "${markerFile}")
          local podName=${markerFile%.marker}
          if ! arrContains ${podName} "${LIVING_PODS[@]}"; then
            echo "[INFO] pod '${podName}' is death but having existing marker file at '${markerFile}. Removing it."
            rm -f "${markerFile}"
          else
            echo "[WARNING] pod '${podName}' is alive but there are multiple marker files at '${applicationPodDir}. Leaving the marker '${markerFile}' untouched."
          fi
        done
        continue
      fi
      # just one marker file - using the data directory for app server to be started with node name equal to one assigned at the marker file
      applicationPodNameWithDir=$(echo ${applicationPodDir}/*.marker)
      applicationPodNameMarkerFile=$(basename "${applicationPodNameWithDir}")
      applicationPodName=${applicationPodNameMarkerFile%.marker}

      # 1.a) create /pods/<applicationPodName>-RECOVERY-<recoveryPodName>
      touch "${podsDir}/${applicationPodName}-RECOVERY-${recoveryPodName}"
      STATUS=42 # expecting there could be  error on getting living pods

      # 1.a.i) if <applicationPodName> is not in the cluster
      echo "examining existence of living pod for directory: '${applicationPodDir}'"
      unset LIVING_PODS
      LIVING_PODS=($($(dirname ${BASH_SOURCE[0]})/query.py -q pods_living -f list_space ${DEBUG_QUERY_API_PARAM}))
      [ $? -ne 0 ] && echo "ERROR: Can't get list of living pods" && continue
      # expecting the application pod of the same name was started/is living, it will manage recovery on its own
      local IS_APPLICATION_POD_LIVING=true
      if ! arrContains ${applicationPodName} "${LIVING_PODS[@]}"; then

        IS_APPLICATION_POD_LIVING=false

        (
          # 1.a.ii) run recovery until empty (including orphan checks and empty object store hierarchy deletion)
          MIGRATION_POD_TIMESTAMP=$(getPodLogTimestamp)  # investigating on current pod timestamp
          SERVER_DATA_DIR="${applicationPodDir}/serverData"
          NODE_NAME=$(truncate_jboss_node_name "${applicationPodName}") runMigration "${SERVER_DATA_DIR}" &

          PID=$!

          trap "echo Received TERM ; kill -TERM $PID" TERM

          wait $PID 2>/dev/null
          STATUS=$?
          trap - TERM
          wait $PID 2>/dev/null

          echo "Migration terminated with status $STATUS ($(kill -l $STATUS))"

          if [ "$STATUS" -eq 255 ] ; then
            echo "Server returned 255, changing to 254"
            STATUS=254
          fi
          exit $STATUS
        ) &

        PID=$!

        trap "kill -TERM $PID" TERM

        wait $PID 2>/dev/null
        STATUS=$?
        trap - TERM
        wait $PID 2>/dev/null

        if [ $STATUS -eq 0 ]; then
          # 1.a.iii) Delete /pods/<applicationPodName> when recovery was succesful
          echo "`date`: Migration succesfully finished for application directory ${applicationPodDir} thus removing it by recovery pod ${recoveryPodName}"
          rm -rf "${applicationPodDir}"
        fi
      fi

      # 1.b.) Deleting the recovery marker
      if [ $STATUS -eq 0 ] || [ $IS_APPLICATION_POD_LIVING ]; then
        # STATUS is 0: we are free from in-doubt transactions
        # IS_APPLICATION_POD_LIVING==true: there is a running pod of the same name, will do the recovery on his own, recovery pod won't manage it
        rm -f "${podsDir}/${applicationPodName}-RECOVERY-${recoveryPodName}"
      fi

      # 2) Periodically, for files /pods/<applicationPodName>-RECOVERY-<recoveryPodName>, for failed recovery pods
      for recoveryPodFilePathToCheck in "${podsDir}/"*-RECOVERY-*; do
        local recoveryPodFileToCheck="$(basename ${recoveryPodFilePathToCheck})"
        local recoveryPodNameToCheck=${recoveryPodFileToCheck#*RECOVERY-}

        unset LIVING_PODS
        LIVING_PODS=($($(dirname ${BASH_SOURCE[0]})/query.py -q pods_living -f list_space ${DEBUG_QUERY_API_PARAM}))
        [ $? -ne 0 ] && echo "ERROR: Can't get list of living pods" && continue

        if ! arrContains ${recoveryPodNameToCheck} "${LIVING_PODS[@]}"; then
          # recovery pod is dead, garbage collecting
          rm -f "${recoveryPodFilePathToCheck}"
        fi
      done

    done

    echo "`date`: Finished Migration Check cycle, pausing for ${MIGRATION_PAUSE} seconds before resuming"
    MIGRATION_POD_TIMESTAMP=$(getPodLogTimestamp)
    sleep "${MIGRATION_PAUSE}"
  done
}

# parameters
# - pod name (optional)
function getPodLogTimestamp() {
  init_pod_name
  local podNameToProbe=${1:-$POD_NAME}

  local logOutput=$($(dirname ${BASH_SOURCE[0]})/query.py -q log --pod ${podNameToProbe} --tailline 1)
  # only one, last line of the log, is returned, taking the start which is timestamp
  echo $logOutput | sed 's/ .*$//'
}

# parameters
# - since time (when the pod listing start at)
# - pod name (optional)
function probePodLogForRecoveryErrors() {
  init_pod_name
  local sinceTimestampParam=''
  local sinceTimestamp=${1:-$MIGRATION_POD_TIMESTAMP}
  [ "x$sinceTimestamp" != "x" ] && sinceTimestampParam="--sincetime ${sinceTimestamp}"
  local podNameToProbe=${2:-$POD_NAME}

  local logOutput=$($(dirname ${BASH_SOURCE[0]})/query.py -q log --pod ${podNameToProbe} ${sinceTimestampParam})
  local probeStatus=$?

  if [ $probeStatus -ne 0 ]; then
    echo "Cannot contact OpenShift API to get log for pod ${POD_NAME}"
    return 1
  fi

  local isPeriodicRecoveryError=false
  local patternToCheck="ERROR.*Periodic Recovery"
  [ "x${SCRIPT_DEBUG}" = "xtrue" ] && set +x # even for debug it's too verbose to print this pattern checking
  while read line; do
    [[ $line =~ $patternToCheck ]] && isPeriodicRecoveryError=true && break
  done <<< "$logOutput"
  [ "x${SCRIPT_DEBUG}" = "xtrue" ] && set -x

  if $isPeriodicRecoveryError; then # ERROR string was found in the log output
    echo "Pod '${POD_NAME}' started with periodic recovery errors: '$line'"
    return 1
  fi

  return 0
}
