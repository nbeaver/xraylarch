#!/usr/bin/env python
MODDOC = """
=== Epics Scanning Functions for Larch ===


This does not used the Epics SScan Record, and the scan is intended to run
as a python application, but many concepts from the Epics SScan Record are
borrowed.  Where appropriate, the difference will be noted here.

A Step Scan consists of the following objects:
   a list of Positioners
   a list of Triggers
   a list of Counters

Each Positioner will have a list (or numpy array) of position values
corresponding to the steps in the scan.  As there is a fixed number of
steps in the scan, the position list for each positioners must have the
same length -- the number of points in the scan.  Note that, unlike the
SScan Record, the list of points (not start, stop, step, npts) must be
given.  Also note that the number of positioners or number of points is not
limited.

A Trigger is simply an Epics PV that will start a particular detector,
usually by having 1 written to its field.  It is assumed that when the
Epics ca.put() to the trigger completes, the Counters associated with the
triggered detector will be ready to read.

A Counter is simple a PV whose value should be recorded at every step in
the scan.  Any PV can be a Counter, including waveform records.  For many
detector types, it is possible to build a specialized class that creates
many counters.

Because Triggers and Counters are closely associated with detectors, a
Detector is also defined, which simply contains a single Trigger and a list
of Counters, and will cover most real use cases.

In addition to the core components (Positioners, Triggers, Counters, Detectors),
a Step Scan contains the following objects:

   breakpoints   a list of scan indices at which to pause and write data
                 collected so far to disk.
   extra_pvs     a list of (description, PV) tuples that are recorded at
                 the beginning of scan, and at each breakpoint, to be
                 recorded to disk file as metadata.
   pre_scan()    method to run prior to scan.
   post_scan()   method to run after scan.
   at_break()    method to run at each breakpoint.

Note that Postioners and Detectors may add their own pieces into extra_pvs,
pre_scan(), post_scan(), and at_break().

With these concepts, a Step Scan ends up being a fairly simple loop, going
roughly (that is, skipping error checking) as:

   pos = <DEFINE POSITIONER LIST>
   det = <DEFINE DETECTOR LIST>
   run_pre_scan(pos, det)
   [p.move_to_start() for p in pos]
   record_extra_pvs(pos, det)
   for i in range(len(pos[0].array)):
       [p.move_to_pos(i) for p in pos]
       while not all([p.done for p in pos]):
           time.sleep(0.001)
       [trig.start() for trig in det.triggers]
       while not all([trig.done for trig in det.triggers]):
           time.sleep(0.001)
       [det.read() for det in det.counters]

       if i in breakpoints:
           write_data(pos, det)
           record_exrta_pvs(pos, det)
           run_at_break(pos, det)
   write_data(pos, det)
   run_post_scan(pos, det)

Note that multi-dimensional mesh scans over a rectangular grid is not
explicitly supported, but these can be easily emulated with the more
flexible mechanism of unlimited list of positions and breakpoints.
Non-mesh scans are also possible.

A step scan can have an Epics SScan Record or StepScan database associated
with it.  It will use these for PVs to post data at each point of the scan.
"""

import time
import threading
import json
import numpy as np
from epics import PV, poll

from larch import use_plugin_path, Group, ValidateLarchPlugin
use_plugin_path('epics')

from detectors import Counter, DeviceCounter, Trigger, get_detector
from datafile import ASCIIScanFile
from positioner import Positioner
# from xafsscan import XAFS_Scan

from epics_plugin import pv_units

from scandb import ScanDB

use_plugin_path('io')
from fileutils import fix_varname

MODNAME = '_scan'
SCANDB_NAME = '%s._scandb' % MODNAME

MIN_POLL_TIME = 1.e-3

