# vim: sw=4:ts=4:et
#
# ACE Collectors
# These objects collect things for remote ACE nodes to analyze.
#

import os, os.path
import pickle
import socket
import threading
import logging

import ace_api

import saq
from saq.database import use_db, execute_with_retry, get_db_connection, enable_cached_db_connections
from saq.error import report_exception

# some constants used as return values
WORK_SUBMITTED = 1
NO_WORK_AVAILABLE = 2
NO_NODES_AVAILABLE = 3
NO_WORK_SUBMITTED = 4

class Submission(object):
    """A single analysis submission.
       Keep in mind that this object gets serialized into a database blob via the pickle module.
       NOTE - The files parameter MUST be a list of file names (not file descriptors.)"""

    # this is basically just all the arguments that are passed to ace_api.submit
    
    def __init__(self,
                 description, 
                 analysis_mode,
                 tool,
                 tool_instance,
                 type,
                 event_time,
                 details,
                 observables,
                 tags,
                 files):

        self.description = description
        self.analysis_mode = analysis_mode
        self.tool = tool
        self.tool_instance = tool_instance
        self.type = type
        self.event_time = event_time
        self.details = details
        self.observables = observables
        self.tags = tags
        self.files = files

    def __str__(self):
        return "{} ({})".format(self.description, self.analysis_mode)

    def success(self):
        """Called by the RemoteNodeGroup when this has been successfully submitted to a remote node.
           By default this deletes any files submitted."""
        self.cleanup_files()

    def fail(self):
        """Called by the RemoteNodeGroup when this has failed to be submitted and full_delivery is disabled.
           By default this deletes any files submitted."""
        self.cleanup_files()

    def cleanup_files(self):
        """Removes any files passed in the files parameter."""
        for file_path in self.files:
            if os.path.exists(file_path):
                try:
                    os.remove(file_path)
                except Exception as e:
                    logging.error("unable to delete {}: {}".format(file_path, e))

class RemoteNode(object):
    def __init__(self, id, name, location, any_mode, last_update, analysis_mode, workload_count):
        self.id = id
        self.name = name
        self.location = location
        self.any_mode = any_mode
        self.last_update = last_update
        self.analysis_mode = analysis_mode
        self.workload_count = workload_count

    def __str__(self):
        return "RemoteNode(id={},name={})".format(self.id, self.name)

    def submit(self, submission):
        """Attempts to submit the given Submission to this node."""
        assert isinstance(submission, Submission)
        # we need to convert the list of files to what is expected by the ace_api.submit function
        files = [ (os.path.basename(f), open(f)) for f in submission.files]
        result = ace_api.submit(
            submission.description,
            remote_host=self.location,
            ssl_verification=saq.CONFIG['SSL']['ca_chain_path'],
            analysis_mode=submission.analysis_mode,
            tool=submission.tool,
            tool_instance=submission.tool_instance,
            type=submission.type,
            event_time=submission.event_time,
            details=submission.details,
            observables=submission.observables,
            tags=submission.tags,
            files=files)

        # clean up our file descriptors
        for name, fp in files:
            try:
                fp.close()
            except Exception as e:
                logging.error("unable to close file descriptor for {}: {}".format(name, e))

        return result

