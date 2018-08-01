#!/usr/bin/python2.7
from flask import Flask, request
from flask_restful import Resource, Api, reqparse, abort
from json import dumps, loads
from flask_jsonpify import jsonify
from datetime import datetime
from shutil import copyfile
from glob import glob
from flask_httpauth import HTTPBasicAuth
import subprocess, os, time, json, multiprocessing, redis, configparser, itertools, ssl

config = configparser.ConfigParser()
config.read('barque.conf')
# configs
__host = config['flask']['host']  # ip address for API to bind to
__port = int(config['flask']['port'])  # port for API to bind to
cert = config['flask']['cert']  # Location of cert.pem file for SSL
key = config['flask']['key']  # location of key.pem file for SSL
uname = config['flask']['username']  # HTTP Basic Auth username
upass = config['flask']['password']  # HTTP Basic Auth password
path = config['settings']['path']  # Destination path for backups, terminating / required
pool = config['settings']['pool']  # Ceph RBD pool, terminating / required. Leave empty for default
minions = int(config['settings']['workers'])  # number of worker processes to spawn
r_host = config['redis']['host']  # Redis server host
r_port = int(config['redis']['port'])  # Redis server port
r_pw = config['redis']['password']  # Redis server password
locations = {}  # backup storage destinations
barque_storage = {}
barque_ips = {}
for option in config.options('destinations'):
    locations[option] = config.get('destinations', option)
for option in config.options('barque_storage'):
    barque_storage[option] = config.get('barque_storage', option)
for option in config.options('barque_ips'):
    barque_ips[option] = config.get('barque_ips', option)
version = '0.80'
starttime = None

# global vars
app = Flask(__name__)
api = Api(app)
auth = HTTPBasicAuth()
r = None
workers = []
admin_auth = {uname: upass}
parser = reqparse.RequestParser()
parser.add_argument('file', 'vmid')

