#
# Copyright 2010-2016 Red Hat, Inc.
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA
#
# Refer to the README and COPYING files for full details of the license
#


"""
Generic LVM interface wrapper

Incapsulates the actual LVM mechanics.
"""
from __future__ import absolute_import

import errno

import os
import re
import pwd
import glob
import grp
import logging
from collections import namedtuple
import pprint as pp
import threading
import time

from itertools import chain
from subprocess import list2cmdline
import six

from vdsm import constants
from vdsm import utils
from vdsm.common import commands
from vdsm.common import errors
from vdsm.common import logutils
from vdsm.common.compat import subprocess
from vdsm.common.units import MiB

from vdsm.storage import devicemapper
from vdsm.storage import constants as sc
from vdsm.storage import exception as se
from vdsm.storage import lsof
from vdsm.storage import misc
from vdsm.storage import multipath
from vdsm.storage import rwlock

from vdsm.config import config

log = logging.getLogger("storage.LVM")

PV_FIELDS = ("uuid,name,size,vg_name,vg_uuid,pe_start,pe_count,"
             "pe_alloc_count,mda_count,dev_size,mda_used_count")
PV_FIELDS_LEN = len(PV_FIELDS.split(","))

VG_FIELDS = ("uuid,name,attr,size,free,extent_size,extent_count,free_count,"
             "tags,vg_mda_size,vg_mda_free,lv_count,pv_count,pv_name")
VG_FIELDS_LEN = len(VG_FIELDS.split(","))

LV_FIELDS = "uuid,name,vg_name,attr,size,seg_start_pe,devices,tags"
LV_FIELDS_LEN = len(LV_FIELDS.split(","))

VG_ATTR_BITS = ("permission", "resizeable", "exported",
                "partial", "allocation", "clustered")
VG_ATTR = namedtuple("VG_ATTR", VG_ATTR_BITS)

LV_ATTR_BITS = ("voltype", "permission", "allocations", "fixedminor", "state",
                "devopen", "target", "zero")
LV_ATTR = namedtuple("LV_ATTR", LV_ATTR_BITS)

# Returned by vgs and pvs for missing pv or unknown vg name.
UNKNOWN = "[unknown]"


class InvalidOutputLine(errors.Base):
    msg = "Invalid {self.command} command ouptut line: {self.line!r}"

    def __init__(self, command, line):
        self.command = command
        self.line = line


class PV(namedtuple("_PV", PV_FIELDS + ",guid")):
    __slots__ = ()

    @classmethod
    def fromlvm(cls, *args):
        """
        Create PV from lvm pvs command output.
        """
        guid = os.path.basename(args[1])
        args += (guid,)
        return cls(*args)

    def is_stale(self):
        return False

    def is_metadata_pv(self):
        """
        This method returns boolean indicating whether this pv is used for
        storing the vg metadata. When we create a vg we create on all the pvs 2
        metadata areas but enable them only on one of the pvs, for that pv the
        mda_used_count should be therefore 2 - see createVG().
        """
        return self.mda_used_count == '2'


class VG(namedtuple("_VG", VG_FIELDS + ",writeable,partial")):
    __slots__ = ()

    @classmethod
    def fromlvm(cls, *args):
        """
        Create VG from lvm vgs command output.
        """
        args = list(args)
        # Convert tag string into tuple.
        tags = _tags2Tuple(args[VG._fields.index("tags")])
        args[VG._fields.index("tags")] = tags
        # Convert attr string into named tuple fields.
        # tuple("wz--n-") = ('w', 'z', '-', '-', 'n', '-')
        sAttr = args[VG._fields.index("attr")]
        attr_values = tuple(sAttr[:len(VG_ATTR._fields)])
        attrs = VG_ATTR(*attr_values)
        args[VG._fields.index("attr")] = attrs
        # Convert pv_names list to tuple.
        args[VG._fields.index("pv_name")] = \
            tuple(args[VG._fields.index("pv_name")])
        # Add properties. Should be ordered as VG_PROPERTIES.
        args.append(attrs.permission == "w")  # Writable
        args.append(VG_OK if attrs.partial == "-" else VG_PARTIAL)  # Partial
        return cls(*args)

    def is_stale(self):
        return False


class LV(namedtuple("_LV", LV_FIELDS + ",writeable,opened,active")):
    __slots__ = ()

    @classmethod
    def fromlvm(cls, *args):
        """
        Create LV from lvm pvs command output.
        """
        args = list(args)
        # Convert tag string into tuple.
        tags = _tags2Tuple(args[cls._fields.index("tags")])
        args[LV._fields.index("tags")] = tags
        # Convert attr string into named tuple fields.
        sAttr = args[cls._fields.index("attr")]
        attr_values = tuple(sAttr[:len(LV_ATTR._fields)])
        attrs = LV_ATTR(*attr_values)
        args[cls._fields.index("attr")] = attrs
        # Add properties. Should be ordered as VG_PROPERTIES.
        args.append(attrs.permission == "w")  # writable
        args.append(attrs.devopen == "o")     # opened
        args.append(attrs.state == "a")       # active
        return cls(*args)

    def is_stale(self):
        return False


class Stale(namedtuple("_Stale", "name")):
    __slots__ = ()

    def is_stale(self):
        return True


class Unreadable(namedtuple("_Unreadable", "name")):
    __slots__ = ()

    def is_stale(self):
        return True

    def __getattr__(self, attrName):
        log.warning("%s can't be reloaded, please check your storage "
                    "connections.", self.name)
        raise AttributeError("Failed reload: %s" % self.name)

# VG states
VG_OK = "OK"
VG_PARTIAL = "PARTIAL"

SEPARATOR = "|"
LVM_NOBACKUP = ("--autobackup", "n")
LVM_FLAGS = ("--noheadings", "--units", "b", "--nosuffix", "--separator",
             SEPARATOR, "--ignoreskippedcluster")

PV_PREFIX = "/dev/mapper"
# Assuming there are no spaces in the PV name
re_pvName = re.compile(PV_PREFIX + r'[^\s\"]+', re.MULTILINE)

PVS_CMD = ("pvs",) + LVM_FLAGS + ("-o", PV_FIELDS)
VGS_CMD = ("vgs",) + LVM_FLAGS + ("-o", VG_FIELDS)
LVS_CMD = ("lvs",) + LVM_FLAGS + ("-o", LV_FIELDS)

# FIXME we must use different METADATA_USER ownership for qemu-unreadable
# metadata volumes
USER_GROUP = constants.DISKIMAGE_USER + ":" + constants.DISKIMAGE_GROUP

# Runtime configuration notes
# ===========================
#
# This configuration is used for all commands using --config option. This
# overrides various options built into lvm comamnds, or defined in
# /etc/lvm/lvm.conf or /etc/lvm/lvmlocl.conf.
#
# hints="none"
# ------------
# prevent from lvm to remember which devices are PVs so that lvm can avoid
# scanning other devices that are not PVs, since we create and remove PVs from
# other hosts, then the hints might be wrong.  Finally because oVirt host is
# like to use strict lvm filter, the hints are not needed.  Disable hints for
# lvm commands run by vdsm, even if hints are enabled on the host.
#
# obtain_device_list_from_udev=0
# ------------------------------
# Avoid random faiures in lvcreate an lvchange seen during stress tests
# (using tests/storage/stress/reload.py). This was disabled in RHEL 6, and
# enabled back in RHEL 7, and seems to be broken again in RHEL 8.

LVMCONF_TEMPLATE = """
devices {
 preferred_names=["^/dev/mapper/"]
 ignore_suspended_devices=1
 write_cache_state=0
 disable_after_error_count=3
 filter=%(filter)s
 hints="none"
 obtain_device_list_from_udev=0
}
global {
 locking_type=%(locking_type)s
 prioritise_write_locks=1
 wait_for_locks=1
 use_lvmetad=0
}
backup {
 retain_min=50
 retain_days=0
}
"""