class RemoteNodeGroup(object):
    """Represents a collection of one or more RemoteNode objects that share the
       same group configuration property."""

    def __init__(self, name, coverage, full_delivery, database, group_id, batch_size=32):
        assert isinstance(name, str) and name
        assert isinstance(coverage, int) and coverage > 0 and coverage <= 100
        assert isinstance(full_delivery, bool)
        assert isinstance(database, str)
        assert isinstance(group_id, int)

        self.name = name

        # this the percentage of submissions that are actually sent to this node group
        self.coverage = coverage
        self.coverage_counter = 0

        # if full_delivery is True then all submissions assigned to the group will eventually be submitted
        # if set to False then at least one attempt is made to submit
        # setting to False is useful for QA and development type systems
        self.full_delivery = full_delivery

        # the name of the database to query for node status
        self.database = database

        # the id of this group in the work_distribution_groups table
        self.group_id = group_id

        # the (maximum) number of work items to pull at once from the database
        self.batch_size = batch_size

        # metrics
        self.assigned_count = 0 # how many emails were assigned to this group
        self.skipped_count = 0 # how many emails have skipped due to coverage rules
        self.delivery_failures = 0 # how many emails failed to delivery when full_delivery is disabled

        # main thread of execution for this group
        self.thread = None

        # set this to True to gracefully shut down the group
        self.shutdown_event = threading.Event()

        # when do we think a node has gone offline
        # each node (engine) should update it's status every [engine][node_status_update_frequency] seconds
        # so we wait for twice that long until we think a node is offline
        # at which point we no longer consider it for submissions
        self.node_status_update_frequency = saq.CONFIG['engine'].getint('node_status_update_frequency')

    def start(self):
        self.shutdown_event.clear()

        # main thread of execution for this group
        self.thread = threading.Thread(target=self.loop, name="RemoteNodeGroup {}".format(self.name))
        self.thread.start()

    def stop(self):
        self.shutdown_event.set()

    def wait(self):
        self.thread.join()

    def loop(self):
        enable_cached_db_connections()

        while True:
            try:
                result = self.execute()

                # if we did something then we immediately look for more work unless we're shutting down
                if result == WORK_SUBMITTED:
                    if self.shutdown_event.is_set():
                        break
                # if were was no work available to be submitted then wait a second and look again
                elif result == NO_WORK_AVAILABLE:
                    if self.shutdown_event.wait(1):
                        break
                # if there were no NODES available then wait a little while longer and look again
                elif result == NO_NODES_AVAILABLE:
                    if self.shutdown_event.wait(self.node_status_update_frequency / 2):
                        break
                elif result == NO_WORK_SUBMITTED:
                    if self.shutdown_event.wait(1):
                        break

            except Exception as e:
                logging.error("unexpected exception thrown in loop for {}: {}".format(self, e))
                report_exception()
                if self.shutdown_event.wait(1):
                    break

    @use_db
    def execute(self, db, c):
        # first we get a list of all the distinct analysis modes available in the work queue
        c.execute("""
SELECT DISTINCT(incoming_workload.mode)
FROM
    incoming_workload JOIN work_distribution ON incoming_workload.id = work_distribution.work_id
WHERE
    work_distribution.group_id = %s
    AND work_distribution.status = 'READY'
""", (self.group_id,))
        available_modes = c.fetchall()
        db.commit()

        # if we get nothing from this query then no work is available for this group
        if not available_modes:
            logging.debug("no work available for {}".format(self))
            return NO_WORK_AVAILABLE

        # flatten this out to a list of analysis modes
        available_modes = [_[0] for _ in available_modes]

        # given this list of modes that need remote targets, see what is currently available
        with get_db_connection(self.database) as node_db:
            node_c = node_db.cursor()
            sql = """
SELECT
    nodes.id, 
    nodes.name, 
    nodes.location, 
    nodes.any_mode,
    nodes.last_update,
    node_modes.analysis_mode,
    COUNT(workload.id) AS 'WORKLOAD_COUNT'
FROM
    nodes LEFT JOIN node_modes ON nodes.id = node_modes.node_id
    LEFT JOIN workload ON nodes.id = workload.node_id
WHERE
    nodes.company_id = %s
    AND nodes.is_local = 0
    AND TIMESTAMPDIFF(SECOND, nodes.last_update, NOW()) <= %s
    AND ( nodes.any_mode OR node_modes.analysis_mode in ( {} ) )
GROUP BY
    nodes.id,
    nodes.name,
    nodes.location,
    nodes.any_mode,
    nodes.last_update,
    node_modes.analysis_mode
ORDER BY
    WORKLOAD_COUNT ASC,
    nodes.last_update ASC
""".format(','.join(['%s' for _ in available_modes]))
            params = [ saq.COMPANY_ID, self.node_status_update_frequency * 2 ]
            params.extend(available_modes)
            node_c.execute(sql, tuple(params))
            node_status = node_c.fetchall()

        if not node_status:
            logging.debug("no remote nodes are avaiable for all analysis modes {} for {}".format(
                          ','.join(available_modes), self))
            return NO_NODES_AVAILABLE

        # now figure out what analysis modes are actually available for processing
        analysis_mode_mapping = {} # key = analysis_mode, value = [ RemoteNode ]
        any_mode_nodes = [] # list of nodes with any_mode set to True
        
        for node_id, name, location, any_mode, last_update, analysis_mode, workload_count in node_status:
            remote_node = RemoteNode(node_id, name, location, any_mode, last_update, analysis_mode, workload_count)
            if any_mode:
                any_mode_nodes.append(remote_node)

            if analysis_mode:
                if analysis_mode not in analysis_mode_mapping:
                    analysis_mode_mapping[analysis_mode] = []

                analysis_mode_mapping[analysis_mode].append(remote_node)

        # now we trim our list of analysis modes down to what is available
        # if we don't have a node that supports any mode
        if not any_mode_nodes:
            available_modes = [m for m in available_modes if m in remote_nodes.keys()]
            logging.debug("available_modes = {} after checking available nodes".format(available_modes))

        if not available_modes:
            logging.debug("no nodes are available that support the available analysis modes")
            return NO_NODES_AVAILABLE

        # now we get the next things to submit from the database that have an analysis mode that is currently
        # available to be submitted to

        sql = """
SELECT 
    incoming_workload.id,
    incoming_workload.mode,
    incoming_workload.work
FROM
    incoming_workload JOIN work_distribution ON incoming_workload.id = work_distribution.work_id
WHERE
    work_distribution.group_id = %s
    AND incoming_workload.mode IN ( {} )
    AND work_distribution.status = 'READY'
ORDER BY
    incoming_workload.id ASC
LIMIT %s""".format(','.join(['%s' for _ in available_modes]))
        params = [ self.group_id ]
        params.extend(available_modes)
        params.append(self.batch_size)

        c.execute(sql, tuple(params))
        work_batch = c.fetchall()
        db.commit()

        logging.info("submitting {} items".format(len(work_batch)))

        # simple flag that gets set if ANY submission is successful
        submission_success = False

        # we should have a small list of things to submit to remote nodes for this group
        for work_id, analysis_mode, submission_blob in work_batch:
            # first make sure we can un-pickle this
            try:
                submission = pickle.loads(submission_blob)
            except Exception as e:
                execute_with_retry(db, c, """UPDATE work_distribution SET status = 'COMPLETED' 
                                             WHERE group_id = %s AND work_id = %s""",
                                  (self.group_id, self.work_id), commit=True)
                logging.error("unable to un-pickle submission blob for id {}: {}".format(work_id, e))
                
            self.coverage_counter += self.coverage
            if self.coverage_counter < 100:
                # we'll be skipping this one
                execute_with_retry(db, c, """UPDATE work_distribution SET status = 'COMPLETED' 
                                             WHERE group_id = %s AND work_id = %s""",
                                  (self.group_id, work_id), commit=True)
                logging.debug("skipped work id {} for group {} due to coverage constraints".format(
                              work_id, self.name))
                #logging.info("MARKER: coverage counter for {} is {}".format(self, self.coverage_counter))
                continue

            # otherwise we try to submit it
            self.coverage_counter -= 100

            # sort the list of RemoteNode objects by the workload_count
            available_targets = any_mode_nodes[:]
            if analysis_mode in analysis_mode_mapping:
                available_targets.extend(analysis_mode_mapping[analysis_mode])
        
            target = sorted(available_targets, key=lambda n: n.workload_count)
            target = target[0] 

            # simple flag to remember if we failed to send
            submission_failed = False

            # attempt the send
            try:
                result = target.submit(submission)
                logging.info("{} got submission result {} for {}".format(self, result, submission))
                submission_success = True
            except Exception as e:
                log_function = logging.warning
                if not self.full_delivery:
                    log_function = logging.debug
                #else:
                    #report_exception()

                log_function("unable to submit work item {} to {} via group {}: {}".format(
                             submission, target, self, e))

                # if we are in full delivery mode then we need to try this one again later
                if self.full_delivery:
                    continue

                submission_failed = True

            # at this point we either sent it or we tried and failed but that's OK
            execute_with_retry(db, c, """UPDATE work_distribution SET status = 'COMPLETED' 
                                         WHERE group_id = %s AND work_id = %s""",
                              (self.group_id, work_id), commit=True)

            # check to see if we're the last attempt on this work item
            c.execute("""
SELECT 
    COUNT(*) 
FROM 
    incoming_workload JOIN work_distribution ON incoming_workload.id = work_distribution.work_id
WHERE
    incoming_workload.id = %s
    AND work_distribution.status = 'READY'
""", (work_id,))
            result = c.fetchone()
            db.commit()
            result = result[0]

            if result == 0:
                logging.debug("completed work item {}".format(submission))
                execute_with_retry(db, c, "DELETE FROM incoming_workload WHERE id = %s", (work_id,), commit=True)
                
                if submission_failed:
                    try:
                        submission.fail()
                    except Exception as e:
                        logging.error("call to {}.fail() failed: {}".format(submission, e))
                else:
                    try:
                        submission.success()
                    except Exception as e:
                        logging.error("call to {}.success() failed: {}".format(submission, e))

        if submission_success:
            return WORK_SUBMITTED

        return NO_WORK_SUBMITTED

    def __str__(self):
        return "RemoteNodeGroup(name={}, coverage={}, full_delivery={}, database={})".format(
                self.name, self.coverage, self.full_delivery, self.database)