class Worker(multiprocessing.Process):
    r = None
    def run(self):
        licensed_to_live = True
        my = multiprocessing.current_process()
        name = 'Process {}'.format(my.pid)
        print("{} started".format(name))
        r = redis.Redis(host=r_host, port=r_port, password=r_pw)
        print("{} connected to redis".format(name))
        #		#block for items in joblock
        while licensed_to_live:
            for job in r.smembers('joblock'):
                if r.hget(job, 'state') == 'enqueued':
                    r.hset(job, 'state', 'locked')
                    r.hset(job, 'worker', str(my.pid))
                    print("{} attempting to lock {}".format(name, job))
                    time.sleep(0.5)
                    # check if lock belongs to this worker
                    if r.hget(job, 'worker') == str(my.pid):
                        print("{} lock successful, proceeding".format(name))
                        task = r.hget(job, 'job')
                        if task == 'backup':
                            self.backup(job)
                        elif task == 'restore':
                            self.restore(job)
                        elif task == 'scrub':
                            self.scrubSnaps(job)
                        elif task == 'migrate':
                            self.migrate(job)
                        elif task == 'poisoned':
                            self.poison(job)
                        elif task == 'deepscrub':
                            self.scrubDeep()
                        else:
                            print("{} unable to determine task...".format(name))
                    else:
                        print("{} lock unsuccessful, reentering queue".format(name))
                # be nice to redis
                time.sleep(0.1)
            time.sleep(5)
        # licensed_to_live = False
        return

    def backup(self, vmid):
        storage = None
        vmdisk = None
        destination = r.hget(vmid, 'dest')
        destHealth = checkDest(destination)
        if not destHealth:
            cmd = subprocess.check_output('/bin/bash /etc/pve/utilities/detect_stale.sh', shell=True)
            print(cmd)
            destHealth = checkDest(destination)
            if not destHealth:
                r.hset(vmid, 'msg', '{} storage destination is offline, unable to recover')
                r.hset(vmid, 'state', 'error')
                return
        r.hset(vmid, 'state', 'active')
        # vmdisk = 'vm-{}-disk-1'.format(vmid)
        timestamp = datetime.strftime(datetime.utcnow(), "_%Y-%m-%d_%H-%M-%S")
        config_file = ""
        config_target = "{}.conf".format(vmid)
        # get config file
        for paths, dirs, files in os.walk('/etc/pve/nodes'):
            if config_target in files:
                config_file = os.path.join(paths, config_target)
                print(config_file)

        # catch if container does not exist
        if len(config_file) == 0:
            r.hset(vmid, 'state', 'error')
            r.hset(vmid, 'msg', '{} is invalid CTID'.format(vmid))
            return
        valid = checkConf(vmid)
        print("config exists within worker process? {}".format(valid))
        # get storage info from config file
        parser = configparser.ConfigParser()
        try:
            with open(config_file) as lines:
                lines = itertools.chain(("[root]",), lines)
                parser.read_file(lines)
        except:
            r.hset(vmid, 'state', 'error')
            r.hset(vmid, 'msg', 'unable to open config file')
            return
        try:
            storage, vmdisk = parser['root']['rootfs'].split(',')[0].split(':')
        except:
            r.hset(vmid, 'state', 'error')
            r.hset(vmid, 'msg', 'unable to get storage info from config file')
            return
        print(storage)
        print(vmdisk)

        # check if poisoned 1
        if r.hget(vmid, 'job') == 'poisoned':
            r.srem('joblock', vmid)
            r.hset(vmid, 'msg', 'Task successfully cancelled')
            r.hset(vmid, 'state', 'OK')
            return

        # create snapshot for backup
        try:
            cmd = subprocess.check_output('rbd snap create {}{}@barque'.format(pool, vmdisk), shell=True)
        except:
            r.hset(vmid, 'state', 'error')
            r.hset(vmid, 'msg', 'error creating backup snapshot')
            return

        # copy config file
        config_dest = "".join([destination, vmdisk, timestamp, ".conf"])
        try:
            copyfile(config_file, config_dest)
        except:
            # delete barque snapshot
            cmd = subprocess.check_output('rbd snap rm {}{}@barque'.format(pool, vmdisk), shell=True)
            r.hset(vmid, 'msg', 'unable to copy config file')
            r.hset(vmid, 'state', 'error')
            return
        # protect snapshot during backup
        cmd = subprocess.check_output('rbd snap protect {}{}@barque'.format(pool, vmdisk), shell=True)

        # check if poisoned 2
        if r.hget(vmid, 'job') == 'poisoned':
            try:
                # unprotect barque snapshot
                cmd = subprocess.check_output('rbd snap unprotect {}{}@barque'.format(pool, vmdisk), shell=True)
                # delete barque snapshot
                cmd = subprocess.check_output('rbd snap rm {}{}@barque'.format(pool, vmdisk), shell=True)
                # delete stored config file
                os.remove(config_dest)
                r.hset(vmid, 'msg', 'Task successfully cancelled')
                r.hset(vmid, 'state', 'OK')
            except:
                r.hset(vmid, 'state', 'error')
                r.hset(vmid, 'msg', 'error removing backup snapshot while poisoned')
                return
            r.srem('joblock', vmid)
            return

        # create compressed backup file from backup snapshot
        dest = "".join([destination, vmdisk, timestamp, ".lz4"])
        args = ['rbd export --rbd-concurrent-management-ops 20 --export-format 2 {}{}@barque - | lz4 -1 - {}'.format(pool, vmdisk, dest)]
        r.hset(vmid, 'msg', 'Creating backup image')
        try:
            cmd = subprocess.check_output(args, shell=True)#.split('\n') #run command then convert output to list, splitting on newline
        except:
            r.hset(vmid, 'state', 'error')
            r.hset(vmid, 'msg', 'unable to aquire rbd image for CTID: {}'.format(vmid))
            # unprotect barque snapshot
            cmd = subprocess.check_output('rbd snap unprotect {}{}@barque'.format(pool, vmdisk), shell=True)
            # delete barque snapshot
            cmd = subprocess.check_output('rbd snap rm {}{}@barque'.format(pool, vmdisk), shell=True)
            return

        # check poisoned 3
        if r.hget(vmid, 'job') == 'poisoned':
            try:
                # remove backup file
                os.remove(dest)
                # unprotect barque snapshot
                cmd = subprocess.check_output('rbd snap unprotect {}{}@barque'.format(pool, vmdisk), shell=True)
                # delete barque snapshot
                cmd = subprocess.check_output('rbd snap rm {}{}@barque'.format(pool, vmdisk), shell=True)
                # delete stored config file
                os.remove(config_dest)
                r.hset(vmid, 'msg', 'Task successfully cancelled')
                r.hset(vmid, 'state', 'OK')
            except:
                r.hset(vmid, 'state', 'error')
                r.hset(vmid, 'msg', 'error cancelling backup, at poisoned 3')
                return
            r.srem('joblock', vmid)
            return

        # unprotect barque snapshot
        cmd = subprocess.check_output('rbd snap unprotect {}{}@barque'.format(pool, vmdisk), shell=True)

        # delete barque snapshot
        cmd = subprocess.check_output('rbd snap rm {}{}@barque'.format(pool, vmdisk), shell=True)

        # mark complete and unlock CTID
        r.hset(vmid, 'state', 'OK')
        r.srem('joblock', vmid)
        return

    def restore(self, vmid):
        config_file = ""
        node = ""
        destination = r.hget(vmid, 'dest')
        destHealth = checkDest(destination)
        if not destHealth:
            cmd = subprocess.check_output('/bin/bash /etc/pve/utilities/detect_stale.sh', shell=True)
            print(cmd)
            destHealth = checkDest(destination)
            if not destHealth:
                r.hset(vmid, 'msg', '{} storage destination is offline, unable to recover')
                r.hset(vmid, 'state', 'error')
                return
        filename = r.hget(vmid, 'file')
        r.hset(vmid, 'state', 'active')

        # vmdisk = 'vm-{}-disk-1'.format(vmid)
        fileimg = "".join([destination, filename, ".lz4"])
        fileconf = "".join([destination, filename, ".conf"])

        # find node hosting container
        config_file = ""
        config_target = "{}.conf".format(vmid)
        for paths, dirs, files in os.walk('/etc/pve/nodes'):
            if config_target in files:
                config_file = os.path.join(paths, config_target)
                print(config_file)
                node = config_file.split('/')[4]
                print(node)
        if len(config_file) == 0:
            r.hset(vmid, 'state', 'error')
            r.hset(vmid, 'msg', 'unable to locate container')
            return

        # get storage info from running container
        parserCurr = configparser.ConfigParser()
        with open(config_file) as lines:
            lines = itertools.chain(("[root]",), lines)
            parserCurr.read_file(lines)
        try:
            storage_current, vmdisk_current = parserCurr['root']['rootfs'].split(',')[0].split(':')
        except:
            r.hset(vmid, 'state', 'error')
            r.hset(vmid, 'msg', 'unable to get storage info from active config file')
            return
        # get storage info from config file
        parserFinal = configparser.ConfigParser()
        with open(fileconf) as lines:
            lines = itertools.chain(("[root]",), lines)
            parserFinal.read_file(lines)
        try:
            storage_final, vmdisk_final = parserFinal['root']['rootfs'].split(',')[0].split(':')
        except:
            r.hset(vmid, 'state', 'error')
            r.hset(vmid, 'msg', 'unable to get storage info from backup config file')
            return
        # check if poisoned 1
        if r.hget(vmid, 'job') == 'poisoned':
            r.srem('joblock', vmid)
            r.hset(vmid, 'msg', 'Task successfully cancelled')
            r.hset(vmid, 'state', 'OK')
            return

        # stop container if not already stopped
        r.hset(vmid, 'msg', 'stopping container')
        if not loads(subprocess.check_output("pvesh get /nodes/{}/lxc/{}/status/current".format(node, vmid), shell=True))["status"] == "stopped":
            ctstop = subprocess.check_output("pvesh create /nodes/{}/lxc/{}/status/stop".format(node, vmid), shell=True)
        timeout = time.time() + 60
        while True:  # wait for container to stop
            stat = loads(subprocess.check_output("pvesh get /nodes/{}/lxc/{}/status/current".format(node,vmid), shell=True))["status"]
            print(stat)
            if stat == "stopped":
                break
            elif time.time() > timeout:
                r.hset(vmid, 'state', 'error')
                r.hset(vmid, 'msg', 'Unable to stop container - timeout')
                return

        # make recovery copy of container image
        r.hset(vmid, 'msg', 'creating disaster recovery image')
        imgcpy = subprocess.check_output("rbd cp --rbd-concurrent-management-ops 20 {}{} {}{}-barque".format(pool,
            vmdisk_current, pool, vmdisk_current), shell=True)

        # print("Waiting for poison test2")
        # time.sleep(15)
        # check if poisoned 2
        if r.hget(vmid, 'job') == 'poisoned':
            try:
                # remove recovery copy
                imgrm = subprocess.check_output("rbd rm {}{}-barque".format(pool, vmdisk_current), shell=True)
                # un-stop container
                ctstart = subprocess.check_output("pvesh create /nodes/{}/lxc/{}/status/start".format(node,vmid), shell=True)
                r.hset(vmid, 'msg', 'Task successfully cancelled')
                r.hset(vmid, 'state', 'OK')
            except:
                r.hset(vmid, 'msg', 'Error cancelling restore, at poisoned 2')
                r.hset(vmid, 'state', 'error')
                return
            r.srem('joblock', vmid)
            return

        # delete container storage
        r.hset(vmid, 'msg', 'removing container image')
        try:
            imgdel = subprocess.check_output("pvesh delete /nodes/{}/storage/{}/content/{}:{}".format(node, storage_current,
                                                                         storage_current, vmdisk_current), shell=True)
            print(imgdel)
        except:
            try:  # attempt to force unmap image if has watchers
                cmd = subprocess.check_output("rbd unmap -o force {}{}".format(pool, vmdisk_current))
            except:
                r.hset(vmid, 'state', 'error')
                r.hset(vmid, 'msg', "unable to unmap container image")
                return
            try:  # retry deleting image
                cmd = subprocess.check_output("pvesh delete /nodes/{}/storage/{}/content/{}:{}".format(node,  storage_current,
                                                                             storage_current, vmdisk_current),
                    shell=True)
            except:
                r.hset(vmid, 'state', 'error')
                r.hset(vmid, 'msg', "unable to remove container image", shell=True)
                return

        # extract lz4 compressed image file
        filetarget = "".join([destination, filename, ".img"])
        uncompress = subprocess.check_output("lz4 -d {} {}".format(fileimg, filetarget), shell=True)
        print(uncompress)

        # print("Waiting for poison test3")
        # time.sleep(15)
        # check if poisoned 3
        if r.hget(vmid, 'job') == 'poisoned':
            try:
                # remove uncompressed image
                os.remove(filetarget)
                # try adding container image
                cmd = subprocess.check_output("rbd mv {}{}-barque {}{}".format(pool, vmdisk_current, pool, vmdisk_current), shell=True)
                print(cmd)
                ctstart = subprocess.check_output("pvesh create /nodes/{}/lxc/{}/status/start".format(node,vmid), shell=True)
                r.hset(vmid, 'msg', 'Task successfully cancelled')
                r.hset(vmid, 'state', 'OK')
            except:
                r.hset(vmid, 'msg', 'Error cancelling restore, at poisoned 3')
                r.hset(vmid, 'state', 'error')
                return
            r.srem('joblock', vmid)
            return

        # import new image
        r.hset(vmid, 'msg', 'importing backup image')
        try:
            rbdimp = subprocess.check_output("rbd import --rbd-concurrent-management-ops 20 --export-format 2 {} {}{}".format(filetarget, pool, vmdisk_final), shell=True)
            print(rbdimp)
        except:
            r.hset(vmid, 'state', 'error')
            r.hset(vmid, 'msg', rbdimp)
            cmd = subprocess.check_output("rbd mv {}{}-barque {}{}".format(pool, vmdisk_current, pool, vmdisk_current), shell=True)
            os.remove(filetarget)
            return

        # delete uncompressed image file
        r.hset(vmid, 'msg', 'cleaning up')
        rmuncomp = subprocess.check_output("rm {}".format(filetarget), shell=True)
        print(rmuncomp)

        # delete barque snapshot
        cmd = subprocess.check_output('rbd snap rm {}{}@barque'.format(pool, vmdisk_final), shell=True)
        # image attenuation for kernel params #Removed after switching to format 2
        # imgatten = subprocess.check_output("rbd feature disable {} object-map fast-diff deep-flatten".format(vmdisk), shell=True)
        # print(imgatten)

        # print("Waiting for poison test4")
        # time.sleep(15)
        # check if poisoned 4
        if r.hget(vmid, 'job') == 'poisoned':
            try:
                # try removing the recovered image
                cmd = subprocess.check_output("rbd rm {}{}".format(pool, vmdisk_current), shell=True)
                # try adding container image
                cmd = subprocess.check_output("rbd mv {}{}-barque {}{}".format(pool, vmdisk_current, pool, vmdisk_current), shell=True)
                print(cmd)
                ctstart = subprocess.check_output("pvesh create /nodes/{}/lxc/{}/status/start".format(node,vmid), shell=True)
                r.hset(vmid, 'msg', 'Task successfully cancelled')
                r.hset(vmid, 'state', 'OK')
            except:
                r.hset(vmid, 'msg', 'Error cancelling restore, at poisoned 4')
                r.hset(vmid, 'state', 'error')
                return
            r.srem('joblock', vmid)
            return
        # replace config file
        copyfile(fileconf, config_file)

        # start container
        ctstart = subprocess.check_output("pvesh create /nodes/{}/lxc/{}/status/start".format(node, vmid), shell=True)
        # time.sleep(5)
        print(ctstart)

        # cleanup recovery copy
        cmd = subprocess.check_output("rbd rm {}{}-barque".format(pool, vmdisk_current), shell=True)
        r.hset(vmid, 'state', 'OK')
        r.srem('joblock', vmid)
        return

    def scrubSnaps(self, vmid):
        r.hset(vmid, 'state', 'active')
        config_file = None
        # vmdisk = 'vm-{}-disk-1'.format(vmid)
        config_target = "{}.conf".format(vmid)
        # get config file
        for paths, dirs, files in os.walk('/etc/pve/nodes'):
            if config_target in files:
                config_file = os.path.join(paths, config_target)
            # print(config_file)
        # get storage info from config file
        parser = configparser.ConfigParser()
        with open(config_file) as lines:
            lines = itertools.chain(("[root]",), lines)
            parser.read_file(lines)
        storage, vmdisk = parser['root']['rootfs'].split(',')[0].split(':')

        try:
            cmd = subprocess.check_output('rbd snap unprotect {}{}@barque'.format(pool, vmdisk), shell=True)
        except:
            # could not unprotect, maybe snap wasn't protected
            try:
                cmd = subprocess.check_output('rbd snap rm {}{}@barque'.format(pool, vmdisk), shell=True)
            except:
                r.hset(vmid, 'state', 'error')
                r.hset(vmid, 'msg', "critical snapshotting error: unable to unprotect or remove")
                return
            # snapshot successfully removed, set OK
            r.hset(vmid, 'state', 'OK')
            r.hset(vmid, 'msg', 'snapshot successfully scrubbed - removed only')
            r.srem('joblock', vmid)
            return
        # snapshot successfully unprotected, attempt removal
        try:
            cmd = subprocess.check_output('rbd snap rm {}{}@barque'.format(pool, vmdisk), shell=True)
        except:
            r.hset(vmid, 'state', 'error')
            r.hset(vmid, 'msg', 'critical snapshotting error: snap unprotected but unable to remove')
            return
        # snapshot successfully removed, set OK
        r.hset(vmid, 'state', 'OK')
        r.hset(vmid, 'msg', 'snapshot successfully scrubbed - unprotected and removed')
        r.srem('joblock', vmid)
        retry = r.hget(vmid, 'retry')
        # if retry == 'backup': #REMOVE - used for testing snap scrubbing
        # 	r.hset(vmid, 'job', 'backup')
        # 	r.hset(vmid, 'state', 'enqueued')
        return
    def scrubDeep(self):
        r.hset('0', 'state', 'active')
        out = subprocess.check_output('rbd ls {}'.format(pool.strip('/')), shell=True)
        for disk in out.split():
            cmd = subprocess.check_output('rbd snap ls {}{}'.format(pool, disk), shell=True)
            if "barque" in cmd.split():
                vmid = disk.split('-')[1]
                if not r.hget(vmid, 'state') == 'active':
                    r.hset(vmid, 'job', 'scrub')
                    r.hset(vmid, 'state', 'enqueued')
                    r.sadd('joblock', vmid)
                    print('snap found on {}, adding to queue'.format(disk))
            else:
                print("{} clean, moving on".format(disk))
        r.srem('joblock', 0)

    def poison(self, vmid):
        r.srem('joblock', vmid)
        r.hset(vmid, 'state', 'OK')
        r.hset(vmid, 'msg', 'Task successfully cancelled')
        return

    def migrate(self, vmid):
                #TODO: Check if target cluster is known
                #TODO: Add Poisoning
                #TODO: Refactor for alternate storage destinations
                #TODO: Move rate limit to config
        r.hset(vmid, 'state', 'active')
        target_file = r.hget(vmid, 'file')
        target_cluster = r.hget(vmid, 'target_cluster')
        config_file = ""
        node = ""
        dest_disk = ""
        dest_storage = ""

        ##
        ## Gather Information
        ##

        r.hset(vmid, 'msg', 'collecting information')
        #Find node hosting destingation container, get config_file
        config_target = "{}.conf".format(vmid)
        for paths, dirs, files in os.walk('/etc/pve/nodes'):
            if config_target in files:
                config_file = os.path.join(paths, config_target)
                print(config_file)
                node = config_file.split('/')[4]
                print(node)
        if len(config_file) == 0:
            r.hset(vmid, 'state', 'error')
            r.hset(vmid, 'msg', 'unable to locate container')
            return

        # get storage info from running container
        parserCurr = configparser.ConfigParser()
        with open(config_file) as lines:
            lines = itertools.chain(("[root]",), lines)
            parserCurr.read_file(lines)
        try:
            dest_storage, dest_disk = parserCurr['root']['rootfs'].split(',')[0].split(':')
            print(dest_disk)
        except:
            r.hset(vmid, 'state', 'error')
            r.hset(vmid, 'msg', 'unable to get storage info from active config file')
            return

        # get info of target barque node
        target_ip = barque_ips[target_cluster]
        target_path = barque_storage[target_cluster]

        ##
        ## Perform Operations
        ##

        # stop container if not already stopped
        r.hset(vmid, 'msg', 'stopping container')
        if not loads(subprocess.check_output("pvesh get /nodes/{}/lxc/{}/status/current".format(node, vmid), shell=True))["status"] == "stopped":
            ctstop = subprocess.check_output("pvesh create /nodes/{}/lxc/{}/status/stop".format(node, vmid), shell=True)
        timeout = time.time() + 60
        while True:  # wait for container to stop
            stat = loads(subprocess.check_output("pvesh get /nodes/{}/lxc/{}/status/current".format(node,vmid), shell=True))["status"]
            print(stat)
            if stat == "stopped":
                break
            elif time.time() > timeout:
                r.hset(vmid, 'state', 'error')
                r.hset(vmid, 'msg', 'Unable to stop container - timeout')
                return

        try:
            r.hset(vmid, 'msg', 'Fetching backup image')
            cmd = subprocess.check_output("ssh root@{} \"cat {}{}\" | mbuffer -r 10M | lz4 -d - {}{}.img".format(target_ip, target_path, target_file, locations[locations.keys()[-1]], vmid), shell=True)
            #print(cmd)
        except:
            r.hset(vmid, 'state', 'error')
            r.hset(vmid, 'msg', 'unable to fetch backup image')
            return

        try: # Delete disk if it exists
            subprocess.check_call("rbd info {}{}".format(pool, dest_disk), shell=True)
            r.hset(vmid, 'msg', 'removing container image')
            try:
                imgdel = subprocess.check_output("pvesh delete /nodes/{}/storage/{}/content/{}:{}".format(node, dest_storage,
                                                                             dest_storage, dest_disk), shell=True)
                print(imgdel)
            except:
                try:  # attempt to force unmap image and pray
                    cmd = subprocess.check_output("rbd unmap -o force {}{}".format(pool, dest_disk))
                except:
                    r.hset(vmid, 'state', 'error')
                    r.hset(vmid, 'msg', "unable to unmap container image")
                    return
                try:  # retry deleting image
                    cmd = subprocess.check_output("pvesh delete /nodes/{}/storage/{}/content/{}:{}".format(node,  dest_storage,
                                                                                 dest_storage, dest_disk),
                        shell=True)
                except:
                    r.hset(vmid, 'state', 'error')
                    r.hset(vmid, 'msg', "unable to remove container image", shell=True)
                    return
        except:
            print("disk does not exist, proceeding")
        try:
            r.hset(vmid, 'msg', 'Importing disk image')
            cmd = subprocess.check_output("rbd import --export-format 2 {}{}.img {}{}".format(locations[locations.keys()[-1]], vmid, pool, dest_disk), shell=True)
            print(cmd)
        except:
            r.hset(vmid, 'state', 'error')
            r.hset(vmid, 'msg', 'unable to import disk image')
            return
        ##
        ## Clean up
        ##

        r.hset(vmid, 'msg', 'Cleaning up...')
        os.remove("{}{}.img".format(locations[locations.keys()[-1]], vmid))
        ctstart = subprocess.check_output("pvesh create /nodes/{}/lxc/{}/status/start".format(node, vmid), shell=True)
        r.hset(vmid, 'state', 'OK')
        r.hset(vmid, 'msg', 'Migration complete')
        r.srem('joblock', vmid)
        return

