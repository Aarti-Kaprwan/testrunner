import logger
import unittest
import copy
import datetime
import time

from couchbase.cluster import Cluster
from couchbase.document import View
from couchbase.documentgenerator import DocumentGenerator
from TestInput import TestInputSingleton
from membase.api.rest_client import RestConnection, Bucket
from membase.helper.bucket_helper import BucketOperationHelper
from membase.helper.cluster_helper import ClusterOperationHelper
from membase.helper.rebalance_helper import RebalanceHelper
from memcached.helper.data_helper import MemcachedClientHelper

class BaseTestCase(unittest.TestCase):
    def setUp(self):
        self.log = logger.Logger.get_logger()
        self.input = TestInputSingleton.input
        self.servers = self.input.servers
        self.buckets = []
        self.master = self.servers[0]
        self.cluster = Cluster()
        self.pre_warmup_stats = {}
        try:
            self.wait_timeout = self.input.param("wait_timeout", 60)
            # number of case that is performed from testrunner( increment each time)
            self.case_number = self.input.param("case_number", 0)
            self.default_bucket = self.input.param("default_bucket", True)
            if self.default_bucket:
                self.default_bucket_name = "default"
            self.standard_buckets = self.input.param("standard_buckets", 0)
            self.sasl_buckets = self.input.param("sasl_buckets", 0)
            self.total_buckets = self.sasl_buckets + self.default_bucket + self.standard_buckets
            self.num_servers = self.input.param("servers", len(self.servers))
            # initial number of items in the cluster
            self.nodes_init = self.input.param("nodes_init", 1)
            self.nodes_in = self.input.param("nodes_in", 1)
            self.nodes_out = self.input.param("nodes_out", 1)

            self.num_replicas = self.input.param("replicas", 1)
            self.num_items = self.input.param("items", 1000)
            self.value_size = self.input.param("value_size", 512)
            self.dgm_run = self.input.param("dgm_run", False)
            # max items number to verify in ValidateDataTask, None - verify all
            self.max_verify = self.input.param("max_verify", None)
            # we don't change consistent_view on server by default
            self.disabled_consistent_view = self.input.param("disabled_consistent_view", None)
            self.rebalanceIndexWaitingDisabled = self.input.param("rebalanceIndexWaitingDisabled", None)
            self.rebalanceIndexPausingDisabled = self.input.param("rebalanceIndexPausingDisabled", None)
            self.maxParallelIndexers = self.input.param("maxParallelIndexers", None)
            self.maxParallelReplicaIndexers = self.input.param("maxParallelReplicaIndexers", None)
            self.log.info("==============  basetestcase setup was started for test #{0} {1}=============="\
                          .format(self.case_number, self._testMethodName))
            # avoid any cluster operations in setup for new upgrade tests
            if str(self.__class__).find('newupgradetests') != -1:
                self.log.info("any cluster operation in setup will be skipped")
                self.log.info("==============  basetestcase setup was finished for test #{0} {1} =============="\
                          .format(self.case_number, self._testMethodName))
                return
            # avoid clean up if the previous test has been tear down
            if not self.input.param("skip_cleanup", True) or self.case_number == 1:
                self.tearDown()
                self.cluster = Cluster()
            if str(self.__class__).find('rebalanceout.RebalanceOutTests') != -1:
                # rebalance all nodes into the cluster before each test
                self.cluster.rebalance(self.servers[:self.num_servers], self.servers[1:self.num_servers], [])
            elif self.nodes_init > 1:
                self.cluster.rebalance(self.servers[:1], self.servers[1:self.nodes_init], [])
            self.quota = self._initialize_nodes(self.cluster, self.servers, self.disabled_consistent_view,
                                            self.rebalanceIndexWaitingDisabled, self.rebalanceIndexPausingDisabled,
                                            self.maxParallelIndexers, self.maxParallelReplicaIndexers)
            if self.dgm_run:
                self.quota = 256
            if self.total_buckets > 0:
                self.bucket_size = self._get_bucket_size(self.quota, self.total_buckets)
            if str(self.__class__).find('newupgradetests') == -1:
                self._bucket_creation()
            self.log.info("==============  basetestcase setup was finished for test #{0} {1} =============="\
                          .format(self.case_number, self._testMethodName))
            self._log_start(self)
        except Exception, e:
            self.cluster.shutdown()
            self.fail(e)

    def tearDown(self):
            try:
                if (hasattr(self, '_resultForDoCleanups') and len(self._resultForDoCleanups.failures) > 0 \
                    and TestInputSingleton.input.param("stop-on-failure", False))\
                        or self.input.param("skip_cleanup", False):
                    self.log.warn("CLEANUP WAS SKIPPED")
                else:
                    self.log.info("==============  basetestcase cleanup was started for test #{0} {1} =============="\
                          .format(self.case_number, self._testMethodName))
                    rest = RestConnection(self.master)
                    alerts = rest.get_alerts()
                    if alerts is not None and len(alerts) != 0:
                        self.log.warn("Alerts were found: {0}".format(alerts))
                    if rest._rebalance_progress_status() == 'running':
                        self.log.warning("rebalancing is still running, test should be verified")
                        stopped = rest.stop_rebalance()
                        self.assertTrue(stopped, msg="unable to stop rebalance")
                    BucketOperationHelper.delete_all_buckets_or_assert(self.servers, self)
                    ClusterOperationHelper.cleanup_cluster(self.servers)
                    time.sleep(10)
                    ClusterOperationHelper.wait_for_ns_servers_or_assert(self.servers, self)
                    self.log.info("==============  basetestcase cleanup was finished for test #{0} {1} =============="\
                          .format(self.case_number, self._testMethodName))
            finally:
                # stop all existing task manager threads
                self.cluster.shutdown()
                self._log_finish(self)

    @staticmethod
    def _log_start(self):
        try:
            msg = "{0} : {1} started ".format(datetime.datetime.now(), self._testMethodName)
            RestConnection(self.servers[0]).log_client_error(msg)
        except:
            pass

    @staticmethod
    def _log_finish(self):
        try:
            msg = "{0} : {1} finished ".format(datetime.datetime.now(), self._testMethodName)
            RestConnection(self.servers[0]).log_client_error(msg)
        except:
            pass

    def _initialize_nodes(self, cluster, servers, disabled_consistent_view=None, rebalanceIndexWaitingDisabled=None,
                          rebalanceIndexPausingDisabled=None, maxParallelIndexers=None, maxParallelReplicaIndexers=None):
        quota = 0
        init_tasks = []
        for server in servers:
            init_tasks.append(cluster.async_init_node(server, disabled_consistent_view, rebalanceIndexWaitingDisabled,
                          rebalanceIndexPausingDisabled, maxParallelIndexers, maxParallelReplicaIndexers))
        for task in init_tasks:
            node_quota = task.result()
            if node_quota < quota or quota == 0:
                quota = node_quota
        return quota

    def _bucket_creation(self):
        if self.default_bucket:
            self.cluster.create_default_bucket(self.master, self.bucket_size, self.num_replicas)
            self.buckets.append(Bucket(name="default", authType="sasl", saslPassword="",
                                           num_replicas=self.num_replicas, bucket_size=self.bucket_size))

        self._create_sasl_buckets(self.master, self.sasl_buckets)
        self._create_standard_buckets(self.master, self.standard_buckets)


    def _get_bucket_size(self, quota, num_buckets, ratio=2.0 / 3.0):
        ip = self.servers[0]
        for server in self.servers:
            if server.ip == ip:
                return int(ratio / float(self.num_servers) / float(num_buckets) * float(quota))
        return int(ratio / float(num_buckets) * float(quota))

    def _create_sasl_buckets(self, server, num_buckets):
        bucket_tasks = []
        for i in range(num_buckets):
            name = 'bucket' + str(i)
            bucket_tasks.append(self.cluster.async_create_sasl_bucket(server, name,
                                                                      'password',
                                                                      self.bucket_size,
                                                                      self.num_replicas))
            self.buckets.append(Bucket(name=name, authType="sasl", saslPassword='password',
                                       num_replicas=self.num_replicas, bucket_size=self.bucket_size));
        for task in bucket_tasks:
            task.result()

    def _create_standard_buckets(self, server, num_buckets):
        bucket_tasks = []
        for i in range(num_buckets):
            name = 'standard_bucket' + str(i)
            bucket_tasks.append(self.cluster.async_create_standard_bucket(server, name,
                                                                          11214 + i,
                                                                          self.bucket_size,
                                                                          self.num_replicas))

            self.buckets.append(Bucket(name=name, authType=None, saslPassword=None, num_replicas=self.num_replicas,
                                       bucket_size=self.bucket_size, port=11214 + i));
        for task in bucket_tasks:
            task.result()

    def _all_buckets_delete(self, server):
        delete_tasks = []
        for bucket in self.buckets:
            delete_tasks.append(self.cluster.async_bucket_delete(server, bucket.name))

        for task in delete_tasks:
            task.result()
        self.buckets = []

    def _verify_stats_all_buckets(self, servers, wait_time=60):
        stats_tasks = []
        for bucket in self.buckets:
            items = sum([len(kv_store) for kv_store in bucket.kvs.values()])
            stats_tasks.append(self.cluster.async_wait_for_stats(servers, bucket, '',
                               'curr_items', '==', items))
            stats_tasks.append(self.cluster.async_wait_for_stats(servers, bucket, '',
                               'vb_active_curr_items', '==', items))

            available_replicas = self.num_replicas
            if len(servers) == self.num_replicas:
                available_replicas = len(servers) - 1
            elif len(servers) <= self.num_replicas:
                available_replicas = len(servers) - 1

            stats_tasks.append(self.cluster.async_wait_for_stats(servers, bucket, '',
                                   'vb_replica_curr_items', '==', items * available_replicas))
            stats_tasks.append(self.cluster.async_wait_for_stats(servers, bucket, '',
                                   'curr_items_tot', '==', items * (available_replicas + 1)))
        try:
            for task in stats_tasks:
                task.result(wait_time)
        except Exception as e:
            print e;
            for task in stats_tasks:
                task.cancel()
            raise Exception("unable to get expected stats during {0} sec".format(wait_time))

    """Asynchronously applys load generation to all bucekts in the cluster.
 bucket.name, gen,
                                                          bucket.kvs[kv_store],
                                                          op_type, exp
    Args:
        server - A server in the cluster. (TestInputServer)
        kv_gen - The generator to use to generate load. (DocumentGenerator)
        op_type - "create", "read", "update", or "delete" (String)
        exp - The expiration for the items if updated or created (int)
        kv_store - The index of the bucket's kv_store to use. (int)

    Returns:
        A list of all of the tasks created.
    """
    def _async_load_all_buckets(self, server, kv_gen, op_type, exp, kv_store=1, flag=0, only_store_hash=True, batch_size=1, pause_secs=1, timeout_secs=30):
        tasks = []
        for bucket in self.buckets:
            gen = copy.deepcopy(kv_gen)
            tasks.append(self.cluster.async_load_gen_docs(server, bucket.name, gen,
                                                          bucket.kvs[kv_store],
                                                          op_type, exp, flag, only_store_hash, batch_size, pause_secs, timeout_secs))
        return tasks

    """Synchronously applys load generation to all bucekts in the cluster.

    Args:
        server - A server in the cluster. (TestInputServer)
        kv_gen - The generator to use to generate load. (DocumentGenerator)
        op_type - "create", "read", "update", or "delete" (String)
        exp - The expiration for the items if updated or created (int)
        kv_store - The index of the bucket's kv_store to use. (int)
    """
    def _load_all_buckets(self, server, kv_gen, op_type, exp, kv_store=1, flag=0, only_store_hash=True, batch_size=1000, pause_secs=1, timeout_secs=30):
        tasks = self._async_load_all_buckets(server, kv_gen, op_type, exp, kv_store, flag, only_store_hash, batch_size, pause_secs, timeout_secs)
        for task in tasks:
            task.result()

    """Waits for queues to drain on all servers and buckets in a cluster.

    A utility function that waits for all of the items loaded to be persisted
    and replicated.

    Args:
        servers - A list of all of the servers in the cluster. ([TestInputServer])
        ep_queue_size - expected ep_queue_size (int)
        ep_flusher_todo - expected ep_flusher_todo (int)
        ep_queue_size_cond - condition for comparing (str)
        timeout - Waiting the end of the thread. (str)
    """
    def _wait_for_stats_all_buckets(self, servers, ep_queue_size=0, ep_flusher_todo=0, \
                                     ep_queue_size_cond='==', timeout=360):
        tasks = []
        for server in servers:
            for bucket in self.buckets:
                tasks.append(self.cluster.async_wait_for_stats([server], bucket, '',
                                   'ep_queue_size', ep_queue_size_cond, ep_queue_size))
                tasks.append(self.cluster.async_wait_for_stats([server], bucket, '',
                                   'ep_flusher_todo', '==', ep_flusher_todo))
        for task in tasks:
            task.result(timeout)

    """Verifies data on all of the nodes in a cluster.

    Verifies all of the data in a specific kv_store index for all buckets in
    the cluster.

    Args:
        server - A server in the cluster. (TestInputServer)
        kv_store - The kv store index to check. (int)
        timeout - Waiting the end of the thread. (str)
    """
    def _verify_all_buckets(self, server, kv_store=1, timeout=180, max_verify=None, only_store_hash=True, batch_size=1000):
        tasks = []
        for bucket in self.buckets:
            tasks.append(self.cluster.async_verify_data(server, bucket, bucket.kvs[kv_store], max_verify, only_store_hash, batch_size))
        for task in tasks:
            task.result(timeout)


    def disable_compaction(self, server=None, bucket="default"):

        server = server or self.servers[0]
        new_config = {"viewFragmntThresholdPercentage" : None,
                      "dbFragmentThresholdPercentage" :  None,
                      "dbFragmentThreshold" : None,
                      "viewFragmntThreshold" : None}
        self.cluster.modify_fragmentation_config(server, new_config, bucket)

    def async_create_views(self, server, design_doc_name, views, bucket="default", with_query=True):
        tasks = []
        if len(views):
            for view in views:
                t_ = self.cluster.async_create_view(server, design_doc_name, view, bucket, with_query)
                tasks.append(t_)
        else:
            t_ = self.cluster.async_create_view(server, design_doc_name, None, bucket, with_query)
            tasks.append(t_)
        return tasks

    def create_views(self, server, design_doc_name, views, bucket="default", timeout=None):
        if len(views):
            for view in views:
                self.cluster.create_view(server, design_doc_name, view, bucket, timeout)
        else:
            self.cluster.create_view(server, design_doc_name, None, bucket, timeout)

    def make_default_views(self, prefix, count, is_dev_ddoc=False, different_map=False):
        ref_view = self.default_view
        ref_view.name = (prefix, ref_view.name)[prefix is None]
        if different_map:
            views = []
            for i in xrange(count):
                views.append(View(ref_view.name + str(i),
                                  'function (doc, meta) {'
                                  'emit(meta.id, "emitted_value%s");}' % str(i),
                                  None, is_dev_ddoc))
            return views
        else:
            return [View(ref_view.name + str(i), ref_view.map_func, None, is_dev_ddoc) for i in xrange(count)]

    def _load_doc_data_all_buckets(self, data_op="create", batch_size=1000, gen_load=None):
        # initialize the template for document generator
        age = range(5)
        first = ['james', 'sharon']
        template = '{{ "mutated" : 0, "age": {0}, "first_name": "{1}" }}'
        if gen_load is None:
            gen_load = DocumentGenerator('test_docs', template, age, first, start=0, end=self.num_items)

        self.log.info("%s %s documents..." % (data_op, self.num_items))
        self._load_all_buckets(self.master, gen_load, data_op, 0, batch_size=batch_size)
        return gen_load

    def verify_cluster_stats(self, servers=None, master=None, max_verify=None, timeout=None, check_items=True):
        if servers is None:
            servers = self.servers
        if master is None:
            master = self.master
        if max_verify is None:
            max_verify = self.max_verify
        self._wait_for_stats_all_buckets(servers, timeout=timeout)
        if check_items:
            self._verify_all_buckets(master, timeout=timeout, max_verify=max_verify)
            self._verify_stats_all_buckets(servers)
            # verify that curr_items_tot corresponds to sum of curr_items from all nodes
            verified = True
            for bucket in self.buckets:
                verified &= RebalanceHelper.wait_till_total_numbers_match(master, bucket)
            self.assertTrue(verified, "Lost items!!! Replication was completed but sum(curr_items) don't match the curr_items_total")
        else:
            self.log.warn("verification of items was omitted")

    def _stats_befor_warmup(self, bucket_name):
        if not self.access_log:
            self.stat_str = ""
        else:
            self.stat_str = "warmup"
        self.stats_monitor = self.input.param("stats_monitor", "curr_items_tot")
        self.stats_monitor = self.stats_monitor.split(";")
        for server in self.servers[:self.nodes_init]:
            mc_conn = MemcachedClientHelper.direct_client(server, bucket_name, self.timeout)
            self.pre_warmup_stats["{0}:{1}".format(server.ip, server.port)] = {}
            for stat_to_monitor in self.stats_monitor:
                self.pre_warmup_stats["%s:%s" % (server.ip, server.port)][stat_to_monitor] = mc_conn.stats(self.stat_str)[stat_to_monitor]
                self.pre_warmup_stats["%s:%s" % (server.ip, server.port)]["uptime"] = mc_conn.stats("")["uptime"]
                self.pre_warmup_stats["%s:%s" % (server.ip, server.port)]["curr_items_tot"] = mc_conn.stats("")["curr_items_tot"]
                self.log.info("memcached %s:%s has %s value %s" % (server.ip, server.port, stat_to_monitor , mc_conn.stats(self.stat_str)[stat_to_monitor]))
            mc_conn.close()

    def _kill_nodes(self, nodes, bucket_name):
        is_partial = self.input.param("is_partial", "True")
        _nodes = []
        if len(self.servers) > 1 :
         skip = 2
        else:
         skip = 1
        if is_partial:
         _nodes = nodes[:len(nodes):skip]
        else:
         _nodes = nodes

        for node in _nodes:
         _node = {"ip": node.ip, "port": node.port, "username": self.servers[0].rest_username,
                  "password": self.servers[0].rest_password}
         _mc = MemcachedClientHelper.direct_client(_node, bucket_name)
         self.log.info("restarted the node %s:%s" % (node.ip, node.port))
         pid = _mc.stats()["pid"]
         node_rest = RestConnection(_node)
         command = "os:cmd(\"kill -9 {0} \")".format(pid)
         self.log.info(command)
         killed = node_rest.diag_eval(command)
         self.log.info("killed ??  {0} ".format(killed))
         _mc.close()

    def _restart_memcache(self, bucket_name):
        rest = RestConnection(self.master)
        nodes = rest.node_statuses()
        self._kill_nodes(nodes, bucket_name)
        start = time.time()
        memcached_restarted = False
        for server in self.servers[:self.nodes_init]:
            mc = None
            while time.time() - start < 60:
                try:
                    mc = MemcachedClientHelper.direct_client(server, bucket_name)
                    stats = mc.stats()
                    new_uptime = int(stats["uptime"])
                    self.log.info("warmutime%s:%s" % (new_uptime, self.pre_warmup_stats["%s:%s" % (server.ip, server.port)]["uptime"]))
                    if new_uptime < self.pre_warmup_stats["%s:%s" % (server.ip, server.port)]["uptime"]:
                        self.log.info("memcached restarted...")
                        memcached_restarted = True
                        break;
                except Exception:
                    self.log.error("unable to connect to %s:%s" % (server.ip, server.port))
                    if mc:
                        mc.close()
                    time.sleep(1)
            if not memcached_restarted:
                self.fail("memcached did not start %s:%s" % (server.ip, server.port))

    def perform_verify_queries(self, num_views, prefix, ddoc_name, query, wait_time=120,
                               bucket="default", expected_rows=None, retry_time=2):
        tasks = []
        if expected_rows is None:
            expected_rows = self.num_items
        for i in xrange(num_views):
            tasks.append(self.cluster.async_query_view(self.servers[0], prefix + ddoc_name,
                                                       self.default_view_name + str(i), query,
                                                       expected_rows, bucket, retry_time))
        try:
            for task in tasks:
                task.result(wait_time)
        except Exception as e:
            print e;
            for task in tasks:
                task.cancel()
            raise Exception("unable to get expected results for view queries during {0} sec".format(wait_time))
