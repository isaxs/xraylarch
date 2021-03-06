import sys
import os
import socket
import time
import h5py
import numpy as np
import scipy.stats as stats
import json
import larch
from larch import use_plugin_path
from larch.utils.debugtime import debugtime

use_plugin_path('io')
use_plugin_path('xrf')
use_plugin_path('xrfmap')

from fileutils import nativepath, new_filename
from mca import MCA
from roi import ROI

from configfile import FastMapConfig
from xmap_netcdf import read_xmap_netcdf
from xsp3_hdf5 import read_xsp3_hdf5
from asciifiles import (readASCII, readMasterFile, readROIFile,
                        readEnvironFile, parseEnviron)

NINIT = 32
COMPRESSION_LEVEL = 4 # compression level
DEFAULT_ROOTNAME = 'xrfmap'

class GSEXRM_FileStatus:
    no_xrfmap    = 'hdf5 does not have top-level XRF map'
    created      = 'hdf5 has empty schema'  # xrfmap exists, no data
    hasdata      = 'hdf5 has map data'      # array sizes known
    wrongfolder  = 'hdf5 exists, but does not match folder name'
    err_notfound = 'file not found'
    empty        = 'file is empty (read from folder)'
    err_nothdf5  = 'file is not hdf5 (or cannot be read)'

def getFileStatus(filename, root=None, folder=None):
    """return status, top-level group, and version"""
    # set defaults for file does not exist
    status, top, vers = GSEXRM_FileStatus.err_notfound, '', ''
    if root not in ('', None):
        top = root
    # see if file exists:
    if (not os.path.exists(filename) or
        not os.path.isfile(filename) ):
        return status, top, vers

    # see if file is empty/too small(signifies "read from folder")
    if os.stat(filename).st_size < 512:
        return GSEXRM_FileStatus.empty, top, vers

    # see if file is an H5 file
    try:
        fh = h5py.File(filename)
    except IOError:
        return GSEXRM_FileStatus.err_nothdf5, top, vers

    status =  GSEXRM_FileStatus.no_xrfmap
    ##
    def test_h5group(group, folder=None):
        valid = ('config' in group and 'roimap' in group)
        for attr in  ('Version', 'Map_Folder',
                      'Dimension', 'Start_Time'):
            valid = valid and attr in group.attrs
        if not valid:
            return None, None
        status = GSEXRM_FileStatus.hasdata
        vers = group.attrs['Version']
        if folder is not None and folder != group.attrs['Map_Folder']:
            status = GSEXRM_FileStatus.wrongfolder
        return status, vers

    if root is not None and root in fh:
        s, v = test_h5group(fh[root], folder=folder)
        if s is not None:
            status, top, vers = s, root, v
    else:
        # print 'Root was None ', fh.items()
        for name, group in fh.items():
            s, v = test_h5group(group, folder=folder)
            if s is not None:
                status, top, vers = s, name, v
                break
    fh.close()
    return status, top, vers

def isGSEXRM_MapFolder(fname):
    "return whether folder a valid Scan Folder (raw data)"
    if (fname is None or not os.path.exists(fname) or
        not os.path.isdir(fname)):
        return False
    flist = os.listdir(fname)
    for f in ('Master.dat', 'Environ.dat', 'Scan.ini'):
        if f not in flist:
            return False
    has_xrfdata = False
    for f in ('xmap.0001', 'xsp3.0001'):
        if f in flist: has_xrfdata = True
    return has_xrfdata

H5ATTRS = {'Type': 'XRF 2D Map',
           'Version': '1.4.0',
           'Title': 'Epics Scan Data',
           'Beamline': 'GSECARS, 13-IDE / APS',
           'Start_Time':'',
           'Stop_Time':'',
           'Map_Folder': '',
           'Dimension': 2,
           'Process_Machine':'',
           'Process_ID': 0}

def create_xrfmap(h5root, root=None, dimension=2,
                  folder='', start_time=None):
    """creates a skeleton '/xrfmap' group in an open HDF5 file

    This is left as a function, not method of GSEXRM_MapFile below
    because it may be called by the mapping collection program
    (ie, from collector.py) when a map is started

    This leaves a structure to be filled in by
    GSEXRM_MapFile.init_xrfmap(),
    """
    attrs = {}
    attrs.update(H5ATTRS)
    if start_time is None:
        start_time = time.ctime()
    attrs.update({'Dimension':dimension, 'Start_Time':start_time,
                  'Map_Folder': folder, 'Last_Row': -1})
    if root in ('', None):
        root = DEFAULT_ROOTNAME
    xrfmap = h5root.create_group(root)
    for key, val in attrs.items():
        xrfmap.attrs[key] = str(val)

    g = xrfmap.create_group('roimap')
    g.attrs['type'] = 'roi maps'
    g.attrs['desc'] = 'ROI data, including summed and deadtime corrected maps'

    g = xrfmap.create_group('config')
    g.attrs['type'] = 'scan config'
    g.attrs['desc'] = '''scan configuration, including scan definitions,
    ROI definitions, MCA calibration, Environment Data, etc'''

    xrfmap.create_group('areas')
    xrfmap.create_group('positions')

    conf = xrfmap['config']
    for name in ('scan', 'general', 'environ', 'positioners',
                 'motor_controller', 'rois', 'mca_settings', 'mca_calib'):
        conf.create_group(name)
    h5root.flush()

class GSEXRM_Exception(Exception):
    """GSEXRM Exception: General Errors"""
    def __init__(self, msg):
        Exception.__init__(self)
        self.msg = msg
    def __str__(self):
        return self.msg

class GSEXRM_NotOwner(Exception):
    """GSEXRM Not Owner Host/Process ID"""
    def __init__(self, msg):
        Exception.__init__(self)
        self.msg = 'Not Owner of HDF5 file %s' % msg
    def __str__(self):
        return self.msg