class ScanMessenger(threading.Thread):
    """ Provides a way to run user-supplied functions per scan point,
    in a separate thread, so as to not delay scan operation.

    Initialize a ScanMessenger with a function to call per point, and the
    StepScan instance.  On .start(), a separate thread will createrd and
    the .run() method run.  Here, this runs a loop, looking at the .cpt
    attribute.  When this .cpt changes, the executing will run the user
    supplied code with arguments of 'scan=scan instance', and 'cpt=cpt'

    Thus, at each point in the scan the scanning process should set .cpt,
    and the user-supplied func will execute.

    To stop the thread, set .cpt to None.  The thread will also automatically
    stop if .cpt has not changed in more than 1 hour
    """
    # number of seconds to wait for .cpt to change before exiting thread
    timeout = 3600.
    def __init__(self, func=None, scan=None,
                 cpt=-1, npts=None, func_kws=None):
        threading.Thread.__init__(self)
        self.func = func
        self.scan = scan
        self.cpt = cpt
        self.npts = npts
        if func_kws is None:
            func_kws = {}
        self.func_kws = func_kws
        self.func_kws['npts'] = npts

    def run(self):
        """execute thread, watching the .cpt attribute. Any chnage will
        cause self.func(cpt=self.cpt, scan=self.scan) to be run.
        The thread will stop when .pt == None or has not changed in
        a time  > .timeout
        """
        last_point = self.cpt
        t0 = time.time()
        while True:
            poll(MIN_POLL_TIME, 0.25)
            if self.cpt != last_point:
                last_point =  self.cpt
                t0 = time.time()
                if self.cpt is not None and hasattr(self.func, '__call__'):
                    self.func(cpt=self.cpt, scan=self.scan,
                              **self.func_kws)
            if self.cpt is None or time.time()-t0 > self.timeout:
                return