USER_DEV_LIST = [d for d in config.get("irs", "lvm_dev_whitelist").split(",")
                 if d is not None]


def _buildFilter(devices):
    devices = set(d.strip() for d in chain(devices, USER_DEV_LIST))
    devices.discard('')
    if devices:
        # Accept specified devices, reject everything else.
        # ["a|^/dev/1$|^/dev/2$|", "r|.*|"]
        devices = sorted(d.replace(r'\x', r'\\x') for d in devices)
        pattern = "|".join("^{}$".format(d) for d in devices)
        accept = '"a|{}|", '.format(pattern)
    else:
        # Reject all devices.
        # ["r|.*|"]
        accept = ''
    return '[{}"r|.*|"]'.format(accept)


def _buildConfig(dev_filter, locking_type):
    conf = LVMCONF_TEMPLATE % {
        "filter": dev_filter,
        "locking_type": locking_type,
    }
    return conf.replace("\n", " ").strip()


#
# Make sure that "args" is suitable for consumption in interfaces
# that expect an iterabale argument. As we pass as an argument on of
# None, list or string, we check only None and string.
# Once we enforce iterables in all functions, this function should be removed.
#
def normalize_args(args=None):
    if args is None:
        args = []
    elif isinstance(args, six.string_types):
        args = [args]

    return args


def _tags2Tuple(sTags):
    """
    Tags comma separated string as a list.

    Return an empty tuple for sTags == ""
    """
    return tuple(sTags.split(",")) if sTags else tuple()


class LVMRunner(object):
    """
    Does actual execution of the LVM command and handle output, e.g. decode
    output or log warnings.
    """

    # Warnings written to LVM stderr that should not be logged as warnings.
    SUPPRESS_WARNINGS = re.compile(
        "|".join([
            "WARNING: This metadata update is NOT backed up",
            (r"WARNING: ignoring metadata seqno \d+ on /dev/mapper/\w+ for "
             r"seqno \d+ on /dev/mapper/\w+ for VG \w+"),
            r"WARNING: Inconsistent metadata found for VG \w+"
        ]),
        re.IGNORECASE)

    def run(self, cmd):
        """
        Run LVM command, logging warnings for successful commands.

        An example case is when LVM decide to fix VG metadata when running a
        command that should not change the metadata on non-SPM host. In this
        case LVM will log this warning:

            WARNING: Inconsistent metadata found for VG xxx-yyy-zzz - updating
            to use version 42

        We log warnings only for successful commands since callers are already
        handling failures.
        """

        rc, out, err = self._run_command(cmd)

        out = out.decode("utf-8").splitlines()
        err = err.decode("utf-8").splitlines()

        err = [s for s in err if not self.SUPPRESS_WARNINGS.search(s)]

        if rc == 0 and err:
            log.warning("Command %s succeeded with warnings: %s", cmd, err)

        return rc, out, err

    def _run_command(self, cmd):
        p = commands.start(
            cmd,
            sudo=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )
        out, err = commands.communicate(p)
        return p.returncode, out, err