####                     ####
##  pveBarque API Classes  ##
####                     ####

class Backup(Resource):
    @auth.login_required
    def post(self, vmid):
        dest = ""
        # catch if container does not exist
        if not checkConf(vmid):
            return {'error': "{} is not a valid CTID"}, 400
        # clear error and try again if retry flag in request args
        if 'retry' in request.args and r.hget(vmid, 'state') == 'error':
            r.srem('joblock', vmid)
        # handle destination setting
        if 'dest' in request.args:
            result, response, err = checkDest(request.args['dest'])
            if result:
                dest = locations[request.args['dest']]
            else:
                return response, err
        else:
            result, response, err = checkDest(locations.keys()[-1])
            if result:
                dest = locations[locations.keys()[-1]]
            else:
                return response, err
        if str(vmid) in r.smembers('joblock'):
            return {'error': "VMID locked, another operation is in progress for container: {}".format(vmid),
                    'status': r.hget(vmid, 'state'), 'job': r.hget(vmid, 'job')}, 409
        else:
            r.hset(vmid, 'job', 'backup')
            r.hset(vmid, 'file', '')
            r.hset(vmid, 'worker', '')
            r.hset(vmid, 'msg', '')
            r.hset(vmid, 'state', 'enqueued')
            r.hset(vmid, 'dest', dest)
            r.sadd('joblock', vmid)

        return {'status': "backup job created for CTID {}".format(vmid)}, 202

