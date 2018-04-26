source ${JBOSS_HOME}/bin/launch/openshift-node-name.sh
[ "${SCRIPT_DEBUG}" = "true" ] && DEBUG_QUERY_API_PARAM="-l debug"

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

# parameters
# - base directory
function partitionPV() {
  local podsDir="$1"
  local applicationPodDir

  mkdir -p "${podsDir}"

  init_pod_name
  local applicationPodDir="${podsDir}/${POD_NAME}"

  local waitCounter=0
  # 2) while any file matching, sleep
  while true; do
    local isRecoveryInProgress=false
    # TODO: expecting the ConfigMap ${CONFIG_MAP_MARKER_NAME} exists what if it won't exist? Script should probably create on for itself.
    # is there an existing RECOVERY descriptor that means a recovery is in progress
    unset recoveryMarkers
    recoveryMarkers=($(python ${JBOSS_HOME}/bin/queryapi/query.py ${DEBUG_QUERY_API_PARAM} -f list_space -q cm_keys ${CONFIG_MAP_MARKER_NAME}))
    local successOnApiConnection=$?
    for recoveryMarker in ${recoveryMarkers[@]}; do
      [[ "$recoveryMarker" =~ "${POD_NAME}-RECOVERY-" ]] && isRecoveryInProgress=true && break
    done

    if [ $successOnApiConnection -ne 0 ]; then
      # fail to connect OpenShift API server
      echo "Failure on connecting to the OpenShift API server, let's wait and try again"
    elif $isRecoveryInProgress; then
      # recovery in progress
      echo "Waiting to start pod ${POD_NAME} as recovery process '${recoveryMarker}' is currently cleaning data directory."
    else
      # we are free to start the app container
      break
    fi

    sleep 1
    echo "`date`: waiting for recovery process to clean the environment for the pod to start"
  done

  # 3) create /pods/<applicationPodName>
  SERVER_DATA_DIR="${applicationPodDir}/serverData"
  mkdir -p "${SERVER_DATA_DIR}"

  if [ ! -f "${SERVER_DATA_DIR}/../data_initialized" ]; then
    init_data_dir ${SERVER_DATA_DIR}
    touch "${SERVER_DATA_DIR}/../data_initialized"
  fi

  # 4) launch EAP with node name as pod name
  NODE_NAME="${POD_NAME}" runServer "${SERVER_DATA_DIR}" &

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

    # 1) Periodically, for each /pods/<applicationPodName>
    for applicationPodDir in "${podsDir}"/*; do
      # check if the found file is type of directory, if not directory move to the next item
      [ ! -d "$applicationPodDir" ] && continue

      local applicationPodName="$(basename ${applicationPodDir})"

      # doing potentialy two(!) checks for living pods for not writing to the etcd when not necessary, otherwise we would write in each cycle
      echo "examining if the pod: '${applicationPodName}' is living"
      unset LIVING_PODS
      LIVING_PODS=($(python ${JBOSS_HOME}/bin/queryapi/query.py -q pods_living -f list_space ${DEBUG_QUERY_API_PARAM}))
      # the pod is living thus not starting the recovery process
      if arrContains ${applicationPodName} "${LIVING_PODS[@]}"; then
        continue
      fi

      # 1.a) create <applicationPodName>-RECOVERY-<recoveryPodName> marker at etcd
      python ${JBOSS_HOME}/bin/queryapi/query.py ${DEBUG_QUERY_API_PARAM} -q cm_store ${CONFIG_MAP_MARKER_NAME} "${applicationPodName}-RECOVERY-${recoveryPodName}"
      STATUS=42 # expecting there could be  error on getting living pods

      # 1.a.i) if <applicationPodName> is not in the cluster
      unset LIVING_PODS
      LIVING_PODS=($(python ${JBOSS_HOME}/bin/queryapi/query.py -q pods_living -f list_space ${DEBUG_QUERY_API_PARAM}))
      [ $? -ne 0 ] && echo "ERROR: Can't get list of living pods" && continue
      STATUS=-1 # here we have data about living pods and the recovery marker can be removed if the pod is living
      if ! arrContains ${applicationPodName} "${LIVING_PODS[@]}"; then
          
        (
          # 1.a.ii) run recovery until empty (including orphan checks and empty object store hierarchy deletion)
          SERVER_DATA_DIR="${applicationPodDir}/serverData"
          JBOSS_NODE_NAME="$applicationPodName" runMigration "${SERVER_DATA_DIR}" &

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

      # 1.b.) Deleting the recovery marker from etcd storage
      if [ $STATUS -eq 0 ] || [ $STATUS -eq -1 ]; then
        # STATUS is 0: we are free from in-doubt transactions, -1: there is a running pod of the same name (do the recovery on his own if needed)
        python ${JBOSS_HOME}/bin/queryapi/query.py ${DEBUG_QUERY_API_PARAM} -q cm_remove ${CONFIG_MAP_MARKER_NAME} "${applicationPodName}-RECOVERY-${recoveryPodName}"
      fi
    done

    # 2) Periodically, for recovery markers <applicationPodName>-RECOVERY-<recoveryPodName>, for failed recovery pods
    local recoveryMarkers=($(python ${JBOSS_HOME}/bin/queryapi/query.py ${DEBUG_QUERY_API_PARAM} -f list_space -q cm_keys ${CONFIG_MAP_MARKER_NAME}))
    unset LIVING_PODS
    LIVING_PODS=($(python ${JBOSS_HOME}/bin/queryapi/query.py -q pods_living -f list_space ${DEBUG_QUERY_API_PARAM}))
    if [ $? -ne 0 ]; then
      echo "ERROR: Can't get list of living pods from OpenShift API. Garbage collection of recovery markers is not succesful this round."
    else
      for recoveryMarker in ${recoveryMarkers[@]}; do
        local recoveryPodNameToCheck=${recoveryMarker#*RECOVERY-}
        if ! arrContains ${recoveryPodNameToCheck} "${LIVING_PODS[@]}"; then
          # recovery pod is dead, garbage collecting
          python ${JBOSS_HOME}/bin/queryapi/query.py ${DEBUG_QUERY_API_PARAM} -q cm_remove ${CONFIG_MAP_MARKER_NAME} "${recoveryMarker}"
        fi
      done
    fi

    echo "`date`: Finished Migration Check cycle, pausing for ${MIGRATION_PAUSE} seconds before resuming"
    sleep "${MIGRATION_PAUSE}"
  done
}

# parameters
# - pod name (optional)
function probePodLog() {
  init_pod_name
  local podNameToProbe=${1:-$POD_NAME}

  local logOutput=$(python ${JBOSS_HOME}/bin/queryapi/query.py -q log ${podNameToProbe})
  local probeStatus=$?

  if [ $probeStatus -ne 0 ]; then
    echo "Cannot contact OpenShift API to get log for pod ${POD_NAME}"
    return 1
  fi

  printf $logOutput | grep 'ERROR'
  local logProbeStatus=$?

  if [ $logProbeStatus -eq 0 ]; then # ERROR string was found in the log output
    echo "Server at ${NAMESPACE}/${POD_NAME} started with errors"
    return 1
  fi

  return 0
}