class LVMCache(object):
    """
    Keep all the LVM information.
    """

    # Maximum number of concurent commands. This is important both for
    # limiting the I/O caused by lvm comamdns during refreshes, and for
    # having exponential back-off for read-only commands.
    MAX_COMMANDS = 10

    # Read-only commands may fail if the SPM modified VG metadata while
    # a read-only command was reading the metadata. We retry the command
    # with exponential back-off delay to recover for these failures.
    #
    # Testing with 10 times higher load compared with real systems show that
    # 1.2% of the read-only commands failed and needed up to 3 retries to
    # succeed. 97.45% of the failing commands succeeded after 1 retry. 2.3% of
    # the failing commands needed 2 retries, and 0.21% of the commands needed 3
    # retries.
    #
    # Here are stats from a test using tests/storage/stress/extend.py:
    #
    # $ python extend.py log-stats run-regular.log
    # {
    #   "activating": 5000,
    #   "creating": 5000,
    #   "deactivating": 4999,
    #   "extend-rate": 4.684750527055517,
    #   "extending": 99996,
    #   "max-retry": 3,
    #   "read-only": 109995,
    #   "refreshing": 99996,
    #   "removing": 4999,
    #   "retries": 1374,
    #   "retry 1": 1339,
    #   "retry 2": 32,
    #   "retry 3": 3,
    #   "retry-rate": 0.012491476885312968,
    #   "total-time": 21345,
    #   "warnings": 3766
    # }
    #
    # Use 4 retries for extra safety. This translates to typical delay of 0.1
    # seconds, and worst case delay of 1.5 seconds.
    READ_ONLY_RETRIES = 4
    RETRY_DELAY = 0.1
    RETRY_BACKUP_OFF = 2

    def __init__(self, cmd_runner=LVMRunner(), cache_lvs=False):
        """
        Arguemnts:
            cmd_runner (LVMRunner): used to run LVM command
            cache_lvs (bool): use LVs cache when looking up LVs. False by
                defualt since it works only on the SPM.
        """
        self._runner = cmd_runner
        self._cache_lvs = cache_lvs
        self._read_only_lock = rwlock.RWLock()
        self._read_only = False
        self._filter = None
        self._filterStale = True
        self._filterLock = threading.Lock()
        self._lock = threading.Lock()
        self._cmd_sem = threading.BoundedSemaphore(self.MAX_COMMANDS)
        self._stalepv = True
        self._stalevg = True
        self._freshlv = set()
        self._pvs = {}
        self._vgs = {}
        self._lvs = {}
        self._stats = CacheStats()

    @property
    def stats(self):
        return self._stats

    def set_read_only(self, value):
        """
        Called when the SPM is started or stopped.
        """
        # Take an exclusive lock, so we wait for commands using the previous
        # mode before switching to the new mode.
        with self._read_only_lock.exclusive:
            if self._read_only != value:
                log.info("Switching to read_only=%s", value)
                self._read_only = value

    def _getCachedFilter(self):
        with self._filterLock:
            if self._filterStale:
                self._filter = _buildFilter(multipath.getMPDevNamesIter())
                self._filterStale = False
            return self._filter

    def _addExtraCfg(self, cmd, devices=tuple()):
        newcmd = [constants.EXT_LVM, cmd[0]]

        if devices:
            dev_filter = _buildFilter(devices)
        else:
            dev_filter = self._getCachedFilter()

        # TODO: remove locking type configuration
        # once we require only lvm-2.03
        conf = _buildConfig(
            dev_filter=dev_filter,
            locking_type="4" if self._read_only else "1")

        newcmd += ["--config", conf]

        if len(cmd) > 1:
            newcmd += cmd[1:]

        return newcmd

    def invalidateFilter(self):
        self._filterStale = True

    def invalidateCache(self):
        self.invalidateFilter()
        self.flush()

    def cmd(self, cmd, devices=tuple()):
        # Take a shared lock, so set_read_only() can wait for commands using
        # the previous mode.
        with self._cmd_sem, self._read_only_lock.shared:
            tries = 1

            # 1. Try the command with fast specific filter including the
            # specified devices. If the command succeeded and wanted output was
            # returned we are done.
            full_cmd = self._addExtraCfg(cmd, devices)
            rc, out, err = self._runner.run(full_cmd)
            if rc == 0:
                return rc, out, err

            # 2. Retry the command with a wider filter, in case the we failed
            # or got no data because of a stale filter.
            self.invalidateFilter()
            wider_cmd = self._addExtraCfg(cmd)
            if wider_cmd != full_cmd:
                log.warning(
                    "Command with specific filter failed or returned no data, "
                    "retrying with a wider filter, cmd=%r rc=%r out=%r err=%r",
                    full_cmd, rc, out, err)
                full_cmd = wider_cmd
                tries += 1
                rc, out, err = self._runner.run(full_cmd)
                if rc == 0:
                    return rc, out, err

            # 3. If we run in read-only mode, retry the command in case it
            # failed because VG metadata was modified while the command was
            # reading the metadata.
            if rc != 0 and self._read_only:
                delay = self.RETRY_DELAY
                for retry in range(1, self.READ_ONLY_RETRIES + 1):
                    log.warning(
                        "Retry %d failed, retrying in %.2f seconds, cmd=%r "
                        "rc=%r err=%r",
                        retry, delay, full_cmd, rc, err)

                    time.sleep(delay)
                    delay *= self.RETRY_BACKUP_OFF

                    tries += 1
                    rc, out, err = self._runner.run(full_cmd)
                    if rc == 0:
                        return rc, out, err

            log.warning("All %d tries have failed: cmd=%r rc=%r err=%r",
                        tries, full_cmd, rc, err)
            return rc, out, err

    def __str__(self):
        return ("PVS:\n%s\n\nVGS:\n%s\n\nLVS:\n%s" %
                (pp.pformat(self._pvs),
                 pp.pformat(self._vgs),
                 pp.pformat(self._lvs)))

    def bootstrap(self):
        self._reloadpvs()
        self._reloadvgs()
        self._loadAllLvs()

    def _reloadpvs(self, pvName=None):
        cmd = list(PVS_CMD)

        pvNames = normalize_args(pvName)
        if pvNames:
            cmd.extend(pvNames)

        rc, out, err = self.cmd(cmd)

        with self._lock:
            updatedPVs = {}

            if rc != 0:
                pvNames = pvNames if pvNames else self._pvs
                for p in pvNames:
                    pv = self._pvs.get(p)
                    if pv and pv.is_stale():
                        pv = Unreadable(pv.name)
                        self._pvs[p] = pv
                        updatedPVs[p] = pv

                if updatedPVs:
                    # This may be a real error (failure to reload existing PV)
                    # or no error at all (failure to reload non-existing PV),
                    # so we cannot make this an error.
                    log.warning(
                        "Marked pvs=%r as Unreadable due to reload failure",
                        logutils.Head(updatedPVs, max_items=20))

                return updatedPVs

            for line in out:
                fields = [field.strip() for field in line.split(SEPARATOR)]
                if len(fields) != PV_FIELDS_LEN:
                    raise InvalidOutputLine("pvs", line)

                pv = PV.fromlvm(*fields)
                if pv.name == UNKNOWN:
                    log.error("Missing pv: %s in vg: %s", pv.uuid, pv.vg_name)
                    continue
                self._pvs[pv.name] = pv
                updatedPVs[pv.name] = pv

            # Remove stalePVs
            stalePVs = [name for name in (pvNames or self._pvs)
                        if name not in updatedPVs]
            for name in stalePVs:
                if name in self._pvs:
                    log.warning("Removing stale PV %s", name)
                    del self._pvs[name]

            # If we updated all the PVs drop stale flag
            if not pvName:
                self._stalepv = False

        return updatedPVs

    def _getVGDevs(self, vgNames):
        devices = []
        with self._lock:
            for name in vgNames:
                try:
                    pvs = self._vgs[name].pv_name  # pv_names tuple
                except (KeyError, AttributeError):  # Yet unknown VG, stale
                    devices = tuple()
                    break  # unknownVG = True
                else:
                    devices.extend(pvs)
            else:  # All known VGs
                devices = tuple(devices)
        return devices

    def _reloadvgs(self, vgName=None):
        cmd = list(VGS_CMD)

        vgNames = normalize_args(vgName)
        if vgNames:
            cmd.extend(vgNames)

        rc, out, err = self.cmd(cmd, self._getVGDevs(vgNames))

        with self._lock:
            if rc != 0:
                unreadable_vgs = []
                for v in (vgNames or self._vgs):
                    vg = self._vgs.get(v)
                    if vg and vg.is_stale():
                        self._vgs[v] = Unreadable(vg.name)
                        unreadable_vgs.append(vg.name)

                if unreadable_vgs:
                    # This may be a real error (failure to reload existing VG)
                    # or no error at all (failure to reload non-existing VG),
                    # so we cannot make this an error.
                    log.warning(
                        "Marked vgs=%r as Unreadable due to reload failure",
                        logutils.Head(unreadable_vgs, max_items=20))

                # NOTE: vgs may return useful output even on failure, so we
                # don't retrun here.

            updatedVGs = {}
            vgsFields = {}
            for line in out:
                fields = [field.strip() for field in line.split(SEPARATOR)]
                if len(fields) != VG_FIELDS_LEN:
                    raise InvalidOutputLine("vgs", line)

                uuid = fields[VG._fields.index("uuid")]
                pvNameIdx = VG._fields.index("pv_name")
                pv_name = fields[pvNameIdx]
                if pv_name == UNKNOWN:
                    # PV is missing, e.g. device lost of target not connected
                    continue
                if uuid not in vgsFields:
                    fields[pvNameIdx] = [pv_name]  # Make a pv_names list
                    vgsFields[uuid] = fields
                else:
                    vgsFields[uuid][pvNameIdx].append(pv_name)
            for fields in six.itervalues(vgsFields):
                vg = VG.fromlvm(*fields)
                if int(vg.pv_count) != len(vg.pv_name):
                    log.error("vg %s has pv_count %s but pv_names %s",
                              vg.name, vg.pv_count, vg.pv_name)
                self._vgs[vg.name] = vg
                updatedVGs[vg.name] = vg

            # Remove stale VGs
            staleVGs = [name for name in (vgNames or self._vgs)
                        if name not in updatedVGs]
            for name in staleVGs:
                if name in self._vgs:
                    log.warning("Removing stale VG %s", name)
                    del self._vgs[name]
                    # Remove fresh lvs indication of the vg removed from cache.
                    self._freshlv.discard(name)

            # If we updated all the VGs drop stale flag
            if not vgName:
                self._stalevg = False

        return updatedVGs

    def _reloadlvs(self, vgName, lvNames=None):
        cmd = list(LVS_CMD)

        lvNames = normalize_args(lvNames)
        if lvNames:
            cmd.extend("%s/%s" % (vgName, lvName) for lvName in lvNames)
        else:
            cmd.append(vgName)

        rc, out, err = self.cmd(cmd, self._getVGDevs((vgName,)))

        with self._lock:
            updatedLVs = {}

            if rc != 0:
                if not lvNames:
                    lvNames = (lvn for vgn, lvn in self._lvs if vgn == vgName)
                for lvName in lvNames:
                    key = (vgName, lvName)
                    lv = self._lvs.get(key)
                    if lv and lv.is_stale():
                        lv = Unreadable(lv.name)
                        self._lvs[key] = lv
                        updatedLVs[key] = lv

                if updatedLVs:
                    # This may be a real error (failure to reload existing LV)
                    # or no error at all (failure to reload non-existing LV),
                    # so we cannot make this an error.
                    log.warning(
                        "Marked lvs=%r as Unreadable due to reload failure",
                        logutils.Head(updatedLVs, max_items=20))

                return updatedLVs

            for line in out:
                fields = [field.strip() for field in line.split(SEPARATOR)]
                if len(fields) != LV_FIELDS_LEN:
                    raise InvalidOutputLine("lvs", line)

                lv = LV.fromlvm(*fields)
                # For LV we are only interested in its first extent
                if lv.seg_start_pe == "0":
                    self._lvs[(lv.vg_name, lv.name)] = lv
                    updatedLVs[(lv.vg_name, lv.name)] = lv

            # Determine if there are stale LVs
            if lvNames:
                staleLVs = [lvName for lvName in lvNames
                            if (vgName, lvName) not in updatedLVs]
            else:
                # All the LVs in the VG
                staleLVs = [lvName for v, lvName in self._lvs
                            if (v == vgName) and
                            ((vgName, lvName) not in updatedLVs)]

            for lvName in staleLVs:
                if (vgName, lvName) in self._lvs:
                    log.warning("Removing stale lv: %s/%s", vgName, lvName)
                    del self._lvs[(vgName, lvName)]

            if not lvNames:
                self._freshlv.add(vgName)

            log.debug("lvs reloaded")

        return updatedLVs

    def _loadAllLvs(self):
        """
        Used only during bootstrap.
        """
        cmd = list(LVS_CMD)
        rc, out, err = self.cmd(cmd)

        if rc == 0:
            new_lvs = {}
            for line in out:
                fields = [field.strip() for field in line.split(SEPARATOR)]
                if len(fields) != LV_FIELDS_LEN:
                    raise InvalidOutputLine("lvs", line)

                lv = LV.fromlvm(*fields)
                # For LV we are only interested in its first extent
                if lv.seg_start_pe == "0":
                    new_lvs[(lv.vg_name, lv.name)] = lv

            with self._lock:
                self._lvs = new_lvs
                self._freshlv = {vg_name for vg_name, _ in self._lvs}

        return self._lvs.copy()

    def _invalidatepvs(self, pvNames):
        pvNames = normalize_args(pvNames)
        with self._lock:
            for pvName in pvNames:
                self._pvs[pvName] = Stale(pvName)

    def _invalidatevgpvs(self, vgName):
        with self._lock:
            for pv in self._pvs.values():
                if not pv.is_stale() and pv.vg_name == vgName:
                    self._pvs[pv.name] = Stale(pv.name)

    def _invalidateAllPvs(self):
        with self._lock:
            self._stalepv = True
            self._pvs.clear()

    def _invalidatevgs(self, vgNames):
        vgNames = normalize_args(vgNames)
        with self._lock:
            for vgName in vgNames:
                self._vgs[vgName] = Stale(vgName)

    def _invalidateAllVgs(self):
        with self._lock:
            self._stalevg = True
            self._vgs.clear()
            self._freshlv = set()

    def _invalidatelvs(self, vgName, lvNames=None):
        lvNames = normalize_args(lvNames)
        with self._lock:
            # Invalidate LVs in a specific VG
            if lvNames:
                # Invalidate a specific LVs
                for lvName in lvNames:
                    self._lvs[(vgName, lvName)] = Stale(lvName)
            else:
                # Invalidate all the LVs in a given VG
                for lv in self._lvs.values():
                    if not lv.is_stale() and lv.vg_name == vgName:
                        self._lvs[(vgName, lv.name)] = Stale(lv.name)

    def _invalidateAllLvs(self):
        with self._lock:
            self._freshlv = set()
            self._lvs.clear()

    def _removelvs(self, vgName, lvNames=None):
        lvNames = normalize_args(lvNames)
        with self._lock:
            if not lvNames:
                # Find all LVs of the specified VG.
                lvNames = (lvn for vgn, lvn in self._lvs if vgn == vgName)
            for lvName in lvNames:
                self._lvs.pop((vgName, lvName), None)

    def _removevgs(self, vgNames):
        vgNames = normalize_args(vgNames)
        with self._lock:
            for vgName in vgNames:
                self._vgs.pop(vgName, None)

    def flush(self):
        self._invalidateAllPvs()
        self._invalidateAllVgs()
        self._invalidateAllLvs()

    def getPv(self, pvName):
        # Get specific PV
        pv = self._pvs.get(pvName)
        if not pv or pv.is_stale():
            self.stats.miss()
            pvs = self._reloadpvs(pvName)
            pv = pvs.get(pvName)
        else:
            self.stats.hit()
        return pv

    def getAllPvs(self):
        # Get everything we have
        if self._stalepv:
            self.stats.miss()
            pvs = self._reloadpvs()
        else:
            pvs = self._pvs.copy()
            stalepvs = [pv.name for pv in six.itervalues(pvs) if pv.is_stale()]
            if stalepvs:
                self.stats.miss()
                for name in stalepvs:
                    del pvs[name]
                reloaded = self._reloadpvs(stalepvs)
                pvs.update(reloaded)
            else:
                self.stats.hit()
        return list(six.itervalues(pvs))

    def getPvs(self, vgName):
        """
        Returns the pvs of the given vg.
        Reloads pvs only once if some of the pvs are missing or stale.
        """
        stalepvs = []
        pvs = []
        vg = self.getVg(vgName)
        for pvName in vg.pv_name:
            pv = self._pvs.get(pvName)
            if pv is None or pv.is_stale():
                stalepvs.append(pvName)
            else:
                pvs.append(pv)

        if stalepvs:
            self.stats.miss()
            reloadedpvs = self._reloadpvs(pvName=stalepvs)
            pvs.extend(reloadedpvs.values())
        else:
            self.stats.hit()
        return pvs

    def getVg(self, vgName):
        # Get specific VG
        vg = self._vgs.get(vgName)
        if not vg or vg.is_stale():
            self.stats.miss()
            vgs = self._reloadvgs(vgName)
            vg = vgs.get(vgName)
        else:
            self.stats.hit()
        return vg

    def getVgs(self, vgNames):
        """Reloads all the VGs of the set.

        Can block for suspended devices.
        Fills the cache but not uses it.
        Only returns found VGs.
        """
        self.stats.miss()
        return [vg for vgName, vg in six.iteritems(self._reloadvgs(vgNames))
                if vgName in vgNames]

    def getAllVgs(self):
        # Get everything we have
        if self._stalevg:
            self.stats.miss()
            vgs = self._reloadvgs()
        else:
            vgs = self._vgs.copy()
            stalevgs = [vg.name for vg in six.itervalues(vgs) if vg.is_stale()]
            if stalevgs:
                self.stats.miss()
                for name in stalevgs:
                    del vgs[name]
                reloaded = self._reloadvgs(stalevgs)
                vgs.update(reloaded)
            else:
                self.stats.hit()
        return list(six.itervalues(vgs))

    def getLv(self, vgName, lvName=None):
        """
        Get specific LV or all LVs in specified VG.

        If there are any stale LVs reload the whole VG, since it would
        cost us around same efforts anyhow and these stale LVs can
        be in the vg.

        We never return Stale or Unreadable LVs when
        getting all LVs for a VG, but may return a Stale or an Unreadable
        LV when LV name is specified as argument.

        Arguments:
            vgName (str): VG name to query.
            lvName (str): Optional LV name.

        Returns:
            LV nameduple if lvName is specified, otherwise list of LV
            namedtuple for all lvs in VG vgName.
        """

        if lvName:
            # vgName, lvName
            lv = self._lvs.get((vgName, lvName))
            if not lv or lv.is_stale():
                self.stats.miss()
                # while we here reload all the LVs in the VG
                lvs = self._reloadlvs(vgName)
                lv = lvs.get((vgName, lvName))
            else:
                self.stats.hit()

            return lv

        if self._lvs_needs_reload(vgName):
            self.stats.miss()
            lvs = self._reloadlvs(vgName)
        else:
            self.stats.hit()
            lvs = self._lvs.copy()

        lvs = [lv for lv in lvs.values()
               if not lv.is_stale() and (lv.vg_name == vgName)]
        return lvs

    def _lvs_needs_reload(self, vg_name):
        # TODO: Return True only if VG has changed.
        if not self._cache_lvs:
            return True

        if vg_name not in self._freshlv:
            return True

        return any(lv.is_stale()
                   for (vgn, _), lv in self._lvs.items()
                   if vgn == vg_name)