class GSEXRM_MapRow:
    """
    read one row worth of data:
    """
    def __init__(self, yvalue, xmapfile, xpsfile, sisfile, folder,
                 xrftype='xmap', reverse=False, ixaddr=0, dimension=2,
                 nrows_expected=None,
                 npts=None,  irow=None, dtime=None):

        self.nrows_expected = nrows_expected
        xrf_reader = read_xmap_netcdf
        if xmapfile.startswith('xsp'):
            xrf_reader = read_xsp3_hdf5
        # print 'MapRow: ', xrf_reader, xmapfile, self.nrows_expected

        self.npts = npts
        self.irow = irow
        self.yvalue = yvalue
        self.xmapfile = xmapfile
        self.xpsfile = xpsfile
        self.sisfile = sisfile

        shead, sdata = readASCII(os.path.join(folder, sisfile))
        ghead, gdata = readASCII(os.path.join(folder, xpsfile))
        self.sishead = shead
        if dtime is not None:  dtime.add('maprow: read ascii files')
        t0 = time.time()
        atime = -1

        xmapdat = None
        xmfile = os.path.join(folder, xmapfile)
        while atime < 0 and time.time()-t0 < 10:
            try:
                atime = os.stat(xmfile).st_ctime
                xmapdat = xrf_reader(xmfile, npixels=self.nrows_expected, verbose=False)
            except (IOError, IndexError):
                time.sleep(0.010)

        if atime < 0 or xmapdat is None:
            print( 'Failed to read XRF data from %s' % self.xmapfile)
            return
        if dtime is not None:  dtime.add('maprow: read XRF files')
        #
        self.counts    = xmapdat.counts # [:]
        self.inpcounts = xmapdat.inputCounts[:]
        self.outcounts = xmapdat.outputCounts[:]

        # times are extracted from the netcdf file as floats of microseconds
        # here we truncate to nearest microsecond (clock tick is 0.32 microseconds)
        self.livetime  = (xmapdat.liveTime[:]).astype('int')
        self.realtime  = (xmapdat.realTime[:]).astype('int')

        dt_denom = xmapdat.outputCounts*xmapdat.liveTime
        dt_denom[np.where(dt_denom < 1)] = 1.0
        self.dtfactor  = xmapdat.inputCounts*xmapdat.realTime/dt_denom

        gnpts, ngather  = gdata.shape
        snpts, nscalers = sdata.shape
        xnpts, nmca, nchan = self.counts.shape
        # npts = min(gnpts, xnpts, snpts)
        # print '  MapRow: ', self.npts, npts, gnpts, snpts, xnpts
        if self.npts is None:
            self.npts = min(gnpts, xnpts) - 1
        if snpts < self.npts:  # extend struck data if needed
            print '     extending SIS data!', snpts, self.npts
            sdata = list(sdata)
            for i in range(self.npts+1-snpts):
                sdata.append(sdata[snpts-1])
            sdata = np.array(sdata)
            snpts = self.npts
        self.sisdata = sdata[:self.npts]

        if xnpts > self.npts:
            self.counts  = self.counts[:self.npts]
            self.realtime = self.realtime[:self.npts]
            self.livetime = self.livetime[:self.npts]
            self.dtfactor = self.dtfactor[:self.npts]
            self.inpcounts= self.inpcounts[:self.npts]
            self.outcounts= self.outcounts[:self.npts]

        points = range(1, self.npts+1)
        if reverse:
            points.reverse()
            self.sisdata  = self.sisdata[::-1]
            self.counts  = self.counts[::-1]
            self.realtime = self.realtime[::-1]
            self.livetime = self.livetime[::-1]
            self.dtfactor = self.dtfactor[::-1]
            self.inpcounts= self.inpcounts[::-1]
            self.outcounts= self.outcounts[::-1]

        xvals = [(gdata[i, ixaddr] + gdata[i-1, ixaddr])/2.0 for i in points]

        self.posvals = [np.array(xvals)]
        if dimension == 2:
            self.posvals.append(np.array([float(yvalue) for i in points]))
        self.posvals.append(self.realtime.sum(axis=1).astype('float32') / nmca)
        self.posvals.append(self.livetime.sum(axis=1).astype('float32') / nmca)

        total = None
        for imca in range(nmca):
            dtcorr = self.dtfactor[:, imca].astype('float32')
            cor   = dtcorr.reshape((dtcorr.shape[0], 1))
            if total is None:
                total = self.counts[:, imca, :] * cor
            else:
                total = total + self.counts[:, imca, :] * cor
        self.total = total.astype('int16')
        self.dtfactor = self.dtfactor.astype('float32')
        self.dtfactor = self.dtfactor.transpose()
        self.inpcounts= self.inpcounts.transpose()
        self.outcounts= self.outcounts.transpose()
        self.livetime = self.livetime.transpose()
        self.realtime = self.realtime.transpose()
        self.counts   = self.counts.swapaxes(0, 1)
        
class GSEXRM_Detector(object):
    """Detector class, representing 1 detector element (real or virtual)
    has the following properties (many of these as runtime-calculated properties)

    rois           list of ROI objects
    rois[i].name        names
    rois[i].address     address
    rois[i].left        index of lower limit
    rois[i].right       index of upper limit
    energy         array of energy values
    counts         array of count values
    dtfactor       array of deadtime factor
    realtime       array of real time
    livetime       array of live time
    inputcounts    array of input counts
    outputcount    array of output count

    """
    def __init__(self, xrfmap, index=None):
        self.xrfmap = xrfmap
        self.__ndet =  xrfmap.attrs['N_Detectors']
        self.det = None
        self.rois = []
        detname = 'det1'
        if index is not None:
            self.det = self.xrfmap['det%i' % index]
            detname = 'det%i' % index

        self.shape =  self.xrfmap['%s/livetime' % detname].shape

        # energy
        self.energy = self.xrfmap['%s/energy' % detname].value

        # set up rois
        rnames = self.xrfmap['%s/roi_names' % detname].value
        raddrs = self.xrfmap['%s/roi_addrs' % detname].value
        rlims  = self.xrfmap['%s/roi_limits' % detname].value
        for name, addr, lims in zip(rnames, raddrs, rlims):
            self.rois.append(ROI(name=name, address=addr,
                                 left=lims[0], right=lims[1]))

    def __getval(self, param):
        if self.det is None:
            out = self.xrfmap['det1/%s' % (param)].value
            for i in range(2, self.__ndet):
                out += self.xrfmap['det%i/%s' % (i, param)].value
            return out
        return self.det[param].value

    @property
    def counts(self):
        "detector counts array"
        return self.__getval('counts')

    @property
    def dtfactor(self):
        """deadtime factor"""
        return self.__getval('dtfactor')

    @property
    def realtime(self):
        """real time"""
        return self.__getval('realtime')

    @property
    def livetime(self):
        """live time"""
        return self.__getval('livetime')

    @property
    def inputcounts(self):
        """inputcounts"""
        return self.__getval('inputcounts')

    @property
    def outputcount(self):
        """output counts"""
        return self.__getval('outputcounts')


class GSEXRM_Area(object):
    """Map Area class, representing a map area for a detector
    """
    def __init__(self, xrfmap, index, det=None):
        self.xrfmap = xrfmap
        self.det = GSEXRM_Detector(xrfmap, index=det)
        if isinstance(index, int):
            index = 'area_%3.3i' % index
        self._area = self.xrfmap['areas/%s' % index]
        self.npts = self._area.value.sum()

        sy, sx = [slice(min(_a), max(_a)+1) for _a in np.where(self._area)]
        self.yslice, self.xslice = sy, sx

    def roicounts(self, roiname):
        iroi = -1
        for ir, roi in enumerate(self.det.rois):
            if roiname.lower() == roi.name.lower():
                iroi = ir
                break
        if iroi < 0:
            raise ValueError('ROI name %s not found' % roiname)
        elo, ehi = self.det.rois[iroi].left, self.det.rois[iroi].right
        counts = self.det.counts[self.yslice, self.xslice, elo:ehi]


