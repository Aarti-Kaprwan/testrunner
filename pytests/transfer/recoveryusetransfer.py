from testconstants import COUCHBASE_DATA_PATH
from transfer.transfer_base import TransferBaseTest
from couchbase.documentgenerator import BlobGenerator
from remote.remote_util import RemoteMachineShellConnection
from membase.api.rest_client import RestConnection, Bucket
import time

class RecoveryUseTransferTests(TransferBaseTest):

    def setUp(self):
        self.times_teardown_called = 1
        super(RecoveryUseTransferTests, self).setUp()
        self.server_origin = self.servers[0]
        self.server_recovery = self.servers[1]
        self.shell = RemoteMachineShellConnection(self.server_origin)
        info = self.shell.extract_remote_info()
        self.os = info.type.lower()

    def tearDown(self):
        super(RecoveryUseTransferTests, self).tearDown()
        if not self.input.param("skip_cleanup", True):
            if times_tear_down_called > 1 :
                if self.os == 'windows':
                    self.shell.delete_files("/cygdrive/c%s" % (self.backup_location))
                else:
                    self.shell.delete_files(self.backup_location)
                self.shell.disconnect()
                del self.buckets
        if self.input.param("skip_cleanup", True):
            if self.case_number > 1 or self.times_teardown_called >1:
                if self.os == 'windows':
                    self.shell.delete_files("/cygdrive/c%s" % (self.backup_location))
                else:
                    self.shell.delete_files(self.backup_location)
                self.shell.disconnect()
                del self.buckets
        self.times_teardown_called +=1

    def recover_to_cbserver(self):
        """Recover data with 2.0 couchstore files to a 2.0 online server

        We load a number of items to one node first and then do some mutation on these items.
        Later we use cbtranfer to transfer the couchstore files we have on this
        node to a new node. We verify the data by comparison between the items in KVStore
        and items in the new node."""

        self.load_data()

        kvs_before = {}
        bucket_names = []
        for bucket in self.buckets:
            kvs_before[bucket.name] = bucket.kvs[1]
            bucket_names.append(bucket.name)

        if self.default_bucket:
            self.cluster.create_default_bucket(self.server_recovery, self.bucket_size, self.num_replicas)
            self.buckets.append(Bucket(name="default", authType="sasl", saslPassword="", num_replicas=self.num_replicas, bucket_size=self.bucket_size))
        self._create_sasl_buckets(self.server_recovery, self.sasl_buckets)
        self._create_standard_buckets(self.server_recovery, self.standard_buckets)

        for bucket in self.buckets:
            bucket.kvs[1] = kvs_before[bucket.name]
            transfer_source = "couchstore-files://%s" % (COUCHBASE_DATA_PATH)
            transfer_destination = "http://%s@%s:%s -b %s -B %s -v -v -v" % (self.couchbase_login_info,
                                                                             self.server_recovery.ip,
                                                                             self.server_recovery.port,
                                                                             bucket.name, bucket.name)
            self.shell.execute_cbtransfer(transfer_source, transfer_destination)
        del kvs_before
        time.sleep(self.expire_time + 1)
        shell_server_recovery = RemoteMachineShellConnection(self.server_recovery)
        for bucket in self.buckets:
            shell_server_recovery.execute_cbepctl(bucket, "", "set flush_param", "exp_pager_stime", 5)
        shell_server_recovery.disconnect()
        time.sleep(30)

        self._wait_for_stats_all_buckets([self.server_recovery])
        self._verify_all_buckets(self.server_recovery, 1, self.wait_timeout*50, None, True)
        self._verify_stats_all_buckets([self.server_recovery])

    def recover_to_backupdir(self):
        """Recover data with 2.0 couchstore files to a 2.0 backup diretory

        We load a number of items to a node first and then do some mutataion on these items.
        Later we use cbtransfer to transfer the couchstore files we have on this node to
        a backup directory. We use cbrestore to restore these backup files to the same node
        for verification."""

        self.load_data()

        kvs_before = {}
        bucket_names = []

        self.shell.delete_files(self.backup_location)
        self.shell.create_directory(self.backup_location)

        for bucket in self.buckets:
            kvs_before[bucket.name] = bucket.kvs[1]
            bucket_names.append(bucket.name)
            transfer_source = "-v -v -v couchstore-files://%s" % (COUCHBASE_DATA_PATH)
            transfer_destination = self.backup_location
            self.shell.execute_cbtransfer(transfer_source, transfer_destination)

        self._all_buckets_delete(self.server_origin)
        if self.default_bucket:
            self.cluster.create_default_bucket(self.server_origin, self.bucket_size, self.num_replicas)
            self.buckets.append(Bucket(name="default", authType="sasl", saslPassword="", num_replicas=self.num_replicas, bucket_size=self.bucket_size))
        self._create_sasl_buckets(self.server_origin, self.sasl_buckets)
        self._create_standard_buckets(self.server_origin, self.standard_buckets)

        for bucket in self.buckets:
            bucket.kvs[1] = kvs_before[bucket.name]
        del kvs_before
        self.shell.restore_backupFile(self.couchbase_login_info, self.backup_location, bucket_names)
        time.sleep(self.expire_time + 1)
        for bucket in self.buckets:
            self.shell.execute_cbepctl(bucket, "", "set flush_param", "exp_pager_stime", 5)
        time.sleep(30)

        self._wait_for_stats_all_buckets([self.server_origin])
        self._verify_all_buckets(self.server_origin, 1, self.wait_timeout*50, None, True)
        self._verify_stats_all_buckets([self.server_origin])

    def load_data(self):
        gen_load = BlobGenerator('nosql', 'nosql-', self.value_size, end=self.num_items)
        gen_update = BlobGenerator('nosql', 'nosql-', self.value_size, end=(self.num_items/2-1))
        gen_expire = BlobGenerator('nosql', 'nosql-', self.value_size, start=self.num_items/2, end=(self.num_items*3/4-1))
        gen_delete = BlobGenerator('nosql', 'nosql-', self.value_size, start=self.num_items*3/4, end=self.num_items)
        self._load_all_buckets(self.server_origin, gen_load, "create", 0, 1, self.item_flag, True, batch_size=20000, pause_secs=5, timeout_secs=180)

        if(self.doc_ops is not None):
            if("update" in self.doc_ops):
                self._load_all_buckets(self.server_origin, gen_update, "update", 0, 1, self.item_flag, True, batch_size=20000, pause_secs=5, timeout_secs=180)
            if("delete" in self.doc_ops):
                self._load_all_buckets(self.server_origin, gen_delete, "delete", 0, 1, self.item_flag, True, batch_size=20000, pause_secs=5, timeout_secs=180)
            if("expire" in self.doc_ops):
                self._load_all_buckets(self.server_origin, gen_expire, "update", self.expire_time, 1, self.item_flag, True, batch_size=20000, pause_secs=5, timeout_secs=180)
        self._wait_for_stats_all_buckets([self.server_origin])
        time.sleep(30)
