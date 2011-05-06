import base64
import json
import urllib
import httplib2
import socket
import time
import logger
from exception import ServerAlreadyJoinedException, ServerUnavailableException, InvalidArgumentException
from membase.api.exception import BucketCreationException, ServerJoinException

log = logger.Logger.get_logger()
#helper library methods built on top of RestConnection interface
class RestHelper(object):
    def __init__(self, rest_connection):
        self.rest = rest_connection

    def is_ns_server_running(self, timeout_in_seconds=40):
        end_time = time.time() + timeout_in_seconds
        while time.time() <= end_time:
            try:
                if self.is_cluster_healthy():
                    return True
            except ServerUnavailableException:
                time.sleep(1)
        msg = 'unable to connect to the node {0} even after waiting {1} seconds'
        log.info(msg.format(self.rest.ip,))
        return False


    def is_cluster_healthy(self):
        #get the nodes and verify that all the nodes.status are healthy
        nodes = self.rest.node_statuses()
        return all(node.status == 'healthy' for node in nodes)


    def is_cluster_rebalanced(self):
        #get the nodes and verify that all the nodes.status are healthy
        return self.rest.rebalance_statuses()

    #this method will rebalance the cluster by passing the remote_node as
    #ejected node
    def remove_nodes(self, knownNodes, ejectedNodes):
        self.rest.rebalance(knownNodes, ejectedNodes)
        return self.rest.monitorRebalance()

    def bucket_exists(self, bucket):
        buckets = self.rest.get_buckets()
        names = [item.name for item in buckets]
        log.info("existing buckets : {0}".format(names))
        for item in buckets:
            if item.name == bucket:
                log.info("found bucket {0}".format(bucket))
                return True
        return False

    def wait_for_replication(self, timeout_in_seconds=120):
        end_time = time.time() + timeout_in_seconds
        while time.time() <= end_time:
            if self.all_nodes_replicated():
                break
            time.sleep(1)
        log.info('replication state : {0}'.format(self.all_nodes_replicated()))
        return self.all_nodes_replicated()

    def all_nodes_replicated(self):
        nodes = self.rest.node_statuses()
        for node in nodes:
            if node.replication != 1.0:
                return False
        return True


