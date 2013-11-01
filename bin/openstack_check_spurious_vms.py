#!/usr/bin/env python
# -*- coding: utf-8 -*-#
# @(#)openstack_check_spurious_vms.py
#
#
# Copyright (C) 2013, GC3, University of Zurich. All rights reserved.
#
#
# This program is free software; you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the
# Free Software Foundation; either version 2 of the License, or (at your
# option) any later version.
#
# This program is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# General Public License for more details.
#
# You should have received a copy of the GNU General Public License along
# with this program; if not, write to the Free Software Foundation, Inc.,
# 59 Temple Place, Suite 330, Boston, MA 02111-1307 USA

from __future__ import print_function

__docformat__ = 'reStructuredText'
__author__ = 'Antonio Messina <antonio.s.messina@gmail.com>'


# pdsh -g nova-compute -l root virsh list | grep instance | (while read node virsh_id nova_id rest; do
#   node=$(echo $node | tr -d :)
#   row_id=0x$(echo $nova_id | cut -d- -f2)
#   deleted=$(ssh -n root@cloud1.gc3 mysql nova -e "'select id from instances where id=$row_id and deleted=1'")
#   [ -z "$deleted" ] && continue
#   echo ssh -n -l root $node virsh destroy $virsh_id "# $nova_id"
#   done
# )

import argparse
from collections import defaultdict
import re
import multiprocessing as mp
import subprocess
import sys

from nova import db
from nova import context
from nova import flags
from nova.compute import instance_types
from nova.openstack.common import cfg
from nova.openstack.common import log as logging

logging.setup("nova")
log = logging.getLogger("nova")
verbose = 0

FLAGS = flags.FLAGS
args = flags.parse_args(['openstack_free_usage'])

def debug(*args):
    if verbose > 2:
        print(*args)

def info(*args):
    if verbose > 1:
        print(*args)

class RunVirsh(mp.Process):
    def __init__(self, host, sshopts, queue):
        cmd = ['ssh'] + sshopts + [host, 'virsh', 'list']
        mp.Process.__init__(self, args=cmd)
        self.host = host
        self.queue = queue

    def run(self):
        cmd = str.join(' ', self._args)
        debug("Executing command `%s`" % cmd)
        pipe = subprocess.Popen(self._args, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE)
        stdout, stderr = pipe.communicate()
        ret = pipe.returncode
        debug("Command %s exited with status %d" % (cmd, ret))
        self.queue.put((ret, self.host, stdout, stderr))


def parse_virsh_output(output):
    """
    Parses the output of virsh list.

    @returns a list of tuple (`id`, `name`) containing id and names of
    all the running instances.
    """
    vm_re = re.compile(' *(?P<id>[0-9]+) +(?P<name>[^ ]+) +(running|idle|paused|shutdown|shut off|crashed|dying|pmsuspended)')
    instances = []
    for line in output.splitlines():
        match = vm_re.match(line)
        if match:
            name = match.group('name')
            hexid = int(name[name.rfind('-')+1:], 16)
            instances.append({'id':match.group('id'),
                              'name': match.group('name'),
                              'nova_id': hexid})
    return instances

def maybe_kill_instance(kill, host, sshopts, vmid):
    """
    SSh to `host` using ssh options `sshopts` and kill the VM with id
    `vmid` unless `dryrun` is `False`, otherwise print the ssh command
    to run to destroy the instance.
    """
    cmd = ['ssh'] + sshopts + [host, 'virsh', 'destroy', str(vmid)]
    if not kill:
        print(str.join(' ', cmd))
        return 0
    else:
        info("Executing command `%s`" % cmd)
        pipe = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE)
        stdout, stderr = pipe.communicate()
        retcodes.append(pipe.returncode)
        if pipe.returncode != 0:
            print("Error running command %s" % (cmd))
            print("Command return code: %d, stderr: %s" % (pipe.returncode, stderr))
        return pipe.returncode

def main(args):
    ctxt = context.get_admin_context()
    instances = db.instance_get_all(ctxt)

    # Get a list of all running instances
    instances_by_hosts = defaultdict(list)
    for instance in instances:
        host = instance.host[:instance.host.find('.')]
        instances_by_hosts[instance.host].append(instance.id)

    # Get a list of all the compute nodes
    compute_nodes = []
    for s in db.service_get_all(ctxt):
        if not s.disabled:
            compute_nodes.extend(s.compute_node)

    # Get a list of all the vms running on each compute node
    vms_on_host = {}
    queue = mp.Queue()
    jobs = []
    for node in compute_nodes:
        hostname = node.hypervisor_hostname
        job = RunVirsh(hostname, args.sshopts, queue)
        jobs.append(job)
        job.start()

    # Wait until all of them are done.
    print("Waiting until all the jobs are done")
    for job in jobs:
        job.join()

    while not queue.empty():
        (ret, host, stdout, stderr) = queue.get()

        if ret != 0:
            print("Error `%d` on host %s: %s" % (ret,
                                                 host,
                                                 stderr))
        else:
            vms_on_host[host] = parse_virsh_output(stdout)
            debug("host %s: %d instances" % (host, len(vms_on_host[host])))

    for host, vms in vms_on_host.items():
        shorthost = host[:host.find('.')]
        for vm in vms:
            vmid = vm['nova_id']
            if vmid not in instances_by_hosts[host] and \
              vmid not in instances_by_hosts[shorthost]:
                maybe_kill_instance(args.kill, host, args.sshopts, vm.group('id'))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument('-v', '--verbose', help="Increase verbosity.", action="count", default=0)
    parser.add_argument('-k', '--kill', help='Kill spurious virtual machines', default=False, action="store_true")
    parser.add_argument('sshopts', nargs='*', help="ssh options.")
    args = parser.parse_args()
    verbose = args.verbose
    sys.argv = ['openstack_check_spurious_vms']
    main(args)