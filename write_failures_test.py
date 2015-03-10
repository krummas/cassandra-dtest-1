import os
import stat
import glob
import subprocess
import ccmlib
import uuid

from dtest import Tester, debug
from thrift_tests import get_thrift_client

from cassandra import WriteTimeout, WriteFailure
from cassandra import ConsistencyLevel
from cassandra.query import SimpleStatement
from cassandra.cluster import InvalidRequest

from tools import since
from assertions import assert_one, assert_none

from thrift_bindings.v30 import ttypes as thrift_types

KEYSPACE = "foo"

class TestWriteFailures(Tester):
    """
    Tests for write failures in the replicas,
    https://issues.apache.org/jira/browse/CASSANDRA-8592.

    They require CURRENT_VERSION = VERSION_4 in CassandraDaemon.Server
    otherwise these tests will fail.
    """

    def setUp(self):
        super(TestWriteFailures, self).setUp()

        self.ignore_log_patterns = [
            "Testing write failures", # The error to simulate a write failure
            "ERROR WRITE_FAILURE",    # Logged in DEBUG mode for write failures
            "MigrationStage"          # This occurs sometimes due to node down (because of restart)
        ]

        self.cluster.populate(3).start()
        self.node1, self.node2, self.node3 = self.cluster.nodes.values()

        self.expected_expt = WriteFailure
        self.protocol_version = 4

        self.replication_factor = 3
        self.consistency_level = ConsistencyLevel.ALL
        self.failing_nodes = [self.node2, self.node3]

    def tearDown(self):
        super(TestWriteFailures, self).tearDown()

    def _prepare_cluster(self):
        session = self.patient_cql_connection(self.node1, protocol_version=self.protocol_version)

        session.execute("""
            CREATE KEYSPACE IF NOT EXISTS %s
            WITH replication = { 'class': 'SimpleStrategy', 'replication_factor': '%s' }
            """ % (KEYSPACE, self.replication_factor))
        session.set_keyspace(KEYSPACE)

        session.execute("CREATE TABLE IF NOT EXISTS mytable (key text PRIMARY KEY, value text) WITH COMPACT STORAGE")
        session.execute("CREATE TABLE IF NOT EXISTS countertable (key uuid PRIMARY KEY, value counter)")

        for node in self.failing_nodes:
            node.stop()
            node.start(jvm_args=["-Dcassandra.test.fail_writes_ks=" + KEYSPACE])

            if node is self.node1:
                session = self.patient_cql_connection(self.node1, protocol_version=self.protocol_version)
                session.set_keyspace(KEYSPACE)

        return session

    def _perform_cql_statement(self, text):
        session = self._prepare_cluster()

        statement = session.prepare(text)
        statement.consistency_level = self.consistency_level

        if self.expected_expt is None:
            session.execute(statement)
        else:
            with self.assertRaises(self.expected_expt) as cm:
                session.execute(statement)

    @since('2.0')
    def test_mutation_v2(self):
        """
            A failed mutation at v2 receives a WriteTimeout
        """
        self.expected_expt = WriteTimeout
        self.protocol_version = 2
        self._perform_cql_statement("INSERT INTO mytable (key, value) VALUES ('key1', 'Value 1')")

    @since('2.1')
    def test_mutation_v3(self):
        """
            A failed mutation at v3 receives a WriteTimeout
        """
        self.expected_expt = WriteTimeout
        self.protocol_version = 3
        self._perform_cql_statement("INSERT INTO mytable (key, value) VALUES ('key1', 'Value 1')")

    @since('3.0')
    def test_mutation_v4(self):
        """
            A failed mutation at v4 receives a WriteFailure
        """
        self.expected_expt = WriteFailure
        self.protocol_version = 4
        self._perform_cql_statement("INSERT INTO mytable (key, value) VALUES ('key1', 'Value 1')")

    @since('3.0')
    def test_mutation_any(self):
        """
            A WriteFailure is not received at consistency level ANY
            even if all nodes fail because of hinting
        """
        self.consistency_level = ConsistencyLevel.ANY
        self.expected_expt = None
        self.failing_nodes = [self.node1, self.node2, self.node3]
        self._perform_cql_statement("INSERT INTO mytable (key, value) VALUES ('key1', 'Value 1')")

    @since('3.0')
    def test_mutation_one(self):
        """
            A WriteFailure is received at consistency level ONE
            if all nodes fail
        """
        self.consistency_level = ConsistencyLevel.ONE
        self.failing_nodes = [self.node1, self.node2, self.node3]
        self._perform_cql_statement("INSERT INTO mytable (key, value) VALUES ('key1', 'Value 1')")

    @since('3.0')
    def test_mutation_quorum(self):
        """
            A WriteFailure is not received at consistency level
            QUORUM if quorum succeeds
        """
        self.consistency_level = ConsistencyLevel.QUORUM
        self.expected_expt = None
        self.failing_nodes = [self.node3]
        self._perform_cql_statement("INSERT INTO mytable (key, value) VALUES ('key1', 'Value 1')")

    @since('3.0')
    def test_batch(self):
        """
            A failed batch receives a WriteFailure
        """
        self._perform_cql_statement("""
            BEGIN BATCH
            INSERT INTO mytable (key, value) VALUES ('key2', 'Value 2') USING TIMESTAMP 1111111111111111
            INSERT INTO mytable (key, value) VALUES ('key3', 'Value 3') USING TIMESTAMP 1111111111111112
            APPLY BATCH
        """)

    @since('3.0')
    def test_counter(self):
        """
            A failed counter mutation receives a WriteFailure
        """
        _id = str(uuid.uuid4())
        self._perform_cql_statement("""
            UPDATE countertable
                SET value = value + 1
                where key = {uuid}
        """.format(uuid=_id))

    @since('3.0')
    def test_paxos(self):
        """
            A light transaction receives a WriteFailure
        """
        self._perform_cql_statement("INSERT INTO mytable (key, value) VALUES ('key1', 'Value 1') IF NOT EXISTS")

    @since('3.0')
    def test_paxos_any(self):
        """
            A light transaction at consistency level ANY does not receive a WriteFailure
        """
        self.consistency_level = ConsistencyLevel.ANY
        self.expected_expt = None
        self._perform_cql_statement("INSERT INTO mytable (key, value) VALUES ('key1', 'Value 1') IF NOT EXISTS")

    @since('3.0')
    def test_thrift(self):
        """
            A thrift client receives a TimedOutException
        """
        self.expected_expt = thrift_types.TimedOutException

        session = self._prepare_cluster()
        client = get_thrift_client()
        client.transport.open()
        client.set_keyspace(KEYSPACE)

        with self.assertRaises(self.expected_expt) as cm:
            client.insert('key1',
                thrift_types.ColumnParent('mytable'),
                thrift_types.Column('value', 'Value 1', 0),
                thrift_types.ConsistencyLevel.ALL)

        client.transport.close()