class LarchStepScan(object):
    """
    Epics Step Scanning for Larch
    """
    def __init__(self, filename=None, auto_increment=True, _larch=None):
        self.pos_settle_time = MIN_POLL_TIME
        self.det_settle_time = MIN_POLL_TIME
        self.pos_maxmove_time = 3600.0
        self.det_maxcount_time = 86400.0
        self._larch = _larch
        self._scangroup =  _larch.symtable._scan
        self.scandb = None
        if self._scangroup._scandb is not None:
            self.scandb = self._scangroup._scandb

        self.dwelltime = None
        self.comments = None

        self.filename = filename
        self.auto_increment = auto_increment
        self.filetype = 'ASCII'

        self.verified = False
        self.abort = False
        self.pause = False
        self.inittime = 0 # time to initialize scan (pre_scan, move to start, begin i/o)
        self.looptime = 0 # time to run scan loop (even if aborted)
        self.exittime = 0 # time to complete scan (post_scan, return positioners, complete i/o)
        self.runtime  = 0 # inittime + looptime + exittime

        self.cpt = 0
        self.npts = 0
        self.complete = False
        self.debug = False
        self.message_points = 2
        self.extra_pvs = []
        self.positioners = []
        self.triggers = []
        self.counters = []
        self.detectors = []

        self.breakpoints = []
        self.at_break_methods = []
        self.pre_scan_methods = []
        self.post_scan_methods = []
        self.pos_actual  = []

    def open_output_file(self, filename=None, comments=None):
        """opens the output file"""
        creator = ASCIIScanFile
        # if self.filetype == 'ASCII':
        #     creator = ASCIIScanFile
        if filename is not None:
            self.filename = filename
        if comments is not None:
            self.comments = comments

        return creator(name=self.filename,
                       auto_increment=self.auto_increment,
                       comments=self.comments, scan=self)

    def add_counter(self, counter, label=None):
        "add simple counter"
        if isinstance(counter, (str, unicode)):
            counter = Counter(counter, label)
        if (isinstance(counter, (Counter, DeviceCounter)) and
            counter not in self.counters):
            self.counters.append(counter)
        self.verified = False

    def add_trigger(self, trigger, label=None, value=1):
        "add simple detector trigger"
        if isinstance(trigger, (str, unicode)):
            trigger = Trigger(trigger, label=label, value=value)
        if (isinstance(trigger, Trigger) and
            trigger not in self.triggers):
            self.triggers.append(trigger)
        self.verified = False

    def add_extra_pvs(self, extra_pvs):
        """add extra pvs (tuple of (desc, pvname))"""
        if extra_pvs is None or len(extra_pvs) == 0:
            return
        for desc, pvname in extra_pvs:
            if isinstance(pvname, PV):
                pv = pvname
            else:
                pv = PV(pvname)

            if (desc, pv) not in self.extra_pvs:
                self.extra_pvs.append((desc, pv))

    def add_positioner(self, pos):
        """ add a Positioner """
        self.add_extra_pvs(pos.extra_pvs)
        self.at_break_methods.append(pos.at_break)
        self.post_scan_methods.append(pos.post_scan)
        self.pre_scan_methods.append(pos.pre_scan)

        if pos not in self.positioners:
            self.positioners.append(pos)
        self.verified = False

    def add_detector(self, det):
        """ add a Detector -- needs to be derived from Detector_Mixin"""
        if det.extra_pvs is None: # not fully connected!
            det.connect_counters()

        self.add_extra_pvs(det.extra_pvs)
        self.at_break_methods.append(det.at_break)
        self.post_scan_methods.append(det.post_scan)
        self.pre_scan_methods.append(det.pre_scan)
        self.add_trigger(det.trigger)
        for counter in det.counters:
            self.add_counter(counter)
        if det not in self.detectors:
            self.detectors.append(det)
        self.verified = False

    def set_dwelltime(self, dtime=None):
        """set scan dwelltime per point to constant value"""
        if dtime is not None:
            self.dwelltime = dtime
	for d in self.detectors:
            d.set_dwelltime(self.dwelltime)

    def at_break(self, breakpoint=0, clear=False):
        out = [m(breakpoint=breakpoint) for m in self.at_break_methods]
        if self.datafile is not None:
            self.datafile.write_data(breakpoint=breakpoint)
        return out

    def pre_scan(self, **kws):
        if self.debug: print 'PRE SCAN '
        for (desc, pv) in self.extra_pvs:
            pv.connect()
        return [m(scan=self) for m in self.pre_scan_methods]

    def post_scan(self):
        if self.debug: print 'POST SCAN '
        return [m() for m in self.post_scan_methods]

    def verify_scan(self):
        """ this does some simple checks of Scans, checking that
        the length of the positions array matches the length of the
        positioners array.

        For each Positioner, the max and min position is checked against
        the HLM and LLM field (if available)
        """
        npts = None
        for pos in self.positioners:
            if not pos.verify_array():
                self.set_error('Positioner {0} array out of bounds'.format(
                    pos.pv.pvname))
                return False
            if npts is None:
                npts = len(pos.array)
            if len(pos.array) != npts:
                self.set_error('Inconsistent positioner array length')
                return False
        return True


    def check_outputs(self, out, msg='unknown'):
        """ check outputs of a previous command
            Any True value indicates an error
        That is, return values must be None or evaluate to False
        to indicate success.
        """
        if any(out):
            raise Warning('error on output: %s' % msg)

    def read_extra_pvs(self):
        "read values for extra PVs"
        out = []
        for desc, pv in self.extra_pvs:
            out.append((desc, pv.pvname, pv.get(as_string=True)))
        return out

    def clear_data(self):
        """clear scan data"""
        for c in self.counters:
            c.clear()
        self.pos_actual = []


    def _messenger(self, cpt, npts=0, **kws):
        time_left = (npts-cpt)* (self.pos_settle_time + self.det_settle_time)
        if self.dwelltime_varys:
            time_left += self.dwelltime[cpt:].sum()
        else:
            time_left += (npts-cpt)*self.dwelltime
        self.set_info('scan_time_estimate', time_left)
        if cpt < 4:
            self.set_info('filename', self.filename)
        msg = 'Point %i/%i' % (cpt, npts)
        if cpt % self.message_points == 0:
            print msg
        self.set_info('scan_message', msg)
        for c in self.counters:
            self.set_scandata(fix_varname(c.label), c.buff)

    def set_error(self, msg):
        """set scan error message"""
        self._scangroup.error_message = msg
        if self.scandb is not None:
            self.scandb.set_info('last_error', msg)

    def set_info(self, attr, value):
        """set scan info to _scan variable"""
        setattr(self._scangroup, attr, value)
        if self.scandb is not None:
            self.scandb.set_info(attr, value)

    def set_scandata(self, attr, value):
        setattr(self._scangroup, attr, value)
        if self.scandb is not None:
            self.scandb.set_scandata(fix_varname(attr), value)

    def init_scandata(self):
        if self.scandb is None:
            return
        self.scandb.clear_scandata()
        names = []
        npts = len(self.positioners[0].array)
        for p in self.positioners:
            units = pv_units(p.pv, 'unknown')
            name = fix_varname(p.label)
            if name in names:
                name += '_2'
            if name not in names:
                self.scandb.add_scandata(name, p.array.tolist(),
                                         pvname=p.pv.pvname,
                                         units=units, notes='positioner')
                names.append(name)
        for c in self.counters:
            units = pv_units(c.pv, 'counts')
            name = fix_varname(c.label)
            if name in names:
                name += '_2'
            if name not in names:
                self.scandb.add_scandata(name, [],
                                         pvname=c.pv.pvname,
                                         units=units, notes='counter')
                names.append(name)


    def look_for_interrupts(self):
        """set interrupt requests:

        abort / pause / resume

        if scandb is being used, these are looked up from database.
        otherwise local larch variables are used.
        """
        if self.scandb is None:
            def isset(key):
                getattr(self._scan, key)
        else:
            def isset(key):
                return self.scandb.get_info(key, as_bool=True)
        self.abort  = isset('request_abort')
        self.pause  = isset('request_pause')
        self.resume = isset('request_resume')
        return self.abort

    def clear_interrupts(self):
        """re-set interrupt requests:

        abort / pause / resume

        if scandb is being used, these are looked up from database.
        otherwise local larch variables are used.
        """
        if self.scandb is None:
            def doset(key, val):
                setattr(self._scan, key, val)
        else:
            def doset(key, val):
                return self.scandb.set_info(key, val)
        self.abort = self.pause = self.resume = False
        doset('request_abort', 0)
        doset('request_pause', 0)
        doset('request_resume', 0)

    def run(self, filename=None, comments=None):
        """ run the actual scan:
           Verify, Save original positions,
           Setup output files and messenger thread,
           run pre_scan methods
           Loop over points
           run post_scan methods
        """

        self.complete = False
        if filename is not None:
            self.filename  = filename
        if comments is not None:
            self.comments = comments

        self.pos_settle_time = max(MIN_POLL_TIME, self.pos_settle_time)
        self.det_settle_time = max(MIN_POLL_TIME, self.det_settle_time)

        ts_start = time.time()
        if not self.verify_scan():
            print 'Cannot execute scan ',  self._scangroup.error_message
            self.set_info('scan_message', 'cannot execute scan')
            return
        self.clear_interrupts()

        orig_positions = [p.current() for p in self.positioners]

        # print 'StepScan Run 2 (move to start)'
        out = [p.move_to_start(wait=False) for p in self.positioners]
        self.check_outputs(out, msg='move to start')

        self.datafile = self.open_output_file(filename=self.filename,
                                              comments=self.comments)


        self.datafile.write_data(breakpoint=0)
        self.filename =  self.datafile.filename
        if self.debug: print 'StepScan Run (data file opened)'
        self.set_info('scan_message', 'pre-scan')
        out = self.pre_scan()
        self.check_outputs(out, msg='pre scan')
        if self.debug:  print 'StepScan Run (prescan done)'
        self.set_info('scan_message', 'starting scan')
        npts = len(self.positioners[0].array)
        self.dwelltime_varys = False
        if self.dwelltime is not None:
            self.min_dwelltime = self.dwelltime
            self.max_dwelltime = self.dwelltime
            if isinstance(self.dwelltime, (list, tuple)):
                self.dwelltime = np.array(self.dwelltime)
            if isinstance(self.dwelltime, np.ndarray):
                self.min_dwelltime = min(self.dwelltime)
                self.max_dwelltime = max(self.dwelltime)
                self.dwelltime_varys = True
            else:
                for d in self.detectors:
                    d.set_dwelltime(self.dwelltime)

        time_est = npts*(self.pos_settle_time + self.det_settle_time)
        if self.dwelltime_varys:
            time_est += self.dwelltime.sum()
        else:
            time_est += npts*self.dwelltime

        self.clear_data()

        if self.scandb is not None:
            self.init_scandata()
            self.scandb.set_info('scan_time_estimate', time_est)
            self.scandb.set_info('scan_total_points', npts)
            self.scandb.set_info('request_abort', 0)
            self.scandb.set_info('scan_message', 'Preparing Scan (watcher)')

        if self.debug: print 'StepScan Run (dwelltimes set)'
        self.msg_thread = ScanMessenger(func=self._messenger, npts=npts, cpt=0)
        self.msg_thread.start()
        self.cpt = 0
        self.npts = npts
        t0 = time.time()
        out = [p.move_to_start(wait=True) for p in self.positioners]
        self.check_outputs(out, msg='move to start, wait=True')
        [p.current() for p in self.positioners]
        [d.pv.get() for d in self.counters]
        i = -1
        ts_init = time.time()
        self.inittime = ts_init - ts_start

        while not self.abort:
            i += 1
            if i >= npts:
                break
            try:
                point_ok = True
                self.cpt = i+1
                self.look_for_interrupts()
                while self.pause:
                    time.sleep(0.25)
                    if self.look_for_interrupts():
                        break
                # move to next position, wait for moves to finish
                [p.move_to_pos(i) for p in self.positioners]
                if self.dwelltime_varys:
                    for d in self.detectors:
                        d.set_dwelltime(self.dwelltime[i])
                t0 = time.time()
                mcount = 0
                while (not all([p.done for p in self.positioners]) and
                       time.time() - t0 < self.pos_maxmove_time):
                    if self.look_for_interrupts():
                        break
                    poll(5*MIN_POLL_TIME, 0.25)
                    mcount += 1
                if self.look_for_interrupts():
                    break
                # wait for positioners to settle
                # print 'Move completed in %.5f s, %i' % (time.time()-t0, mcount)
                poll(self.pos_settle_time, 0.25)
                if self.look_for_interrupts():
                    break
                # start triggers, wait for them to finish
                # print 'Trigger...'
                [trig.start() for trig in self.triggers]
                t0 = time.time()
                time.sleep(max(0.01, self.min_dwelltime/4.0))
                while not (all([trig.done for trig in self.triggers]) and
                           (time.time() - t0 < self.det_maxcount_time) and
                           (time.time() - t0 > self.min_dwelltime/2.0)):
                    if self.look_for_interrupts():
                        break
                    poll(MIN_POLL_TIME, 0.25)
                if self.look_for_interrupts():
                    break
                poll(MIN_POLL_TIME, 0.25)
                for trig in self.triggers:
                    if trig.stop is not None:
                        trig.stop()
                    if trig.runtime < self.min_dwelltime / 2.0:
                        point_ok = False
                if not point_ok:
                    point_ok = True
                    poll(5*MIN_POLL_TIME, 0.25)
                    for trig in self.triggers:
                        if trig.runtime < self.min_dwelltime / 2.0:
                            point_ok = False
                if not point_ok:
                    print 'Trigger problem: ', trig, trig.runtime, self.min_dwelltime

                # wait, then read read counters and actual positions
                poll(self.det_settle_time, 0.25)
                if self.look_for_interrupts():
                    break
                [c.read() for c in self.counters]
                # print 'Read Counters done'
                self.cdat = [c.buff[-1] for c in self.counters]
                self.pos_actual.append([p.current() for p in self.positioners])

                # if a messenger exists, let it know this point has finished
                if self.msg_thread is not None:
                    self.msg_thread.cpt = self.cpt

                # if this is a breakpoint, execute those functions
                if i in self.breakpoints:
                    self.at_break(breakpoint=i, clear=True)
                    if self.look_for_interrupts():
                        break

            except KeyboardInterrupt:
                self.scandb.set_info('request_abort', 1l)
            if not point_ok:
                print 'point messed up... try again?'
                i -= 1

        # scan complete
        # return to original positions, write data
        ts_loop = time.time()
        self.looptime = ts_loop - ts_init
        if self.look_for_interrupts():
            print "scan aborted at point %i of %i." % (self.cpt, self.npts)

        for val, pos in zip(orig_positions, self.positioners):
            pos.move_to(val, wait=False)
        self.datafile.write_data(breakpoint=-1, close_file=True, clear=False)
        self.abort = False
        self.clear_interrupts()

        # run post_scan methods
        out = self.post_scan()
        self.check_outputs(out, msg='post scan')

        self.complete = True

        # end messenger thread
        if self.msg_thread is not None:
            self.msg_thread.cpt = None
            self.msg_thread.join()

        ts_exit = time.time()
        self.exittime = ts_exit - ts_loop
        self.runtime  = ts_exit - ts_start
        return self.datafile.filename
        ##