class CacheStats(object):

    def __init__(self):
        self._lock = threading.Lock()
        self._hits = 0
        self._misses = 0

    def info(self):
        with self._lock:
            calls = self._hits + self._misses
            hit_ratio = (100 * self._hits / calls) if calls > 0 else 0
            return {
                "hits": self._hits,
                "misses": self._misses,
                "hit_ratio": hit_ratio
            }

    def clear(self):
        with self._lock:
            self._hits = 0
            self._misses = 0

    def miss(self):
        with self._lock:
            self._misses += 1

    def hit(self):
        with self._lock:
            self._hits += 1


_lvminfo = LVMCache()


def bootstrap(skiplvs=()):
    """
    Bootstrap lvm module

    This function builds the lvm cache and ensure that all unused lvs are
    deactivated, expect lvs matching skiplvs.
    """
    _lvminfo.bootstrap()

    skiplvs = set(skiplvs)

    for vg in _lvminfo.getAllVgs():
        deactivateUnusedLVs(vg.name, skiplvs=skiplvs)


def deactivateUnusedLVs(vgname, skiplvs=()):
    deactivate = []

    # List prepared images LVs if any
    pattern = "{}/{}/*/*".format(sc.P_VDSM_STORAGE, vgname)
    prepared = frozenset(os.path.basename(n) for n in glob.iglob(pattern))

    for lv in _lvminfo.getLv(vgname):
        if lv.active:
            if lv.name in skiplvs:
                log.debug("Skipping active lv: vg=%s lv=%s",
                          vgname, lv.name)
            elif lv.name in prepared:
                log.debug("Skipping prepared volume lv: vg=%s lv=%s",
                          vgname, lv.name)
            elif lv.opened:
                log.debug("Skipping open lv: vg=%s lv=%s", vgname,
                          lv.name)
            else:
                deactivate.append(lv.name)

    if deactivate:
        log.info("Deactivating lvs: vg=%s lvs=%s", vgname, deactivate)
        try:
            _setLVAvailability(vgname, deactivate, "n")
        except se.CannotDeactivateLogicalVolume:
            log.error("Error deactivating lvs: vg=%s lvs=%s", vgname,
                      deactivate)
        # Some lvs are inactive now
        _lvminfo._invalidatelvs(vgname, deactivate)