class Collector(object):
    def __init__(self):
        # often used as the "tool_instance" property of analysis
        self.fqdn = socket.getfqdn()

        # set this to True to gracefully shut down the collector
        self.shutdown_event = threading.Event()

        # the list of RemoteNodeGroup targets this collector will send to
        self.remote_node_groups = []

    @use_db
    def add_group(self, name, coverage, full_delivery, database, db, c):
        c.execute("SELECT id FROM work_distribution_groups WHERE name = %s", (name,))
        row = c.fetchone()
        if row is None:
            c.execute("INSERT INTO work_distribution_groups ( name ) VALUES ( %s )", (name,))
            group_id = c.lastrowid
            db.commit()
        else:
            group_id = row[0]

        remote_node_group = RemoteNodeGroup(name, coverage, full_delivery, database, group_id)
        self.remote_node_groups.append(remote_node_group)
        logging.info("added {}".format(remote_node_group))
        return remote_node_group

    def start(self):
        # you need to add at least one group to send to
        if not self.remote_node_groups:
            raise RuntimeError("no RemoteNodeGroup objects have been added to {}".format(self))

        self.collection_thread = threading.Thread(target=self.loop, name="Collector")
        self.collection_thread.start()

        # start the node groups
        for group in self.remote_node_groups:
            group.start()

    def stop(self):
        self.shutdown_event.set()
        for group in self.remote_node_groups:
            group.stop()

    def wait(self):
        logging.info("waiting for collection thread to terminate...")
        self.collection_thread.join()
        for group in self.remote_node_groups:
            logging.info("waiting for {} thread to terminate...".format(group))
            group.wait()

        logging.info("collection ended")

    def loop(self):
        enable_cached_db_connections()

        while True:
            try:
                self.execute()
            except Exception as e:
                logging.error("unexpected exception thrown during loop for {}: {}".format(self, e))
                report_exception()
                if self.shutdown_event.wait(1):
                    break

            if self.shutdown_event.is_set():
                break

    @use_db
    def execute(self, db, c):
        next_submission = self.get_next_submission()
        # did we not get anything to submit?
        if next_submission is None:
            # wait a second before we check again... TODO make the wait optional
            self.shutdown_event.wait(1)
            return

        if not isinstance(next_submission, Submission):
            logging.critical("get_next_submission() must return an object derived from Submission")

        # add this as a workload item to the database queue
        execute_with_retry(db, c, self.insert_workload, (next_submission,), commit=True)
        logging.info("scheduled {} mode {}".format(next_submission.description, next_submission.analysis_mode))

    def insert_workload(self, db, c, next_submission):
        c.execute("INSERT INTO incoming_workload ( mode, work ) VALUES ( %s, %s )",
                 (next_submission.analysis_mode, pickle.dumps(next_submission)))

        if c.lastrowid is None:
            raise RuntimeError("missing lastrowid for INSERT transaction")

        work_id = c.lastrowid

        # assign this work to each configured group
        for remote_node_group in self.remote_node_groups:
            c.execute("INSERT INTO work_distribution ( work_id, group_id ) VALUES ( %s, %s )",
                     (work_id, remote_node_group.group_id))

    def get_next_submission(self):
        """Returns the next Submission object to be submitted to the remote nodes."""
        raise NotImplementedError()