class BackupAll(Resource):
    @auth.login_required
    def post(self):
        targets = []
        dest = ""
        # handle destination setting
        if 'dest' in request.args:
            result, response, err = checkDest(request.args['dest'])
            if result:
                dest = locations[request.args['dest']]
            else:
                return response, err
        else:
            result, response, err = checkDest(locations.keys()[-1])
            if result:
                dest = locations[locations.keys()[-1]]
            else:
                return response, err
        response = []
        for paths, dirs, files in os.walk('/etc/pve/nodes'):
            for f in files:
                if f.endswith(".conf"):
                    targets.append(f.split(".")[0])
        for vmid in targets:
            if str(vmid) in r.smembers('joblock'):
                response.append({vmid: {"status": "error", "message": "CTID locked, another operation is in progress"}})
            else:
                r.hset(vmid, 'job', 'backup')
                r.hset(vmid, 'file', '')
                r.hset(vmid, 'worker', '')
                r.hset(vmid, 'msg', '')
                r.hset(vmid, 'state', 'enqueued')
                r.hset(vmid, 'dest', dest)
                r.sadd('joblock', vmid)
                response.append({vmid: {"status": "enqueued", "message": "Backup job added to queue"}})
        return response

class Restore(Resource):
    @auth.login_required
    def post(self, vmid):
        path = None
        # handle destination setting
        if 'dest' in request.args:
            result, response, err = checkDest(request.args['dest'])
            if result:
                path = locations[request.args['dest']]
            else:
                return response, err
        else:
            result, response, err = checkDest(locations.keys()[-1])
            if result:
                path = locations[locations.keys()[-1]]
            else:
                return response, err
        # check if file specified
        if 'file' not in request.args:
            return {'error': 'Resource requires a file argument'}, 400
        filename = os.path.splitext(request.args['file'])[0]
        if not filename.split('-')[1] == str(vmid):
            return {'error': 'File name does not match VMID'}, 400
        fileimg = "".join([path, filename, ".lz4"])
        fileconf = "".join([path, filename, ".conf"])

        # catch if container does not exist
        if not checkConf(vmid):
            return {'error': "{} is not a valid CTID"}, 400
        # check if backup and config files exist
        if not os.path.isfile(fileimg) and not os.path.isfile(fileconf):
            return {'error': "unable to proceed, backup file or config file (or both) does not exist"}, 400
        if str(vmid) in r.smembers('joblock'):
            return {'error': "VMID locked, another operation is in progress for container: {}".format(vmid)}, 409

        r.hset(vmid, 'job', 'restore')
        r.hset(vmid, 'file', filename)
        r.hset(vmid, 'worker', '')
        r.hset(vmid, 'msg', '')
        r.hset(vmid, 'dest', path)
        r.hset(vmid, 'state', 'enqueued')
        r.sadd('joblock', vmid)

        return {'status': "restore job created for CTID {}".format(vmid)}, 202