def invalidateCache():
    _lvminfo.invalidateCache()


def _fqpvname(pv):
    if pv[0] == "/":
        # Absolute path, use as is.
        return pv
    else:
        # Multipath device guid
        return os.path.join(PV_PREFIX, pv)


def _createpv(devices, metadataSize, options=tuple()):
    """
    Size for pvcreate should be with units k|m|g
    pvcreate on a dev that is already a PV but not in a VG returns rc = 0.
    The device is re-created with the new parameters.
    """
    cmd = ["pvcreate"]
    if options:
        cmd.extend(options)
    if metadataSize != 0:
        cmd.extend(("--metadatasize", "%sm" % metadataSize,
                    "--metadatacopies", "2",
                    "--metadataignore", "y"))
    cmd.extend(devices)
    rc, out, err = _lvminfo.cmd(cmd, devices)
    return rc, out, err


def _initpvs(devices, metadataSize, force=False):

    def _initpvs_removeHolders():
        """Remove holders for all devices."""
        for device in devices:
            try:
                devicemapper.removeMappingsHoldingDevice(
                    os.path.basename(device))
            except OSError as e:
                if e.errno == errno.ENODEV:
                    raise se.PhysDevInitializationError("%s: %s" %
                                                        (device, str(e)))
                else:
                    raise

    if force is True:
        options = ("-y", "-ff")
        _initpvs_removeHolders()
    else:
        options = tuple()

    rc, out, err = _createpv(devices, metadataSize, options)
    _lvminfo._invalidatepvs(devices)
    if rc != 0:
        log.error("pvcreate failed with rc=%s", rc)
        log.error("%s, %s", out, err)
        raise se.PhysDevInitializationError(str(devices))


def getLvDmName(vgName, lvName):
    return "%s-%s" % (vgName.replace("-", "--"), lvName)


def _removeVgMapping(vgName):
    """
    Removes the mapping of the specified volume group.
    Utilizes the fact that the mapping created by the LVM looks like that
    e45c12b0--f520--498a--82bb--c6cb294b990f-master
    i.e vg name concatenated with volume name (dash is escaped with dash)
    """
    mappingPrefix = getLvDmName(vgName, "")
    mappings = devicemapper.getAllMappedDevices()

    for mapping in mappings:
        if not mapping.startswith(mappingPrefix):
            continue
        try:
            devicemapper.removeMapping(mapping)
        except Exception as e:
            log.error("Removing VG mapping failed: %s", e)


def changelv(vg, lvs, attrs):
    """
    Change multiple attributes on multiple LVs.

    vg: VG name
    lvs: a single LV name or iterable of LV names.
    attrs: an iterable of (attr, value) pairs),
            e.g. (('--available', 'y'), ('--permission', 'rw')

    Note:
    You may activate an activated LV without error
    but lvchange returns an error (RC=5) when activating rw if already rw
    """

    lvs = normalize_args(lvs)
    # If it fails or not we (may be) change the lv,
    # so we invalidate cache to reload these volumes on first occasion
    lvnames = tuple("%s/%s" % (vg, lv) for lv in lvs)
    cmd = ["lvchange"]
    cmd.extend(LVM_NOBACKUP)
    if isinstance(attrs[0], str):
        # ("--attribute", "value")
        cmd.extend(attrs)
    else:
        # (("--aa", "v1"), ("--ab", "v2"))
        for attr in attrs:
            cmd.extend(attr)
    cmd.extend(lvnames)
    rc, out, err = _lvminfo.cmd(tuple(cmd), _lvminfo._getVGDevs((vg, )))
    _lvminfo._invalidatelvs(vg, lvs)
    if rc != 0:
        raise se.StorageException("%d %s %s\n%s/%s" % (rc, out, err, vg, lvs))


def _setLVAvailability(vg, lvs, available):
    if available not in ("y", "n"):
        raise se.VolumeGroupActionError("available=%r" % available)
    try:
        changelv(vg, lvs, ("--available", available))
    except se.StorageException as e:
        if available == "y":
            raise se.CannotActivateLogicalVolumes(str(e))
        else:
            users = _lvs_proc_info(vg, lvs)
            raise se.CannotDeactivateLogicalVolume(str(e), users)


def _lvs_proc_info(vg, lvs):
    """
    Returns a proc info dict for proccesses currently
    using the given lvs paths of the vg.
    """
    paths = [lvPath(vg, lv) for lv in lvs]
    return {p: list(lsof.proc_info(p)) for p in paths}

#
# Public Object Accessors
#


def getPV(pvName):
    pv = _lvminfo.getPv(_fqpvname(pvName))
    if pv is None:
        raise se.InaccessiblePhysDev((pvName,))
    return pv


def getAllPVs():
    return _lvminfo.getAllPvs()


def testPVCreate(devices, metadataSize):
    """
    Only tests the pv creation.

    Should not affect the cache state.

    Receives guids iterable.
    Returns (un)pvables, (un)succeed guids.
    """
    devs = tuple("%s/%s" % (PV_PREFIX, dev) for dev in devices)

    options = ("--test",)
    rc, out, err = _createpv(devs, metadataSize, options)
    if rc == 0:
        unusedDevs = set(devices)
        usedDevs = set()
    else:
        unusedDevs = set(re_pvName.findall("\n".join(out)))
        usedDevs = set(devs) - set(unusedDevs)
        log.debug("rc: %s, out: %s, err: %s, unusedDevs: %s, usedDevs: %s",
                  rc, out, err, unusedDevs, usedDevs)

    return unusedDevs, usedDevs


def resizePV(vgName, guid):
    """
    In case the LUN was increased on storage server, in order to see the
    changes it is needed to resize the PV after the multipath devices have
    been resized

    Raises se.CouldNotResizePhysicalVolume if pvresize fails
    """
    pvName = _fqpvname(guid)
    cmd = ["pvresize", pvName]
    rc, out, err = _lvminfo.cmd(cmd, _lvminfo._getVGDevs((vgName, )))
    if rc != 0:
        raise se.CouldNotResizePhysicalVolume(pvName, err)
    _lvminfo._invalidatepvs(pvName)
    _lvminfo._invalidatevgs(vgName)


