import json
import os
import re
import time
import logging
from contextlib import contextmanager

from .state import STATE, Disk
from .common import error


class OutputParser(object):
    COPY_DISK_RE = re.compile(br'.*Copying disk (\d+)/(\d+) to.*')
    DISK_PROGRESS_RE = re.compile(br'\s+\((\d+\.\d+)/100%\)')
    NBDKIT_DISK_PATH_RE = re.compile(
        br'nbdkit:.* debug: Opening file (.*) \(.*\)')
    OVERLAY_SOURCE_RE = re.compile(
        br' *overlay source qemu URI: json:.*"file\.path": ?"([^"]+)"')
    OVERLAY_SOURCE2_RE = re.compile(
        br'libguestfs: parse_json: qemu-img info JSON output:.*'
        br'"backing-filename".*\\"file\.path\\": ?\\"([^"]+)\\"')
    VMDK_PATH_RE = re.compile(
        br'/vmfs/volumes/(?P<store>[^/]*)/(?P<vm>[^/]*)/'
        br'(?P<disk>.*?)(-flat)?\.vmdk$')
    OVIRT_DISK_UUID = re.compile(br'disk\.id = \'(?P<uuid>[a-fA-F0-9-]*)\'')
    OVIRT_VM_ID = re.compile(
        br'<VirtualSystem ovf:id=\'(?P<uuid>[a-fA-F0-9-]*)\'>')
    OPENSTACK_VOLUME_ID = re.compile(
            br'openstack .*\'?volume\'? \'?show\'?.* '
            br'\'?(?P<uuid>[a-fA-F0-9-]*)\'?$')
    OPENSTACK_VOLUME_PROPS = re.compile(
        br'openstack .*\'?volume\'? \'?set.*'
        br'\'?--property\'?'
        br' \'?virt_v2v_disk_index=(?P<volume>[0-9]+)/[0-9]+.*'
        br' \'?(?P<uuid>[a-fA-F0-9-]*)\'?$')
    SSH_VMX_GUEST_NAME = re.compile(br'^displayName = "(.*)"$')

    def __init__(self, duplicate=False):
        # Wait for the log files to appear
        for i in range(10):
            if os.path.exists(STATE.v2v_log) \
                    and os.path.exists(STATE.machine_readable_log):
                continue
            time.sleep(1)
        self._log = open(STATE.v2v_log, 'rb')
        self._machine_log = open(STATE.machine_readable_log, 'rb')
        self._current_disk = None
        self._current_path = None
        self._duplicate = duplicate

    def __del__(self):
        self._log.close()
        self._machine_log.close()

    def parse(self):
        line = self._machine_log.readline()
        while line != b'':
            try:
                message = json.loads(line)
                if message.get('type') == 'error':
                    message = message.get('message')
                    error('virt-v2v error: {}'.format(message))
            except json.decoder.JSONDecodeError:
                logging.exception(
                    'Failed to parse line from'
                    ' virt-v2v machine readable output')
                logging.error('Offending line: %r', line)
            line = self._machine_log.readline()
        line = self._log.readline()
        while line != b'':
            if self._duplicate:
                logging.debug('%r', line)
            self.parse_line(line)
            line = self._log.readline()

    def parse_line(self, line):
        # Ovirt VM UUID
        m = self.OVIRT_VM_ID.search(line)
        if m is not None:
            vm_id = m.group('uuid').decode('utf-8')
            STATE.vm_id = vm_id
            logging.info('Created VM with id=%s', vm_id)

        if STATE.pre_copy:
            # There is nothing else to parse for two-phase conversion
            return

        m = self.COPY_DISK_RE.match(line)
        if m is not None:
            try:
                self._current_disk = int(m.group(1))-1
                self._current_path = None
                STATE.disk_count = int(m.group(2))
                logging.info('Copying disk %d/%d',
                             self._current_disk+1, STATE.disk_count)
                if STATE.disk_count != len(STATE.disks):
                    logging.warning(
                        'Number of supplied disk paths (%d) does not match'
                        ' number of disks in VM (%s)',
                        len(STATE.disks),
                        STATE.disk_count)
            except ValueError:
                error(
                    'Failed to decode disk number',
                    'Failed to decode disk number -- conversion error',
                    exception=True)

        # VDDK
        m = self.NBDKIT_DISK_PATH_RE.match(line)
        if m is not None:
            self._current_path = m.group(1).decode('utf-8')
            if self._current_disk is not None:
                logging.info('Copying path: %s', self._current_path)
                self._locate_disk()

        # SSH (all outputs)
        m = self.SSH_VMX_GUEST_NAME.match(line)
        if m is not None:
            STATE.internal['display_name'] = m.group(1)
            logging.info('Set VM display name to: %s',
                         STATE.internal['display_name'])

        # SSH + Ovirt
        m = self.OVERLAY_SOURCE_RE.match(line)
        if m is not None:
            path = m.group(1)
            # Transform path to be raltive to storage
            self._current_path = self.VMDK_PATH_RE.sub(
                br'[\g<store>] \g<vm>/\g<disk>.vmdk', path).decode('utf-8')
            if self._current_disk is not None:
                logging.info('Copying path: %s', self._current_path)
                self._locate_disk()

        # SSH + OpenStack
        m = self.OVERLAY_SOURCE2_RE.match(line)
        if m is not None:
            path = m.group(1)
            # Transform path to be raltive to storage
            self._current_path = self.VMDK_PATH_RE.sub(
                br'[\g<store>] \g<vm>/\g<disk>.vmdk', path).decode('utf-8')
            if self._current_disk is not None:
                logging.info('Copying path: %s', self._current_path)
                self._locate_disk()

        m = self.DISK_PROGRESS_RE.match(line)
        if m is not None:
            if self._current_path is not None and \
                    self._current_disk is not None:
                try:
                    STATE.disks[self._current_disk].progress = \
                        float(m.group(1))
                    logging.debug('Updated progress: %s', m.group(1))
                except ValueError:
                    error(
                        'Failed to decode progress'
                        'Failed to decode progress -- conversion error',
                        exception=True)
            else:
                logging.debug('Skipping progress update for unknown disk')

        m = self.OVIRT_DISK_UUID.match(line)
        if m is not None:
            path = STATE.disks[self._current_disk].path
            disk_id = m.group('uuid')
            STATE.internal['disk_ids'][path] = disk_id
            logging.debug('Path \'%s\' has disk id=\'%s\'', path, disk_id)

        # OpenStack volume UUID
        m = self.OPENSTACK_VOLUME_ID.match(line)
        if m is not None:
            volume_id = m.group('uuid').decode('utf-8')
            ids = STATE.internal['disk_ids']
            ids[len(ids)+1] = volume_id
            logging.debug('Adding Openstack volume %s', volume_id)

        # OpenStack volume index
        m = self.OPENSTACK_VOLUME_PROPS.match(line)
        if m is not None:
            volume_id = m.group('uuid').decode('utf-8')
            index = int(m.group('volume'))
            # Just check
            if STATE.internal['disk_ids'].get(index) != volume_id:
                logging.debug(
                    'Volume \'%s\' is NOT at index %d', volume_id, index)

    def close(self):
        self._log.close()

    def _locate_disk(self):
        if self._current_disk is None:
            # False alarm, not copying yet
            return

        # NOTE: We assume that _current_disk is monotonic
        for i in range(self._current_disk, len(STATE.disks)):
            if STATE.disks[i].path == self._current_path:
                if i == self._current_disk:
                    # We have correct index
                    logging.debug('Found path at correct index')
                else:
                    # Move item to current index
                    logging.debug('Moving path from index %d to %d', i,
                                  self._current_disk)
                    d = STATE.disks.pop(i)
                    STATE.disks.insert(self._current_disk, d)
                return

        # Path not found
        logging.debug('Path \'%s\' not found in %r', self._current_path,
                      STATE.disks)
        STATE.disks.insert(self._current_disk, Disk(self._current_path, 0))


@contextmanager
def log_parser(duplicate=False):
    parser = None
    try:
        parser = OutputParser(duplicate)
        yield parser
    finally:
        if parser is not None:
            parser.close()