@ValidateLarchPlugin
def scan_from_json(text, filename='scan.001', _larch=None):
    sdict = json.loads(text)

    scan = LarchStepScan(filename=filename, _larch=_larch)
    if sdict['type'] == 'xafs':
        print 'xafs scan soon'
        x = """
        scan  = XAFS_Scan(energy_pv=sdict['energy_drive'],
                          read_pv=sdict['energy_read'],
                          e0=sdict['e0'])
        t_kw  = sdict['time_kw']
        t_max = sdict['max_time']
        nreg  = len(sdict['regions'])
        kws  = {'relative': sdict['is_relative']}
        for i, det in enumerate(sdict['regions']):
            start, stop, npts, dt, units = det
            kws['dtime'] =  dt
            kws['use_k'] =  units.lower() !='ev'
            if i == nreg-1: # final reg
                if t_max > dt and t_kw>0 and kws['use_k']:
                    kws['dtime_final'] = t_max
                    kws['dtime_wt'] = t_kw
            scan.add_region(start, stop, npts=npts, **kws)
        """
    elif sdict['type'] == 'linear':
        for pos in sdict['positioners']:
            label, pvs, start, stop, npts = pos
            p = Positioner(pvs[0], label=label)
            p.array = np.linspace(start, stop, npts)
            scan.add_positioner(p)
            if len(pvs) > 0:
                scan.add_counter(pvs[1], label="%s_read" % label)

    elif sdict['type'] == 'mesh':
        label1, pvs1, start1, stop1, npts1 = sdict['inner']
        label2, pvs2, start2, stop2, npts2 = sdict['outer']
        p1 = Positioner(pvs1[0], label=label1)
        p2 = Positioner(pvs2[0], label=label2)

        inner = npts2* [np.linspace(start1, stop1, npts1)]
        outer = [[i]*npts1 for i in np.linspace(start2, stop2, npts2)]

        p1.array = np.array(inner).flatten()
        p2.array = np.array(outer).flatten()
        scan.add_positioner(p1)
        scan.add_positioner(p2)
        if len(pvs1) > 0:
            scan.add_counter(pvs1[1], label="%s_read" % label1)
        if len(pvs2) > 0:
            scan.add_counter(pvs2[1], label="%s_read" % label2)

    elif sdict['type'] == 'slew':
        label1, pvs1, start1, stop1, npts1 = sdict['inner']
        p1 = Positioner(pvs1[0], label=label1)
        p1.array = np.linspace(start1, stop1, npts1)
        scan.add_positioner(p1)
        if len(pvs1) > 0:
            scan.add_counter(pvs1[1], label="%s_read" % label1)
        if sdict['dimension'] >=2:
            label2, pvs2, start2, stop2, npts2 = sdict['outer']
            p2 = Positioner(pvs2[0], label=label2)
            p2.array = np.linspace(start2, stop2, npts2)
            scan.add_positioner(p2)
            if len(pvs2) > 0:
                scan.add_counter(pvs2[1], label="%s_read" % label2)

    for dpars in sdict['detectors']:
        scan.add_detector(get_detector(**dpars))

    if 'counters' in sdict:
        for label, pvname  in sdict['counters']:
            scan.add_counter(pvname, label=label)

    scan.add_extra_pvs(sdict['extra_pvs'])
    scan.scantime  = sdict.get('scantime', -1)
    scan.filename  = sdict.get('filename', 'scan.dat')
    if filename is not None:
        scan.filename  = filename
    scan.pos_settle_time = sdict.get('pos_settle_time', 0.01)
    scan.det_settle_time = sdict.get('det_settle_time', 0.01)
    if scan.dwelltime is None:
        scan.set_dwelltime(sdict.get('dwelltime', 1))
    return scan