def movePV(vgName, src_device, dst_devices):
    """
    Moves the data stored on a PV to other PVs that are part of the VG.

    Raises se.CouldNotMovePVData if pvmove fails
    """
    pvName = _fqpvname(src_device)

    # we invalidate the pv as we can't rely on the cache for checking the
    # current state
    _lvminfo._invalidatepvs(pvName)
    pv = getPV(pvName)
    if pv.pe_alloc_count == "0":
        log.info("No data to move on pv %s (vg %s), considering as successful",
                 pvName, vgName)
        return

    cmd = ["pvmove", pvName]
    if dst_devices:
        cmd.extend(_fqpvname(pdev) for pdev in dst_devices)

    log.info("Moving pv %s data (vg %s)", pvName, vgName)
    rc, out, err = _lvminfo.cmd(cmd, _lvminfo._getVGDevs((vgName, )))
    # We invalidate all the caches even on failure so we'll have up to date
    # data after moving data within the vg.
    _lvminfo._invalidatepvs(pvName)
    _lvminfo._invalidatelvs(vgName)
    _lvminfo._invalidatevgs(vgName)
    if rc != 0:
        raise se.CouldNotMovePVData(pvName, vgName, err)


def getVG(vgName):
    vg = _lvminfo.getVg(vgName)  # returns single VG namedtuple
    if not vg:
        raise se.VolumeGroupDoesNotExist(vgName)
    else:
        return vg


def getVGs(vgNames):
    return _lvminfo.getVgs(vgNames)  # returns list


def getAllVGs():
    return _lvminfo.getAllVgs()  # returns list


# TODO: lvm VG UUID should not be exposed.
# Remove this function when hsm.public_createVG is removed.
def getVGbyUUID(vgUUID):
    # cycle through all the VGs until the one with the given UUID found
    for vg in getAllVGs():
        try:
            if vg.uuid == vgUUID:
                return vg
        except AttributeError as e:
            # An unreloadable VG found but may be we are not looking for it.
            log.debug("%s", e, exc_info=True)
            continue
    # If not cry loudly
    raise se.VolumeGroupDoesNotExist("vg_uuid: %s" % vgUUID)


def getLV(vgName, lvName=None):
    lv = _lvminfo.getLv(vgName, lvName)
    # getLV() should not return None
    if not lv:
        raise se.LogicalVolumeDoesNotExistError("%s/%s" % (vgName, lvName))
    else:
        return lv


#
# Public configuration
#

def set_read_only(read_only):
    """
    Change lvm module mode to read-only or read-write.

    In read-wite mode, any lvm command may attempt to recover VG metadata if
    the metadata looks inconsistent. This may happen if the SPM is modifying
    the VG metadata at the same time. Attempting to recover the metadata may
    corrupt the metadata.

    In read-only mode, lvm commands that find inconsistant metadata will fail.
    The module retries read-only commands automaticaly to recover from such
    errors.

    If there are running lvm commands, this will wait until the commands are
    finished before changing the mode. New commands started when read-only mode
    is changed will wait until the change is complete.

    See https://bugzilla.redhat.com/1553133 for more info.
    """
    _lvminfo.set_read_only(read_only)


#
# Public Volume Group interface
#