class ListAllBackups(Resource):
    @auth.login_required
    def get(self):
        path = None

        # handle destination setting
        if 'dest' in request.args:
            result, response, err = checkDest(request.args['dest'])
            if result:
                path = locations[request.args['dest']]
            else:
                return response, err
        else:
            result, response, err = checkDest(locations.keys()[-1])
            if result:
                path = locations[locations.keys()[-1]]
            else:
                return response, err
        images = []
        confs = []
        for paths, dirs, files in os.walk(path):
            for f in files:
                if f.endswith('.lz4'):
                    images.append(f)
                elif f.endswith('.conf'):
                    confs.append(f)
        return {'backup files': images, 'config files': confs}

class ListBackups(Resource):
    @auth.login_required
    def get(self, vmid):
        path = None
        if 'dest' in request.args:
            result, response, err = checkDest(request.args['dest'])
            if result:
                path = locations[request.args['dest']]
            else:
                return response, err
        else:
            result, response, err = checkDest(locations.keys()[-1])
            if result:
                path = locations[locations.keys()[-1]]
            else:
                return response, err
        files = sorted(os.path.basename(f) for f in glob("".join([path, "vm-{}-disk*.lz4".format(vmid)])))
        return {'backups': files}

class DeleteBackup(Resource):
    @auth.login_required
    def post(self, vmid):
        path = None
        if str(vmid) in r.smembers('joblock'):
            return {'error': 'CTID locked, another operation is in progress for container {}'.format(vmid)}, 409
        if 'dest' in request.args:
            result, response, err = checkDest(request.args['dest'])
            if result:
                path = locations[request.args['dest']]
            else:
                return response, err
        else:
            result, response, err = checkDest(locations.keys()[-1])
            if result:
                path = locations[locations.keys()[-1]]
            else:
                return response, err
        if 'file' in request.args:
            if not request.args['file'].split('-')[1] == str(vmid):
                return {'error': 'File name does not match VMID'}, 400
            r.sadd('joblock', vmid)
            print(request.args['file'])
            fileimg = "".join([path, request.args['file']])
            fileconf = "".join([os.path.splitext(fileimg)[0], ".conf"])
            if os.path.isfile(fileimg):
                os.remove(fileimg)
                if os.path.isfile(fileconf):
                    os.remove(fileconf)
                r.srem('joblock', vmid)
                return {'file removed': os.path.basename(fileimg)}
            else:
                r.srem('joblock', vmid)
                return {'file does not exist': os.path.basename(fileimg)}, 400
        else:
            return {'error': "resource requires a file argument"}, 400