class GSEXRM_MapFile(object):
    """
    Access to GSECARS X-ray Microprobe Map File:

    The GSEXRM Map file is an HDF5 file built from a folder containing
    'raw' data from a set of sources
         xmap:   XRF spectra saved to NetCDF by the Epics MCA detector
         struck: a multichannel scaler, saved as ASCII column data
         xps:    stage positions, saved as ASCII file from the Newport XPS

    The object here is intended to expose an HDF5 file that:
         a) watches the corresponding folder and auto-updates when new
            data is available, as for on-line collection
         b) stores locking information (Machine Name/Process ID) in the top-level

    For extracting data from a GSEXRM Map File, use:

    >>> from epicscollect.io import GSEXRM_MapFile
    >>> map = GSEXRM_MapFile('MyMap.001')
    >>> fe  = map.get_roimap('Fe')
    >>> as  = map.get_roimap('As Ka', det=1, dtcorrect=True)
    >>> rgb = map.get_rgbmap('Fe', 'Ca', 'Zn', det=None, dtcorrect=True, scale_each=False)
    >>> en  = map.get_energy(det=1)

    All these take the following options:

       det:         which detector element to use (1, 2, 3, 4, None), [None]
                    None means to use the sum of all detectors
       dtcorrect:   whether to return dead-time corrected spectra     [True]

    """

    ScanFile   = 'Scan.ini'
    EnvFile    = 'Environ.dat'
    ROIFile    = 'ROI.dat'
    MasterFile = 'Master.dat'

    def __init__(self, filename=None, folder=None, root=None,
                 chunksize=None):
        self.filename = filename
        self.folder   = folder
        self.root     = root
        self.chunksize=chunksize
        self.status   = GSEXRM_FileStatus.err_notfound
        self.dimension = None
        self.ndet       = None
        self.start_time = None
        self.xrfmap   = None
        self.xrfdet_type = 'xmap'
        self.h5root   = None
        self.last_row = -1
        self.rowdata = []
        self.npts = None
        self.roi_slices = None
        self.pixeltime = None
        self.dt = debugtime()
        self.masterfile = None
        self.masterfile_mtime = -1

        # initialize from filename or folder
        if self.filename is not None:
            self.status, self.root, self.version = \
                         getFileStatus(self.filename, root=root)
            # print 'Filename ', self.filename, self.status, self.root, self.version
            # see if file is too small (signifies "read from folder")
            if self.status == GSEXRM_FileStatus.empty:
                ftmp = open(self.filename, 'r')
                self.folder = ftmp.readlines()[0][:-1].strip()
                ftmp.close()
                os.unlink(self.filename)

        if isGSEXRM_MapFolder(self.folder):
            self.read_master()
            if self.filename is None:
                raise GSEXRM_Exception(
                    "'%s' is not a valid GSEXRM Map folder" % self.folder)
            self.status, self.root, self.version = \
                         getFileStatus(self.filename, root=root,
                                       folder=self.folder)


        # for existing file, read initial settings
        if self.status in (GSEXRM_FileStatus.hasdata,
                           GSEXRM_FileStatus.created):
            self.open(self.filename, root=self.root, check_status=False)
            return

        # file exists but is not hdf5
        if self.status ==  GSEXRM_FileStatus.err_nothdf5:
            raise GSEXRM_Exception(
                "'%s' is not a readlable HDF5 file" % self.filename)

        # create empty HDF5 if needed
        # print '-> filename ', self.filename, self.status
        if (self.status in (GSEXRM_FileStatus.err_notfound,
                            GSEXRM_FileStatus.wrongfolder) and
            self.folder is not None and isGSEXRM_MapFolder(self.folder)):
            self.read_master()
            if self.status == GSEXRM_FileStatus.wrongfolder:
                self.filename = new_filename(self.filename)
                cfile = FastMapConfig()
                cfile.Read(os.path.join(self.folder, self.ScanFile))
                cfile.config['scan']['filename'] = self.filename
                cfile.Save(os.path.join(self.folder, self.ScanFile))

            self.h5root = h5py.File(self.filename)
            if self.dimension is None and isGSEXRM_MapFolder(self.folder):
                self.read_master()
            create_xrfmap(self.h5root, root=self.root, dimension=self.dimension,
                          folder=self.folder, start_time=self.start_time)
            self.status = GSEXRM_FileStatus.created
            self.open(self.filename, root=self.root, check_status=False)
        else:
            raise GSEXRM_Exception(
                "'GSEXMAP Error: could not locate map file or folder")

    def get_det(self, index):
        return GSEXRM_Detector(self.xrfmap, index=index)

    def area_obj(self, index, det=None):
        return GSEXRM_Area(self.xrfmap, index, det=det)

    def get_scanconfig(self):
        """return scan configuration from file"""
        conftext = self.xrfmap['config/scan/text'].value
        return FastMapConfig(conftext=conftext)

    def get_coarse_stages(self):
        """return coarse stage positions for map"""
        stages = []
        env_addrs = list(self.xrfmap['config/environ/address'])
        env_vals  = list(self.xrfmap['config/environ/value'])
        for addr, pname in self.xrfmap['config/positioners'].items():
            name = str(pname.value)
            addr = str(addr)
            val = ''
            if not addr.endswith('.VAL'):
                addr = '%s.VAL' % addr
            if addr in env_addrs:
                val = env_vals[env_addrs.index(addr)]

            stages.append((addr, val, name))

        return stages

    def open(self, filename, root=None, check_status=True):
        """open GSEXRM HDF5 File :
        with check_status=False, this **must** be called
        for an existing, valid GSEXRM HDF5 File!!
        """
        if root in ('', None):
            root = DEFAULT_ROOTNAME
        if check_status:
            self.status, self.root, self.version = \
                         getFileStatus(filename, root=root)
            if self.status not in (GSEXRM_FileStatus.hasdata,
                                   GSEXRM_FileStatus.created):
                raise GSEXRM_Exception(
                    "'%s' is not a valid GSEXRM HDF5 file" % self.filename)
        self.filename = filename
        if self.h5root is None:
            self.h5root = h5py.File(self.filename)
        self.xrfmap = self.h5root[root]
        if self.folder is None:
            self.folder = self.xrfmap.attrs['Map_Folder']
        self.last_row = int(self.xrfmap.attrs['Last_Row'])

        try:
            self.dimension = self.xrfmap['config/scan/dimension'].value
        except:
            pass

        if (len(self.rowdata) < 1 or
            (self.dimension is None and isGSEXRM_MapFolder(self.folder))):
            self.read_master()

    def close(self):
        if self.check_hostid():
            self.xrfmap.attrs['Process_Machine'] = ''
            self.xrfmap.attrs['Process_ID'] = 0
            self.xrfmap.attrs['Last_Row'] = self.last_row
        self.h5root.close()
        self.h5root = None

    def add_data(self, group, name, data, attrs=None, **kws):
        """ creata an hdf5 dataset"""
        if not self.check_hostid():
            raise GSEXRM_NotOwner(self.filename)

        kwargs = {'compression': COMPRESSION_LEVEL}
        kwargs.update(kws)
        d = group.create_dataset(name, data=data, **kwargs)
        if isinstance(attrs, dict):
            for key, val in attrs.items():
                d.attrs[key] = val
        return d

    def add_map_config(self, config):
        """add configuration from Map Folder to HDF5 file
        ROI, DXP Settings, and Config data
        """
        if not self.check_hostid():
            raise GSEXRM_NotOwner(self.filename)

        group = self.xrfmap['config']
        scantext = open(os.path.join(self.folder, self.ScanFile), 'r').read()
        for name, sect in (('scan', 'scan'),
                           ('general', 'general'),
                           ('positioners', 'slow_positioners'),
                           ('motor_controller', 'xps')):
            for key, val in config[sect].items():
                group[name].create_dataset(key, data=val)

        group['scan'].create_dataset('text', data=scantext)

        roidat, calib, extra = readROIFile(os.path.join(self.folder, self.ROIFile))
        self.ndet = len(calib['slope'])
        self.xrfmap.attrs['N_Detectors'] = self.ndet
        roi_desc, roi_addr, roi_lim = [], [], []
        roi_slices = []
        for iroi, label, lims in roidat:
            roi_desc.append(label)
            roi_addr.append("%smca%%i.R%i" % (config['xrf']['prefix'], iroi))
            roi_lim.append([lims[i] for i in range(self.ndet)])
            roi_slices.append([slice(lims[i][0], lims[i][1]) for i in range(self.ndet)])
        roi_lim = np.array(roi_lim)

        self.add_data(group['rois'], 'name',     roi_desc)
        self.add_data(group['rois'], 'address',  roi_addr)
        self.add_data(group['rois'], 'limits',   roi_lim)

        for key, val in calib.items():
            self.add_data(group['mca_calib'], key, val)

        for key, val in extra.items():
            self.add_data(group['mca_settings'], key, val)

        self.roi_desc = roi_desc
        self.roi_addr = roi_addr
        self.roi_slices = roi_slices
        self.calib = calib
        # add env data
        envdat = readEnvironFile(os.path.join(self.folder, self.EnvFile))
        env_desc, env_addr, env_val = parseEnviron(envdat)

        self.add_data(group['environ'], 'name',     env_desc)
        self.add_data(group['environ'], 'address',  env_addr)
        self.add_data(group['environ'], 'value',     env_val)
        self.h5root.flush()

    def initialize_xrfmap(self):
        """ initialize '/xrfmap' group in HDF5 file, generally
        possible once at least 1 row of raw data is available
        in the scan folder.
        """
        if self.status == GSEXRM_FileStatus.hasdata:
            return
        if self.status != GSEXRM_FileStatus.created:
            print( 'Warning, cannot initialize xrfmap yet.')
            return

        if not self.check_hostid():
            raise GSEXRM_NotOwner(self.filename)

        if (len(self.rowdata) < 1 or
            (self.dimension is None and isGSEXRM_MapFolder(self.folder))):
            self.read_master()
        self.npts = None
        if len(self.rowdata) < 1:
            return
        self.last_row = -1
        self.add_map_config(self.mapconf)
        row = self.read_rowdata(0)
        self.build_schema(row)
        self.add_rowdata(row)
        self.status = GSEXRM_FileStatus.hasdata

    def process(self, maxrow=None, force=False, callback=None, verbose=True):
        "look for more data from raw folder, process if needed"
        # print('PROCESS  ', maxrow, force, self.filename, self.dimension, len(self.rowdata))
        if not self.check_hostid():
            raise GSEXRM_NotOwner(self.filename)

        if self.status == GSEXRM_FileStatus.created:
            self.initialize_xrfmap()
        if (force or len(self.rowdata) < 1 or
            (self.dimension is None and isGSEXRM_MapFolder(self.folder))):
            self.read_master()
        nrows = len(self.rowdata)
        if maxrow is not None:
            nrows = min(nrows, maxrow)
        if force or self.folder_has_newdata():
            irow = self.last_row + 1
            while irow < nrows:
                # self.dt.add('=>PROCESS %i' % irow)
                if hasattr(callback, '__call__'):
                    callback(row=irow, maxrow=nrows,
                             filename=self.filename, status='reading')
                row = self.read_rowdata(irow)
                # self.dt.add('  == read row data')
                if row is not None:
                    self.add_rowdata(row, verbose=verbose)
                # self.dt.add('  == added row data')
                if hasattr(callback, '__call__'):
                    callback(row=irow, maxrow=nrows,
                             filename=self.filename, status='complete')
                irow  = irow + 1
            # self.dt.show()
        self.resize_arrays(self.last_row+1)
        self.h5root.flush()
        if self.pixeltime is None:
            self.calc_pixeltime()

    def calc_pixeltime(self):
        scanconf = self.xrfmap['config/scan']
        rowtime = float(scanconf['time1'].value)
        start = float(scanconf['start1'].value)
        stop = float(scanconf['stop1'].value)
        step = float(scanconf['step1'].value)
        npts = 1 + int((abs(stop - start) + 1.1*step)/step)
        self.pixeltime = rowtime/npts
        return self.pixeltime

    def read_rowdata(self, irow):
        """read a row's worth of raw data from the Map Folder
        returns arrays of data
        """
        if self.dimension is None or irow > len(self.rowdata):
            self.read_master()

        if self.folder is None or irow >= len(self.rowdata):
            return

        yval, xmapf, sisf, xpsf, etime = self.rowdata[irow]
        reverse = (irow % 2 != 0)
        return GSEXRM_MapRow(yval, xmapf, xpsf, sisf, irow=irow,
                             xrftype=self.xrfdet_type,
                             nrows_expected=self.nrows_expected,
                             ixaddr=self.ixaddr,
                             dimension=self.dimension, npts=self.npts,
                             folder=self.folder, reverse=reverse)

    def add_rowdata(self, row, verbose=True):
        """adds a row worth of real data"""
        if not self.check_hostid():
            raise GSEXRM_NotOwner(self.filename)
        thisrow = self.last_row + 1
        nmca, xnpts, nchan = row.counts.shape
        mcas = []
        nrows = 0
        map_items = sorted(self.xrfmap.keys())
        for gname in map_items:
            g = self.xrfmap[gname]
            if g.attrs.get('type', None) == 'mca detector':
                mcas.append(g)
                nrows, npts, nchan =  g['counts'].shape

        if thisrow >= nrows:
            self.resize_arrays(32*(1+nrows/32))

        total = None
        # self.dt.add('add_rowdata b4 adding mcas')
        for imca, grp in enumerate(mcas):
            grp['dtfactor'][thisrow, :]  = row.dtfactor[imca, :]
            grp['realtime'][thisrow, :]  = row.realtime[imca, :]
            grp['livetime'][thisrow, :]  = row.livetime[imca, :]
            grp['inpcounts'][thisrow, :] = row.inpcounts[imca, :]
            grp['outcounts'][thisrow, :] = row.outcounts[imca, :]
            grp['counts'][thisrow, :, :] = row.counts[imca, :, :]

        # self.dt.add('add_rowdata for mcas')
        # here, we add the total dead-time-corrected data to detsum.
        self.xrfmap['detsum']['counts'][thisrow, :] = row.total[:]
        # self.dt.add('add_rowdata for detsum')

        pos    = self.xrfmap['positions/pos']
        # print(" ADD ROWDATA ", row)
        # print(" ADD ROWDATA ", row.posvals)

        pos[thisrow, :, :] = np.array(row.posvals).transpose()

        # now add roi map data
        roimap = self.xrfmap['roimap']
        det_raw = roimap['det_raw']
        det_cor = roimap['det_cor']
        sum_raw = roimap['sum_raw']
        sum_cor = roimap['sum_cor']

        detraw = list(row.sisdata[:npts].transpose())

        if verbose:
            pform = "Add row %4i, yval=%s, npts=%i, xrffile=%s"
            print(pform % (thisrow+1, row.yvalue, npts, row.xmapfile))

        detcor = detraw[:]
        sumraw = detraw[:]
        sumcor = detraw[:]

        # self.dt.add('add_rowdata b4 roi')
        if self.roi_slices is None:
            lims = self.xrfmap['config/rois/limits'].value
            nrois, nmca, nx = lims.shape
            self.roi_slices = []
            for iroi in range(nrois):
                x = [slice(lims[iroi, i, 0],
                           lims[iroi, i, 1]) for i in range(nmca)]
                self.roi_slices.append(x)

        for slices in self.roi_slices:
            iraw = [row.counts[i, :, slices[i]].sum(axis=1)
                    for i in range(nmca)]
            icor = [row.counts[i, :, slices[i]].sum(axis=1)*row.dtfactor[i, :]
                    for i in range(nmca)]
            detraw.extend(iraw)
            detcor.extend(icor)
            sumraw.append(np.array(iraw).sum(axis=0))
            sumcor.append(np.array(icor).sum(axis=0))

        # self.dt.add('add_rowdata after roi')
        det_raw[thisrow, :, :] = np.array(detraw).transpose()
        det_cor[thisrow, :, :] = np.array(detcor).transpose()
        sum_raw[thisrow, :, :] = np.array(sumraw).transpose()
        sum_cor[thisrow, :, :] = np.array(sumcor).transpose()

        # self.dt.add('add_rowdata end')
        self.last_row = thisrow
        self.xrfmap.attrs['Last_Row'] = thisrow
        self.h5root.flush()

    def build_schema(self, row):
        """build schema for detector and scan data"""
        if not self.check_hostid():
            raise GSEXRM_NotOwner(self.filename)

        if self.npts is None:
            self.npts = row.npts
        npts = self.npts
        nmca, xnpts, nchan = row.counts.shape

        if self.chunksize is None:
            nxx = min(xnpts-1, 2**int(np.log2(xnpts)))
            nxm = 1024
            if nxx > 256:
                nxm = min(1024, int(65536*1.0/ nxx))
            self.chunksize = (1, nxx, nxm)
        en_index = np.arange(nchan)

        xrfmap = self.xrfmap
        conf   = self.xrfmap['config']

        offset = conf['mca_calib/offset'].value
        slope  = conf['mca_calib/slope'].value
        quad   = conf['mca_calib/quad'].value

        roi_names = list(conf['rois/name'])
        roi_addrs = list(conf['rois/address'])
        roi_limits = conf['rois/limits'].value
        for imca in range(nmca):
            dname = 'det%i' % (imca+1)
            dgrp = xrfmap.create_group(dname)
            dgrp.attrs['type'] = 'mca detector'
            dgrp.attrs['desc'] = 'mca%i' % (imca+1)
            en  = 1.0*offset[imca] + slope[imca]*1.0*en_index
            self.add_data(dgrp, 'energy', en, attrs={'cal_offset':offset[imca],
                                                     'cal_slope': slope[imca]})

            self.add_data(dgrp, 'roi_name',    roi_names)
            self.add_data(dgrp, 'roi_address', [s % (imca+1) for s in roi_addrs])
            self.add_data(dgrp, 'roi_limits',  roi_limits[:,imca,:])

            dgrp.create_dataset('counts', (NINIT, npts, nchan), np.int16,
                                compression=COMPRESSION_LEVEL,
                                chunks=self.chunksize,
                                maxshape=(None, npts, nchan))
            for name, dtype in (('realtime', np.int),  ('livetime', np.int),
                                ('dtfactor', np.float32),
                                ('inpcounts', np.float32),
                                ('outcounts', np.float32)):
                dgrp.create_dataset(name, (NINIT, npts), dtype,
                                    compression=COMPRESSION_LEVEL,
                                    maxshape=(None, npts))

        # add 'virtual detector' for corrected sum:
        dgrp = xrfmap.create_group('detsum')
        dgrp.attrs['type'] = 'virtual mca'
        dgrp.attrs['desc'] = 'deadtime corrected sum of detectors'
        en = 1.0*offset[0] + slope[0]*1.0*en_index
        self.add_data(dgrp, 'energy', en, attrs={'cal_offset':offset[0],
                                                 'cal_slope': slope[0]})
        self.add_data(dgrp, 'roi_name',    roi_names)
        self.add_data(dgrp, 'roi_address', [s % 1 for s in roi_addrs])
        self.add_data(dgrp, 'roi_limits',  roi_limits[: ,0, :])
        dgrp.create_dataset('counts', (NINIT, npts, nchan), np.int16,
                            compression=COMPRESSION_LEVEL,
                            chunks=self.chunksize,
                            maxshape=(None, npts, nchan))
        # roi map data
        scan = xrfmap['roimap']
        det_addr = [i.strip() for i in row.sishead[-2][1:].split('|')]
        det_desc = [i.strip() for i in row.sishead[-1][1:].split('|')]
        for addr in roi_addrs:
            det_addr.extend([addr % (i+1) for i in range(nmca)])

        for desc in roi_names:
            det_desc.extend(["%s (mca%i)" % (desc, i+1)
                             for i in range(nmca)])

        sums_map = {}
        sums_desc = []
        nsum = 0
        for idet, addr in enumerate(det_desc):
            if '(mca' in addr:
                addr = addr.split('(mca')[0].strip()

            if addr not in sums_map:
                sums_map[addr] = []
                sums_desc.append(addr)
            sums_map[addr].append(idet)
        nsum = max([len(s) for s in sums_map.values()])
        sums_list = []
        for sname in sums_desc:
            slist = sums_map[sname]
            if len(slist) < nsum:
                slist.extend([-1]*(nsum-len(slist)))
            sums_list.append(slist)

        nsum = len(sums_list)
        nsca = len(det_desc)

        sums_list = np.array(sums_list)

        self.add_data(scan, 'det_name',    det_desc)
        self.add_data(scan, 'det_address', det_addr)
        self.add_data(scan, 'sum_name',    sums_desc)
        self.add_data(scan, 'sum_list',    sums_list)

        nxx = min(nsca, 8)
        for name, nx, dtype in (('det_raw', nsca, np.int32),
                                ('det_cor', nsca, np.float32),
                                ('sum_raw', nsum, np.int32),
                                ('sum_cor', nsum, np.float32)):
            scan.create_dataset(name, (NINIT, npts, nx), dtype,
                                compression=COMPRESSION_LEVEL,
                                chunks=(2, npts, nx),
                                maxshape=(None, npts, nx))
        # positions
        pos = xrfmap['positions']
        for pname in ('mca realtime', 'mca livetime'):
            self.pos_desc.append(pname)
            self.pos_addr.append(pname)
        npos = len(self.pos_desc)
        self.add_data(pos, 'name',     self.pos_desc)
        self.add_data(pos, 'address',  self.pos_addr)
        pos.create_dataset('pos', (NINIT, npts, npos), dtype,
                           compression=COMPRESSION_LEVEL,
                           maxshape=(None, npts, npos))
        self.h5root.flush()

    def resize_arrays(self, nrow):
        "resize all arrays for new nrow size"
        if not self.check_hostid():
            raise GSEXRM_NotOwner(self.filename)
        realmca_groups = []
        virtmca_groups = []
        for g in self.xrfmap.values():
            # include both real and virtual mca detectors!
            if g.attrs.get('type', '').startswith('mca det'):
                realmca_groups.append(g)
            elif g.attrs.get('type', '').startswith('virtual mca'):
                virtmca_groups.append(g)
        # print 'resize arrays ', realmca_groups
        oldnrow, npts, nchan = realmca_groups[0]['counts'].shape
        for g in realmca_groups:
            g['counts'].resize((nrow, npts, nchan))
            for aname in ('livetime', 'realtime',
                          'inpcounts', 'outcounts', 'dtfactor'):
                g[aname].resize((nrow, npts))

        for g in virtmca_groups:
            g['counts'].resize((nrow, npts, nchan))

        g = self.xrfmap['positions/pos']
        old, npts, nx = g.shape
        g.resize((nrow, npts, nx))

        for bname in ('det_raw', 'det_cor', 'sum_raw', 'sum_cor'):
            g = self.xrfmap['roimap'][bname]
            old, npts, nx = g.shape
            g.resize((nrow, npts, nx))
        self.h5root.flush()

    def add_area(self, mask, name=None, desc=None):
        """add a selected area, with optional name
        the area is encoded as a boolean array the same size as the map

        """
        if not self.check_hostid():
            raise GSEXRM_NotOwner(self.filename)

        group = self.xrfmap['areas']
        name = 'area_001'
        if len(group) > 0:
            count = len(group)
            while name in group and count < 9999:
                name = 'area_%3.3i' % (count)
                count += 1
        ds = group.create_dataset(name, data=mask)
        if desc is None:
            desc = name
        ds.attrs['description'] = desc
        self.h5root.flush()
        return name

    def get_area(self, name=None, desc=None):
        """
        get area group by name or description
        """
        group = self.xrfmap['areas']
        if name is not None and name in group:
            return group[name]
        if desc is not None:
            for name in group:
                if desc == group[name].attrs['description']:
                    return group[name]
        return None

    def get_area_stats(self, name=None, desc=None):
        """return statistics for all raw detector counts/sec values

        for each raw detector returns
           name, length, mean, standard_deviation,
           median, mode, minimum, maximum,
           gmean, hmean, skew, kurtosis

        """
        area = self.get_area(name=name, desc=desc)
        if area is None:
            return None

        if 'roistats' in area.attrs:
            return json.loads(area.attrs['roistats'])

        amask = area.value

        roidata = []
        d_addrs = [d.lower() for d in self.xrfmap['roimap/det_address']]
        d_names = [d for d in self.xrfmap['roimap/det_name']]
        # count times
        ctime = [1.e-6*self.xrfmap['roimap/det_raw'][:,:,0][amask]]
        for i in range(self.xrfmap.attrs['N_Detectors']):
            tname = 'det%i/realtime' % (i+1)
            ctime.append(1.e-6*self.xrfmap[tname].value[amask])

        for idet, dname in enumerate(d_names):
            daddr = d_addrs[idet]
            det = 0
            if 'mca' in daddr:
                det = 1
                words = daddr.split('mca')
                if len(words) > 1:
                    det = int(words[1].split('.')[0])
            if idet == 0:
                d = ctime[0]
            else:
                d = self.xrfmap['roimap/det_raw'][:,:,idet][amask]/ctime[det]

            try:
                hmean, gmean = stats.gmean(d), stats.hmean(d)
                skew, kurtosis = stats.skew(d), stats.kurtosis(d)
            except ValueError:
                hmean, gmean, skew, kurtosis = 0, 0, 0, 0
            mode = stats.mode(d)
            roidata.append((dname, len(d), d.mean(), d.std(), np.median(d),
                            stats.mode(d), d.min(), d.max(),
                            gmean, hmean, skew, kurtosis))

            if 'roistats' not in area.attrs:
                area.attrs['roistats'] = json.dumps(roidata)
                self.h5root.flush()

        return roidata

    def claim_hostid(self):
        "claim ownershipf of file"
        if self.xrfmap is None:
            return
        self.xrfmap.attrs['Process_Machine'] = socket.gethostname()
        self.xrfmap.attrs['Process_ID'] = os.getpid()
        self.h5root.flush()

    def take_ownership(self):
        "claim ownershipf of file"
        if self.xrfmap is None:
            return
        self.xrfmap.attrs['Process_Machine'] = socket.gethostname()
        self.xrfmap.attrs['Process_ID'] = os.getpid()
        self.h5root.flush()

    def release_ownership(self):
        self.xrfmap.attrs['Process_Machine'] = ''
        self.xrfmap.attrs['Process_ID'] = 0
        self.xrfmap.attrs['Last_Row'] = self.last_row

    def check_ownership(self):
        return self.check_hostid()

    def check_hostid(self):
        """checks host and id of file:
        returns True if this process the owner of the file
        """
        if self.xrfmap is None:
            return
        attrs = self.xrfmap.attrs
        self.folder = attrs['Map_Folder']

        file_mach = attrs['Process_Machine']
        file_pid  = attrs['Process_ID']
        if len(file_mach) < 1 or file_pid < 1:
            self.claim_hostid()
            return True
        return (file_mach == socket.gethostname() and
                file_pid == os.getpid())

    def folder_has_newdata(self):
        # print("XRM_MAPFILE ", self.folder, isGSEXRM_MapFolder(self.folder))
        if self.folder is not None and isGSEXRM_MapFolder(self.folder):
            self.read_master()
            return (self.last_row < len(self.rowdata)-1)
        return False

    def read_master(self):
        "reads master file for toplevel scan info"
        if self.folder is None or not isGSEXRM_MapFolder(self.folder):
            return
        self.masterfile = os.path.join(nativepath(self.folder),
                                       self.MasterFile)
        mtime = int(os.stat(self.masterfile).st_mtime)
        #print "READ MASTER ", self.masterfile
        #if mtime <= self.masterfile_mtime:
        #    print "could skip reading master file, mtime too soon"
        #    print mtime, self.masterfile_mtime , abs(mtime- self.masterfile_mtime )
        #    # return
        self.masterfile_mtime = mtime
        try:
            header, rows = readMasterFile(self.masterfile)
        except IOError:
            raise GSEXRM_Exception(
                "cannot read Master file from '%s'" % self.masterfile)

        self.master_header = header
        self.rowdata = rows
        self.scan_version = '1.0'
        self.nrows_expected = None
        self.start_time = time.ctime()
        for line in header:
            words = line.split('=')
            if 'scan.starttime' in words[0].lower():
                self.start_time = words[1].strip()
            elif 'scan.version' in words[0].lower():
                self.scan_version = words[1].strip()
            elif 'scan.nrows_expected' in words[0].lower():
                self.nrows_expected = int(words[1].strip())

        self.folder_modtime = os.stat(self.masterfile).st_mtime
        self.stop_time = time.ctime(self.folder_modtime)

        cfile = FastMapConfig()
        cfile.Read(os.path.join(self.folder, self.ScanFile))
        self.mapconf = cfile.config

        if self.filename is None:
            self.filename = self.mapconf['scan']['filename']
        if not self.filename.endswith('.h5'):
            self.filename = "%s.h5" % self.filename

        mapconf = self.mapconf
        slow_pos = mapconf['slow_positioners']
        fast_pos = mapconf['fast_positioners']

        scanconf = mapconf['scan']
        self.dimension = scanconf['dimension']
        start = mapconf['scan']['start1']
        stop  = mapconf['scan']['stop1']
        step  = mapconf['scan']['step1']
        span = abs(stop-start)
        self.npts = int(abs(step*1.01 + span)/step)

        try:
            self.xrfdet_type = mapconf['xrf']['type'].lower()
        except:
            print 'Could not read xrf type'


        pos1 = scanconf['pos1']
        self.pos_addr = [pos1]
        self.pos_desc = [slow_pos[pos1]]
        # note: XPS gathering file now saving ONLY data for the fast axis
        self.ixaddr = 0

        if self.dimension > 1:
            yaddr = scanconf['pos2']
            self.pos_addr.append(yaddr)
            self.pos_desc.append(slow_pos[yaddr])

    def _det_group(self, det=None):
        "return  XRFMAP group for a detector"
        dgroup= 'detsum'
        if self.ndet is None:
            self.ndet =  self.xrfmap.attrs['N_Detectors']
        if det in range(1, self.ndet+1):
            dgroup = 'det%i' % det
        return self.xrfmap[dgroup]

    def get_energy(self, det=None):
        """return energy array for a detector"""
        group = self._det_group(det)
        return group['energy'].value

    def get_shape(self):
        """returns NY, NX shape of array data"""
        ny, nx, npos = self.xrfmap['positions/pos'].shape
        return ny, nx

    def get_mca_area(self, areaname, det=None, dtcorrect=True,
                     callback = None):
        """return XRF spectra as MCA() instance for
        spectra summed over a pre-defined area

        Parameters
        ---------
        areaname :   str       name of area
        det :        optional, None or int         index of detector
        dtcorrect :  optional, bool [True]         dead-time correct data

        Returns
        -------
        MCA object for XRF counts in area

        """

        try:
            area = self.get_area(areaname).value
        except:
            raise GSEXRM_Exception("Could not find area '%s'" % areaname)

        mapdat = self._det_group(det)
        ix, iy, nmca = mapdat['counts'].shape

        npix = len(np.where(area)[0])
        if npix < 1:
            return None
        sy, sx = [slice(min(_a), max(_a)+1) for _a in np.where(area)]
        xmin, xmax, ymin, ymax = sx.start, sx.stop, sy.start, sy.stop
        nx, ny = (xmax-xmin), (ymax-ymin)
        NCHUNKSIZE = 16384 # 8192
        use_chunks = nx*ny > NCHUNKSIZE
        step = int((nx*ny)/NCHUNKSIZE)

        if not use_chunks:
            try:
                if hasattr(callback , '__call__'):
                    callback(1, 1, nx*ny)
                counts = self.get_counts_rect(ymin, ymax, xmin, xmax,
                                           mapdat=mapdat, det=det, area=area,
                                           dtcorrect=dtcorrect)
            except MemoryError:
                use_chunks = True
        if use_chunks:
            counts = np.zeros(nmca)
            if nx > ny:
                for i in range(step+1):
                    x1 = xmin + int(i*nx/step)
                    x2 = min(xmax, xmin + int((i+1)*nx/step))
                    if x1 >= x2: break
                    if hasattr(callback , '__call__'):
                        callback(i, step, (x2-x1)*ny)
                    counts += self.get_counts_rect(ymin, ymax, x1, x2, mapdat=mapdat,
                                                det=det, area=area,
                                                dtcorrect=dtcorrect)
            else:
                for i in range(step+1):
                    y1 = ymin + int(i*ny/step)
                    y2 = min(ymax, ymin + int((i+1)*ny/step))
                    if y1 >= y2: break
                    if hasattr(callback , '__call__'):
                        callback(i, step, nx*(y2-y1))
                    counts += self.get_counts_rect(y1, y2, xmin, xmax, mapdat=mapdat,
                                                det=det, area=area,
                                                dtcorrect=dtcorrect)

        ltime, rtime = self.get_livereal_rect(ymin, ymax, xmin, xmax, det=det,
                                              dtcorrect=dtcorrect, area=area)
        return self._getmca(mapdat, counts, areaname, npixels=npix,
                            real_time=rtime, live_time=ltime)

    def get_mca_rect(self, ymin, ymax, xmin, xmax, det=None, dtcorrect=True):
        """return mca counts for a map rectangle, optionally

        Parameters
        ---------
        ymin :       int       low y index
        ymax :       int       high y index
        xmin :       int       low x index
        xmax :       int       high x index
        det :        optional, None or int         index of detector
        dtcorrect :  optional, bool [True]         dead-time correct data

        Returns
        -------
        MCA object for XRF counts in rectangle

        """

        mapdat = self._det_group(det)
        counts = self.get_counts_rect(ymin, ymax, xmin, xmax, mapdat=mapdat,
                                      det=det, dtcorrect=dtcorrect)
        name = 'rect(y=[%i:%i], x==[%i:%i])' % (ymin, ymax, xmin, xmax)
        npix = (ymax-ymin+1)*(xmax-xmin+1)
        ltime, rtime = self.get_livereal_rect(ymin, ymax, xmin, xmax, det=det,
                                              dtcorrect=dtcorrect, area=None)

        return self._getmca(mapdat, counts, name, npixels=npix,
                            real_time=rtime, live_time=ltime)


    def get_counts_rect(self, ymin, ymax, xmin, xmax, mapdat=None, det=None,
                     area=None, dtcorrect=True):
        """return counts for a map rectangle, optionally
        applying area mask and deadtime correction

        Parameters
        ---------
        ymin :       int       low y index
        ymax :       int       high y index
        xmin :       int       low x index
        xmax :       int       high x index
        mapdat :     optional, None or map data
        det :        optional, None or int         index of detector
        dtcorrect :  optional, bool [True]         dead-time correct data
        area :       optional, None or area object  area for mask

        Returns
        -------
        ndarray for XRF counts in rectangle

        Does *not* check for errors!

        Note:  if mapdat is None, the map data is taken from the 'det' parameter
        """
        if mapdat is None:
            mapdat = self._det_group(det)


        nx, ny = (xmax-xmin, ymax-ymin)
        sx = slice(xmin, xmax)
        sy = slice(ymin, ymax)

        ix, iy, nmca = mapdat['counts'].shape
        cell   = mapdat['counts'].regionref[sy, sx, :]

        counts = mapdat['counts'][cell]
        counts = counts.reshape(ny, nx, nmca)

        if dtcorrect and det in range(1, self.ndet+1):
            cell   = mapdat['dtfactor'].regionref[sy, sx]
            dtfact = mapdat['dtfactor'][cell].reshape(ny, nx)
            dtfact = dtfact.reshape(dtfact.shape[0], dtfact.shape[1], 1)
            counts = counts * dtfact

        if area is not None:
            counts = counts[area[sy, sx]]
        else:
            counts = counts.sum(axis=0)
        return counts.sum(axis=0)

    def get_livereal_rect(self, ymin, ymax, xmin, xmax, det=None,
                          area=None, dtcorrect=True):
        """return livetime, realtime for a map rectangle, optionally
        applying area mask and deadtime correction

        Parameters
        ---------
        ymin :       int       low y index
        ymax :       int       high y index
        xmin :       int       low x index
        xmax :       int       high x index
        det :        optional, None or int         index of detector
        dtcorrect :  optional, bool [True]         dead-time correct data
        area :       optional, None or area object  area for mask

        Returns
        -------
        realtime, livetime in seconds

        Does *not* check for errors!

        """
        # need real size, not just slice values, for np.zeros()
        shape = self._det_group(1)['livetime'].shape
        if ymax < 0: ymax += shape[0]
        if xmax < 0: xmax += shape[1]
        nx, ny = (xmax-xmin, ymax-ymin)
        sx = slice(xmin, xmax)
        sy = slice(ymin, ymax)
        if det is None:
            livetime = np.zeros((ny, nx))
            realtime = np.zeros((ny, nx))
            for d in range(1, self.ndet+1):
                dmap = self._det_group(d)
                livetime += dmap['livetime'][sy, sx]
                realtime += dmap['realtime'][sy, sx]
            livetime /= (1.0*self.ndet)
            realtime /= (1.0*self.ndet)
        else:
            dmap = self._det_group(det)
            livetime = dmap['livetime'][sy, sx]
            realtime = dmap['realtime'][sy, sx]
        if area is not None:
            livetime = livetime[area[sy, sx]]
            realtime = realtime[area[sy, sx]]

        livetime = 1.e-6*livetime.sum()
        realtime = 1.e-6*realtime.sum()
        return livetime, realtime

    def _getmca(self, map, counts, name, npixels=None, **kws):
        """return an MCA object for a detector group
        (map is one of the  'det1', ... 'detsum')
        with specified counts array and a name


        Parameters
        ---------
        det :        detector object (one of det1, det2, ..., detsum)
        counts :     ndarray array of counts
        name  :      name for MCA

        Returns
        -------
        MCA object

        """
        # map  = self.xrfmap[dgroup]
        cal  = map['energy'].attrs
        _mca = MCA(counts=counts, offset=cal['cal_offset'],
                   slope=cal['cal_slope'], **kws)
        
        _mca.energy =  map['energy'].value
        env_names = list(self.xrfmap['config/environ/name'])
        env_addrs = list(self.xrfmap['config/environ/address'])
        env_vals  = list(self.xrfmap['config/environ/value'])
        for desc, val, addr in zip(env_names, env_vals, env_addrs):
            _mca.add_environ(desc=desc, val=val, addr=addr)

        if npixels is not None:
            _mca.npixels=npixels
            
        # a workaround for poor practice -- some '1.3.0' files
        # were built with 'roi_names', some with 'roi_name'
        roiname = 'roi_name'
        if roiname not in map:
            roiname = 'roi_names'
        roinames = list(map[roiname])
        roilims  = list(map['roi_limits'])
        for roi, lims in zip(roinames, roilims):
            _mca.add_roi(roi, left=lims[0], right=lims[1])
        _mca.areaname = _mca.title = name
        path, fname = os.path.split(self.filename)
        _mca.filename = fname
        fmt = "Data from File '%s', detector '%s', area '%s'"
        mapname = map.name.split('/')[-1]
        _mca.info  =  fmt % (self.filename, mapname, name)
        return _mca


    def get_pos(self, name, mean=True):
        """return  position by name (matching 'roimap/pos_name' if
        name is a string, or using name as an index if it is an integer

        Parameters
        ---------
        name :       str    ROI name
        mean :       optional, bool [True]        return mean x-value

        with mean=True, and a positioner in the first two position,
        returns a 1-d array of mean x-values

        with mean=False, and a positioner in the first two position,
        returns a 2-d array of x values for each pixel
        """
        index = -1
        if isinstance(name, int):
            index = name
        else:
            for ix, nam in enumerate(self.xrfmap['positions/name']):
                if nam.lower() == nam.lower():
                    index = ix
                    break

        if index == -1:
            raise GSEXRM_Exception("Could not find position '%s'" % repr(name))
        pos = self.xrfmap['positions/pos'][:, :, index]
        if index in (0, 1) and mean:
            pos = pos.sum(axis=index)/pos.shape[index]
        return pos

    def get_roimap(self, name, det=None, no_hotcols=True, dtcorrect=True):
        """extract roi map for a pre-defined roi by name

        Parameters
        ---------
        name :       str    ROI name
        det  :       optional, None or int [None]  index for detector
        dtcorrect :  optional, bool [True]         dead-time correct data
        no_hotcols   optional, bool [True]         suprress hot columns

        Returns
        -------
        ndarray for ROI data
        """
        imap = -1
        roi_names = [r.lower() for r in self.xrfmap['config/rois/name']]
        det_names = [r.lower() for r in self.xrfmap['roimap/sum_name']]
        dat = 'roimap/sum_raw'

        # scaler, non-roi data
        if name.lower() in det_names and name.lower() not in roi_names:
            imap = det_names.index(name.lower())
            if no_hotcols:
                return self.xrfmap[dat][:, 1:-1, imap]
            else:
                return self.xrfmap[dat][:, :, imap]

        dat = 'roimap/sum_raw'
        if dtcorrect:
            dat = 'roimap/sum_cor'

        if self.ndet is None:
            self.ndet =  self.xrfmap.attrs['N_Detectors']

        if det in range(1, self.ndet+1):
            name = '%s (mca%i)' % (name, det)
            det_names = [r.lower() for r in self.xrfmap['roimap/det_name']]
            dat = 'roimap/det_raw'
            if dtcorrect:
                dat = 'roimap/det_cor'

        imap = det_names.index(name.lower())
        if imap < 0:
            raise GSEXRM_Exception("Could not find ROI '%s'" % name)

        if no_hotcols:
            return self.xrfmap[dat][:, 1:-1, imap]
        else:
            return self.xrfmap[dat][:, :, imap]

    def get_map_erange(self, det=None, dtcorrect=True,
                       emin=None, emax=None, by_energy=True):
        """extract map for an ROI set here, by energy range:

        not implemented
        """
        pass

    def get_rgbmap(self, rroi, groi, broi, det=None, no_hotcols=True,
                   dtcorrect=True, scale_each=True, scales=None):
        """return a (NxMx3) array for Red, Green, Blue from named
        ROIs (using get_roimap).

        Parameters
        ----------
        rroi :       str    name of ROI for red channel
        groi :       str    name of ROI for green channel
        broi :       str    name of ROI for blue channel
        det  :       optional, None or int [None]  index for detector
        dtcorrect :  optional, bool [True]         dead-time correct data
        no_hotcols   optional, bool [True]         suprress hot columns
        scale_each : optional, bool [True]
                     scale each map separately to span the full color range.
        scales :     optional, None or 3 element tuple [None]
                     multiplicative scale for each map.

        By default (scales_each=True, scales=None), each map is scaled by
        1.0/map.max() -- that is 1 of the max value for that map.

        If scales_each=False, each map is scaled by the same value
        (1/max intensity of all maps)

        """
        rmap = self.get_roimap(rroi, det=det, no_hotcols=no_hotcols,
                               dtcorrect=dtcorrect)
        gmap = self.get_roimap(groi, det=det, no_hotcols=no_hotcols,
                               dtcorrect=dtcorrect)
        bmap = self.get_roimap(broi, det=det, no_hotcols=no_hotcols,
                               dtcorrect=dtcorrect)

        if scales is None or len(scales) != 3:
            scales = (1./rmap.max(), 1./gmap.max(), 1./bmap.max())
        if scale_each:
            rmap *= scales[0]
            gmap *= scales[1]
            bmap *= scales[2]
        else:
            scale = min(scales[0], scales[1], scales[2])
            rmap *= scale
            bmap *= scale
            gmap *= scale

        return np.array([rmap, gmap, bmap]).swapaxes(0, 2).swapaxes(0, 1)

    def add_roi(self, name, high, low,  address='', det=1,
                overwrite=False, **kws):
        """add named ROI to an XRFMap file.
        These settings will be propogated through the
        ROI maps and all detectors.

        """
        # data structures affected:
        #   config/rois/address
        #   config/rois/name
        #   config/rois/limits
        #   roimap/det_address
        #   roimap/det_name
        #   roimap/det_raw
        #   roimap/det_cor
        #   roimap/sum_list
        #   roimap/sum_name
        #   roimap/sum_raw
        #   roimap/sum_cor
        #   det{I}/roi_address      for I = 1, N_detectors (xrfmap attribute)
        #   det{I}/roi_name         for I = 1, N_detectors (xrfmap attribute)
        #   det{I}/roi_limits       for I = 1, N_detectors (xrfmap attribute)
        #   detsum/roi_address      for I = 1, N_detectors (xrfmap attribute)
        #   detsum/roi_name         for I = 1, N_detectors (xrfmap attribute)
        #   detsum/roi_limits       for I = 1, N_detectors (xrfmap attribute)

        roi_names = [i.lower().strip() for i in self.xrfmap['config/rois/name']]
        if name.lower().strip() in roi_name:
            if overwrite:
                self.del_roi(name)
            else:
                print("An ROI named '%s' exists, use overwrite=True to overwrite" % name)
                return
        #

    def del_roi(self, name):
        """ delete an ROI"""
        roi_names = [i.lower().strip() for i in self.xrfmap['config/rois/name']]
        if name.lower().strip() not in roi_name:
            print("No ROI named '%s' found to delete" % name)
            return
        iroi = roi_name.index(name.lower().strip())
        roi_names = [i in self.xrfmap['config/rois/name']]
        roi_names.pop(iroi)

def read_xrfmap(filename, root=None):
    """read GSE XRM FastMap data from HDF5 file or raw map folder"""
    key = 'filename'
    if os.path.isdir(filename):
        key = 'folder'
    kws = {key: filename, 'root': root}
    return GSEXRM_MapFile(**kws)

def registerLarchPlugin():
    return ('_xrf', {'read_xrfmap': read_xrfmap})