def createVG(vgName, devices, initialTag, metadataSize, force=False):
    pvs = [_fqpvname(pdev) for pdev in normalize_args(devices)]
    _checkpvsblksize(pvs)

    _initpvs(pvs, metadataSize, force)
    # Activate the 1st PV metadata areas
    cmd = ["pvchange", "--metadataignore", "n"]
    cmd.append(pvs[0])
    rc, out, err = _lvminfo.cmd(cmd, tuple(pvs))
    if rc != 0:
        raise se.PhysDevInitializationError(pvs[0])

    options = ["--physicalextentsize", "%dm" % (sc.VG_EXTENT_SIZE // MiB)]
    if initialTag:
        options.extend(("--addtag", initialTag))
    cmd = ["vgcreate"] + options + [vgName] + pvs
    rc, out, err = _lvminfo.cmd(cmd, tuple(pvs))
    if rc == 0:
        _lvminfo._invalidatepvs(pvs)
        _lvminfo._invalidatevgs(vgName)
        log.debug("Cache after createvg %s", _lvminfo._vgs)
    else:
        raise se.VolumeGroupCreateError(vgName, pvs)


def removeVG(vgName):
    log.info("Removing VG %s", vgName)
    deactivateVG(vgName)
    # Remove VG with force option to skip confirmation VG should be removed
    # and checks that it can be removed.
    cmd = ["vgremove", "-f", vgName]
    rc, out, err = _lvminfo.cmd(cmd, _lvminfo._getVGDevs((vgName, )))
    # PVS needs to be reloaded anyhow: if vg is removed they are staled,
    # if vg remove failed, something must be wrong with devices and we want
    # cache updated as well
    _lvminfo._invalidatevgpvs(vgName)
    # If vgremove failed reintroduce the VG into the cache
    if rc != 0:
        _lvminfo._invalidatevgs(vgName)
        raise se.VolumeGroupRemoveError("VG %s remove failed." % vgName)
    else:
        # Remove the vg from the cache
        _lvminfo._removevgs(vgName)


def removeVGbyUUID(vgUUID):
    vg = getVGbyUUID(vgUUID)
    if vg:
        removeVG(vg.name)


def extendVG(vgName, devices, force):
    pvs = [_fqpvname(pdev) for pdev in normalize_args(devices)]
    _checkpvsblksize(pvs, getVGBlockSizes(vgName))
    vg = _lvminfo.getVg(vgName)

    member_pvs = set(vg.pv_name).intersection(pvs)
    if member_pvs:
        log.error("Cannot extend vg %s: pvs already belong to vg %s",
                  vg.name, member_pvs)
        raise se.VolumeGroupExtendError(vgName, pvs)

    # Format extension PVs as all the other already in the VG
    _initpvs(pvs, int(vg.vg_mda_size) // MiB, force)

    cmd = ["vgextend", vgName] + pvs
    devs = tuple(_lvminfo._getVGDevs((vgName, )) + tuple(pvs))
    rc, out, err = _lvminfo.cmd(cmd, devs)
    if rc == 0:
        _lvminfo._invalidatepvs(pvs)
        _lvminfo._invalidatevgs(vgName)
        log.debug("Cache after extending vg %s", _lvminfo._vgs)
    else:
        raise se.VolumeGroupExtendError(vgName, pvs)


def reduceVG(vgName, device):
    pvName = _fqpvname(device)
    log.info("Removing pv %s from vg %s", pvName, vgName)
    cmd = ["vgreduce", vgName, pvName]
    rc, out, err = _lvminfo.cmd(cmd, _lvminfo._getVGDevs((vgName, )))
    if rc != 0:
        raise se.VolumeGroupReduceError(vgName, pvName, err)
    _lvminfo._invalidatepvs(pvName)
    _lvminfo._invalidatevgs(vgName)


def chkVG(vgName):
    cmd = ["vgck", vgName]
    rc, out, err = _lvminfo.cmd(cmd, _lvminfo._getVGDevs((vgName, )))
    if rc != 0:
        _lvminfo._invalidatevgs(vgName)
        _lvminfo._invalidatelvs(vgName)
        raise se.StorageDomainAccessError("%s: %s" % (vgName, err))
    return True


def deactivateVG(vgName):
    cmd = ["vgchange", "--available", "n", vgName]
    rc, out, err = _lvminfo.cmd(cmd, _lvminfo._getVGDevs([vgName]))
    _lvminfo._invalidatelvs(vgName)
    if rc != 0:
        # During deactivation we ignore error here because we don't care about
        # this vg anymore.
        log.info("Error deactivating VG %s: rc=%s out=%s err=%s",
                 vgName, rc, out, err)

        # When the storage is not available, DM mappings for LVs are not
        # removed by LVM, so we have to clean it up manually. For more details
        # see https://bugzilla.redhat.com/1881468
        _removeVgMapping(vgName)


def invalidateVG(vgName, invalidateLVs=True, invalidatePVs=False):
    _lvminfo._invalidatevgs(vgName)
    if invalidateLVs:
        _lvminfo._invalidatelvs(vgName)
    if invalidatePVs:
        _lvminfo._invalidatevgpvs(vgName)


def _getpvblksize(pv):
    dev = os.path.realpath(pv)
    return multipath.getDeviceBlockSizes(dev)


def _checkpvsblksize(pvs, vgBlkSize=None):
    for pv in pvs:
        pvBlkSize = _getpvblksize(pv)
        logPvBlkSize, phyPvBlkSize = pvBlkSize

        if logPvBlkSize not in sc.SUPPORTED_BLOCKSIZE:
            raise se.DeviceBlockSizeError(pvBlkSize)

        if phyPvBlkSize < logPvBlkSize:
            raise se.DeviceBlockSizeError(pvBlkSize)

        # WARN: This is setting vgBlkSize to the first value found by
        #       _getpvblksize (if not provided by the function call).
        #       It makes sure that all the PVs have the same block size.
        if vgBlkSize is None:
            vgBlkSize = pvBlkSize

        if logPvBlkSize != vgBlkSize[0]:
            raise se.VolumeGroupBlockSizeError(vgBlkSize, pvBlkSize)


def checkVGBlockSizes(vgUUID, vgBlkSize=None):
    pvs = listPVNames(vgUUID)
    if not pvs:
        raise se.VolumeGroupDoesNotExist("vg_uuid: %s" % vgUUID)
    _checkpvsblksize(pvs, vgBlkSize)


def getVGBlockSizes(vgUUID):
    pvs = listPVNames(vgUUID)
    if not pvs:
        raise se.VolumeGroupDoesNotExist("vg_uuid: %s" % vgUUID)
    # Returning the block size of the first pv is correct since we don't allow
    # devices with different block size to be on the same VG.
    return _getpvblksize(pvs[0])

#
# Public Logical volume interface
#


def createLV(vgName, lvName, size, activate=True, contiguous=False,
             initialTags=(), device=None):
    """
    Size units: MiB.
    """
    # WARNING! From man vgs:
    # All sizes are output in these units: (h)uman-readable, (b)ytes,
    # (s)ectors, (k)ilobytes, (m)egabytes, (g)igabytes, (t)erabytes,
    # (p)etabytes, (e)xabytes.
    # Capitalise to use multiples of 1000 (S.I.) instead of 1024.

    log.info("Creating LV (vg=%s, lv=%s, size=%sm, activate=%s, "
             "contiguous=%s, initialTags=%s, device=%s)",
             vgName, lvName, size, activate, contiguous, initialTags, device)
    cont = {True: "y", False: "n"}[contiguous]
    cmd = ["lvcreate"]
    cmd.extend(LVM_NOBACKUP)
    cmd.extend(("--contiguous", cont, "--size", "%sm" % size))
    for tag in initialTags:
        cmd.extend(("--addtag", tag))
    cmd.extend(("--name", lvName, vgName))
    if device is not None:
        cmd.append(_fqpvname(device))
    rc, out, err = _lvminfo.cmd(cmd, _lvminfo._getVGDevs((vgName, )))

    if rc == 0:
        _lvminfo._invalidatevgs(vgName)
        _lvminfo._invalidatelvs(vgName, lvName)
    else:
        raise se.CannotCreateLogicalVolume(vgName, lvName, err)

    # TBD: Need to explore the option of running lvcreate w/o devmapper
    # so that if activation is not needed it would be skipped in the
    # first place
    if activate:
        lv_path = lvPath(vgName, lvName)
        st = os.stat(lv_path)
        uName = pwd.getpwuid(st.st_uid).pw_name
        gName = grp.getgrgid(st.st_gid).gr_name
        if ":".join((uName, gName)) != USER_GROUP:
            cmd = [constants.EXT_CHOWN, USER_GROUP, lv_path]
            if misc.execCmd(cmd, sudo=True)[0] != 0:
                log.warning("Could not change ownership of one or more "
                            "volumes in vg (%s) - %s", vgName, lvName)
    else:
        _setLVAvailability(vgName, lvName, "n")


def removeLVs(vgName, lvNames):
    assert isinstance(lvNames, (list, tuple))
    log.info("Removing LVs (vg=%s, lvs=%s)", vgName, lvNames)
    # Assert that the LVs are inactive before remove.
    for lvName in lvNames:
        if _isLVActive(vgName, lvName):
            # Fix me
            # Should not remove active LVs
            # raise se.CannotRemoveLogicalVolume(vgName, lvName)
            log.warning("Removing active volume %s/%s" % (vgName, lvName))

    # LV exists or not in cache, attempting to remove it.
    # Removing Stales also. Active Stales should raise.
    # Destroy LV
    # Fix me:removes active LVs too. "-f" should be removed.
    cmd = ["lvremove", "-f"]
    cmd.extend(LVM_NOBACKUP)
    for lvName in lvNames:
        cmd.append("%s/%s" % (vgName, lvName))
    rc, out, err = _lvminfo.cmd(cmd, _lvminfo._getVGDevs((vgName, )))
    if rc == 0:
        # Remove the LV from the cache
        _lvminfo._removelvs(vgName, lvNames)
        # If lvremove succeeded it affected VG as well
        _lvminfo._invalidatevgs(vgName)
    else:
        # Otherwise LV info needs to be refreshed
        _lvminfo._invalidatelvs(vgName, lvNames)
        raise se.CannotRemoveLogicalVolume(vgName, str(lvNames), err)


def extendLV(vgName, lvName, size_mb):
    # Since this runs only on the SPM, assume that cached vg and lv metadata
    # are correct.
    vg = getVG(vgName)
    lv = getLV(vgName, lvName)
    extent_size = int(vg.extent_size)

    # Convert sizes to extents to match lvm behavior.
    lv_extents = int(lv.size) // extent_size
    requested_extents = utils.round(size_mb * MiB, extent_size) // extent_size

    # Check if lv is large enough before trying to extend it to avoid warnings,
    # filter invalidation and pointless retries if the lv is already large
    # enough.
    if lv_extents >= requested_extents:
        log.debug("LV %s/%s already extended (extents=%d, requested=%d)",
                  vgName, lvName, lv_extents, requested_extents)
        return

    log.info("Extending LV %s/%s to %s megabytes", vgName, lvName, size_mb)
    cmd = ("lvextend",) + LVM_NOBACKUP
    cmd += ("--size", "%sm" % (size_mb,), "%s/%s" % (vgName, lvName))
    rc, out, err = _lvminfo.cmd(cmd, _lvminfo._getVGDevs((vgName,)))

    # Invalidate vg and lv to ensure cached metadata is correct if we need to
    # access it when handling errors.
    _lvminfo._invalidatevgs(vgName)
    _lvminfo._invalidatelvs(vgName, lvName)

    if rc != 0:
        # Reload lv to get updated size.
        lv = getLV(vgName, lvName)
        lv_extents = int(lv.size) // extent_size

        if lv_extents >= requested_extents:
            log.debug("LV %s/%s already extended (extents=%d, requested=%d)",
                      vgName, lvName, lv_extents, requested_extents)
            return

        # Reload vg to get updated free extents.
        vg = getVG(vgName)
        needed_extents = requested_extents - lv_extents
        free_extents = int(vg.free_count)
        if free_extents < needed_extents:
            raise se.VolumeGroupSizeError(
                "Not enough free extents for extending LV %s/%s (free=%d, "
                "needed=%d)"
                % (vgName, lvName, free_extents, needed_extents))

        raise se.LogicalVolumeExtendError(vgName, lvName, "%sm" % (size_mb,))


def reduceLV(vgName, lvName, size_mb, force=False):
    log.info("Reducing LV %s/%s to %s megabytes (force=%s)",
             vgName, lvName, size_mb, force)
    cmd = ("lvreduce",) + LVM_NOBACKUP
    if force:
        cmd += ("--force",)
    cmd += ("--size", "%sm" % (size_mb,), "%s/%s" % (vgName, lvName))
    rc, out, err = _lvminfo.cmd(cmd, _lvminfo._getVGDevs((vgName,)))
    if rc != 0:
        # Since this runs only on the SPM, assume that cached vg and lv
        # metadata is correct.
        vg = getVG(vgName)
        lv = getLV(vgName, lvName)

        # Convert sizes to extents
        extent_size = int(vg.extent_size)
        lv_extents = int(lv.size) // extent_size
        requested_extents = utils.round(
            size_mb * MiB, extent_size) // extent_size

        if lv_extents <= requested_extents:
            log.debug("LV %s/%s already reduced (extents=%d, requested=%d)",
                      vgName, lvName, lv_extents, requested_extents)
            return

        # TODO: add and raise LogicalVolumeReduceError
        raise se.LogicalVolumeExtendError(vgName, lvName, "%sm" % (size_mb,))

    _lvminfo._invalidatevgs(vgName)
    _lvminfo._invalidatelvs(vgName, lvName)


def activateLVs(vgName, lvNames, refresh=True):
    """
    Ensure that all lvNames are active and reflect the current mapping on
    storage.

    Active lvs may not reflect the current mapping on storage if the lv was
    extended or removed on another host. By default, active lvs are refreshed.
    To skip refresh, call with refresh=False.
    """
    active = []
    inactive = []
    for lvName in lvNames:
        if _isLVActive(vgName, lvName):
            active.append(lvName)
        else:
            inactive.append(lvName)

    if refresh and active:
        log.info("Refreshing active lvs: vg=%s lvs=%s", vgName, active)
        _refreshLVs(vgName, active)

    if inactive:
        log.info("Activating lvs: vg=%s lvs=%s", vgName, inactive)
        _setLVAvailability(vgName, inactive, "y")


def deactivateLVs(vgName, lvNames):
    toDeactivate = [lvName for lvName in lvNames
                    if _isLVActive(vgName, lvName)]
    if toDeactivate:
        log.info("Deactivating lvs: vg=%s lvs=%s", vgName, toDeactivate)
        _setLVAvailability(vgName, toDeactivate, "n")


def renameLV(vg, oldlv, newlv):
    log.info("Renaming LV (vg=%s, oldlv=%s, newlv=%s)", vg, oldlv, newlv)
    cmd = ("lvrename",) + LVM_NOBACKUP + (vg, oldlv, newlv)
    rc, out, err = _lvminfo.cmd(cmd, _lvminfo._getVGDevs((vg, )))
    if rc != 0:
        raise se.LogicalVolumeRenameError("%s %s %s" % (vg, oldlv, newlv))

    _lvminfo._removelvs(vg, oldlv)
    _lvminfo._reloadlvs(vg, newlv)


def refreshLVs(vgName, lvNames):
    log.info("Refreshing LVs (vg=%s, lvs=%s)", vgName, lvNames)
    _refreshLVs(vgName, lvNames)


def _refreshLVs(vgName, lvNames):
    # If  the  logical  volumes  are active, reload their metadata.
    cmd = ['lvchange', '--refresh']
    cmd.extend("%s/%s" % (vgName, lv) for lv in lvNames)
    rc, out, err = _lvminfo.cmd(cmd, _lvminfo._getVGDevs((vgName, )))
    _lvminfo._invalidatelvs(vgName, lvNames)
    if rc != 0:
        raise se.LogicalVolumeRefreshError("%s failed" % list2cmdline(cmd))


def changeLVsTags(vg, lvs, delTags=(), addTags=()):
    log.info("Change LVs tags (vg=%s, lvs=%s, delTags=%s, addTags=%s)",
             vg, lvs, delTags, addTags)

    delTags = set(delTags)
    addTags = set(addTags)
    if delTags.intersection(addTags):
        raise se.LogicalVolumeReplaceTagError(
            "Cannot add and delete the same tag lvs: `%s` tags: `%s`" %
            (lvs, ", ".join(delTags.intersection(addTags))))

    attrs = []
    for tag in delTags:
        attrs.extend(("--deltag", tag))
    for tag in addTags:
        attrs.extend(('--addtag', tag))

    try:
        changelv(vg, lvs, attrs)
    except se.StorageException as e:
        raise se.LogicalVolumeReplaceTagError(
            'lvs: `%s` add: `%s` del: `%s` (%s)' %
            (lvs, ", ".join(addTags), ", ".join(delTags), e))


#
# Helper functions
#
def lvPath(vgName, lvName):
    return os.path.join("/dev", vgName, lvName)


def lvDmDev(vgName, lvName):
    """Return the LV dm device.

    returns: dm-X
    If the LV is inactive there is no dm device
    and OSError will be raised.
    """
    lvp = lvPath(vgName, lvName)
    return os.path.basename(os.readlink(lvp))


def _isLVActive(vgName, lvName):
    """Active volumes have a mp link.

    This function should not be used out of this module.
    """
    return os.path.exists(lvPath(vgName, lvName))


def changeVGTags(vgName, delTags=(), addTags=()):
    log.info("Changing VG tags (vg=%s, delTags=%s, addTags=%s)",
             vgName, delTags, addTags)
    delTags = set(delTags)
    addTags = set(addTags)
    if delTags.intersection(addTags):
        raise se.VolumeGroupReplaceTagError(
            "Cannot add and delete the same tag vg: `%s` tags: `%s`" %
            (vgName, ", ".join(delTags.intersection(addTags))))

    cmd = ["vgchange"]

    for tag in delTags:
        cmd.extend(("--deltag", tag))
    for tag in addTags:
        cmd.extend(("--addtag", tag))

    cmd.append(vgName)
    rc, out, err = _lvminfo.cmd(cmd, _lvminfo._getVGDevs((vgName, )))
    _lvminfo._invalidatevgs(vgName)
    if rc != 0:
        raise se.VolumeGroupReplaceTagError(
            "vg:%s del:%s add:%s (%s)" %
            (vgName, ", ".join(delTags), ", ".join(addTags), err[-1]))


def replaceVGTag(vg, oldTag, newTag):
    changeVGTags(vg, [oldTag], [newTag])


def getFirstExt(vg, lv):
    return getLV(vg, lv).devices.strip(" )").split("(")


def getVgMetadataPv(vgName):
    pvs = _lvminfo.getPvs(vgName)
    mdpvs = [pv for pv in pvs
             if not pv.is_stale() and pv.is_metadata_pv()]
    if len(mdpvs) != 1:
        raise se.UnexpectedVolumeGroupMetadata("Expected one metadata pv in "
                                               "vg: %s, vg pvs: %s" %
                                               (vgName, pvs))
    return mdpvs[0].name


def listPVNames(vgName):
    try:
        pvNames = _lvminfo._vgs[vgName].pv_name
    except (KeyError, AttributeError):
        pvNames = getVG(vgName).pv_name
    return pvNames


def setrwLV(vg_name, lv_name, rw=True):
    log.info(
        "Changing LV permissions (vg=%s, lv=%s, rw=%s)", vg_name, lv_name, rw)
    permission = {False: 'r', True: 'rw'}[rw]
    try:
        changelv(vg_name, lv_name, ("--permission", permission))
    except se.StorageException:
        lv = getLV(vg_name, lv_name)
        if lv.writeable == rw:
            # Ignore the error since lv is now rw, hoping that the error was
            # because lv was already rw, see BZ#654691. We may hide here
            # another lvchange error.
            return

        raise se.CannotSetRWLogicalVolume(vg_name, lv_name, permission)


def lvsByTag(vgName, tag):
    return [lv for lv in getLV(vgName) if tag in lv.tags]


def invalidateFilter():
    _lvminfo.invalidateFilter()


def cache_stats():
    return _lvminfo.stats.info()


def clear_stats():
    _lvminfo.stats.clear()