class Status(Resource):
    @auth.login_required
    def get(self, vmid):
        response = []
        # catch if container does not exist
        if not checkConf(vmid):
            return {'error': "{} is not a valid CTID"}, 400
        status = r.hget(vmid, 'state')
        msg = r.hget(vmid, 'msg')
        job = r.hget(vmid, 'job')
        file = r.hget(vmid, 'file')
        return {vmid: {'status': status, 'message': msg, 'job': job, 'file': file}}, 200

class AllStatus(Resource):
    @auth.login_required
    def get(self):
        response = []
        for worker in workers:
            print(worker)
        for vmid in r.smembers('joblock'):
            status = r.hget(vmid, 'state')
            msg = r.hget(vmid, 'msg')
            job = r.hget(vmid, 'job')
            file = r.hget(vmid, 'file')
            response.append({vmid: {'status': status, 'message': msg, 'job': job, 'file': file}})
        return response

class ClearQueue(Resource):
    @auth.login_required
    def post(self):
        response = []
        for vmid in r.smembers('joblock'):
            status = r.hget(vmid, 'state')
            if status == 'enqueued':
                r.srem('joblock', vmid)
                r.hset(vmid, 'state', 'OK')
                response.append({vmid: {"status": "OK", "message": "Successfully dequeued"}})
            else:
                msg = r.hget(vmid, 'msg')
                job = r.hget(vmid, 'job')
                file = r.hget(vmid, 'file')
                response.append({vmid: {'status': status, 'message': msg, 'job': job, 'file': file}})
        return response

