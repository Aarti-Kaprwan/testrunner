import sys
import time
import datetime
import copy
import os

sys.path = ["../"] + sys.path

import unittest
import logger
from membase.api.rest_client import RestConnection, Bucket, RestHelper
from couchbase.cluster import Cluster
from TestInput import TestInputSingleton
from membase.helper.bucket_helper import BucketOperationHelper
from membase.helper.cluster_helper import ClusterOperationHelper

class initialize(unittest.TestCase):
    def setUp(self):
        self._log = logger.Logger.get_logger()
        self._input = TestInputSingleton.input
        self._clusters_dic = self._input.clusters
        self._clusters_keys_olst = range(len(self._clusters_dic))
        self._buckets = []
        self._default_bucket = self._input.param("default_bucket", True)
        if self._default_bucket:
            self.default_bucket_name = "default"
        self._standard_buckets = self._input.param("standard_buckets", 0)
        self._sasl_buckets = self._input.param("sasl_buckets",0)
        self._buckets = []
        self._mem_quota_int = 0
        self._num_replicas = self._input.param("replicas", 1)
        self._xdcr = self._input.param("xdcr", False)
        self._rdirection = self._input.param("rdirection","unidirection")
        if self._xdcr:
            #Considering that there be a maximum of 2 clusters for XDCR
            self._s_master = self._clusters_dic[0][0]
            self._d_master = self._clusters_dic[1][0]

    def tearDown(self):
        pass

class SETUP(initialize):
    def setitup(self):
        for key in self._clusters_keys_olst:
            self.set_the_cluster_up(self._clusters_dic[key])
        time.sleep(20)
        if self._xdcr:
            self._link_create_replications(self._s_master, self._d_master, "cluster1")
            if self._rdirection == "bidirection":
                self._link_create_replications(self._d_master, self._s_master, "cluster0")

    def setupXDCR(self):
        self._link_create_replications(self._s_master, self._d_master, "cluster1")
        if self._rdirection == "bidirection":
            self._link_create_replications(self._d_master, self._s_master, "cluster0")

    def terminate(self):
        if self._xdcr:
            self._terminate_replications(self._s_master, "cluster1")
            if self._rdirection == "bidirection":
                self._terminate_replications(self._d_master, "cluster0")
        for key in self._clusters_keys_olst:
            nodes = self._clusters_dic[key]
            for node in nodes:
                rest = RestConnection(node)
                buckets = rest.get_buckets()
                for bucket in buckets:
                    status = rest.delete_bucket(bucket.name)
                    if status:
                        self._log.info('Deleted bucket : {0} from {1}'.format(bucket.name, node.ip))
            rest = RestConnection(nodes[0])
            helper = RestHelper(rest)
            servers = rest.node_statuses()
            master_id = rest.get_nodes_self().id
            if len(nodes) > 1:
                removed = helper.remove_nodes(knownNodes=[node.id for node in servers],
                                          ejectedNodes=[node.id for node in servers if node.id != master_id],
                                          wait_for_rebalance=True   )

    def _terminate_replications(self, master, cluster_name):
        rest = RestConnection(master)
        rest.remove_all_replications()
        os.system("curl --user {0}:{1} -X DELETE http://{2}:{3}/pools/default/remoteClusters/{4}".format(
                    master.rest_username, master.rest_password, master.ip, master.port, cluster_name))

    def set_the_cluster_up(self, nodes):
        self._init_nodes(nodes)
        self._config_cluster(nodes)
        self._create_buckets(nodes)

    def _init_nodes(self, nodes):
        for node in nodes:
            rest = RestConnection(node)
            rest.init_cluster(node.rest_username, node.rest_password)
            info = rest.get_nodes_self()
            quota = int(info.mcdMemoryReserved)
            self._mem_quota_int = quota
            rest.init_cluster_memoryQuota(node.rest_username, node.rest_password, quota)

    def _config_cluster(self, nodes):
        master = nodes[0]
        rest = RestConnection(master)
        for node in nodes[1:]:
            rest.add_node(master.rest_username, master.rest_password,
                          node.ip, node.port)
        servers = rest.node_statuses()
        rest.rebalance(otpNodes=[node.id for node in servers], ejectedNodes=[])
        time.sleep(5)

    def _create_buckets(self, nodes):
        master_node = nodes[0]
        num_buckets = 0
        if self._default_bucket:
            num_buckets += 1
        num_buckets += self._sasl_buckets + self._standard_buckets
        bucket_size = self._get_bucket_size(master_node, nodes, self._mem_quota_int, num_buckets)
        rest = RestConnection(master_node)
        master_id = rest.get_nodes_self().id
        if self._default_bucket:
            rest = RestConnection(nodes[0])
            rest.create_bucket(bucket=self.default_bucket_name,
                               ramQuotaMB=bucket_size,
                               replicaNumber=self._num_replicas,
                               proxyPort=11211,
                               authType="none",
                               saslPassword=None)
            self._buckets.append(self.default_bucket_name)
        if self._sasl_buckets > 0:
            self._create_sasl_buckets(master_node, master_id, bucket_size, password="password")
        if self._standard_buckets > 0:
            self._create_standard_buckets(master_node, master_id, bucket_size)

    def _link_create_replications(self, master_1, master_2, cluster_name):
        rest = RestConnection(master_1)
        rest.add_remote_cluster(master_2.ip, master_2.port, master_1.rest_username,
                                 master_1.rest_password, cluster_name)
        time.sleep(30)
        if len(self._buckets) == 0:
            self._buckets = rest.get_buckets()
        for bucket in set(self._buckets):
            rep_database, rep_id = rest.start_replication("continuous", bucket, cluster_name)

    def _create_sasl_buckets(self, server, server_id, bucket_size, password):
        rest = RestConnection(server)
        for i in range(self._sasl_buckets):
            name = "sasl-" + str(i+1)
            rest.create_bucket(bucket=name,
                               ramQuotaMB=bucket_size,
                               replicaNumber=self._num_replicas,
                               proxyPort=11211,
                               authType="sasl",
                               saslPassword=password)
            self._buckets.append(name)

    def _create_standard_buckets(self, server, server_id, bucket_size):
        rest = RestConnection(server)
        for i in range(self._standard_buckets):
            name = "standard-" + str(i+1)
            rest.create_bucket(bucket=name,
                               ramQuotaMB=bucket_size,
                               replicaNumber=self._num_replicas,
                               proxyPort=11214+i,
                               authType="none",
                               saslPassword=None)
            self._buckets.append(name)

    def _get_bucket_size(self, master_node, nodes, mem_quota, num_buckets, ratio=3.0 / 2.0):
        for node in nodes:
            if node.ip == master_node.ip:
                return int(ratio / float(len(nodes)) / float(num_buckets) * float(mem_quota))
        return int(ratio / float(num_buckets) * float(mem_quota))
