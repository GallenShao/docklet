import sys
if sys.path[0].endswith("worker"):
    sys.path[0] = sys.path[0][:-6]
from utils import env, tools
#config = env.getenv("CONFIG")
config = "/opt/docklet/local/docklet-running.conf"
tools.loadenv(config)
from utils.log import initlogging
initlogging("docklet-taskworker")
from utils.log import logger

from concurrent import futures
import grpc
#from utils.log import logger
#from utils import env
import json,lxc,subprocess,threading,os,time,traceback
from utils import imagemgr,etcdlib,gputools
from utils.lvmtool import sys_run
from worker import ossmounter
from protos import rpc_pb2, rpc_pb2_grpc

_ONE_DAY_IN_SECONDS = 60 * 60 * 24
MAX_RUNNING_TIME = _ONE_DAY_IN_SECONDS

class TaskWorker(rpc_pb2_grpc.WorkerServicer):

    def __init__(self):
        rpc_pb2_grpc.WorkerServicer.__init__(self)
        etcdaddr = env.getenv("ETCD")
        logger.info ("using ETCD %s" % etcdaddr )

        clustername = env.getenv("CLUSTER_NAME")
        logger.info ("using CLUSTER_NAME %s" % clustername )

        # init etcdlib client
        try:
            self.etcdclient = etcdlib.Client(etcdaddr, prefix = clustername)
        except Exception:
            logger.error ("connect etcd failed, maybe etcd address not correct...")
            sys.exit(1)
        else:
            logger.info("etcd connected")

        # get master ip and report port
        [success,masterip] = self.etcdclient.getkey("service/master")
        if not success:
            logger.error("Fail to get master ip address.")
            sys.exit(1)
        else:
            self.master_ip = masterip
            logger.info("Get master ip address: %s" % (self.master_ip))
        self.master_port = env.getenv('BATCH_MASTER_PORT')

        self.imgmgr = imagemgr.ImageMgr()
        self.fspath = env.getenv('FS_PREFIX')
        self.confpath = env.getenv('DOCKLET_CONF')

        self.taskmsgs = []
        self.msgslock = threading.Lock()
        self.report_interval = 2

        self.lock = threading.Lock()
        self.mount_lock = threading.Lock()

        self.gpu_lock = threading.Lock()
        self.gpu_status = {}
        gpus = gputools.get_gpu_status()
        for gpu in gpus:
            self.gpu_status[gpu['id']] = ""

        #self.start_report()
        logger.info('TaskWorker init success')
        
    def add_gpu_device(self, lxcname, gpu_need):
        if gpu_need < 1:
            return [True, ""]
        self.gpu_lock.acquire()
        use_gpus = []
        for gpuid in self.gpu_status.keys():
            if self.gpu_status[gpuid] == "" and gpu_need > 0:
                use_gpus.append(gpuid)
                gpu_need -= 1
        if gpu_need > 0:
            self.gpu_lock.release()
            return [False, "No free GPUs"]
        for gpuid in use_gpus:
            self.gpu_status[gpuid] = lxcname
        try:
            gputools.add_device(lxcname, "/dev/nvidiactl")
            gputools.add_device(lxcname, "/dev/nvidia-uvm")
            for gpuid in use_gpus:
                gputools.add_device(lxcname,"/dev/nvidia"+str(gpuid))
                logger.info("Add gpu:"+str(gpuid) +" to lxc:"+str(lxcname))
        except Exception as e:
            logger.error(traceback.format_exc())
            for gpuid in use_gpus:
                self.gpu_status[gpuid] = ""
            self.gpu_lock.release()
            return [False, "Error occurs when adding gpu device."]

        self.gpu_lock.release()
        return [True, ""]

    def release_gpu_device(self, lxcname):
        self.gpu_lock.acquire()
        for gpuid in self.gpu_status.keys():
            if self.gpu_status[gpuid] == lxcname:
                self.gpu_status[gpuid] = ""
        self.gpu_lock.release()

    #mount_oss
    def mount_oss(self, datapath, mount_info):
        self.mount_lock.acquire()
        try:
            for mount in mount_info:
                provider = mount.provider
                mounter = getattr(ossmounter,provider+"OssMounter",None)
                if mounter is None:
                    self.mount_lock.release()
                    return [False, provider + " doesn't exist!"]
                [success, msg] = mounter.mount_oss(datapath,mount)
                if not success:
                    self.mount_lock.release()
                    return [False, msg]
        except Exception as err:
            self.mount_lock.release()
            logger.error(traceback.format_exc())
            return [False,""]

        self.mount_lock.release()
        return [True,""]

    #umount oss
    def umount_oss(self, datapath, mount_info):
        try:
            for mount in mount_info:
                provider = mount.provider
                mounter = getattr(ossmounter,provider+"OssMounter",None)
                if mounter is None:
                    return [False, provider + " doesn't exist!"]
                [success, msg] = mounter.umount_oss(datapath,mount)
                if not success:
                    return [False, msg]
        except Exception as err:
            logger.error(traceback.format_exc())
            return [False,""]

    def start_vnode(self, request, context):
        logger.info('start vnode with config: ' + str(request))
        taskid = request.taskid
        vnodeid = request.vnodeid

        envs = {}
        envs['taskid'] = str(taskid)
        envs['vnodeid'] = str(vnodeid)
        image = {}
        image['name'] = request.vnode.image.name
        if request.vnode.image.type == rpc_pb2.Image.PRIVATE:
            image['type'] = 'private'
        elif request.vnode.image.type == rpc_pb2.Image.PUBLIC:
            image['type'] = 'public'
        else:
            image['type'] = 'base'
        image['owner'] = request.vnode.image.owner
        username = request.username
        lxcname = '%s-batch-%s-%s' % (username,taskid,str(vnodeid))
        instance_type =  request.vnode.instance
        mount_list = request.vnode.mount
        gpu_need = int(request.vnode.instance.gpu)
        ipaddr = request.vnode.network.ipaddr
        gateway = request.vnode.network.gateway
        brname = request.vnode.network.brname

        #create container
        [success, msg] = self.create_container(taskid, vnodeid, username, image, lxcname, instance_type, ipaddr, gateway, brname)
        if not success:
            return rpc_pb2.Reply(status=rpc_pb2.Reply.REFUSED, message=msg)

        #mount oss
        self.mount_oss("%s/global/users/%s/oss" % (self.fspath,username), mount_list)
        conffile = open("/var/lib/lxc/%s/config" % lxcname, 'a+')
        mount_str = "lxc.mount.entry = %s/global/users/%s/oss/%s %s/root/oss/%s none bind,rw,create=dir 0 0"
        for mount in mount_list:
            conffile.write("\n"+ mount_str % (self.fspath, username, mount.remotePath, rootfs, mount.remotePath))
        conffile.close()

        logger.info("Start container %s..." % lxcname)
        #container = lxc.Container(lxcname)
        ret = subprocess.run('lxc-start -n %s'%lxcname,stdout=subprocess.PIPE,stderr=subprocess.STDOUT, shell=True)
        if ret.returncode != 0:
            logger.error('start container %s failed' % lxcname)
            self.imgmgr.deleteFS(lxcname)
            return rpc_pb2.Reply(status=rpc_pb2.Reply.REFUSED,message="Can't start the container")

        logger.info('start container %s success' % lxcname)

        #add GPU
        [success, msg] = self.add_gpu_device(lxcname,gpu_need)
        if not success:
            logger.error("Fail to add gpu device. " + msg)
            container.stop()
            self.imgmgr.deleteFS(lxcname)
            return rpc_pb2.Reply(status=rpc_pb2.Reply.REFUSED,message="Fail to add gpu device. " + msg)

        return rpc_pb2.Reply(status=rpc_pb2.Reply.ACCEPTED,message="")

    def start_task(self, request, context):
        pass

    def stop_task(self, request, context):
        for msg in request.taskmsgs:
            lxcname = '%s-batch-%s-%s-%s' % (msg.username,msg.taskid,str(msg.instanceid),msg.token)
            logger.info("Stop the task with lxc:"+lxcname)
            subprocess.run("lxc-stop -k -n %s" % lxcname, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, shell=True)
        return rpc_pb2.Reply(status=rpc_pb2.Reply.ACCEPTED,message="")

    # stop and remove container
    def stop_vnode(self, request, context):
        taskid = request.taskid
        username = request.username
        vnodeid = request.vnodeid
        mount_list = request.vnode.mount
        lxcname = '%s-batch-%s-%s' % (username,taskid,str(vnodeid))

        logger.info("Stop the task with lxc:"+lxcname)
        container = lxc.Container(lxcname)
        if container.stop():
            logger.info("stop container %s success" % lxcname)
        else:
            logger.error("stop container %s failed" % lxcname)

        logger.info("deleting container:%s" % lxcname)
        if self.imgmgr.deleteFS(lxcname):
            logger.info("delete container %s success" % lxcname)
        else:
            logger.error("delete container %s failed" % lxcname)

        #release gpu
        self.release_gpu_device(lxcname)

        #umount oss
        self.umount_oss("%s/global/users/%s/oss" % (self.fspath,username), mount_list)

        return rpc_pb2.Reply(status=rpc_pb2.Reply.ACCEPTED,message="")


    #accquire ip and create a container
    def create_container(self,taskid,vnodeid,username,image,lxcname,quota,ipaddr,gateway,brname):
        # prepare image and filesystem
        status = self.imgmgr.prepareFS(username,image,lxcname,str(quota.disk))
        if not status:
            return [False, "Create container for batch failed when preparing filesystem"]

        rootfs = "/var/lib/lxc/%s/rootfs" % lxcname

        if not os.path.isdir("%s/global/users/%s" % (self.fspath,username)):
            path = env.getenv('DOCKLET_LIB')
            subprocess.call([path+"/master/userinit.sh", username])
            logger.info("user %s directory not found, create it" % username)
        sys_run("mkdir -p /var/lib/lxc/%s" % lxcname)
        logger.info("generate config file for %s" % lxcname)

        def config_prepare(content):
            content = content.replace("%ROOTFS%",rootfs)
            content = content.replace("%HOSTNAME%","batch-%s" % str(vnodeid))
            content = content.replace("%CONTAINER_MEMORY%",str(quota.memory))
            content = content.replace("%CONTAINER_CPU%",str(quota.cpu*100000))
            content = content.replace("%FS_PREFIX%",self.fspath)
            content = content.replace("%LXCSCRIPT%",env.getenv("LXC_SCRIPT"))
            content = content.replace("%USERNAME%",username)
            content = content.replace("%LXCNAME%",lxcname)
            content = content.replace("%VETHPAIR%",str(taskid)+"-"+str(vnodeid))
            content = content.replace("%IP%",ipaddr)
            content = content.replace("%BRNAME%",brname)
            content = content.replace("%GATEWAY%",gateway)
            return content

        logger.info(self.confpath)
        conffile = open(self.confpath+"/container.batch.conf", 'r')
        conftext = conffile.read()
        conffile.close()

        conftext = config_prepare(conftext)

        conffile = open("/var/lib/lxc/%s/config" % lxcname, 'w')
        conffile.write(conftext)
        conffile.close()
        return [True, ""]

    def add_msg(self,taskid,username,vnodeid,status,token,errmsg):
        self.msgslock.acquire()
        try:
            self.taskmsgs.append(rpc_pb2.TaskMsg(taskid=str(taskid),username=username,vnodeid=int(vnodeid),subTaskStatus=status,token=token,errmsg=errmsg))
        except Exception as err:
            logger.error(traceback.format_exc())
        self.msgslock.release()

    def report_msg(self):
        channel = grpc.insecure_channel(self.master_ip+":"+self.master_port)
        stub = rpc_pb2_grpc.MasterStub(channel)
        while True:
            self.msgslock.acquire()
            reportmsg = rpc_pb2.ReportMsg(taskmsgs = self.taskmsgs)
            try:
                response = stub.report(reportmsg)
                logger.info("Response from master by reporting: "+str(response.status)+" "+response.message)
            except Exception as err:
                logger.error(traceback.format_exc())
            self.taskmsgs = []
            self.msgslock.release()
            time.sleep(self.report_interval)

    def start_report(self):
        thread = threading.Thread(target = self.report_msg, args=())
        thread.setDaemon(True)
        thread.start()
        logger.info("Start to report task messages to master every %d seconds." % self.report_interval)

def TaskWorkerServe():
    max_threads = int(env.getenv('BATCH_MAX_THREAD_WORKER'))
    worker_port = int(env.getenv('BATCH_WORKER_PORT'))
    logger.info("Max Threads on a worker is %d" % max_threads)
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=max_threads))
    rpc_pb2_grpc.add_WorkerServicer_to_server(TaskWorker(), server)
    server.add_insecure_port('[::]:'+str(worker_port))
    server.start()
    logger.info("Start TaskWorker Servicer on port:%d" % worker_port)
    try:
        while True:
            time.sleep(_ONE_DAY_IN_SECONDS)
    except KeyboardInterrupt:
        server.stop(0)

if __name__ == "__main__":
    TaskWorkerServe()