class CleanSnaps(Resource):
    @auth.login_required
    def post(self):
        response = []
        if 'deep' in request.args:
            r.hset(0, 'job', 'deepscrub')
            r.hset(0, 'state', 'enqueued')
            r.sadd('joblock', 0)
            return {'Status': "Deep scrub in progress"}, 200
        for vmid in r.smembers('joblock'):
            status = r.hget(vmid, 'state')
            msg = r.hget(vmid, 'msg')
            job = r.hget(vmid, 'job')  # REMOVE- used for testing scrub functions
            if (status == 'error') and (msg == "error creating backup snapshot"):
                # add to scrub queue
                # if job == 'backup': #REMOVE - used for testing scrub functions
                # 	r.hset(vmid, 'retry', 'backup')
                r.hset(vmid, 'job', 'scrub')
                r.hset(vmid, 'state', 'enqueued')

class Poison(Resource):
    @auth.login_required
    def post(self, vmid):
        # catch if container does not exist
        if str(vmid) in r.smembers('joblock'):
            r.hset(vmid, 'job', 'poisoned')
            if r.hget(vmid, 'state') == 'error':
                r.hset(vmid, 'state', 'enqueued')
            return {'status': "Attempting to cancel task or error for container: {}".format(vmid)}, 200
        else:
            return {'status': "Container not in job queue, nothing to do"}, 200

class Info(Resource):
    @auth.login_required
    def get(self):
        response = {}
        barqueHealth = 'OK'
        response['version'] = version
        now = datetime.utcnow().replace(microsecond=0)
        uptime = now - starttime
        response['uptime'] = str(uptime)
        workerTask = {}
        # queue status
        setSize = r.scard('joblock')
        active = 0
        errors = 0
        for member in r.smembers('joblock'):
            if r.hget(member, 'state') == "active":
                active += 1
                workerTask[r.hget(member, 'worker')] = {'1': r.hget(member,'job'), '2':r.hget(member, 'msg'), '3':member}
            if r.hget(member, 'state') == "error":
                errors += 1
                if not barqueHealth == 'CRITICAL':
                    barqueHealth = 'WARNING'
        response['Queue'] = {'jobs in queue': setSize, 'active': active, 'errors': errors}
        # worker status
        workerStatus = {}
        print(workerTask.keys())
        for worker in workers:
            if worker.is_alive():
                healthy = "Alive"
            else:
                healthy = "Dead"
                barqueHealth = 'CRITICAL'
            try:
                task = workerTask[str(worker.pid)][1]
            except:
                task = ""
            try:
                message = workerTask[str(worker.pid)][2]
            except:
                message = ""
            try:
                container = workerTask[str(worker.pid)][3]
            except:
                container = ""
            workerStatus[worker.name] = {'Health': healthy, 'Job': task, 'Message': message, 'CTID': container}
        response['workers'] = workerStatus
        print(workerTask)
        # destination status
        dests = {}
        for spot in locations.keys():
            if os.path.isdir(locations[spot]):
                healthy = "OK"
            else:
                healthy = "Down"
                barqueHealth = 'CRITICAL'
            dests[spot] = healthy
        response['Storage'] = dests
        response['Health'] = barqueHealth
        return response