class RestConnection(object):
    #port is always 8091
    def __init__(self, ip, username='Administrator', password='password'):
        #throw some error here if the ip is null ?
        self.ip = ip
        self.username = username
        self.password = password
        self.baseUrl = "http://{0}:8091/".format(self.ip)

    def __init__(self, serverInfo):
        #throw some error here if the ip is null ?
        self.ip = serverInfo.ip
        self.username = serverInfo.rest_username
        self.password = serverInfo.rest_password
        self.baseUrl = "http://{0}:8091/".format(self.ip)


    #authorization mut be a base64 string of username:password
    def _create_headers(self):
        authorization = base64.encodestring('%s:%s' % (self.username, self.password))
        return {'Content-Type': 'application/x-www-form-urlencoded',
                'Authorization': 'Basic %s' % authorization,
                'Accept': '*/*'}

    def init_cluster(self, username='Administrator', password='password'):
        api = self.baseUrl + 'settings/web'
        params = urllib.urlencode({'port': '8091',
                                   'username': username,
                                   'password': password})

        try:
            response, content = httplib2.Http().request(api, 'POST', params, headers=self._create_headers())
            if response['status'] == '400':
                log.error('init_cluster error {0}'.format(content))
                return False
            elif response['status'] == '200':
                return True
        except socket.error as socket_error:
            log.error(socket_error)
            raise ServerUnavailableException(ip=self.ip)
        except httplib2.ServerNotFoundError:
            raise ServerUnavailableException(ip=self.ip)

    def init_cluster_port(self):
        api = self.baseUrl + 'settings/web'
        params = urllib.urlencode({'port': '8091',
                                   'username': 'Administrator',
                                   'password': 'password'})
        try:
            response, content = httplib2.Http().request(api, 'GET', params, headers=self._create_headers())
            if response['status'] == '400':
                log.error('init_cluster_port error {0}'.format(content))
                return False
            elif response['status'] == '200':
                return True
        except socket.error:
            raise ServerUnavailableException(ip=self.ip)
        except httplib2.ServerNotFoundError:
            raise ServerUnavailableException(ip=self.ip)

    def init_cluster_memoryQuota(self, username='Administrator',
                                 password='password',
                                 memoryQuota=200):
        api = self.baseUrl + 'pools/default'
        params = urllib.urlencode({'memoryQuota': memoryQuota,
                                   'username': username,
                                   'password': password})
        try:
            response, content = httplib2.Http().request(api, 'GET', params, headers=self._create_headers())
            if response['status'] == '400':
                log.error('init_cluster_memoryQuota error {0}'.format(content))
                return False
            elif response['status'] == '200':
                return True
        except socket.error:
            raise ServerUnavailableException(ip=self.ip)
        except httplib2.ServerNotFoundError:
            raise ServerUnavailableException(ip=self.ip)

    #params serverIp : the server to add to this cluster
    #raises exceptions when
    #unauthorized user
    #server unreachable
    #can't add the node to itself ( TODO )
    #server already added
    #returns otpNode
    def add_node(self, user='', password='', remoteIp='' ):
        otpNode = None
        log.info('adding remote node : {0} to this cluster @ : {1}'\
        .format(remoteIp, self.ip))
        api = self.baseUrl + 'controller/addNode'
        params = urllib.urlencode({'hostname': remoteIp,
                                   'user': user,
                                   'password': password})
        try:
            response, content = httplib2.Http().request(api, 'POST', params,
                                                        headers=self._create_headers())
            log.info('add_node response : {0} content : {1}'.format(response, content))
            if response['status'] == '400':
                log.error('error occured while adding remote node: {0}'.format(remoteIp))
                if content.find('Prepare join failed. Node is already part of cluster') >= 0:
                    raise ServerAlreadyJoinedException(nodeIp=self.ip,
                                                       remoteIp=remoteIp)
                elif content.find('Prepare join failed. Joining node to itself is not allowed') >= 0:
                    raise ServerJoinException(nodeIp=self.ip,
                                              remoteIp=remoteIp)
                else:
                    #todo: raise an exception here
                    log.error('get_pools error : {0}'.format(content))
            elif response['status'] == '200':
                log.info('added node {0} : response {1}'.format(remoteIp, content))
                dict = json.loads(content)
                otpNodeId = dict['otpNode']
                otpNode = OtpNode(otpNodeId)
                if otpNode.ip == '127.0.0.1':
                    otpNode.ip = self.ip
            return otpNode
        except socket.error:
            raise ServerUnavailableException(ip=self.ip)
        except httplib2.ServerNotFoundError:
            raise ServerUnavailableException(ip=self.ip)


    def eject_node(self, user='', password='', otpNode=None ):
        if not otpNode:
            log.error('otpNode parameter required')
            return False
        try:
            api = self.baseUrl + 'controller/ejectNode'
            params = urllib.urlencode({'otpNode': otpNode,
                                       'user': user,
                                       'password': password})
            response, content = httplib2.Http().request(api, 'POST', params,
                                                        headers=self._create_headers())
            if response['status'] == '400':
                if content.find('Prepare join failed. Node is already part of cluster') >= 0:
                    raise ServerAlreadyJoinedException(nodeIp=self.ip,
                                                       remoteIp=otpNode)
                else:
                    # todo : raise an exception here
                    log.error('eject_node error {0}'.format(content))
            elif response['status'] == '200':
                log.info('ejectNode successful')
            return True
        except socket.error:
            raise ServerUnavailableException(ip=self.ip)
        except httplib2.ServerNotFoundError:
            raise ServerUnavailableException(ip=self.ip)

    def fail_over(self, otpNode=None ):
        if not otpNode:
            log.error('otpNode parameter required')
            return False
        try:
            api = self.baseUrl + 'controller/failOver'
            params = urllib.urlencode({'otpNode': otpNode})
            response, content = httplib2.Http().request(api, 'POST', params,
                                                        headers=self._create_headers())
            if response['status'] == '400':
                log.error('fail_over error : {0}'.format(content))
            elif response['status'] == '200':
                log.info('fail_over successful')
            return True
        except socket.error:
            raise ServerUnavailableException(ip=self.ip)
        except httplib2.ServerNotFoundError:
            raise ServerUnavailableException(ip=self.ip)


    def rebalance(self, otpNodes, ejectedNodes):
        knownNodes = ''
        index = 0
        for node in otpNodes:
            if not index:
                knownNodes += node
            else:
                knownNodes += ',' + node
            index += 1
        ejectedNodesString = ''
        index = 0
        for node in ejectedNodes:
            if not index:
                ejectedNodesString += node
            else:
                ejectedNodesString += ',' + node
            index += 1

        params = urllib.urlencode({'knownNodes': knownNodes,
                                   'ejectedNodes': ejectedNodesString,
                                   'user': self.username,
                                   'password': self.password})
        log.info('rebalanace params : {0}'.format(params))

        api = self.baseUrl + "controller/rebalance"
        try:
            response, content = httplib2.Http().request(api, 'POST', params,
                                                        headers=self._create_headers())
            #if status is 200 then it was a success otherwise it was a failure
            log.info('rebalance: {0}'.format(response))
            log.info('rebalance: {0}'.format(content))
            if response['status'] == '400':
                #extract the error
                raise InvalidArgumentException('controller/rebalance',
                                               parameters=params)
            elif response['status'] == '200':
                log.info('rebalance operation started')
            return True
        except socket.error:
            raise ServerUnavailableException(ip=self.ip)
        except httplib2.ServerNotFoundError:
            raise ServerUnavailableException(ip=self.ip)

    def monitorRebalance(self):
        start = time.time()
        progress = 0
        while progress is not -1 and progress is not 100:
            progress = self._rebalance_progress()
            #sleep for 2 seconds
            time.sleep(2)

        if progress == -1:
            return False
        else:
            duration = time.time() - start
            log.info('rebalance progress took {0} seconds '.format(duration))
            return True

    def _rebalance_progress(self):
        percentage = -1
        api = self.baseUrl + "pools/default/rebalanceProgress"
        try:
            response, content = httplib2.Http().request(api, 'GET',
                                                        headers=self._create_headers())
            #if status is 200 then it was a success otherwise it was a failure
            if response['status'] == '400':
                #extract the error , how ?
                log.info('unable to obtain rebalance progress ?')
            elif response['status'] == '200':
                parsed = json.loads(content)
                if parsed.has_key('status'):
                    if parsed.has_key('errorMessage'):
                        log.error('{0} - rebalance failed'.format(parsed))
                    elif parsed['status'] == 'running':
                        for key in parsed:
                            if key.find('ns_1') >= 0:
                                ns_1_dictionary = parsed[key]
                                percentage = ns_1_dictionary['progress'] * 100
                                log.info('rebalance percentage : {0} %' .format(percentage))
                                break
                    else:
                        percentage = 100
            return percentage
        except socket.error:
            raise ServerUnavailableException(ip=self.ip)
        except httplib2.ServerNotFoundError:
            raise ServerUnavailableException(ip=self.ip)
            #if status is none , is there an errorMessage
            #convoluted logic which figures out if the rebalance failed or suceeded

    def rebalance_statuses(self):
        api = self.baseUrl + 'pools/rebalanceStatuses'
        try:
            response, content = httplib2.Http().request(api, 'GET', headers=self._create_headers())
            #if status is 200 then it was a success otherwise it was a failure
            if response['status'] == '400':
                #extract the error
                log.error('unable to retrieve nodesStatuses')
            elif response['status'] == '200':
                parsed = json.loads(content)
                rebalanced = parsed['balanced']
                return rebalanced

        except socket.error:
            raise ServerUnavailableException(ip=self.ip)
        except httplib2.ServerNotFoundError:
            raise ServerUnavailableException(ip=self.ip)

    def log_client_error(self, post):
        api = self.baseUrl + 'logClientError'
        try:
            response, content = httplib2.Http().request(api, 'POST', body=post, headers=self._create_headers())
            #if status is 200 then it was a success otherwise it was a failure
            if response['status'] == '400':
                #extract the error
                log.error('unable to logClientError')
        except socket.error:
            raise ServerUnavailableException(ip=self.ip)
        except httplib2.ServerNotFoundError:
            raise ServerUnavailableException(ip=self.ip)

    #retuns node data for this host
    def get_nodes_self(self):
        node = None
        api = self.baseUrl + 'nodes/self'
        try:
            response, content = httplib2.Http().request(api, 'GET', headers=self._create_headers())
            #if status is 200 then it was a success otherwise it was a failure
            if response['status'] == '400':
                #extract the error
                log.error('unable to retrieve nodesStatuses')
            elif response['status'] == '200':
                parsed = json.loads(content)
                node = RestParser().parse_get_nodes_response(parsed)
        except socket.error:
            raise ServerUnavailableException(ip=self.ip)
        except httplib2.ServerNotFoundError:
            raise ServerUnavailableException(ip=self.ip)
        return node

    def node_statuses(self):
        nodes = []
        api = self.baseUrl + 'nodeStatuses'
        try:
            response, content = httplib2.Http().request(api, 'GET', headers=self._create_headers())
            #if status is 200 then it was a success otherwise it was a failure
            if response['status'] == '400':
                #extract the error
                log.error('unable to retrieve nodesStatuses')
            elif response['status'] == '200':
                parsed = json.loads(content)
                for key in parsed:
                    #each key contain node info
                    value = parsed[key]
                    #get otp,get status
                    node = OtpNode(id=value['otpNode'],
                                   status=value['status'])
                    if node.ip == '127.0.0.1':
                        node.ip = self.ip
                    node.replication = value['replication']
                    nodes.append(node)
                    #let's also populate the membase_version_info
            return nodes
        except socket.error:
            raise ServerUnavailableException(ip=self.ip)
        except httplib2.ServerNotFoundError:
            raise ServerUnavailableException(ip=self.ip)

    def get_pools_info(self):
        api = self.baseUrl + 'pools'
        try:
            response, content = httplib2.Http().request(api, 'GET', headers=self._create_headers())
            if response['status'] == '400':
                log.error('get_pools error {0}'.format(content))
            elif response['status'] == '200':
                parsed = json.loads(content)
                return parsed
        except socket.error:
            raise ServerUnavailableException(ip=self.ip)
        except httplib2.ServerNotFoundError:
            raise ServerUnavailableException(ip=self.ip)


    def get_pools(self):
        version = None
        api = self.baseUrl + 'pools'
        try:
            response, content = httplib2.Http().request(api, 'GET', headers=self._create_headers())
            if response['status'] == '400':
                log.error('get_pools error {0}'.format(content))
            elif response['status'] == '200':
                parsed = json.loads(content)
                version = MembaseServerVersion(parsed['implementationVersion'], parsed['componentsVersion'])
            return version
        except socket.error:
            raise ServerUnavailableException(ip=self.ip)
        except httplib2.ServerNotFoundError:
            raise ServerUnavailableException(ip=self.ip)

    def get_buckets(self):
        #get all the buckets
        buckets = []
        api = '{0}{1}'.format(self.baseUrl, 'pools/default/buckets/')
        try:
            response, content = httplib2.Http().request(api, 'GET', headers=self._create_headers())
            if response['status'] == '400':
                log.error('get_buckets error {0}'.format(content))
            elif response['status'] == '200':
                parsed = json.loads(content)
                # for each elements
                for item in parsed:
                    bucketInfo = RestParser().parse_get_bucket_json(item)
                    buckets.append(bucketInfo)
                return buckets
        except socket.error:
            raise ServerUnavailableException(ip=self.ip)
        except httplib2.ServerNotFoundError:
            raise ServerUnavailableException(ip=self.ip)
        return buckets

    def get_bucket_stats_for_node(self, bucket='default', node_ip=None):
        if not Node:
            log.error('node_ip not specified')
            return None
        api = "{0}{1}{2}{3}{4}{5}".format(self.baseUrl, 'pools/default/buckets/',
                                          bucket, "/nodes/", node_ip, ":8091/stats")
        try:
            response, content = httplib2.Http().request(api, 'GET', headers=self._create_headers())
            if response['status'] == '400':
                log.error('get_bucket error {0}'.format(content))
            elif response['status'] == '200':
                parsed = json.loads(content)
                #let's just return the samples
                #we can also go through the list
                #and for each item remove all items except the first one ?
                op = parsed["op"]
                samples = op["samples"]
                stats = {}
                #for each sample
                for stat_name in samples:
                    stats[stat_name] = samples[stat_name][0]
                return stats
        except socket.error:
            raise ServerUnavailableException(ip=self.ip)
        except httplib2.ServerNotFoundError:
            raise ServerUnavailableException(ip=self.ip)

    def get_bucket_stats(self, bucket='default'):
        api = "{0}{1}{2}{3}".format(self.baseUrl, 'pools/default/buckets/', bucket, "/stats")
        try:
            response, content = httplib2.Http().request(api, 'GET', headers=self._create_headers())
            if response['status'] == '400':
                log.error('get_bucket error {0}'.format(content))
            elif response['status'] == '200':
                parsed = json.loads(content)
                #let's just return the samples
                #we can also go through the list
                #and for each item remove all items except the first one ?
                op = parsed["op"]
                samples = op["samples"]
                stats = {}
                #for each sample
                for stat_name in samples:
                    #pick the last item ?
                    last_sample = len(samples[stat_name]) - 1
                    stats[stat_name] = samples[stat_name][last_sample]
                return stats
        except socket.error:
            raise ServerUnavailableException(ip=self.ip)
        except httplib2.ServerNotFoundError:
            raise ServerUnavailableException(ip=self.ip)

    def get_bucket(self, bucket='default'):
        bucketInfo = None
        api = '{0}{1}{2}'.format(self.baseUrl, 'pools/default/buckets/', bucket)
        try:
            response, content = httplib2.Http().request(api, 'GET', headers=self._create_headers())
            if response['status'] == '400':
                log.error('get_bucket error {0}'.format(content))
            elif response['status'] == '200':
                bucketInfo = RestParser().parse_get_bucket_response(content)
            #                log.debug('set stats to {0}'.format(bucketInfo.stats.ram))
        except socket.error:
            raise ServerUnavailableException(ip=self.ip)
        except httplib2.ServerNotFoundError:
            raise ServerUnavailableException(ip=self.ip)
        return bucketInfo

    def delete_bucket(self, bucket='default'):
        api = '{0}{1}{2}'.format(self.baseUrl, '/pools/default/buckets/', bucket)
        try:
            response, content = httplib2.Http().request(api, 'DELETE', headers=self._create_headers())
            if response['status'] == '200':
                return True
            else:
                log.error('delete_bucket error {0}'.format(content))
        except socket.error:
            raise ServerUnavailableException(ip=self.ip)
        except httplib2.ServerNotFoundError:
            raise ServerUnavailableException(ip=self.ip)
        return False

    # figure out the proxy port
    def create_bucket(self, bucket='',
                      ramQuotaMB=1,
                      authType='none',
                      saslPassword='',
                      replicaNumber=1,
                      proxyPort=11211,
                      bucketType='membase'):
        api = '{0}{1}'.format(self.baseUrl, '/pools/default/buckets')
        params = urllib.urlencode({})
        #this only works for default bucket ?
        if bucket == 'default':
            params = urllib.urlencode({'name': bucket,
                                       'authType': 'sasl',
                                       'saslPassword': saslPassword,
                                       'ramQuotaMB': ramQuotaMB,
                                       'replicaNumber': replicaNumber,
                                       'proxyPort': proxyPort,
                                       'bucketType': bucketType})

        elif authType == 'none':
            params = urllib.urlencode({'name': bucket,
                                       'ramQuotaMB': ramQuotaMB,
                                       'authType': authType,
                                       'replicaNumber': replicaNumber,
                                       'proxyPort': proxyPort,
                                       'bucketType': bucketType})

        elif authType == 'sasl':
            params = urllib.urlencode({'name': bucket,
                                       'ramQuotaMB': ramQuotaMB,
                                       'authType': authType,
                                       'saslPassword': saslPassword,
                                       'replicaNumber': replicaNumber,
                                       'proxyPort': 11211,
                                       'bucketType': bucketType})

        try:
            log.info("{0} with param: {1}".format(api, params))
            response, content = httplib2.Http().request(api, 'POST', params,
                                                        headers=self._create_headers())
            log.info(content)
            log.info(response)
            status = response['status']
            if status == '200' or status == '201' or status == '202':
                return True
            else:
                log.error('create_bucket error {0} {1}'.format(content, response))
                raise BucketCreationException(ip=self.ip, bucket_name=bucket)
        except socket.error:
            raise ServerUnavailableException(ip=self.ip)
        except httplib2.ServerNotFoundError:
            raise ServerUnavailableException(ip=self.ip)