@ValidateLarchPlugin
def scan_from_db(name, filename='scan.001', _larch=None):
    """get scan definition from ScanDB"""
    if _larch.symtable._scan._scandb is None:
        return
    sdb = _larch.symtable._scan._scandb
    return scan_from_json(sdb.get_scandef(name).text,
                          filename=filename, _larch=_larch)


@ValidateLarchPlugin
def connect_scandb(dbname=None, server='postgresql',
                   _larch=None, **kwargs):
    if (_larch.symtable.has_symbol(SCANDB_NAME) and
        _larch.symtable.get_symbol(SCANDB_NAME) is not None):
        return _larch.symtable.get_symbol(SCANDB_NAME)
    scandb = ScanDB(dbname=dbname, server=server, **kwargs)
    _larch.symtable.set_symbol(SCANDB_NAME, scandb)
    return scandb

@ValidateLarchPlugin
def do_scan(scanname, filename='scan.001', number=1,
            _larch=None):
    """execute scan defined in ScanDB"""
    if _larch.symtable._scan._scandb is None:
        print 'need to connect to scandb!'
        return
    scan = scan_from_db(scanname, filename=filename,
                            _larch=_larch)
    for i in range(number):
        scan.run()


def initializeLarchPlugin(_larch=None):
    """initialize _scan"""
    if not _larch.symtable.has_group(MODNAME):
        g = Group()
        g.__doc__ = MODDOC
        _larch.symtable.set_symbol(MODNAME, g)

def registerLarchPlugin():
    print 'loaded step scan plugin'
    return ('_epics', {'scan_from_json': scan_from_json,
                       'scan_from_db':   scan_from_db,
                       'connect_scandb':    connect_scandb,
                       'do_scan': do_scan,
                       })