class AVtoggle(Resource):
    @auth.login_required
    def post(self):
        if 'node' not in request.args:
            return {'error': 'Node argument required beacuse... it needs a node'}, 400
        node = request.args['node']

        convertAVLocks(node)
        # if 'ctid' not in request.args:
        # 	return {'error': 'Container ID required for locking'}, 400
        if 'switch' not in request.args:
            return {'error': 'switch not specified, should be "on" or "off"'}, 400

        # timestamp = time.time()
        if 'ctid' not in request.args:
            return {'error': 'CTID not specified, required for racing lock'}, 400

        ctid = request.args['ctid']

        if request.args['switch'] == 'off':
            ctid = request.args['ctid']

            try:
                print("node: {}, ctid: {}".format(node, ctid))
                r.sadd(node, ctid)
                cmd = subprocess.check_output(
                    "ssh -t root@{} '/opt/sophos-av/bin/savdctl disable'".format(node), shell=True)
                print("disabling antivirus on node: {}, output: {}".format(node, cmd))
            except:
                r.srem(node, ctid)
                return {'state': 'error'}, 200
            return {'state': 'disabling'}, 200

        if request.args['switch'] == 'on':
            r.srem(node, ctid)
            if r.type(node) == "none":
                try:
                    print("node: {}".format(node))
                    cmd = subprocess.check_output(
                        "ssh -t root@{} '/opt/sophos-av/bin/savdctl enable'".format(node), shell=True)
                    r.hdel(node, ctid)
                    print("enabling antivirus on node: {}, output: {}".format(node, cmd))
                except:
                    r.sadd(node, ctid)
                    return {'state': 'error'}, 200
                return {'state': 'enabling'}, 200
            else:
                locked_by = ", ".join(r.sunion(node))
                return {'state': "enabling deferred, another CTID has the lock by {}".format(locked_by)}, 200

    @auth.login_required
    def get(self):
        if 'node' not in request.args:
            return {'error': 'Node argument required beacuse... it needs a node'}, 400
        node = request.args['node']
        active = None
        try:
            print("node: {}".format(node))
            cmd = subprocess.check_output("ssh -t root@{} '/opt/sophos-av/bin/savdstatus'".format(node), shell=True)
            active = cmd.strip()
        except subprocess.CalledProcessError as e:
            active = e.output.strip()
        except:
            print("error getting status")
            return {'error': 'problem getting status'}, 500

        locked_by = list(r.smembers(node))
        if 'scanning is running' in active:
            return {'active': True, 'status': active, "locked_by": locked_by}, 200
        elif 'scanning is not running' in active:
            return {'active': False, 'status': active, "locked_by": locked_by}, 200
        else:
            return {'error': "Problem determining AV status"}, 500

class Migrate(Resource):
    @auth.login_required
    def post(self, vmid):
        # Check if file is specified
        if 'file' not in request.args:
            return {'error': 'Resource requires a file argument'}, 400
        # Check if destination container does not exist
        if not checkConf(vmid):
            return {'error': "{} is not a valid CTID"}, 400
        # Check if cluster is specified
        if 'cluster' not in request.args:
            return {'error': 'Resource requires a cluster argument (dtla01, dtla02, dtla03, dtla04, ny01)'}, 400
        # Check if container is available
        if str(vmid) in r.smembers('joblock'):
            return {'error': "VMID locked, another operation is in progress for container: {}".format(vmid)}, 409

        r.hset(vmid, 'job', 'migrate')
        r.hset(vmid, 'file', request.args['file'])
        r.hset(vmid, 'worker', '')
        r.hset(vmid, 'msg', '')
        r.hset(vmid, 'target_cluster', request.args['cluster'])
        r.hset(vmid, 'state', 'enqueued')
        r.sadd('joblock', vmid)

        return {'status': "restore job created for CTID {}".format(vmid)}, 202

api.add_resource(ListAllBackups, '/barque/')
api.add_resource(ListBackups, '/barque/<int:vmid>')
api.add_resource(Backup, '/barque/<int:vmid>/backup')
api.add_resource(BackupAll, '/barque/all/backup')
api.add_resource(Restore, '/barque/<int:vmid>/restore')
api.add_resource(DeleteBackup, '/barque/<int:vmid>/delete')
api.add_resource(Status, '/barque/<int:vmid>/status')
api.add_resource(AllStatus, '/barque/all/status')
api.add_resource(Info, '/barque/info')
api.add_resource(ClearQueue, '/barque/all/clear')
api.add_resource(CleanSnaps, '/barque/all/clean')
api.add_resource(Poison, '/barque/<int:vmid>/poison')
api.add_resource(AVtoggle, '/barque/avtoggle')
api.add_resource(Migrate, '/barque/<int:vmid>/migrate')

def sanitize():
    for item in r.smembers('joblock'):
        status = r.hget(item, 'state')
        if status != 'error':
            r.srem('joblock', item)

@auth.verify_password
def verify(username, password):
    if not (username and password):
        return False
    return admin_auth.get(username) == password

def checkConf(vmid):
    # catch if container does not exist
    config_file = ""
    config_target = "{}.conf".format(vmid)
    for paths, dirs, files in os.walk('/etc/pve/nodes'):
        if config_target in files:
            config_file = os.path.join(paths, config_target)
            print(config_file)
    if len(config_file) == 0:
        r.hset(vmid, 'state', 'error')
        r.hset(vmid, 'msg', '{} is invalid CTID'.format(vmid))
        return False
    return True

def checkDest(dest):
    if dest in locations:
        directory = locations[dest]
        if os.path.exists(directory):
            return True, None, None
        else:
            cmd = subprocess.check_output('/bin/bash /etc/pve/utilities/detect_stale.sh', shell=True)
            if os.path.exists(directory):
                return True, None, None
            else:
                return False, {'error': '{} is not currently accessible'.format(directory)}, 500
    else:
        return False, {'error': '{} is not a configured destination'.format(dest)}, 400

# remove old style storage
def convertAVLocks(node):
    if r.type(node) != 'hash':
        return
    locked_by = r.hget(node, "ctid")
    r.hdel(node, 'ctid')
    r.sadd(node, locked_by)

if __name__ == '__main__':
    starttime = datetime.utcnow().replace(microsecond=0)
    r = redis.Redis(host=r_host, port=r_port, password=r_pw)
    sanitize()
    print("redis connection successful")
    for i in range(minions):
        p = Worker()
        workers.append(p)
        p.start()
        print("worker started")
    app.run(host=__host, port=__port, debug=True, ssl_context=(cert, key), use_reloader=False)