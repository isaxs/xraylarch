"""
setup.py script for cx_Freeze

Usage:
    python setup_freeze.py bdist_mac --iconfile=GSEMap.icns

then use disk utility to make a .dmg
"""

from cx_Freeze import setup, Executable

import os, sys
import wx
import wx.lib.agw.flatnotebook
import wx.dataview as dv
import numpy, scipy, matplotlib
matplotlib.use('WXAgg')
import sqlalchemy

from scipy.sparse.csgraph import _validation
import scipy.sparse.linalg.dsolve.umfpack.umfpack
# import scipy.special._ufuncs
# from  scipy.special import wofz, _ufuncs_cxx, _cephes
from scipy.special import *
from scipy.integrate import _ode, vode, lsoda, _dop, _odepack, _quadpack
from scipy.optimize import *
from  scipy.io import netcdf
netcdf_open = netcdf.netcdf_file

import wxmplot
from wxmplot.plotframe import PlotFrame
import larch

import epics
import Carbon
import h5py

try:
    import Image
except ImportError:
    import PIL as Image
        

print 'Found all Imports'

libca = epics.ca.initialize_libca()
mpl_data_files = matplotlib.get_py2exe_datafiles()

resource_files = ['../COPYING']
## also: use
##  if getattr(sys, 'frozen', None) == 'macosx_app':
## in lib/site_config.py to specify plugin path

plugin_files = []
psrc = "/usr/share/larch"
for dname in os.listdir('%s/plugins' % psrc):
    pname = '%s/plugins/%s' % (psrc, dname)
    if os.path.isdir(pname):
        flist = ['%s/%s' % (pname, f) for f in os.listdir(pname) if not f.endswith('.pyc')]
        plugin_files.append(("larch/plugins/%s" % dname, flist))


dll_files = [("larch/dlls/darwin/",
              ['%s/dlls/darwin/%s' % (psrc, f) for f in os.listdir('%s/dlls/darwin' % psrc)]
              )]

DATA_FILES = []

exe_opts = {# 'packages': ['wx', 'numpy','sqlalchemy'],
            'includes': ['Carbon', 'Carbon.Appearance', 'ConfigParser',
                         'Image', 'ctypes', 'epics', 'epics.devices',
                         'fpformat', 'h5py',
                         'h5py._objects',
                         'h5py._proxy', 'h5py.defs', 'h5py.utils',
                         'larch.wxlib', 
                         'matplotlib', 'numpy', 'scipy',
                         'scipy.constants', 'scipy.fftpack',
                         'scipy.special', 'scipy.special._cephes',
                         'scipy.special._ufuncs',
                         'scipy.integrate._ode', 'scipy.integrate.vode','scipy.integrate.lsoda',
                         'scipy.special._ufuncs_cxx',
                         'scipy.io.matlab.mio5_utils',
                         'scipy.sparse.linalg.dsolve',
                         'scipy.sparse.linalg.dsolve.umfpack.umfpack',
                         'scipy.io.matlab.streams', 'scipy.io.netcdf',
                         'scipy.optimize', 'scipy.signal',
                         'skimage', 'skimage.exposure', 'wxutils',
                         'scipy.sparse.csgraph._validation', 'sqlalchemy',
                         'sqlalchemy.dialects.sqlite', 'sqlalchemy.orm',
                         'sqlalchemy.pool', 'sqlite3', 'wx', 'wx._core',
                         'wx.lib', 'wx.lib.agw', 'wx.lib.agw.flatnotebook',
                         'wx.lib.colourselect', 'wx.lib.masked',
                         'wx.lib.mixins', 'wx.lib.mixins.inspection',
                         'wx.lib.agw.pycollapsiblepane',
                         'wx.lib.newevent', 'wx.py',
                         'wx.dataview',
                         'wxmplot', 'wxversion',
                         'xdrlib', 'xml.etree', 'xml.etree.cElementTree'],
            'excludes': ['Tkinter', '_tkinter', 'Tkconstants', 'tcl',
                         '_imagingtk', 'Image._imagingtk', 'ImageTk',
                         'Image.ImageTk', 'FixTk''_gtkagg', '_tkagg',
                         'matplotlib.tests',
                         'qt', 'PyQt4Gui', 'email', 'IPython'],
            # 'iconfile': 'GSEMap.icns',
            }

appname = 'GSEMapViewer'
appvers = '1.2'
setup(name = appname,
      version = appvers,
      description = "GSECARS XRM MapViewer",
      options = {'build_exe': exe_opts},
      data_files = mpl_data_files + dll_files, ##  + plugin_files
      executables = [Executable('../bin/GSE_MapViewer', base=None)])

contents = 'build/%s-%s.app/Contents' % (appname, appvers)
contents = contents.replace(' ', '\ ')

def sys(cmd):
    print ' >> ', cmd
    os.system(cmd)

sys("cp -pr GSEMap.icns  %s/Resources/." % contents)
sys("cp -pr ../dlls/darwin/* %s/MacOS/." % contents)

try:
    os.makedirs("%s/Resources/larch/" % contents)
except:
    pass

for subdir in ('modules', 'plugins', 'dlls'):
    sys("cp -pr ../%s   %s/Resources/larch/." % (subdir, contents))