class MembaseServerVersion:
    def __init__(self, implementationVersion='', componentsVersion=''):
        self.implementationVersion = implementationVersion
        self.componentsVersion = componentsVersion

#this class will also contain more node related info
class OtpNode(object):
    def __init__(self, id='', status=''):
        self.id = id
        self.ip = ''
        self.replication = ''
        #extract ns ip from the otpNode string
        #its normally ns_1@10.20.30.40
        if id.find('@') >= 0:
            self.ip = id[id.index('@') + 1:]
        self.status = status


class NodeInfo(object):
    def __init__(self):
        self.availableStorage = None # list
        self.memoryQuota = None


class NodeDataStorage(object):
    def __init__(self):
        self.type = '' #hdd or ssd
        self.path = ''
        self.quotaMb = ''
        self.state = '' #ok

    def __str__(self):
        return '{0}'.format({'type': self.type,
                             'path': self.path,
                             'quotaMb': self.quotaMb,
                             'state': self.state})


class NodeDiskStorage(object):
    def __init__(self):
        self.type = 0
        self.path = ''
        self.sizeKBytes = 0
        self.usagePercent = 0


class Bucket(object):
    def __init__(self):
        self.name = ''
        self.type = ''
        self.nodes = None
        self.stats = None
        self.servers = []
        self.vbuckets = []
        self.forward_map = []


class Node(object):
    def __init__(self):
        self.uptime = 0
        self.memoryTotal = 0
        self.memoryFree = 0
        self.mcdMemoryReserved = 0
        self.mcdMemoryAllocated = 0
        self.status = ""
        self.hostname = ""
        self.clusterCompatibility = ""
        self.version = ""
        self.os = ""
        self.ports = []
        self.availableStorage = []
        self.storage = []
        self.memoryQuota = 0


class NodePort(object):
    def __init__(self):
        self.proxy = 0
        self.direct = 0


class BucketStats(object):
    def __init__(self):
        self.quotaPercentUsed = 0
        self.opsPerSec = 0
        self.diskFetches = 0
        self.itemCount = 0
        self.diskUsed = 0
        self.memUsed = 0
        self.ram = 0


class vBucket(object):
    def __init__(self):
        self.master = ''
        self.replica = []


class RestParser(object):
    def parse_get_nodes_response(self, parsed):
        node = Node()
        node.uptime = parsed['uptime']
        node.memoryFree = parsed['memoryFree']
        node.memoryTotal = parsed['memoryTotal']
        node.mcdMemoryAllocated = parsed['mcdMemoryAllocated']
        node.mcdMemoryReserved = parsed['mcdMemoryReserved']
        node.status = parsed['status']
        node.hostname = parsed['hostname']
        node.clusterCompatibility = parsed['clusterCompatibility']
        node.version = parsed['version']
        node.os = parsed['os']
        # memoryQuota
        if 'memoryQuota' in parsed:
            node.memoryQuota = parsed['memoryQuota']
        if 'availableStorage' in parsed:
            availableStorage = parsed['availableStorage']
            for key in availableStorage:
                #let's assume there is only one disk in each noce
                dict = parsed['availableStorage']
                if 'path' in dict and 'sizeKBytes' in dict and 'usagePercent' in dict:
                    diskStorage = NodeDiskStorage()
                    diskStorage.path = dict['path']
                    diskStorage.sizeKBytes = dict['sizeKBytes']
                    diskStorage.type = key
                    diskStorage.usagePercent = dict['usagePercent']
                    node.availableStorage.append(diskStorage)
                    log.info(diskStorage)

        if 'storage' in parsed:
            storage = parsed['storage']
            for key in storage:
                disk_storage_list = storage[key]
                for dict in disk_storage_list:
                    if 'path' in dict and 'state' in dict and 'quotaMb' in dict:
                        dataStorage = NodeDataStorage()
                        dataStorage.path = dict['path']
                        dataStorage.quotaMb = dict['quotaMb']
                        dataStorage.state = dict['state']
                        dataStorage.type = key
                        node.storage.append(dataStorage)
        return node

    def parse_get_bucket_response(self, response):
        parsed = json.loads(response)
        return self.parse_get_bucket_json(parsed)

    def parse_get_bucket_json(self, parsed):
        bucket = Bucket()
        bucket.name = parsed['name']
        bucket.type = parsed['bucketType']
        bucket.nodes = list()
        if 'vBucketServerMap' in parsed:
            vBucketServerMap = parsed['vBucketServerMap']
            serverList = vBucketServerMap['serverList']
            bucket.servers.extend(serverList)
            #vBucketMapForward
            if 'vBucketMapForward' in vBucketServerMap:
                #let's gather the forward map
                vBucketMapForward = vBucketServerMap['vBucketMapForward']
                for vbucket in vBucketMapForward:
                    #there will be n number of replicas
                    vbucketInfo = vBucket()
                    vbucketInfo.master = serverList[vbucket[0]]
                    if vbucket:
                        for i in range(1, len(vbucket)):
                            if vbucket[i] != -1:
                                vbucketInfo.replica.append(serverList[vbucket[i]])
                    bucket.forward_map.append(vbucketInfo)
            vBucketMap = vBucketServerMap['vBucketMap']
            for vbucket in vBucketMap:
                #there will be n number of replicas
                vbucketInfo = vBucket()
                vbucketInfo.master = serverList[vbucket[0]]
                if vbucket:
                    for i in range(1, len(vbucket)):
                        if vbucket[i] != -1:
                            vbucketInfo.replica.append(serverList[vbucket[i]])
                bucket.vbuckets.append(vbucketInfo)
                #now go through each vbucket and populate the info
            #who is master , who is replica
        # get the 'storageTotals'
        log.debug('read {0} vbuckets'.format(len(bucket.vbuckets)))
        stats = parsed['basicStats']
        #vBucketServerMap
        bucketStats = BucketStats()
        log.debug('stats:{0}'.format(stats))
        bucketStats.quotaPercentUsed = stats['quotaPercentUsed']
        bucketStats.opsPerSec = stats['opsPerSec']
        if 'diskFetches' in stats:
            bucketStats.diskFetches = stats['diskFetches']
        bucketStats.itemCount = stats['itemCount']
        bucketStats.diskUsed = stats['diskUsed']
        bucketStats.memUsed = stats['memUsed']
        quota = parsed['quota']
        bucketStats.ram = quota['ram']
        bucket.stats = bucketStats
        nodes = parsed['nodes']
        for nodeDictionary in nodes:
            node = Node()
            node.uptime = nodeDictionary['uptime']
            node.memoryFree = nodeDictionary['memoryFree']
            node.memoryTotal = nodeDictionary['memoryTotal']
            node.mcdMemoryAllocated = nodeDictionary['mcdMemoryAllocated']
            node.mcdMemoryReserved = nodeDictionary['mcdMemoryReserved']
            node.status = nodeDictionary['status']
            node.hostname = nodeDictionary['hostname']
            node.clusterCompatibility = nodeDictionary['clusterCompatibility']
            node.version = nodeDictionary['version']
            node.os = nodeDictionary['os']
            # todo : node.ports
            bucket.nodes.append(node)
        return bucket

